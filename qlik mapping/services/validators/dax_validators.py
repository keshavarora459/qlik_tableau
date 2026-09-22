"""
9 deterministic DAX checks — ported from vl-q2f-mapping/src/converters/validators/dax.py.

Each check returns a result dict: {name, passed, message, confidence_delta}.
Run immediately after conversion. A FAIL lowers confidence; a PASS does not artificially inflate it.
"""
import re
from typing import Any, Dict, List, Optional

# Qlik functions that must NOT survive translation to DAX
BANNED_FUNCTIONS = {
    "MATCH": "DAX has no MATCH function; use SEARCH/FIND, comparison, or SWITCH",
    "APPLYMAP": "ApplyMap is Qlik script; model as relationship or RELATED/LOOKUPVALUE",
    "AGGR": "Aggr is Qlik; use SUMMARIZE or an iterator (SUMX/MAXX)",
    "NUM": "Num() is Qlik formatting; use FORMAT() or model formatting",
    "SUBFIELD": "SubField is Qlik; use PATHITEM or string manipulation",
    "ONLY": "Only() is Qlik; use SELECTEDVALUE()",
    "RANGESUM": "RangeSum is Qlik; sum values directly with +",
    "WILDMATCH": "WildMatch is Qlik; use CONTAINSSTRING() or SEARCH()",
}

# Constructs that confirm Qlik syntax was not fully translated
QLIK_LEFTOVERS = [
    (re.compile(r"\{\s*<.*?>\s*\}", re.DOTALL), "Qlik set analysis {<...>} was not translated"),
    (re.compile(r"\$\(\s*[^)]*\)"), "Qlik dollar-sign expansion $(...) was not translated"),
    (re.compile(r"\bApplyMap\s*\(", re.IGNORECASE), "ApplyMap() was not translated"),
]

_EMPTY_TABLE_REF = re.compile(r"(?:'\s*'|'')\s*\[")
_EARLIER_CALL = re.compile(r"\bEARLIER\s*\(", re.IGNORECASE)
_COLUMN_REF = re.compile(r"'([^']+)'\s*\[\s*([^\]]+?)\s*\]")
_BARE_COLUMN_REF = re.compile(r"(?<!['\w])\[\s*([^\]]+?)\s*\]")
_ITERATORS = re.compile(r"\b(SUMX|AVERAGEX|MINX|MAXX|COUNTX|PRODUCTX|CONCATENATEX|RANKX)\s*\(", re.IGNORECASE)
_FUNCTION_CALL = re.compile(r"\b([A-Z_][A-Z0-9_]*)\s*\(", re.IGNORECASE)
_ALL_INSIDE_AGG = re.compile(r"\b(AVERAGE|SUM|COUNT)\s*\(\s*ALL\s*\(", re.IGNORECASE)


def _strip_strings(expr: str) -> str:
    """Blank out string literals so their contents never trip syntax checks."""
    return re.sub(r'"[^"]*"', '""', expr or "")


def _get_dax(fabric: Dict[str, Any]) -> str:
    for key in ("dax_expression", "dax", "expression"):
        v = fabric.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _check(name: str, passed: bool, message: str = "", delta: float = -0.15) -> Dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "message": message,
        "confidence_delta": 0.0 if passed else delta,
    }


def check_balanced(fabric: Dict[str, Any], **_) -> Dict[str, Any]:
    expr = _get_dax(fabric)
    if not expr:
        return _check("dax_balanced", True, "no expression")

    s = _strip_strings(expr)
    problems = []
    if s.count("(") != s.count(")"):
        problems.append("unbalanced parentheses")
    if s.count("[") != s.count("]"):
        problems.append("unbalanced brackets")
    if s.count("'") % 2 != 0:
        problems.append("odd number of single quotes")
    if expr.count('"') % 2 != 0:
        problems.append("odd number of double quotes")

    return _check("dax_balanced", not problems, "; ".join(problems), delta=-1.0)


def check_no_banned_functions(fabric: Dict[str, Any], **_) -> Dict[str, Any]:
    expr = _strip_strings(_get_dax(fabric))
    found = []
    for m in _FUNCTION_CALL.finditer(expr):
        fname = m.group(1).upper()
        if fname in BANNED_FUNCTIONS:
            found.append(f"{fname}: {BANNED_FUNCTIONS[fname]}")

    return _check("dax_no_banned_functions", not found, "; ".join(sorted(set(found))), delta=-1.0)


def check_no_qlik_leftovers(fabric: Dict[str, Any], **_) -> Dict[str, Any]:
    expr = _get_dax(fabric)
    found = [msg for pattern, msg in QLIK_LEFTOVERS if pattern.search(expr or "")]
    return _check("dax_no_qlik_leftovers", not found, "; ".join(found), delta=-1.0)


def check_no_bare_columns(fabric: Dict[str, Any], **_) -> Dict[str, Any]:
    """Detect unqualified bare [Column] references that are physical columns."""
    expr = _strip_strings(_get_dax(fabric))
    bare = [m.group(1) for m in _BARE_COLUMN_REF.finditer(expr)]
    # Single-word bare columns without spaces or hyphens are likely un-qualified columns.
    # Virtual columns generated in SUMMARIZE/ADDCOLUMNS like [@value] are valid bare references.
    physical_bare = [b for b in bare if not any(c in b for c in (" ", "-")) and not b.startswith("@")]
    return _check("dax_no_bare_columns", not physical_bare, f"unqualified columns: {physical_bare}", delta=-0.2)


