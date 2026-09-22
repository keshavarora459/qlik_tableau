import re


def extract_table_from_dax(dax_formula: str, tables: list = None) -> str:
    """
    Extracts the first table name found in a DAX formula.
    Looks for the pattern 'Table Name'[Column Name] or 'Table Name'.
    Fallback: If no table prefix is found, scans for [Column Names] and looks them up in the provided schema.
    Returns the table name or an empty string if not found.
    """
    if not dax_formula:
        return ""

    # 1. Primary Match: 'Table'[Column] or 'Table'
    match = re.search(r"'([^']+)'(?:\[.*?\])?", dax_formula)
    if match:
        return match.group(1)

    # 2. Schema Fallback: If we have the tables list, look for bare [Column] references
    if tables:
        found_cols = re.findall(r"\[([^\]]+)\]", dax_formula)
        for col_name in found_cols:
            # Skip if it's likely a measure reference (measures don't have table prefixes usually)
            # but in Power BI, every column MUST belong to a table.
            for t in tables:
                t_name = t.get("bi_table_name") or t.get("tableau_table_name") or t.get("table_name")
                for c in t.get("columns", []):
                    # Check against all possible names
                    names = [c.get("bi_column_name"), c.get("tableau_column_name"), c.get("name")]
                    if any(n == col_name for n in names if n):
                        return t_name

    return ""
