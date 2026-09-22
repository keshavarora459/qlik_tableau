"""Column-identifier utilities shared by DAXConverter and ConfidenceEvaluator.

Split out of dax_converter.py so confidence_evaluator.py can reuse the same
column index / identifier logic without importing DAXConverter, which itself
imports ConfidenceEvaluator (that import would be circular).
"""

import re
from typing import Any, Dict, List

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


def build_column_index(known_tables: List[Dict[str, Any]]) -> Dict[str, str]:
    """column name -> owning table, first table wins."""
    index: Dict[str, str] = {}
    for table in known_tables or []:
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
            if name and name not in index:
                index[name] = table_name
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
        if token in index:
            result["known_unqualified"].append(token)
        else:
            result["unresolved"].append(token)
    return result
