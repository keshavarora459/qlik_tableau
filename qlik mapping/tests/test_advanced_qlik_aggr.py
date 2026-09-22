"""Tests for Advanced Qlik Aggr(), TOTAL modifiers, and Section Access RLS."""

import pytest
from services.dax_converter import DAXConverter
from services.dax_guard import validate
from services.section_access_converter import build_security_contract, convert_section_access

MULTI_TABLES = [
    {
        "name": "Sales",
        "columns": [
            {"name": "SalesID", "qlik_column_name": "SalesID"},
            {"name": "Amount", "qlik_column_name": "Amount"},
            {"name": "Cost", "qlik_column_name": "Cost"},
            {"name": "CustomerID", "qlik_column_name": "CustomerID"},
            {"name": "ProductID", "qlik_column_name": "ProductID"},
            {"name": "Region", "qlik_column_name": "Region"},
            {"name": "Year", "qlik_column_name": "Year"},
        ],
    },
    {
        "name": "Customers",
        "columns": [
            {"name": "CustomerID", "qlik_column_name": "CustomerID"},
            {"name": "CustomerName", "qlik_column_name": "CustomerName"},
            {"name": "CustomerRegion", "qlik_column_name": "CustomerRegion"},
        ],
    },
    {
        "name": "Products",
        "columns": [
            {"name": "ProductID", "qlik_column_name": "ProductID"},
            {"name": "ProductName", "qlik_column_name": "ProductName"},
            {"name": "Category", "qlik_column_name": "Category"},
        ],
    },
]

RELATIONSHIPS = [
    {
        "from_table": "Sales",
        "from_column": "CustomerID",
        "to_table": "Customers",
        "to_column": "CustomerID",
        "cardinality": "manyToOne",
    },
    {
        "from_table": "Sales",
        "from_column": "ProductID",
        "to_table": "Products",
        "to_column": "ProductID",
        "cardinality": "manyToOne",
    },
]


