"""ConnectionMapper: custom SQL / per-table data loading previously only had
a working NativeQuery path for Redshift - every other driver (Snowflake, SQL
Server, ...) silently fell back to `let Source = TableName in Source`, which
does not reference the real connection at all.
"""

from services.connection_mapper import ConnectionMapper, escape_m_identifier, extract_embedded_sql

SNOWFLAKE_CONN = {"driver": "snowflake", "server": "acct.snowflakecomputing.com",
                   "database": "ANALYTICS", "warehouse": "WH"}
SQLSERVER_CONN = {"driver": "sqlserver", "server": "sqlsrv01", "database": "Sales"}
POSTGRES_CONN = {"driver": "postgres", "server": "pg01", "database": "app"}


def test_snowflake_table_query_uses_native_query_not_placeholder():
    mapper = ConnectionMapper()
    mquery = mapper.build_table_mquery("Orders", "source", None, SNOWFLAKE_CONN, "SELECT * FROM orders")
    assert "Snowflake.Databases(" in mquery
    assert 'Db = Source{[Name="ANALYTICS",Kind="Database"]}[Data]' in mquery
    assert "Value.NativeQuery(Db," in mquery
    assert mquery != "let\n    Source = Orders\nin\n    Source"


def test_sqlserver_table_query_uses_native_query():
    mapper = ConnectionMapper()
    mquery = mapper.build_table_mquery("Orders", "source", None, SQLSERVER_CONN, "SELECT * FROM orders")
    assert 'Sql.Database("sqlsrv01", "Sales")' in mquery
    assert "Value.NativeQuery(Source," in mquery


def test_postgres_is_not_misclassified_as_sqlserver():
    """'postgresql' contains the substring 'sql' - must not match the
    generic sqlserver branch."""
    mapper = ConnectionMapper()
    mquery = mapper.build_table_mquery("Orders", "source", None, POSTGRES_CONN, "SELECT * FROM orders")
    assert "PostgreSQL.Database(" in mquery
    assert "Sql.Database(" not in mquery


def test_embedded_double_quotes_are_escaped_for_m():
    mapper = ConnectionMapper()
    mquery = mapper.build_table_mquery(
        "Orders", "source", None, SNOWFLAKE_CONN, 'SELECT "ID" FROM "ORDERS"'
    )
    assert '""ID""' in mquery
    assert '""ORDERS""' in mquery


def test_unresolvable_driver_falls_back_to_placeholder():
    mapper = ConnectionMapper()
    mquery = mapper.build_table_mquery("Orders", "source", None, {"driver": "unknown_thing"}, None)
    assert mquery is None


def test_resident_load_ignores_connection():
    mapper = ConnectionMapper()
    mquery = mapper.build_table_mquery("Derived", "resident", "Upstream Table", SNOWFLAKE_CONN, None)
    assert mquery == 'let\n    Source = #"Upstream Table"\nin\n    Source'


def test_escape_m_identifier():
    assert escape_m_identifier("PlainName") == "PlainName"
    assert escape_m_identifier("Has Space") == '#"Has Space"'
    assert escape_m_identifier("Loads-13") == '#"Loads-13"'


def test_extract_embedded_sql_pulls_select_out_of_load_script():
    script = (
        "// --- Source Part ---\n"
        "LOAD *;\n"
        'SQL SELECT "ID", "NAME" FROM "SCHEMA"."TABLE";'
    )
    sql = extract_embedded_sql(script)
    assert sql.startswith("SELECT")
    assert sql.endswith('"SCHEMA"."TABLE"')
    assert not sql.endswith(";")


def test_extract_embedded_sql_returns_none_when_absent():
    assert extract_embedded_sql("LOAD * FROM [Trips];") is None
    assert extract_embedded_sql(None) is None


def test_get_expected_source_function():
    mapper = ConnectionMapper()
    assert mapper.get_expected_source_function(SNOWFLAKE_CONN) == "Snowflake.Databases"
    assert mapper.get_expected_source_function(SQLSERVER_CONN) == "Sql.Database"
    assert mapper.get_expected_source_function(None) is None
