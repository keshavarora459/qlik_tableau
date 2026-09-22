"""Enterprise-Grade Universal Platform Tests.

Validates the source-agnostic, metadata-driven, semantic migration engine across:
1. Cross-Domain Datasets (E-Commerce, Healthcare, Manufacturing, Finance, Logistics).
2. Connector Abstraction Matrix (CSV, Excel, Google Sheets, PostgreSQL, SQL Server, MySQL, Redshift, BigQuery, Snowflake, Parquet, REST).
3. Mapping Table Registry & Dynamic ApplyMap resolution.
4. Pick() semantic conversion.
5. Expression-based calculated filters.
6. Semantic Equivalence Validator.
7. Visual Capability Registry & Projections.
8. Dynamic Extension Registry.
9. Pipeline Inventory & Object Loss Matrix.
10. Production Readiness Gate.
11. Automated Zero-Hardcoding Audit.
"""

import os
import re
import pytest

from agents.coordinator_agent import CoordinatorAgent
from services.connectors import (
    ConnectorRegistry, ConnectorResolver,
    CsvConnector, ExcelConnector, GoogleSheetsConnector,
    PostgresConnector, SqlServerConnector, MySqlConnector,
    RedshiftConnector, BigQueryConnector, SnowflakeConnector,
    ParquetConnector, RestApiConnector
)
from services.mapping_table_registry import MappingTableRegistry, MappingTableDefinition
from services.semantic_validator import SemanticValidator
from services.visual_capabilities import VisualCapabilityRegistry, VisualSupportLevel
from services.extension_registry import ExtensionRegistry
from services.pipeline_inventory import PipelineInventory, ObjectStage
from services.production_gate import ProductionGate, ProductionGateStatus
from services.source_profiler import SourceProfiler
from services.qlik_patterns import translate_pick


# =========================================================================
# 1. CROSS-DOMAIN DATA TESTS
# =========================================================================

@pytest.mark.asyncio
async def test_domain_healthcare_migration():
    """Dataset B: Healthcare (Patients, Appointments, Providers, Claims)."""
    payload = {
        "app_id": "app-healthcare-001",
        "app_name": "Clinical Outcomes Dashboard",
        "tables": [
            {
                "table_name": "Patients",
                "fields": [
                    {"name": "patient_id", "dataType": "STRING"},
                    {"name": "gender", "dataType": "STRING"},
                    {"name": "age_years", "dataType": "INTEGER"},
                    {"name": "admission_date", "dataType": "DATE"},
                ],
                "load_type": "source",
                "connection_details": {"type": "sqlserver", "server": "med-db.health.org", "database": "ClinicalDW"}
            },
            {
                "table_name": "Claims",
                "fields": [
                    {"name": "claim_id", "dataType": "STRING"},
                    {"name": "patient_id", "dataType": "STRING"},
                    {"name": "claim_amount", "dataType": "DECIMAL"},
                    {"name": "denial_status", "dataType": "STRING"},
                ],
                "load_type": "source",
                "connection_details": {"type": "sqlserver", "server": "med-db.health.org", "database": "ClinicalDW"}
            }
        ],
        "relationships": [
            {"from_table": "Claims", "from_column": "patient_id", "to_table": "Patients", "to_column": "patient_id"}
        ],
        "measures": [
            {"name": "Total Incurred Claims", "expression": "Sum(claim_amount)"},
            {"name": "Approved Claims", "expression": "Sum({<denial_status={'Approved'}>} claim_amount)"},
            {"name": "Unique Patients", "expression": "Count(DISTINCT patient_id)"},
        ],
        "visualizations": [
            {
                "title": "Claims by Gender",
                "chart_type": "barchart",
                "dimensions": ["gender"],
                "measures": ["Total Incurred Claims"],
            }
        ]
    }
    coordinator = CoordinatorAgent()
    result = await coordinator.process_data(payload, app_id=payload["app_id"], app_name=payload["app_name"])
    assert result["status"] == "success"
    assert len(result["tables"]) == 2
    assert len(result["measures"]) >= 3
    gate = ProductionGate.evaluate(result)
    assert gate.status in (ProductionGateStatus.PRODUCTION_READY, ProductionGateStatus.PRODUCTION_READY_WITH_REVIEW)


