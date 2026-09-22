import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional

try:
    from groq import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        Groq,
        RateLimitError,
    )
except ImportError:
    class _MockChatCompletions:
        def create(self, *args, **kwargs):
            raise RuntimeError("The 'groq' package is not installed. Please install it using 'pip install groq'.")

    class _MockChat:
        def __init__(self):
            self.completions = _MockChatCompletions()

    class Groq:
        def __init__(self, *args, **kwargs):
            self.chat = _MockChat()

    class APIConnectionError(Exception): pass
    class APIStatusError(Exception): pass
    class APITimeoutError(Exception): pass
    class RateLimitError(Exception): pass

from app.core.config import settings
from app.core.structured_logger import get_structured_logger
from .rules import ALL_PROMPTS_TABLEAU_TO_DAX

logger = get_structured_logger(__name__)


class LLMRateLimitError(Exception):
    """Raised when LLM rate limit is exceeded or daily quota is exhausted."""
    pass


class LLMResponseError(Exception):
    """Raised when LLM returns an unexpected payload shape."""
    pass


# ──────────────────────────────────────────────────────────────────
# Global concurrency limiter and cache (aligned with Qlik architecture)
# ──────────────────────────────────────────────────────────────────
_llm_semaphore = threading.Semaphore(settings.llm_max_concurrent_requests)
_llm_cache: Dict[str, str] = {}
_cache_lock = threading.Lock()

# Errors worth retrying: rate limits, transient connectivity/timeouts, and 5xx responses
_RETRYABLE_EXCEPTIONS = (RateLimitError, APIConnectionError, APITimeoutError)

# Pattern to extract wait duration from Groq rate-limit messages
# e.g. "Please try again in 4.24s" or "Please try again in 5m45.6s"
_WAIT_SECONDS_RE = re.compile(
    r"try again in\s+(?:(\d+(?:\.\d+)?)\s*s|(\d+)\s*m\s*(\d+(?:\.\d+)?)\s*s)",
    re.IGNORECASE,
)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        return status is not None and (status == 429 or status >= 500)
    return False


def _get_cache_key(model: str, system_prompts: str, user_prompt: str) -> str:
    raw = f"{model}:{system_prompts}:{user_prompt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clamp_input_tokens(messages: List[Dict[str, str]], max_tokens: int = 3000) -> List[Dict[str, str]]:
    """Ensure message payload does not exceed configured input token limit (rough character estimation)."""
    # 1 token ~= 4 characters in English
    char_budget = max_tokens * 4
    total_chars = sum(len(m.get("content", "")) for m in messages)
    if total_chars <= char_budget:
        return messages

    # Truncate system prompt first, keeping user prompt intact
    clamped = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role == "system" and len(content) > 2000:
            content = content[:2000] + "\n...(rules compacted for brevity)"
        clamped.append({"role": role, "content": content})
    return clamped


def _extract_retry_delay(exc: Exception, attempt: int, base_delay: float) -> float:
    """Extract retry delay from headers or message, with strict upper bound (max 10s) just like Qlik."""
    # 1. Check Retry-After header
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response is not None else None
    if header:
        try:
            return min(max(float(header), 1.0), 10.0)
        except (TypeError, ValueError):
            pass

    # 2. Check message body for Groq specific duration
    msg = str(exc)
    match = _WAIT_SECONDS_RE.search(msg)
    if match:
        sec_str, m_str, m_sec_str = match.groups()
        if sec_str:
            try:
                return min(max(float(sec_str), 1.0), 10.0)
            except ValueError:
                pass
        elif m_str:
            try:
                # If provider asks for minutes, cap at 10s or fail fast
                return 10.0
            except ValueError:
                pass

    # 3. Fallback to exponential backoff with jitter (max 8s)
    delay = base_delay * (2 ** attempt)
    delay += random.uniform(0, delay * 0.3)
    return min(max(delay, 1.0), 8.0)


