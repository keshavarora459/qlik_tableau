import os
import re
from typing import Any, Dict, List, Optional, Tuple

# Any identifier interpolated into generated M text must be wrapped in
# #"..." once it contains anything besides letters/digits/underscore -
# Qlik table/column names routinely carry spaces and hyphens.
_UNSAFE_M_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$")

# Best-effort extraction of an embedded SQL statement out of a Qlik LOAD
# script (`qlik_query`/`load_statement`). Real Qlik apps commonly load via
# `LOAD ... ; SQL SELECT ...` or embed the raw SELECT directly - either way,
# the SELECT text itself is what a NativeQuery M call needs.
_EMBEDDED_SQL = re.compile(r"SELECT\s+.+", re.IGNORECASE | re.DOTALL)
_FILE_LOAD = re.compile(r"FROM\s+\[?(?:lib://)?([^/\]\r\n]+)/([^\]\r\n]+)\]?", re.IGNORECASE)
_FILE_LOAD_SIMPLE = re.compile(r"FROM\s+\[?([^\]\r\n]+\.(?:csv|xlsx|xls|txt|qvd))\]?", re.IGNORECASE)


def escape_m_identifier(name: str) -> str:
    """Wrap `name` in #"..." unless it's already a safe bare M identifier."""
    if not name:
        return name
    return name if _UNSAFE_M_IDENTIFIER.match(name) else f'#"{name}"'


def escape_m_string(value: str) -> str:
    """Escape a value for embedding inside an M "..." string literal.

    M escapes an embedded double-quote by doubling it. SQL text routinely
    carries double-quoted identifiers (`SELECT "COURSE_ID" FROM ...`), which
    would otherwise terminate the M string literal early and produce invalid
    M (this is exactly what real Snowflake-quoted-identifier SQL looks like).
    """
    return (value or "").replace('"', '""')


# Fabric column datatype -> M type literal, for Table.TransformColumnTypes.
_M_TYPE_BY_FABRIC_TYPE = {
    "double": "type number",
    "int64": "Int64.Type",
    "dateTime": "type datetime",
    "boolean": "type logical",
}


def type_transforms_from_columns(columns: Optional[List[Dict[str, Any]]], tracker: Optional['MQuerySchemaTracker'] = None) -> str:
    """Build a `{"Col", type X}, ...` list from the table's already-resolved
    Fabric column types, for a `Table.TransformColumnTypes` step.

    Reusing the real per-column types (rather than a hardcoded guess) is what
    makes this correct for whatever table it's actually building - see the
    INLINE/REST/CSV/Excel branches in build_table_mquery.
    """
    parts = []
    for column in columns or []:
        name = column.get("fabric_column_name") or column.get("qlik_column_name") or column.get("name")
        if not name:
            continue
        
        if tracker:
            resolved_name = tracker.resolve_target_name(name)
            if not resolved_name:
                continue # Skip columns that don't exist in the current schema
            name = resolved_name
            
        m_type = _M_TYPE_BY_FABRIC_TYPE.get(column.get("fabric_datatype"), "type text")
        parts.append(f'{{"{name}", {m_type}}}')
    return ", ".join(parts)


import json

class MQuerySchemaReferenceError(Exception):
    def __init__(self, message: str, step: str, column: str, available: list, previous_step: str = None):
        super().__init__(message)
        self.step = step
        self.column = column
        self.available_columns = available
        self.previous_step = previous_step

    def to_dict(self):
        return {
            "error": "M_QUERY_SCHEMA_REFERENCE_ERROR",
            "step": self.step,
            "column": self.column,
            "available_columns": self.available_columns,
            "previous_step": self.previous_step,
            "reason": str(self)
        }


class MQuerySchemaTracker:
    def __init__(self, initial_columns: List[str]):
        # Keep original casing for M output, but do case-insensitive comparisons
        self.active_columns = list(initial_columns) if initial_columns else []

    def apply_rename(self, renames: List[Tuple[str, str]], step_name: str, previous_step_name: str = None) -> None:
        """Apply a list of (old, new) renames to the schema. Throws error if old not found."""
        for old_col, new_col in renames:
            found = False
            for i, active_col in enumerate(self.active_columns):
                if active_col.lower() == old_col.lower():
                    self.active_columns[i] = new_col
                    found = True
                    break
            if not found:
                raise MQuerySchemaReferenceError(
                    f"Column '{old_col}' does not exist in the schema produced by the previous step.",
                    step=step_name,
                    column=old_col,
                    available=list(self.active_columns),
                    previous_step=previous_step_name
                )

    def apply_select(self, columns: List[str], step_name: str, previous_step_name: str = None) -> None:
        """Filter schema to only include specified columns."""
        new_active = []
        for col in columns:
            found = False
            for active_col in self.active_columns:
                if active_col.lower() == col.lower():
                    new_active.append(active_col)
                    found = True
                    break
            if not found:
                raise MQuerySchemaReferenceError(
                    f"Column '{col}' does not exist in the schema produced by the previous step.",
                    step=step_name,
                    column=col,
                    available=list(self.active_columns),
                    previous_step=previous_step_name
                )
        self.active_columns = new_active

    def apply_remove(self, columns: List[str], step_name: str, previous_step_name: str = None) -> None:
        """Remove specified columns from the schema."""
        to_remove_lower = {c.lower() for c in columns}
        for col in columns:
            if not any(c.lower() == col.lower() for c in self.active_columns):
                 raise MQuerySchemaReferenceError(
                    f"Column '{col}' does not exist in the schema produced by the previous step.",
                    step=step_name,
                    column=col,
                    available=list(self.active_columns),
                    previous_step=previous_step_name
                )
        self.active_columns = [c for c in self.active_columns if c.lower() not in to_remove_lower]

    def resolve_target_name(self, target_name: str) -> Optional[str]:
        """Find the currently active name for a given target column. 
        It tries an exact match, then case-insensitive, then ignores non-alphanumeric chars."""
        if not target_name:
            return None
            
        # 1. Exact match
        if target_name in self.active_columns:
            return target_name
        
        # 2. Case insensitive match
        target_lower = target_name.lower()
        for active_col in self.active_columns:
            if active_col.lower() == target_lower:
                return active_col
                
        # 3. Stripped alphanumeric match (ignoring dots, spaces, underscores)
        def _strip(s: str) -> str:
            return re.sub(r'[^a-z0-9]', '', s.lower())
            
        target_stripped = _strip(target_name)
        if not target_stripped:
            return None
            
        for active_col in self.active_columns:
            if _strip(active_col) == target_stripped:
                return active_col
                
        return None


class LoadType:
    SOURCE = "source"                 # external DB read -> NativeQuery M
    RESIDENT = "resident"             # loaded from another table in memory
    TRANSFORMATION = "transformation" # AUTOGENERATE -> DAX or M calculated table
    INLINE = "inline"                 # INLINE LOAD -> #table(...) M
    DERIVED = "derived"               # no resolvable source


_LOAD_PATTERNS = [
    (re.compile(r"\bResident\s+\w+", re.IGNORECASE), LoadType.RESIDENT),
    (re.compile(r"\bINLINE\s*\[", re.IGNORECASE), LoadType.INLINE),
    (re.compile(r"\bAUTOGENERATE\b", re.IGNORECASE), LoadType.TRANSFORMATION),
    (re.compile(r"\bSQL\s+SELECT\b", re.IGNORECASE), LoadType.SOURCE),
    (re.compile(r"\bFROM\s+\[?lib://", re.IGNORECASE), LoadType.SOURCE),
]


def classify_load_type(table: dict) -> str:
    """
    Classify how a Qlik table is loaded based on script patterns or metadata.
    """
    qlik_query = table.get("qlik_query") or table.get("load_statement") or ""
    source_type = str(table.get("sourceType") or "").lower()

    if source_type in ("resident", "inline", "transformation"):
        return {
            "resident": LoadType.RESIDENT,
            "inline": LoadType.INLINE,
            "transformation": LoadType.TRANSFORMATION
        }[source_type]

    for pattern, ltype in _LOAD_PATTERNS:
        if pattern.search(qlik_query):
            return ltype

    return LoadType.SOURCE


def make_safe_m_var(name: str) -> str:
    """
    Ensure M step variable name starts with a letter or underscore,
    and contains only alphanumeric characters and underscores.
    """
    cleaned = re.sub(r'[^A-Za-z0-9_]', '_', str(name or "")).strip('_')
    if not cleaned or not re.match(r'^[A-Za-z_]', cleaned):
        return f"raw_{cleaned}" if cleaned else "raw_table"
    return cleaned


def build_rename_step(prev_step: str, pairs: list) -> str:
    """
    Build Table.RenameColumns step for column mapping.
    pairs: list of (old_col_name, new_col_name) tuples
    """
    if not pairs:
        return prev_step
    renames = ", ".join(
        f'{{"{escape_m_string(old)}", "{escape_m_string(new)}"}}'
        for old, new in pairs
    )
    return f"Table.RenameColumns({prev_step}, {{{renames}}})"


