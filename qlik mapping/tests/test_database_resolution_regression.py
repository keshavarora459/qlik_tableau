"""Regression tests for dynamic database resolution in Qlik -> Fabric Power Query M generation.

Verifies:
TEST 1: Resolved database metadata -> Sql.Database() receives dynamically resolved server/database -> no <DATABASE> -> validation passes
TEST 2: Missing database metadata -> no guessed value -> no hard-coded value -> validation fails -> deployable=false
TEST 3: Generated M contains <DATABASE> -> validation fails -> publish_ready=false
TEST 4: Different connection/database -> same generic code resolves it -> no code modification required
TEST 5: Database name contains spaces/special characters -> generated M remains syntactically valid
"""

import pytest
from services.connection_mapper import (
    ConnectionMapper,
    extract_sql_object_identifiers,
    resolve_database_name,
    resolve_server_name,
    validate_m_query,
)
from services.production_gate import ProductionGate, ProductionGateStatus
from src.converters.mquery.converter import validate_mquery


def test_1_resolved_database_metadata_passes_validation():
    """TEST 1:
    Resolved database metadata
    -> Sql.Database() receives dynamically resolved server/database
    -> no <DATABASE>
    -> validation passes
    """
    mapper = ConnectionMapper()

    # Scenario 1A: Database explicitly in connection dict
    conn_a = {
        "driver": "sqlserver",
        "server": "server-prod.database.windows.net",
        "database": "OperationsDB",
    }
    sql_a = "SELECT order_id, customer_id FROM Orders"
    m_query_a = mapper.build_table_mquery("Orders", "source", None, conn_a, sql_a)

    assert "<DATABASE>" not in m_query_a
    assert 'Sql.Database("server-prod.database.windows.net", "OperationsDB")' in m_query_a
    val_a = validate_m_query(m_query_a)
    assert val_a["passed"] is True, f"Errors: {val_a['errors']}"

    # Scenario 1B: Database resolved dynamically from 3-part SQL object identifier
    conn_b = {
        "connector_type": "Database",
        "name": "Analytics_Connection",
        "server": "analytics-dw.company.com",
    }
    sql_b = 'SELECT "COURSE_ID", "CREDITS" FROM "LEARNING_ANALYTICS"."PUBLIC"."COURSES"'
    m_query_b = mapper.build_table_mquery("Courses", "source", None, conn_b, sql_b)

    assert "<DATABASE>" not in m_query_b
    assert 'Sql.Database("analytics-dw.company.com", "LEARNING_ANALYTICS")' in m_query_b
    val_b = validate_m_query(m_query_b)
    assert val_b["passed"] is True, f"Errors: {val_b['errors']}"

    # Production gate evaluation should pass
    gate_payload = {
        "tables": [
            {
                "name": "Courses",
                "columns": [{"name": "COURSE_ID", "type": "string"}],
                "m_query": m_query_b,
            }
        ]
    }
    gate_res = ProductionGate.evaluate(gate_payload)
    assert gate_res.checks["m_validation"] is True
    assert gate_res.migration_status["m_validation"] == "passed"
    assert gate_res.migration_status["publish_ready"] is True
    assert gate_res.migration_status["deployable"] is True


def test_2_missing_database_metadata_fails_validation():
    """TEST 2:
    Missing database metadata
    -> no guessed value
    -> no hard-coded value
    -> validation fails
    -> deployable=false
    """
    mapper = ConnectionMapper()

    # Connection has server, but database is completely absent from conn and 1-part SQL
    conn = {
        "driver": "sqlserver",
        "server": "sqlsrv01.corp.net",
    }
    sql = "SELECT item_id, price FROM Products"
    m_query = mapper.build_table_mquery("Products", "source", None, conn, sql)

    # Must NOT guess a database, must NOT inject <DATABASE>, must NOT use Folder.Files()
    assert "<DATABASE>" not in m_query
    assert "Folder.Files" not in m_query
    assert "Learning_Analytics" not in m_query
    assert "// REVIEW_REQUIRED: Database/catalog could not be resolved" in m_query

    # Validation must fail
    val = validate_m_query(m_query)
    assert val["passed"] is False
    assert any("Database/catalog could not be resolved" in err for err in val["errors"])

    # Production gate evaluation must fail
    gate_payload = {
        "tables": [
            {
                "name": "Products",
                "columns": [{"name": "item_id", "type": "string"}],
                "m_query": m_query,
            }
        ]
    }
    gate_res = ProductionGate.evaluate(gate_payload)
    assert gate_res.checks["m_validation"] is False
    assert gate_res.migration_status["m_validation"] == "failed"
    assert gate_res.migration_status["publish_ready"] is False
    assert gate_res.migration_status["deployable"] is False
    assert gate_res.status == ProductionGateStatus.NOT_PRODUCTION_READY


