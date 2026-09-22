# src/converters/mquery/rules.py
# Qlik load script / data connection -> Power Query M (the `partition ... = m`
# block of a TMDL table) conversion rules.
#
# services/connection_mapper.py builds these M expressions with a deterministic
# per-connector template. That is the right default - a connector signature is
# a fact, not a judgement - but it cannot handle the parts of a Qlik LOAD that
# genuinely require interpretation: preceding loads, resident chains, inline
# tables, mapping loads, CROSSTABLE, and embedded SQL dialect differences.
# These rules govern the LLM pass that reviews and repairs the generated M.
#
# The deterministic M is always the baseline. The model corrects it; it never
# writes one from scratch.

MQUERY_RULES = [
    # ── output contract ───────────────────────────────────────────────────
    {
        "type": "mquery",
        "id": "mq1",
        "priority": "critical",
        "rule": "Output a single complete Power Query M expression: 'let' ... 'in' <FinalStep>. Every step must be "
                "referenced or lead to the final step. No markdown, no commentary, no trailing semicolon after the "
                "final step name."
    },
    {
        "type": "mquery",
        "id": "mq2",
        "rule": "STEP NAMES — Use descriptive step names (Source, Navigation, ChangedType, RemovedColumns, "
                "RenamedColumns, FilteredRows). A step name containing a space or a reserved word must be written "
                "in the #\"quoted\" form. Referencing a quoted step without the # prefix is a syntax error."
    },
    {
        "type": "mquery",
        "id": "mq3",
        "priority": "critical",
        "rule": "NEVER INVENT A CONNECTION — Use only the server, database, schema, path or URL supplied in the "
                "connection context. If a required part is missing, keep the placeholder the deterministic draft "
                "used and note it; never substitute a plausible-looking host or database name."
    },

    # ── connector selection ───────────────────────────────────────────────
    {
        "type": "mquery",
        "id": "mq4",
        "rule": "CONNECTOR MAPPING — Map the Qlik connector to its M function: SQL Server -> Sql.Database, "
                "PostgreSQL -> PostgreSQL.Database, MySQL -> MySQL.Database, Oracle -> Oracle.Database, "
                "Snowflake -> Snowflake.Databases, Google BigQuery -> GoogleBigQuery.Database, Databricks -> "
                "Databricks.Catalogs, Amazon Redshift -> AmazonRedshift.Database, REST -> Web.Contents, "
                "CSV/TXT file -> Csv.Document(File.Contents(...)) or Csv.Document(Web.Contents(...)), "
                "Excel -> Excel.Workbook, Parquet -> Parquet.Document. Preserve the connector the draft chose "
                "unless it clearly contradicts the connection metadata."
    },
    {
        "type": "mquery",
        "id": "mq5",
        "priority": "critical",
        "rule": "NATIVE QUERY — When the Qlik table carries embedded SQL, emit it via the connector's Query "
                "option, e.g. Sql.Database(server, db, [Query=\"...\"]), so it folds to the source. Keep the SQL "
                "verbatim apart from dialect fixes required by the target connector. Escape every embedded double "
                "quote by doubling it, and escape a literal # as #(#) inside an M string."
    },
    {
        "type": "mquery",
        "id": "mq6",
        "rule": "FILE SOURCES — A Qlik LOAD FROM a local or server path (lib://, .qvd, .csv, .xlsx) has no "
                "equivalent path in Fabric. Point it at the staged storage URL supplied in the context using "
                "Web.Contents, and preserve the original path in a comment so the lineage is recoverable. Never "
                "emit a C:\\ or lib:// path into M - it will not resolve in the service."
    },
    {
        "type": "mquery",
        "id": "mq7",
        "rule": "QVD FILES — A QVD has no Power Query reader. The table must come from the QVD's own upstream "
                "source when that is known, otherwise from the staged export of the QVD. State which was used in "
                "the note; never pretend a QVD can be read directly."
    },

    # ── Qlik load constructs ──────────────────────────────────────────────
    {
        "type": "mquery",
        "id": "mq8",
        "priority": "critical",
        "rule": "RESIDENT LOAD — 'LOAD ... RESIDENT OtherTable' becomes a reference to that table's query: the "
                "Source step is #\"Other Table\" (the M identifier of the upstream query), not a new connection. "
                "Preserve the WHERE clause as Table.SelectRows and the field list as Table.SelectColumns."
    },
    {
        "type": "mquery",
        "id": "mq9",
        "priority": "critical",
        "rule": "PRECEDING LOAD — A Qlik preceding load (a LOAD directly above another LOAD/SELECT with no FROM) "
                "executes bottom-up: the lower statement is the source and the upper one transforms its result. "
                "Emit the lower statement as the Source step and the upper statement's expressions as subsequent "
                "Table.AddColumn / Table.SelectColumns steps. Reversing this order silently produces the wrong "
                "columns."
    },
    {
        "type": "mquery",
        "id": "mq10",
        "rule": "INLINE TABLES — 'LOAD * INLINE [ ... ]' becomes #table(type table [Col = type text, ...], "
                "{{row}, {row}}) with the header row supplying column names. Preserve the original row order and "
                "every value exactly; do not re-type values silently, add an explicit Table.TransformColumnTypes "
                "step instead."
    },
    {
        "type": "mquery",
        "id": "mq11",
        "rule": "AUTOGENERATE — 'LOAD ... AUTOGENERATE n' becomes List.Numbers/List.Generate converted with "
                "Table.FromList. A Qlik master-calendar autogenerate should become a proper date table: "
                "List.Dates(startDate, count, #duration(1,0,0,0)) plus added year/month/quarter columns."
    },
    {
        "type": "mquery",
        "id": "mq12",
        "priority": "critical",
        "rule": "MAPPING LOAD / APPLYMAP — A MAPPING LOAD builds a two-column lookup and is not a model table: "
                "mark it excluded from the model. Each ApplyMap('Map', key, default) in a consuming load becomes a "
                "Table.NestedJoin against that query followed by Table.ExpandTableColumn, with the default value "
                "supplied by a Table.ReplaceValue or an if-null guard."
    },
    {
        "type": "mquery",
        "id": "mq13",
        "rule": "CONCATENATE / NOCONCATENATE — An explicit or implicit CONCATENATE becomes Table.Combine of the "
                "contributing queries. NOCONCATENATE means the tables stay separate; do not merge them."
    },
    {
        "type": "mquery",
        "id": "mq14",
        "rule": "CROSSTABLE — 'CROSSTABLE(attribute, value, n) LOAD' unpivots: keep the first n columns as "
                "identifiers and emit Table.UnpivotOtherColumns(source, {first n columns}, \"attribute\", "
                "\"value\") using the exact attribute and value names from the CROSSTABLE prefix."
    },
    {
        "type": "mquery",
        "id": "mq15",
        "rule": "JOIN / KEEP — Qlik JOIN becomes Table.NestedJoin + Table.ExpandTableColumn with the matching "
                "JoinKind (LeftOuter, Inner, FullOuter, RightOuter). LEFT KEEP / RIGHT KEEP filter rather than "
                "widen: emit a semi-join, Table.SelectRows against the key list, and do not expand the other "
                "table's columns."
    },
    {
        "type": "mquery",
        "id": "mq16",
        "rule": "WHERE EXISTS — 'WHERE Exists(Field)' filters to values already loaded for that field. Emit it as "
                "a semi-join against the query that supplies the field, not as a literal Exists call - M has no "
                "such function."
    },

    # ── expressions inside the load ───────────────────────────────────────
    {
        "type": "mquery",
        "id": "mq17",
        "rule": "FIELD ALIASES — 'expr AS FieldName' becomes a Table.AddColumn named FieldName, or a "
                "Table.RenameColumns when the expression is a bare field. Preserve the alias spelling exactly; it "
                "is the name every downstream visual and measure binds to."
    },
    {
        "type": "mquery",
        "id": "mq18",
        "rule": "M FUNCTION MAPPING — Qlik-to-M scalar equivalents: If -> if/then/else, "
                "Date#/Num# -> Date.FromText/Number.FromText, Date() -> Date.ToText with a format, "
                "Today() -> Date.From(DateTime.LocalNow()), Now() -> DateTime.LocalNow(), "
                "Year/Month/Day -> Date.Year/Date.Month/Date.Day, Left/Right/Mid -> Text.Start/Text.End/"
                "Text.Middle, Len -> Text.Length, Upper/Lower -> Text.Upper/Text.Lower, Trim -> Text.Trim, "
                "Replace -> Text.Replace, SubField -> Text.Split with an index, "
                "IsNull/Len(x)=0 -> x = null, Alt(a,b) -> if a <> null then a else b. M is case-sensitive: "
                "Text.Upper is valid, TEXT.UPPER is not."
    },
    {
        "type": "mquery",
        "id": "mq19",
        "rule": "NULL SEMANTICS — Qlik treats null and empty string as distinct; M's null propagates through "
                "arithmetic. Guard any expression that can receive a null in a comparison or a division, and never "
                "translate a Qlik Alt()/Coalesce chain into a bare reference."
    },
    {
        "type": "mquery",
        "id": "mq20",
        "priority": "critical",
        "rule": "TYPES ARE EXPLICIT — End every query with a Table.TransformColumnTypes covering every column, "
                "using the resolved Fabric datatype for each: Int64.Type, type number, type date, type datetime, "
                "type logical, type text. An untyped column arrives as 'any' and cannot be aggregated or related."
    },
    {
        "type": "mquery",
        "id": "mq21",
        "rule": "QUERY FOLDING — Keep filters and column removals as early as possible and prefer native-foldable "
                "operations, so the work executes at the source. Do not introduce Table.Buffer; it defeats folding "
                "and is almost never what a migrated load needs."
    },
    {
        "type": "mquery",
        "id": "mq22",
        "rule": "SECTION ACCESS — Qlik section access is row-level security, not a load step. Do not emit it as M "
                "filtering. Report it so it becomes a TMDL role with a DAX filter expression, and note that the "
                "reduction field must be mapped to the target's identity function."
    },
    {
        "type": "mquery",
        "id": "mq23",
        "priority": "critical",
        "rule": "NO SILENT DATA LOSS — If a construct genuinely has no M equivalent, keep the table loading with "
                "the columns you can produce, and record the gap in the note with the exact Qlik statement. Never "
                "drop rows or columns to make a query valid, and never emit an empty #table as a placeholder for "
                "real data."
    },
    {
        "type": "mquery",
        "id": "mq24",
        "priority": "high",
        "rule": "TIME TRAVEL & HISTORICAL SNAPSHOTS — When querying Delta Lake, Snowflake, BigQuery or Fabric "
                "Lakehouse tables with historical versioning or time travel clauses (e.g. AT TIMESTAMP, AS OF "
                "VERSION, TIMESTAMP_AS_OF), pass the historical query via Value.NativeQuery or connector Query "
                "options: Value.NativeQuery(Source, \"SELECT * FROM Table AT(TIMESTAMP => '...')\", null, [EnableFolding=true]) "
                "so time-travel queries fold natively into the cloud data platform."
    },
    {
        "type": "mquery",
        "id": "mq25",
        "rule": "INCREMENTAL REFRESH & PARTITION RANGE FILTERS — When filtering large date-partitioned tables, "
                "apply date range filtering using Table.SelectRows with standard Fabric parameters (RangeStart and "
                "RangeEnd): Table.SelectRows(Source, each [DateColumn] >= RangeStart and [DateColumn] < RangeEnd). "
                "Ensure date comparisons are against native datetime/date types for optimal query folding."
    },
    {
        "type": "mquery",
        "id": "mq26",
        "priority": "high",
        "rule": "CLOUD CSV & STORAGE ENCODING — When loading CSV or flat files from cloud storage (OneLake / Web.Contents), "
                "always configure explicit delimiter, UTF-8 encoding (65001), and header promotion: "
                "Csv.Document(Web.Contents(Url), [Delimiter=\",\", Columns=N, Encoding=65001, QuoteStyle=QuoteStyle.None]) "
                "followed by Table.PromoteHeaders(Source, [PromoteAllScalars=true]) and Table.TransformColumnTypes."
    },
]
