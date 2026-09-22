"""Contract Adapters and Normalizer for converting Qlik and Tableau payloads to Common IR Contract 2.0."""

import re
from typing import Any, Dict, List, Optional, Tuple

from services.common_ir.models import (
    CalculatedColumnIR,
    CanonicalContract2,
    ColumnIR,
    ConfidenceIR,
    DiagnosticIR,
    FieldIR,
    FilterIR,
    LineageIR,
    MeasureIR,
    ParameterIR,
    RelationshipIR,
    SecurityIR,
    SetIR,
    TableIR,
    VisualIR,
)


class QlikContractAdapter:
    """Adapts Qlik Mapping outputs and legacy raw payloads into Canonical Contract 2.0 Common IR."""

    @staticmethod
    def adapt(payload: Dict[str, Any]) -> CanonicalContract2:
        contract = CanonicalContract2()
        contract.contract_version = "2.0"
        contract.status = payload.get("status", "success")
        contract.message = payload.get("message", "Qlik Contract 2.0 generated")
        contract.error_message = payload.get("error_message")

        # Identity & Metadata
        contract.workbook_metadata = payload.get("workbook_metadata") or {
            "app_id": payload.get("app_id", ""),
            "app_name": payload.get("app_name", ""),
            "run_id": payload.get("run_id", ""),
        }
        contract.app_metadata = payload.get("app_metadata", {})
        contract.app_layout = payload.get("app_layout", {})
        contract.connections = payload.get("connections") or payload.get("datasources") or []

        # Tables & Columns
        raw_tables = payload.get("tables", [])
        tables_ir: List[TableIR] = []
        columns_ir: List[ColumnIR] = []
        fields_ir: List[FieldIR] = []

        for t_idx, t in enumerate(raw_tables):
            t_name = t.get("table_name") or t.get("name") or f"Table_{t_idx}"
            canonical_t_name = t.get("canonical_name") or t_name
            t_id = t.get("table_id") or f"tbl_{t_name}"

            cols_for_table = []
            for col in t.get("fields", []) or t.get("columns", []):
                col_name = col.get("name") or col.get("column_name") or ""
                canon_col_name = col.get("canonical_name") or col_name
                dt = col.get("dataType") or col.get("data_type") or "string"
                col_ir = ColumnIR(
                    name=col_name,
                    canonical_name=canon_col_name,
                    datatype=dt.lower(),
                    nullable=col.get("nullable", True),
                    source_type=dt,
                    semantic_role=col.get("semantic_role", "dimension"),
                    expression=col.get("expression"),
                    source_expression=col.get("source_expression"),
                    lineage=[f"qlik:{t_name}.{col_name}"],
                )
                cols_for_table.append(col_ir)
                columns_ir.append(col_ir)

                field_ir = FieldIR(
                    field_id=f"{t_name}.{col_name}",
                    source_platform="qlik",
                    source_table=t_name,
                    source_field=col_name,
                    canonical_table=canonical_t_name,
                    canonical_column=canon_col_name,
                    datatype=dt.lower(),
                    semantic_role="dimension",
                    lineage=[f"qlik:{t_name}.{col_name}"],
                )
                fields_ir.append(field_ir)

            tbl_ir = TableIR(
                table_id=t_id,
                source_name=t_name,
                canonical_name=canonical_t_name,
                table_type=t.get("table_type", "dimension"),
                columns=cols_for_table,
                source_lineage=[f"qlik:{t_name}"],
                m_query=t.get("m_query"),
            )
            tables_ir.append(tbl_ir)

        contract.tables = tables_ir
        contract.columns = columns_ir
        contract.fields = fields_ir

        # Relationships
        rels_ir: List[RelationshipIR] = []
        raw_rels = payload.get("relationships", [])
        for r_idx, r in enumerate(raw_rels):
            from_tbl = r.get("table1") or r.get("from_table") or r.get("fromTable") or ""
            from_col = r.get("field1") or r.get("from_column") or r.get("fromColumn") or ""
            to_tbl = r.get("table2") or r.get("to_table") or r.get("toTable") or ""
            to_col = r.get("field2") or r.get("to_column") or r.get("toColumn") or ""
            card = r.get("cardinality", "many_to_one").replace("-", "_")

            rels_ir.append(RelationshipIR(
                relationship_id=f"rel_{r_idx}_{from_tbl}_{to_tbl}",
                from_table=from_tbl,
                from_column=from_col,
                to_table=to_tbl,
                to_column=to_col,
                cardinality=card,
                cross_filter=r.get("cross_filter", "single"),
                active=r.get("active", True),
                source_platform="qlik",
                confidence_score=r.get("confidence_score", 100.0),
            ))
        contract.relationships = rels_ir

        # Measures
        measures_ir: List[MeasureIR] = []
        raw_measures = payload.get("measures", [])
        for m in raw_measures:
            # Handle Qlik qMeasure shape or standard mapping shape
            m_name = (
                m.get("name")
                or (m.get("qMeasure") or {}).get("qLabel")
                or (m.get("qMetaDef") or {}).get("title")
                or "Unnamed_Measure"
            )
            expr = (
                m.get("expression")
                or m.get("dax_expression")
                or (m.get("qMeasure") or {}).get("qDef")
                or ""
            )
            src_expr = m.get("source_expression") or (m.get("qMeasure") or {}).get("qDef") or ""
            fmt = m.get("formatting") or (m.get("qMeasure") or {}).get("qNumFormat") or {}
            score = float(m.get("confidence_score", m.get("confidence", 95.0)))
            req_rev = bool(m.get("requires_review", False))

            measures_ir.append(MeasureIR(
                name=m_name,
                expression=expr,
                expression_language="DAX",
                source_expression=src_expr,
                source_platform="qlik",
                mapping_method=m.get("mapping_method", "deterministic"),
                confidence_score=score,
                requires_review=req_rev,
                lineage=[f"qlik:measure:{m_name}"],
                formatting=fmt,
                dependencies=m.get("dependencies", []),
                table_name=m.get("table_name") or (m.get("tables", [None])[0] if m.get("tables") else None),
            ))
        contract.measures = measures_ir

        # Dimensions & Calculated Columns
        contract.dimensions = payload.get("dimensions", [])
        calc_cols_ir: List[CalculatedColumnIR] = []
        for d in contract.dimensions:
            if d.get("is_calculated") or d.get("calculated"):
                d_name = d.get("title") or (d.get("qDim") or {}).get("title") or "CalcCol"
                t_name = (d.get("tables") or ["Main"])[0]
                src_expr = (d.get("qDim") or {}).get("qFieldDefs", [""])[0]
                expr = d.get("expression") or d.get("dax_expression") or src_expr
                calc_cols_ir.append(CalculatedColumnIR(
                    name=d_name,
                    expression=expr,
                    table_name=t_name,
                    expression_language="DAX",
                    source_expression=src_expr,
                    datatype=d.get("dataType", "string"),
                    dependencies=d.get("dependencies", []),
                    lineage=[f"qlik:calc_col:{d_name}"],
                    confidence=float(d.get("confidence_score", 95.0)),
                    requires_review=bool(d.get("requires_review", False)),
                ))
        contract.calculated_columns = calc_cols_ir

        # Visuals
        visuals_ir: List[VisualIR] = []
        raw_visuals = payload.get("visuals", []) or payload.get("visualizations", [])
        if isinstance(raw_visuals, dict):
            raw_visuals = raw_visuals.get("sheet_visuals") or raw_visuals.get("visuals") or []

        for v_idx, v in enumerate(raw_visuals):
            v_id = v.get("visual_id") or v.get("id") or f"vis_{v_idx}"
            v_type = v.get("type") or v.get("visual_type") or "table"
            v_title = v.get("title") or (v.get("properties") or {}).get("title") or ""
            dims = v.get("dimensions") or []
            meas = v.get("measures") or []
            enc = v.get("encoding") or {}

            visuals_ir.append(VisualIR(
                visual_id=v_id,
                type=v_type,
                title=v_title,
                dimensions=[d if isinstance(d, str) else d.get("name", "") for d in dims],
                measures=[m if isinstance(m, str) else m.get("name", "") for m in meas],
                encoding=enc,
                filters=[FilterIR(field=f.get("field", ""), operator=f.get("operator", "equal"), value=f.get("value")) for f in v.get("filters", [])],
                properties=v.get("properties", {}),
                coordinates=v.get("coordinates", {}),
                source_sheet=v.get("source_sheet") or v.get("sheet_name") or "",
                confidence=float(v.get("confidence_score", 95.0)),
            ))
        contract.visuals = visuals_ir

        # Security
        sec_raw = payload.get("rls") or payload.get("security") or payload.get("section_access")
        if isinstance(sec_raw, dict):
            contract.security = SecurityIR(
                type="rls",
                roles=sec_raw.get("roles", []),
                source=sec_raw.get("source", "qlik_section_access"),
                review_required=sec_raw.get("review_required", False),
                unsupported_reasons=sec_raw.get("unsupported_reasons", []),
            )
            contract.rls = contract.security.to_dict()

        # Other Contract 2.0 sections
        contract.variables = payload.get("variables", [])
        contract.stories = payload.get("stories", [])
        contract.bookmarks = payload.get("bookmarks", [])
        contract.themes = payload.get("themes", [])
        contract.extensions = payload.get("extensions", [])
        contract.data_files = payload.get("data_files") or payload.get("dataFiles") or []
        contract.conversion_summary = payload.get("conversion_summary", {})
        contract.llm_status = payload.get("llm_status", {})

        return contract


