"""Unit tests for token optimization, request deduplication, caching, and rate-limit prevention."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from groq import RateLimitError, APIStatusError

import config
from config import Config
from src.converters.llm_cache import get_llm_cache
from src.converters.llm_client import GroqLLMClient, get_llm_client
from src.converters.measures.converter import MeasureConverter, classify_measure
from src.converters.mquery.converter import MQueryConverter, validate_mquery
from services import llm_usage


def _make_rate_limit_error(msg="Rate limit exceeded"):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(429, request=request, headers={"retry-after": "0.01"})
    return RateLimitError(msg, response=response, body=None)


def _fake_response(content):
    msg = MagicMock()
    msg.choices = [MagicMock(message=MagicMock(content=content))]
    return msg


@pytest.fixture(autouse=True)
def reset_llm_state():
    get_llm_cache().clear()
    llm_usage.start_run()
    Config._validated = False
    yield
    get_llm_cache().clear()


def test_measure_classification():
    """Verify SIMPLE vs COMPLEX classification."""
    assert classify_measure("Sum(Sales)") == "SIMPLE"
    assert classify_measure("Avg(Price)") == "SIMPLE"
    assert classify_measure("Count(Distinct CustomerID)") == "SIMPLE"
    assert classify_measure("Sum(Sales) / Sum(Cost)") == "SIMPLE"
    assert classify_measure("Sum({<Year={2024}>} Sales)") == "COMPLEX"
    assert classify_measure("Aggr(Sum(Sales), Region)") == "COMPLEX"
    assert classify_measure("ApplyMap('MapTable', ID, 'Unknown')") == "COMPLEX"


def test_deterministic_mquery_skips_llm():
    """Valid M-queries must skip LLM calls entirely."""
    converter = MQueryConverter()
    table = {
        "name": "Customers",
        "m_query": [
            {"step": 1, "content": "let\n    Source = Sql.Database(\"server\", \"db\"),\n    dbo_Customers = Source{[Schema=\"dbo\",Item=\"Customers\"]}[Data]\nin\n    dbo_Customers"}
        ],
    }

    with patch.object(converter.llm_client, "generate_text") as mock_llm:
        result = asyncio.run(converter.refine_all([table]))
        assert mock_llm.call_count == 0
        assert result[0]["conversion_method"] == "deterministic_rule"
        assert result[0]["llm_status"] == "not_needed"


def test_simple_measure_skips_llm():
    """Simple measures with valid DAX must skip LLM calls."""
    converter = MeasureConverter()
    tables = [{"name": "Sales", "columns": [{"fabric_column_name": "Amount", "fabric_datatype": "double"}]}]
    measure = {
        "name": "Total Sales",
        "qlik_expression": "Sum(Amount)",
        "dax_expression": "SUM('Sales'[Amount])",
        "fabric": {"dax_expression": "SUM('Sales'[Amount])"},
        "confidence": {"score": 0.95, "requires_review": False},
    }

    with patch.object(converter.llm_client, "generate_text") as mock_llm:
        result = asyncio.run(converter.refine_all([measure], tables))
        assert mock_llm.call_count == 0
        assert result[0]["conversion_method"] == "deterministic_rule"
        assert result[0]["llm_status"] == "not_needed"


def test_cache_deduplication_prevents_duplicate_calls():
    """Repeated identical expressions must hit cache without extra Groq calls."""
    client = get_llm_client()
    call_count = {"n": 0}

    async def fake_create(**kwargs):
        call_count["n"] += 1
        return _fake_response("CALCULATE(SUM('Sales'[Amount]), 'Sales'[Year] = 2024)")

    async def run():
        with patch.object(client.client.chat.completions, "create", new=AsyncMock(side_effect=fake_create)):
            res1 = await client.generate_text("System", "User Qlik 1", stage="measures")
            res2 = await client.generate_text("System", "User Qlik 1", stage="measures")
            return res1, res2

    res1, res2 = asyncio.run(run())
    assert res1 == res2
    assert call_count["n"] == 1  # Second call was served from cache
    assert llm_usage.current().cache_hits == 1


def test_rate_limit_fallback_preserves_deterministic_baseline():
    """When Groq returns 429 rate limit, converter falls back gracefully."""
    converter = MeasureConverter()
    tables = [{"name": "Sales", "columns": [{"fabric_column_name": "Amount"}]}]
    measure = {
        "name": "Complex KPI",
        "qlik_expression": "Aggr(Sum(Amount), Region)",
        "dax_expression": "SUMX(VALUES('Sales'[Region]), CALCULATE(SUM('Sales'[Amount])))",
        "fabric": {"dax_expression": "SUMX(VALUES('Sales'[Region]), CALCULATE(SUM('Sales'[Amount])))"},
        "confidence": {"score": 0.50, "requires_review": True},
    }

    async def rate_limited_create(**kwargs):
        raise _make_rate_limit_error("Rate limit reached for model groq/compound-mini on TPM")

    async def run():
        with patch.object(converter.llm_client.client.chat.completions, "create", new=AsyncMock(side_effect=rate_limited_create)):
            return await converter.refine_all([measure], tables)

    result = asyncio.run(run())
    assert result[0]["conversion_method"] == "regex_fallback"
    assert result[0]["llm_status"] == "rate_limited"
    assert result[0]["dax_expression"] == "SUMX(VALUES('Sales'[Region]), CALCULATE(SUM('Sales'[Amount])))"
    assert llm_usage.current().rate_limited >= 1


def test_concurrency_and_token_limits():
    """Verify concurrency and max_tokens parameters passed to Groq create()."""
    client = get_llm_client()
    passed_kwargs = {}

    async def inspect_create(**kwargs):
        passed_kwargs.update(kwargs)
        return _fake_response("SUM('Sales'[Amount])")

    async def run():
        with patch.object(client.client.chat.completions, "create", new=AsyncMock(side_effect=inspect_create)):
            return await client.generate_text("System prompt", "User prompt", stage="measures")

    asyncio.run(run())
    assert passed_kwargs.get("max_tokens") == Config.LLM_MAX_OUTPUT_TOKENS
    assert passed_kwargs.get("temperature") == 0.0
