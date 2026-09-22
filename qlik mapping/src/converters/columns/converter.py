"""LLM-assisted Qlik field -> Fabric column typing.

Replaces the most report-specific logic in the service. `_process_tables` in
agents/coordinator_agent.py inferred column types from hardcoded English
keyword lists:

    ["result","status","type","name","category","side","symbol","trader",
     "reduction", ...]                                          -> string
    ["pnl","price","qty","volume","amount","gpa","credits", ...]  -> double

Those words come from the crypto-trading and student demo apps the pipeline
was first built against. A hospital app's `admission_ward`, `los_days`,
`readmit_flag` and `icd10_code` match none of them, default to string, and
its numeric KPIs render as text with summarizeBy=none.

Batched per table rather than per column, because typing depends on the
surrounding columns (rule col3) and one call per column would be both worse
and far more expensive.

Baseline-first, like every other stage: the deterministic types already on
the columns are kept unless the model's answer passes validation.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from config import Config
from services import llm_usage
from services.prompt_builder import build_column_prompts

from .rules import COLUMNS_OUTPUT_SCHEMA, FABRIC_DATATYPES, SUMMARIZE_BY

logger = logging.getLogger(__name__)

STAGE = "columns"

# Types the engine reports authoritatively. When Qlik itself says a field is
# a date or a number, that is stronger evidence than anything the model can
# infer, so those columns are not sent (rule col1).
AUTHORITATIVE_QLIK_TYPES = {
    "DATE", "TIMESTAMP", "TIME", "INTERVAL", "NUMERIC", "INTEGER", "REAL",
}


class ColumnConverter:
    """Refines deterministic column typing with the LLM, per table."""

    def __init__(self, llm_client=None):
        self._llm_client = llm_client

    @property
    def llm_client(self):
        if self._llm_client is None:
            from src.converters.llm_client import GroqLLMClient

            self._llm_client = GroqLLMClient()
        return self._llm_client

    @staticmethod
    def _needs_review(column: Dict[str, Any]) -> bool:
        """True when the deterministic pass had nothing solid to go on.

        A column whose Qlik type is a definite date/number is already
        correctly typed; only the ones that fell through to the keyword
        guesswork are worth a model call.
        """
        qlik_type = str(column.get("qlik_datatype") or "").upper()
        if any(t in qlik_type for t in AUTHORITATIVE_QLIK_TYPES):
            return False
        # STRING/UNKNOWN/empty means the engine did not commit to a type, so
        # the deterministic result came from the field name alone.
        return True

    async def refine_table(
        self,
        table: Dict[str, Any],
        app_subject: str = "",
        sample_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Refine one table's columns in place."""
        usage = llm_usage.current()
        columns = [c for c in (table.get("columns") or []) if isinstance(c, dict)]
        if not columns:
            return table

        ambiguous = [c for c in columns if self._needs_review(c)]
        if not ambiguous:
            return table

        payload = [
            {
                "qlik_column_name": c.get("qlik_column_name"),
                "qlik_datatype": c.get("qlik_datatype"),
                "current_fabric_datatype": c.get("fabric_datatype"),
                "current_summarize_by": c.get("summarize_by"),
                "format_string": c.get("format_string"),
            }
            for c in ambiguous
        ]

        usage.record_attempt(STAGE)
        try:
            system, user = build_column_prompts(
                table_name=str(table.get("name") or table.get("table_name") or ""),
                columns=payload,
                sample_rows=sample_rows,
                app_subject=app_subject,
            )
            answer = await self.llm_client.generate_structured_response(
                system, user, COLUMNS_OUTPUT_SCHEMA
            )
            usage.record_success(STAGE)
        except Exception as exc:  # noqa: BLE001
            usage.record_failure(STAGE, str(exc))
            logger.warning(
                "LLM column typing failed for table '%s' (%s); keeping rule-based types.",
                table.get("name"), exc,
            )
            return table

        if not isinstance(answer, dict) or not isinstance(answer.get("columns"), list):
            usage.record_rejected(STAGE, "response missing a columns array")
            return table

        by_name = {
            str(c.get("qlik_column_name") or "").strip().lower(): c for c in ambiguous
        }
        applied = 0
        for entry in answer["columns"]:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("qlik_column_name") or "").strip().lower()
            target = by_name.get(key)
            if target is None:
                # The model returned a column that was not sent. Never create
                # one: an invented column would reach TMDL and fail to load.
                logger.info(
                    "Ignoring unknown column '%s' returned for table '%s'.",
                    entry.get("qlik_column_name"), table.get("name"),
                )
                continue

            datatype = entry.get("fabric_datatype")
            summarize = entry.get("summarize_by")
            if datatype not in FABRIC_DATATYPES:
                continue
            if summarize not in SUMMARIZE_BY:
                summarize = target.get("summarize_by") or "none"

            # An identifier must never be summarised, whatever the model
            # said (rule col5) - a report that sums customer IDs looks
            # plausible and is meaningless.
            if entry.get("is_key") and summarize != "none":
                summarize = "none"

            target["baseline_fabric_datatype"] = target.get("fabric_datatype")
            target["fabric_datatype"] = datatype
            target["summarize_by"] = summarize
            if entry.get("format_string"):
                target["format_string"] = entry["format_string"]
            if entry.get("is_hidden") is not None:
                target["is_hidden"] = bool(entry["is_hidden"])
            target["typing_evidence"] = entry.get("evidence")
            target["conversion_method"] = "llm"
            if entry.get("requires_review"):
                target["requires_review"] = True
                target["note"] = entry.get("note")
            applied += 1

        if applied:
            usage.record_accepted(STAGE)
        return table

    async def refine_all(
        self,
        tables: List[Dict[str, Any]],
        app_subject: str = "",
        hypercube_samples: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not Config.USE_LLM_COLUMNS or not tables:
            return tables

        samples = hypercube_samples or {}
        targets = [t for t in tables if isinstance(t, dict) and t.get("columns")]
        if not targets:
            return tables

        logger.info("Refining column types for %d table(s) with the LLM", len(targets))
        await asyncio.gather(
            *(
                self.refine_table(
                    table,
                    app_subject=app_subject,
                    sample_rows=samples.get(table.get("name")) if isinstance(samples, dict) else None,
                )
                for table in targets
            ),
            return_exceptions=True,
        )
        return tables
