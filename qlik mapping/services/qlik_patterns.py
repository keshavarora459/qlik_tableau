"""Translate Qlik constructs (Set Analysis, Aggr, TOTAL, Num, RangeSum) into Power BI DAX.

Deterministic rewrites using AST and relationship-aware context:
    Sum({<Status={'Done'}>} rev)             -> CALCULATE(SUM(rev), Status = "Done")
    Sum({<Year={$(=Max(Year)-1)}>} rev)      -> CALCULATE(SUM(rev), 'Table'[Year] = CALCULATE(MAX('Table'[Year]), ALL('Table')) - 1)
    Max(Aggr(Sum(rev), Cust))                -> MAXX(SUMMARIZE('Sales', Cust, "@value", SUM(rev)), [@value])
    Max(Aggr(Sum(rev), Cust, Prod))          -> MAXX(SUMMARIZE('Sales', Cust, Prod, "@value", SUM(rev)), [@value])
    Sum(TOTAL <Region> Sales)                -> CALCULATE(SUM(Sales), ALLEXCEPT('Sales', 'Sales'[Region]))
    Num(expr, '$#,##0')                      -> expr (format moved to formatString)
    RangeSum(a, b, c)                        -> (a + b + c)
"""

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .set_analysis_ast import split_top_level, translate_set_analysis_ast

# Aggregation names that can host set analysis or TOTAL
AGGREGATIONS = "SUM|COUNT|AVERAGE|AVG|MIN|MAX|DISTINCTCOUNT"

NUM = re.compile(r"\bNum\s*\(", re.IGNORECASE)
RANGESUM = re.compile(r"\bRangeSum\s*\(", re.IGNORECASE)

# Qlik functions with no direct DAX rewrite attempted here
UNTRANSLATABLE = re.compile(r"\b(Above|Below)\s*\(", re.IGNORECASE)

OUTER_TO_ITERATOR = {
    "SUM": "SUMX",
    "COUNT": "COUNTX",
    "AVERAGE": "AVERAGEX",
    "AVG": "AVERAGEX",
    "MIN": "MINX",
    "MAX": "MAXX",
}


def _split_top_level(text: str, separator: str = ",") -> List[str]:
    """Bracket- and quote-aware top level split."""
    return split_top_level(text, separator=separator)


def translate_set_analysis(
    expression: str,
    table_resolver: Optional[Callable[[str], Optional[str]]] = None,
    known_tables: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, bool]:
    """Rewrite set analysis as CALCULATE using SetAnalysisParser AST."""
    return translate_set_analysis_ast(expression, table_resolver, known_tables)