class TableauContractAdapter:
    """Adapts Tableau Mapping outputs and payloads into Canonical Contract 2.0 Common IR."""

    @staticmethod
    def adapt(payload: Dict[str, Any]) -> CanonicalContract2:
        contract = CanonicalContract2()
        contract.contract_version = "2.0"
        contract.status = payload.get("status", "success")
        contract.message = payload.get("message", "Tableau Contract 2.0 generated")
        contract.error_message = payload.get("error_message")

        # Identity & Metadata
        contract.workbook_metadata = payload.get("workbook_metadata") or {
            "workbook_id": payload.get("workbook_id") or payload.get("project_id", ""),
            "workbook_name": payload.get("workbook_name") or payload.get("project_name", ""),
            "run_id": payload.get("run_id", ""),
        }
        contract.app_metadata = payload.get("app_metadata", {})
        contract.connections = payload.get("connections") or payload.get("datasources") or []

        # Tables & Columns
        raw_tables = payload.get("tables", [])
        tables_ir: List[TableIR] = []
        columns_ir: List[ColumnIR] = []
        fields_ir: List[FieldIR] = []

        for t_idx, t in enumerate(raw_tables):
            t_name = t.get("table_name") or t.get("name") or f"Table_{t_idx}"
            canonical_t_name = t.get("canonical_name") or t.get("bi_table_name") or t_name
            t_id = t.get("table_id") or f"tbl_{t_name}"

            cols_for_table = []
            for col in t.get("fields", []) or t.get("columns", []):
                col_name = col.get("name") or col.get("column_name") or col.get("tableau_column_name") or ""
                canon_col_name = col.get("canonical_name") or col.get("bi_column_name") or col_name
                dt = col.get("dataType") or col.get("data_type") or "string"
                col_ir = ColumnIR(
                    name=col_name,
                    canonical_name=canon_col_name,
                    datatype=dt.lower(),
                    nullable=col.get("nullable", True),
                    source_type=dt,
                    semantic_role=col.get("semantic_role", "dimension"),
                    expression=col.get("expression"),
                    source_expression=col.get("source_expression"),
                    lineage=[f"tableau:{t_name}.{col_name}"],
                )
                cols_for_table.append(col_ir)
                columns_ir.append(col_ir)

                field_ir = FieldIR(
                    field_id=f"{t_name}.{col_name}",
                    source_platform="tableau",
                    source_table=t_name,
                    source_field=col_name,
                    canonical_table=canonical_t_name,
                    canonical_column=canon_col_name,
                    datatype=dt.lower(),
                    semantic_role="dimension",
                    lineage=[f"tableau:{t_name}.{col_name}"],
                )
                fields_ir.append(field_ir)

            tbl_ir = TableIR(
                table_id=t_id,
                source_name=t_name,
                canonical_name=canonical_t_name,
                table_type=t.get("table_type", "dimension"),
                columns=cols_for_table,
                source_lineage=[f"tableau:{t_name}"],
                m_query=t.get("m_query"),
            )
            tables_ir.append(tbl_ir)

        contract.tables = tables_ir
        contract.columns = columns_ir
        contract.fields = fields_ir

        # Relationships
        rels_ir: List[RelationshipIR] = []
        raw_rels = payload.get("relationships", [])
        for r_idx, r in enumerate(raw_rels):
            from_tbl = r.get("from_table") or r.get("fromTable") or r.get("table1") or ""
            from_col = r.get("from_column") or r.get("fromColumn") or r.get("field1") or ""
            to_tbl = r.get("to_table") or r.get("toTable") or r.get("table2") or ""
            to_col = r.get("to_column") or r.get("toColumn") or r.get("field2") or ""
            card = r.get("cardinality", "many_to_one").replace("-", "_")

            rels_ir.append(RelationshipIR(
                relationship_id=f"rel_{r_idx}_{from_tbl}_{to_tbl}",
                from_table=from_tbl,
                from_column=from_col,
                to_table=to_tbl,
                to_column=to_col,
                cardinality=card,
                cross_filter=r.get("cross_filter") or r.get("crossFilteringBehavior") or "single",
                active=r.get("active", True),
                source_platform="tableau",
                confidence_score=r.get("confidence_score", 100.0),
            ))
        contract.relationships = rels_ir

        # Measures / Calculated Fields / LODs
        measures_ir: List[MeasureIR] = []
        raw_measures = payload.get("measures") or payload.get("dax_measures") or []
        if not raw_measures and "Calculated Fields & LODs" in payload:
            calc_lods = payload.get("Calculated Fields & LODs", [])
            for block in calc_lods:
                if isinstance(block, dict) and "calculated_fields" in block:
                    raw_measures.extend(block.get("calculated_fields", []))

        for m in raw_measures:
            m_name = m.get("name") or m.get("calculated_field_name") or m.get("field_name") or "Unnamed_Measure"
            expr = m.get("expression") or m.get("dax_expression") or m.get("dax") or ""
            src_expr = m.get("source_expression") or m.get("tableau_formula") or m.get("formula") or ""
            score = float(m.get("confidence_score", m.get("confidence", 95.0)))
            req_rev = bool(m.get("requires_review", False))

            measures_ir.append(MeasureIR(
                name=m_name,
                expression=expr,
                expression_language="DAX",
                source_expression=src_expr,
                source_platform="tableau",
                mapping_method=m.get("mapping_method", "deterministic"),
                confidence_score=score,
                requires_review=req_rev,
                lineage=[f"tableau:calc:{m_name}"],
                formatting=m.get("formatting", {}),
                dependencies=m.get("dependencies", []),
                table_name=m.get("table_name") or m.get("target_table"),
            ))
        contract.measures = measures_ir

        # Parameters
        params_ir: List[ParameterIR] = []
        raw_params = payload.get("parameters") or payload.get("Parameters") or []
        for p in raw_params:
            params_ir.append(ParameterIR(
                name=p.get("name", ""),
                datatype=p.get("datatype") or p.get("data_type") or "string",
                default_value=p.get("default_value") or p.get("current_value"),
                allowed_values=p.get("allowable_values") or p.get("allowed_values") or [],
                source_expression=p.get("source_expression"),
            ))
        contract.parameters = params_ir

        # Sets
        sets_ir: List[SetIR] = []
        raw_sets = payload.get("sets") or payload.get("Sets") or []
        for s_idx, s in enumerate(raw_sets):
            sets_ir.append(SetIR(
                set_id=s.get("set_id") or f"set_{s_idx}",
                name=s.get("name", ""),
                field=s.get("field") or s.get("column", ""),
                operation=s.get("operation", "include"),
                values=s.get("values", []),
                expression=s.get("expression"),
                source_platform="tableau",
                requires_review=bool(s.get("requires_review", False)),
            ))
        contract.sets = sets_ir

        # Visuals
        visuals_ir: List[VisualIR] = []
        raw_visuals = payload.get("visuals") or payload.get("sheet_visuals") or []
        if isinstance(raw_visuals, dict):
            raw_visuals = raw_visuals.get("sheet_visuals") or raw_visuals.get("visuals") or []

        for v_idx, v in enumerate(raw_visuals):
            v_id = v.get("visual_id") or v.get("id") or f"vis_{v_idx}"
            v_type = v.get("type") or v.get("visual_type") or "barChart"
            v_title = v.get("title") or (v.get("properties") or {}).get("title") or ""
            dims = v.get("dimensions") or []
            meas = v.get("measures") or []
            enc = v.get("encoding") or {}

            # Handle encoding.x / encoding.y fallback if dimensions/measures empty
            if not dims and "x" in enc:
                dims = [enc["x"]] if isinstance(enc["x"], str) else enc["x"]
            if not meas and "y" in enc:
                meas = [enc["y"]] if isinstance(enc["y"], str) else enc["y"]

            visuals_ir.append(VisualIR(
                visual_id=v_id,
                type=v_type,
                title=v_title,
                dimensions=[d if isinstance(d, str) else d.get("name", "") for d in dims],
                measures=[m if isinstance(m, str) else m.get("name", "") for m in meas],
                encoding=enc,
                filters=[FilterIR(field=f.get("field", ""), operator=f.get("operator", "equal"), value=f.get("value")) for f in v.get("filters", [])],
                properties=v.get("properties", {}),
                coordinates=v.get("coordinates", {}),
                source_sheet=v.get("source_sheet") or v.get("sheet_name") or "",
                confidence=float(v.get("confidence_score", 95.0)),
            ))
        contract.visuals = visuals_ir

        # Security
        sec_raw = payload.get("rls") or payload.get("security")
        if isinstance(sec_raw, dict):
            contract.security = SecurityIR(
                type="rls",
                roles=sec_raw.get("roles", []),
                source=sec_raw.get("source", "tableau_user_filters"),
                review_required=sec_raw.get("review_required", False),
                unsupported_reasons=sec_raw.get("unsupported_reasons", []),
            )
            contract.rls = contract.security.to_dict()

        contract.data_files = payload.get("data_files") or payload.get("dataFiles") or []
        contract.conversion_summary = payload.get("conversion_summary", {})
        contract.llm_status = payload.get("llm_status", {})

        return contract


class ContractNormalizer:
    """Universal Normalizer routing Qlik or Tableau payloads into Canonical Contract 2.0."""

    @classmethod
    def normalize(cls, payload: Dict[str, Any]) -> CanonicalContract2:
        if not isinstance(payload, dict):
            return CanonicalContract2(status="error", error_message="Payload must be a dictionary")

        # Detect platform
        source_type = (
            payload.get("source_type")
            or payload.get("source_platform")
            or (payload.get("workbook_metadata") or {}).get("source_type")
            or ("qlik" if "qMeasure" in str(payload) or "section_access" in payload else "tableau")
        )

        if str(source_type).lower().strip() == "qlik":
            return QlikContractAdapter.adapt(payload)
        else:
            return TableauContractAdapter.adapt(payload)
