"""Comprehensive Test Suite for Qlik -> Power BI Migration Hardening (30 Requirements).

Covers:
1. Source Resolution & Power Query M generation (lib:// paths, QVD handling, XLSX vs CSV, identifier escaping)
2. Mapping Tables, Nested APPLYMAP, and Date Function matrix
3. Model Relationships, Inferrer, and Measure Reachability
4. DAX Table/Column Schema Binding Validation
5. Table & Measure Reconciliation Engine
6. Production Gate & Migration Status Object Schema
7. End-to-end CoordinatorAgent execution with validation gates
"""

import pytest
from services.connectors.source_resolver import SourceResolver, ResolvedSource
from services.mapping_table_registry import MappingTableRegistry
from services.qlik_function_matrix import QlikFunctionMatrix
from services.relationship_inferrer import RelationshipInferrer
from services.validators.dax_validators import validate_dax_schema_binding
from services.reconciliation_engine import ReconciliationEngine
from services.production_gate import ProductionGate, ProductionGateStatus
from services.connection_mapper import ConnectionMapper
from agents.coordinator_agent import CoordinatorAgent


# ============================================================================
# 1. Source Resolution & M Generation Tests
# ============================================================================

def test_source_resolver_lib_path_resolution():
    resolver = SourceResolver(connection_catalog={
        "Executive Data": {
            "type": "azure_blob",
            "account": "myacct",
            "container": "finance"
        }
    })

    # Azure Blob resolution
    res = resolver.resolve_source("lib://Executive Data/Transactions.csv")
    assert res.connector_type in ("azure_blob", "blob")
    assert "AzureStorage.Blobs" in res.m_expression
    assert "lib://" not in res.m_expression
    assert res.file_extension == ".csv"
    assert "Csv.Document" in res.m_expression

    # Local / UNC Path resolution
    res_unc = resolver.resolve_source(r"\\fileserver\data\reports\Sales.xlsx")
    assert res_unc.connector_type in ("file", "excel")
    assert "Excel.Workbook" in res_unc.m_expression
    assert res_unc.file_extension == ".xlsx"


def test_source_resolver_qvd_handling_never_csv():
    resolver = SourceResolver()

    # Unresolved QVD -> REVIEW_REQUIRED, NOT Csv.Document
    res_qvd = resolver.resolve_source("lib://DataPool/AccountMaster.qvd")
    assert res_qvd.connector_type == "qvd"
    assert res_qvd.requires_review is True
    assert "Csv.Document" not in res_qvd.m_expression
    assert "REVIEW_REQUIRED" in res_qvd.m_expression
    assert "QVD" in res_qvd.blocking_reason

    # Resolved QVD with upstream SQL fallback
    conn_mapper = ConnectionMapper()
    m_code = conn_mapper.build_table_mquery(
        table_name="AccountMaster",
        connection={"type": "sqlserver", "server": "sql.corp.local", "database": "DW"},
        custom_sql="SELECT * FROM dbo.AccountMaster",
        qlik_query="LOAD * FROM [lib://DataPool/AccountMaster.qvd] (qvd);",
        columns=[{"name": "AccountId"}, {"name": "AccountName"}]
    )
    assert "Sql.Database" in m_code
    assert "Csv.Document" not in m_code
    assert "lib://" not in m_code


def test_source_resolver_identifier_escaping():
    # Hyphenated names
    assert SourceResolver.escape_identifier("ARSummary-1") == '#"ARSummary-1"'
    # Spaces
    assert SourceResolver.escape_identifier("Order Details") == '#"Order Details"'
    # Reserved keywords
    assert SourceResolver.escape_identifier("Table") == '#"Table"'
    assert SourceResolver.escape_identifier("each") == '#"each"'
    # Plain simple name
    assert SourceResolver.escape_identifier("Orders") == "Orders"


def test_excel_vs_csv_source_selection():
    conn_mapper = ConnectionMapper()
    
    # XLSX
    m_xlsx = conn_mapper.build_table_mquery(
        table_name="Financials",
        connection={"type": "folder", "path": "C:/Reports"},
        qlik_query="LOAD * FROM [C:/Reports/Financials.xlsx] (ooxml, embedded labels, table is Sheet1);",
        columns=[{"name": "Date"}, {"name": "Revenue"}]
    )
    assert "Excel.Workbook" in m_xlsx
    assert "Csv.Document" not in m_xlsx

    # CSV
    m_csv = conn_mapper.build_table_mquery(
        table_name="Logs",
        connection={"type": "folder", "path": "C:/Logs"},
        qlik_query="LOAD * FROM [C:/Logs/access.csv] (txt, utf8, embedded labels, delimiter is ',');",
        columns=[{"name": "Timestamp"}, {"name": "IP"}]
    )
    assert "Csv.Document" in m_csv
    assert "Excel.Workbook" not in m_csv


# ============================================================================
# 2. Mapping Tables, Nested APPLYMAP & Function Matrix
# ============================================================================

