"""
Deterministic TMDL emission.
Ported and enhanced from vl-q2f-mapping/src/converters/tmdl_builder.py.

This module lays out already-converted DAX and M in TMDL's indentation-sensitive syntax.
Deterministic assembly ensures that output is structurally valid TMDL.
"""
import re
import uuid
from typing import Any, Dict, List, Optional

TAB = "\t"

# Qlik / BI / Fabric datatype -> TMDL dataType keyword
TMDL_TYPES = {
    "STRING": "string",
    "TEXT": "string",
    "INTEGER": "int64",
    "INT": "int64",
    "INT64": "int64",
    "NUMERIC": "double",
    "NUMBER": "double",
    "REAL": "double",
    "DOUBLE": "double",
    "FLOAT": "double",
    "DECIMAL": "decimal",
    "DATE": "dateTime",
    "TIMESTAMP": "dateTime",
    "DATETIME": "dateTime",
    "TIME": "dateTime",
    "BOOLEAN": "boolean",
    "BOOL": "boolean",
}

_NEEDS_QUOTING = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NAMESPACE_TMDL = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def tmdl_type_for(raw_type: Optional[str]) -> str:
    """Map any Qlik/Fabric datatype string to the TMDL dataType keyword."""
    raw = str(raw_type or "").strip().upper()
    if not raw:
        return "string"
    if raw in TMDL_TYPES:
        return TMDL_TYPES[raw]
    if any(k in raw for k in ["INT", "NUM", "DEC", "REAL", "FLOAT", "DOUBLE", "MONEY", "CURRENCY", "BIGINT", "SMALLINT", "TINYINT", "BYTE"]):
        return "double"
    if any(k in raw for k in ["DATE", "TIME", "TIMESTAMP"]):
        return "dateTime"
    if any(k in raw for k in ["BOOL", "LOGICAL"]):
        return "boolean"
    return "string"


def quote_name(name: str) -> str:
    """TMDL names containing anything but word characters are single-quoted."""
    text = str(name or "")
    return text if _NEEDS_QUOTING.match(text) else f"'{text}'"


def new_lineage_tag() -> str:
    return str(uuid.uuid4())


def stable_lineage_tag(seed: str) -> str:
    """Deterministic UUID from a seed string — same seed = same UUID across runs."""
    return str(uuid.uuid5(NAMESPACE_TMDL, seed))


def _expression_block(expression: str, indent: int) -> List[str]:
    """
    Render an expression after `=`.
    Single-line expressions sit inline; multi-line ones use the fenced ``` form,
    which is the only way TMDL accepts embedded newlines.
    """
    pad = TAB * indent
    text = (expression or "").strip()
    if "\n" not in text:
        return [f" = {text}"]

    lines = [" = ```", *[f"{pad}{line}" for line in text.splitlines()], f"{pad}```"]
    return lines


def build_measure(
    name: str,
    dax: str,
    format_string: Optional[str] = None,
    lineage_tag: Optional[str] = None,
    description: Optional[str] = None,
    indent: int = 1,
) -> str:
    pad = TAB * indent
    head = f"{pad}measure {quote_name(name)}"
    parts = _expression_block(dax, indent + 1)

    lines = [head + parts[0]] if len(parts) == 1 else [head + parts[0], *parts[1:]]
    if format_string:
        lines.append(f"{pad}{TAB}formatString: {format_string}")
    lines.append(f"{pad}{TAB}lineageTag: {lineage_tag or new_lineage_tag()}")
    if description:
        lines.append(f"{pad}{TAB}/// {description}")
    return "\n".join(lines)


