"""When a Qlik visual type has no real Fabric/Power BI equivalent, that fact
must be explicit in the output (supported=False + a concrete replacement
suggestion) - never silently hidden behind an optimistic LLM guess.
"""

import asyncio
import os
from unittest.mock import patch

os.environ.setdefault("GROQ_API_KEY", "test-key")

from src.converters.dashboard_objects.converter import DashboardObjectConverter
from src.converters.base import ConversionContext


async def confident_llm(self, system_prompt, user_prompt, json_schema):
    # The LLM optimistically returns a syntactically valid type even though
    # the underlying Qlik visual has no real Fabric equivalent.
    return {"visual_type": "tableEx", "supported": True, "status": "mapped",
            "confidence": 0.9, "rationale": "looks fine to me"}


def test_unrecognized_qlik_type_is_marked_unsupported_with_replacement():
    conv = DashboardObjectConverter()
    ctx = ConversionContext(app_id="a1", tables=[])
    item = {"title": "Weird Viz", "chart_type": "totally-unknown-chart-xyz"}

    async def run():
        with patch("src.converters.llm_client.GroqLLMClient.generate_structured_response", new=confident_llm):
            return await conv.convert_one(item, ctx)

    result = asyncio.run(run())
    assert result.fabric["supported"] is False
    assert result.fabric["status"] == "unmapped"
    assert result.fabric["replacement_strategy"]
    assert "No direct Power BI/Fabric visual" in result.fabric["replacement_strategy"]


def test_recognized_qlik_type_stays_supported():
    conv = DashboardObjectConverter()
    ctx = ConversionContext(app_id="a1", tables=[])
    item = {"title": "Revenue", "chart_type": "barchart"}

    async def run():
        with patch("src.converters.llm_client.GroqLLMClient.generate_structured_response", new=confident_llm):
            return await conv.convert_one(item, ctx)

    result = asyncio.run(run())
    assert result.fabric["supported"] is True
    assert result.fabric["replacement_strategy"] is None


async def failing_llm(self, system_prompt, user_prompt, json_schema):
    raise RuntimeError("rate limit exhausted after retries")


async def unreachable_llm(self, system_prompt, user_prompt, json_schema):
    raise AssertionError("LLM should not be called for a type the deterministic path already resolves")


def test_sn_prefixed_type_never_needs_the_llm():
    """Regression guard for the actual production failure: a native Qlik
    Cloud 'sn-*' object used to be misclassified as unsupported (missing
    sn- prefix strip), which forced it down the LLM path - and if the LLM
    call then failed, the visual ended up as the dead "unsupported" literal.
    Now that sn-* is stripped correctly, the deterministic shortcut resolves
    it immediately and the LLM is never even called.
    """
    conv = DashboardObjectConverter()
    ctx = ConversionContext(app_id="a1", tables=[])
    item = {"title": "Revenue by Region", "chart_type": "sn-barchart"}

    async def run():
        with patch("src.converters.llm_client.GroqLLMClient.generate_structured_response", new=unreachable_llm):
            return await conv.convert_one(item, ctx)

    result = asyncio.run(run())
    assert result.fabric["visual_type"] == "barChart"
    assert result.fabric["supported"] is True


def test_llm_failure_falls_back_to_rule_based_mapping_not_unsupported():
    """When a visual's raw type is a placeholder ("extension") that bypasses
    the deterministic shortcut, but a real match still exists via
    object_category/KNOWN_EXTENSION_MAPPINGS, an LLM failure must fall back
    to that real, already-computed type - not the dead "unsupported" literal
    that has no Fabric renderer at all.
    """
    conv = DashboardObjectConverter()
    ctx = ConversionContext(app_id="a1", tables=[])
    item = {"title": "Embedded Table", "chart_type": "extension", "object_category": "sn-table"}

    async def run():
        with patch("src.converters.llm_client.GroqLLMClient.generate_structured_response", new=failing_llm):
            return await conv.convert_one(item, ctx)

    result = asyncio.run(run())
    assert result.fabric["visual_type"] == "tableEx"
    assert result.fabric["supported"] is True
    assert "LLM classification failed" in result.fabric["rationale"]


def test_llm_failure_on_unmapped_type_still_gets_actionable_replacement():
    conv = DashboardObjectConverter()
    ctx = ConversionContext(app_id="a1", tables=[])
    item = {"title": "Weird Viz", "chart_type": "totally-unknown-chart-xyz"}

    async def run():
        with patch("src.converters.llm_client.GroqLLMClient.generate_structured_response", new=failing_llm):
            return await conv.convert_one(item, ctx)

    result = asyncio.run(run())
    assert result.fabric["supported"] is False
    assert result.fabric["visual_type"] != "unsupported"  # real fabric type (tableEx), not a dead literal
    assert result.fabric["replacement_strategy"]
