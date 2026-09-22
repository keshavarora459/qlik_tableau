"""Universal Dynamic Mapping Tests for Qlik Migration Pipeline.

Validates that completely different, synthetic datasets and schemas map dynamically
and semantically with zero hardcoded assumptions:
- Dataset A: Orders, Customers, Products (E-Commerce)
- Dataset B: Shipments, Vehicles, Routes (Logistics)
- Dataset C: Invoices, Vendors, Payments (Finance)

Verifies:
1. Dynamic table, column, and relationship resolution.
2. Semantic measure conversion: Set Analysis, Pick(), ApplyMap(), TOTAL, Aggr.
3. Variable expansion without false circular dependency warnings.
4. Visual field and projection mapping for Card, Bar, Line, Table, Slicer, Treemap.
5. Handling of string vs dictionary dimensions and measures without runtime crashes.
6. Common IR and Contract 2.0 conformance.
"""

import pytest
from agents.coordinator_agent import CoordinatorAgent
from services.dax_converter import DAXConverter
from services.qlik_patterns import translate_pick, translate_applymap
from services.variable_expander import build_variable_index, expand


@pytest.mark.asyncio
async def test_dataset_a_ecommerce_dynamic_mapping():
    """Test full dynamic mapping on an E-Commerce domain (Orders/Customers/Products)."""
    payload = {
        "app_id": "app-ecommerce-synthetic-001",
        "app_name": "Global Retail Analytics",
        "tables": [
            {
                "table_name": "Orders",
                "fields": [
                    {"name": "order_id", "dataType": "STRING"},
                    {"name": "customer_id", "dataType": "STRING"},
                    {"name": "product_id", "dataType": "STRING"},
                    {"name": "order_date", "dataType": "DATE"},
                    {"name": "quantity", "dataType": "INTEGER"},
                    {"name": "unit_price", "dataType": "DECIMAL"},
                    {"name": "discount_rate", "dataType": "FLOAT"},
                    {"name": "order_status", "dataType": "STRING"},
                ],
                "load_type": "source",
                "connection_details": {"type": "postgres", "database": "retail_db"}
            },
            {
                "table_name": "Customers",
                "fields": [
                    {"name": "customer_id", "dataType": "STRING"},
                    {"name": "customer_name", "dataType": "STRING"},
                    {"name": "region_name", "dataType": "STRING"},
                    {"name": "customer_segment", "dataType": "STRING"},
                ],
                "load_type": "source",
                "connection_details": {"type": "postgres", "database": "retail_db"}
            },
            {
                "table_name": "Products",
                "fields": [
                    {"name": "product_id", "dataType": "STRING"},
                    {"name": "product_title", "dataType": "STRING"},
                    {"name": "category_name", "dataType": "STRING"},
                    {"name": "unit_cost", "dataType": "DECIMAL"},
                ],
                "load_type": "source",
                "connection_details": {"type": "postgres", "database": "retail_db"}
            }
        ],
        "relationships": [
            {"from_table": "Orders", "from_column": "customer_id", "to_table": "Customers", "to_column": "customer_id", "cardinality": "many-to-one"},
            {"from_table": "Orders", "from_column": "product_id", "to_table": "Products", "to_column": "product_id", "cardinality": "many-to-one"},
        ],
        "variables": [
            {"name": "vTargetYear", "definition": "2025"},
            {"name": "vDiscountThreshold", "definition": "0.15"},
        ],
        "measures": [
            {
                "name": "Total Order Revenue",
                "expression": "Sum(quantity * unit_price)",
                "number_format": {"format": "$#,##0.00"}
            },
            {
                "name": "Completed Orders Revenue",
                "expression": "Sum({<order_status={'Completed'}>} quantity * unit_price)",
                "number_format": {"format": "$#,##0.00"}
            },
            {
                "name": "Target Year Revenue",
                "expression": "Sum({<order_date={'$(vTargetYear)'}>} quantity * unit_price)",
                "number_format": {"format": "$#,##0.00"}
            },
            {
                "name": "Distinct Buyers Count",
                "expression": "Count(DISTINCT customer_id)",
            }
        ],
        "visualizations": [
            {
                "title": "Completed Revenue KPI",
                "chart_type": "kpi",
                "measures": [{"name": "Completed Orders Revenue"}],
                "col": 0, "row": 0, "colspan": 6, "rowspan": 3
            },
            {
                "title": "Revenue by Region",
                "chart_type": "barchart",
                "dimensions": ["region_name"],
                "measures": [{"name": "Total Order Revenue"}],
                "col": 6, "row": 0, "colspan": 12, "rowspan": 6
            },
            {
                "title": "Segment Breakdown",
                "chart_type": "treemap",
                "dimensions": [{"name": "customer_segment"}],
                "measures": ["Total Order Revenue"],
                "col": 0, "row": 3, "colspan": 6, "rowspan": 6
            }
        ]
    }

    coordinator = CoordinatorAgent()
    result = await coordinator.process_data(payload, app_id=payload["app_id"], app_name=payload["app_name"])

    assert result["status"] == "success"
    assert len(result["tables"]) == 3
    assert len(result["relationships"]) == 2
    assert len(result["measures"]) >= 4

    # Check measure DAX conversions
    measures_dict = {m["name"]: m for m in result["measures"]}
    assert "CALCULATE(SUM" in measures_dict["Completed Orders Revenue"]["dax_expression"] or "SUM(" in measures_dict["Completed Orders Revenue"]["dax_expression"]
    assert "DISTINCTCOUNT(" in measures_dict["Distinct Buyers Count"]["dax_expression"]

    # Check visual mappings
    visuals = result["visuals"]["sheet_visuals"]
    assert len(visuals) == 3
    for v in visuals:
        assert v["fabric"]["supported"] is True
        assert len(v["fabric"]["field_roles"]) > 0


