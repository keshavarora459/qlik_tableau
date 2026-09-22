import re
from typing import Any, Dict, List, Optional, Tuple

from services.dax_identifiers import build_column_index
from services.input_normalizer import normalize_filters


class FilterMapper:
    def __init__(self):
        self.column_index: Dict[str, str] = {}
        self.known_tables: List[Dict[str, Any]] = []

    def index_columns(self, tables: List[Dict[str, Any]]) -> None:
        self.known_tables = tables or []
        self.column_index = build_column_index(tables)

    def _resolve(self, field: Optional[str]) -> Optional[str]:
        if not field:
            return None
        clean = field.strip("[] ")
        return self.column_index.get(clean) or self.column_index.get(field)

    def _resolve_expression_filter(self, expr: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Resolve calculated filter expressions like =origin_city & ' -> ' & destination_city."""
        clean_expr = expr.lstrip("=").strip()
        tokens = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", clean_expr)
        ref_cols = [t for t in tokens if t in self.column_index or t.lower() in [k.lower() for k in self.column_index]]

        if not ref_cols:
            return None, None, None

        # Resolve primary table from referenced columns
        table_name = None
        for col in ref_cols:
            matched_t = self._resolve(col)
            if matched_t:
                table_name = matched_t
                break

        if not table_name and self.known_tables:
            table_name = self.known_tables[0].get("name") or "Table"

        # Generate a clean calculated column name
        col_name = re.sub(r"[^a-zA-Z0-9_]", "_", clean_expr[:30]).strip("_") or "CalculatedFilter"
        dax_expr = clean_expr
        for col in ref_cols:
            t = self._resolve(col) or table_name
            dax_expr = re.sub(rf"\b{col}\b", f"'{t}'[{col}]", dax_expr)

        return table_name, col_name, dax_expr

    def map_filter(self, filt: Dict[str, Any]) -> Dict[str, Any]:
        field = filt.get("field") or filt.get("expression") or filt.get("qDef")
        table = self._resolve(field)
        target_column = field
        target_dax = None
        is_calculated = False

        if not table and field and (str(field).startswith("=") or any(op in str(field) for op in ["&", "+", "-", "(", ")"])):
            calc_t, calc_c, calc_dax = self._resolve_expression_filter(str(field))
            if calc_t and calc_c:
                table = calc_t
                target_column = calc_c
                target_dax = calc_dax
                is_calculated = True

        resolved = bool(table and target_column)
        scope = "page" if filt.get("sheet_name") else "report"

        replacement_strategy = None if resolved else (
            f"Field '{field}' did not match any column in the mapped tables. Confirm the field name "
            "against the source schema, then add it as a slicer bound to the correct table.column manually."
        )

        fabric = {
            "filter_scope": scope,
            "target_table": table,
            "target_column": target_column if resolved else None,
            "target_dax": target_dax,
            "is_calculated": is_calculated,
            "sheet_name": filt.get("sheet_name"),
            "powerbi_filter_type": "categorical",
            "replacement_strategy": replacement_strategy,
        }

        checks = [{"id": "filter_field_resolved", "status": "pass" if resolved else "fail"}]
        score = 0.95 if resolved else 0.40
        rationale = (
            f"Filter '{field}' resolved to '{table}'.'{target_column}'."
            if resolved else
            f"Filter '{field}' could not be resolved against any known table column; left unbound."
        )

        return {
            "name": filt.get("name") or field or "Filter",
            "qlik_source": filt,
            "fabric": fabric,
            "confidence": {
                "score": score,
                "score_out_of_100": int(round(score * 100)),
                "percentage": f"{int(round(score * 100))}%",
                "band": "high" if score >= 0.85 else ("medium" if score >= 0.60 else "low"),
                "llm_score": score,
                "requires_review": not resolved,
                "rationale": rationale,
            },
        }

    def map_filters(self, raw_filters: List[Dict[str, Any]], tables: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        self.index_columns(tables or [])
        return [self.map_filter(f) for f in normalize_filters(raw_filters)]