def build_column(
    name: str,
    data_type: str,
    source_column: Optional[str] = None,
    dax: Optional[str] = None,
    lineage_tag: Optional[str] = None,
    summarize_by: str = "none",
    is_hidden: bool = False,
    format_string: Optional[str] = None,
    indent: int = 1,
) -> str:
    pad = TAB * indent
    if dax:
        parts = _expression_block(dax, indent + 1)
        lines = [f"{pad}column {quote_name(name)}" + parts[0]]
        lines.extend(parts[1:])
    else:
        lines = [f"{pad}column {quote_name(name)}"]

    lines.append(f"{pad}{TAB}dataType: {data_type}")
    if is_hidden:
        lines.append(f"{pad}{TAB}isHidden")
    lines.append(f"{pad}{TAB}lineageTag: {lineage_tag or new_lineage_tag()}")
    lines.append(f"{pad}{TAB}summarizeBy: {summarize_by}")
    if not dax:
        lines.append(f"{pad}{TAB}sourceColumn: {source_column or name}")
    if format_string:
        lines.append(f"{pad}{TAB}formatString: {format_string}")
    return "\n".join(lines)


def build_partition(
    name: str,
    m_expression: str,
    mode: str = "import",
    indent: int = 1,
) -> str:
    pad = TAB * indent
    lines = [f"{pad}partition {quote_name(name)} = m", f"{pad}{TAB}mode: {mode}", f"{pad}{TAB}source ="]
    lines.extend(f"{pad}{TAB}{TAB}{line}" for line in (m_expression or "").splitlines())
    return "\n".join(lines)


def build_table(
    name: str,
    columns: Optional[List[Dict[str, Any]]] = None,
    measures: Optional[List[Dict[str, Any]]] = None,
    partition: Optional[Dict[str, Any]] = None,
    lineage_tag: Optional[str] = None,
) -> str:
    """Assemble a complete `table` block."""
    lines = [f"table {quote_name(name)}", f"{TAB}lineageTag: {lineage_tag or stable_lineage_tag(f'table:{name}')}", ""]

    for column in columns or []:
        lines.append(
            build_column(
                name=column.get("fabric_column_name") or column.get("name"),
                data_type=column.get("data_type") or tmdl_type_for(column.get("fabric_datatype") or column.get("qlik_datatype")),
                source_column=column.get("source_column"),
                dax=column.get("dax"),
                lineage_tag=column.get("lineage_tag"),
                summarize_by=column.get("summarize_by", "none"),
                is_hidden=column.get("is_hidden", False),
                format_string=column.get("format_string"),
            )
        )
        lines.append("")

    for measure in measures or []:
        lines.append(
            build_measure(
                name=measure.get("name"),
                dax=measure.get("dax") or measure.get("dax_expression", ""),
                format_string=measure.get("format_string"),
                lineage_tag=measure.get("lineage_tag"),
            )
        )
        lines.append("")

    if partition and partition.get("m_expression"):
        lines.append(
            build_partition(
                name=partition.get("name") or name,
                m_expression=partition["m_expression"],
                mode=partition.get("mode", "import"),
            )
        )

    return "\n".join(lines).rstrip() + "\n"


def build_relationship(
    from_table: str,
    from_column: str,
    to_table: str,
    to_column: str,
    cardinality: str = "manyToOne",
    cross_filter: str = "singleDirection",
    is_active: bool = True,
    name: Optional[str] = None,
) -> str:
    identifier = name or str(uuid.uuid4())
    lines = [
        f"relationship {identifier}",
        f"{TAB}fromColumn: {quote_name(from_table)}.{quote_name(from_column)}",
        f"{TAB}toColumn: {quote_name(to_table)}.{quote_name(to_column)}",
    ]
    if cardinality == "manyToMany":
        lines.append(f"{TAB}crossFilteringBehavior: bothDirections")
        lines.append(f"{TAB}toCardinality: many")
    elif cardinality == "oneToOne":
        lines.append(f"{TAB}crossFilteringBehavior: bothDirections")
        lines.append(f"{TAB}fromCardinality: one")
    elif cross_filter != "singleDirection":
        lines.append(f"{TAB}crossFilteringBehavior: bothDirections")
    if not is_active:
        lines.append(f"{TAB}isActive: false")
    return "\n".join(lines)


