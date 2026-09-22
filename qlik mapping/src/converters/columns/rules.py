# src/converters/columns/rules.py
# Qlik field -> Fabric column: data type, summarizeBy, and format string.
#
# This replaces the single most report-specific piece of logic in the service.
# agents/coordinator_agent.py inferred column types from hardcoded English
# keyword lists:
#
#   ["result","status","type","name","category","side","symbol","trader",
#    "reduction", ...]                                        -> string
#   ["pnl","price","qty","volume","amount","gpa","credits", ...] -> double
#
# Those words come from the crypto-trading and student demo apps the pipeline
# was first built against. A hospital app's admission_ward, los_days,
# readmit_flag and icd10_code match none of them and silently default to
# string, so numeric KPIs render as text and summarizeBy is set to none.
#
# Typing a column is a judgement about meaning, which is exactly what the
# keyword list was badly approximating. The engine-reported type and any
# sample values are still authoritative where they exist - the model only
# decides the cases the deterministic pass cannot.

COLUMNS_RULES = [
    # ── evidence order ────────────────────────────────────────────────────
    {
        "type": "columns",
        "id": "col1",
        "priority": "critical",
        "rule": "EVIDENCE ORDER — Decide the type from the strongest available evidence, in this order: "
                "(1) the Qlik engine's reported field type, (2) the source database column type when the table "
                "came from a database, (3) sample values, (4) the Qlik number-format string, (5) the field name. "
                "The name is the WEAKEST signal and must never override an explicit type. Never guess from the "
                "name alone when any stronger evidence exists."
    },
    {
        "type": "columns",
        "id": "col2",
        "priority": "critical",
        "rule": "SAMPLE VALUES DECIDE AMBIGUOUS CASES — When sample values are supplied, they outrank the field "
                "name. Values that are all digits with leading zeros (00123), or fixed-width codes, are TEXT even "
                "if the name sounds numeric - converting them to a number destroys the leading zeros. Values with "
                "mixed separators or units are text."
    },
    {
        "type": "columns",
        "id": "col3",
        "priority": "critical",
        "rule": "NO DOMAIN VOCABULARY — Do not rely on a fixed list of business words. Reason about what the field "
                "means in the context of its own table and the app's subject. A field called 'grade' is numeric in "
                "a school app and text in a construction app; 'stage' is text in a sales pipeline and numeric in a "
                "clinical dataset. Use the surrounding columns to decide."
    },

    # ── type selection ────────────────────────────────────────────────────
    {
        "type": "columns",
        "id": "col4",
        "rule": "TARGET TYPES — Emit exactly one of: int64, double, decimal, dateTime, boolean, string. Use int64 "
                "for whole-number counts, years and ranks; double for continuous quantities and ratios; decimal "
                "for currency where exact arithmetic matters; dateTime for any date, time or timestamp; boolean "
                "only for genuine two-state flags; string otherwise."
    },
    {
        "type": "columns",
        "id": "col5",
        "priority": "critical",
        "rule": "IDENTIFIERS ARE NOT MEASURES — A key or identifier (primary key, foreign key, code, reference "
                "number) is never summarised, whatever its storage type. Emit summarize_by='none'. A numeric ID "
                "may stay int64 for join performance, but must never default to Sum - a report that sums customer "
                "IDs looks plausible and is meaningless."
    },
    {
        "type": "columns",
        "id": "col6",
        "priority": "critical",
        "rule": "SUMMARIZE_BY — Additive quantities (amounts, counts, volumes) get 'sum'. Ratios, rates, "
                "percentages, scores, averages, prices per unit and any pre-divided value get 'average' or 'none' "
                "- summing them is mathematically wrong. Dimensions, dates, booleans and text get 'none'."
    },
    {
        "type": "columns",
        "id": "col7",
        "rule": "BOOLEAN DETECTION — Treat a field as boolean only when its values are genuinely two-state "
                "(true/false, 1/0, Y/N, yes/no). A field named *_flag holding more than two distinct values is "
                "string. Qlik has no native boolean, so an explicit -1/0 pair from a Qlik expression is boolean."
    },
    {
        "type": "columns",
        "id": "col8",
        "rule": "DERIVED DATE PARTS — Qlik autoCalendar and date-part fields inherit their parent's type by name "
                "but not by meaning. A *.Year or *Year field is int64; a *.Month, *MonthName or any *Label field "
                "is text used for display and ordering; a *.Date or *.WeekStart field is dateTime. A text label "
                "must not carry the parent's date format string - a stale date format on a string column is "
                "meaningless in the field pane."
    },

    # ── formatting ────────────────────────────────────────────────────────
    {
        "type": "columns",
        "id": "col9",
        "rule": "FORMAT STRINGS — Translate the Qlik number format to a Power BI format string: thousands "
                "'#,##0', two decimals '#,##0.00', percentage '0.00%', currency with the correct symbol "
                "'\\u00a4#,##0.00', dates to the pattern the source used (YYYY-MM-DD, DD/MM/YYYY). Preserve the "
                "source's decimal precision; do not standardise it."
    },
    {
        "type": "columns",
        "id": "col10",
        "rule": "PERCENTAGE SCALE — Establish whether the source stores a percentage as a fraction (0.15) or as a "
                "whole number (15) before choosing the format. Applying '0.00%' to a value already multiplied by "
                "100 renders 1500%. When the scale cannot be determined, use a plain numeric format and flag it."
    },
    {
        "type": "columns",
        "id": "col11",
        "rule": "CURRENCY — Use the currency symbol from the Qlik MoneyFormat reserved variable when present. Do "
                "not assume a currency from the field name or the app's language."
    },

    # ── naming ────────────────────────────────────────────────────────────
    {
        "type": "columns",
        "id": "col12",
        "rule": "NAME SANITISATION — TMDL cannot carry dots or leading/trailing spaces in an unquoted column "
                "identifier, so replace them with underscores. Change nothing else: do not re-case, expand "
                "abbreviations, translate, or 'tidy' a name. Every visual binding and DAX expression refers to the "
                "column by its exact name, and a cosmetic rename silently unbinds them. Record the original in "
                "qlik_column_name."
    },
    {
        "type": "columns",
        "id": "col13",
        "rule": "HIDDEN COLUMNS — Hide surrogate keys and technical join columns that have no analytical meaning, "
                "so the field pane stays usable. Never hide a column that a visual, measure or relationship "
                "references."
    },
    {
        "type": "columns",
        "id": "col14",
        "priority": "critical",
        "rule": "UNCERTAINTY IS REPORTED — When the evidence genuinely does not settle the type, choose string "
                "(the only lossless target) and set requires_review with a note naming the evidence you had. "
                "A wrong numeric type loses data; a string can always be converted later."
    },
]

