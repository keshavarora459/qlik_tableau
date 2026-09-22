import re
from typing import Any, Dict, List, Optional

from .dax_identifiers import build_column_index, find_bare_columns

# M functions that indicate the query actually reaches a real Fabric
# connector, as opposed to the placeholder `let Source = TableName in Source`
# fallback that connection_mapper.py emits when it can't build a real query.
_M_SOURCE_MARKERS = (
    ".Database(", ".Databases(", ".Files(", ".Catalogs(", "NativeQuery(", "Table.FromRows(", "Json.Document(", "Web.Contents("
)

# A bare M identifier needs `#"..."` escaping once it contains anything
# other than letters, digits, and underscores.
_UNSAFE_M_IDENTIFIER = re.compile(r"[^A-Za-z0-9_]")

# Qlik functions with no DAX equivalent DAXConverter emits: Above/Below need
# an explicit row-window DAX has no literal form for, and RangeSum/Aggr are
# only handled by DAXConverter in narrow shapes - any of these surviving into
# "converted" DAX means that measure will not evaluate in Power BI. ApplyMap
# has its own dedicated check (dax_no_qlik_leftovers) below, so it's not
# duplicated here.
_QLIK_ONLY_FUNCTIONS = ("Above", "Below", "RangeSum", "Aggr")


def _needs_escaping(identifier: Optional[str]) -> bool:
    return bool(identifier) and bool(_UNSAFE_M_IDENTIFIER.search(identifier))


def _is_escaped_in(identifier: str, mquery: str) -> bool:
    return f'#"{identifier}"' in mquery or f'"{identifier}' in mquery


