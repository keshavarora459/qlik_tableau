"""Column-identifier utilities shared by DAXConverter and ConfidenceEvaluator.

Split out of dax_converter.py so confidence_evaluator.py can reuse the same
column index / identifier logic without importing DAXConverter, which itself
imports ConfidenceEvaluator (that import would be circular).
"""

import re
from typing import Any, Dict, List, Optional

# A bare identifier: not preceded by [ or ' and not a function call.
IDENTIFIER = re.compile(r"(?<![\[\w'])([A-Za-z_]\w*)\b(?!\s*\()")
SINGLE_QUOTED = re.compile(r"'([^']*)'")
DOUBLE_QUOTED = re.compile(r'"[^"]*"')
QUALIFIED_REF = re.compile(r"'[^']+'\[[^\]]+\]")

# Words that are DAX/Qlik syntax rather than column names.
RESERVED = {
    "sum", "avg", "average", "count", "min", "max", "distinct", "total",
    "aggr", "if", "then", "else", "and", "or", "not", "null", "true", "false",
    "divide", "calculate", "filter", "blank", "num", "only", "applymap",
    "date", "year", "month", "day", "now", "today",
}

# DAX built-in function/keyword names that legitimately appear as bare,
# unqualified tokens in a converted expression (they are never columns).
DAX_FUNCTIONS_AND_KEYWORDS = RESERVED | {
    "sumx", "averagex", "countx", "countrows", "minx", "maxx",
    "distinctcount", "calculatetable", "all", "allexcept", "allselected",
    "removefilters", "related", "relatedtable", "values", "selectedvalue",
    "selectcolumns", "summarize", "switch", "coalesce", "isblank",
    "iserror", "var", "return", "in", "asc", "desc", "trim", "substitute",
    "value", "format", "concatenate", "left", "right", "len", "upper",
    "lower", "round", "roundup", "rounddown", "abs", "rankx", "topn",
    "earlier", "totalytd", "totalqtd", "totalmtd", "sameperiodlastyear",
    "dateadd", "datediff", "keepfilters", "userelationship", "hasonevalue",
    "isfiltered", "isempty",
}


def build_column_index(
    known_tables: List[Dict[str, Any]],
    preferred_tables: Optional[List[str]] = None,
) -> Dict[str, str]:
    """column name -> owning table, first table wins.

    If preferred_tables is provided, tables in preferred_tables take
    precedence so their columns are indexed before other tables.
    """
    index: Dict[str, str] = {}
    if not known_tables:
        return index

    pref_list = [str(p).strip().lower() for p in (preferred_tables or []) if p and str(p).strip()]

    def sort_key(t: Dict[str, Any]) -> int:
        if not isinstance(t, dict):
            return 999999
        tname = str(t.get("name") or t.get("table_name") or "").strip().lower()
        if tname in pref_list:
            return pref_list.index(tname)
        return len(pref_list) + 1

    tables_to_index = sorted(known_tables, key=sort_key) if pref_list else known_tables
    for table in tables_to_index:
        if not isinstance(table, dict):
            continue
        table_name = table.get("name") or table.get("table_name")
        if not table_name:
            continue
        for column in table.get("columns") or table.get("fields") or []:
            if not isinstance(column, dict):
                continue
            name = (
                column.get("qlik_column_name")
                or column.get("fabric_column_name")
                or column.get("column_name")
                or column.get("name")
                or column.get("Name")
            )
            if name and name.lower() not in index:
                index[name.lower()] = table_name
    return index


def find_bare_columns(expr: str, index: Dict[str, str]) -> Dict[str, List[str]]:
    """Scan a converted DAX expression for identifiers that were never
    qualified as 'Table'[Column].

    Returns {"known_unqualified": [...], "unresolved": [...]}:
    - known_unqualified: a real column (present in `index`) left bare -
      qualify_columns should have wrapped it and didn't.
    - unresolved: a bare identifier that isn't a DAX keyword/function and
      isn't a known column either - likely a reference to a field that was
      never added to the table model.
    """
    result = {"known_unqualified": [], "unresolved": []}
    if not expr:
        return result

    working = QUALIFIED_REF.sub("", expr)
    working = DOUBLE_QUOTED.sub("", working)
    working = SINGLE_QUOTED.sub("", working)

    for match in IDENTIFIER.finditer(working):
        token = match.group(1)
        if token.lower() in DAX_FUNCTIONS_AND_KEYWORDS or token.isdigit():
            continue
        if token.lower() in index:
            result["known_unqualified"].append(token)
        else:
            result["unresolved"].append(token)
    return result
