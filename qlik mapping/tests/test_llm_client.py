"""GroqLLMClient previously had no retry/backoff and no concurrency cap, so
a single rate-limited Groq call would raise immediately, and a burst of
concurrent dashboard-visual conversions could flood a low-quota key with
unlimited simultaneous requests.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("GROQ_API_KEY", "test-key")

import httpx
from groq import RateLimitError

import config
import src.converters.llm_client as llm_client_mod
from src.converters.llm_client import GroqLLMClient


def _rate_limit_error():
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(429, request=request, headers={})
    return RateLimitError("rate limited", response=response, body=None)


def _fake_response(payload):
    msg = MagicMock()
    msg.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]
    return msg


import pytest
from src.converters.llm_cache import get_llm_cache

@pytest.fixture(autouse=True)
def reset_cache():
    get_llm_cache().clear()
    yield
    get_llm_cache().clear()


def test_retries_transient_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr(config.Config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(config.Config, "RETRY_DELAY", 0.001)
    monkeypatch.setattr(config.Config, "MAX_RETRIES", 2)
    client = GroqLLMClient()
    attempts = {"n": 0}

    async def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _rate_limit_error()
        return _fake_response({"ok": True})

    async def run():
        with patch.object(client.client.chat.completions, "create", new=AsyncMock(side_effect=flaky)):
            return await client.generate_structured_response("sys", "user", {"type": "object"})

    result = asyncio.run(run())
    assert result == {"ok": True}
    assert attempts["n"] == 3


def test_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(config.Config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(config.Config, "RETRY_DELAY", 0.001)
    monkeypatch.setattr(config.Config, "MAX_RETRIES", 2)
    client = GroqLLMClient()
    attempts = {"n": 0}

    async def always_fails(*args, **kwargs):
        attempts["n"] += 1
        raise _rate_limit_error()

    async def run():
        with patch.object(client.client.chat.completions, "create", new=AsyncMock(side_effect=always_fails)):
            await client.generate_structured_response("sys", "user", {"type": "object"})

    try:
        asyncio.run(run())
        assert False, "should have raised"
    except RateLimitError:
        pass
    assert attempts["n"] == 3  # MAX_RETRIES + 1


def test_concurrent_calls_are_capped_by_semaphore(monkeypatch):
    monkeypatch.setattr(config.Config, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(config.Config, "LLM_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(llm_client_mod, "_LLM_SEMAPHORE", asyncio.Semaphore(2))
    client = GroqLLMClient()

    in_flight = {"n": 0, "max": 0}
    lock = asyncio.Lock()

    async def slow(*args, **kwargs):
        async with lock:
            in_flight["n"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["n"])
        await asyncio.sleep(0.05)
        async with lock:
            in_flight["n"] -= 1
        return _fake_response({"ok": True})

    async def run():
        with patch.object(client.client.chat.completions, "create", new=AsyncMock(side_effect=slow)):
            await asyncio.gather(*(
                client.generate_structured_response("sys", "user", {"type": "object"}) for _ in range(6)
            ))

    asyncio.run(run())
    assert in_flight["max"] <= 2