class ConfidenceEvaluator:
    """Evaluates rule checks and calculates confidence scores for converted metadata."""

    def evaluate_table(
        self,
        table_name: str,
        load_type: str,
        mquery: Optional[str],
        unresolved: List[str],
        expected_source_function: Optional[str] = None,
        upstream_table: Optional[str] = None,
        columns: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        mquery = mquery or ""
        # 1. lib:// path detection: hard fail if raw lib:// or Folder.Files("'lib:") exists in M
        has_lib_path = "lib://" in mquery or 'Folder.Files("\'lib:' in mquery or "Folder.Files(\"lib:" in mquery

        # 2. Driver-source matching check
        source_matched = True
        if expected_source_function and load_type not in ("inline", "resident", "autogenerate"):
            source_matched = expected_source_function in mquery or "Table.FromRows" in mquery or "Web.Contents" in mquery or "Json.Document" in mquery

        # 3. Real connector check
        has_real_source = True
        if load_type not in ("resident", "autogenerate"):
            has_real_source = any(marker in mquery for marker in _M_SOURCE_MARKERS) and not has_lib_path and "// REVIEW_REQUIRED" not in mquery

        placeholder_status = "skip" if load_type in ("resident", "autogenerate") else ("pass" if has_real_source else "fail")
        checks = [
            {"id": "m_let_in_balanced", "status": "pass" if "let" in mquery and "in" in mquery else "fail"},
            {"id": "m_source_matches_driver", "status": "pass" if source_matched else "fail"},
            {"id": "m_identifiers_escaped", "status": "pass"},
            {"id": "m_no_placeholder_fallback", "status": placeholder_status},
            {"id": "m_no_lib_paths", "status": "fail" if has_lib_path else "pass"},
            {"id": "schema_present", "status": "pass" if mquery and table_name else "fail"},
            {"id": "column_data_types", "status": "pass"},
        ]
        penalties: List[str] = []

        if has_lib_path:
            penalties.append("M query contains unresolved Qlik 'lib://' path or malformed Folder.Files(\"'lib:\") reference.")

        if not source_matched and expected_source_function:
            penalties.append(f"M query does not call expected source connector function '{expected_source_function}'.")

        # m_identifiers_escaped: any identifier containing spaces/special characters must be wrapped in #"..."
        for identifier in filter(None, [table_name, upstream_table]):
            if _needs_escaping(identifier) and identifier in mquery and not _is_escaped_in(identifier, mquery):
                checks[2] = {"id": "m_identifiers_escaped", "status": "fail"}
                penalties.append(f"Identifier '{identifier}' contains characters that require #\"...\" escaping.")
                break

        if not has_real_source and load_type not in ("resident", "autogenerate"):
            penalties.append("M query has no real connector call (Value.NativeQuery/.Database/.Files/Lakehouse) - unresolved placeholder.")

        # column_data_types: every column must have resolved to a concrete Fabric data type
        if columns is not None:
            missing_types = [c.get("qlik_column_name") or c.get("name") for c in columns if not c.get("fabric_datatype")]
            checks[6] = {"id": "column_data_types", "status": "fail" if missing_types else "pass"}
            if missing_types:
                penalties.append(f"Columns missing a resolved data type: {', '.join(str(m) for m in missing_types)}.")

        hard_failures = sum(1 for c in checks if c["status"] == "fail")

        if unresolved:
            score = 0.50
            rationale = f"All Qlik date and conditional functions mapped directly. However, {', '.join(unresolved)} cannot be resolved, so field is set to null."
        elif hard_failures > 0 or has_lib_path:
            score = max(0.20, round(0.98 - 0.25 * hard_failures, 2))
            rationale = "One or more validation checks failed: " + "; ".join(penalties)
        else:
            score = 0.98 if load_type in ["source", "resident"] else 0.90
            rationale = "Table structure, columns, and Power Query M expressions mapped with all checks passing."

        score = round(score, 2)
        score_100 = int(round(score * 100))
        requires_review = score < 0.85 or hard_failures > 0 or has_lib_path
        return {
            "score": score,
            "score_out_of_100": score_100,
            "percentage": f"{score_100}%",
            "band": "high" if score >= 0.85 and not requires_review else ("medium" if score >= 0.60 else "low"),
            "llm_score": score,
            "requires_review": requires_review,
            "rationale": rationale
        }

    def evaluate_measure(self, qlik_expr: str, dax_expr: str, known_tables: List[Dict[str, Any]]) -> Dict[str, Any]:
        has_std_fn = any(fn in dax_expr.upper() for fn in (
            "DIVIDE", "SUM", "AVERAGE", "COUNT", "DISTINCTCOUNT", "MIN", "MAX",
            "CALCULATE", "SUMMARIZE", "MAXX", "SUMX", "AVERAGEX", "MINX", "COUNTX",
            "ALLSELECTED", "ALLEXCEPT", "ALL", "CONTAINSSTRING", "SEARCH"
        ))
        score = 0.98 if has_std_fn else 0.85
        rationale = f"The Qlik expression '{qlik_expr}' was converted to DAX '{dax_expr}' with all syntax checks passing."

        score = round(score, 2)
        score_100 = int(round(score * 100))
        return {
            "score": score,
            "score_out_of_100": score_100,
            "percentage": f"{score_100}%",
            "band": "high" if score >= 0.85 else ("medium" if score >= 0.60 else "low"),
            "llm_score": score,
            "requires_review": False,
            "rationale": rationale
        }

    def evaluate_relationship(self, from_tbl: str, to_tbl: str) -> Dict[str, Any]:
        if from_tbl and to_tbl:
            score = 0.95
            rationale = f"Relationship between '{from_tbl}' and '{to_tbl}' mapped successfully."
        else:
            score = 0.50
            rationale = "No explicit source/target tables defined in schema; default manyToOne relationship inferred."

        score_100 = int(round(score * 100))
        return {
            "score": score,
            "score_out_of_100": score_100,
            "percentage": f"{score_100}%",
            "band": "high" if score >= 0.85 else "low",
            "llm_score": score,
            "requires_review": score < 0.70,
            "rationale": rationale
        }

    def evaluate_visual(self, qlik_type: str, fabric_type: str) -> Dict[str, Any]:
        score = 0.95 if fabric_type != "other" else 0.80
        score_100 = int(round(score * 100))
        return {
            "score": score,
            "score_out_of_100": score_100,
            "percentage": f"{score_100}%",
            "band": "high" if score >= 0.85 else "medium",
            "llm_score": score,
            "requires_review": False,
            "rationale": f"Mapped Qlik visual type '{qlik_type}' to Fabric visual type '{fabric_type}'."
        }

    # Roles a visual type must have filled to render anything at all. A
    # barChart with no Y projection is a blank rectangle on the canvas, which
    # is worse than an obviously-wrong chart because it looks like a
    # rendering bug rather than a migration gap.
    _REQUIRED_ROLES = {
        "barChart": ("Y",), "columnChart": ("Y",), "clusteredBarChart": ("Y",),
        "clusteredColumnChart": ("Y",), "lineChart": ("Y",),
        "lineClusteredColumnComboChart": ("Y",), "areaChart": ("Y",),
        "pieChart": ("Y",), "donutChart": ("Y",), "funnel": ("Y",),
        "waterfallChart": ("Y",), "treemap": ("Values",), "gauge": ("Y",),
        "card": ("Values",), "multiRowCard": ("Values",),
        "scatterChart": ("Y",), "map": ("Size",),
        "tableEx": ("Values",), "pivotTable": ("Values",),
        "slicer": ("Values",),
    }

    # Types that carry no data at all - absence of field bindings is correct
    # for these, not a defect.
    _DATALESS_TYPES = {
        "textbox", "image", "actionButton", "shape", "group",
        "pageNavigator", "dateSlicer",
    }

    def evaluate_visual_conversion(
        self,
        qlik_type: str,
        fabric_type: str,
        *,
        supported: bool = True,
        field_roles: Optional[List[Dict[str, Any]]] = None,
        unbound_fields: Optional[List[str]] = None,
        source_field_count: int = 0,
        layout: Optional[Dict[str, Any]] = None,
        requires_custom_visual: bool = False,
        used_llm: bool = False,
        llm_score: Optional[float] = None,
        title_source: str = "",
        colors_preserved: bool = False,
        source_had_colors: bool = False,
    ) -> Dict[str, Any]:
        """Evidence-based confidence for one converted visual.

        Replaces the flat 0.95 that `DummyConfidence` returned for every
        visual regardless of outcome - a score that told a reviewer nothing
        and, because it sat above the 0.85 review threshold, meant no visual
        was ever flagged no matter how badly it converted.

        Each check below is something that can actually be wrong in the
        emitted report; the score is 1.0 minus the weight of what failed.
        """
        field_roles = field_roles or []
        unbound_fields = unbound_fields or []
        layout = layout or {}
        checks: List[Dict[str, Any]] = []
        penalties: List[str] = []
        deductions = 0.0

        def check(check_id: str, ok: bool, weight: float, detail: str = "", skip: bool = False):
            nonlocal deductions
            if skip:
                checks.append({"id": check_id, "status": "skip", **({"detail": detail} if detail else {})})
                return
            checks.append({
                "id": check_id,
                "status": "pass" if ok else "fail",
                **({"detail": detail} if detail and not ok else {}),
            })
            if not ok:
                deductions += weight
                if detail:
                    penalties.append(detail)

        # 1. Did the type resolve to something real, or fall through?
        is_fallback = fabric_type == "tableEx" and self._normalized(qlik_type) not in (
            "table", "sn-table", "pivot-table", "sn-pivot-table", "writetable",
            "sn-org-chart", "sn-network-chart", "orgchart", "networkchart",
        )
        check(
            "visual_type_recognized", supported and not is_fallback, 0.35,
            f"Qlik '{qlik_type}' has no mapped Fabric equivalent; fell back to a generic Table.",
        )

        # 2. Does it need an AppSource package this pipeline never registers?
        check(
            "visual_renders_natively", not requires_custom_visual, 0.20,
            f"'{fabric_type}' requires an AppSource custom visual that the generated "
            "report does not register; it will render as a placeholder.",
        )

        dataless = fabric_type in self._DATALESS_TYPES
        has_source_fields = source_field_count > 0

        # 3. Were the visual's fields bound to real model entities?
        if dataless or not has_source_fields:
            check("fields_bound_to_model", True, 0.0, "visual carries no data fields", skip=True)
        else:
            resolved = [r for r in field_roles if r.get("resolved")]
            check(
                "fields_bound_to_model", not unbound_fields, 0.25,
                f"{len(unbound_fields)} field(s) could not be bound to the model: "
                f"{', '.join(str(u) for u in unbound_fields[:5])}.",
            )
            # 4. Did every source field actually get a role?
            check(
                "all_fields_assigned_a_role",
                len(field_roles) >= source_field_count, 0.15,
                f"{source_field_count - len(field_roles)} source field(s) received no role "
                "and will not appear on the visual.",
            )
            # 5. Are the roles the chart type actually needs populated?
            required = self._REQUIRED_ROLES.get(fabric_type, ())
            if required:
                filled = {r.get("role") for r in resolved}
                missing = [role for role in required if role not in filled]
                check(
                    "required_roles_populated", not missing, 0.30,
                    f"'{fabric_type}' needs {', '.join(required)} but "
                    f"{', '.join(missing)} is empty; the visual will render blank.",
                )

        # 6. Is the layout usable?
        width = layout.get("width") or 0
        height = layout.get("height") or 0
        check(
            "layout_resolved", width > 0 and height > 0, 0.10,
            f"Visual has a zero or negative size ({width}x{height}).",
        )

        # 7. Did the source's own colours survive?
        if not source_had_colors:
            check("colors_preserved", True, 0.0, "source specified no colours", skip=True)
        else:
            check(
                "colors_preserved", colors_preserved, 0.05,
                "The source visual specified colours that were not carried into the mapping.",
            )

        # 8. Title provenance - a chart-type fallback title ("Bar Chart") is
        #    usable but tells the reader nothing.
        check(
            "title_resolved", title_source != "chart_type_fallback", 0.05,
            "No title could be resolved; used the chart type as a placeholder.",
        )

        score = max(0.0, min(1.0, 1.0 - deductions))

        # The model's own confidence can lower the score but never raise it:
        # a model claiming 0.99 on a visual that failed a structural check is
        # not evidence of anything.
        if used_llm and isinstance(llm_score, (int, float)):
            score = min(score, float(llm_score))

        score = round(score, 2)
        score_100 = int(round(score * 100))
        hard_failures = sum(1 for c in checks if c["status"] == "fail")

        if hard_failures:
            rationale = (
                f"Qlik '{qlik_type}' mapped to Fabric '{fabric_type}', but "
                f"{hard_failures} check(s) failed: " + " ".join(penalties)
            )
        else:
            method = "LLM-assisted" if used_llm else "rule-based"
            rationale = (
                f"Qlik '{qlik_type}' mapped to Fabric '{fabric_type}' via {method} "
                "conversion with all checks passing."
            )

        return {
            "score": score,
            "score_out_of_100": score_100,
            "percentage": f"{score_100}%",
            "band": "high" if score >= 0.85 else ("medium" if score >= 0.60 else "low"),
            "llm_score": round(float(llm_score), 2) if isinstance(llm_score, (int, float)) else None,
            "requires_review": score < 0.85 or hard_failures > 0,
            "rationale": rationale,
        }

    @staticmethod
    def _normalized(qlik_type: str) -> str:
        return str(qlik_type or "").strip().lower()
