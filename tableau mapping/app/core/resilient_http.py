"""
Resilient HTTP client wrapper with timeouts, retries, and circuit breakers.

Provides both sync (requests-based) and async (httpx-based) clients that
automatically apply:
  - Configurable connect / read timeouts
  - Capped exponential backoff with jitter for retries
  - Per-host circuit breaker that fails fast when a dependency is down

Usage:
    from app.core.resilient_http import resilient_get, resilient_post, resilient_async_post

    # Sync GET with retries
    response = resilient_get("https://api.example.com/data", headers={...})

    # Sync POST with retries
    response = resilient_post("https://api.example.com/write", json={...}, headers={...})

    # Async POST
    response = await resilient_async_post("https://api.example.com/write", json={...})
"""

import logging
import random
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx
import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------
class CircuitBreaker:
    """
    Per-host circuit breaker.

    States:
      CLOSED  — normal operation, requests pass through
      OPEN    — dependency is down, requests fail immediately
      HALF_OPEN — allow one probe request to test recovery

    Transitions:
      CLOSED → OPEN: after `failure_threshold` consecutive failures
      OPEN → HALF_OPEN: after `recovery_timeout` seconds
      HALF_OPEN → CLOSED: if the probe request succeeds
      HALF_OPEN → OPEN: if the probe request fails
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        failure_threshold: int = 15,
        recovery_timeout: float = 60.0,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN:
                # Check if recovery timeout has elapsed
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = self.HALF_OPEN
            return self._state

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._failure_count >= self.failure_threshold:
                self._state = self.OPEN

    def allow_request(self) -> bool:
        current_state = self.state
        return current_state in (self.CLOSED, self.HALF_OPEN)


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is open and the request is rejected."""
    pass


# ---------------------------------------------------------------------------
# Global circuit breaker registry (per host)
# ---------------------------------------------------------------------------
_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def _get_breaker(url: str) -> CircuitBreaker:
    """Get or create a circuit breaker for the given URL's host."""
    host = urlparse(url).netloc or url
    with _breakers_lock:
        if host not in _breakers:
            _breakers[host] = CircuitBreaker()
        return _breakers[host]


# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------
_DEFAULT_CONNECT_TIMEOUT = 5.0   # seconds
_DEFAULT_READ_TIMEOUT = 30.0     # seconds
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 1.0      # seconds
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRYABLE_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "POST"}  # Include POST for idempotent upserts


def _should_retry(method: str, status_code: int | None, attempt: int, max_retries: int) -> bool:
    """Determine if a request should be retried."""
    if attempt >= max_retries:
        return False

    if status_code is not None and status_code in _RETRYABLE_STATUS_CODES:
        return True

    # Only retry on connection errors for idempotent methods
    if status_code is None and method.upper() in _RETRYABLE_METHODS:
        return True

    return False


def _compute_backoff(attempt: int, base: float = _DEFAULT_BACKOFF_BASE) -> float:
    """Compute backoff with jitter: base * 2^attempt + random jitter."""
    wait = base * (2 ** attempt)
    jitter = wait * random.uniform(-0.25, 0.25)
    return max(0.5, wait + jitter)


# ---------------------------------------------------------------------------
# Synchronous resilient client (requests-based)
# ---------------------------------------------------------------------------
def resilient_get(
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    timeout: tuple[float, float] | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    use_circuit_breaker: bool = True,
) -> requests.Response:
    """Resilient GET with retries, timeouts, and circuit breaker."""
    return _resilient_request(
        method="GET",
        url=url,
        headers=headers,
        params=params,
        timeout=timeout,
        max_retries=max_retries,
        use_circuit_breaker=use_circuit_breaker,
    )


def resilient_post(
    url: str,
    *,
    json: Any | None = None,
    data: Any | None = None,
    headers: dict | None = None,
    timeout: tuple[float, float] | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    use_circuit_breaker: bool = True,
) -> requests.Response:
    """Resilient POST with retries, timeouts, and circuit breaker."""
    return _resilient_request(
        method="POST",
        url=url,
        json=json,
        data=data,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
        use_circuit_breaker=use_circuit_breaker,
    )


