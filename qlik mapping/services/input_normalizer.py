"""Normalize parsing-agent input into the flat shape the converters expect.

The parsing agent emits measures and dimensions in Qlik's own envelope:

    {"qInfo": {...}, "qMeasure": {"qLabel": "Total Revenue",
                                  "qDef": "Sum(revenue)/1000000"}, ...}

while DAXConverter reads `name` / `expression` from the top level. The old
guard in MappingAgent.extract_measures required a top-level `name`, so every
raw Qlik measure was silently discarded — 29 in, 0 out, no error and a
conversion_summary that still reported zero failures.

Both shapes now normalize here, so the agent works whether it is fed the raw
engine envelope or an already-flattened payload.
"""

from typing import Any, Dict, List


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


import re
import hashlib

def _sanitize_name(name: Any, fallback: str = "Item") -> str:
    """Ensure name is valid for Fabric/Power BI (length < 100, no illegal chars)."""
    name_str = str(name).strip() if name else ""
    if not name_str:
        return fallback
    
    # Remove newlines, brackets, and characters illegal in Analysis Services object names
    clean = re.sub(r'[\r\n\t]', ' ', name_str)
    # Power BI doesn't strictly ban all these, but keeping it alphanumeric+spaces prevents expression-name issues
    clean = re.sub(r'[\\/\[\]|{}"<>]', '', clean).strip()
    
    if not clean:
        clean = fallback
        
    if len(clean) > 80:
        h = hashlib.md5(name_str.encode('utf-8')).hexdigest()[:6]
        clean = clean[:70].strip() + "_" + h
        
    return clean


def normalize_measure(raw: Any) -> Dict[str, Any]:
    """Flatten one measure to {name, expression, qlik_number_format, tables}."""
    raw = _as_dict(raw)
    qmeasure = _as_dict(raw.get("qMeasure"))
    qmeta = _as_dict(raw.get("qMetaDef"))
    qinfo = _as_dict(raw.get("qInfo"))

    raw_name = _first(
        raw.get("name"), raw.get("qlik_name"),
        qmeasure.get("qLabel"), qmeta.get("title"), qinfo.get("qId"),
    )
    expression = _first(
        raw.get("expression"), raw.get("qlik_expression"), qmeasure.get("qDef")
    )
    if not raw_name and not expression:
        return {}

    name = _sanitize_name(raw_name or expression, "Measure")

    return {
        "name": name,
        "expression": expression or "",
        "qlik_number_format": _first(
            raw.get("qlik_number_format"), raw.get("number_format"),
            qmeasure.get("qNumFormat"),
        ) or {},
        "tables": _as_list(raw.get("tables")),
        "description": qmeta.get("description"),
        "measure_id": qinfo.get("qId"),
    }


def normalize_measures(raw: Any) -> List[Dict[str, Any]]:
    """Accept a list, or a {"measures": [...]} wrapper, and flatten each item."""
    if isinstance(raw, dict):
        raw = raw.get("measures", [])
    return [m for m in (normalize_measure(item) for item in _as_list(raw)) if m]


