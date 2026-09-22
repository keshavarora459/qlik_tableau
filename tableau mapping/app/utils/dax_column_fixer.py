"""
DAX Column Name Fixer — Post-LLM Validation Step

After the LLM generates a DAX formula, this module scans every
'Table'[Column] reference and ensures the column name matches a valid
bi_column_name from the schema.  If the LLM used a tableau_column_name
(or any other alias), it is silently replaced with the correct
bi_column_name.

This is the MANDATORY guard that ensures the final DAX output always
uses the authoritative Power BI column identities.
"""

import re

# ──────────────────────────────────────────────────────────
# Normalisation (mirrors the one in calculated_fields_service)
# ──────────────────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Lowercase, strip spaces/underscores/parens/punctuation for fuzzy matching."""
    if not name:
        return ""
    # Remove spaces, underscores, parens, brackets, and colons/punctuation
    chars_to_remove = [" ", "_", "(", ")", "[", "]", ":", ".", ",", ";"]
    res = name.lower()
    for c in chars_to_remove:
        res = res.replace(c, "")
    return res


# ──────────────────────────────────────────────────────────
# Build lookup:  (norm_table, norm_any_alias) → bi_column_name
# ──────────────────────────────────────────────────────────

def _build_column_lookup(all_tables: list) -> dict:
    """
    Returns a dict keyed by (normalised_table_name, normalised_alias)
    → actual bi_column_name string.

    Every known alias for a column is registered:
      - tableau_column_name
      - tableau_renamed_column_name
      - renamed_column_name
      - bi_column_name  (identity mapping)
      - name            (raw parser name)

    A second tier (table-agnostic) is keyed by normalised_alias alone
    → bi_column_name, used when the DAX references a column without
    a table qualifier.
    """
    table_qualified: dict[tuple[str, str], str] = {}
    unqualified: dict[str, str] = {}

    for table in all_tables:
        # All possible table name variants
        table_names_raw = [
            table.get("bi_table_name"),
            table.get("tableau_table_name"),
            table.get("table_name"),
        ]
        norm_table_names = {_normalize(n) for n in table_names_raw if n}

        for col in table.get("columns", []):
            # The AUTHORITATIVE name
            bi_col = (
                col.get("bi_column_name")
                or col.get("tableau_renamed_column_name")
                or col.get("renamed_column_name")
                or col.get("tableau_column_name")
                or col.get("name")
            )
            if not bi_col:
                continue

            # Collect every alias the LLM might have used
            aliases_raw = [
                col.get("tableau_column_name"),
                col.get("tableau_renamed_column_name"),
                col.get("renamed_column_name"),
                col.get("bi_column_name"),
                col.get("name"),
            ]

            # Register the "Source Aliases" we use in the schema context
            # so if the LLM hallucinations them, they are corrected to bi_col.
            bi_name_val = col.get("bi_column_name")
            tab_name_val = col.get("tableau_column_name")
            if bi_name_val and tab_name_val and bi_name_val != tab_name_val:
                aliases_raw.append(f"{bi_name_val} (Source: {tab_name_val})")
                aliases_raw.append(f"{bi_name_val} [Source: {tab_name_val}]")

            norm_aliases = {_normalize(a) for a in aliases_raw if a}


            for nt in norm_table_names:
                for na in norm_aliases:
                    table_qualified[(nt, na)] = bi_col

            for na in norm_aliases:
                # First-come wins for unqualified (avoids cross-table collisions)
                if na not in unqualified:
                    unqualified[na] = bi_col

    return {"qualified": table_qualified, "unqualified": unqualified}


# ──────────────────────────────────────────────────────────
# Core fixer
# ──────────────────────────────────────────────────────────

# Matches  'Table Name'[Column Name] and consumes any number of adjacent brackets
_QUALIFIED_RE = re.compile(r"'([^']+)'\[+(.*?)\]+")

# Matches bare  [Column Name]  (not preceded by ') and consumes adjacent brackets
_UNQUALIFIED_RE = re.compile(r"(?<!')\[+(.*?)\]+")