def _resilient_request(
    method: str,
    url: str,
    *,
    json: Any | None = None,
    data: Any | None = None,
    headers: dict | None = None,
    params: dict | None = None,
    timeout: tuple[float, float] | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    use_circuit_breaker: bool = True,
) -> requests.Response:
    """Internal: execute a resilient HTTP request."""
    if timeout is None:
        timeout = (_DEFAULT_CONNECT_TIMEOUT, _DEFAULT_READ_TIMEOUT)

    breaker = _get_breaker(url) if use_circuit_breaker else None

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        # Circuit breaker check
        if breaker and not breaker.allow_request():
            raise CircuitOpenError(
                f"Circuit breaker OPEN for {urlparse(url).netloc}. "
                f"Request rejected to prevent cascade failure."
            )

        try:
            response = requests.request(
                method=method,
                url=url,
                json=json,
                data=data,
                headers=headers,
                params=params,
                timeout=timeout,
            )

            # Check for retryable status codes
            if _should_retry(method, response.status_code, attempt, max_retries):
                # Respect Retry-After header if present
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_time = float(retry_after)
                    except ValueError:
                        wait_time = _compute_backoff(attempt)
                else:
                    wait_time = _compute_backoff(attempt)

                logger.warning(
                    f"Retryable status {response.status_code} from {method} {url}. "
                    f"Waiting {wait_time:.1f}s (attempt {attempt + 1}/{max_retries + 1})"
                )
                if breaker and response.status_code != 429:
                    breaker.record_failure()
                time.sleep(wait_time)
                continue

            # Success or non-retryable error
            if breaker:
                breaker.record_success()
            return response

        except requests.exceptions.RequestException as exc:
            last_error = exc
            if breaker:
                breaker.record_failure()

            if _should_retry(method, None, attempt, max_retries):
                wait_time = _compute_backoff(attempt)
                logger.warning(
                    f"Connection error for {method} {url}: {exc}. "
                    f"Retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries + 1})"
                )
                time.sleep(wait_time)
                continue

            raise

    # All retries exhausted
    raise last_error or requests.exceptions.ConnectionError(
        f"Request to {url} failed after {max_retries + 1} attempts"
    )


# ---------------------------------------------------------------------------
# Async resilient client (httpx-based)
# ---------------------------------------------------------------------------
# Shared async client — recreated when the event loop changes or closes
_async_client: httpx.AsyncClient | None = None
_async_client_loop: object | None = None  # tracks which loop owns the client


def _get_async_client() -> httpx.AsyncClient:
    """Get or create the shared async HTTP client for the *current* event loop."""
    import asyncio

    global _async_client, _async_client_loop

    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    # Recreate if: no client, client closed, or loop changed
    needs_new = (
        _async_client is None
        or _async_client.is_closed
        or _async_client_loop is not current_loop
    )

    if needs_new:
        # Drop the old client reference; Python GC will clean up connections.
        # Do NOT try to aclose() here — the old loop may already be closed.

        _async_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                _DEFAULT_READ_TIMEOUT,
                connect=_DEFAULT_CONNECT_TIMEOUT,
            ),
            follow_redirects=True,
        )
        _async_client_loop = current_loop

    return _async_client


async def resilient_async_post(
    url: str,
    *,
    json: Any | None = None,
    headers: dict | None = None,
    timeout: httpx.Timeout | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    use_circuit_breaker: bool = True,
) -> httpx.Response:
    """Async resilient POST with retries, timeouts, and circuit breaker."""
    import asyncio

    effective_timeout = timeout or httpx.Timeout(
        _DEFAULT_READ_TIMEOUT, connect=_DEFAULT_CONNECT_TIMEOUT
    )

    breaker = _get_breaker(url) if use_circuit_breaker else None
    last_error: Exception | None = None
    client = _get_async_client()

    for attempt in range(max_retries + 1):
        # Circuit breaker check
        if breaker and not breaker.allow_request():
            raise CircuitOpenError(
                f"Circuit breaker OPEN for {urlparse(url).netloc}. "
                f"Request rejected to prevent cascade failure."
            )

        try:
            response = await client.post(
                url, json=json, headers=headers, timeout=effective_timeout
            )

            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < max_retries:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_time = float(retry_after)
                    except ValueError:
                        wait_time = _compute_backoff(attempt)
                else:
                    wait_time = _compute_backoff(attempt)

                logger.warning(
                    f"Retryable status {response.status_code} from POST {url}. "
                    f"Waiting {wait_time:.1f}s (attempt {attempt + 1}/{max_retries + 1})"
                )
                if breaker and response.status_code != 429:
                    breaker.record_failure()
                await asyncio.sleep(wait_time)
                continue

            if breaker:
                breaker.record_success()
            return response

        except (httpx.RequestError, httpx.TimeoutException) as exc:
            last_error = exc
            if breaker:
                breaker.record_failure()

            if attempt < max_retries:
                wait_time = _compute_backoff(attempt)
                logger.warning(
                    f"Connection error for POST {url}: {exc}. "
                    f"Retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries + 1})"
                )
                await asyncio.sleep(wait_time)
                continue

            raise

    raise last_error or httpx.RequestError(
        f"Request to {url} failed after {max_retries + 1} attempts"
    )
