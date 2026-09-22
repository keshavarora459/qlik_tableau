"""Semantic Equivalence Validator for Source to Target DAX Expressions."""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SemanticValidationResult:
    is_equivalent: bool = True
    confidence_score: float = 1.0
    semantic_loss: bool = False
    loss_reasons: List[str] = field(default_factory=list)
    source_ast_summary: Dict[str, Any] = field(default_factory=dict)
    target_ast_summary: Dict[str, Any] = field(default_factory=dict)


class SemanticValidator:
    """Compares source expressions (Qlik / Tableau) with generated DAX for semantic fidelity."""

    AGGREGATION_MAP = {
        "sum": "SUM",
        "avg": "AVERAGE",
        "average": "AVERAGE",
        "count": "COUNT",
        "distinctcount": "DISTINCTCOUNT",
        "min": "MIN",
        "max": "MAX",
    }

    @classmethod
    def validate_measure_semantics(
        cls,
        source_expr: str,
        target_dax: str,
        known_tables: Optional[List[Dict[str, Any]]] = None,
    ) -> SemanticValidationResult:
        if not source_expr or not target_dax:
            return SemanticValidationResult(
                is_equivalent=False,
                confidence_score=0.0,
                semantic_loss=True,
                loss_reasons=["Empty source or target expression"],
            )

        src_clean = source_expr.strip()
        tgt_clean = target_dax.strip()

        # If target is a stub or unmapped blank
        if tgt_clean == "BLANK()":
            return SemanticValidationResult(
                is_equivalent=False,
                confidence_score=0.3,
                semantic_loss=True,
                loss_reasons=["Target DAX is unmapped BLANK()"],
            )

        loss_reasons: List[str] = []
        score = 1.0

        # 1. Aggregation Matching
        src_agg_match = re.search(r"\b(sum|avg|average|count|distinctcount|min|max)\s*\(", src_clean, re.IGNORECASE)
        tgt_agg_match = re.search(r"\b(SUM|AVERAGE|COUNT|DISTINCTCOUNT|MIN|MAX|SUMX|AVERAGEX|MAXX|MINX)\s*\(", tgt_clean)

        if src_agg_match:
            src_agg = src_agg_match.group(1).lower()
            expected_tgt = cls.AGGREGATION_MAP.get(src_agg, "SUM")
            if tgt_agg_match:
                tgt_agg = tgt_agg_match.group(1).upper()
                if expected_tgt not in tgt_agg and not ("COUNT" in expected_tgt and "COUNT" in tgt_agg):
                    loss_reasons.append(f"Aggregation mismatch: source used '{src_agg}', target DAX used '{tgt_agg}'")
                    score -= 0.3
            else:
                loss_reasons.append(f"Source aggregation '{src_agg}' missing in target DAX")
                score -= 0.3

        # 2. Set Analysis / Filter Context Matching
        if "{<" in src_clean or "<" in src_clean and ">}" in src_clean:
            if "CALCULATE(" not in tgt_clean:
                loss_reasons.append("Source contains Set Analysis but target DAX lacks CALCULATE filter wrapper")
                score -= 0.4

        # 3. TOTAL modifier matching
        if re.search(r"\btotal\b", src_clean, re.IGNORECASE):
            if not ("ALLSELECTED(" in tgt_clean or "ALLEXCEPT(" in tgt_clean or "ALL(" in tgt_clean):
                loss_reasons.append("Source contains TOTAL modifier but target DAX lacks ALL/ALLSELECTED/ALLEXCEPT")
                score -= 0.3

        # 4. Aggr() iterator matching
        if re.search(r"\bAggr\s*\(", src_clean, re.IGNORECASE):
            if not ("SUMMARIZE(" in tgt_clean or "X(" in tgt_clean):
                loss_reasons.append("Source contains Aggr() but target DAX lacks SUMMARIZE iterator")
                score -= 0.3

        # 5. Pick() switch matching
        if re.search(r"\bPick\s*\(", src_clean, re.IGNORECASE):
            if not ("SWITCH(" in tgt_clean or "(" in tgt_clean):
                loss_reasons.append("Source contains Pick() but target DAX lacks SWITCH/branch selection")
                score -= 0.3

        # 6. ApplyMap() lookup matching
        if re.search(r"\bApplyMap\s*\(", src_clean, re.IGNORECASE):
            if "LOOKUPVALUE(" not in tgt_clean:
                loss_reasons.append("Source contains ApplyMap() but target DAX lacks LOOKUPVALUE")
                score -= 0.3

        score = max(0.0, min(1.0, score))
        semantic_loss = len(loss_reasons) > 0

        return SemanticValidationResult(
            is_equivalent=(len(loss_reasons) == 0),
            confidence_score=score,
            semantic_loss=semantic_loss,
            loss_reasons=loss_reasons,
            source_ast_summary={"expression": src_clean},
            target_ast_summary={"expression": tgt_clean},
        )