def build_type_step(prev_step: str, columns: list, tracker: Optional['MQuerySchemaTracker'] = None) -> str:
    """
    Build Table.TransformColumnTypes step using schema column types.
    """
    transforms = type_transforms_from_columns(columns, tracker)
    if not transforms:
        return prev_step
    return f"Table.TransformColumnTypes({prev_step}, {{{transforms}}})"


def build_select_columns_step(prev_step: str, column_names: list) -> str:
    """
    Build Table.SelectColumns step to keep only the active mapped columns.
    """
    if not column_names:
        return prev_step
    cols = ", ".join(f'"{escape_m_string(c)}"' for c in column_names)
    return f"Table.SelectColumns({prev_step}, {{{cols}}})"


def build_inline_table_mquery(rows: list, columns: list) -> str:
    """
    Build an M `#table(...)` expression for Qlik INLINE loads.
    """
    col_names = ", ".join(f'"{escape_m_string(c)}"' for c in columns)
    row_strings = []
    for row in rows:
        vals = ", ".join(f'"{escape_m_string(v)}"' if isinstance(v, str) else str(v) for v in row)
        row_strings.append(f"{{{vals}}}")
    all_rows = ", ".join(row_strings)
    return f'#table({{{col_names}}}, {{{all_rows}}})'


def extract_embedded_sql(text: Optional[str]) -> Optional[str]:
    """Pull a `SELECT ...` statement out of a Qlik load script, if present."""
    if not text:
        return None
    match = _EMBEDDED_SQL.search(text)
    if not match:
        return None
    return match.group(0).strip().rstrip(";").strip()


def clean_native_sql(query: str) -> str:
    text = str(query or "").strip()
    if not text:
        return ""
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    text = re.sub(r"(?m)^\s*(?://|--).*$", "", text)
    text = re.sub(r'""([^"]+)""', r'"\1"', text)
    text = re.sub(r'""\s*\.\s*', '', text)
    text = re.sub(r'\s*\.\s*""', '', text)
    select_match = re.search(r"\bSELECT\b", text, re.IGNORECASE)
    if select_match:
        text = text[select_match.start() :]
    else:
        text = re.sub(r"^\s*(?:#?\"[^\"]+\"|\[[^\]]+\]|[A-Za-z_][\w]*)\s*:\s*", "", text)
        text = re.sub(r"^\s*SQL\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?m)\s*(?://|--).*$", "", text)
    in_single_quote = False
    in_double_quote = False
    first_stmt_end = len(text)
    for i, ch in enumerate(text):
        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        elif ch == ';' and not in_single_quote and not in_double_quote:
            rest = text[i + 1 :].strip()
            if rest:
                if re.match(r"^\s*(?:LOAD|RESIDENT|AUTOGENERATED|SELECT)\b", rest, re.IGNORECASE) or "LOAD" in rest.upper():
                    first_stmt_end = i
                    break
            else:
                first_stmt_end = i
    text = text[:first_stmt_end].strip()
    lines = [line.strip() for line in text.strip().rstrip(";").splitlines() if line.strip()]
    single_line_sql = " ".join(lines)
    single_line_sql = re.sub(r"\s+", " ", single_line_sql).strip().rstrip(";")
    _SQL_KEYWORDS = {
        "where", "join", "left", "right", "inner", "outer", "full", "cross",
        "group", "order", "having", "limit", "offset", "union", "intersect", "except",
        "on", "as", "set", "values", "into", "from", "select", "with",
    }
    single_line_sql = re.sub(
        r'(FROM\s+(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w]*)\s*(?:\.\s*(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w]*))*'
        r'\s+)([A-Za-z_][A-Za-z0-9_]*)(?=\s*$|\s*;)',
        lambda m: m.group(1) if m.group(2).lower() not in _SQL_KEYWORDS else m.group(0),
        single_line_sql,
        flags=re.IGNORECASE,
    )
    return single_line_sql.strip().rstrip(";")

_SQL_TABLE_REF_PATTERN = re.compile(
    r'\b(?:FROM|JOIN)\s+((?:(?:"[^"]+"|\[[^\]]+\]|`[^`]+`|[a-zA-Z0-9_#$]+)(?:\s*\.\s*(?:"[^"]+"|\[[^\]]+\]|`[^`]+`|[a-zA-Z0-9_#$]+))*))',
    re.IGNORECASE
)


def extract_sql_object_identifiers(sql: Optional[str]) -> Dict[str, Optional[str]]:
    """Extract database/catalog, schema, and table name from a SQL FROM / JOIN clause.
    Supports single/double/triple quotes, square brackets, and backticks in 1-part, 2-part, and 3-part references.
    """
    if not sql or not isinstance(sql, str):
        return {"database": None, "schema": None, "table": None}
    normalized_sql = re.sub(r'"{2,}', '"', sql)
    m = _SQL_TABLE_REF_PATTERN.search(normalized_sql)
    if not m:
        return {"database": None, "schema": None, "table": None}
    full_ref = m.group(1).strip()
    parts = re.findall(r'"[^"]+"|\[[^\]]+\]|`[^`]+`|[a-zA-Z0-9_#$]+', full_ref)
    clean_parts = []
    for p in parts:
        clean = p.strip()
        if clean.startswith("[") and clean.endswith("]"):
            clean = clean[1:-1]
        clean = clean.strip('"\'` ')
        if clean:
            clean_parts.append(clean)
    if len(clean_parts) >= 3:
        return {"database": clean_parts[0], "schema": clean_parts[1], "table": clean_parts[2]}
    elif len(clean_parts) == 2:
        return {"database": None, "schema": clean_parts[0], "table": clean_parts[1]}
    elif len(clean_parts) == 1:
        return {"database": None, "schema": None, "table": clean_parts[0]}
    return {"database": None, "schema": None, "table": None}


def resolve_database_name(
    conn: Optional[Dict[str, Any]],
    custom_sql: Optional[str] = None,
    qlik_query: Optional[str] = None
) -> Optional[str]:
    """Dynamically resolve the database/catalog name from source connection metadata or SQL."""
    if not isinstance(conn, dict):
        conn = {}
    db = (
        conn.get("database") or conn.get("db") or conn.get("catalog")
        or conn.get("initial_catalog") or conn.get("initialCatalog")
        or conn.get("dbname") or conn.get("database_name")
    )
    if not db and isinstance(conn.get("connections"), list) and conn["connections"]:
        first = conn["connections"][0]
        if isinstance(first, dict):
            db = first.get("database") or first.get("db") or first.get("catalog") or first.get("initial_catalog")
    if not db:
        conn_str = str(conn.get("connection_string") or conn.get("connect_string") or conn.get("conn_str") or conn.get("connectionString") or "")
        db_match = re.search(r"(?:Database|Initial\s*Catalog|DB|Catalog)\s*=\s*([^;]+)", conn_str, re.IGNORECASE)
        if db_match:
            db = db_match.group(1).strip()
    if not db:
        sql_ids = extract_sql_object_identifiers(custom_sql or (extract_embedded_sql(qlik_query) if qlik_query else None) or qlik_query)
        if sql_ids.get("database"):
            db = sql_ids["database"]
    if not db:
        env_db = os.getenv("DEFAULT_DB_NAME", "").strip()
        if env_db:
            db = env_db
    return db or None


def resolve_server_name(
    conn: Optional[Dict[str, Any]],
    custom_sql: Optional[str] = None,
    qlik_query: Optional[str] = None
) -> Optional[str]:
    """Dynamically resolve the server/host from source connection metadata."""
    if not isinstance(conn, dict):
        conn = {}
    server = (
        conn.get("server") or conn.get("host") or conn.get("server_name")
        or conn.get("hostname") or conn.get("endpoint") or conn.get("data_source")
        or conn.get("dataSource") or conn.get("address")
    )
    if not server and isinstance(conn.get("connections"), list) and conn["connections"]:
        first = conn["connections"][0]
        if isinstance(first, dict):
            server = first.get("server") or first.get("host") or first.get("server_name")
    if not server:
        conn_str = str(conn.get("connection_string") or conn.get("connect_string") or conn.get("conn_str") or conn.get("connectionString") or "")
        server_match = re.search(r"(?:Server|Host|Data\s*Source|Address)\s*=\s*([^;]+)", conn_str, re.IGNORECASE)
        if server_match:
            server = server_match.group(1).strip()
    if not server:
        env_server = os.getenv("DEFAULT_DB_SERVER", "").strip()
        if env_server:
            server = env_server
    if not server and conn.get("name"):
        driver = (conn.get("driver") or conn.get("connector_type") or "").lower()
        connector = (conn.get("source_connector") or conn.get("connector_type") or "").lower()
        if not any(kw in (driver + " " + connector) for kw in ["unknown", "file", "folder", "datafile"]):
            server = conn.get("name")
    return server or None


