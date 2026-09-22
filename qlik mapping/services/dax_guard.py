"""Validate a DAX expression before it is allowed to replace the baseline.

Every LLM-assisted conversion in this service keeps the deterministic
DAXConverter result as a floor. The model's answer is only accepted when it
passes `validate()` here, so enabling the LLM can improve output but can
never drop below what the regex path already produced.

The checks are deliberately conservative: they look for things that are
*definitely* wrong (unbalanced parentheses, invented placeholder tables,
`MATCH`, a `VAR` with no `RETURN`, references to tables that do not exist in
the model) rather than trying to judge semantics. A wrong-but-valid
expression is a review problem; an invalid one breaks the whole semantic
model at import time.
"""

import re
from typing import Any, Dict, List, Tuple

from services.dax_identifiers import (
    DAX_FUNCTIONS_AND_KEYWORDS,
    QUALIFIED_REF,
    build_column_index,
    find_bare_columns,
)

# A bare measure reference: [Measure Name], with no table qualifier.
MEASURE_REF = re.compile(r"\[[^\]\[]*\]")

# Table references in a DAX expression: 'TableName'[Column] or 'TableName' as
# the first argument of an iterator.
TABLE_REF = re.compile(r"'([^']+)'")
CODE_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")

# Names the model reaches for when it has not grounded itself in the schema.
PLACEHOLDER_TABLES = {
    "table", "tablename", "fact_table", "facttable", "unknowntable",
    "unknown_table", "yourtable", "mytable", "sourcetable", "data",
}

# Functions that do not exist in DAX but are commonly hallucinated from
# Excel/Qlik/SQL habits.
FORBIDDEN_FUNCTIONS = {
    "match", "vlookup", "iferror", "ifnull", "nz", "isnull", "nullif",
    "concat_ws", "substr", "instr", "len_", "strpos", "to_char", "cast",
}

# DAX reserved words that must never be used as a VAR name.
RESERVED_VAR_NAMES = {
    "value", "table", "column", "row", "filter", "all", "rank", "order",
    "index", "format", "date", "true", "false", "blank", "return", "define",
    "measure", "switch", "calculate", "sum", "average", "min", "max", "count",
    "union", "intersect", "except", "topn", "offset", "window", "partition",
    "dense", "skip", "left", "right", "mid", "find", "search", "replace",
    "trim", "upper", "lower", "year", "month", "day", "hour", "minute",
    "second", "not", "and", "or", "in",
}

VAR_DECL = re.compile(r"\bVAR\s+([A-Za-z_]\w*)", re.IGNORECASE)


def strip_model_formatting(text: str) -> str:
    """Remove markdown fences and a stray leading `=` from a model answer.

    Asking for "no markdown" works most of the time; this makes the remaining
    cases harmless instead of rejecting an otherwise-correct expression.
    """
    if not text:
        return ""
    cleaned = text.strip()
    if "```" in cleaned:
        # Keep the largest fenced block if the model wrapped its answer.
        blocks = re.findall(r"```[a-zA-Z]*\s*(.*?)```", cleaned, flags=re.DOTALL)
        if blocks:
            cleaned = max(blocks, key=len)
        else:
            cleaned = CODE_FENCE.sub("", cleaned)
    cleaned = cleaned.strip()
    if cleaned.startswith("="):
        cleaned = cleaned[1:].strip()
    return cleaned


def _balanced(expr: str) -> bool:
    """Parentheses and brackets balanced, ignoring quoted spans."""
    depth_paren = 0
    depth_bracket = 0
    in_single = False
    in_double = False
    for char in expr:
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if in_single or in_double:
            continue
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren -= 1
            if depth_paren < 0:
                return False
        elif char == "[":
            depth_bracket += 1
        elif char == "]":
            depth_bracket -= 1
            if depth_bracket < 0:
                return False
    return depth_paren == 0 and depth_bracket == 0 and not in_single and not in_double


def _has_nested_brackets(expr: str) -> bool:
    """True when a `[` opens while another is still open, outside quotes.

    DAX never nests square brackets: a reference is either `[Measure]` or
    `'Table'[Column]`, both depth 1. Depth 2 means a column qualifier was
    written *inside* a measure reference, e.g.

        [Total Cost]  ->  [Total 'Sales'[Cost]]

    which is exactly what DAXConverter.qualify_columns produces for a Qlik
    expression that references another measure by its label (per Qlik's
    docs, a measure name inside an expression is an alias for that measure).
    The result parses as balanced but is not valid DAX, so a bracket-count
    check alone lets it through.
    """
    depth = 0
    in_single = False
    in_double = False
    for char in expr:
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if in_single or in_double:
            continue
        if char == "[":
            depth += 1
            if depth > 1:
                return True
        elif char == "]":
            depth = max(0, depth - 1)
    return False


