# rules/measures_core_rules.py
# Qlik Sense -> Power BI / Fabric Measures rules (Core rules 1 to 23)

MEASURES_CORE_RULES = [
    {
        "type": "measures",
        "id": "rule1",
        "priority": "critical",
        "rule": "HIGHEST PRIORITY RULE #1: When iterating with SUMX/AVERAGEX over a table, ANY column belonging to a different table MUST be wrapped with RELATED('OtherTable'[Column])."
    },
    {
        "type": "measures",
        "id": "1.6",
        "priority": "critical",
        "rule": "DATE-DIFF ITERATOR PRIORITY — Date difference between two tables must iterate over the MANY-side table, wrapping ONE-side field in RELATED()."
    },
    {
        "type": "measures",
        "id": "rule2",
        "priority": "critical",
        "rule": "DYNAMIC MANY-SIDE ITERATION — Select iterator (base) table dynamically from relationship metadata choosing the MANY-side fact table."
    },
    {
        "type": "measures",
        "id": "rule2.5",
        "priority": "critical",
        "rule": "There is no function named MATCH in DAX; never emit MATCH."
    },
    {
        "type": "measures",
        "id": "rule2.7",
        "priority": "critical",
        "rule": "BASE TABLE RESOLUTION — Identify referenced tables and select MANY-side table in One-to-Many relationships before applying RELATED() rules."
    },
    {
        "type": "measures",
        "id": "rule3",
        "rule": "Example: Qlik Sum(PRICEPERNIGHT * NIGHTS) over BOOKINGS → DAX SUMX('BOOKINGS', RELATED('ROOMS'[PRICEPERNIGHT]) * 'BOOKINGS'[NIGHTS])."
    },
    {
        "type": "measures",
        "id": "rule4",
        "rule": "Check for null values using NOT ISBLANK in FILTER conditions for aggregations to exclude blanks."
    },
    {
        "type": "measures",
        "id": "rule5",
        "priority": "critical",
        "rule": "Reference columns in full format 'Table'[ColumnName], enclosing table names in single quotes and columns in square brackets."
    },
    {
        "type": "measures",
        "id": "rule5.1",
        "priority": "critical",
        "rule": "NEVER output empty single-quoted table prefixes like ' '[Column] or ''[Column]. If a column's table is not specified or unresolvable, reference it as [Column] or qualify it with the base fact table."
    },
    {
        "type": "measures",
        "id": "rule6",
        "rule": "BOOLEAN NUMERIC MAPPING — Map Qlik TRUE (1 or {1}) → -1 and FALSE (0 or {0}) → 0."
    },
    {
        "type": "measures",
        "id": "rule7",
        "rule": "For Count(DISTINCT) in Qlik, use COUNTROWS(DISTINCT('Table'[Column])) or DISTINCTCOUNT."
    },
    {
        "type": "measures",
        "id": "rule8",
        "rule": "Dynamically apply RELATED() for cross-table field references in iterators like SUMX('BaseTable', ...)."
    },
    {
        "type": "measures",
        "id": "rule9",
        "rule": "CONSISTENCY ENFORCEMENT FOR CROSS-TABLE REFERENCES: Systematically wrap foreign table references in RELATED() within base table context."
    },
    {
        "type": "measures",
        "id": "rule10",
        "priority": "critical",
        "rule": "For divisions, use DIVIDE(numerator, denominator, 0) to handle divide-by-zero gracefully."
    },
    {
        "type": "measures",
        "id": "rule11",
        "rule": "Set dataType to 'int64' for counts, 'decimal' for sums/averages/percentages; use formattext like '0.00%' for percentages."
    },
    {
        "type": "measures",
        "id": "rule12",
        "rule": "For text fields representing numbers in math operations, convert using VALUE(TRIM('Table'[Column])) before aggregation."
    },
    {
        "type": "measures",
        "id": "rule13",
        "rule": "Apply column renames from mappings in DAX expressions, using renamed versions where specified."
    },
    {
        "type": "measures",
        "id": "rule14",
        "priority": "critical",
        "rule": "If column not found, replace with '// COLUMN_NOT_FOUND: [ColumnName]' and continue; validate using schema."
    },
    {
        "type": "measures",
        "id": "rule15",
        # scope=output_format rules describe the response envelope, not the
        # conversion itself. services/prompt_builder.py owns the output
        # contract (a bare DAX expression for measure/dimension prompts), so
        # injecting this would put two contradictory format instructions in
        # the same prompt and make the answer unparseable roughly half the
        # time. Excluded from prompts, kept here for reference.
        "scope": "output_format",
        "rule": "Return JSON with 'expression', 'dataType', 'formattext' only, ensuring valid syntax without extra text."
    },
    {
        "type": "measures",
        "id": "rule16",
        "rule": "For iterators like SUMX/AVERAGEX, provide table first, then expression with full column references."
    },
    {
        "type": "measures",
        "id": "rule17",
        "rule": "Preserve exact casing for tables/columns from schema; avoid uppercase unless specified."
    },
    {
        "type": "measures",
        "id": "rule18",
        "rule": "In conditionals like SWITCH(TRUE(), ...), use && for AND, || for OR; avoid single & or |."
    },
    {
        "type": "measures",
        "id": "rule19",
        "rule": "Use VALUE() for text-to-numeric conversions only when schema indicates 'string' type for numeric ops."
    },
    {
        "type": "measures",
        "id": "rule20",
        "rule": "For boolean fields, use TRUE/FALSE; for text Yes/No, use UPPER('Table'[Column]) = 'YES'."
    }
]
