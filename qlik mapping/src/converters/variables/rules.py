# src/converters/variables/rules.py
# Qlik Sense -> Power BI / Fabric VARIABLE conversion rules.
#
# There was no rules file for variables at all, and no converter: the mapping
# result passed `variables` through verbatim (summary_builder.format_passthrough),
# so nothing ever decided what a Qlik variable should *become* in Fabric. The
# generation agent then swept every variable - including the ~25 Qlik reserved
# locale variables present in a typical app (ThousandSep, DateFormat,
# MoneyFormat, FirstMonthOfYear) - into a single shared Parameters table.
#
# The classification below is the core of the fix, and follows Qlik's own
# documentation on variables and dollar-sign expansion:
#
#   reserved    a Qlik system/locale setting (SET ThousandSep=',')
#               -> model culture + column format strings. NOT a parameter.
#   literal     a plain value used as a constant (vTaxRate = 0.2)
#               -> inline the literal, or a constant DAX measure.
#   parameter   a value variable driven by a variable-input object (vTopN)
#               -> its own what-if parameter table + SELECTEDVALUE measure.
#   expression  the variable holds a formula (vSales = Sum(Sales))
#               -> a DAX MEASURE. A Parameters row holding the text
#                  "Sum(Sales)" is inert and computes nothing.

VARIABLES_RULES = [
    # ── classification ────────────────────────────────────────────────────
    {
        "type": "variables",
        "id": "var1",
        "priority": "critical",
        "rule": "CLASSIFY FIRST — Every Qlik variable maps to exactly one of four Fabric targets: 'reserved' "
                "(a Qlik system/locale setting), 'literal' (a constant value), 'parameter' (a value the user "
                "changes interactively), or 'expression' (the variable holds a formula). Choose the kind before "
                "deciding anything else; the correct Fabric artifact is completely different for each."
    },
    {
        "type": "variables",
        "id": "var2",
        "priority": "critical",
        "rule": "RESERVED VARIABLES ARE NOT PARAMETERS — A variable flagged is_reserved, or created by a SET "
                "statement in the load script for locale/formatting (ThousandSep, DecimalSep, MoneyThousandSep, "
                "MoneyDecimalSep, MoneyFormat, DateFormat, TimeFormat, TimestampFormat, MonthNames, DayNames, "
                "LongMonthNames, LongDayNames, FirstWeekDay, BrokenWeeks, ReferenceDay, FirstMonthOfYear, "
                "CollationLocale, NumericalAbbreviation), maps to kind='reserved'. It becomes semantic-model "
                "culture and column format strings - NEVER a row in a parameter table and NEVER a measure. "
                "Emitting these as parameters pollutes the report with meaningless slicer values."
    },
    {
        "type": "variables",
        "id": "var3",
        "priority": "critical",
        "rule": "EXPRESSION VS VALUE — If the definition begins with '=' or contains a Qlik aggregation "
                "(Sum, Count, Avg, Min, Max, Only, Aggr) or set analysis ('{<'), it is kind='expression'. "
                "Otherwise, if the definition is a plain number, date or string, it is a value variable "
                "(kind='literal' or 'parameter')."
    },
    {
        "type": "variables",
        "id": "var4",
        "rule": "PARAMETER DETECTION — A value variable is kind='parameter' when it is bound to an interactive "
                "object (a qlik-variable-input / variable-input visual references it), when the payload supplies "
                "alternatives/options/values for it, or when its name signals user choice (vTopN, vSelected*, "
                "vChosen*, vShow*, vToggle*, vMeasure, vThreshold). Otherwise it is kind='literal'."
    },
    {
        "type": "variables",
        "id": "var5",
        "rule": "UNUSED VARIABLES — A non-reserved variable with usage_count 0 and no interactive binding still "
                "converts, but set requires_review true and note that nothing in the app referenced it. Do not "
                "silently drop it: a variable may be referenced from a load script this payload does not carry."
    },

    # ── expression variables -> DAX measures ──────────────────────────────
    {
        "type": "variables",
        "id": "var6",
        "priority": "critical",
        "rule": "EXPRESSION VARIABLE -> MEASURE — kind='expression' produces a DAX measure named exactly after the "
                "variable. Convert the Qlik formula to DAX under the full measure ruleset: qualify columns as "
                "'Table'[Column], use DIVIDE for division, apply RELATED() toward the One side and aggregate "
                "toward the Many side. Set fabric.kind='measure' and put the DAX in fabric.dax_expression."
    },
    {
        "type": "variables",
        "id": "var7",
        "rule": "HOME TABLE FOR A VARIABLE MEASURE — Place the measure on the table supplying most of its "
                "referenced columns; when it references none (a pure constant or a disconnected calculation), "
                "place it on the dedicated measures table. Never invent a table that is not in the schema."
    },
    {
        "type": "variables",
        "id": "var8",
        "rule": "NESTED VARIABLE REFERENCES — A variable definition may reference other variables via $(...). Those "
                "are expanded before you see them. If an unexpanded '$(' remains, the reference was circular or "
                "the target is missing: emit the rest and add '// UNRESOLVED_VARIABLE: name'. Never emit '$(' in "
                "DAX or M - it is valid in neither language."
    },

    # ── parameter variables -> what-if parameters ─────────────────────────
    {
        "type": "variables",
        "id": "var9",
        "priority": "critical",
        "rule": "PARAMETER VARIABLE -> OWN TABLE — kind='parameter' produces its OWN single-column table named "
                "after the variable, not a row in a shared Parameters table. A shared Parameter/Value/Label table "
                "makes one slicer filter every parameter at once, so no parameter can be set independently. Set "
                "fabric.kind='parameter' and fabric.table to the variable name."
    },
    {
        "type": "variables",
        "id": "var10",
        "priority": "critical",
        "rule": "PARAMETER SELECTION MEASURE — Every kind='parameter' variable also needs its selection measure, "
                "'<name> Value' = SELECTEDVALUE('<name>'[<name>], <default>), where <default> is the variable's "
                "own current value. Expressions reference the measure, never the raw parameter column."
    },
    {
        "type": "variables",
        "id": "var11",
        "rule": "PARAMETER DATA TYPE — Infer the parameter column type from the value: a whole number is int64, a "
                "decimal is double, a date is dateTime, anything else is string. Emit a numeric parameter as a "
                "numeric column so downstream DAX does not need VALUE() at every use; only fall back to string "
                "when the alternatives are genuinely non-numeric."
    },
    {
        "type": "variables",
        "id": "var12",
        "rule": "PARAMETER RANGE — When a numeric parameter has no explicit alternatives, generate the table with "
                "GENERATESERIES(min, max, step) using the source min/max when supplied, otherwise a sensible range "
                "bracketing the current value. When explicit alternatives exist, use a DATATABLE of exactly those "
                "values in their original order - never invent extra options."
    },
    {
        "type": "variables",
        "id": "var13",
        "rule": "MEASURE-SELECTOR PARAMETERS — A variable whose alternatives are measure names or field names "
                "(commonly vMeasure, vDimension) drives a switch, not a number. Emit the parameter table of those "
                "names and a companion SWITCH(SELECTEDVALUE(...), \"OptionA\", [MeasureA], \"OptionB\", [MeasureB], "
                "BLANK()) measure. Always include the final BLANK() default."
    },

    # ── literals ──────────────────────────────────────────────────────────
    {
        "type": "variables",
        "id": "var14",
        "rule": "LITERAL VARIABLES — kind='literal' does not need any model artifact of its own: the value is "
                "substituted directly into the expressions that used it. Emit fabric.kind='literal' with the "
                "resolved value and its inferred data type, so downstream conversion can inline it. Only promote a "
                "literal to a constant measure when it is referenced by three or more expressions, where a single "
                "named measure is easier to maintain than repeated magic numbers."
    },
    {
        "type": "variables",
        "id": "var15",
        "rule": "DATE LITERALS — A Qlik date variable is usually a serial number (vMinDate = 44562). Convert it to "
                "a real date, since a raw serial compared against a dateTime column produces a type error. Qlik "
                "day 1 is 1899-12-30, matching Excel. Emit both the serial and the ISO date so the consumer can "
                "choose."
    },
    {
        "type": "variables",
        "id": "var16",
        "rule": "NAME SANITISATION — Keep the Qlik variable name as the Fabric object name wherever it is legal. "
                "Strip characters TMDL cannot carry in an unquoted identifier, but never translate, abbreviate or "
                "re-case the name: expressions elsewhere refer to it by its exact original spelling. Record any "
                "change in fabric.renamed_from."
    },
    {
        "type": "variables",
        "id": "var17",
        "priority": "critical",
        "rule": "NO FABRICATED VALUES — Never invent a default, a range, or an alternative that the source did not "
                "supply. If a parameter's options cannot be determined, emit the single current value and set "
                "requires_review true with a note explaining what a human must supply."
    },
]