FABRIC_DATATYPES = ("int64", "double", "decimal", "dateTime", "boolean", "string")
SUMMARIZE_BY = ("sum", "average", "count", "distinctCount", "min", "max", "none")

COLUMNS_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["columns"],
    "properties": {
        "columns": {
            "type": "array",
            "description": "One entry per supplied column, same order, same names.",
            "items": {
                "type": "object",
                "required": ["qlik_column_name", "fabric_datatype", "summarize_by"],
                "properties": {
                    "qlik_column_name": {
                        "type": "string",
                        "description": "Exactly as supplied. Never renamed or re-cased.",
                    },
                    "fabric_column_name": {
                        "type": "string",
                        "description": "Sanitised for TMDL (dots/spaces -> underscore) and nothing else.",
                    },
                    "fabric_datatype": {"type": "string", "enum": list(FABRIC_DATATYPES)},
                    "summarize_by": {"type": "string", "enum": list(SUMMARIZE_BY)},
                    "format_string": {"type": ["string", "null"]},
                    "is_hidden": {"type": "boolean"},
                    "is_key": {
                        "type": "boolean",
                        "description": "Identifier/key column - never summarised (col5).",
                    },
                    "evidence": {
                        "type": "string",
                        "enum": ["engine_type", "source_type", "sample_values",
                                 "format_string", "field_name"],
                        "description": "Strongest evidence actually used (col1).",
                    },
                    "requires_review": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            },
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}
