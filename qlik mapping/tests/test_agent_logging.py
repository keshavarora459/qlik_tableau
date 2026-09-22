"""CoordinatorAgent.process_data must:
  - store the mapping result via the MongoDB API (app.py's save_mapping_to_cosmos,
    already covered by the endpoint, not re-tested here),
  - log a detailed agent action at every major phase (more than 6, each
    carrying specific counts, not a bare label),
  - never let logging block or slow down the actual mapping work (fire-and-forget).
"""

import asyncio
import time
from unittest.mock import patch

import agents.coordinator_agent as coordinator_mod
from agents.coordinator_agent import CoordinatorAgent

PAYLOAD = {
    "app_id": "app-1", "app_name": "FleetVision KSA", "run_id": "run-1", "space_id": "space-1",
    "tables": [
        {"table_name": "Trips", "fields": [{"name": "driver_id", "dataType": "STRING"}]},
        {"table_name": "Drivers", "fields": [{"name": "driver_id", "dataType": "STRING"}]},
    ],
    "measures": [], "dimensions": [],
    "relationships": [{"table1": "Trips", "field1": "driver_id", "table2": "Drivers", "field2": "driver_id"}],
    "datasources": [], "visualizations": [], "sheets": [], "filters": [],
}


async def _run_and_drain(payload):
    result = await CoordinatorAgent().process_data(payload, False, "app-1")
    pending = list(coordinator_mod._BACKGROUND_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return result


import pytest
import config

@pytest.fixture(autouse=True)
def disable_live_llm(monkeypatch):
    monkeypatch.setattr(config.Config, "USE_LLM_MQUERY", False)
    monkeypatch.setattr(config.Config, "USE_LLM_MEASURES", False)
    monkeypatch.setattr(config.Config, "USE_LLM_VISUALS", False)


def test_more_than_six_detailed_agent_actions_are_logged():
    calls = []

    async def fake_log(action, app_id=None, run_id=None, details=None, **kwargs):
        calls.append((action, details))

    with patch("agents.coordinator_agent.log_action_to_api", new=fake_log):
        asyncio.run(_run_and_drain(PAYLOAD))

    assert len(calls) > 6, f"expected more than 6 agent actions, got {len(calls)}: {calls}"
    # Each action must carry real detail (counts/context), not just a bare label.
    for action, details in calls:
        assert action
        assert details and len(details) > 5


def test_logging_never_blocks_the_request_even_if_the_api_is_slow():
    async def slow_log(action, app_id=None, run_id=None, details=None, **kwargs):
        await asyncio.sleep(5)  # far longer than a real mapping run should take

    start = time.monotonic()
    with patch("agents.coordinator_agent.log_action_to_api", new=slow_log):
        asyncio.run(CoordinatorAgent().process_data(PAYLOAD, False, "app-1"))
    elapsed = time.monotonic() - start

    assert elapsed < 5, f"process_data took {elapsed:.1f}s - action logging must be fire-and-forget, not awaited"


def test_workbook_metadata_carries_identity_fields():
    result = asyncio.run(
        CoordinatorAgent().process_data(PAYLOAD, False, "app-1", "space-1", "FleetVision KSA", "run-1")
    )
    wm = result["workbook_metadata"]
    assert wm["app_id"] == "app-1"
    assert wm["space_id"] == "space-1"
    assert wm["app_name"] == "FleetVision KSA"
    assert wm["name"] == "FleetVision KSA"
    assert wm["run_id"] == "run-1"