# The four kinds a Qlik variable can resolve to. `reserved` produces no model
# artifact at all - it feeds the model's culture/format settings instead.
VARIABLE_KINDS = ("reserved", "literal", "parameter", "expression")

VARIABLES_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["name", "kind", "confidence"],
    "properties": {
        "name": {"type": "string", "description": "The Qlik variable name, unchanged."},
        "kind": {
            "type": "string",
            "enum": list(VARIABLE_KINDS),
            "description": "Which Fabric target this variable maps to (see var1).",
        },
        "renamed_from": {
            "type": ["string", "null"],
            "description": "Set only when the Fabric name had to differ from the Qlik name.",
        },
        "data_type": {
            "type": "string",
            "enum": ["int64", "double", "decimal", "dateTime", "boolean", "string"],
        },
        # kind = literal
        "literal_value": {
            "type": ["string", "number", "null"],
            "description": "The resolved constant, for inlining into expressions.",
        },
        "iso_date": {
            "type": ["string", "null"],
            "description": "For date serials, the ISO date the serial resolves to (var15).",
        },
        # kind = expression
        "dax_expression": {
            "type": ["string", "null"],
            "description": "DAX measure body, for kind=expression only.",
        },
        "home_table": {
            "type": ["string", "null"],
            "description": "Table the measure is placed on; must exist in the SCHEMA (var7).",
        },
        # kind = parameter
        "parameter": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string"},
                "column_name": {"type": "string"},
                "selection_measure_name": {"type": "string"},
                "selection_measure_dax": {"type": "string"},
                "generation_strategy": {
                    "type": "string",
                    "enum": ["datatable", "generateseries"],
                },
                "values": {
                    "type": "array",
                    "items": {"type": ["string", "number"]},
                    "description": "Explicit alternatives, in source order. Never invented.",
                },
                "min": {"type": ["number", "null"]},
                "max": {"type": ["number", "null"]},
                "step": {"type": ["number", "null"]},
                "default_value": {"type": ["string", "number", "null"]},
                "switch_measure_dax": {
                    "type": ["string", "null"],
                    "description": "For measure-selector parameters (var13).",
                },
            },
        },
        # kind = reserved
        "format_target": {
            "type": ["string", "null"],
            "description": "Which model setting this locale variable feeds "
                           "(culture, date_format, currency_symbol, decimal_separator, ...).",
        },
        "requires_review": {"type": "boolean"},
        "note": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "rationale": {"type": "string"},
    },
}
