"""Test Suite for Unified Common IR & Canonical Contract 2.0."""

import pytest
from services.common_ir import (
    CanonicalContract2,
    ColumnIR,
    ContractNormalizer,
    ContractValidator,
    FieldIR,
    MeasureIR,
    QlikContractAdapter,
    RelationshipIR,
    TableIR,
    TableauContractAdapter,
)


def test_qlik_contract_adapter():
    payload = {
        "app_id": "qlik_app_1",
        "app_name": "Sales Performance",
        "tables": [
            {
                "table_name": "Sales",
                "fields": [
                    {"name": "Amount", "dataType": "NUMERIC"},
                    {"name": "Date", "dataType": "DATE"},
                ],
            }
        ],
        "measures": [
            {
                "qInfo": {"qId": "m1"},
                "qMeasure": {"qLabel": "Total Sales", "qDef": "Sum(Amount)"},
            }
        ],
        "relationships": [
            {
                "table1": "Sales",
                "field1": "Date",
                "table2": "Calendar",
                "field2": "Date",
                "cardinality": "many-to-one",
            }
        ],
    }

    contract = QlikContractAdapter.adapt(payload)
    assert isinstance(contract, CanonicalContract2)
    assert contract.contract_version == "2.0"
    assert len(contract.tables) == 1
    assert contract.tables[0].canonical_name == "Sales"
    assert len(contract.measures) == 1
    assert contract.measures[0].name == "Total Sales"
    assert contract.measures[0].source_platform == "qlik"
    assert len(contract.relationships) == 1
    assert contract.relationships[0].cardinality == "many_to_one"


def test_tableau_contract_adapter():
    payload = {
        "workbook_id": "tab_wb_1",
        "workbook_name": "Executive Dashboard",
        "tables": [
            {
                "table_name": "Orders",
                "columns": [
                    {"name": "Sales", "dataType": "REAL"},
                    {"name": "Order Date", "dataType": "DATE"},
                ],
            }
        ],
        "measures": [
            {
                "name": "Total Sales",
                "expression": "SUM('Orders'[Sales])",
                "source_expression": "SUM([Sales])",
            }
        ],
        "parameters": [
            {
                "name": "Select Measure",
                "datatype": "string",
                "default_value": "Sales",
                "allowed_values": ["Sales", "Profit"],
            }
        ],
        "sets": [
            {
                "set_id": "set_top_cust",
                "name": "Top Customers",
                "field": "Customer ID",
                "operation": "include",
            }
        ],
    }

    contract = TableauContractAdapter.adapt(payload)
    assert isinstance(contract, CanonicalContract2)
    assert contract.contract_version == "2.0"
    assert len(contract.tables) == 1
    assert len(contract.measures) == 1
    assert contract.measures[0].source_platform == "tableau"
    assert len(contract.parameters) == 1
    assert contract.parameters[0].name == "Select Measure"
    assert len(contract.sets) == 1
    assert contract.sets[0].name == "Top Customers"


def test_contract_validator_valid():
    contract = CanonicalContract2(
        tables=[
            TableIR(
                table_id="tbl_sales",
                source_name="Sales",
                canonical_name="Sales",
                columns=[
                    ColumnIR(name="SalesID", canonical_name="SalesID"),
                    ColumnIR(name="Amount", canonical_name="Amount"),
                ],
            )
        ],
        measures=[
            MeasureIR(
                name="Total Sales",
                expression="SUM('Sales'[Amount])",
                source_expression="Sum(Amount)",
                source_platform="qlik",
            )
        ],
    )

    result = ContractValidator.validate(contract)
    assert result["valid"] is True
    assert result["confidence_score"] == 100.0
    assert len(result["errors"]) == 0


def test_contract_validator_unbalanced_parentheses():
    contract = CanonicalContract2(
        tables=[
            TableIR(
                table_id="tbl_sales",
                source_name="Sales",
                canonical_name="Sales",
                columns=[ColumnIR(name="Amount", canonical_name="Amount")],
            )
        ],
        measures=[
            MeasureIR(
                name="Broken Measure",
                expression="CALCULATE(SUM('Sales'[Amount])",
                source_platform="qlik",
            )
        ],
    )

    result = ContractValidator.validate(contract)
    assert result["valid"] is False
    assert any("unbalanced parentheses" in err.lower() for err in result["errors"])
    assert result["review_required"] is True


def test_cross_platform_semantic_equivalence():
    qlik_payload = {
        "tables": [{"table_name": "Sales", "fields": [{"name": "Amount", "dataType": "NUMERIC"}]}],
        "measures": [{"name": "Revenue", "expression": "SUM('Sales'[Amount])", "source_expression": "Sum(Amount)"}],
        "source_platform": "qlik",
    }
    tableau_payload = {
        "tables": [{"table_name": "Sales", "columns": [{"name": "Amount", "dataType": "NUMERIC"}]}],
        "measures": [{"name": "Revenue", "expression": "SUM('Sales'[Amount])", "source_expression": "SUM([Amount])"}],
        "source_platform": "tableau",
    }

    contract_qlik = ContractNormalizer.normalize(qlik_payload)
    contract_tab = ContractNormalizer.normalize(tableau_payload)

    assert contract_qlik.measures[0].expression == contract_tab.measures[0].expression
    assert contract_qlik.measures[0].name == contract_tab.measures[0].name
    assert contract_qlik.tables[0].canonical_name == contract_tab.tables[0].canonical_name
