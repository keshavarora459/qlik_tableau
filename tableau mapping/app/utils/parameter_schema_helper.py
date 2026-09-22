"""
Shared helper to build a consistent parameter schema context for all LLM prompts.

This ensures that EVERY conversion path (Measures, Calculated Fields, LODs, Sets)
tells the LLM the EXACT SELECTEDVALUE reference to use for each parameter,
eliminating guesswork about column names.
"""

import re


def build_parameter_schema_lines(parameters: list[dict]) -> list[str]:
    """
    Builds schema context lines for parameters that explicitly tell the LLM
    the correct SELECTEDVALUE('Table'[Column]) reference for each parameter.

    Handles three parameter types:
      1. DATATABLE parameters -> column is always 'Value'
      2. DISTINCT parameters  -> column is extracted from the DISTINCT() DAX
      3. Other parameters     -> generic description with data type

    Returns:
        A list of formatted schema-context strings.
    """
    if not parameters:
        return []

    lines = ["\nAvailable Power BI Parameters:"]

    for p in parameters:
        p_name = p.get("name", "Unknown")

        tableau_info = p.get("tableau", {})
        powerbi_info = p.get("powerbi", {})

        p_type = tableau_info.get("data_type", "Unknown")
        pbi_kind = powerbi_info.get("parameter_kind", "Unknown")
        pbi_dax = powerbi_info.get("dax") or ""

        # --- DATATABLE and GENERATESERIES parameters: column is always 'Value' ---
        if pbi_dax and ("DATATABLE" in pbi_dax or "GENERATESERIES" in pbi_dax):
            lines.append(
                f"- Parameter '{p_name}' -> MUST use: SELECTEDVALUE('{p_name}'[Value])"
            )

        # --- DISTINCT parameters: extract column from DAX ---
        elif pbi_dax and "DISTINCT(" in pbi_dax:
            col_match = re.search(
                r"MAX\('[^']+'\[([^\]]+)\]\)|DISTINCT\('[^']+'\[([^\]]+)\]\)",
                pbi_dax
            )
            if col_match:
                p_col = col_match.group(1) or col_match.group(2)
                lines.append(
                    f"- Parameter '{p_name}' -> MUST use: SELECTEDVALUE('{p_name}_Table'[{p_col}])"
                )
            else:
                lines.append(
                    f"- Parameter '{p_name}' (Data Type: {p_type} | Implementation: {pbi_kind})"
                )

        # --- Fallback: generic description ---
        else:
            lines.append(
                f"- Parameter '{p_name}' (Data Type: {p_type} | Implementation: {pbi_kind})"
            )

    return lines