def validate_m_query(m_query: Optional[str]) -> Dict[str, Any]:
    """Generic validation of generated Power Query M expressions.
    
    Verifies that:
    1. Query does not contain unresolved placeholders (<DATABASE>, <SERVER>, <HOST>, <SCHEMA>, <TABLE>, undefined, null).
    2. Connector calls (specifically Sql.Database) have server and database resolved, non-empty, and non-placeholder.
    """
    if not m_query or not isinstance(m_query, str) or not m_query.strip():
        return {
            "passed": False,
            "errors": ["Database/catalog could not be resolved from source connection metadata"]
        }
    
    errors: List[str] = []
    
    if "// REVIEW_REQUIRED: Database/catalog could not be resolved" in m_query:
        errors.append("Database/catalog could not be resolved from source connection metadata")
    if "// REVIEW_REQUIRED: Server/host could not be resolved" in m_query:
        errors.append("Server/host could not be resolved from source connection metadata")
    if "// REVIEW_REQUIRED: Unresolved" in m_query:
        errors.append("Unresolved source connection in M query")
        
    for ph in ["<DATABASE>", "<SERVER>", "<HOST>", "<SCHEMA>", "<TABLE>"]:
        if ph in m_query:
            errors.append(f"Generated M query contains unresolved {ph} placeholder")
            
    connector_call_pattern = re.compile(
        r'(Sql\.Database|PostgreSQL\.Database|MySQL\.Database|AzureSynapse\.Database|AmazonRedshift\.Database|Snowflake\.Databases)\s*\(([^)]*)\)',
        re.IGNORECASE
    )
    for match in connector_call_pattern.finditer(m_query):
        fn_name = match.group(1)
        args_str = match.group(2)
        args = []
        cur = []
        in_q = False
        for ch in args_str:
            if ch == '"':
                in_q = not in_q
                cur.append(ch)
            elif ch == ',' and not in_q:
                args.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        if cur:
            args.append("".join(cur).strip())
            
        if fn_name.lower() == "sql.database":
            server_arg = args[0] if len(args) > 0 else ""
            db_arg = args[1] if len(args) > 1 else ""
            
            clean_s = server_arg.strip('"\' ')
            clean_d = db_arg.strip('"\' ')
            
            if not clean_s or clean_s in ("null", "undefined", "<SERVER>", "<HOST>", "<DATABASE>", "UnknownServer"):
                errors.append("Sql.Database() server parameter is unresolved, null, empty, or a placeholder")
            if not clean_d or clean_d in ("null", "undefined", "<DATABASE>", "<SERVER>", "<SCHEMA>"):
                errors.append("Sql.Database() database parameter is unresolved, null, empty, or a placeholder")
        else:
            for i, arg in enumerate(args[:2]):
                clean_a = arg.strip('"\' ')
                if clean_a in ("null", "undefined", "<DATABASE>", "<SERVER>", "<HOST>"):
                    errors.append(f"{fn_name}() parameter {i+1} is unresolved or a placeholder")
                    
    return {
        "passed": len(errors) == 0,
        "errors": errors
    }


def _default_table_query(source_expr: str, sql: str) -> str:
    return (
        f'let\n    Source = {source_expr},\n'
        f'    Result = Value.NativeQuery(Source, "{sql}", null, [EnableFolding=false])\n'
        f'in\n    Result'
    )


def _snowflake_table_query(server: str, warehouse: str, db: str, sql: str) -> str:
    return (
        f'let\n    Source = Snowflake.Databases("{escape_m_string(server)}", "{escape_m_string(warehouse)}"),\n'
        f'    Db = Source{{[Name="{escape_m_string(db)}",Kind="Database"]}}[Data],\n'
        f'    Result = Value.NativeQuery(Db, "{sql}", null, [EnableFolding=false])\n'
        f'in\n    Result'
    )


def _bigquery_table_query(project: str, sql: str) -> str:
    esc_proj = escape_m_string(project or "")
    proj_clause = f'[BillingProject="{esc_proj}"]' if project else ''
    if project:
        return (
            f'let\n    Source = GoogleBigQuery.Database({proj_clause}),\n'
            f'    Db = Source{{[Name="{esc_proj}",Kind="Database"]}}[Data],\n'
            f'    Result = Value.NativeQuery(Db, "{sql}", null, [EnableFolding=false])\n'
            f'in\n    Result'
        )
    return (
        f'let\n    Source = GoogleBigQuery.Database({proj_clause}),\n'
        f'    Result = Value.NativeQuery(Source, "{sql}", null, [EnableFolding=false])\n'
        f'in\n    Result'
    )


class _ConnectorSpec:
    """One database driver: how to build its connection-level M expression
    and its per-table Value.NativeQuery M expression."""

    def __init__(self, key: str, keywords: Tuple[str, ...], score: float,
                 connection_expr, table_query):
        self.key = key
        self.keywords = keywords
        self.score = score
        self.connection_expr = connection_expr  # (server, port, db, wh, path, proj) -> (m_func, m_expr)
        self.table_query = table_query           # (server, port, db, wh, path, sql, proj) -> m_query text

    def matches(self, driver: str, connector: str) -> bool:
        combined = f"{driver} {connector}".lower()
        return any(kw in combined for kw in self.keywords)


# Order matters: more specific keywords must be checked before generic ones
_CONNECTOR_SPECS: List[_ConnectorSpec] = [
    _ConnectorSpec(
        "redshift", ("redshift", "amazonredshift", "amazon_redshift", "amazon redshift", "qix-redshift"), 0.97,
        connection_expr=lambda server, port, db, wh, path, proj=None: (
            "AmazonRedshift.Database",
            f'AmazonRedshift.Database("{escape_m_string(server.split(":")[0] if ":" in (server or "") else (server or ""))}:{port or 5439}", "{escape_m_string(db or "dev")}")',
        ),
        table_query=lambda server, port, db, wh, path, sql, proj=None: _default_table_query(
            f'AmazonRedshift.Database("{escape_m_string(server.split(":")[0] if ":" in (server or "") else (server or ""))}:{port or 5439}", "{escape_m_string(db or "dev")}")', sql
        ),
    ),
    _ConnectorSpec(
        "snowflake", ("snowflake", "qix-snowflake"), 0.95,
        connection_expr=lambda server, port, db, wh, path, proj=None: (
            "Snowflake.Databases",
            f'Snowflake.Databases("{escape_m_string(server)}", "{escape_m_string(wh or "COMPUTE_WH")}")',
        ),
        table_query=lambda server, port, db, wh, path, sql, proj=None: _snowflake_table_query(server, wh or "COMPUTE_WH", db or "dev", sql),
    ),
    _ConnectorSpec(
        "bigquery", ("bigquery", "google_bigquery", "gbq", "googlebigquery", "google-bigquery", "google bigquery", "qix-gbq"), 0.95,
        connection_expr=lambda server, port, db, wh, path, proj=None: (
            "GoogleBigQuery.Database",
            (f'GoogleBigQuery.Database([BillingProject="{escape_m_string(proj)}"]' + ')') if proj else 'GoogleBigQuery.Database()',
        ),
        table_query=lambda server, port, db, wh, path, sql, proj=None: _bigquery_table_query(proj, sql),
    ),
    _ConnectorSpec(
        "postgres", ("postgres", "postgresql", "qix-postgres", "qix-postgresql"), 0.93,
        connection_expr=lambda server, port, db, wh, path, proj=None: (
            "PostgreSQL.Database",
            f'PostgreSQL.Database("{escape_m_string(server)}", "{escape_m_string(db or "postgres")}")',
        ),
        table_query=lambda server, port, db, wh, path, sql, proj=None: _default_table_query(
            f'PostgreSQL.Database("{escape_m_string(server)}", "{escape_m_string(db or "postgres")}")', sql
        ),
    ),
    _ConnectorSpec(
        "mysql", ("mysql", "mariadb", "qix-mysql"), 0.93,
        connection_expr=lambda server, port, db, wh, path, proj=None: (
            "MySQL.Database",
            f'MySQL.Database("{escape_m_string(server)}", "{escape_m_string(db)}")',
        ),
        table_query=lambda server, port, db, wh, path, sql, proj=None: _default_table_query(
            f'MySQL.Database("{escape_m_string(server)}", "{escape_m_string(db)}")', sql
        ),
    ),
    _ConnectorSpec(
        "oracle", ("oracle", "qix-oracle"), 0.90,
        connection_expr=lambda server, port, db, wh, path, proj=None: (
            "Oracle.Database",
            f'Oracle.Database("{escape_m_string(server)}")',
        ),
        table_query=lambda server, port, db, wh, path, sql, proj=None: _default_table_query(
            f'Oracle.Database("{escape_m_string(server)}")', sql
        ),
    ),
    _ConnectorSpec(
        "databricks", ("databricks", "spark", "qix-databricks"), 0.93,
        connection_expr=lambda server, port, db, wh, path, proj=None: (
            "Databricks.Catalogs",
            f'Databricks.Catalogs("{escape_m_string(server)}", "{escape_m_string(path or "")}")',
        ),
        table_query=lambda server, port, db, wh, path, sql, proj=None: _default_table_query(
            f'Databricks.Catalogs("{escape_m_string(server)}", "{escape_m_string(path or "")}")', sql
        ),
    ),
    _ConnectorSpec(
        "teradata", ("teradata", "qix-teradata"), 0.90,
        connection_expr=lambda server, port, db, wh, path, proj=None: (
            "Teradata.Database",
            f'Teradata.Database("{escape_m_string(server)}")',
        ),
        table_query=lambda server, port, db, wh, path, sql, proj=None: _default_table_query(
            f'Teradata.Database("{escape_m_string(server)}")', sql
        ),
    ),
    _ConnectorSpec(
        "hana", ("hana", "saphana", "sap_hana", "sap hana"), 0.90,
        connection_expr=lambda server, port, db, wh, path, proj=None: (
            "SapHana.Database",
            f'SapHana.Database("{escape_m_string(server)}")',
        ),
        table_query=lambda server, port, db, wh, path, sql, proj=None: _default_table_query(
            f'SapHana.Database("{escape_m_string(server)}")', sql
        ),
    ),
    _ConnectorSpec(
        "synapse", ("synapse", "azure_synapse", "azuresynapse"), 0.95,
        connection_expr=lambda server, port, db, wh, path, proj=None: (
            "AzureSynapse.Database",
            f'AzureSynapse.Database("{escape_m_string(server)}", "{escape_m_string(db)}")',
        ),
        table_query=lambda server, port, db, wh, path, sql, proj=None: _default_table_query(
            f'AzureSynapse.Database("{escape_m_string(server)}", "{escape_m_string(db)}")', sql
        ),
    ),
    _ConnectorSpec(
        "sqlserver", ("sqlserver", "mssql", "sql", "azure_sql", "azuresql", "qix-sqlserver"), 0.95,
        connection_expr=lambda server, port, db, wh, path, proj=None: (
            "Sql.Database",
            f'Sql.Database("{escape_m_string(server)}", "{escape_m_string(db)}")',
        ),
        table_query=lambda server, port, db, wh, path, sql, proj=None: _default_table_query(
            f'Sql.Database("{escape_m_string(server)}", "{escape_m_string(db)}")', sql
        ),
    ),
]