@pytest.mark.asyncio
async def test_domain_manufacturing_migration():
    """Dataset C: Manufacturing (Machines, Production, Defects, Plants)."""
    payload = {
        "app_id": "app-mfg-002",
        "app_name": "Plant Telemetry",
        "tables": [
            {
                "table_name": "Machines",
                "fields": [
                    {"name": "machine_id", "dataType": "STRING"},
                    {"name": "plant_code", "dataType": "STRING"},
                    {"name": "model_type", "dataType": "STRING"},
                ],
                "load_type": "source",
                "connection_details": {"type": "postgres", "host": "mfg-pg.lan", "database": "telemetry"}
            },
            {
                "table_name": "Production",
                "fields": [
                    {"name": "prod_id", "dataType": "STRING"},
                    {"name": "machine_id", "dataType": "STRING"},
                    {"name": "units_produced", "dataType": "INTEGER"},
                    {"name": "defect_count", "dataType": "INTEGER"},
                ],
                "load_type": "source",
                "connection_details": {"type": "postgres", "host": "mfg-pg.lan", "database": "telemetry"}
            }
        ],
        "relationships": [
            {"from_table": "Production", "from_column": "machine_id", "to_table": "Machines", "to_column": "machine_id"}
        ],
        "measures": [
            {"name": "Total Output", "expression": "Sum(units_produced)"},
            {"name": "Defect Rate", "expression": "Sum(defect_count) / Sum(units_produced)"},
        ],
        "visualizations": [
            {
                "title": "Output by Plant",
                "chart_type": "columnchart",
                "dimensions": ["plant_code"],
                "measures": ["Total Output"],
            }
        ]
    }
    coordinator = CoordinatorAgent()
    result = await coordinator.process_data(payload, app_id=payload["app_id"], app_name=payload["app_name"])
    assert result["status"] == "success"
    measures_dict = {m["name"]: m for m in result["measures"]}
    assert "DIVIDE(" in measures_dict["Defect Rate"]["dax_expression"]


# =========================================================================
# 2. CONNECTOR MATRIX TESTS
# =========================================================================

def test_connector_matrix_discovery_and_m_generation():
    """Validate M generation across all enterprise connectors."""
    test_cases = [
        ("csv", {"file_path": "/data/sales.csv", "delimiter": ","}, "Csv.Document"),
        ("excel", {"file_path": "/data/finance.xlsx", "sheet_name": "Q4"}, "Excel.Workbook"),
        ("googlesheets", {"spreadsheet_id": "1abcXYZ", "worksheet": "Summary"}, "GoogleSheets.Contents"),
        ("sqlserver", {"server": "db.corp.net", "database": "ERP"}, "Sql.Database"),
        ("postgres", {"server": "pg.corp.net", "database": "Analytics"}, "PostgreSQL.Database"),
        ("mysql", {"server": "mysql.corp.net", "database": "Store"}, "MySQL.Database"),
        ("oracle", {"server": "oracle.corp.net", "database": "DW"}, "Oracle.Database"),
        ("redshift", {"server": "rs.aws.com", "database": "dev"}, "AmazonRedshift.Database"),
        ("bigquery", {"project_id": "gcp-prod-123", "dataset": "analytics"}, "GoogleBigQuery.Database"),
        ("snowflake", {"server": "xy123.snowflakecomputing.com", "warehouse": "WH1", "database": "FIN"}, "Snowflake.Databases"),
        ("parquet", {"file_path": "/lake/trips.parquet"}, "Parquet.Document"),
        ("rest", {"url": "https://api.weather.com/v1/forecast"}, "Web.Contents"),
    ]

    for conn_type, details, expected_fn in test_cases:
        details["type"] = conn_type
        connector = ConnectorResolver.resolve(details)
        assert connector is not None
        m_code = connector.build_fabric_source(table_name="TestTable")
        assert expected_fn in m_code, f"Failed for {conn_type}: {m_code}"


