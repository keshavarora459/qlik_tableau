import asyncio
import json
import logging
import os
import random
import re
from typing import Any, Dict, List, Optional

try:
    from groq import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AsyncGroq,
        RateLimitError,
    )
except ImportError:
    class _MockChatCompletions:
        async def create(self, *args, **kwargs):
            raise RuntimeError("The 'groq' package is not installed. Please install it using 'pip install groq'.")

    class _MockChat:
        def __init__(self):
            self.completions = _MockChatCompletions()

    class AsyncGroq:
        def __init__(self, *args, **kwargs):
            self.chat = _MockChat()
    class APIConnectionError(Exception): pass
    class APIStatusError(Exception): pass
    class APITimeoutError(Exception): pass
    class RateLimitError(Exception): pass

from config import Config
from common.llm import redact_sensitive_data
from src.converters.llm_cache import get_llm_cache
from services import llm_usage

logger = logging.getLogger(__name__)

# Errors worth retrying: rate limits, transient connectivity/timeouts, and 5xx responses.
_RETRYABLE_EXCEPTIONS = (RateLimitError, APIConnectionError, APITimeoutError)

# Pattern to extract wait duration from Groq rate-limit messages
# e.g. "Please try again in 4.24s" or "Please try again in 1m28s"
_WAIT_SECONDS_RE = re.compile(r"try again in\s+(?:(\d+(?:\.\d+)?)\s*s|(\d+)\s*m\s*(\d+(?:\.\d+)?)\s*s)", re.IGNORECASE)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        return status is not None and (status == 429 or status >= 500)
    return False


def _extract_retry_delay(exc: Exception, attempt: int) -> float:
    """Extract retry delay from headers or message, with fallback to exponential backoff."""
    # 1. Check Retry-After header
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response is not None else None
    if header:
        try:
            return min(float(header), 10.0)
        except (TypeError, ValueError):
            pass

    # 2. Check message text for Groq specific duration
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
                total = float(m_str) * 60 + float(m_sec_str or 0)
                # If wait is very long (>15s), cap at 10s or fail fast
                return min(total, 10.0)
            except ValueError:
                pass

    # 3. Fallback to exponential backoff with jitter
    delay = Config.RETRY_DELAY * (2 ** attempt)
    delay += random.uniform(0, delay * 0.3)
    return min(max(delay, 1.0), 8.0)


def _clamp_input_tokens(messages: List[Dict[str, str]], max_tokens: int = 3000) -> List[Dict[str, str]]:
    """Ensure message payload does not exceed configured input token limit (rough character estimation)."""
    # 1 token ~= 4 characters in English
    char_budget = max_tokens * 4
    total_chars = sum(len(m.get("content", "")) for m in messages)
    if total_chars <= char_budget:
        return messages

    # If exceeding, trim system prompt first while leaving user payload intact
    clamped = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role == "system" and len(content) > 2000:
            # Keep first 2000 characters of system instructions
            content = content[:2000] + "\n...(rules compacted for brevity)"
        clamped.append({"role": role, "content": content})
    return clamped


_LLM_SEMAPHORE = asyncio.Semaphore(Config.LLM_MAX_CONCURRENCY)


class GroqLLMClient:
    """Async client for Groq API with concurrency control, token limits, and caching."""

    _instance: Optional["GroqLLMClient"] = None

    def __new__(cls, *args, **kwargs):
        """Singleton pattern to avoid repeated client re-initialization."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self.api_key = Config.GROQ_API_KEY
        self.client = AsyncGroq(
            api_key=self.api_key or "unconfigured_key",
            timeout=float(os.getenv("LLM_TIMEOUT", "10.0")),
        )
        self.model = Config.GROQ_MODEL
        self.cache = get_llm_cache()
        self._initialized = True

    @property
    def semaphore(self) -> asyncio.Semaphore:
        global _LLM_SEMAPHORE
        return _LLM_SEMAPHORE

    async def _complete(
        self,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict[str, str]] = None,
        expect_json: bool = False,
        stage: str = "general",
    ) -> Any:
        """One chat completion with retry/backoff, concurrency capping, and token limits."""
        Config.validate()
        last_exc: Optional[Exception] = None

        # Clamp input message size to prevent blowing token budget
        clamped_messages = _clamp_input_tokens(messages, Config.LLM_MAX_INPUT_TOKENS)

        for attempt in range(Config.MAX_RETRIES + 1):
            try:
                kwargs: Dict[str, Any] = {
                    "model": self.model,
                    "messages": clamped_messages,
                    "temperature": 0.0,
                    "max_tokens": Config.LLM_MAX_OUTPUT_TOKENS,
                }
                if response_format:
                    kwargs["response_format"] = response_format

                async with self.semaphore:
                    response = await self.client.chat.completions.create(**kwargs)

                content = response.choices[0].message.content
                if not expect_json:
                    return content or ""

                try:
                    return json.loads(content)
                except json.JSONDecodeError as parse_exc:
                    last_exc = parse_exc
                    logger.warning(
                        "Groq response was not valid JSON on attempt %d/%d: %s",
                        attempt + 1, Config.MAX_RETRIES + 1, redact_sensitive_data(parse_exc),
                    )
            except Exception as exc:  # noqa: BLE001 - reclassified below
                last_exc = exc
                redacted_exc = redact_sensitive_data(exc)
                is_rate_limit = isinstance(exc, RateLimitError) or (isinstance(exc, APIStatusError) and getattr(exc, "status_code", 0) == 429)

                if is_rate_limit:
                    llm_usage.current().record_rate_limited(stage, str(exc))

                if not _is_retryable(exc) or attempt == Config.MAX_RETRIES:
                    logger.error("Error calling Groq API (%s): %s", "rate_limit" if is_rate_limit else "api_error", redacted_exc)
                    raise

                delay = _extract_retry_delay(last_exc, attempt)
                logger.warning(
                    "Retryable Groq error on attempt %d/%d; waiting %.2fs: %s",
                    attempt + 1, Config.MAX_RETRIES + 1, delay, redacted_exc,
                )
                await asyncio.sleep(delay)

            if attempt == Config.MAX_RETRIES:
                break

        logger.error("Groq API call failed after %d attempts: %s", Config.MAX_RETRIES + 1, last_exc)
        raise last_exc

    async def generate_structured_response(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict,
        stage: str = "general",
    ) -> Dict[str, Any]:
        """Calls Groq API to return a structured JSON object with caching."""
        cache_key = self.cache.make_key(
            self.model, f"structured_{stage}", user_prompt, extra_context=system_prompt[:500]
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            llm_usage.current().record_cache_hit(stage)
            return cached

        schema_str = json.dumps(json_schema, indent=2)
        full_system_prompt = (
            f"{system_prompt}\n\nReturn only valid JSON matching this schema:\n{schema_str}"
        )
        result = await self._complete(
            messages=[
                {"role": "system", "content": full_system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            expect_json=True,
            stage=stage,
        )
        if result and isinstance(result, dict):
            self.cache.set(cache_key, result)
        return result

    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        stage: str = "general",
    ) -> str:
        """Calls Groq API for a plain-text answer with caching."""
        cache_key = self.cache.make_key(
            self.model, f"text_{stage}", user_prompt, extra_context=system_prompt[:500]
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            llm_usage.current().record_cache_hit(stage)
            return cached

        content = await self._complete(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            expect_json=False,
            stage=stage,
        )
        cleaned = (content or "").strip()
        if cleaned:
            self.cache.set(cache_key, cleaned)
        return cleaned


def get_llm_client() -> GroqLLMClient:
    return GroqLLMClient()
