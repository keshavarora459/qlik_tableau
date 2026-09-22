"""Render the resolved Fabric model as prompt context.

Every LLM-assisted conversion in this service is *grounded*: the model is
never asked to invent a table or column name, it is asked to pick from the
schema that `_process_tables` already resolved. Without this block the model
guesses plausible-looking names ('FACT_TABLE'[Amount]) that do not exist in
the emitted TMDL, and the generated report binds to nothing.

The equivalent on the Tableau side of the platform lives in
`vl-t2f-mapping/app/utils/prompt_builder.py`; this is the Qlik shape of the
same idea.
"""

from typing import Any, Dict, List, Optional

from config import Config

# Datatypes worth telling the model about; anything else is noise.
_NUMERIC = {"double", "int64", "decimal", "number"}


def _column_label(column: Dict[str, Any]) -> str:
    """`FabricName (type)`, noting the Qlik name only when it differs.

    The rename matters: `_process_tables` sanitises dots and spaces out of
    column names for TMDL, so the Qlik name in a visual's y_axis often is
    not the name the model must emit.
    """
    fabric_name = column.get("fabric_column_name") or column.get("qlik_column_name") or ""
    qlik_name = column.get("qlik_column_name") or fabric_name
    dtype = column.get("fabric_datatype") or "string"
    label = f"{fabric_name} ({dtype})"
    if qlik_name and qlik_name != fabric_name:
        label += f" [Qlik: {qlik_name}]"
    return label


def build_schema_context(
    tables: List[Dict[str, Any]],
    measures: Optional[List[Dict[str, Any]]] = None,
    relationships: Optional[List[Dict[str, Any]]] = None,
    column_limit: Optional[int] = None,
) -> str:
    """A compact text description of the model the DAX must resolve against."""
    limit = column_limit or Config.LLM_SCHEMA_COLUMN_LIMIT
    lines: List[str] = []

    lines.append("TABLES AND COLUMNS:")
    for table in tables or []:
        if not isinstance(table, dict):
            continue
        name = table.get("name") or table.get("table_name")
        if not name:
            continue
        columns = [c for c in (table.get("columns") or []) if isinstance(c, dict)]
        shown = [_column_label(c) for c in columns[:limit]]
        overflow = f" ... (+{len(columns) - limit} more)" if len(columns) > limit else ""
        lines.append(f"- '{name}': {', '.join(shown)}{overflow}")

    if not tables:
        lines.append("- (no tables resolved)")

    if measures:
        lines.append("")
        lines.append(
            "AVAILABLE MEASURES (reference as [Name] with NO table prefix):"
        )
        for measure in measures:
            if not isinstance(measure, dict):
                continue
            m_name = measure.get("name")
            if m_name:
                lines.append(f"- [{m_name}]")

    if relationships:
        lines.append("")
        lines.append(
            "RELATIONSHIPS (use RELATED() toward the One side; aggregate over the Many side):"
        )
        for rel in relationships:
            if not isinstance(rel, dict):
                continue
            src_t = rel.get("source_table")
            src_c = rel.get("source_column")
            tgt_t = rel.get("target_table")
            tgt_c = rel.get("target_column")
            if not (src_t and tgt_t):
                continue
            cardinality = (
                (rel.get("fabric") or {}).get("cardinality")
                or rel.get("qlik_relationship_type")
                or "manyToOne"
            )
            lines.append(
                f"- '{src_t}'[{src_c}] -> '{tgt_t}'[{tgt_c}] ({cardinality}); "
                f"'{src_t}' is the Many side, '{tgt_t}' is the One side"
            )

    return "\n".join(lines)


def numeric_columns(tables: List[Dict[str, Any]]) -> List[str]:
    """`'Table'[Column]` for every numeric column, for aggregation hints."""
    found: List[str] = []
    for table in tables or []:
        if not isinstance(table, dict):
            continue
        name = table.get("name") or table.get("table_name")
        for column in table.get("columns") or []:
            if not isinstance(column, dict):
                continue
            if str(column.get("fabric_datatype") or "").lower() in _NUMERIC:
                col = column.get("fabric_column_name") or column.get("qlik_column_name")
                if name and col:
                    found.append(f"'{name}'[{col}]")
    return found