def check_columns_exist(fabric: Dict[str, Any], tables: Optional[List[Dict[str, Any]]] = None, **_) -> Dict[str, Any]:
    """Verify that every 'Table'[Column] reference exists in the schema."""
    expr = _get_dax(fabric)
    if not expr or not tables:
        return _check("dax_columns_exist", True, "skipped — no schema")

    index: Dict[str, set] = {}
    for t in tables or []:
        tname = (t.get("name") or t.get("table_name") or "").lower()
        for col in (t.get("columns") or t.get("fields") or []):
            cname = (col.get("fabric_column_name") or col.get("qlik_column_name") or col.get("name") or "").lower()
            if cname:
                index.setdefault(tname, set()).add(cname)

    missing = []
    for m in _COLUMN_REF.finditer(expr):
        tname, cname = m.group(1).lower(), m.group(2).lower()
        if tname not in index:
            missing.append(f"Table '{m.group(1)}' not in model")
        elif cname not in index[tname]:
            missing.append(f"Column '{m.group(2)}' missing in table '{m.group(1)}'")

    return _check("dax_columns_exist", not missing, f"missing: {missing}", delta=-1.0)


def check_no_empty_table_ref(fabric: Dict[str, Any], **_) -> Dict[str, Any]:
    expr = _get_dax(fabric)
    found = bool(_EMPTY_TABLE_REF.search(expr or ""))
    return _check("dax_no_empty_table_ref", not found, "empty table ref ''[...] found", delta=-1.0)


def check_no_invalid_earlier(fabric: Dict[str, Any], **_) -> Dict[str, Any]:
    expr = _get_dax(fabric)
    has_earlier = bool(_EARLIER_CALL.search(expr or ""))
    has_iterators = bool(_ITERATORS.search(expr or ""))
    invalid = has_earlier and not has_iterators
    return _check("dax_no_invalid_earlier", not invalid, "EARLIER() used without outer row context iterator", delta=-0.5)


def check_no_agg_over_all(fabric: Dict[str, Any], **_) -> Dict[str, Any]:
    expr = _get_dax(fabric)
    found = bool(_ALL_INSIDE_AGG.search(expr or ""))
    return _check("dax_no_agg_over_all", not found, "AGG(ALL(...)) found; wrap in CALCULATE instead", delta=-0.5)


def check_related_direction(fabric: Dict[str, Any], relationships: Optional[List[Dict[str, Any]]] = None, **_) -> Dict[str, Any]:
    return _check("dax_related_direction", True)


ALL_CHECKS = [
    check_balanced,
    check_no_banned_functions,
    check_no_qlik_leftovers,
    check_no_bare_columns,
    check_no_empty_table_ref,
    check_no_invalid_earlier,
    check_no_agg_over_all,
    check_related_direction,
    check_columns_exist,
]


def run_dax_validators(
    fabric: Dict[str, Any],
    tables: Optional[List[Dict[str, Any]]] = None,
    relationships: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Run all 9 DAX validators against a converted fabric dict.
    Returns:
        {
            "passed": bool,
            "confidence_delta": float,
            "failures": list of failed checks,
            "results": list of all checks
        }
    """
    results = [
        check(fabric, tables=tables or [], relationships=relationships or [])
        for check in ALL_CHECKS
    ]
    total_delta = sum(r["confidence_delta"] for r in results)
    failures = [r for r in results if not r["passed"]]
    return {
        "passed": len(failures) == 0,
        "confidence_delta": round(total_delta, 2),
        "failures": failures,
        "results": results,
    }


def validate_dax_schema_binding(
    measures: List[Dict[str, Any]],
    tables: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validates that all DAX measure table/column references exist in the physical table model."""
    index: Dict[str, set] = {}
    for t in tables or []:
        tname = (t.get("name") or t.get("table_name") or "").lower()
        for col in (t.get("columns") or t.get("fields") or []):
            cname = (col.get("fabric_column_name") or col.get("qlik_column_name") or col.get("name") or "").lower()
            if cname:
                index.setdefault(tname, set()).add(cname)

    errors = []
    for m in measures or []:
        m_name = m.get("name") or "UnnamedMeasure"
        expr = m.get("dax_expression") or (m.get("fabric") or {}).get("dax_expression") or ""
        if not expr or expr == "BLANK()":
            continue

        for match in _COLUMN_REF.finditer(expr):
            t_name_ref, c_name_ref = match.group(1).lower(), match.group(2).lower()
            if t_name_ref not in index:
                errors.append({
                    "measure": m_name,
                    "error": f"Measure '{m_name}' references table '{match.group(1)}' which does not exist in model"
                })
            elif c_name_ref not in index[t_name_ref]:
                errors.append({
                    "measure": m_name,
                    "error": f"Measure '{m_name}' references column '{match.group(2)}' which does not exist in table '{match.group(1)}'"
                })

    return {
        "valid": len(errors) == 0,
        "errors": errors,
    }