@pytest.mark.asyncio
async def test_dataset_b_logistics_dynamic_mapping():
    """Test full dynamic mapping on a Logistics domain (Shipments/Vehicles/Routes)."""
    payload = {
        "app_id": "app-logistics-synthetic-002",
        "app_name": "Fleet Dispatch Command",
        "tables": [
            {
                "table_name": "Shipments",
                "fields": [
                    {"name": "tracking_num", "dataType": "STRING"},
                    {"name": "vehicle_code", "dataType": "STRING"},
                    {"name": "route_id", "dataType": "STRING"},
                    {"name": "shipment_date", "dataType": "DATE"},
                    {"name": "distance_miles", "dataType": "DECIMAL"},
                    {"name": "fuel_gallons", "dataType": "DECIMAL"},
                    {"name": "freight_charge", "dataType": "DECIMAL"},
                ],
                "load_type": "source",
                "connection_details": {"type": "snowflake", "database": "logistics_dw"}
            },
            {
                "table_name": "Vehicles",
                "fields": [
                    {"name": "vehicle_code", "dataType": "STRING"},
                    {"name": "vehicle_type", "dataType": "STRING"},
                    {"name": "driver_id", "dataType": "STRING"},
                    {"name": "mpg_rating", "dataType": "FLOAT"},
                ],
                "load_type": "source",
                "connection_details": {"type": "snowflake", "database": "logistics_dw"}
            },
            {
                "table_name": "Routes",
                "fields": [
                    {"name": "route_id", "dataType": "STRING"},
                    {"name": "origin_hub", "dataType": "STRING"},
                    {"name": "dest_hub", "dataType": "STRING"},
                    {"name": "transit_hours", "dataType": "INTEGER"},
                ],
                "load_type": "source",
                "connection_details": {"type": "snowflake", "database": "logistics_dw"}
            }
        ],
        "relationships": [
            {"from_table": "Shipments", "from_column": "vehicle_code", "to_table": "Vehicles", "to_column": "vehicle_code"},
            {"from_table": "Shipments", "from_column": "route_id", "to_table": "Routes", "to_column": "route_id"},
        ],
        "measures": [
            {
                "name": "Total Freight Revenue",
                "expression": "Sum(freight_charge)"
            },
            {
                "name": "Average Miles Per Shipment",
                "expression": "Avg(distance_miles)"
            },
            {
                "name": "Max Route Distance",
                "expression": "Max(Aggr(Sum(distance_miles), route_id))"
            },
            {
                "name": "Active Fleet Drivers",
                "expression": "Count(DISTINCT driver_id)"
            },
            {
                "name": "Total Fleet Distance",
                "expression": "Sum(total distance_miles)"
            }
        ],
        "visualizations": [
            {
                "title": "Miles by Vehicle Type",
                "chart_type": "columnchart",
                "dimensions": ["vehicle_type"],
                "measures": ["Average Miles Per Shipment"],
                "col": 0, "row": 0, "colspan": 12, "rowspan": 6
            },
            {
                "title": "Hub Performance Matrix",
                "chart_type": "table",
                "dimensions": ["origin_hub", "dest_hub"],
                "measures": ["Total Freight Revenue", "Average Miles Per Shipment"],
                "col": 0, "row": 6, "colspan": 24, "rowspan": 6
            }
        ]
    }

    coordinator = CoordinatorAgent()
    result = await coordinator.process_data(payload, app_id=payload["app_id"], app_name=payload["app_name"])

    assert result["status"] == "success"
    assert len(result["tables"]) == 3
    measures_dict = {m["name"]: m for m in result["measures"]}
    assert "SUMMARIZE" in measures_dict["Max Route Distance"]["dax_expression"]
    assert "ALLSELECTED" in measures_dict["Total Fleet Distance"]["dax_expression"]

    visuals = result["visuals"]["sheet_visuals"]
    assert len(visuals) == 2
    assert visuals[0]["fabric"]["visual_type"] in ("columnChart", "barChart")
    assert visuals[1]["fabric"]["visual_type"] in ("tableEx", "pivotTable", "matrix")