def test_nested_applymap_translation():
    registry = MappingTableRegistry()
    registry.register_mapping(
        map_name="CategoryMap",
        source_table="Categories",
        key_column="CatID",
        value_column="CatName"
    )
    registry.register_mapping(
        map_name="RegionMap",
        source_table="Regions",
        key_column="RegCode",
        value_column="RegName"
    )

    qlik_expr = "ApplyMap('RegionMap', ApplyMap('CategoryMap', ItemCode, 'UnknownCat'), 'UnknownRegion')"
    dax_out = registry.translate_applymap_expression(qlik_expr)
    
    # Should resolve inside-out without crash
    assert "COALESCE" in dax_out or "LOOKUPVALUE" in dax_out
    assert "Regions" in dax_out or "Categories" in dax_out


def test_qlik_function_matrix_dates_and_semantics():
    # Temporal M conversion
    m_date = QlikFunctionMatrix.qlik_date_to_m("Date#(TxDate, 'YYYY-MM-DD')")
    assert "Date.FromText" in m_date

    m_addmonths = QlikFunctionMatrix.qlik_date_to_m("AddMonths(OrderDate, 3)")
    assert "Date.AddMonths" in m_addmonths

    # Temporal DAX conversion
    dax_date = QlikFunctionMatrix.qlik_date_to_dax("Date#(TxDate, 'YYYY-MM-DD')")
    assert "DATEVALUE" in dax_date

    dax_addmonths = QlikFunctionMatrix.qlik_date_to_dax("AddMonths(OrderDate, 3)")
    assert "EDATE" in dax_addmonths

    # Semantic summarize-by inference
    assert QlikFunctionMatrix.infer_summarize_by("CustomerID", "INTEGER") == "none"
    assert QlikFunctionMatrix.infer_summarize_by("TransactionDate", "DATE") == "none"
    assert QlikFunctionMatrix.infer_summarize_by("TotalRevenue", "DECIMAL") == "sum"
    assert QlikFunctionMatrix.infer_summarize_by("GrossMarginPercent", "FLOAT") in ("average", "none")


# ============================================================================
# 3. Model Relationships, Inferrer & Reachability
# ============================================================================

def test_relationship_inferrer_and_reachability():
    tables = [
        {
            "name": "Orders",
            "columns": [
                {"fabric_column_name": "OrderID"},
                {"fabric_column_name": "CustomerID"},
                {"fabric_column_name": "Amount"}
            ]
        },
        {
            "name": "Customers",
            "columns": [
                {"fabric_column_name": "CustomerID"},
                {"fabric_column_name": "CustomerName"}
            ]
        },
        {
            "name": "IsolatedLog",
            "columns": [
                {"fabric_column_name": "LogID"},
                {"fabric_column_name": "Payload"}
            ]
        }
    ]

    # Infer relationships
    inferred = RelationshipInferrer.infer_relationships(tables)
    assert len(inferred) >= 1
    rel = inferred[0]
    assert (rel["from_table"] == "Orders" and rel["to_table"] == "Customers") or (rel["from_table"] == "Customers" and rel["to_table"] == "Orders")

    # Measure Reachability
    measures = [
        {
            "name": "CustomerSpend",
            "dax_expression": "CALCULATE(SUM('Orders'[Amount]), 'Customers'[CustomerName] = \"Acme\")"
        },
        {
            "name": "InvalidCrossTable",
            "dax_expression": "CALCULATE(SUM('Orders'[Amount]), 'IsolatedLog'[Payload] = \"Test\")"
        }
    ]

    reachability = RelationshipInferrer.validate_measure_reachability(measures, tables, inferred)
    assert reachability["reachable"] is False
    assert len(reachability["unreachable_measures"]) >= 1
    assert reachability["unreachable_measures"][0]["measure"] == "InvalidCrossTable"


# ============================================================================
# 4. DAX Schema Binding Validator Tests
# ============================================================================

def test_dax_schema_binding_validator():
    tables = [
        {
            "name": "Sales",
            "columns": [
                {"fabric_column_name": "Revenue"},
                {"fabric_column_name": "Quantity"}
            ]
        }
    ]

    valid_measures = [
        {"name": "TotalRevenue", "dax_expression": "SUM('Sales'[Revenue])"}
    ]
    res_valid = validate_dax_schema_binding(valid_measures, tables)
    assert res_valid["valid"] is True
    assert len(res_valid["errors"]) == 0

    invalid_measures = [
        {"name": "TotalMargin", "dax_expression": "SUM('Sales'[ProfitMargin])"},
        {"name": "UnknownTableSum", "dax_expression": "SUM('Inventory'[Units])"}
    ]
    res_invalid = validate_dax_schema_binding(invalid_measures, tables)
    assert res_invalid["valid"] is False
    assert len(res_invalid["errors"]) == 2
    err_msgs = [e["error"] for e in res_invalid["errors"]]
    assert any("ProfitMargin" in msg for msg in err_msgs)
    assert any("Inventory" in msg for msg in err_msgs)


# ============================================================================
# 5. Reconciliation Engine Tests
# ============================================================================