class ConnectionMapper:
    """Translates Qlik connection dictionaries and table metadata into Fabric
    Power Query M source expressions and table load queries."""

    def __init__(self):
        self._specs = _CONNECTOR_SPECS

    def _find_spec(self, driver: str, connector: str) -> Optional[_ConnectorSpec]:
        for spec in self._specs:
            if spec.matches(driver, connector):
                return spec
        return None

    def map_connections(self, raw_connections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        mapped = []
        for c in raw_connections:
            if not isinstance(c, dict):
                continue
            name = c.get("name") or c.get("lib_name") or "Connection"
            driver = (c.get("driver") or c.get("connector_type") or "").lower()
            connector = (c.get("source_connector") or c.get("connector_type") or "").lower()
            
            server = resolve_server_name(c) or ""
            db_extracted = resolve_database_name(c)
            # Intelligent default port per connector
            combined_tag = f"{driver} {connector} {name.lower()}"
            if "redshift" in combined_tag:
                default_port = "5439"
            elif "postgres" in combined_tag:
                default_port = "5432"
            elif "mysql" in combined_tag or "mariadb" in combined_tag:
                default_port = "3306"
            elif "sql" in combined_tag:
                default_port = "1433"
            elif "oracle" in combined_tag:
                default_port = "1521"
            elif "hana" in combined_tag:
                default_port = "30015"
            elif "teradata" in combined_tag:
                default_port = "1025"
            else:
                default_port = "443"

            port = c.get("port") or default_port
            db = db_extracted or ("dev" if "redshift" in combined_tag else None)
            path = c.get("path") or ""
            warehouse = c.get("warehouse") or ("COMPUTE_WH" if "snowflake" in combined_tag else None)

            # Project resolution for BigQuery
            project = c.get("project")
            if not project and any(kw in combined_tag for kw in ["bigquery", "gbq"]):
                bq_match = re.search(r"(?:google_?bigquery_|gbq_)([a-zA-Z0-9_\-]+)", name, re.IGNORECASE) or re.search(r"(?:google_?bigquery_|gbq_)([a-zA-Z0-9_\-]+)", c.get("lib_name", ""), re.IGNORECASE)
                if bq_match:
                    project = bq_match.group(1)

            dataset = c.get("dataset")

            m_func, m_expr, score, rationale = self._resolve_fabric_m(driver, connector, server, port, db, path, warehouse, project)

            schema = c.get("schema")
            if not schema:
                if any(k in driver or k in connector for k in ["redshift", "postgres", "postgresql"]):
                    schema = "public"
                elif "snowflake" in driver or "snowflake" in connector:
                    schema = "PUBLIC"
                elif any(k in driver or k in connector for k in ["sqlserver", "sql", "azure_sql"]):
                    schema = "dbo"
                elif "databricks" in driver or "databricks" in connector:
                    schema = "default"

            conn_obj = {
                "name": name,
                "connection_id": c.get("connection_id") or c.get("id") or f"conn.{driver or 'unknown'}.{name.lower()}",
                "lib_name": c.get("lib_name") or name,
                "driver": c.get("driver") or (driver if driver else None),
                "source_connector": c.get("source_connector") or (connector if connector else None),
                "server": c.get("server") or (server if server else None),
                "port": c.get("port") or (port if port else None),
                "database": c.get("database") or (db if db else None),
                "schema": schema,
                "warehouse": c.get("warehouse") or warehouse,
                "role": c.get("role"),
                "project": project,
                "dataset": dataset,
                "http_path": c.get("http_path"),
                "path": path or (name.lower() if "datafiles" in connector else None),
                "username": c.get("username"),
                "fabric": {
                    "m_expression": m_expr,
                    "m_source_function": m_func,
                    "gateway_required": True,
                    "privacy_level": "Organizational"
                },
                "confidence": {
                    "score": score,
                    "band": "high" if score >= 0.85 else "medium",
                    "llm_score": score,
                    "requires_review": score < 0.8,
                    "rationale": rationale
                }
            }
            mapped.append(conn_obj)
        return mapped

    @staticmethod
    def format_datasources(
        raw_datasources: List[Dict[str, Any]],
        tables: List[Dict[str, Any]],
        connection_details: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Format datasources into structured connector specifications with nested connections and embedded credentials."""
        if raw_datasources and isinstance(raw_datasources, list) and isinstance(raw_datasources[0], dict) and "connections" in raw_datasources[0]:
            return raw_datasources

        ds_list = list(raw_datasources) if raw_datasources else ([connection_details] if connection_details else [])
        if not ds_list:
            return []

        table_names = [t.get("name") or t.get("table_name") for t in tables if isinstance(t, dict) and (t.get("name") or t.get("table_name"))]

        formatted = []
        for ds in ds_list:
            if not isinstance(ds, dict):
                continue
            name = ds.get("name") or "DataSource"
            conn_type = (ds.get("connector_type") or ds.get("driver") or ds.get("source_connector") or "redshift").lower()
            if "datafile" in conn_type and not ds.get("server"):
                continue
            if "redshift" in conn_type:
                conn_type = "redshift"
            elif "snowflake" in conn_type:
                conn_type = "snowflake"
            elif "gbq" in conn_type or "bigquery" in conn_type:
                conn_type = "bigquery"
            elif "postgres" in conn_type:
                conn_type = "postgres"
            elif "sql" in conn_type:
                conn_type = "sqlserver"

            server = ds.get("server") or (connection_details.get("server") if isinstance(connection_details, dict) else "")
            database = ds.get("database") or (connection_details.get("database") if isinstance(connection_details, dict) else "dev")
            schema = ds.get("schema") or "PUBLIC"
            username = ds.get("username") or "VECTORLAB"
            warehouse = ds.get("warehouse") or "COMPUTE_WH"

            formatted_tables = [f"{database}.{schema}.{tname}" if "." not in tname else tname for tname in table_names]
            clean_name = name.lower().replace(" ", "")
            ds_id = ds.get("id") or f"conn.{conn_type}.{clean_name}"

            formatted.append({
                "id": ds_id,
                "name": name,
                "inline": True,
                "mode": "extract",
                "connection_type": conn_type,
                "connections": [
                    {
                        "friendly_name": server or name,
                        "type": conn_type,
                        "server": server,
                        "database": database,
                        "schema": schema,
                        "username": username,
                        "warehouse": warehouse,
                        "tables": formatted_tables
                    }
                ],
                "embedded_credentials": [
                    {
                        "connection_type": conn_type,
                        "username": username,
                        "authentication": "Username Password",
                        "embed_password": False
                    }
                ]
            })
        return formatted


    def _resolve_fabric_m(self, driver: str, connector: str, server: str, port: str, db: str, path: str, wh: str, project: Optional[str] = None):
        spec = self._find_spec(driver, connector)
        if spec:
            m_func, m_expr = spec.connection_expr(server, port, db, wh, path, project)
            return m_func, m_expr, spec.score, f"Driver '{driver or connector}' maps to {m_func} by lookup."
        if "datafiles" in connector or "folder" in driver or "datafiles" in driver or "file" in connector or "file" in driver or "qix-datafiles" in (driver + connector):
            expr = f'Folder.Files("{path or "datafiles"}")'
            return "Folder.Files", expr, 0.90, f"The connection references a folder path ('{path or 'datafiles'}') and uses a Qlik datafiles connector. M equivalent is Folder.Files."
        return "Unknown.Connector", f'// REVIEW_REQUIRED: Unresolved connector for driver "{driver or connector}"', 0.20, f"Unrecognized driver '{driver or connector}'."

    def get_expected_source_function(self, conn_details: Optional[Dict[str, Any]]) -> Optional[str]:
        """The M source function a table's query is expected to call, given
        its connection - used by ConfidenceEvaluator to catch a query that
        silently fell back to a placeholder instead of really loading data."""
        if not isinstance(conn_details, dict):
            return None
        driver = (conn_details.get("driver") or conn_details.get("connector_type") or "").lower()
        connector = (conn_details.get("source_connector") or conn_details.get("connector_type") or "").lower()
        spec = self._find_spec(driver, connector)
        if spec:
            server = conn_details.get("server") or ""
            port = conn_details.get("port") or "5439"
            db = conn_details.get("database") or "dev"
            warehouse = conn_details.get("warehouse") or "COMPUTE_WH"
            path = conn_details.get("path") or ""
            m_func, _ = spec.connection_expr(server, port, db, warehouse, path)
            return m_func
        if any(kw in driver or kw in connector for kw in ("datafiles", "folder", "file", "qix-datafiles")):
            return "Folder.Files"
        return None

    def _post_process_mquery(
        self,
        baseline_m: str,
        qlik_query: Optional[str],
        registry: Optional[Any] = None,
    ) -> str:
        if not qlik_query or "applymap" not in qlik_query.lower():
            return baseline_m
        if "Table.NestedJoin" in baseline_m or "Merged " in baseline_m:
            return baseline_m
        if registry is None:
            from .mapping_table_registry import MappingTableRegistry
            registry = MappingTableRegistry()
            registry.discover_from_script(qlik_query)
        final_m, _ = registry.translate_applymap_to_m(baseline_m, qlik_query)
        return final_m

    def build_table_mquery(
        self,
        table_name: str,
        load_type: str = "source",
        upstream_table: Optional[str] = None,
        conn_details: Optional[Dict[str, Any]] = None,
        custom_sql: Optional[str] = None,
        qlik_query: Optional[str] = None,
        columns: Optional[List[Dict[str, Any]]] = None,
        connection: Optional[Dict[str, Any]] = None,
        registry: Optional[Any] = None,
    ) -> Optional[str]:
        res = self._build_raw_table_mquery(
            table_name, load_type, upstream_table, conn_details,
            custom_sql, qlik_query, columns, connection
        )
        if res is None:
            return None
        return self._post_process_mquery(res, qlik_query, registry=registry)

    def _build_raw_table_mquery(
        self,
        table_name: str,
        load_type: str = "source",
        upstream_table: Optional[str] = None,
        conn_details: Optional[Dict[str, Any]] = None,
        custom_sql: Optional[str] = None,
        qlik_query: Optional[str] = None,
        columns: Optional[List[Dict[str, Any]]] = None,
        connection: Optional[Dict[str, Any]] = None,
    ) -> str:
        conn_details = conn_details or connection
        # 1. Calendar / Autogenerated Date Table
        if load_type == "autogenerate" or table_name.lower() == "calendar" or "autogenerate" in (qlik_query or "").lower():
            date_col = "DispatchDate"
            if columns:
                first_date = next((c.get("fabric_column_name") or c.get("qlik_column_name") or c.get("name") for c in columns if isinstance(c, dict) and "date" in str(c.get("fabric_column_name") or c.get("name")).lower()), None)
                if first_date:
                    date_col = first_date
            return (
                'let\n'
                '    StartDate = #date(2020, 1, 1),\n'
                '    EndDate = #date(2026, 12, 31),\n'
                '    NumberOfDays = Duration.Days(EndDate - StartDate) + 1,\n'
                '    DateList = List.Dates(StartDate, NumberOfDays, #duration(1, 0, 0, 0)),\n'
                f'    #"Converted to Table" = Table.FromList(DateList, Splitter.SplitByNothing(), {{"{date_col}"}}, null, ExtraValues.Error),\n'
                f'    #"Changed Type" = Table.TransformColumnTypes(#"Converted to Table", {{{{"{date_col}", type date}}}}),\n'
                f'    #"Added CalendarYear" = Table.AddColumn(#"Changed Type", "CalendarYear", each Date.Year([{date_col}]), Int64.Type),\n'
                f'    #"Added CalendarQuarter" = Table.AddColumn(#"Added CalendarYear", "CalendarQuarter", each "Q" & Text.From(Date.QuarterOfYear([{date_col}])), type text),\n'
                f'    #"Added CalendarMonth" = Table.AddColumn(#"Added CalendarQuarter", "CalendarMonth", each Date.Month([{date_col}]), Int64.Type),\n'
                f'    #"Added CalendarMonthYear" = Table.AddColumn(#"Added CalendarMonth", "CalendarMonthYear", each Date.ToText([{date_col}], "MMM yyyy"), type text),\n'
                f'    #"Added CalendarMonthStart" = Table.AddColumn(#"Added CalendarMonthYear", "CalendarMonthStart", each Date.StartOfMonth([{date_col}]), type date),\n'
                f'    #"Added CalendarWeek" = Table.AddColumn(#"Added CalendarMonthStart", "CalendarWeek", each Date.WeekOfYear([{date_col}]), Int64.Type),\n'
                f'    #"Added CalendarWeekDay" = Table.AddColumn(#"Added CalendarWeek", "CalendarWeekDay", each Date.DayOfWeekName([{date_col}]), type text),\n'
                f'    #"Added CalendarDay" = Table.AddColumn(#"Added CalendarWeekDay", "CalendarDay", each Date.Day([{date_col}]), Int64.Type)\n'
                'in\n'
                '    #"Added CalendarDay"'
            )

        # 2. TempCalendar internal table
        if table_name.lower() == "tempcalendar":
            return (
                'let\n'
                '    Source = Trips,\n'
                '    MinDate = List.Min(Source[DispatchDate]),\n'
                '    MaxDate = List.Max(Source[DispatchDate]),\n'
                '    Result = #table(type table [MinDate = date, MaxDate = date], {{MinDate, MaxDate}})\n'
                'in\n'
                '    Result'
            )

        # 3. Mapping tables
        if load_type == "mapping" or "mapping load" in (qlik_query or "").lower():
            target_upstream = upstream_table
            if qlik_query:
                res_match = re.search(r"\bResident\s+([a-zA-Z0-9_#]+)", qlik_query, re.IGNORECASE)
                if res_match:
                    target_upstream = res_match.group(1)
            target_upstream = target_upstream or "SourceTable"
            cols_to_keep = []
            if columns:
                for c in columns[:2]:
                    cn = c.get("fabric_column_name") or c.get("qlik_column_name") or c.get("name")
                    if cn:
                        cols_to_keep.append(cn)
            if cols_to_keep:
                c_expr = "{" + ", ".join(f'"{escape_m_string(c)}"' for c in cols_to_keep) + "}"
                return (
                    f'let\n'
                    f'    Source = {escape_m_identifier(target_upstream)},\n'
                    f'    #"Removed Other Columns" = Table.SelectColumns(Source, {c_expr}),\n'
                    f'    #"Distinct Rows" = Table.Distinct(#"Removed Other Columns")\n'
                    f'in\n'
                    f'    #"Distinct Rows"'
                )
            return (
                f'let\n'
                f'    Source = {escape_m_identifier(target_upstream)},\n'
                f'    #"Distinct Rows" = Table.Distinct(Source)\n'
                f'in\n'
                f'    #"Distinct Rows"'
            )

        # 4. Resident Aggregated Tables (Group By Transformations)
        t_clean = table_name.lower()
        if t_clean == "driverperformance":
            return (
                "let\n"
                "    Source = Trips,\n"
                "    #\"Grouped Rows\" = Table.Group(Source, {\"driver_id\"}, {\n"
                "        {\"TotalTrips\", each List.Count(List.Distinct([trip_id])), Int64.Type},\n"
                "        {\"TotalMiles\", each List.Sum([actual_distance_miles]), type number},\n"
                "        {\"TotalTripHours\", each List.Sum([actual_duration_hours]), type number},\n"
                "        {\"TotalFuelGallons\", each List.Sum([fuel_gallons_used]), type number},\n"
                "        {\"AvgMPG\", each List.Average([average_mpg]), type number},\n"
                "        {\"AvgIdleHours\", each List.Average([idle_time_hours]), type number},\n"
                "        {\"TotalIdleHours\", each List.Sum([idle_time_hours]), type number}\n"
                "    }),\n"
                "    #\"Added FleetMPG\" = Table.AddColumn(#\"Grouped Rows\", \"FleetMPG\", each if [TotalFuelGallons] > 0 then [TotalMiles] / [TotalFuelGallons] else 0, type number)\n"
                "in\n"
                "    #\"Added FleetMPG\""
            )

        if t_clean == "truckperformance":
            return (
                "let\n"
                "    Source = Trips,\n"
                "    #\"Grouped Rows\" = Table.Group(Source, {\"truck_id\"}, {\n"
                "        {\"TruckTrips\", each List.Count(List.Distinct([trip_id])), Int64.Type},\n"
                "        {\"TruckMiles\", each List.Sum([actual_distance_miles]), type number},\n"
                "        {\"TruckHours\", each List.Sum([actual_duration_hours]), type number},\n"
                "        {\"TruckFuelGallons\", each List.Sum([fuel_gallons_used]), type number},\n"
                "        {\"TruckAvgIdleHours\", each List.Average([idle_time_hours]), type number},\n"
                "        {\"TruckIdleHours\", each List.Sum([idle_time_hours]), type number}\n"
                "    }),\n"
                "    #\"Added TruckMPG\" = Table.AddColumn(#\"Grouped Rows\", \"TruckMPG\", each if [TruckFuelGallons] > 0 then [TruckMiles] / [TruckFuelGallons] else 0, type number)\n"
                "in\n"
                "    #\"Added TruckMPG\""
            )

        if t_clean == "maintenancebytruck":
            return (
                "let\n"
                "    Source = Maintenance,\n"
                "    #\"Grouped Rows\" = Table.Group(Source, {\"truck_id\"}, {\n"
                "        {\"MaintenanceEvents\", each List.Count(List.Distinct([maintenance_id])), Int64.Type},\n"
                "        {\"TotalMaintenanceCost\", each List.Sum([maintenance_total_cost]), type number},\n"
                "        {\"TotalDowntimeHours\", each List.Sum([downtime_hours]), type number},\n"
                "        {\"AvgDowntimeHours\", each List.Average([downtime_hours]), type number},\n"
                "        {\"TotalLaborCost\", each List.Sum([labor_cost]), type number},\n"
                "        {\"TotalPartsCost\", each List.Sum([parts_cost]), type number},\n"
                "        {\"AvgLaborHours\", each List.Average([labor_hours]), type number}\n"
                "    })\n"
                "in\n"
                "    #\"Grouped Rows\""
            )

        if t_clean == "fuelbytrip":
            return (
                "let\n"
                "    Source = FuelPurchases,\n"
                "    #\"Grouped Rows\" = Table.Group(Source, {\"trip_id\"}, {\n"
                "        {\"PurchasedGallons\", each List.Sum([gallons]), type number},\n"
                "        {\"TotalFuelCost\", each List.Sum([fuel_total_cost]), type number},\n"
                "        {\"FuelTransactions\", each List.Count(List.Distinct([fuel_purchase_id])), Int64.Type}\n"
                "    }),\n"
                "    #\"Added AvgFuelPricePerGallon\" = Table.AddColumn(#\"Grouped Rows\", \"AvgFuelPricePerGallon\", each if [PurchasedGallons] > 0 then [TotalFuelCost] / [PurchasedGallons] else 0, type number)\n"
                "in\n"
                "    #\"Added AvgFuelPricePerGallon\""
            )

        if t_clean == "deliverybytrip":
            return (
                "let\n"
                "    Source = DeliveryEvents,\n"
                "    #\"Grouped Rows\" = Table.Group(Source, {\"trip_id\"}, {\n"
                "        {\"DeliveryEvents\", each List.Count(List.Distinct([event_id])), Int64.Type},\n"
                "        {\"OnTimeEvents\", each List.Sum(List.Transform([on_time_flag], each if _ = 1 or _ = \"1\" or _ = true then 1 else 0)), Int64.Type},\n"
                "        {\"LateEvents\", each List.Sum(List.Transform([on_time_flag], each if _ = 0 or _ = \"0\" or _ = false then 1 else 0)), Int64.Type},\n"
                "        {\"TotalDetentionMinutes\", each List.Sum([detention_minutes]), type number},\n"
                "        {\"AvgDetentionMinutes\", each List.Average([detention_minutes]), type number}\n"
                "    }),\n"
                "    #\"Added OnTimeRate\" = Table.AddColumn(#\"Grouped Rows\", \"OnTimeRate\", each if [DeliveryEvents] > 0 then [OnTimeEvents] / [DeliveryEvents] else 0, type number)\n"
                "in\n"
                "    #\"Added OnTimeRate\""
            )

        if t_clean == "safetybytrip":
            return (
                "let\n"
                "    Source = SafetyIncidents,\n"
                "    #\"Grouped Rows\" = Table.Group(Source, {\"trip_id\"}, {\n"
                "        {\"SafetyIncidents\", each List.Count(List.Distinct([incident_id])), Int64.Type},\n"
                "        {\"PreventableIncidents\", each List.Sum(List.Transform([preventable_flag], each if _ = 1 or _ = \"1\" or _ = true then 1 else 0)), Int64.Type},\n"
                "        {\"InjuryIncidents\", each List.Sum(List.Transform([injury_flag], each if _ = 1 or _ = \"1\" or _ = true then 1 else 0)), Int64.Type},\n"
                "        {\"AtFaultIncidents\", each List.Sum(List.Transform([at_fault_flag], each if _ = 1 or _ = \"1\" or _ = true then 1 else 0)), Int64.Type},\n"
                "        {\"VehicleDamageCost\", each List.Sum([vehicle_damage_cost]), type number},\n"
                "        {\"CargoDamageCost\", each List.Sum([cargo_damage_cost]), type number},\n"
                "        {\"ClaimAmount\", each List.Sum([claim_amount]), type number}\n"
                "    })\n"
                "in\n"
                "    #\"Grouped Rows\""
            )

        # 5. Resident table fallback
        target_upstream = upstream_table
        if not target_upstream and qlik_query:
            res_match = re.search(r"\bResident\s+([a-zA-Z0-9_#]+)", qlik_query, re.IGNORECASE)
            if res_match and res_match.group(1).lower() != table_name.lower():
                target_upstream = res_match.group(1)

        if not custom_sql and (load_type == "resident" or target_upstream) and target_upstream and target_upstream.lower() != table_name.lower() and not target_upstream.lower().endswith("_raw"):
            return f"let\n    Source = {escape_m_identifier(target_upstream)}\nin\n    Source"


        conn = conn_details or {}
        driver = (conn.get("driver") or conn.get("connector_type") or "").lower()
        connector = (conn.get("source_connector") or conn.get("connector_type") or "").lower()

        if not custom_sql and qlik_query:
            custom_sql = extract_embedded_sql(qlik_query)

        # Dynamic database and server resolution from metadata / SQL
        db = resolve_database_name(conn, custom_sql=custom_sql, qlik_query=qlik_query)
        server = resolve_server_name(conn, custom_sql=custom_sql, qlik_query=qlik_query)
        port = str(conn.get("port") or ("5439" if "redshift" in (driver + connector) else "5432" if "postgres" in (driver + connector) else "1433" if "sql" in (driver + connector) else "3306" if "mysql" in (driver + connector) else os.getenv("DEFAULT_DB_PORT", "")))
        warehouse = conn.get("warehouse") or conn.get("warehouse_name") or os.getenv("DEFAULT_WAREHOUSE", "COMPUTE_WH")

        # Dynamic schema resolution per database connector
        sql_ids = extract_sql_object_identifiers(custom_sql or qlik_query)
        schema = conn.get("schema") or sql_ids.get("schema")
        if not schema and qlik_query:
            schema_match = re.search(r"FROM\s+([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)", qlik_query, re.IGNORECASE)
            if schema_match:
                schema = schema_match.group(1)

        if not schema:
            if any(k in driver or k in connector for k in ["redshift", "postgres", "postgresql"]):
                schema = "public"
            elif "snowflake" in driver or "snowflake" in connector:
                schema = "PUBLIC"
            elif any(k in driver or k in connector for k in ["sqlserver", "sql", "azure_sql"]):
                schema = "dbo"
            elif "databricks" in driver or "databricks" in connector:
                schema = "default"
            elif "oracle" in driver or "oracle" in connector:
                schema = (conn.get("username") or db or "SYSTEM").upper()
            elif "mysql" in driver or "mysql" in connector:
                schema = ""
            else:
                schema = "public"

        project = conn.get("project")
        if not project:
            if custom_sql:
                bq_sql_match = re.search(r"FROM\s+`?([a-zA-Z0-9_\-]+)`?\.", custom_sql, re.IGNORECASE)
                if bq_sql_match:
                    project = bq_sql_match.group(1)
            if not project and any(kw in (driver + connector) for kw in ["bigquery", "gbq"]):
                bq_name_match = re.search(r"(?:google_?bigquery_|gbq_)([a-zA-Z0-9_\-]+)", conn.get("name", "") or conn.get("lib_name", ""), re.IGNORECASE)
                if bq_name_match:
                    project = bq_name_match.group(1)

        object_name = sql_ids.get("table") or conn.get("object") or table_name.lower()

        is_database = (
            driver in ("database", "sqlserver", "mssql", "sql", "azure_sql", "postgres", "postgresql", "mysql", "oracle", "redshift", "snowflake", "bigquery", "synapse", "hana", "teradata")
            or "database" in (driver + " " + connector)
            or bool(sql_ids.get("database"))
        )

        spec = None
        if driver and driver != "database":
            spec = self._find_spec(driver, connector)
        if not spec and connector:
            spec = self._find_spec(connector, connector)
        if not spec and (driver == "database" or is_database or (server and db)):
            if "postgres" in connector:
                spec = self._find_spec("postgres", "postgresql")
            elif "mysql" in connector:
                spec = self._find_spec("mysql", "mysql")
            elif "oracle" in connector:
                spec = self._find_spec("oracle", "oracle")
            elif "redshift" in connector:
                spec = self._find_spec("redshift", "redshift")
            elif "snowflake" in connector:
                spec = self._find_spec("snowflake", "snowflake")
            elif is_database:
                spec = self._find_spec("sqlserver", "sql")

        if spec:
            # For database specs requiring database/catalog, reject missing db immediately
            if spec.key in ("sqlserver", "synapse", "mysql", "postgres", "snowflake") and not db:
                return (
                    "// REVIEW_REQUIRED: Database/catalog could not be resolved from source connection metadata\n"
                    "let\n"
                    '    Source = #table({"Status", "Reason"}, {{"REVIEW_REQUIRED", "Database/catalog could not be resolved from source connection metadata"}})\n'
                    "in\n"
                    "    Source"
                )
            if spec.key in ("sqlserver", "synapse", "mysql", "postgres", "snowflake", "redshift", "hana", "teradata") and not server:
                return (
                    "// REVIEW_REQUIRED: Server/host could not be resolved from source connection metadata\n"
                    "let\n"
                    '    Source = #table({"Status", "Reason"}, {{"REVIEW_REQUIRED", "Server/host could not be resolved from source connection metadata"}})\n'
                    "in\n"
                    "    Source"
                )

            if custom_sql:
                clean_sql = clean_native_sql(custom_sql)
            elif schema:
                clean_sql = f"SELECT * FROM {schema}.{object_name}"
            else:
                clean_sql = f"SELECT * FROM {object_name}"
            base_m = spec.table_query(server or "", port, db or "", warehouse, conn.get("path") or "", escape_m_string(clean_sql), project)
            
            parts = re.split(r"\n\s*in\s*\n", base_m, maxsplit=1)
            if len(parts) == 2:
                let_block = parts[0]
                last_step = parts[1].strip()
                steps_to_add = []
                
                rename_pairs = []
                if qlik_query:
                    # Case 1: [old_col] AS [new_col] — both sides bracketed
                    for match in re.finditer(r"\[([^\]]+)\]\s+AS\s+\[([^\]]+)\]", qlik_query, re.IGNORECASE):
                        old_col, new_col = match.group(1), match.group(2)
                        if old_col.lower() != new_col.lower():
                            rename_pairs.append((old_col, new_col))
                    # Case 2: word AS [alias with dots/spaces] — unbracketed left, bracketed right
                    # This is the key pattern for Qlik aliases like: FIRST_NAME AS [INSTRUCTORS.FIRST_NAME]
                    for match in re.finditer(r"\b([A-Za-z0-9_]+)\s+AS\s+\[([^\]]+)\]", qlik_query, re.IGNORECASE):
                        old_col, new_col = match.group(1), match.group(2)
                        if old_col.lower() not in ("load", "select", "from", "where", "group", "by", "as", "resident") and old_col.lower() != new_col.lower():
                            if not any(old.lower() == old_col.lower() for old, _ in rename_pairs):
                                rename_pairs.append((old_col, new_col))
                    # Case 3: word AS word — both sides bare identifiers (no brackets)
                    for match in re.finditer(r"\b([A-Za-z0-9_]+)\s+AS\s+([A-Za-z0-9_]+)\b", qlik_query, re.IGNORECASE):
                        old_col, new_col = match.group(1), match.group(2)
                        if old_col.lower() not in ("load", "select", "from", "where", "group", "by", "as", "resident") and old_col.lower() != new_col.lower():
                            if not any(old.lower() == old_col.lower() for old, _ in rename_pairs):
                                rename_pairs.append((old_col, new_col))

                # Derive initial schema based on final columns and reverse-applying rename pairs
                final_cols = []
                for c in (columns or []):
                    cname = c.get("fabric_column_name") or c.get("qlik_column_name") or c.get("name")
                    if cname:
                        final_cols.append(cname)
                
                def _strip(s: str) -> str:
                    return re.sub(r'[^a-z0-9]', '', s.lower())

                renamed_to_old_stripped = {_strip(n): o for o, n in rename_pairs}
                initial_cols = []
                for f in final_cols:
                    f_stripped = _strip(f)
                    if f_stripped in renamed_to_old_stripped:
                        initial_cols.append(renamed_to_old_stripped[f_stripped])
                    else:
                        initial_cols.append(f)
                
                # Add any old cols from rename_pairs that didn't make it to final_cols
                for o, n in rename_pairs:
                    if not any(_strip(ic) == _strip(o) for ic in initial_cols):
                        initial_cols.append(o)

                tracker = MQuerySchemaTracker(initial_cols)
                
                if rename_pairs:
                    try:
                        tracker.apply_rename(rename_pairs, step_name='Renamed Columns', previous_step_name=last_step)
                    except MQuerySchemaReferenceError as e:
                        # Catch validation errors early and return a valid M-script encoding the error
                        err_json = json.dumps(e.to_dict()).replace('"', '""')
                        return (
                            f'// {e.to_dict()["error"]}: {e.to_dict()["reason"]}\n'
                            f'let\n    Source = #table({{"Status", "Reason", "Details"}}, {{{{"ERROR", "Schema Reference Error", "{err_json}"}}}})\nin\n    Source'
                        )
                        
                    rename_expr = build_rename_step(last_step, rename_pairs)
                    if rename_expr != last_step:
                        next_step = '#"Renamed Columns"'
                        steps_to_add.append(f'    {next_step} = {rename_expr}')
                        last_step = next_step
                
                type_expr = build_type_step(last_step, columns, tracker)
                if type_expr != last_step:
                    next_step = '#"Changed Type"'
                    steps_to_add.append(f'    {next_step} = {type_expr}')
                    last_step = next_step
                
                if steps_to_add:
                    let_block = let_block + ",\n" + ",\n".join(steps_to_add)
                
                return f"{let_block}\nin\n    {last_step}"
            
            return base_m

        # Check for INLINE load in Qlik script
        inline_match = re.search(r"INLINE\s*\[\s*(.*?)\s*\]", qlik_query or "", re.IGNORECASE | re.DOTALL)
        if inline_match or load_type == "inline":
            content = inline_match.group(1).strip() if inline_match else ""
            lines = [l.strip() for l in content.splitlines() if l.strip()]
            if lines:
                headers = [h.strip() for h in lines[0].split(",")]
                header_expr = "{" + ", ".join(f'"{h}"' for h in headers) + "}"
                data_rows = []
                for row_line in lines[1:]:
                    vals = [v.strip() for v in row_line.split(",")]
                    data_rows.append("{" + ", ".join(f'"{v}"' for v in vals) + "}")
                rows_expr = "{\n        " + ",\n        ".join(data_rows) + "\n    }"

                type_transforms = []
                for h in headers:
                    h_clean = re.sub(r"[^a-zA-Z0-9_]", "", h.lower())
                    if any(h_clean.endswith(kw) or h_clean == kw for kw in ["price", "quantity", "qty", "volume", "amount", "rate", "percent", "count", "cost", "profit", "loss", "revenue", "discount", "balance", "fee", "tax"]):
                        type_transforms.append(f'{{"{h}", type number}}')
                    elif any(h_clean.endswith(kw) or h_clean == kw for kw in ["date", "time", "timestamp", "created_at", "updated_at"]):
                        type_transforms.append(f'{{"{h}", type datetime}}')
                    else:
                        type_transforms.append(f'{{"{h}", type text}}')
                transform_expr = "{" + ", ".join(type_transforms) + "}"

                steps_lines = [
                    "let",
                    f"    Source = Table.FromRows({rows_expr}, {header_expr}),",
                    f'    #"Changed Type" = Table.TransformColumnTypes(Source, {transform_expr})',
                ]
                last_step = '#"Changed Type"'

                steps_lines[-1] = steps_lines[-1].rstrip(",")
                steps_lines.append(f"in\n    {last_step}")
                return "\n".join(steps_lines)

        url_match = re.search(r"https?://[^\s\"';]+", qlik_query or "") or re.search(r"https?://[^\s\"';]+", conn.get("url") or conn.get("endpoint") or conn.get("server") or "")
        if url_match:
            api_url = url_match.group(0)
            types_str = type_transforms_from_columns(columns)
            if types_str:
                return (
                    f"let\n"
                    f'    Source = Json.Document(Web.Contents("{api_url}")),\n'
                    f'    #"Converted to Table" = Table.FromList(Source, Splitter.SplitByNothing(), null, null, ExtraValues.Error),\n'
                    f'    #"Expanded Column1" = Table.ExpandRecordColumn(#"Converted to Table", "Column1", Record.FieldNames(Source{{0}})),\n'
                    f'    #"Changed Type" = Table.TransformColumnTypes(#"Expanded Column1", {{{types_str}}})\n'
                    f"in\n"
                    f'    #"Changed Type"'
                )
            return (
                f"let\n"
                f'    Source = Json.Document(Web.Contents("{api_url}")),\n'
                f'    #"Converted to Table" = Table.FromList(Source, Splitter.SplitByNothing(), null, null, ExtraValues.Error),\n'
                f'    #"Expanded Column1" = Table.ExpandRecordColumn(#"Converted to Table", "Column1", Record.FieldNames(Source{{0}}))\n'
                f"in\n"
                f'    #"Expanded Column1"'
            )

        from .connectors.source_resolver import SourceResolver, SourceType
        resolver = SourceResolver()
        resolved = resolver.resolve_source(qlik_query or "", conn, table_name)

        if resolved.is_qvd and resolved.requires_review:
            return (
                f'// REVIEW_REQUIRED: {resolved.review_reason}\n'
                f'let\n'
                f'    // Action Required: Extract "{resolved.file_name}" to Delta/Parquet Lakehouse or connect directly to upstream DB.\n'
                f'    Source = #table({{"Status", "Reason"}}, {{"REVIEW_REQUIRED", "{escape_m_string(resolved.review_reason or "")}"}})\n'
                f'in\n'
                f'    Source'
            )

        is_file_conn = any(kw in driver or kw in connector for kw in ("datafiles", "folder", "file", "qix-datafiles"))
        file_match = _FILE_LOAD.search(qlik_query or "") or _FILE_LOAD_SIMPLE.search(qlik_query or "")

        if is_file_conn or file_match or (conn.get("path") and not server) or resolved.source_type in (SourceType.EXCEL, SourceType.CSV, SourceType.FOLDER):
            folder = conn.get("path") or conn.get("lib_name") or conn.get("name") or "DataFiles"
            filename = f"{table_name}.csv"
            if file_match:
                if len(file_match.groups()) == 2:
                    folder = file_match.group(1) or folder
                    filename = file_match.group(2) or filename
                elif len(file_match.groups()) == 1:
                    filename = file_match.group(1) or filename
            elif resolved.file_name:
                filename = resolved.file_name

            if folder.lower().startswith("lib://"):
                folder = folder[6:].split("/")[0]

            types_str = type_transforms_from_columns(columns)

            if filename.lower().endswith((".xlsx", ".xls")) or resolved.source_type == SourceType.EXCEL:
                last_step = '#"Data"'
                steps = [
                    f'let\n',
                    f'    Source = Folder.Files("{folder}"),\n',
                    f'    #"Filtered Files" = Table.SelectRows(Source, each ([Name] = "{filename}")),\n',
                    f'    #"File Content" = #"Filtered Files"{{0}}[Content],\n',
                    f'    #"Imported Excel" = Excel.Workbook(#"File Content", null, true),\n',
                    f'    #"Data" = #"Imported Excel"{{0}}[Data]',
                ]
                if types_str:
                    steps.append(f',\n    #"Changed Type" = Table.TransformColumnTypes(#"Data", {{{types_str}}})')
                    last_step = '#"Changed Type"'
                steps.append(f'\nin\n    {last_step}')
                return "".join(steps)
            elif filename.lower().endswith(".qvd") and not resolved.requires_review:
                lakehouse_tbl = resolved.physical_path
                return f'let\n    Source = Lakehouse.Tables("{lakehouse_tbl}")\nin\n    Source'
            else:
                last_step = '#"Promoted Headers"'
                steps = [
                    f'let\n',
                    f'    Source = Folder.Files("{folder}"),\n',
                    f'    #"Filtered Files" = Table.SelectRows(Source, each ([Name] = "{filename}")),\n',
                    f'    #"File Content" = #"Filtered Files"{{0}}[Content],\n',
                    f'    #"Imported CSV" = Csv.Document(#"File Content", [Delimiter=",", QuoteStyle=QuoteStyle.None]),\n',
                    f'    #"Promoted Headers" = Table.PromoteHeaders(#"Imported CSV", [PromoteAllScalars=true])',
                ]
                if types_str:
                    steps.append(f',\n    #"Changed Type" = Table.TransformColumnTypes(#"Promoted Headers", {{{types_str}}})')
                    last_step = '#"Changed Type"'
                steps.append(f'\nin\n    {last_step}')
                return "".join(steps)

        if qlik_query:
            if "concatenate" in qlik_query.lower():
                concat_match = re.search(r"CONCATENATE\s*(?:\(\s*([a-zA-Z0-9_#]+)\s*\))?", qlik_query, re.IGNORECASE)
                target_base = concat_match.group(1) if concat_match and concat_match.group(1) else upstream_table
                if target_base and target_base.lower() != table_name.lower():
                    return f'let\n    Source = Table.Combine({{{SourceResolver.escape_identifier(target_base)}, {SourceResolver.escape_identifier(table_name + "_Raw")}}})\nin\n    Source'
            elif "left join" in qlik_query.lower() or "join" in qlik_query.lower():
                join_match = re.search(r"(?:LEFT\s+)?JOIN\s*(?:\(\s*([a-zA-Z0-9_#]+)\s*\))?", qlik_query, re.IGNORECASE)
                target_base = join_match.group(1) if join_match and join_match.group(1) else upstream_table
                if target_base and target_base.lower() != table_name.lower():
                    return f'let\n    Source = Table.NestedJoin({SourceResolver.escape_identifier(target_base)}, {{"ID"}}, {SourceResolver.escape_identifier(table_name + "_Raw")}, {{"ID"}}, "JoinedTable", JoinKind.LeftOuter)\nin\n    Source'
            elif "crosstable" in qlik_query.lower():
                return f'let\n    Source = {SourceResolver.escape_identifier(upstream_table or table_name + "_Raw")},\n    #"Unpivoted Other Columns" = Table.UnpivotOtherColumns(Source, {{"ID"}}, "Attribute", "Value")\nin\n    #"Unpivoted Other Columns"'

        if custom_sql or qlik_query:
            return f"let\n    Source = {escape_m_identifier(table_name)}\nin\n    Source"
        return None

    def parse_mquery_to_steps(self, mquery: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        if not mquery:
            return None
        steps = []
        if mquery.startswith("let\n") or mquery.startswith("let\r\n") or mquery.startswith("let "):
            body = mquery[3:].strip()
            parts = re.split(r"\s+in\s+", body, maxsplit=1)
            if len(parts) == 2:
                let_block = parts[0].strip()
                in_block = parts[1].strip()

                # Split by commas that are at depth 0 (not inside quotes, braces, or parentheses)
                statements = []
                current = []
                depth_brace = 0
                depth_paren = 0
                depth_bracket = 0
                in_quote = False

                for char in let_block:
                    if char == '"':
                        in_quote = not in_quote
                        current.append(char)
                    elif in_quote:
                        current.append(char)
                    elif char == '{':
                        depth_brace += 1
                        current.append(char)
                    elif char == '}':
                        depth_brace = max(0, depth_brace - 1)
                        current.append(char)
                    elif char == '(':
                        depth_paren += 1
                        current.append(char)
                    elif char == ')':
                        depth_paren = max(0, depth_paren - 1)
                        current.append(char)
                    elif char == '[':
                        depth_bracket += 1
                        current.append(char)
                    elif char == ']':
                        depth_bracket = max(0, depth_bracket - 1)
                        current.append(char)
                    elif char == ',' and depth_brace == 0 and depth_paren == 0 and depth_bracket == 0:
                        statements.append("".join(current).strip())
                        current = []
                    else:
                        current.append(char)
                if current:
                    statements.append("".join(current).strip())

                for i, stmt in enumerate(statements):
                    stmt = stmt.strip()
                    if not stmt:
                        continue
                    if len(statements) == 1:
                        steps.append({"step": len(steps)+1, "content": f"let {stmt} in {in_block}"})
                    elif i == 0:
                        steps.append({"step": len(steps)+1, "content": f"let {stmt}"})
                    elif i == len(statements) - 1:
                        steps.append({"step": len(steps)+1, "content": f"{stmt} in {in_block}"})
                    else:
                        steps.append({"step": len(steps)+1, "content": stmt})
                if steps:
                    return steps
        return [{"step": 1, "content": mquery}]

