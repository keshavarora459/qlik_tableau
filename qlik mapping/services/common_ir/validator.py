"""Contract 2.0 and Common IR Conformance Validator."""

import re
from typing import Any, Dict, List, Optional, Set

from services.common_ir.models import (
    CanonicalContract2,
    DiagnosticIR,
    DiagnosticSeverity,
)


class ContractValidator:
    """Validates Canonical Contract 2.0 payloads for Fabric/Power BI generation conformance."""

    @classmethod
    def validate(cls, contract: CanonicalContract2) -> Dict[str, Any]:
        errors: List[str] = []
        warnings: List[str] = []
        diagnostics: List[DiagnosticIR] = []
        review_required = False

        # 1. Basic Schema & Version
        if contract.contract_version != "2.0":
            errors.append(f"Invalid contract_version: {contract.contract_version}, expected '2.0'")

        # 2. Table and Column Indexing
        table_cols_map: Dict[str, Set[str]] = {}
        for tbl in contract.tables:
            t_name = tbl.canonical_name or tbl.source_name
            cols = {c.canonical_name or c.name for c in tbl.columns}
            table_cols_map[t_name] = cols
            if not cols:
                warnings.append(f"Table '{t_name}' has no defined columns")

        all_known_columns: Set[str] = set()
        for cols in table_cols_map.values():
            all_known_columns.update(cols)

        # 3. Relationship Validation
        for r in contract.relationships:
            if not r.from_table or not r.to_table:
                errors.append(f"Relationship '{r.relationship_id}' missing table endpoints: from='{r.from_table}', to='{r.to_table}'")
                diagnostics.append(DiagnosticIR(
                    severity=DiagnosticSeverity.ERROR.value,
                    code="RELATIONSHIP_MISSING_ENDPOINTS",
                    message=f"Missing endpoints for relationship {r.relationship_id}",
                    source_object=r.relationship_id,
                    requires_review=True,
                ))
                review_required = True
                continue

            if table_cols_map and r.from_table not in table_cols_map:
                warnings.append(f"Relationship from_table '{r.from_table}' not found in declared tables")
            if table_cols_map and r.to_table not in table_cols_map:
                warnings.append(f"Relationship to_table '{r.to_table}' not found in declared tables")

            if r.from_table == r.to_table:
                warnings.append(f"Self-relationship detected on table '{r.from_table}'")

        # 4. Measure & Calculated Column Validation
        known_measure_names = {m.name for m in contract.measures}
        for m in contract.measures:
            if not m.name:
                errors.append("Encountered measure with empty name")
                continue
            if not m.expression or not m.expression.strip():
                errors.append(f"Measure '{m.name}' has empty DAX expression")
                diagnostics.append(DiagnosticIR(
                    severity=DiagnosticSeverity.ERROR.value,
                    code="MEASURE_EMPTY_EXPRESSION",
                    message=f"Empty expression for measure {m.name}",
                    source_object=m.name,
                    requires_review=True,
                ))
                review_required = True
                continue

            # Check balanced parentheses
            if m.expression.count("(") != m.expression.count(")"):
                errors.append(f"Measure '{m.name}' has unbalanced parentheses: {m.expression}")
                diagnostics.append(DiagnosticIR(
                    severity=DiagnosticSeverity.ERROR.value,
                    code="DAX_UNBALANCED_PARENTHESES",
                    message=f"Unbalanced parentheses in {m.name}",
                    source_object=m.name,
                    requires_review=True,
                ))
                review_required = True

            if m.requires_review:
                review_required = True
                diagnostics.append(DiagnosticIR(
                    severity=DiagnosticSeverity.WARNING.value,
                    code="MEASURE_REQUIRES_REVIEW",
                    message=f"Measure {m.name} flagged for review",
                    source_object=m.name,
                    requires_review=True,
                ))

        for cc in contract.calculated_columns:
            if not cc.name or not cc.expression:
                errors.append(f"Calculated column '{cc.name}' has empty definition")
            if cc.requires_review:
                review_required = True

        # 5. Visual Validation
        for v in contract.visuals:
            if not v.visual_id:
                errors.append("Visual missing visual_id")
            if not v.type:
                warnings.append(f"Visual '{v.visual_id}' missing visual type")

            # Check for empty projections on standard charts
            if v.type in {"barChart", "lineChart", "scatterChart", "pieChart", "columnChart", "donutChart"}:
                if not v.dimensions and not v.measures and not v.encoding:
                    warnings.append(f"Visual '{v.visual_id}' ({v.type}) has no dimension or measure bindings")
                    diagnostics.append(DiagnosticIR(
                        severity=DiagnosticSeverity.WARNING.value,
                        code="VISUAL_EMPTY_PROJECTIONS",
                        message=f"Visual {v.visual_id} has empty field bindings",
                        source_object=v.visual_id,
                        requires_review=True,
                    ))

        # 6. Security Validation
        if contract.security and contract.security.review_required:
            review_required = True
            for reason in contract.security.unsupported_reasons:
                warnings.append(f"Security limitation: {reason}")
                diagnostics.append(DiagnosticIR(
                    severity=DiagnosticSeverity.WARNING.value,
                    code="SECURITY_UNSUPPORTED_CONSTRUCT",
                    message=reason,
                    source_object="security",
                    requires_review=True,
                ))

        # Calculate Confidence Score
        score = 100.0
        if errors:
            score -= min(50.0, len(errors) * 15.0)
        if warnings:
            score -= min(30.0, len(warnings) * 5.0)
        if review_required:
            score = min(score, 75.0)

        is_valid = len(errors) == 0

        # Append diagnostics to contract
        contract.diagnostics.extend(diagnostics)

        return {
            "valid": is_valid,
            "confidence_score": max(0.0, round(score, 2)),
            "errors": errors,
            "warnings": warnings,
            "review_required": review_required,
            "diagnostics": [d.to_dict() for d in diagnostics],
        }