def test_reconciliation_engine():
    # Table Level
    src_stats = {"row_count": 1000, "columns": {"Amount": {"distinct_count": 500, "null_count": 0, "sum": 50000.0}}}
    tgt_stats = {"row_count": 1000, "columns": {"Amount": {"distinct_count": 500, "null_count": 0, "sum": 50000.0}}}
    tbl_rec = ReconciliationEngine.reconcile_table("Sales", src_stats, tgt_stats)
    assert tbl_rec["reconciled"] is True
    assert tbl_rec["variance_rows"] == 0

    # Measure Level
    meas_rec = ReconciliationEngine.reconcile_measure("Total Sales", qlik_value=100000.0, powerbi_value=100000.05, tolerance_pct=0.001)
    assert meas_rec["reconciled"] is True
    assert meas_rec["status"] == "passed"


# ============================================================================
# 6. Production Gate & Migration Status Schema Tests (Req 30)
# ============================================================================

def test_production_gate_and_migration_status_schema():
    # Valid payload
    payload = {
        "tables": [
            {
                "name": "Orders",
                "columns": [{"fabric_column_name": "OrderID"}, {"fabric_column_name": "Amount"}],
                "m_query": "let Source = Sql.Database(\"srv\", \"db\") in Source"
            }
        ],
        "measures": [
            {
                "name": "TotalAmount",
                "dax_expression": "SUM('Orders'[Amount])",
                "conversion_method": "deterministic"
            }
        ],
        "relationships": [],
        "visuals": {
            "sheet_visuals": [
                {
                    "name": "RevenueCard",
                    "fabric": {"visual_type": "card", "supported": True, "field_roles": {"Fields": ["TotalAmount"]}}
                }
            ]
        }
    }

    gate_res = ProductionGate.evaluate(payload)
    assert gate_res.status == ProductionGateStatus.PRODUCTION_READY
    status_obj = gate_res.migration_status

    # Verify Requirement 30 exact schema keys
    assert status_obj["conversion_status"] == "converted"
    assert status_obj["m_validation"] == "passed"
    assert status_obj["model_validation"] == "passed"
    assert status_obj["dax_validation"] == "passed"
    assert status_obj["visual_validation"] == "passed"
    assert status_obj["publish_ready"] is True
    assert len(status_obj["blocking_reasons"]) == 0

    # Invalid payload (contains raw lib:// and placeholder M)
    bad_payload = {
        "tables": [
            {
                "name": "BadTable",
                "columns": [{"fabric_column_name": "ID"}],
                "m_query": "let Source = Folder.Files(\"'lib://DataPool'\") in #table({\"*\"}, {})"
            }
        ],
        "measures": [],
        "relationships": [],
        "visuals": {}
    }
    bad_gate = ProductionGate.evaluate(bad_payload)
    assert bad_gate.status == ProductionGateStatus.NOT_PRODUCTION_READY
    assert bad_gate.migration_status["publish_ready"] is False
    assert bad_gate.migration_status["m_validation"] == "failed"
    assert len(bad_gate.migration_status["blocking_reasons"]) >= 1


# ============================================================================
# 7. End-to-End CoordinatorAgent Execution with Hardened Pipeline
# ============================================================================

@pytest.mark.asyncio
async def test_coordinator_agent_hardened_pipeline():
    coordinator = CoordinatorAgent()
    input_data = {
        "app_id": "app-hardened-test-01",
        "app_name": "Enterprise Hardened Test App",
        "tables": [
            {
                "name": "Invoices",
                "load_type": "source",
                "fields": [
                    {"name": "InvoiceID", "dataType": "STRING"},
                    {"name": "ClientID", "dataType": "STRING"},
                    {"name": "InvoiceDate", "dataType": "DATE"},
                    {"name": "Amount", "dataType": "DECIMAL"}
                ],
                "connection": {"type": "sqlserver", "server": "sql01.corp", "database": "FinanceDB"}
            },
            {
                "name": "Clients",
                "load_type": "source",
                "fields": [
                    {"name": "ClientID", "dataType": "STRING"},
                    {"name": "ClientName", "dataType": "STRING"}
                ],
                "connection": {"type": "sqlserver", "server": "sql01.corp", "database": "FinanceDB"}
            }
        ],
        "measures": [
            {
                "name": "TotalInvoiced",
                "expression": "Sum(Amount)"
            }
        ],
        "visualizations": [
            {
                "id": "vis_1",
                "title": "Total Invoices by Client",
                "type": "barchart",
                "dimensions": ["ClientName"],
                "measures": ["TotalInvoiced"],
                "sheet_name": "Overview"
            }
        ]
    }

    result = await coordinator.process_data(input_data)
    assert result["status"] == "success"
    assert "migration_status" in result
    assert "production_gate" in result

    # Check relationships were inferred dynamically
    assert len(result["relationships"]) >= 1
    rel = result["relationships"][0]
    assert rel["source_table"] in ("Invoices", "Clients")
    assert rel["target_table"] in ("Invoices", "Clients")

    # Check migration status
    m_stat = result["migration_status"]
    assert m_stat["m_validation"] == "passed"
    assert m_stat["model_validation"] == "passed"
    assert m_stat["dax_validation"] == "passed"
    assert m_stat["publish_ready"] is True
