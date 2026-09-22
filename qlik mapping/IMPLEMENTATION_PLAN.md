# Qlik to DAX Mapping Agent Fixes

This plan outlines the fixes for the 6 specific problems identified in the Qlik-to-DAX conversion logic.

## 1. AGGR() Translation Fix
**Root Cause**: `translate_aggr` in `qlik_patterns.py` uses case-sensitive `expression.find("Aggr")`. When Qlik uses `aggr` (lowercase), `str.find` returns `-1`, breaking the AST paren-matching and falling back to standalone translation, leaving the outer `SUM(` intact.
**Fix**: Update `translate_aggr` to use `expression.lower().find("aggr", start)` to correctly identify the start of the `Aggr` block regardless of casing.

## 2. MATCH() Translation for Decayed GPA
**Root Cause**: The `MATCH()` conversion logic was previously implemented but may not have covered all edge cases in the Decayed GPA metric.
**Fix**: Verify the `translate_match` logic is robust against nested DAX functions (`UPPER`, `TRIM`) and arbitrary whitespace. My recent fix for `MATCH()` already uses AST-aware splitting (`_split_top_level`) and correctly converts it to `SWITCH(TRUE(), ...)`. I will ensure it is correctly integrated without leaving any `MATCH` instances behind.

## 3. Qlik $() Expansion to DAX Variables
**Root Cause**: `$(=...)` expressions are currently preserved intact by `variable_expander.py` and then passed into DAX verbatim as `$(=...)`, which is invalid DAX.
**Fix**: Intercept `$(=...)` and `$(vVar)` constructs in `dax_converter.py` prior to DAX conversion. Extract their inner expressions, convert them to DAX, and hoist them into DAX `VAR var_n = <dax>` declarations. Replace the `$()` in the main expression with `var_n`, and wrap the final DAX output in a `RETURN` block.

## 4. Date and ASSIGNED_DATE_DATE Qualification
**Root Cause**: `build_column_index` in `services/dax_identifiers.py` uses case-sensitive keys for the column index, causing lookups for `Date` to fail if the schema defines it as `DATE`.
**Fix**: Normalize keys in `build_column_index` by lowercasing them: `index[name.lower()] = table_name`, and perform lookups using `token.lower()`.

## 5. Qlik 1 / Set Analysis
**Root Cause**: In `set_analysis_ast.py`, expressions like `Sum({<Date={...}>} 1)` are translated into `CALCULATE(SUM([1]), ...)` because the AST builder assumes `1` is a field name when isolating the body of the aggregation.
**Fix**: Update the AST translation logic in `set_analysis_ast.py`. If the aggregation body is exactly `1` (or another numeric literal), translate `SUM([1])` or `SUM(1)` to `COUNTROWS('Table')` or simply `SUMX('Table', 1)` instead of wrapping `1` in brackets, ensuring it isn't flagged as an unqualified column.

## 6. ASSIGNED_DATE Qualification
**Root Cause**: Bracketed columns like `[ASSIGNED_DATE]` were previously untouched.
**Fix**: My recent update to `qualify_columns` handles this, but I will review it to guarantee that `[ASSIGNED_DATE]` is resolved dynamically against the schema rather than hardcoded, satisfying the regression baseline.

## Final Validation
Ensure `unconverted_qlik_functions`, `unconverted_qlik_syntax`, and `unqualified_columns` are strictly enforced in `convert_measure` and that `conversion_status` fails appropriately.