class GroqLLMClient:
    """Synchronous client for Groq API with concurrency control, token limits, and caching."""

    _instance: Optional["GroqLLMClient"] = None
    _init_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._init_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._setup_client()
        self._initialized = True

    def _get_api_keys(self) -> List[str]:
        keys = []
        for k in [
            settings.groq_api_key,
            os.getenv("TABLEAU_GROQ_API_KEY"),
            os.getenv("QLIK_GROQ_API_KEY"),
            os.getenv("GROQ_API_KEY"),
        ]:
            if k and k != "unconfigured_key" and k not in keys:
                keys.append(k)
        return keys

    def _setup_client(self, api_key: Optional[str] = None):
        available_keys = self._get_api_keys()
        self.api_key = api_key or (available_keys[0] if available_keys else None)
        self.client = Groq(
            api_key=self.api_key or "unconfigured_key",
            timeout=float(settings.llm_timeout),
        )
        self.model = settings.groq_model

    def complete(
        self,
        system_prompts: str,
        user_prompt: str,
        retries: Optional[int] = None,
        backoff: Optional[float] = None,
    ) -> str:
        """Execute one completion with retry/backoff, concurrency limit, and token clamping."""
        if not self.api_key or self.api_key == "unconfigured_key":
            self._setup_client()

        if not self.api_key:
            raise ValueError("GROQ_API_KEY not configured for Tableau LLM")

        max_retries = retries if retries is not None else settings.llm_max_retries
        retry_delay = backoff if backoff is not None else settings.llm_retry_delay
        model = self.model

        # Check in-memory cache first
        cache_key = _get_cache_key(model, system_prompts, user_prompt)
        with _cache_lock:
            if cache_key in _llm_cache:
                return _llm_cache[cache_key]

        raw_messages = [
            {"role": "system", "content": system_prompts},
            {"role": "user", "content": user_prompt},
        ]
        clamped_messages = _clamp_input_tokens(raw_messages, settings.llm_max_input_tokens)

        last_exc: Optional[Exception] = None
        available_keys = self._get_api_keys()
        tried_keys = {self.api_key}

        for attempt in range(max_retries + 1):
            _llm_semaphore.acquire()
            try:
                response = self.client.chat.completions.create(
                    model=model,
                    messages=clamped_messages,
                    temperature=0.0,
                    max_tokens=settings.llm_max_output_tokens,
                )
                choices = (response.choices or [])
                if not choices:
                    raise LLMResponseError("No choices returned in Groq response")

                content = choices[0].message.content or ""
                trimmed = content.strip()
                if trimmed:
                    with _cache_lock:
                        _llm_cache[cache_key] = trimmed
                return trimmed

            except Exception as exc:
                last_exc = exc
                err_text = str(exc).lower()

                is_rate_limit = isinstance(exc, RateLimitError) or (
                    isinstance(exc, APIStatusError) and getattr(exc, "status_code", 0) == 429
                )

                # Detect daily quota exhaustion (e.g., 250 requests per day limit reached)
                has_daily_exhaustion = (
                    "requests per day" in err_text
                    or "limit 250" in err_text
                    or "try again in 5m" in err_text
                )

                if has_daily_exhaustion:
                    # Check if there is a backup key available to fail over to
                    untried = [k for k in available_keys if k not in tried_keys]
                    if untried:
                        next_key = untried[0]
                        logger.info("Failing over to backup Groq key after quota exhaustion...")
                        self._setup_client(api_key=next_key)
                        tried_keys.add(next_key)
                        continue

                if has_daily_exhaustion or (is_rate_limit and attempt >= max_retries):
                    logger.warning(
                        "Groq 429 rate limit / quota exhaustion detected. "
                        "Failing fast to trigger deterministic rule fallback: %s",
                        str(exc)[:180],
                    )
                    raise LLMRateLimitError(f"Groq 429 rate limit exceeded: {str(exc)[:200]}") from exc

                if not _is_retryable(exc) or attempt == max_retries:
                    logger.error("Groq API error on attempt %d/%d: %s", attempt + 1, max_retries + 1, exc)
                    raise

                delay = _extract_retry_delay(exc, attempt, retry_delay)
                logger.warning(
                    "Retryable Groq error on attempt %d/%d; waiting %.2fs: %s",
                    attempt + 1, max_retries + 1, delay, exc,
                )
                time.sleep(delay)

            finally:
                _llm_semaphore.release()

        raise LLMRateLimitError(f"Groq call failed after {max_retries + 1} attempts: {last_exc}") from last_exc



_client_instance = GroqLLMClient()


def call_llm(
    system_prompts: str,
    user_prompt: str,
    retries: Optional[int] = None,
    backoff: Optional[float] = None,
) -> str:
    """Entrypoint function calling the unified Groq LLM client (aligned with Qlik)."""
    return _client_instance.complete(
        system_prompts=system_prompts,
        user_prompt=user_prompt,
        retries=retries,
        backoff=backoff,
    )


def _get_prompt_by_type(prompt_type: str) -> str:
    """Internal helper to find the prompt template from rules.py"""
    for p in ALL_PROMPTS_TABLEAU_TO_DAX:
        if p.get("type") == prompt_type:
            return p["prompt"]

    logger.warning(f"No prompt found for type '{prompt_type}' → using fallback")
    return "Convert the following Tableau syntax to Power BI DAX: {expression}. Table: {table}"


def call_llm_with_rule(rule_type: str, **kwargs) -> str:
    prompt_template = _get_prompt_by_type(rule_type)
    try:
        prompt = prompt_template.format(**kwargs)
        system_instruction = "You are an expert Data Analyst converting Tableau syntax to Power BI DAX."
        return call_llm(
            system_prompts=system_instruction,
            user_prompt=prompt,
            retries=settings.llm_max_retries,
        )
    except Exception as e:
        logger.error(
            f"LLM call failed for rule '{rule_type}': {e}",
            extra={"error_type": type(e).__name__, "error_message": str(e)},
        )
        return f"/* LLM conversion failed - manual review needed */\n{str(kwargs)}"
