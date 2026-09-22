"""LLM-assisted Qlik calculated-dimension -> DAX calculated column.

Mirror of `src/converters/measures/converter.py`, and the second half of the
fix for DIMENSIONS_RULES being dead code. Only *calculated* dimensions go to
the model: a plain field dimension already maps one-to-one onto a model
column, so there is nothing for the model to decide and calling it would be
pure cost.

Drill-down groups become Power BI hierarchies in DimensionMapper and are
likewise skipped - a hierarchy is a list of levels, not an expression.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from config import Config
from services import dax_guard, llm_usage
from services.prompt_builder import build_dimension_prompts
from services.schema_context import build_schema_context

logger = logging.getLogger(__name__)

STAGE = "dimensions"


class DimensionConverter:
    """Refines deterministic calculated-column conversions with the LLM."""

    def __init__(self, llm_client=None):
        self._llm_client = llm_client

    @property
    def llm_client(self):
        if self._llm_client is None:
            from src.converters.llm_client import GroqLLMClient

            self._llm_client = GroqLLMClient()
        return self._llm_client

    @staticmethod
    def _is_candidate(dimension: Dict[str, Any]) -> bool:
        fabric = dimension.get("fabric")
        if not isinstance(fabric, dict):
            return False
        if fabric.get("kind") == "hierarchy":
            return False
        if not fabric.get("is_calculated"):
            return False
        return bool(str(dimension.get("qlik_expression") or "").strip())

    async def refine_one(
        self,
        dimension: Dict[str, Any],
        tables: List[Dict[str, Any]],
        schema_context: str,
    ) -> Dict[str, Any]:
        usage = llm_usage.current()
        fabric = dimension.get("fabric") or {}
        baseline = str(fabric.get("dax_expression") or "").strip()
        qlik_expr = str(dimension.get("qlik_expression") or "").strip()
        name = str(dimension.get("name") or "Dimension")
        table_hint = str(fabric.get("table") or "")

        system, user = build_dimension_prompts(
            name=name,
            qlik_expression=qlik_expr,
            schema_context=schema_context,
            baseline_dax=baseline,
            table_hint=table_hint,
        )

        usage.record_attempt(STAGE)
        try:
            answer = await self.llm_client.generate_text(system, user)
            usage.record_success(STAGE)
        except Exception as exc:  # noqa: BLE001
            usage.record_failure(STAGE, str(exc))
            logger.warning(
                "LLM dimension conversion failed for '%s' (%s); keeping regex draft.",
                name, exc,
            )
            return dimension

        # is_measure=False: a calculated column legitimately references bare
        # columns of its own table, so the measure-only "naked column" check
        # must not apply here.
        chosen, used_llm, problems = dax_guard.choose(
            baseline, answer, tables, is_measure=False
        )

        if not used_llm:
            if problems:
                usage.record_rejected(STAGE, "; ".join(problems[:3]))
                logger.info(
                    "Rejected LLM DAX for dimension '%s' (%s); kept regex draft.",
                    name, "; ".join(problems[:3]),
                )
            return dimension

        if chosen.strip() == baseline.strip():
            return dimension

        usage.record_accepted(STAGE)
        fabric["dax_expression"] = chosen
        fabric["baseline_dax_expression"] = baseline
        fabric["conversion_method"] = "llm_refined"

        confidence = dimension.get("confidence")
        if isinstance(confidence, dict):
            confidence["rationale"] = (
                (confidence.get("rationale") or "").strip()
                + " Refined by LLM against the resolved schema and Qlik dimension rules."
            ).strip()
            confidence["llm_refined"] = True
        return dimension

    async def refine_all(
        self,
        dimensions: List[Dict[str, Any]],
        tables: List[Dict[str, Any]],
        measures: Optional[List[Dict[str, Any]]] = None,
        relationships: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        if not Config.USE_LLM_DIMENSIONS or not dimensions:
            return dimensions

        candidates = [d for d in dimensions if isinstance(d, dict) and self._is_candidate(d)]
        if not candidates:
            return dimensions

        schema_context = build_schema_context(
            tables, measures=measures, relationships=relationships
        )
        logger.info("Refining %d calculated dimension(s) with the LLM", len(candidates))
        await asyncio.gather(
            *(self.refine_one(d, tables, schema_context) for d in candidates),
            return_exceptions=True,
        )
        return dimensions