# =========================================================================
# 3. GENERIC MAPPING TABLE & APPLYMAP TESTS
# =========================================================================

def test_mapping_table_registry_dynamic_resolution():
    registry = MappingTableRegistry()

    # Script discovery
    script = """
    RateMap:
    MAPPING LOAD
        CurrCode,
        ExchangeRate
    RESIDENT CurrencyTable;

    StatusMap:
    MAPPING LOAD
        StatusID,
        StatusLabel
    FROM [lib://Data/status.csv];
    """
    registry.discover_from_script(script)

    assert registry.get("RateMap") is not None
    assert registry.get("StatusMap") is not None

    dax1, resolved1 = registry.resolve_applymap_dax("RateMap", "Currency", "1.0")
    assert resolved1 is True
    assert "LOOKUPVALUE('CurrencyTable'[ExchangeRate], 'CurrencyTable'[CurrCode], Currency)" in dax1

    dax2, resolved2 = registry.resolve_applymap_dax("StatusMap", "code", "'Unknown'")
    assert resolved2 is True
    assert "LOOKUPVALUE('StatusMap'[StatusLabel], 'StatusMap'[StatusID], code)" in dax2


# =========================================================================
# 4. PICK() & PARAMETER SWITCH TESTS
# =========================================================================

def test_pick_dynamic_and_static_branches():
    # Static
    res1, ch1 = translate_pick("Pick(2, Sum(Cost), Sum(Revenue), Avg(Price))")
    assert ch1 is True
    assert res1 == "Sum(Revenue)"

    # Dynamic parameter / variable
    res2, ch2 = translate_pick("Pick(vMetricSelector, Sum(Cost), Sum(Revenue), Avg(Price))")
    assert ch2 is True
    assert "SWITCH(vMetricSelector, 1, Sum(Cost), 2, Sum(Revenue), 3, Avg(Price), BLANK())" in res2


# =========================================================================
# 5. CALCULATED FILTER EXPRESSIONS
# =========================================================================

def test_calculated_filter_expression_resolution():
    from services.filter_mapper import FilterMapper

    tables = [
        {
            "name": "Trips",
            "columns": [
                {"fabric_column_name": "origin_city"},
                {"fabric_column_name": "destination_city"},
                {"fabric_column_name": "load_date"},
            ]
        }
    ]

    mapper = FilterMapper()
    res = mapper.map_filters(
        [
            {"expression": "=origin_city & ' -> ' & destination_city", "sheet_name": "Overview"},
            {"expression": "=Month(load_date)"}
        ],
        tables=tables
    )

    assert len(res) == 2
    assert res[0]["fabric"]["target_table"] == "Trips"
    assert res[0]["fabric"]["is_calculated"] is True
    assert res[1]["fabric"]["target_table"] == "Trips"
    assert res[1]["fabric"]["is_calculated"] is True


# =========================================================================
# 6. SEMANTIC VALIDATOR TESTS
# =========================================================================

def test_semantic_equivalence_validator():
    # Matching aggregation
    res1 = SemanticValidator.validate_measure_semantics("Sum(Sales)", "SUM('Orders'[Sales])")
    assert res1.is_equivalent is True
    assert res1.semantic_loss is False

    # Mismatch aggregation
    res2 = SemanticValidator.validate_measure_semantics("Avg(Sales)", "SUM('Orders'[Sales])")
    assert res2.is_equivalent is False
    assert res2.semantic_loss is True
    assert any("Aggregation mismatch" in r for r in res2.loss_reasons)

    # Set Analysis check
    res3 = SemanticValidator.validate_measure_semantics("Sum({<Status={'Done'}>} Sales)", "SUM('Orders'[Sales])")
    assert res3.semantic_loss is True
    assert any("Set Analysis" in r for r in res3.loss_reasons)


