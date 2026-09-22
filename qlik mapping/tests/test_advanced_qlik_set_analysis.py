"""Tests for Advanced Qlik Set Analysis conversion to DAX."""

import pytest
from services.dax_converter import DAXConverter
from services.dax_guard import validate
from services.set_analysis_ast import SetAnalysisParser, SetModifierType

TABLES = [
    {
        "name": "Sales",
        "columns": [
            {"name": "Amount", "qlik_column_name": "Amount"},
            {"name": "Cost", "qlik_column_name": "Cost"},
            {"name": "Quantity", "qlik_column_name": "Quantity"},
            {"name": "Status", "qlik_column_name": "Status"},
            {"name": "Year", "qlik_column_name": "Year"},
            {"name": "CustomerID", "qlik_column_name": "CustomerID"},
            {"name": "OrderDate", "qlik_column_name": "OrderDate"},
            {"name": "Region", "qlik_column_name": "Region"},
            {"name": "CustomerName", "qlik_column_name": "CustomerName"},
            {"name": "ProductCode", "qlik_column_name": "ProductCode"},
        ],
    }
]


class TestAdvancedSetAnalysis:
    converter = DAXConverter()

    def test_ast_parser_equality(self):
        clauses = SetAnalysisParser.parse_clauses("Status={'Open'}")
        assert len(clauses) == 1
        assert clauses[0].field == "Status"
        assert clauses[0].operator == "="
        assert clauses[0].values == ["Open"]
        assert clauses[0].modifier_type == SetModifierType.EQUALITY

    def test_ast_parser_multi_values(self):
        clauses = SetAnalysisParser.parse_clauses("Status={'Open','Pending','Approved'}")
        assert len(clauses) == 1
        assert clauses[0].field == "Status"
        assert clauses[0].values == ["Open", "Pending", "Approved"]
        assert clauses[0].modifier_type == SetModifierType.MULTI_VALUE

    def test_ast_parser_exclusion(self):
        clauses = SetAnalysisParser.parse_clauses("Status-={'Cancelled'}")
        assert len(clauses) == 1
        assert clauses[0].field == "Status"
        assert clauses[0].operator == "-="
        assert clauses[0].values == ["Cancelled"]
        assert clauses[0].modifier_type == SetModifierType.EXCLUSION

    def test_ast_parser_numeric_comparison(self):
        clauses = SetAnalysisParser.parse_clauses("Year={'>2020'}")
        assert len(clauses) == 1
        assert clauses[0].field == "Year"
        assert clauses[0].modifier_type == SetModifierType.NUMERIC_COMPARISON
        assert clauses[0].comparison_op == ">"
        assert clauses[0].comparison_value == "2020"

    def test_ast_parser_date_comparison(self):
        clauses = SetAnalysisParser.parse_clauses("OrderDate={'>=2023-01-01'}")
        assert len(clauses) == 1
        assert clauses[0].field == "OrderDate"
        assert clauses[0].modifier_type == SetModifierType.DATE_COMPARISON
        assert clauses[0].comparison_op == ">="
        assert clauses[0].comparison_value == "2023-01-01"

    def test_ast_parser_wildcards(self):
        clauses = SetAnalysisParser.parse_clauses("CustomerName={'*Corp*'}, ProductCode={'A?B'}")
        assert len(clauses) == 2
        assert clauses[0].modifier_type == SetModifierType.WILDCARD
        assert clauses[0].wildcard_pattern == "*Corp*"
        assert clauses[1].modifier_type == SetModifierType.WILDCARD
        assert clauses[1].wildcard_pattern == "A?B"

    def test_ast_parser_calculated_dollar(self):
        clauses = SetAnalysisParser.parse_clauses("Year={$(=Max(Year)-1)}")
        assert len(clauses) == 1
        assert clauses[0].modifier_type == SetModifierType.CALCULATED_DOLLAR
        assert clauses[0].calculated_expression == "Max(Year)-1"

    @pytest.mark.parametrize(
        "source,expected_dax",
        [
            # Simple equality
            (
                "Sum({<Status={'Active'}>} Amount)",
                'CALCULATE(SUM(\'Sales\'[Amount]), \'Sales\'[Status] = "Active")',
            ),
            # Multiple values
            (
                "Sum({<Status={'Open','Pending','Approved'}>} Amount)",
                'CALCULATE(SUM(\'Sales\'[Amount]), \'Sales\'[Status] IN {"Open", "Pending", "Approved"})',
            ),
            # Single Exclusion
            (
                "Sum({<Status-={'Cancelled'}>} Amount)",
                'CALCULATE(SUM(\'Sales\'[Amount]), \'Sales\'[Status] <> "Cancelled")',
            ),
            # Multiple Exclusions
            (
                "Sum({<Region-={'North','South'}>} Amount)",
                'CALCULATE(SUM(\'Sales\'[Amount]), NOT(\'Sales\'[Region] IN {"North", "South"}))',
            ),
            # Numeric comparisons
            (
                "Sum({<Year={'>2020'}>} Amount)",
                "CALCULATE(SUM('Sales'[Amount]), 'Sales'[Year] > 2020)",
            ),
            (
                "Sum({<Year={'>=2021'}>} Amount)",
                "CALCULATE(SUM('Sales'[Amount]), 'Sales'[Year] >= 2021)",
            ),
            (
                "Sum({<Quantity={'<=500'}>} Amount)",
                "CALCULATE(SUM('Sales'[Amount]), 'Sales'[Quantity] <= 500)",
            ),
            # Date comparisons
            (
                "Sum({<OrderDate={'>2023-01-01'}>} Amount)",
                'CALCULATE(SUM(\'Sales\'[Amount]), \'Sales\'[OrderDate] > "2023-01-01")',
            ),
            # Wildcard CONTAINSSTRING
            (
                "Sum({<CustomerName={'*Corp*'}>} Amount)",
                'CALCULATE(SUM(\'Sales\'[Amount]), CONTAINSSTRING(\'Sales\'[CustomerName], "Corp"))',
            ),
            # Wildcard single-char SEARCH
            (
                "Sum({<ProductCode={'A?B'}>} Amount)",
                'CALCULATE(SUM(\'Sales\'[Amount]), SEARCH("A?B", \'Sales\'[ProductCode], 1, 0) > 0)',
            ),
            # Calculated dollar-sign expression Max(Year)-1
            (
                "Sum({<Year={$(=Max(Year)-1)}>} Amount)",
                "CALCULATE(SUM('Sales'[Amount]), 'Sales'[Year] = CALCULATE(MAX('Sales'[Year]), ALL('Sales')) - 1)",
            ),
            # Calculated dollar-sign expression Today()-1
            (
                "Sum({<OrderDate={$(=Today()-1)}>} Amount)",
                "CALCULATE(SUM('Sales'[Amount]), 'Sales'[OrderDate] = TODAY() - 1)",
            ),
            # Calculated dollar-sign expression Max(OrderDate)
            (
                "Sum({<OrderDate={$(=Max(OrderDate))}>} Amount)",
                "CALCULATE(SUM('Sales'[Amount]), 'Sales'[OrderDate] = CALCULATE(MAX('Sales'[OrderDate]), ALL('Sales')))",
            ),
            # Set Identifier 1 (Ignore selections)
            (
                "Sum({1<Status={'Active'}>} Amount)",
                'CALCULATE(SUM(\'Sales\'[Amount]), ALL(\'Sales\'), \'Sales\'[Status] = "Active")',
            ),
            # Multiple set clauses combined
            (
                "Sum({<Year={$(=Max(Year))}, Status={'Active'}, Region-={'North'}>} Amount)",
                'CALCULATE(SUM(\'Sales\'[Amount]), \'Sales\'[Year] = CALCULATE(MAX(\'Sales\'[Year]), ALL(\'Sales\')), \'Sales\'[Status] = "Active", \'Sales\'[Region] <> "North")',
            ),
        ],
    )
    def test_set_analysis_dax_translations(self, source, expected_dax):
        dax = self.converter.qlik_to_dax(source, TABLES)
        assert dax == expected_dax
        valid, problems = validate(dax, TABLES, is_measure=True)
        assert valid, f"DAX validation failed for '{dax}': {problems}"
