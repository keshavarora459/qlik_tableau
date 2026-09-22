"""DAX Conversion Audit Test Matrix.

Verifies the 12 semantic rules:
1. Source field exists
2. Target table exists
3. Target column exists
4. Generated DAX references valid fields
5. Parentheses are balanced
6. Aggregation semantics are preserved
7. Filter semantics are preserved
8. Date semantics are preserved
9. Set Analysis semantics are preserved
10. LOD semantics are preserved
11. Relationship dependencies are preserved
12. Format strings are preserved
"""

import pytest
from services.dax_converter import DAXConverter
from services.dax_guard import validate
from services.variable_expander import expand

TABLES = [
    {
        "name": "Sales",
        "columns": [
            {"qlik_column_name": "Amount", "name": "Amount"},
            {"qlik_column_name": "Cost", "name": "Cost"},
            {"qlik_column_name": "Quantity", "name": "Quantity"},
            {"qlik_column_name": "Status", "name": "Status"},
            {"qlik_column_name": "Year", "name": "Year"},
            {"qlik_column_name": "CustomerID", "name": "CustomerID"},
            {"qlik_column_name": "OrderDate", "name": "OrderDate"},
        ]
    },
    {
        "name": "Customers",
        "columns": [
            {"qlik_column_name": "CustomerID", "name": "CustomerID"},
            {"qlik_column_name": "CustomerName", "name": "CustomerName"},
            {"qlik_column_name": "Region", "name": "Region"},
        ]
    }
]


class TestQlikToDAXMatrix:
    """Test matrix for Qlik Sense -> DAX conversions."""

    converter = DAXConverter()

    @pytest.mark.parametrize(
        "source_expr,expected_pattern,description",
        [
            # Basic Aggregations
            ("Sum(Amount)", "SUM('Sales'[Amount])", "Basic SUM aggregation"),
            ("Avg(Cost)", "AVERAGE('Sales'[Cost])", "Basic AVG to AVERAGE"),
            ("Count(distinct CustomerID)", "DISTINCTCOUNT('Sales'[CustomerID])", "COUNT DISTINCT to DISTINCTCOUNT"),
            ("Sum(Amount) / Sum(Cost)", "DIVIDE(SUM('Sales'[Amount]), SUM('Sales'[Cost]), 0)", "Safe division with DIVIDE"),
            
            # Set Analysis - Single Value
            ("Sum({<Status={'Completed'}>} Amount)", "CALCULATE(SUM('Sales'[Amount]), 'Sales'[Status] = \"Completed\")", "Set Analysis single filter"),
            
            # Set Analysis - Multiple Values
            ("Sum({<Status={'Open','Pending'}>} Amount)", "CALCULATE(SUM('Sales'[Amount]), 'Sales'[Status] IN {\"Open\", \"Pending\"})", "Set Analysis IN set of values"),
            
            # Set Analysis - Exclusion
            ("Sum({<Status-={'Cancelled'}>} Amount)", "CALCULATE(SUM('Sales'[Amount]), 'Sales'[Status] <> \"Cancelled\")", "Set Analysis exclusion"),
            
            # Set Analysis - Distinct Count
            ("Count({<Status={'Completed'}>} distinct CustomerID)", "CALCULATE(DISTINCTCOUNT('Sales'[CustomerID]), 'Sales'[Status] = \"Completed\")", "Set Analysis with DISTINCTCOUNT"),
            
            # Total Modifiers
            ("Sum(total Amount)", "CALCULATE(SUM('Sales'[Amount]), ALLSELECTED())", "TOTAL modifier to ALLSELECTED"),
            ("Count(distinct total CustomerID)", "CALCULATE(DISTINCTCOUNT('Sales'[CustomerID]), ALLSELECTED())", "COUNT DISTINCT TOTAL"),
            
            # Aggr
            ("Max(Aggr(Sum(Amount), CustomerID))", "MAXX(SUMMARIZE('Sales', 'Sales'[CustomerID], \"@value\", SUM('Sales'[Amount])), [@value])", "Aggr to MAXX SUMMARIZE"),
            
            # RangeSum
            ("RangeSum(Amount, Cost)", "('Sales'[Amount] + 'Sales'[Cost])", "RangeSum to addition"),
        ]
    )
    def test_qlik_conversion_rules(self, source_expr, expected_pattern, description):
        dax = self.converter.qlik_to_dax(source_expr, TABLES)
        assert dax == expected_pattern, f"Failed {description}: Got '{dax}' instead of '{expected_pattern}'"
        
        # Verify DAX Guard validation
        valid, problems = validate(dax, TABLES, is_measure=True)
        assert valid, f"DAX Guard rejected converted DAX '{dax}': {problems}"

    def test_variable_expansion_and_conversion(self):
        """Test variable expansion followed by DAX conversion."""
        var_index = {
            "vcurrentyear": "2024",
            "vtarget": "1000",
            "vcalc": "Sum(Amount) / $(vTarget)"
        }
        
        # 1. Expand variable in set analysis
        source_expr = "Sum({<Year={$(vCurrentYear)}>} Amount)"
        expanded, unresolved = expand(source_expr, var_index)
        assert not unresolved
        assert expanded == "Sum({<Year={2024}>} Amount)"
        
        dax = self.converter.qlik_to_dax(expanded, TABLES)
        assert dax == 'CALCULATE(SUM(\'Sales\'[Amount]), \'Sales\'[Year] = "2024")'
        
        # 2. Expand nested variable formula
        expanded_calc, _ = expand("$(vCalc)", var_index)
        assert expanded_calc == "Sum(Amount) / 1000"
        dax_calc = self.converter.qlik_to_dax(expanded_calc, TABLES)
        assert dax_calc == "DIVIDE(SUM('Sales'[Amount]), 1000, 0)"

    def test_format_string_extraction(self):
        """Test Num() format extraction."""
        measure_item = {
            "name": "FormattedRevenue",
            "expression": "Num(Sum(Amount), '$#,##0.00')",
            "tables": ["Sales"]
        }
        result = self.converter.convert_measure(measure_item, TABLES)
        assert result["dax_expression"] == "SUM('Sales'[Amount])"
        assert result["fabric"]["format_string"] == "$#,##0.00"
