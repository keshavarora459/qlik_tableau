"""
Centralized structured logging with correlation IDs.

Every log record automatically includes run_id, workbook_id, project_id
so that all logs from a single request can be correlated end-to-end.

Usage:
    from app.core.structured_logger import get_structured_logger, CorrelationContext

    # Set correlation IDs at the start of a request
    CorrelationContext.set(run_id="abc", workbook_id="wb1", project_id="proj1")

    logger = get_structured_logger("my_module")
    logger.info("Something happened")
    # Output: {"timestamp": "...", "level": "INFO", "module": "my_module",
    #          "run_id": "abc", "workbook_id": "wb1", "project_id": "proj1",
    #          "message": "Something happened"}
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Correlation context — propagates IDs across async boundaries automatically
# ---------------------------------------------------------------------------
_run_id_var: ContextVar[str] = ContextVar("run_id", default="UNKNOWN")
_workbook_id_var: ContextVar[str] = ContextVar("workbook_id", default="UNKNOWN")
_project_id_var: ContextVar[str] = ContextVar("project_id", default="UNKNOWN")


class CorrelationContext:
    """Thread/task-safe correlation ID store using contextvars."""

    @staticmethod
    def set(
        run_id: str = "UNKNOWN",
        workbook_id: str = "UNKNOWN",
        project_id: str = "UNKNOWN",
    ) -> None:
        _run_id_var.set(run_id or "UNKNOWN")
        _workbook_id_var.set(workbook_id or "UNKNOWN")
        _project_id_var.set(project_id or "UNKNOWN")

    @staticmethod
    def get_run_id() -> str:
        return _run_id_var.get()

    @staticmethod
    def get_workbook_id() -> str:
        return _workbook_id_var.get()

    @staticmethod
    def get_project_id() -> str:
        return _project_id_var.get()

    @staticmethod
    def as_dict() -> dict:
        return {
            "run_id": _run_id_var.get(),
            "workbook_id": _workbook_id_var.get(),
            "project_id": _project_id_var.get(),
        }


# ---------------------------------------------------------------------------
# JSON Formatter — produces structured log records for container log capture
# ---------------------------------------------------------------------------
class StructuredFormatter(logging.Formatter):
    """Emits each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
            # Correlation IDs
            "run_id": _run_id_var.get(),
            "workbook_id": _workbook_id_var.get(),
            "project_id": _project_id_var.get(),
        }

        # Include exception info if present
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["error_type"] = type(record.exc_info[1]).__name__
            log_entry["error_message"] = str(record.exc_info[1])
            log_entry["traceback"] = self.formatException(record.exc_info)

        # Include any extra fields passed via logger.info("msg", extra={...})
        for key in ("extra_data", "function_name", "error_context"):
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)

        return json.dumps(log_entry, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------
_configured_loggers: set = set()


def get_structured_logger(name: str) -> logging.Logger:
    """
    Returns a logger that emits structured JSON to stdout.

    Safe to call multiple times — handlers are only added once per logger name.
    """
    logger = logging.getLogger(name)

    if name not in _configured_loggers:
        logger.setLevel(logging.INFO)

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)

        # Prevent duplicate propagation to root logger
        logger.propagate = False

        _configured_loggers.add(name)

    return logger