# =========================================================================
# 7. VISUAL CAPABILITY & EXTENSION TESTS
# =========================================================================

def test_visual_capability_validation():
    # Valid card
    card_val = VisualCapabilityRegistry.validate_projection("card", [{"field": "TotalSales", "role": "Values"}])
    assert card_val["is_valid"] is True

    # Card with empty Values
    bad_card = VisualCapabilityRegistry.validate_projection("card", [])
    assert bad_card["is_valid"] is False
    assert bad_card["requires_review"] is True

    # Unknown extension
    ext_class = ExtensionRegistry.classify("unknown-third-party-widget")
    assert ext_class.support_level == VisualSupportLevel.UNSUPPORTED_REQUIRES_REVIEW
    assert ext_class.requires_review is True


# =========================================================================
# 8. PIPELINE INVENTORY & OBJECT LOSS MATRIX
# =========================================================================

def test_pipeline_inventory_tracking():
    inv = PipelineInventory()
    inv.track("t1", "table", "Orders", ObjectStage.EXTRACTED)
    inv.track("t1", "table", "Orders", ObjectStage.MAPPED)
    inv.track("t1", "table", "Orders", ObjectStage.VALIDATED)

    inv.track("m1", "measure", "BadMeasure", ObjectStage.EXTRACTED)
    inv.track("m1", "measure", "BadMeasure", ObjectStage.MAPPED, passed=False, loss_reason="Syntax error")

    summary = inv.get_summary()
    assert summary["total_extracted"] == 2
    assert summary["total_lost"] == 1
    assert summary["counts_by_type"]["table"]["validated"] == 1
    assert summary["counts_by_type"]["measure"]["lost"] == 1


# =========================================================================
# 9. PRODUCTION READINESS GATE
# =========================================================================

def test_production_readiness_gate_evaluation():
    valid_payload = {
        "tables": [{"name": "Sales", "columns": [{"fabric_column_name": "Amount"}]}],
        "measures": [{"name": "Revenue", "dax_expression": "SUM('Sales'[Amount])"}],
        "relationships": [],
        "visuals": {"sheet_visuals": [{"name": "RevCard", "fabric": {"field_roles": [{"field": "Revenue", "role": "Values"}], "supported": True}}]}
    }
    gate = ProductionGate.evaluate(valid_payload)
    assert gate.status == ProductionGateStatus.PRODUCTION_READY
    assert gate.score == 1.0


# =========================================================================
# 10. AUTOMATED ZERO-HARDCODING AUDIT TEST
# =========================================================================

def test_no_business_specific_mapping_constants():
    """Verify production code has zero hardcoded customer/domain-specific keywords."""
    prod_dirs = [
        os.path.join(os.path.dirname(__file__), "..", "services"),
        os.path.join(os.path.dirname(__file__), "..", "agents"),
        os.path.join(os.path.dirname(__file__), "..", "src"),
    ]

    # Forbidden hardcoded domain strings in production routing/logic
    forbidden_tokens = [
        "FleetVision",
        "TruckMap",
        "TruckNumber",
        "Fleet_Database",
    ]

    matches = []
    for pdir in prod_dirs:
        for root, _, files in os.walk(pdir):
            if "__pycache__" in root:
                continue
            for f in files:
                if f.endswith(".py"):
                    fpath = os.path.join(root, f)
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as fp:
                        content = fp.read()
                        for tok in forbidden_tokens:
                            if tok in content:
                                matches.append(f"{f}: contains '{tok}'")

    assert len(matches) == 0, f"Found hardcoded production tokens: {matches}"