def build_role(
    name: str,
    table_permissions: Optional[List[Dict[str, str]]] = None,
    members: Optional[List[str]] = None,
) -> str:
    """A row-level security role, from converted section access."""
    lines = [f"role {quote_name(name)}", f"{TAB}modelPermission: read"]

    for permission in table_permissions or []:
        table = permission.get("table")
        expression = permission.get("filter") or permission.get("dax_filter")
        if not table or not expression:
            continue
        lines.append("")
        lines.append(f"{TAB}tablePermission {quote_name(table)} = {expression}")

    for member in members or []:
        lines.append("")
        lines.append(f"{TAB}member {member}")

    return "\n".join(lines)


# ── Backward-compatible TMDLGenerator class ───────────────────────────────────

class TMDLGenerator:
    """Generates Fabric metadata structures and deterministic lineageTag UUIDs."""

    def generate_uuid(self, seed: str) -> str:
        return stable_lineage_tag(seed)

    def generate_table_tmdl(self, table_name: str, columns: List[Dict[str, Any]], mquery: Optional[str]) -> Dict[str, Any]:
        """Generate a dictionary representation of the TMDL definition for a table."""
        mquery = mquery or ""
        tbl_lineage = self.generate_uuid(f"table:{table_name}")

        cols_metadata = []
        for col in columns:
            col_name = col.get("fabric_column_name") or col.get("qlik_column_name") or col.get("name")
            dtype = tmdl_type_for(col.get("fabric_datatype") or col.get("data_type") or "STRING")
            col_lineage = self.generate_uuid(f"column:{table_name}:{col_name}")
            summarize = col.get("summarize_by")
            if not summarize:
                is_id = str(col_name or "").lower().endswith(("id", "code", "key"))
                summarize = "none" if (dtype not in ("double", "int64", "decimal") or is_id or str(col_name or "").lower() == "year") else "sum"

            cols_metadata.append({
                "qlik_column_name": col.get("qlik_column_name") or col_name,
                "qlik_datatype": col.get("qlik_datatype") or "STRING",
                "fabric_column_name": col_name,
                "fabric_datatype": dtype,
                "summarize_by": summarize,
                "is_hidden": False,
                "format_string": col.get("format_string") or ("#,##0.00" if dtype in ["double", "decimal"] else "General Text"),
                "lineage_tag": col_lineage,
            })

        return {
            "table_name": table_name,
            "lineage_tag": tbl_lineage,
            "partition": {
                "name": table_name,
                "mode": "import",
                "source_type": "m",
                "m_expression": mquery
            },
            "m_query": mquery,
            "columns": cols_metadata
        }

    def generate_measure_tmdl(self, name: str, dax_expr: str, format_str: str) -> Dict[str, Any]:
        lineage = self.generate_uuid(f"measure:{name}")
        expr_upper = (dax_expr or "").upper()
        if any(f in expr_upper for f in ("SUM", "AVERAGE", "COUNT", "DIVIDE", "MIN(", "MAX(")):
            data_type = "double"
        elif "DISTINCTCOUNT" in expr_upper or "COUNTROWS" in expr_upper:
            data_type = "int64"
        else:
            data_type = "string"

        return {
            "dax_expression": dax_expr,
            "data_type": data_type,
            "format_string": format_str,
            "lineage_tag": lineage
        }

    def generate_relationship_tmdl(
        self,
        from_tbl: str,
        from_col: str,
        to_tbl: str,
        to_col: str,
        cardinality: str = "manyToOne",
        cross_filter: str = "singleDirection",
        is_active: bool = True
    ) -> Dict[str, Any]:
        rel_id = self.generate_uuid(f"rel:{from_tbl}.{from_col}->{to_tbl}.{to_col}")
        return {
            "from_table": from_tbl,
            "from_column": from_col,
            "to_table": to_tbl,
            "to_column": to_col,
            "cardinality": cardinality,
            "cross_filter_direction": cross_filter,
            "is_active": is_active,
            "lineage_tag": rel_id
        }
