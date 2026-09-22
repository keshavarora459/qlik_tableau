"""Qlik Section Access -> Power BI Row-Level Security (RLS) converter.

Translates Section Access authorization rules into Power BI TMDL role specifications
using USERPRINCIPALNAME() filter expressions and produces a normalized Contract 2.0 security block.
"""

from typing import Any, Dict, List, Optional
from .tmdl_generator import build_role


def convert_section_access(
    section_access: Optional[Dict[str, Any]],
    tables: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert a parsed Section Access block into Power BI RLS role dicts.

    Each returned dict has: {name, dimension_field, table_name, dax_filter, tmdl_block}

    Args:
        section_access: parsed section access from parsing payload
        tables: list of table dicts (for resolving field ownership)
    Returns:
        List of role dicts ready to be written as TMDL
    """
    if not section_access:
        return []

    access_field = str(section_access.get("access_field") or "ACCESS").upper()
    ntname_field = str(section_access.get("ntname_field") or "NTNAME").upper()
    user_fields = {access_field, ntname_field, "USERID", "GROUP", "SERIAL", "OMIT", "PASSWORD"}

    # Dimension/reduction fields are columns other than user identity & access level
    raw_fields = section_access.get("fields") or []
    dimension_fields = [
        f for f in raw_fields
        if str(f).upper() not in user_fields
    ]

    # If no fields list is explicitly given, look into rows keys
    rows = section_access.get("rows") or []
    if not dimension_fields and rows:
        sample_row = rows[0]
        if isinstance(sample_row, dict):
            dimension_fields = [
                k for k in sample_row.keys()
                if str(k).upper() not in user_fields
            ]

    # Check for reduction fields explicitly specified
    reduction_fields = section_access.get("reduction_fields") or []
    for rf in reduction_fields:
        if rf and str(rf).upper() not in user_fields and rf not in dimension_fields:
            dimension_fields.append(rf)

    roles = []
    for dim_field in dimension_fields:
        table_name = _resolve_field_table(dim_field, tables)
        if not table_name:
            continue

        # DAX RLS filter pattern: user's identity must match or wildcard '*' allows all
        dax_filter = (
            f"'{table_name}'[{dim_field}] = USERPRINCIPALNAME() || "
            f"'{table_name}'[{dim_field}] = \"*\""
        )

        role_name = f"{dim_field} Security"
        tmdl_block = build_role(
            name=role_name,
            table_permissions=[{"table": table_name, "filter": dax_filter}],
        )

        roles.append({
            "name": role_name,
            "dimension_field": dim_field,
            "table_name": table_name,
            "dax_filter": dax_filter,
            "tmdl_block": tmdl_block,
        })

    return roles


def build_security_contract(
    section_access: Optional[Dict[str, Any]],
    tables: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a normalized Contract 2.0 security block."""
    if not section_access:
        return {
            "type": "none",
            "roles": [],
            "source": "none",
            "review_required": False,
        }

    roles = convert_section_access(section_access, tables)
    unsupported_reasons = []

    # Check for unsupported Section Access constructs
    raw_fields = [str(f).upper() for f in (section_access.get("fields") or [])]
    if "OMIT" in raw_fields:
        unsupported_reasons.append("OMIT (column-level security) in Section Access requires manual review or tabular perspective modeling.")
    if "SERIAL" in raw_fields:
        unsupported_reasons.append("SERIAL hardware binding in Section Access is unsupported in Power BI/Fabric RLS.")
    if not roles and (section_access.get("fields") or section_access.get("rows")):
        unsupported_reasons.append("Could not resolve reduction field ownership in semantic model tables.")

    review_required = bool(unsupported_reasons) or not bool(roles)

    return {
        "type": "rls",
        "roles": roles,
        "source": "qlik_section_access",
        "review_required": review_required,
        "unsupported_reasons": unsupported_reasons,
    }


def _resolve_field_table(field_name: str, tables: List[Dict[str, Any]]) -> Optional[str]:
    """Find the owning table for a given field name."""
    norm = field_name.lower().replace(" ", "").replace("_", "")
    for table in tables:
        for col in (table.get("columns") or table.get("fields") or []):
            col_name = (
                col.get("fabric_column_name") or
                col.get("qlik_column_name") or
                col.get("name") or ""
            ).lower().replace(" ", "").replace("_", "")
            if col_name == norm:
                return table.get("name") or table.get("table_name")
    return None
