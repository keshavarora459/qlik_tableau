import asyncio
import json
import logging

import pytest

from app.core.structured_logger import CorrelationContext, StructuredFormatter


def test_correlation_context_vars():
    # Set context
    CorrelationContext.set(
        run_id="test_run",
        workbook_id="test_wb",
        project_id="test_proj"
    )

    assert CorrelationContext.get_run_id() == "test_run"
    assert CorrelationContext.get_workbook_id() == "test_wb"
    assert CorrelationContext.get_project_id() == "test_proj"

def test_structured_formatter():
    # Set context
    CorrelationContext.set(
        run_id="ctx_run",
        workbook_id="ctx_wb",
        project_id="ctx_proj"
    )

    record = logging.LogRecord(
        name="test_logger", level=logging.INFO, pathname="", lineno=0,
        msg="Test message", args=(), exc_info=None
    )

    formatter = StructuredFormatter()
    result = formatter.format(record)
    data = json.loads(result)

    # Check that the formatter injected the context variables into the JSON output
    assert data["run_id"] == "ctx_run"
    assert data["workbook_id"] == "ctx_wb"
    assert data["project_id"] == "ctx_proj"
    assert data["message"] == "Test message"


@pytest.mark.asyncio
async def test_asyncio_context_isolation():
    # Context variables should be isolated per task

    async def task_a():
        CorrelationContext.set(run_id="run_a")
        await asyncio.sleep(0.1)
        return CorrelationContext.get_run_id()

    async def task_b():
        CorrelationContext.set(run_id="run_b")
        await asyncio.sleep(0.1)
        return CorrelationContext.get_run_id()

    res_a, res_b = await asyncio.gather(task_a(), task_b())

    assert res_a == "run_a"
    assert res_b == "run_b"
