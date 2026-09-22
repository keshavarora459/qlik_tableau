# rules/measures_advanced_rules.py
# Qlik Sense -> Power BI / Fabric Measures rules (Advanced rules 21 to 44)

MEASURES_ADVANCED_RULES = [
    # -- Qlik language semantics the regex converter cannot express --------
    # These come straight from Qlik's own documentation on variables and on
    # references to fields/measures/variables, and cover the two constructs
    # that previously produced silently-invalid DAX.
    {
        "type": "measures",
        "id": "rule45",
        "priority": "critical",
        "rule": "DOLLAR-SIGN EXPANSION IS ALREADY RESOLVED — In Qlik, $(vName) is textual substitution performed "
                "BEFORE the expression is parsed, so it is not a value reference. By the time you see the "
                "expression it has been expanded for you: an expression variable is replaced by its formula and a "
                "value variable by its literal. If any '$(' still appears in the input, the variable was "
                "unresolvable: do NOT emit it, do NOT invent a value. Emit the rest of the expression and leave a "
                "'// UNRESOLVED_VARIABLE: vName' comment. A '$(' must never appear in your output - it is not DAX."
    },
    {
        "type": "measures",
        "id": "rule46",
        "priority": "critical",
        "rule": "MEASURE ALIAS REFERENCES — Per Qlik, a measure's label used inside an expression is an alias for "
                "that measure. If a bracketed name in the source matches an entry under AVAILABLE MEASURES, emit it "
                "as a DAX measure reference [Measure Name] with NO table prefix and NO further qualification. "
                "NEVER rewrite it into a column reference, and NEVER nest a qualifier inside it - "
                "[Total 'Sales'[Cost]] is malformed. Square brackets never nest in DAX."
    },
    {
        "type": "measures",
        "id": "rule47",
        "priority": "critical",
        "rule": "WHAT-IF PARAMETER REFERENCES — When an expanded value variable came from an interactive Qlik "
                "variable-input object, it is a Power BI what-if parameter, not a constant. Reference it as "
                "SELECTEDVALUE('vName'[vName]) so the slicer drives it. Wrap it in VALUE() or INT() before any "
                "numeric comparison, because the parameter table stores values as text."
    },
    {
        "type": "measures",
        "id": "rule48",
        "priority": "critical",
        "rule": "SET ANALYSIS WITH AN EXPANDED VARIABLE — A Qlik set modifier whose value came from a variable, "
                "e.g. {<Year={$(vYear)}>}, becomes a CALCULATE filter on the resolved value, NOT a string literal "
                "containing the variable name. Emit CALCULATE(<agg>, 'Table'[Year] = <resolved value or measure>). "
                "Quoting the unresolved token produces a filter that silently matches no rows."
    },
    {
        "type": "measures",
        "id": "rule21",
        "rule": "For concatenations, use & with double-quoted strings; FORMAT numerics to text if needed."
    },
    {
        "type": "measures",
        "id": "rule22",
        "rule": "In FILTER, include NOT ISBLANK('Table'[Column]) for null handling in counts/denominators."
    },
    {
        "type": "measures",
        "id": "rule23",
        "rule": "Replace Qlik KeepChar/Num# with TRIM and SUBSTITUTE for known chars, then VALUE."
    },
    {
        "type": "measures",
        "id": "rule24",
        "priority": "critical",
        "rule": "SET MAPPING — Map Qlik set-analysis with literal lists (<identifier={val1,val2}>) to COUNTROWS(FILTER('Table', 'Table'[identifier] IN {formatted_list}))."
    },
    {
        "type": "measures",
        "id": "rule25",
        "rule": "For ratios/percentages, use DIVIDE with COUNTROWS(FILTER(NOT ISBLANK)) for denominators."
    },
    {
        "type": "measures",
        "id": "rule26",
        "rule": "For averages/sums on text numerics, use AVERAGEX/SUMX('Table', VALUE(TRIM('Table'[Column])))."
    },
    {
        "type": "measures",
        "id": "rule27",
        "rule": "Use ROUND for precision matching if needed, e.g., ROUND(DIVIDE(...) * 100, 2)."
    },
    {
        "type": "measures",
        "id": "rule28",
        "rule": "For Qlik set analysis with flags, use CALCULATE with filters like 'Table'[Flag] = 'Yes'."
    },
    {
        "type": "measures",
        "id": "rule29",
        "rule": "Use variables (VAR) for intermediate calculations in complex measures for readability."
    },
    {
        "type": "measures",
        "id": "rule30",
        "rule": "For date differences, use DATEDIFF or TODAY() - 'Table'[Date]; iterate with AVERAGEX for avgs."
    },
    {
        "type": "measures",
        "id": "rule31",
        "rule": "Prefer direct subtraction ('Table'[EndDate] - 'Table'[StartDate]) for day count differences; use DATEDIFF for non-DAY intervals."
    },
    {
        "type": "measures",
        "id": "rule32",
        "priority": "critical",
        "rule": "Validate tables/columns/relationships exist; replace invalid with '// NOT_FOUND' comments."
    },
    {
        "type": "measures",
        "id": "rule33",
        "rule": "For Qlik Aggr/If counts, use SUMMARIZE with CALCULATE and FILTER for conditional distincts."
    },
    {
        "type": "measures",
        "id": "rule34",
        "priority": "critical",
        "rule": "Enforce full column qualification 'Table'[Column] everywhere; halt on unqualified refs."
    },
    {
        "type": "measures",
        "id": "rule35",
        "rule": "In SWITCH expressions for categorization, include a default case like BLANK() or 0 to handle unmatched values."
    },
    {
        "type": "measures",
        "id": "rule36",
        "rule": "ENFORCE: In SUM(col1 + col2), ALWAYS emit SUMX('Table', VALUE(col1) + VALUE(col2))."
    },
    {
        "type": "measures",
        "id": "rule37",
        "rule": "Qlik Count({<Cond={Mod}>} Key)/Count(Key)*100: resolve via schema, emit DIVIDE with NOT ISBLANK filtering."
    },
    {
        "type": "measures",
        "id": "rule39",
        "rule": "For Num#/KeepChar/Trim agg: X('T', VALUE(TRIM(SUBSTITUTE('T'[F],...)))) with X=SUMX/AVERAGEX."
    },
    {
        "type": "measures",
        "id": "rule40",
        "rule": "For date bucketing like WeekStart([DateField]), convert using COUNTROWS(DISTINCT(SELECTCOLUMNS(...)))."
    },
    {
        "type": "measures",
        "id": "rule41",
        "priority": "critical",
        "rule": "CRITICAL CROSS-TABLE RULE: Only use RELATED() inside row iterators over MANY-side table referencing ONE-side table."
    },
    {
        "type": "measures",
        "id": "rule43",
        "rule": "Distinct Count Expansion: Expand Count(Distinct <identifier>) into COUNTROWS(DISTINCT(SELECTCOLUMNS(FILTER('Table', NOT ISBLANK('Table'[Field])), \"Field\", 'Table'[Field])))."
    },
    {
        "type": "measures",
        "id": "rule44",
        "rule": "QLIK COUNT-SET PERCENT FORMULA: Use VAR TotalCount = CALCULATE(COUNT('T'[Key]), REMOVEFILTERS('T')) pattern with DIVIDE and CALCULATE."
    }
]