def validate(
    expr: str,
    tables: List[Dict[str, Any]],
    is_measure: bool = True,
) -> Tuple[bool, List[str]]:
    """Return (ok, problems) for a candidate DAX expression."""
    problems: List[str] = []

    if not expr or not expr.strip():
        return False, ["expression is empty"]

    candidate = expr.strip()

    if len(candidate) > 8000:
        problems.append("expression is implausibly long (>8000 chars)")

    if not _balanced(candidate):
        problems.append("unbalanced parentheses, brackets or quotes")

    if _has_nested_brackets(candidate):
        problems.append(
            "nested square brackets - a column qualifier was written inside a "
            "measure reference (e.g. [Total 'Sales'[Cost]])"
        )

    if "$(" in candidate:
        # Qlik dollar-sign expansion is textual substitution performed before
        # the expression is parsed; it has no DAX equivalent and must be
        # resolved during mapping, not emitted.
        problems.append(
            "contains an unexpanded Qlik dollar-sign expansion $(...) - resolve "
            "the variable to a literal or a measure reference first"
        )

    lowered = candidate.lower()

    # Prose leaking into the answer.
    for marker in ("here is", "here's", "the dax", "explanation:", "note:", "```"):
        if marker in lowered:
            problems.append("contains prose or markdown rather than a bare expression")
            break

    for func in FORBIDDEN_FUNCTIONS:
        if re.search(r"\b" + re.escape(func) + r"\s*\(", lowered):
            problems.append("uses '{}', which is not a DAX function".format(func.upper()))

    # VAR without RETURN is a hard parse error in Power BI.
    if re.search(r"\bVAR\b", candidate, re.IGNORECASE) and not re.search(
        r"\bRETURN\b", candidate, re.IGNORECASE
    ):
        problems.append("declares VAR but never uses RETURN")

    for var_name in VAR_DECL.findall(candidate):
        if var_name.lower() in RESERVED_VAR_NAMES:
            problems.append("uses reserved word '{}' as a VAR name".format(var_name))

    # Table references must exist in the model.
    known_tables = {
        str(t.get("name") or t.get("table_name") or "").strip()
        for t in (tables or [])
        if isinstance(t, dict)
    }
    known_lower = {t.lower() for t in known_tables if t}

    referenced = set()
    for match in TABLE_REF.finditer(candidate):
        # Only treat it as a table reference when followed by [ or used as an
        # iterator argument; a bare quoted span may be a string literal.
        end = match.end()
        tail = candidate[end : end + 1]
        if tail == "[" or re.match(r"\s*[,)]", candidate[end : end + 3] or ""):
            referenced.add(match.group(1))

    for table in referenced:
        if table.lower() in PLACEHOLDER_TABLES and table.lower() not in known_lower:
            problems.append("references placeholder table '{}'".format(table))
        elif known_lower and table.lower() not in known_lower:
            problems.append("references unknown table '{}'".format(table))

    # A measure made of a naked column reference will not evaluate.
    if is_measure:
        index = build_column_index(tables or [])
        # Strip qualified refs first, then whatever square-bracket spans
        # remain are measure references like [Total Cost]. Without removing
        # them, a word inside a measure name that happens to match a column
        # ("Cost" in "[Total Cost]") is misreported as an unqualified column
        # and a perfectly valid expression gets rejected.
        scannable = QUALIFIED_REF.sub(" ", candidate)
        scannable = MEASURE_REF.sub(" ", scannable)
        bare = find_bare_columns(scannable, index)
        if bare["known_unqualified"]:
            problems.append(
                "leaves known column(s) unqualified: "
                + ", ".join(sorted(set(bare["known_unqualified"]))[:5])
            )

    return (not problems), problems


def choose(
    baseline: str,
    candidate: str,
    tables: List[Dict[str, Any]],
    is_measure: bool = True,
) -> Tuple[str, bool, List[str]]:
    """Pick between the deterministic baseline and the model's answer.

    Returns (chosen_expression, used_llm, problems).

    The baseline wins on any doubt. The candidate must both validate *and*
    differ from the baseline to be considered an improvement worth recording.
    """
    cleaned = strip_model_formatting(candidate)
    if not cleaned:
        return baseline, False, ["model returned nothing usable"]

    ok, problems = validate(cleaned, tables, is_measure=is_measure)
    if not ok:
        return baseline, False, problems

    return cleaned, True, []