def normalize_dimension(raw: Any) -> Dict[str, Any]:
    """Flatten one dimension, keeping calculated/drill-down information."""
    raw = _as_dict(raw)
    qdim = _as_dict(raw.get("qDim"))
    qmeta = _as_dict(raw.get("qMetaDef"))
    qinfo = _as_dict(raw.get("qInfo"))

    field_defs = _as_list(_first(raw.get("field_defs"), qdim.get("qFieldDefs")))
    raw_name = _first(
        raw.get("name"), qdim.get("title"), qmeta.get("title"), qinfo.get("qId")
    )

    # A calculated dimension's definition is an expression, not a field name.
    # Preserve pre-flattened expressions if the parsing result already carries them.
    expression = str(_first(raw.get("qlik_expression"), raw.get("expression"), field_defs[0] if field_defs else ""))

    if not raw_name and not expression and not field_defs:
        return {}
        
    name = _sanitize_name(raw_name or expression, "Dimension")

    is_calculated = bool(raw.get("is_calculated")) or expression.startswith("=")
    grouping = str(_first(raw.get("grouping"), qdim.get("qGrouping")) or "N")

    data_type = str(_first(raw.get("qlik_datatype"), raw.get("dataType")) or "STRING").upper()
    if is_calculated and "CALCULATED" not in data_type:
        data_type = f"{data_type} (CALCULATED)"

    out_expression = expression[1:] if expression.startswith("=") else expression

    # Validation: ensure a calculated dimension with a source expression doesn't lose it
    if raw.get("qlik_expression") and not out_expression.strip():
        raise ValueError(f"Calculated dimension '{name}' with source expression silently produced an empty qlik_expression.")

    return {
        "name": name,
        # The target contract strips the leading "=" from the expression.
        "qlik_expression": out_expression,
        "qlik_datatype": data_type,
        "nature": raw.get("nature"),
        "tables": _as_list(raw.get("tables")),
        "field_defs": field_defs,
        "is_calculated": is_calculated,
        "is_drilldown": bool(raw.get("is_drilldown")) or grouping.upper() == "H",
        "grouping": grouping,
        "cardinality": raw.get("cardinality"),
        "dimension_id": qinfo.get("qId"),
    }


def normalize_dimensions(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("dimensions", [])
    return [d for d in (normalize_dimension(item) for item in _as_list(raw)) if d]


def normalize_filter(raw: Any) -> Dict[str, Any]:
    """Flatten one filter pane / list box to {name, field, sheet_name}.

    Accepts the flattened shape (`field`/`field_name`/`column`) as well as
    the Qlik engine's native list-object envelope
    (`qListObjectDef.qDef.qFieldDefs`), mirroring normalize_measure/
    normalize_dimension above.
    """
    raw = _as_dict(raw)
    qlistobj = _as_dict(raw.get("qListObjectDef"))
    qdef = _as_dict(qlistobj.get("qDef"))
    qmeta = _as_dict(raw.get("qMetaDef"))

    field_defs = _as_list(_first(raw.get("field_defs"), qdef.get("qFieldDefs")))
    field = _first(
        raw.get("field"), raw.get("field_name"), raw.get("qlik_field"),
        raw.get("column"), raw.get("expression"), raw.get("expr"),
        raw.get("qDef") if isinstance(raw.get("qDef"), str) else None,
        field_defs[0] if field_defs else None,
    )
    raw_name = _first(raw.get("title"), raw.get("name"), qmeta.get("title"), raw.get("label"), field)
    if not field and not raw_name:
        return {}

    name = _sanitize_name(raw_name, "Filter")

    field_str = str(field) if field is not None else None
    return {
        "name": name,
        "field": field_str.lstrip("=") if field_str and not field_str.startswith("=") else field_str,
        "sheet_name": _first(raw.get("sheet_name"), raw.get("source")),
    }


def normalize_filters(raw: Any) -> List[Dict[str, Any]]:
    """Accept a list, or a {"filters": [...]}/{"filter_panes": [...]} wrapper."""
    if isinstance(raw, dict):
        raw = raw.get("filters") or raw.get("filter_panes") or []
    return [f for f in (normalize_filter(item) for item in _as_list(raw)) if f]


def resolve_app_metadata(data: Dict[str, Any]) -> Dict[str, Any]:
    """Pick the richest metadata available.

    The parsing agent's top-level `metadata` is projected down to five keys for
    example.json fidelity; the full engine metadata (space, owner, reload time,
    section access, ...) survives under the enrichment block or as
    `app_metadata`, so prefer whichever carries more.
    """
    candidates = [
        _as_dict(data.get("app_metadata")),
        _as_dict(_as_dict(data.get("enrichment")).get("app_metadata")),
        _as_dict(_as_dict(data.get("enrichment")).get("metadata")),
        _as_dict(data.get("metadata")),
    ]
    return max(candidates, key=len) if any(candidates) else {}