def test_3_generated_m_contains_database_placeholder_fails():
    """TEST 3:
    Generated M contains <DATABASE>
    -> validation fails
    -> publish_ready=false
    """
    bad_m_query = (
        'let\n'
        '    Source = Sql.Database("sql-cluster-01", "<DATABASE>"),\n'
        '    Result = Value.NativeQuery(Source, "SELECT * FROM Table", null, [EnableFolding=false])\n'
        'in\n'
        '    Result'
    )

    # validate_m_query catches <DATABASE>
    val = validate_m_query(bad_m_query)
    assert val["passed"] is False
    assert any("<DATABASE>" in err for err in val["errors"])

    # validate_mquery from converter also catches it
    ok, problems = validate_mquery(bad_m_query, "Table", [])
    assert ok is False
    assert any("<DATABASE>" in p for p in problems)

    # Production gate evaluation must reject it
    gate_payload = {
        "tables": [
            {
                "name": "Table",
                "columns": [{"name": "id", "type": "string"}],
                "m_query": bad_m_query,
            }
        ]
    }
    gate_res = ProductionGate.evaluate(gate_payload)
    assert gate_res.checks["m_validation"] is False
    assert gate_res.migration_status["m_validation"] == "failed"
    assert gate_res.migration_status["publish_ready"] is False
    assert gate_res.migration_status["deployable"] is False
    assert gate_res.status == ProductionGateStatus.NOT_PRODUCTION_READY


def test_4_different_connection_and_database_resolved_generically():
    """TEST 4:
    Different connection/database
    -> same generic code resolves it
    -> no code modification required
    """
    mapper = ConnectionMapper()

    # Project A: Server A, Database B, Schema C, Table D
    conn_a = {"driver": "sqlserver", "server": "server-a.contoso.com"}
    sql_a = 'SELECT "col1", "col2" FROM "DATABASE_B"."SCHEMA_C"."TABLE_D"'
    m_a = mapper.build_table_mquery("TABLE_D", "source", None, conn_a, sql_a)

    assert 'Sql.Database("server-a.contoso.com", "DATABASE_B")' in m_a
    assert "<DATABASE>" not in m_a
    val_a = validate_m_query(m_a)
    assert val_a["passed"] is True

    # Project X: Server X, Database Y, Schema Z (with brackets notation)
    conn_x = {"driver": "sqlserver", "server": "server-x.fabrikam.com"}
    sql_x = "SELECT col_x FROM [DATABASE_Y].[SCHEMA_Z].[TABLE_W]"
    m_x = mapper.build_table_mquery("TABLE_W", "source", None, conn_x, sql_x)

    assert 'Sql.Database("server-x.fabrikam.com", "DATABASE_Y")' in m_x
    assert "<DATABASE>" not in m_x
    val_x = validate_m_query(m_x)
    assert val_x["passed"] is True

    # Project PostgreSQL: Server PG, Database PG_DB
    conn_pg = {"driver": "postgres", "server": "pg-host.internal"}
    sql_pg = 'SELECT id FROM "PG_DB"."public"."customers"'
    m_pg = mapper.build_table_mquery("customers", "source", None, conn_pg, sql_pg)

    assert 'PostgreSQL.Database("pg-host.internal", "PG_DB")' in m_pg
    assert "<DATABASE>" not in m_pg
    val_pg = validate_m_query(m_pg)
    assert val_pg["passed"] is True


def test_5_database_name_with_spaces_and_special_characters():
    """TEST 5:
    Database name contains spaces/special characters
    -> generated M remains syntactically valid
    """
    mapper = ConnectionMapper()

    # Database with spaces, ampersands, and hyphens in bracketed SQL
    conn = {"driver": "sqlserver", "server": "sql-server-cluster"}
    sql = 'SELECT trans_id FROM [Retail & Sales - 2026!].[dbo].[Transactions]'
    m_query = mapper.build_table_mquery("Transactions", "source", None, conn, sql)

    assert 'Sql.Database("sql-server-cluster", "Retail & Sales - 2026!")' in m_query
    assert "<DATABASE>" not in m_query

    # Validate with both validators
    val = validate_m_query(m_query)
    assert val["passed"] is True, f"validate_m_query errors: {val['errors']}"

    ok, problems = validate_mquery(m_query, "Transactions", [])
    assert ok is True, f"validate_mquery problems: {problems}"

    # Database with quotes needing M escaping
    conn_quotes = {
        "driver": "sqlserver",
        "server": "server01",
        "database": 'Sales"DB',
    }
    m_query_quotes = mapper.build_table_mquery("T", "source", None, conn_quotes, "SELECT 1 FROM T")
    assert 'Sales""DB' in m_query_quotes
    val_quotes = validate_m_query(m_query_quotes)
    assert val_quotes["passed"] is True
