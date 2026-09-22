import sys
import os
sys.path.append(r'c:\Users\Kesha\Desktop\Combining Mapping\qlik mapping')

from services.connection_mapper import ConnectionMapper, escape_m_identifier, escape_m_string
import re

_SQL_TABLE_REF_PATTERN = re.compile(
    r'\b(?:FROM|JOIN)\s+((?:(?:"[^"]+"|\[[^\]]+\]|`[^`]+`|[a-zA-Z0-9_#$]+)(?:\s*\.\s*(?:"[^"]+"|\[[^\]]+\]|`[^`]+`|[a-zA-Z0-9_#$]+))*))',
    re.IGNORECASE
)

def extract_sql_object_identifiers(sql):
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

def resolve_database_name(conn, custom_sql=None, qlik_query=None):
    if not isinstance(conn, dict):
        conn = {}
    db = conn.get("database") or conn.get("db") or conn.get("catalog") or conn.get("initial_catalog")
    if not db and isinstance(conn.get("connections"), list) and conn["connections"]:
        first = conn["connections"][0]
        if isinstance(first, dict):
            db = first.get("database") or first.get("db") or first.get("catalog")
    if not db:
        conn_str = str(conn.get("connection_string") or conn.get("connect_string") or "")
        db_match = re.search(r"(?:Database|Initial\s*Catalog|DB|Catalog)\s*=\s*([^;]+)", conn_str, re.IGNORECASE)
        if db_match:
            db = db_match.group(1).strip()
    if not db:
        sql_ids = extract_sql_object_identifiers(custom_sql or qlik_query)
        if sql_ids.get("database"):
            db = sql_ids["database"]
    if not db:
        db = os.getenv("DEFAULT_DB_NAME", "").strip() or None
    return db

sql = 'SELECT "COURSE_ID", "COURSE_NAME", "DEPARTMENT", "CREDITS" FROM "LEARNING_ANALYTICS"."PUBLIC"."COURSES"'
conn = {'name': 'Learning_Analytics', 'connector_type': 'Database'}
print("Resolved DB:", resolve_database_name(conn, custom_sql=sql))