def validate_and_fix_dax_columns(dax_formula: str, all_tables: list, known_measures: list = None) -> str:
    """
    Post-processes a DAX formula so that every column reference uses the
    correct bi_column_name.

    1. Scans for 'Table'[Column] patterns → looks up (table, column)
       in the qualified dictionary and replaces with bi_column_name.
    2. Scans for bare [Column] patterns → looks up in unqualified dict.
    3. If known_measures is provided, strips table prefixes from them.
    4. Strips hallucinated SELECTEDVALUE() wrappers from measures.
    """
    if not dax_formula or not all_tables:
        return dax_formula


    # 0. PRE-PASS: Strip '(Source: ...)' or '[Source: ...]' labels from the formula.
    # The LLM often includes these hints from the schema context in the final output.
    # We strip them early so the matching logic below works on the 'clean' names.
    _SOURCE_HINTS_RE = re.compile(r"\s*[\(\[]Source:\s*.*?[\)\]]", re.IGNORECASE)
    dax_formula = _SOURCE_HINTS_RE.sub("", dax_formula)

    lookup = _build_column_lookup(all_tables)
    qualified = lookup["qualified"]
    unqualified = lookup["unqualified"]

    def _format_qualified(table: str, col: str) -> str:
        col = col.strip()
        if col.startswith("[") and col.endswith("]"):
            return f"'{table}'{col}"
        return f"'{table}'[{col}]"

    def _format_unqualified(col: str) -> str:
        col = col.strip()
        if col.startswith("[") and col.endswith("]"):
            return col
        return f"[{col}]"

    # PASS 1 — Fix qualified references  'Table'[Column]
    def _fix_qualified(match: re.Match) -> str:
        table_raw = match.group(1)
        col_raw = match.group(2)
        key = (_normalize(table_raw), _normalize(col_raw))
        bi_col = qualified.get(key)
        if bi_col:
            return _format_qualified(table_raw, bi_col)
        return match.group(0)

    result = _QUALIFIED_RE.sub(_fix_qualified, dax_formula)

    # PASS 2 — Fix unqualified references  [Column]
    # Skip anything that was already handled in pass 1 (inside 'Table'[...])
    def _fix_unqualified(match: re.Match) -> str:
        col_raw = match.group(1)
        # Don't replace if it looks like it just matched inside a qualified ref
        start = match.start()
        if start >= 2 and result[start - 1] == "'" and result[start - 2] == "'":
            return match.group(0)
        key = _normalize(col_raw)
        bi_col = unqualified.get(key)
        if bi_col:
            return _format_unqualified(bi_col)
        return match.group(0)

    result = _UNQUALIFIED_RE.sub(_fix_unqualified, result)

    # PASS 2.5 — Strip table prefixes from known measures
    # If the LLM generated 'Table'[KnownMeasure], turn it into [KnownMeasure]
    if known_measures:
        for m in known_measures:
            if not m:
                continue
            # Match 'AnyTable'[m]
            # Uses re.escape to handle any special characters in the measure name safely
            pattern = r"'[^']+'\s*\[" + re.escape(m) + r"\]"
            result = re.sub(pattern, f"[{m}]", result, flags=re.IGNORECASE)

    # PASS 3 — Fix hallucinated SELECTEDVALUE([Measure]) wrapping
    # The LLM often improperly wraps bare measure references in SELECTEDVALUE().
    # This regex matches SELECTEDVALUE( [something] ) optionally followed by a secondary argument.
    # It ensures there's no single quote preceding the trigger to avoid catching SELECTEDVALUE('Table'[Col]).
    _SV_MEASURE_RE = re.compile(r"SELECTEDVALUE\s*\(\s*(?<!')(\[[^\]]+\])\s*(?:,\s*(.*?))?\s*\)", re.IGNORECASE)

    def _fix_selectedvalue(match: re.Match) -> str:
        measure = match.group(1)
        alt = match.group(2)
        if alt:
            # If a fallback value was provided: SELECTEDVALUE([Measure], "Alt") -> COALESCE([Measure], "Alt")
            return f"COALESCE({measure}, {alt})"
        # If no fallback: SELECTEDVALUE([Measure]) -> [Measure]
        return measure

    result = _SV_MEASURE_RE.sub(_fix_selectedvalue, result)

    return result