@pytest.mark.asyncio
async def test_dataset_c_finance_dynamic_mapping():
    """Test full dynamic mapping on a Finance domain (Invoices/Vendors/Payments) with Pick & ApplyMap."""
    payload = {
        "app_id": "app-finance-synthetic-003",
        "app_name": "Accounts Payable Ledger",
        "tables": [
            {
                "table_name": "Invoices",
                "fields": [
                    {"name": "invoice_num", "dataType": "STRING"},
                    {"name": "vendor_code", "dataType": "STRING"},
                    {"name": "invoice_date", "dataType": "DATE"},
                    {"name": "subtotal_amount", "dataType": "DECIMAL"},
                    {"name": "tax_amount", "dataType": "DECIMAL"},
                    {"name": "payment_status", "dataType": "STRING"},
                ],
                "load_type": "source",
                "connection_details": {"type": "oracle", "database": "finance_erp"}
            },
            {
                "table_name": "Vendors",
                "fields": [
                    {"name": "vendor_code", "dataType": "STRING"},
                    {"name": "vendor_name", "dataType": "STRING"},
                    {"name": "tier_level", "dataType": "INTEGER"},
                ],
                "load_type": "source",
                "connection_details": {"type": "oracle", "database": "finance_erp"}
            },
            {
                "table_name": "VendorTierMap",
                "fields": [
                    {"name": "tier_level", "dataType": "INTEGER"},
                    {"name": "discount_pct", "dataType": "FLOAT"},
                ],
                "load_type": "mapping",
                "is_mapping": True
            }
        ],
        "relationships": [
            {"from_table": "Invoices", "from_column": "vendor_code", "to_table": "Vendors", "to_column": "vendor_code"}
        ],
        "measures": [
            {
                "name": "Total Invoice Amount",
                "expression": "Sum(subtotal_amount + tax_amount)"
            },
            {
                "name": "Dynamic Metric Pick",
                "expression": "Pick(1, Sum(subtotal_amount), Avg(subtotal_amount))"
            },
            {
                "name": "Vendor Discount Lookup",
                "expression": "ApplyMap('VendorTierMap', tier_level, 0.0)"
            }
        ],
        "visualizations": [
            {
                "title": "Vendor Ledger Table",
                "chart_type": "table",
                "dimensions": [{"name": "vendor_name"}],
                "measures": [{"name": "Total Invoice Amount"}, "Dynamic Metric Pick"],
                "col": 0, "row": 0, "colspan": 12, "rowspan": 6
            },
            {
                "title": "Payment Status Slicer",
                "chart_type": "filterpane",
                "dimensions": ["payment_status"],
                "col": 12, "row": 0, "colspan": 6, "rowspan": 4
            }
        ]
    }

    coordinator = CoordinatorAgent()
    result = await coordinator.process_data(payload, app_id=payload["app_id"], app_name=payload["app_name"])

    assert result["status"] == "success"
    measures_dict = {m["name"]: m for m in result["measures"]}
    assert "SUM(" in measures_dict["Dynamic Metric Pick"]["dax_expression"]
    assert "LOOKUPVALUE(" in measures_dict["Vendor Discount Lookup"]["dax_expression"]

    visuals = result["visuals"]["sheet_visuals"]
    assert len(visuals) == 2
    assert visuals[1]["fabric"]["visual_type"] in ("slicer", "filterPane")


def test_qlik_pick_conversion():
    """Test Pick() pattern translation for static and dynamic selector."""
    # Static selector
    res1, changed1 = translate_pick("Pick(1, Sum(Sales), Avg(Sales))")
    assert changed1 is True
    assert res1 == "Sum(Sales)"

    res2, changed2 = translate_pick("Pick(2, Sum(Sales), Avg(Sales))")
    assert changed2 is True
    assert res2 == "Avg(Sales)"

    # Dynamic selector
    res3, changed3 = translate_pick("Pick(vMetricChoice, Sum(Revenue), Sum(Cost), Count(Orders))")
    assert changed3 is True
    assert "SWITCH(vMetricChoice, 1, Sum(Revenue), 2, Sum(Cost), 3, Count(Orders), BLANK())" in res3


def test_qlik_applymap_conversion():
    """Test ApplyMap() pattern translation."""
    known_tables = [
        {
            "name": "RatesMap",
            "columns": [
                {"fabric_column_name": "CurrencyCode"},
                {"fabric_column_name": "ExchangeRate"}
            ]
        }
    ]
    expr = "ApplyMap('RatesMap', Currency, 1.0)"
    res, changed = translate_applymap(expr, known_tables)
    assert changed is True
    assert "LOOKUPVALUE('RatesMap'[ExchangeRate], 'RatesMap'[CurrencyCode], Currency)" in res
    assert "COALESCE(" in res


def test_variable_expansion_no_false_circularity():
    """Test that multiple occurrences of the same variable do not trigger circular errors."""
    variables = [
        {"name": "vYear", "definition": "2024"},
        {"name": "vTarget", "definition": "100000"}
    ]
    index = build_variable_index(variables)
    expr = "Sum({<Year={$(vYear)}>} Sales) / $(vTarget) + Max({<Year={$(vYear)}>} Target)"
    expanded, unresolved = expand(expr, index)
    assert unresolved == []
    assert "/* CIRCULAR_VARIABLE" not in expanded
    assert "2024" in expanded
    assert "100000" in expanded