def _resolve_aggr_base_table(
    dimensions: List[str],
    inner_expr: str,
    column_table: Callable[[str], Optional[str]],
    known_tables: Optional[List[Dict[str, Any]]] = None,
    relationships: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    """Find the best base table for SUMMARIZE in Aggr.

    Priority:
    1. Table of measure/columns in inner expression (Fact table).
    2. Table connecting all dimension tables in relationships.
    3. Table of the first dimension.
    """
    # Check inner expression for qualified table: 'Sales'[Amount]
    inner_table_match = re.search(r"'([^']+)'\[", inner_expr)
    if inner_table_match:
        return inner_table_match.group(1)

    # Check inner expression for bare columns: Sum(Amount)
    inner_bare_cols = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", inner_expr)
    for col in inner_bare_cols:
        tbl = column_table(col)
        if tbl:
            return tbl

    # Check dimension tables
    dim_tables = [column_table(d) for d in dimensions if column_table(d)]
    if dim_tables:
        # If all dims in the same table
        if len(set(dim_tables)) == 1:
            return dim_tables[0]

        # Multi-table dimensions: find fact table in relationships that has relationships to dim_tables
        if relationships:
            # Look for table appearing on from_table ("many" side)
            for rel in relationships:
                from_t = rel.get("from_table")
                to_t = rel.get("to_table")
                if from_t and to_t in dim_tables:
                    return from_t

        return dim_tables[0]

    if known_tables:
        return known_tables[0].get("name") or known_tables[0].get("table_name")

    return None


def translate_aggr(
    expression: str,
    column_table: Callable[[str], Optional[str]],
    known_tables: Optional[List[Dict[str, Any]]] = None,
    relationships: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, bool]:
    """Rewrite `OUTER(Aggr(INNER, dims...))` or standalone `Aggr(INNER, dims...)` as an iterator over SUMMARIZE.

    Supports:
    - Aggr(Sum(Sales), Customer)
    - Aggr(Sum(Sales), Customer, Product)
    - Avg(Aggr(Sum(Sales), Customer))
    - Max(Aggr(Avg(Sales), Region, Year))
    - Multi-table dimension resolution
    - Aggr inside If() or complex expressions
    """
    changed = False

    # 1. Pattern: OUTER_AGG(Aggr(inner, dims...))
    outer_aggr_regex = re.compile(
        r"\b(?P<outer>SUM|COUNT|AVERAGE|AVG|MIN|MAX)\s*\(\s*Aggr\s*\((?P<rest>.*)",
        re.IGNORECASE | re.DOTALL,
    )

    while True:
        match = outer_aggr_regex.search(expression)
        if not match:
            break

        # Find matching closing parenthesis for outer agg
        start = match.start()
        outer_name = match.group("outer").upper()
        iterator = OUTER_TO_ITERATOR.get(outer_name, "MAXX")

        # Scan from start of Aggr args to find Aggr's closing paren and outer's closing paren
        aggr_start = expression.find("Aggr", start)
        paren_start = expression.find("(", aggr_start)
        depth = 1
        curr = paren_start + 1
        while curr < len(expression) and depth > 0:
            if expression[curr] == "(":
                depth += 1
            elif expression[curr] == ")":
                depth -= 1
            curr += 1

        if depth != 0:
            break

        aggr_content = expression[paren_start + 1 : curr - 1]
        aggr_end = curr  # right after Aggr's closing ')'

        # Expect outer closing ')'
        outer_end = aggr_end
        while outer_end < len(expression) and expression[outer_end].isspace():
            outer_end += 1
        if outer_end < len(expression) and expression[outer_end] == ")":
            outer_end += 1
        else:
            outer_end = aggr_end

        parts = _split_top_level(aggr_content)
        if len(parts) < 2:
            break

        inner, dimensions = parts[0].strip(), [p.strip() for p in parts[1:] if p.strip()]
        table = _resolve_aggr_base_table(dimensions, inner, column_table, known_tables, relationships)
        if not table:
            # Fallback to dimension 0 table or 'Table'
            table = column_table(dimensions[0]) or "Table"

        grouping = ", ".join(dimensions)
        replacement = f'{iterator}(SUMMARIZE(\'{table}\', {grouping}, "@value", {inner}), [@value])'

        expression = expression[:start] + replacement + expression[outer_end:]
        changed = True

    # 2. Pattern: Standalone Aggr(inner, dims...)
    standalone_aggr_regex = re.compile(r"\bAggr\s*\(", re.IGNORECASE)
    while True:
        match = standalone_aggr_regex.search(expression)
        if not match:
            break

        start = match.start()
        depth = 1
        curr = match.end()
        while curr < len(expression) and depth > 0:
            if expression[curr] == "(":
                depth += 1
            elif expression[curr] == ")":
                depth -= 1
            curr += 1

        if depth != 0:
            break

        aggr_content = expression[match.end() : curr - 1]
        parts = _split_top_level(aggr_content)
        if len(parts) < 2:
            break

        inner, dimensions = parts[0].strip(), [p.strip() for p in parts[1:] if p.strip()]
        table = _resolve_aggr_base_table(dimensions, inner, column_table, known_tables, relationships) or "Table"
        grouping = ", ".join(dimensions)
        replacement = f'SUMMARIZE(\'{table}\', {grouping}, "@value", {inner})'

        expression = expression[:start] + replacement + expression[curr:]
        changed = True

    return expression, changed


def translate_total_modifiers(
    expression: str,
    column_table: Callable[[str], Optional[str]],
    known_tables: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, bool]:
    """Rewrite Qlik TOTAL and TOTAL <Dim1, Dim2> modifiers to DAX CALCULATE + ALLSELECTED / ALLEXCEPT.

    Examples:
    - Count(distinct total Field)            -> CALCULATE(DISTINCTCOUNT(Field), ALLSELECTED())
    - Count(distinct total <Region> Field)   -> CALCULATE(DISTINCTCOUNT(Field), ALLEXCEPT('Table', 'Table'[Region]))
    - Sum(total <Region, Year> Sales)       -> CALCULATE(SUM(Sales), ALLEXCEPT('Table', 'Table'[Region], 'Table'[Year]))
    - Sum(total Sales)                       -> CALCULATE(SUM(Sales), ALLSELECTED())
    """
    changed = False

    # 1. TOTAL <Dim1, Dim2>
    total_dims_regex = re.compile(
        rf"\b(?P<agg>{AGGREGATIONS})\s*\(\s*(?P<distinct>distinct\s+)?total\s*<(?P<dims>[^>]+)>\s*(?P<body>[^)]+)\)",
        re.IGNORECASE,
    )

    def replace_total_dims(match: re.Match) -> str:
        nonlocal changed
        agg = match.group("agg").upper()
        if agg == "AVG":
            agg = "AVERAGE"
        if match.group("distinct") and agg == "COUNT":
            agg = "DISTINCTCOUNT"

        raw_dims = [d.strip() for d in match.group("dims").split(",") if d.strip()]
        body = match.group("body").strip()

        # Find base table from dimensions or body
        dim_table = column_table(raw_dims[0]) if raw_dims else None
        if not dim_table:
            body_tbl = re.search(r"'([^']+)'\[", body)
            dim_table = body_tbl.group(1) if body_tbl else (known_tables[0].get("name") if known_tables else "Table")

        qualified_dims = []
        for d in raw_dims:
            d_clean = d.strip("[]'\"")
            t = column_table(d_clean) or dim_table
            qualified_dims.append(f"'{t}'[{d_clean}]")

        changed = True
        return f"CALCULATE({agg}({body}), ALLEXCEPT('{dim_table}', {', '.join(qualified_dims)}))"

    expression = total_dims_regex.sub(replace_total_dims, expression)

    # 2. Plain TOTAL
    total_plain_regex = re.compile(
        rf"\b(?P<agg>{AGGREGATIONS})\s*\(\s*(?P<distinct>distinct\s+)?total\s+(?P<body>[^)]+)\)",
        re.IGNORECASE,
    )

    def replace_total_plain(match: re.Match) -> str:
        nonlocal changed
        agg = match.group("agg").upper()
        if agg == "AVG":
            agg = "AVERAGE"
        if match.group("distinct") and agg == "COUNT":
            agg = "DISTINCTCOUNT"

        body = match.group("body").strip()
        changed = True
        return f"CALCULATE({agg}({body}), ALLSELECTED())"

    expression = total_plain_regex.sub(replace_total_plain, expression)
    return expression, changed


def translate_rangesum(expression: str) -> Tuple[str, bool]:
    """`RangeSum(a, b, c)` -> `(a + b + c)`."""
    changed = False
    while True:
        match = RANGESUM.search(expression)
        if not match:
            break
        start = match.end()
        depth, end = 1, start
        while end < len(expression) and depth:
            if expression[end] == "(":
                depth += 1
            elif expression[end] == ")":
                depth -= 1
            end += 1
        arguments = _split_top_level(expression[start : end - 1])
        if not arguments:
            break
        expression = (
            expression[: match.start()]
            + "("
            + " + ".join(arguments)
            + ")"
            + expression[end:]
        )
        changed = True
    return expression, changed


def strip_num(expression: str) -> Tuple[str, Optional[str], bool]:
    """`Num(expr, 'fmt')` -> (expr, fmt, changed). Formatting is not DAX."""
    match = NUM.search(expression)
    if not match:
        return expression, None, False

    start = match.end()
    depth, end = 1, start
    while end < len(expression) and depth:
        if expression[end] == "(":
            depth += 1
        elif expression[end] == ")":
            depth -= 1
        end += 1

    arguments = _split_top_level(expression[start : end - 1])
    if not arguments:
        return expression, None, False

    fmt = None
    if len(arguments) > 1 and arguments[-1].strip().startswith(("'", '"')):
        fmt = arguments[-1].strip().strip("'\"")
        arguments = arguments[:-1]

    body = ", ".join(arguments)
    rebuilt = expression[: match.start()] + body + expression[end:]
    return rebuilt.strip(), fmt, True


PICK_REGEX = re.compile(r"\bPick\s*\(", re.IGNORECASE)
APPLYMAP_REGEX = re.compile(r"\bApplyMap\s*\(", re.IGNORECASE)


def translate_pick(expression: str) -> Tuple[str, bool]:
    """Translate Qlik Pick(index, branch1, branch2, ...) into DAX SWITCH or selected branch."""
    changed = False
    while True:
        match = PICK_REGEX.search(expression)
        if not match:
            break
        start = match.end()
        depth, end = 1, start
        while end < len(expression) and depth:
            if expression[end] == "(":
                depth += 1
            elif expression[end] == ")":
                depth -= 1
            end += 1

        args = _split_top_level(expression[start : end - 1])
        if len(args) < 2:
            break

        selector = args[0].strip()
        branches = [b.strip() for b in args[1:]]

        # Case 1: Static numeric index e.g. Pick(1, a, b, c) -> a
        if selector.isdigit():
            idx = int(selector)
            if 1 <= idx <= len(branches):
                replacement = branches[idx - 1]
            else:
                replacement = "BLANK()"
        else:
            # Case 2: Dynamic selector expression -> SWITCH(selector, 1, b1, 2, b2, ..., BLANK())
            switch_cases = []
            for i, branch in enumerate(branches, start=1):
                switch_cases.append(f"{i}, {branch}")
            replacement = f"SWITCH({selector}, {', '.join(switch_cases)}, BLANK())"

        expression = expression[: match.start()] + replacement + expression[end:]
        changed = True

    return expression, changed


def translate_applymap(
    expression: str,
    known_tables: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, bool]:
    """Translate Qlik ApplyMap('MapName', KeyExpr, [DefaultExpr]) into DAX LOOKUPVALUE / COALESCE."""
    changed = False
    while True:
        match = APPLYMAP_REGEX.search(expression)
        if not match:
            break
        start = match.end()
        depth, end = 1, start
        while end < len(expression) and depth:
            if expression[end] == "(":
                depth += 1
            elif expression[end] == ")":
                depth -= 1
            end += 1

        args = _split_top_level(expression[start : end - 1])
        if len(args) < 2:
            break

        map_name = args[0].strip().strip("'\"")
        key_expr = args[1].strip()
        default_expr = args[2].strip() if len(args) > 2 else None

        # Look up mapping table in known_tables if available
        target_col = "Value"
        source_col = "Key"
        if known_tables:
            for tbl in known_tables:
                tname = tbl.get("name") or tbl.get("table_name") or ""
                if tname.lower() == map_name.lower():
                    cols = tbl.get("columns", [])
                    if len(cols) >= 2:
                        source_col = cols[0].get("fabric_column_name") or cols[0].get("qlik_column_name") or "Key"
                        target_col = cols[1].get("fabric_column_name") or cols[1].get("qlik_column_name") or "Value"
                    elif len(cols) == 1:
                        target_col = cols[0].get("fabric_column_name") or cols[0].get("qlik_column_name") or "Value"
                    break

        lookup = f"LOOKUPVALUE('{map_name}'[{target_col}], '{map_name}'[{source_col}], {key_expr})"
        if default_expr:
            replacement = f"COALESCE({lookup}, {default_expr})"
        else:
            replacement = lookup

        expression = expression[: match.start()] + replacement + expression[end:]
        changed = True

    return expression, changed
