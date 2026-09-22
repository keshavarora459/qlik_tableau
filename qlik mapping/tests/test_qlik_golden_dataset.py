"""Golden Dataset benchmark tests for Qlik -> Power BI DAX conversion.

Each case specifies:
- source Qlik expression
- expected semantic meaning
- expected DAX pattern
- confidence
- validation status
"""

import pytest
from services.dax_converter import DAXConverter
from services.dax_guard import validate

GOLDEN_TABLES = [
    {
        "name": "Orders",
        "columns": [
            {"name": "OrderID", "qlik_column_name": "OrderID"},
            {"name": "SalesAmount", "qlik_column_name": "SalesAmount"},
            {"name": "CostAmount", "qlik_column_name": "CostAmount"},
            {"name": "Quantity", "qlik_column_name": "Quantity"},
            {"name": "OrderStatus", "qlik_column_name": "OrderStatus"},
            {"name": "OrderYear", "qlik_column_name": "OrderYear"},
            {"name": "OrderDate", "qlik_column_name": "OrderDate"},
            {"name": "CustomerID", "qlik_column_name": "CustomerID"},
            {"name": "ProductID", "qlik_column_name": "ProductID"},
            {"name": "Territory", "qlik_column_name": "Territory"},
        ],
    },
    {
        "name": "Customers",
        "columns": [
            {"name": "CustomerID", "qlik_column_name": "CustomerID"},
            {"name": "CustomerName", "qlik_column_name": "CustomerName"},
        ],
    },
]

GOLDEN_CASES = [
    {
        "name": "golden_basic_sum",
        "qlik": "Sum(SalesAmount)",
        "semantic_meaning": "Sum of sales amount across all rows in filter context",
        "expected_dax": "SUM('Orders'[SalesAmount])",
        "min_confidence": 0.85,
    },
    {
        "name": "golden_distinct_count",
        "qlik": "Count(distinct CustomerID)",
        "semantic_meaning": "Distinct count of customer IDs",
        "expected_dax": "DISTINCTCOUNT('Orders'[CustomerID])",
        "min_confidence": 0.85,
    },
    {
        "name": "golden_safe_divide",
        "qlik": "Sum(SalesAmount) / Sum(CostAmount)",
        "semantic_meaning": "Safe ratio of total sales to total cost",
        "expected_dax": "DIVIDE(SUM('Orders'[SalesAmount]), SUM('Orders'[CostAmount]), 0)",
        "min_confidence": 0.85,
    },
    {
        "name": "golden_set_analysis_equality",
        "qlik": "Sum({<OrderStatus={'Shipped'}>} SalesAmount)",
        "semantic_meaning": "Sales amount filtered to shipped status",
        "expected_dax": 'CALCULATE(SUM(\'Orders\'[SalesAmount]), \'Orders\'[OrderStatus] = "Shipped")',
        "min_confidence": 0.85,
    },
    {
        "name": "golden_set_analysis_multi_values",
        "qlik": "Sum({<OrderStatus={'Shipped','Delivered'}>} SalesAmount)",
        "semantic_meaning": "Sales amount filtered to shipped or delivered",
        "expected_dax": 'CALCULATE(SUM(\'Orders\'[SalesAmount]), \'Orders\'[OrderStatus] IN {"Shipped", "Delivered"})',
        "min_confidence": 0.85,
    },
    {
        "name": "golden_set_analysis_exclusion",
        "qlik": "Sum({<OrderStatus-={'Cancelled'}>} SalesAmount)",
        "semantic_meaning": "Sales amount excluding cancelled orders",
        "expected_dax": 'CALCULATE(SUM(\'Orders\'[SalesAmount]), \'Orders\'[OrderStatus] <> "Cancelled")',
        "min_confidence": 0.85,
    },
    {
        "name": "golden_set_analysis_numeric_comp",
        "qlik": "Sum({<OrderYear={'>2022'}>} SalesAmount)",
        "semantic_meaning": "Sales amount for years after 2022",
        "expected_dax": "CALCULATE(SUM('Orders'[SalesAmount]), 'Orders'[OrderYear] > 2022)",
        "min_confidence": 0.85,
    },
    {
        "name": "golden_set_analysis_calculated_max_year",
        "qlik": "Sum({<OrderYear={$(=Max(OrderYear)-1)}>} SalesAmount)",
        "semantic_meaning": "Sales amount for prior year relative to max year in dataset",
        "expected_dax": "CALCULATE(SUM('Orders'[SalesAmount]), 'Orders'[OrderYear] = CALCULATE(MAX('Orders'[OrderYear]), ALL('Orders')) - 1)",
        "min_confidence": 0.85,
    },
    {
        "name": "golden_set_analysis_wildcard_contains",
        "qlik": "Sum({<CustomerName={'*Enterprise*'}>} SalesAmount)",
        "semantic_meaning": "Sales amount where customer name contains Enterprise",
        "expected_dax": 'CALCULATE(SUM(\'Orders\'[SalesAmount]), CONTAINSSTRING(\'Customers\'[CustomerName], "Enterprise"))',
        "min_confidence": 0.85,
    },
    {
        "name": "golden_total_modifier",
        "qlik": "Sum(TOTAL SalesAmount)",
        "semantic_meaning": "Total sales ignoring visual dimensions but retaining user selections",
        "expected_dax": "CALCULATE(SUM('Orders'[SalesAmount]), ALLSELECTED())",
        "min_confidence": 0.85,
    },
    {
        "name": "golden_total_allexcept_dimension",
        "qlik": "Sum(TOTAL <Territory> SalesAmount)",
        "semantic_meaning": "Total sales calculated per territory ignoring other visual dimensions",
        "expected_dax": "CALCULATE(SUM('Orders'[SalesAmount]), ALLEXCEPT('Orders', 'Orders'[Territory]))",
        "min_confidence": 0.85,
    },
    {
        "name": "golden_aggr_summarize",
        "qlik": "Max(Aggr(Sum(SalesAmount), CustomerID))",
        "semantic_meaning": "Max customer total sales across customers",
        "expected_dax": 'MAXX(SUMMARIZE(\'Orders\', \'Orders\'[CustomerID], "@value", SUM(\'Orders\'[SalesAmount])), [@value])',
        "min_confidence": 0.85,
    },
]


class TestQlikGoldenDataset:
    converter = DAXConverter()

    @pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c["name"])
    def test_golden_case(self, case):
        qlik_expr = case["qlik"]
        measure_dict = {
            "name": case["name"],
            "expression": qlik_expr,
            "tables": ["Orders"],
        }
        res = self.converter.convert_measure(measure_dict, GOLDEN_TABLES)
        actual_dax = res["dax_expression"]
        expected_dax = case["expected_dax"]

        # 1. Semantic equivalence verification
        assert actual_dax == expected_dax, (
            f"Case {case['name']} failed semantic match.\n"
            f"Expected: {expected_dax}\n"
            f"Actual:   {actual_dax}\n"
            f"Meaning:  {case['semantic_meaning']}"
        )

        # 2. Validation status
        validation = res["validation"]
        assert validation.get("passed", True), f"Validation failed: {validation}"

        # 3. Confidence score check
        confidence = res["confidence"]
        score = confidence.get("score") if isinstance(confidence, dict) else (confidence or 0.0)
        assert score >= case["min_confidence"], f"Confidence score {score} < {case['min_confidence']}"
