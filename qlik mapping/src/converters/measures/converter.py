"""LLM-assisted Qlik measure -> DAX conversion with token optimization and classification.

Optimization Workflow:
1. Classify measures (SIMPLE, MODERATE, COMPLEX, UNSUPPORTED).
2. For SIMPLE and valid deterministic measures: keep regex draft, SKIP LLM.
3. For candidates needing refinement: construct minimal schema context, use cache & batching.
4. On LLM rate-limit/failure: preserve valid regex fallback without throwing exceptions.
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from config import Config
from services import dax_guard, llm_usage
from services.prompt_builder import build_measure_prompts
from services.schema_context import build_schema_context

logger = logging.getLogger(__name__)

STAGE = "measures"

# Simple standalone aggregations without set analysis or complex logic
_SIMPLE_PATTERN = re.compile(
    r"^\s*(?:sum|avg|average|count|min|max|distinctcount)\s*\(\s*(?:distinct\s+)?['\"\[]?[a-zA-Z0-9_\s\-\.]+['\"\]]?\s*\)\s*$",
    re.IGNORECASE,
)

# Complex functions requiring advanced semantic reasoning
_COMPLEX_KEYWORDS = (
    "{<", "aggr(", "applymap(", "rangesum(", "above(", "below(",
    "p(", "e(", "pick(", "match(", "lookup(", "concat("
)


def classify_measure(qlik_expr: str) -> str:
    """Classify measure complexity: SIMPLE, MODERATE, COMPLEX, UNSUPPORTED."""
    if not qlik_expr or not str(qlik_expr).strip():
        return "SIMPLE"

    expr = str(qlik_expr).strip()
    # Strip comments
    expr = re.sub(r'/\*.*?\*/', '', expr, flags=re.DOTALL)
    expr = re.sub(r'//.*', '', expr).strip()
    if expr.startswith("="):
        expr = expr[1:].strip()
    # Strip Num(...) wrapper
    num_match = re.match(r"^num\s*\((.*)(?:,[^,)]*){1,2}\)$", expr, flags=re.IGNORECASE | re.DOTALL)
    if num_match:
        expr = num_match.group(1).strip()

    expr_lower = expr.lower()

    if any(kw in expr_lower for kw in _COMPLEX_KEYWORDS):
        return "COMPLEX"

    if _SIMPLE_PATTERN.match(expr_lower):
        return "SIMPLE"

    # Simple arithmetic between single aggregations: e.g. Sum(A) / Sum(B)
    if "/" in expr_lower or "*" in expr_lower or "+" in expr_lower or "-" in expr_lower:
        if not any(kw in expr_lower for kw in _COMPLEX_KEYWORDS) and "{" not in expr_lower:
            return "SIMPLE"

    if "if(" in expr_lower or "wildmatch(" in expr_lower:
        return "MODERATE"

    return "MODERATE"


def _extract_referenced_tables(qlik_expr: str, tables: List[Dict[str, Any]]) -> List[str]:
    """Find table names referenced directly or by column match in the Qlik expression."""
    referenced = []
    expr_lower = (qlik_expr or "").lower()
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        t_name = str(t.get("name") or t.get("table_name") or "")
        if not t_name:
            continue
        if t_name.lower() in expr_lower:
            referenced.append(t_name)
            continue
        for col in (t.get("columns") or []):
            if isinstance(col, dict):
                c_name = str(col.get("fabric_column_name") or col.get("qlik_column_name") or "").lower()
                if c_name and c_name in expr_lower:
                    referenced.append(t_name)
                    break
    return list(set(referenced)) or ([tables[0].get("name")] if tables and isinstance(tables[0], dict) else [])


class MeasureConverter:
    """Refines deterministic measure conversions with the LLM using token optimization."""

    def __init__(self, llm_client=None):
        self._llm_client = llm_client

    @property
    def llm_client(self):
        if self._llm_client is None:
            from src.converters.llm_client import get_llm_client
            self._llm_client = get_llm_client()
        return self._llm_client

    @staticmethod
    def _table_hint(measure: Dict[str, Any], tables: List[Dict[str, Any]]) -> str:
        declared = measure.get("tables") or []
        if declared and str(declared[0]).strip():
            return str(declared[0])
        return str((tables[0].get("name") if tables and isinstance(tables[0], dict) else "") or "")

    async def refine_one(
        self,
        measure: Dict[str, Any],
        tables: List[Dict[str, Any]],
        schema_context: str = "",
        relationships: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Return `measure` with its DAX upgraded when the model improves it."""
        usage = llm_usage.current()
        fabric = measure.get("fabric") if isinstance(measure.get("fabric"), dict) else {}
        baseline = str(fabric.get("dax_expression") or measure.get("dax_expression") or "").strip()
        qlik_expr = str(measure.get("qlik_expression") or "").strip()
        name = str(measure.get("name") or "Measure")

        if not qlik_expr:
            return measure

        baseline_val = measure.get("validation") or {}
        baseline_is_valid = bool(
            baseline and baseline != "BLANK()" and baseline != "-- SKIPPED"
            and baseline_val.get("passed", False)
            and not measure.get("unresolved_columns")
            and not measure.get("unconverted_qlik_functions")
            and not measure.get("unconverted_qlik_syntax")
        )
        if not baseline_is_valid and baseline and baseline != "BLANK()":
            ok, _ = dax_guard.validate(baseline, tables, is_measure=True)
            if (
                ok
                and not measure.get("unresolved_columns")
                and not measure.get("unconverted_qlik_functions")
                and not measure.get("unconverted_qlik_syntax")
            ):
                baseline_is_valid = True

        # Construct minimal scoped schema context for this measure
        ref_tables = _extract_referenced_tables(qlik_expr, tables)
        scoped_tables = [t for t in tables if isinstance(t, dict) and t.get("name") in ref_tables] or tables[:3]
        scoped_schema = build_schema_context(scoped_tables, measures=[], column_limit=15)

        system, user = build_measure_prompts(
            name=name,
            qlik_expression=qlik_expr,
            schema_context=scoped_schema,
            baseline_dax=baseline,
            table_hint=self._table_hint(measure, tables),
        )

        usage.record_attempt(STAGE)
        try:
            answer = await self.llm_client.generate_text(system, user, stage=STAGE)
            usage.record_success(STAGE)
        except Exception as exc:  # noqa: BLE001
            usage.record_failure(STAGE, str(exc))
            logger.warning(
                "LLM measure conversion failed for '%s' (%s); keeping regex draft.",
                name, exc,
            )
            measure["llm_status"] = "rate_limited" if "rate" in str(exc).lower() else "failed"
            if baseline_is_valid:
                measure["conversion_method"] = "deterministic_rule"
                measure["conversion_status"] = "converted"
                measure["status"] = "converted"
            else:
                measure["conversion_method"] = "regex_fallback"
                measure["conversion_status"] = "failed_to_convert"
                measure["status"] = "failed to convert"
                measure["confidence_score"] = 0
                measure["review_notes"] = f"conversion failed: {exc}"
            return measure

        chosen, used_llm, problems = dax_guard.choose(
            baseline, answer, tables, is_measure=True
        )

        if not used_llm:
            if problems:
                usage.record_rejected(STAGE, "; ".join(problems[:3]))
                logger.info(
                    "Rejected LLM DAX for measure '%s' (%s); kept regex draft.",
                    name, "; ".join(problems[:3]),
                )
            if baseline_is_valid:
                measure["conversion_method"] = "deterministic_rule"
                measure["conversion_status"] = "converted"
                measure["status"] = "converted"
            else:
                measure["conversion_method"] = "regex_fallback"
                measure["conversion_status"] = "failed_to_convert"
                measure["status"] = "failed to convert"
                measure["confidence_score"] = 0
                prob_desc = "; ".join(problems) if problems else "no usable model response"
                measure["review_notes"] = f"conversion failed: {prob_desc}"
            return measure

        if chosen.strip() == baseline.strip():
            if baseline_is_valid:
                measure["conversion_method"] = "deterministic_rule"
                measure["conversion_status"] = "converted"
                measure["status"] = "converted"
            return measure

        from services.validators.dax_validators import run_dax_validators
        temp_fabric = dict(fabric)
        temp_fabric["dax_expression"] = chosen
        llm_val = run_dax_validators(temp_fabric, tables=tables)

        if llm_val.get("passed", False):
            usage.record_accepted(STAGE)
            measure["dax_expression"] = chosen
            measure["baseline_dax_expression"] = baseline
            measure["conversion_method"] = "llm_refined"
            measure["conversion_status"] = "converted"
            measure["status"] = "converted"
            measure["llm_status"] = "success"
            measure["unresolved_columns"] = []
            measure["unconverted_qlik_functions"] = []
            measure["unconverted_qlik_syntax"] = []
            if isinstance(fabric, dict):
                fabric["dax_expression"] = chosen
                tmdl = fabric.get("tmdl")
                if isinstance(tmdl, str) and baseline and baseline in tmdl:
                    fabric["tmdl"] = tmdl.replace(baseline, chosen)
                else:
                    fabric["tmdl"] = f"measure '{name}' = {chosen}"

            confidence = measure.get("confidence")
            if isinstance(confidence, dict):
                confidence["rationale"] = (
                    (confidence.get("rationale") or "").strip()
                    + " Refined by LLM against the resolved schema."
                ).strip()
                confidence["llm_refined"] = True
                confidence["score"] = max(0.85, confidence.get("score", 0.85))
                confidence["score_out_of_100"] = int(round(confidence["score"] * 100))
                confidence["band"] = "high"
                confidence["requires_review"] = False
            measure["confidence_score"] = confidence.get("score_out_of_100", 90) if isinstance(confidence, dict) else 90
            measure["review_notes"] = ""
            measure["validation"] = llm_val
        else:
            if baseline_is_valid:
                measure["conversion_method"] = "deterministic_rule"
                measure["conversion_status"] = "converted"
                measure["status"] = "converted"
            else:
                measure["conversion_method"] = "regex_fallback"
                measure["conversion_status"] = "failed_to_convert"
                measure["status"] = "failed to convert"
                measure["confidence_score"] = 0
                measure["review_notes"] = f"conversion failed: {'; '.join(f.get('message', '') for f in llm_val.get('failures', []))}"
        return measure

    async def refine_all(
        self,
        measures: List[Dict[str, Any]],
        tables: List[Dict[str, Any]],
        relationships: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Refine measures with classification and token optimization."""
        if not measures:
            return measures

        usage = llm_usage.current()

        candidates = []
        for m in measures:
            if not isinstance(m, dict):
                continue
            qlik = str(m.get("qlik_expression") or m.get("expression") or "").strip()
            if not qlik:
                continue

            baseline = str(m.get("dax_expression") or m.get("fabric", {}).get("dax_expression") or "").strip()
            complexity = classify_measure(qlik)
            m["complexity"] = complexity

            val = m.get("validation") or {}
            val_passed = val.get("passed", False)
            has_valid_baseline = bool(
                baseline and baseline != "BLANK()" and baseline != "-- SKIPPED"
                and val_passed
                and not m.get("unresolved_columns")
                and not m.get("unconverted_qlik_functions")
                and not m.get("unconverted_qlik_syntax")
            )

            # 1. Deterministic conversion succeeded and validated -> NEVER call LLM
            if has_valid_baseline or m.get("conversion_status") == "converted":
                usage.record_deterministic(STAGE)
                m["conversion_method"] = "deterministic_rule"
                m["conversion_status"] = "converted"
                m["status"] = "converted"
                m["llm_status"] = "not_needed"
                continue

            # 2. SIMPLE measures: deterministic conversion is complete
            if complexity == "SIMPLE":
                is_valid, _ = dax_guard.validate(baseline, tables, is_measure=True) if baseline else (False, [])
                if is_valid and baseline and baseline != "BLANK()":
                    usage.record_deterministic(STAGE)
                    m["conversion_method"] = "deterministic_rule"
                    m["conversion_status"] = "converted"
                    m["status"] = "converted"
                    m["llm_status"] = "not_needed"
                    continue

            # 3. MODERATE / COMPLEX measures: validate deterministic baseline first
            if baseline and baseline != "BLANK()":
                is_valid, _ = dax_guard.validate(baseline, tables, is_measure=True)
                conf = m.get("confidence") or {}
                conf_score = conf.get("score") or conf.get("confidence_score") or 1.0
                if is_valid and conf_score >= 0.85 and not conf.get("requires_review"):
                    usage.record_deterministic(STAGE)
                    m["conversion_method"] = "deterministic_rule"
                    m["conversion_status"] = "converted"
                    m["status"] = "converted"
                    m["llm_status"] = "not_needed"
                    continue

            # 4. Only measures with missing/invalid DAX or low confidence need LLM
            if Config.USE_LLM_MEASURES:
                candidates.append(m)
            else:
                usage.record_deterministic(STAGE)

        if not candidates or not Config.USE_LLM_MEASURES:
            logger.info("All %d measure(s) converted deterministically; skipping LLM.", len(measures))
            return measures

        logger.info("Refining %d / %d measure(s) with the LLM (batch_size=%d)", len(candidates), len(measures), Config.LLM_MAX_BATCH_SIZE)

        schema_context = build_schema_context(tables, measures=measures, relationships=relationships, column_limit=15)

        # Batch candidate measures to respect concurrency and rate limits
        batch_size = max(1, Config.LLM_MAX_BATCH_SIZE)
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i : i + batch_size]
            await asyncio.gather(
                *(self.refine_one(m, tables, schema_context) for m in batch),
                return_exceptions=True,
            )

        return measures