class TestAdvancedAggrAndTotal:
    converter = DAXConverter()

    def test_single_dim_aggr(self):
        source = "Max(Aggr(Sum(Amount), CustomerID))"
        dax = self.converter.qlik_to_dax(source, MULTI_TABLES, RELATIONSHIPS)
        assert dax == 'MAXX(SUMMARIZE(\'Sales\', \'Sales\'[CustomerID], "@value", SUM(\'Sales\'[Amount])), [@value])'
        valid, problems = validate(dax, MULTI_TABLES, is_measure=True)
        assert valid, f"Problems: {problems}"

    def test_multi_dim_aggr(self):
        source = "Avg(Aggr(Sum(Amount), CustomerID, ProductID))"
        dax = self.converter.qlik_to_dax(source, MULTI_TABLES, RELATIONSHIPS)
        assert dax == 'AVERAGEX(SUMMARIZE(\'Sales\', \'Sales\'[CustomerID], \'Sales\'[ProductID], "@value", SUM(\'Sales\'[Amount])), [@value])'
        valid, problems = validate(dax, MULTI_TABLES, is_measure=True)
        assert valid, f"Problems: {problems}"

    def test_multi_table_aggr_resolves_fact_base_table(self):
        source = "Max(Aggr(Sum(Amount), CustomerName, ProductName))"
        dax = self.converter.qlik_to_dax(source, MULTI_TABLES, RELATIONSHIPS)
        assert dax == 'MAXX(SUMMARIZE(\'Sales\', \'Customers\'[CustomerName], \'Products\'[ProductName], "@value", SUM(\'Sales\'[Amount])), [@value])'
        valid, problems = validate(dax, MULTI_TABLES, is_measure=True)
        assert valid, f"Problems: {problems}"

    def test_nested_aggr_inside_if(self):
        source = "If(Max(Aggr(Sum(Amount), CustomerID)) > 1000, 1, 0)"
        dax = self.converter.qlik_to_dax(source, MULTI_TABLES, RELATIONSHIPS)
        assert "MAXX(SUMMARIZE('Sales'" in dax
        assert "> 1000" in dax

    def test_aggr_inside_set_analysis(self):
        source = "Sum({<Status={'Active'}>} Aggr(Sum(Amount), CustomerID))"
        dax = self.converter.qlik_to_dax(source, MULTI_TABLES, RELATIONSHIPS)
        assert "SUMMARIZE('Sales'" in dax

    def test_plain_total_modifier(self):
        source = "Sum(TOTAL Amount)"
        dax = self.converter.qlik_to_dax(source, MULTI_TABLES)
        assert dax == "CALCULATE(SUM('Sales'[Amount]), ALLSELECTED())"
        valid, problems = validate(dax, MULTI_TABLES, is_measure=True)
        assert valid, f"Problems: {problems}"

    def test_total_with_dimension_modifier(self):
        source = "Sum(TOTAL <Region> Amount)"
        dax = self.converter.qlik_to_dax(source, MULTI_TABLES)
        assert dax == "CALCULATE(SUM('Sales'[Amount]), ALLEXCEPT('Sales', 'Sales'[Region]))"
        valid, problems = validate(dax, MULTI_TABLES, is_measure=True)
        assert valid, f"Problems: {problems}"

    def test_total_with_multi_dimension_modifier(self):
        source = "Sum(TOTAL <Region, Year> Amount)"
        dax = self.converter.qlik_to_dax(source, MULTI_TABLES)
        assert dax == "CALCULATE(SUM('Sales'[Amount]), ALLEXCEPT('Sales', 'Sales'[Region], 'Sales'[Year]))"
        valid, problems = validate(dax, MULTI_TABLES, is_measure=True)
        assert valid, f"Problems: {problems}"

    def test_count_distinct_total_with_dimension(self):
        source = "Count(distinct TOTAL <Region> CustomerID)"
        dax = self.converter.qlik_to_dax(source, MULTI_TABLES)
        assert dax == "CALCULATE(DISTINCTCOUNT('Sales'[CustomerID]), ALLEXCEPT('Sales', 'Sales'[Region]))"
        valid, problems = validate(dax, MULTI_TABLES, is_measure=True)
        assert valid, f"Problems: {problems}"


class TestSectionAccessRLS:
    def test_basic_section_access_conversion(self):
        sec_access = {
            "fields": ["ACCESS", "USERID", "Region"],
            "rows": [
                {"ACCESS": "USER", "USERID": "DOMAIN\\user1", "Region": "North"},
                {"ACCESS": "USER", "USERID": "DOMAIN\\user2", "Region": "South"},
            ],
        }
        roles = convert_section_access(sec_access, MULTI_TABLES)
        assert len(roles) == 1
        role = roles[0]
        assert role["dimension_field"] == "Region"
        assert role["table_name"] == "Sales"
        assert "USERPRINCIPALNAME()" in role["dax_filter"]
        assert '|| \'Sales\'[Region] = "*"' in role["dax_filter"]

    def test_section_access_contract_generation(self):
        sec_access = {
            "fields": ["ACCESS", "NTNAME", "Region"],
            "rows": [{"ACCESS": "ADMIN", "NTNAME": "INTERNAL\\admin", "Region": "*"}],
        }
        contract = build_security_contract(sec_access, MULTI_TABLES)
        assert contract["type"] == "rls"
        assert contract["source"] == "qlik_section_access"
        assert contract["review_required"] is False
        assert len(contract["roles"]) == 1

    def test_unsupported_section_access_flags_review(self):
        sec_access = {
            "fields": ["ACCESS", "USERID", "OMIT", "Region"],
            "rows": [{"ACCESS": "USER", "USERID": "user1", "OMIT": "Cost", "Region": "North"}],
        }
        contract = build_security_contract(sec_access, MULTI_TABLES)
        assert contract["review_required"] is True
        assert any("OMIT" in r for r in contract["unsupported_reasons"])
