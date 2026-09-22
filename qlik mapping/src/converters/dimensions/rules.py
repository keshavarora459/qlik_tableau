# rules/dimensions_rules.py
# Qlik Sense -> Power BI / Fabric Dimensions rules

DIMENSIONS_RULES = [
    {
        "type": "dimensions",
        "id": "rule1",
        "rule": "Retain the 'Dimension_count' field as provided."
    },
    {
        "type": "dimensions",
        "id": "rule2",
        "priority": "critical",
        "rule": "DIMENSION NAME NORMALIZATION + CALCULATED COLUMN RESOLUTION — When converting any Qlik Sense dimension to a Power BI DAX calculated column, apply dynamic logic: remove 'Calculated_' prefix for clean name matching against schema fields, wrap with 'Calculated_' if exact schema match, preserve Qlik expression, convert to DAX calculated column."
    },
    {
        "type": "dimensions",
        "id": "rule3",
        "rule": "Handle renamed columns: Use the Renames Mapping to replace column names in the DAX expression with their renamed versions."
    },
    {
        "type": "dimensions",
        "id": "rule4",
        "priority": "critical",
        "rule": "Use the schema to map fields to their tables accurately (case-insensitive) and determine if fields need VALUE() wrapping."
    },
    {
        "type": "dimensions",
        "id": "rule5",
        "priority": "critical",
        "rule": "For fields from related tables: Use RELATED('RelatedTable'[ColumnName]) for single values, RELATEDTABLE() for sets of values."
    },
    {
        "type": "dimensions",
        "id": "rule6",
        "priority": "critical",
        "rule": "MISSING FIELD RESOLUTION — Assign unresolvable fields to a predefined fallback table category based on external mapping without overriding existing schema info."
    },
    {
        "type": "dimensions",
        "id": "rule7",
        # See the note on measures rule15: the response envelope is owned by
        # services/prompt_builder.py, so this must not reach a prompt that
        # asks for a bare DAX expression.
        "scope": "output_format",
        "rule": "Return a valid JSON object with 'dimension_count' and 'dimensions' list, without Markdown code fences or extra text."
    },
    {
        "type": "dimensions",
        "id": "rule8",
        "rule": "Transform Capitalize([SPECIALIZATION]) to DAX using UPPER(LEFT(InputText, 1)) & LOWER(RIGHT(InputText, LEN(InputText) - 1))."
    },
    {
        "type": "dimensions",
        "id": "rule9",
        "rule": "NUMERIC TYPE NORMALIZATION — Ensure operands in numeric comparisons (>, >=, <, <=) or arithmetic are wrapped with VALUE() if text-type."
    },
    {
        "type": "dimensions",
        "id": "rule10",
        "rule": "AGGREGATION-AWARE DAX FOR DIMENSIONS: If Qlik dimension contains Sum(), Avg(), Count(), use iterator functions SUMX, AVERAGEX, COUNTROWS, DISTINCTCOUNT."
    },
    {
        "type": "dimensions",
        "id": "rule11",
        "priority": "critical",
        "rule": "INTRA-TABLE COLUMN REFERENCE RULE — Do not use RELATED() for columns residing in the same table as the calculated column."
    },
    {
        "type": "dimensions",
        "id": "rule12",
        "rule": "ITERATOR REMOVAL FOR SINGLE-ROW LOGIC — Avoid row-iteration functions when logic only depends on single row scalar evaluation."
    },
    {
        "type": "dimensions",
        "id": "rule13",
        "rule": "MONTHNAME FUNCTION OVERRIDE — Convert MonthName(...) expressions to DAX FORMAT([ResolvedField], \"dd mmm yyyy\")."
    },
    {
        "type": "dimensions",
        "id": "rule14",
        "rule": "No pseudo-DAX date calls: Always emit valid DAX FORMAT string transformations for date display."
    },
    {
        "type": "dimensions",
        "id": "rule15",
        "rule": "Handle Qlik If statements with numeric comparisons for calculated columns or measures in DirectQuery models."
    },
    {
        "type": "dimensions",
        "id": "rule16",
        "rule": "MonthName guardrails: Ensure final display shape matches \"Jan 2025\" via FORMAT([ResolvedField], \"dd mmm yyyy\")."
    },
    {
        "type": "dimensions",
        "id": "rule17",
        "rule": "CONCATENATION & FIELD RESOLUTION — Convert Qlik string concatenation (&) to DAX & operator, resolving multi-table fields via RELATED()."
    },
    {
        "type": "dimensions",
        "id": "rule18",
        "rule": "VALUE-GUARD VALIDATION — Perform validation pass across numeric logic to wrap text-declared fields in VALUE()."
    },
    {
        "type": "dimensions",
        "id": "rule19",
        "rule": "For age calculations in DAX, replace Qlik Floor with INT to truncate decimal division result, adjusting TODAY() calls."
    },
    {
        "type": "dimensions",
        "id": "rule20",
        "rule": "WILDMATCH → CONTAINSSTRING TRANSLATION — For nested If with WildMatch(Upper(Trim(<Field>)), '*<pattern>*'), generate row-level DAX with CONTAINSSTRING."
    }
]
