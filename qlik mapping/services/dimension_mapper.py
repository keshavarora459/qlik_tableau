"""Map Qlik master dimensions to Fabric columns/hierarchies.

Previously `dimensions` was a raw pass-through: whatever the parsing agent
sent was echoed unchanged, with no `fabric` block and no confidence, so the
section carried no conversion at all.

Calculated dimensions become DAX calculated columns, drill-down groups become
hierarchies, and plain field dimensions map straight to a model column.
"""

import re
import uuid
from typing import Any, Dict, List, Optional

# A bare Qlik identifier: not inside quotes and not already qualified.
IDENTIFIER = re.compile(r"(?<!\[)\b([A-Za-z_]\w*(?:\.\w+)*)\b(?!\s*\()")
# Qlik string literals use single quotes; DAX uses double quotes.
SINGLE_QUOTED = re.compile(r"'([^']*)'")

DAX_KEYWORDS = {
    "and", "or", "not", "true", "false", "in", "if", "then", "else",
    "blank", "null", "date", "time",
}


class DimensionMapper:
    def __init__(self):
        self.column_index: Dict[str, str] = {}

    # -- context -----------------------------------------------------------

    def index_columns(self, tables: List[Dict[str, Any]]) -> None:
        """Map column name -> owning table, so identifiers can be qualified."""
        self.column_index = {}
        for table in tables or []:
            if not isinstance(table, dict):
                continue
            name = table.get("name") or table.get("table_name")
            for column in table.get("columns") or table.get("fields") or []:
                if not isinstance(column, dict):
                    continue
                col = column.get("qlik_column_name") or column.get("name")
                if col and col not in self.column_index:
                    self.column_index[col] = name

    # -- expression translation -------------------------------------------

    def to_dax(self, expression: str, table: str) -> str:
        """Qualify bare field names and convert Qlik literals to DAX form."""
        if not expression:
            return ""

        # Protect string literals, converting them to DAX double quotes, so
        # their contents are never mistaken for field names.
        literals: List[str] = []

        def stash(match: re.Match) -> str:
            literals.append(match.group(1))
            return f"\x00{len(literals) - 1}\x00"

        working = SINGLE_QUOTED.sub(stash, expression)

        def qualify(match: re.Match) -> str:
            token = match.group(1)
            if token.lower() in DAX_KEYWORDS or token.isdigit():
                return token
            owner = self.column_index.get(token, table if not self.column_index else None)
            return f"'{owner}'[{token}]" if owner else token

        working = IDENTIFIER.sub(qualify, working)

        for index, literal in enumerate(literals):
            working = working.replace(f"\x00{index}\x00", f'"{literal}"')
        return working.strip()

    # -- mapping -----------------------------------------------------------

    def _table_for(self, dimension: Dict[str, Any]) -> str:
        tables = dimension.get("tables") or []
        if tables:
            return str(tables[0])
        for field in dimension.get("field_defs") or []:
            owner = self.column_index.get(str(field).lstrip("="))
            if owner:
                return owner
        return "Table"

    def map_dimension(self, dimension: Dict[str, Any]) -> Dict[str, Any]:
        table = self._table_for(dimension)
        name = dimension.get("name") or "Dimension"
        expression = (dimension.get("qlik_expression") or "").strip()
        is_calculated = bool(dimension.get("is_calculated"))
        data_type = "number" if "NUMBER" in str(dimension.get("qlik_datatype", "")).upper() else "string"
        lineage_tag = str(uuid.uuid4())

        if dimension.get("is_drilldown"):
            levels = [str(f).lstrip("=") for f in dimension.get("field_defs") or []]
            fabric = {
                "kind": "hierarchy",
                "hierarchy_name": name,
                "levels": levels,
                "table": table,
                "lineage_tag": lineage_tag,
            }
        else:
            dax = self.to_dax(expression, table)
            fabric = {
                "dax_expression": dax,
                "is_calculated": is_calculated,
                "data_type": data_type,
                "table": table,
                "lineage_tag": lineage_tag,
            }

        return {
            "name": name,
            "qlik_expression": expression,
            "qlik_datatype": dimension.get("qlik_datatype"),
            "nature": dimension.get("nature"),
            "tables": dimension.get("tables") or [table],
            "limitations": [],
            "fabric": fabric,
            "confidence": self._confidence(dimension, fabric),
        }

    # -- confidence --------------------------------------------------------

    def _confidence(self, dimension: Dict[str, Any], fabric: Dict[str, Any]) -> Dict[str, Any]:
        checks: List[Dict[str, str]] = []
        score = 1.0

        if dimension.get("tables"):
            checks.append({"id": "dim_table_resolved", "status": "pass"})
        else:
            checks.append({"id": "dim_table_resolved", "status": "warn",
                           "detail": "table inferred from the column index"})
            score -= 0.15

        dax = fabric.get("dax_expression") or ""
        if dimension.get("is_calculated"):
            balanced = dax.count("(") == dax.count(")")
            checks.append({"id": "dax_balanced", "status": "pass" if balanced else "fail"})
            if not balanced:
                score -= 0.3
            unqualified = bool(dax) and "[" not in dax
            checks.append({
                "id": "dax_columns_qualified",
                "status": "fail" if unqualified else "pass",
            })
            if unqualified:
                score -= 0.25
            checks.append({"id": "dim_calculated_review", "status": "warn",
                           "detail": "calculated dimension becomes a DAX column"})
            score -= 0.10
        else:
            checks.append({"id": "dim_direct_mapping", "status": "pass"})

        score = round(max(0.0, min(1.0, score)), 2)
        score_100 = int(round(score * 100))
        band = "high" if score >= 0.85 else ("medium" if score >= 0.70 else "low")
        return {
            "score": score,
            "score_out_of_100": score_100,
            "percentage": f"{score_100}%",
            "band": band,
            "requires_review": score < 0.70 or bool(dimension.get("is_calculated")),
            "rationale": (
                "Drill-down group mapped to a Power BI hierarchy."
                if dimension.get("is_drilldown")
                else "Calculated dimension mapped to a DAX calculated column; verify the expression."
                if dimension.get("is_calculated")
                else "Plain field dimension maps directly to a model column."
            ),
        }

    def map_dimensions(
        self, dimensions: List[Dict[str, Any]], tables: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        self.index_columns(tables or [])
        return [self.map_dimension(d) for d in dimensions if isinstance(d, dict)]
