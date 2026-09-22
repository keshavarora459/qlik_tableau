"""Unit and integration tests for Qlik APPLYMAP() to Power Query M conversion."""

import unittest
from services.mapping_table_registry import (
    MappingTableRegistry,
    parse_applymap_ast,
    flatten_applymap_node,
)
from services.connection_mapper import ConnectionMapper
from agents.coordinator_agent import CoordinatorAgent


class TestApplyMapMQuery(unittest.TestCase):

    def test_parse_applymap_ast_single(self):
        expr = "APPLYMAP('CustomerGroupMap', CustomerID, 'Unknown')"
        node = parse_applymap_ast(expr)
        self.assertIsNotNone(node)
        self.assertEqual(node.map_name, "CustomerGroupMap")
        self.assertEqual(node.key_expr, "CustomerID")
        self.assertEqual(node.default_expr, "'Unknown'")

    def test_parse_applymap_ast_nested(self):
        expr = "APPLYMAP('__cityKey2GeoPoint', APPLYMAP('__cityName2Key', LOWER([location_city])), '-')"
        node = parse_applymap_ast(expr)
        self.assertIsNotNone(node)
        self.assertEqual(node.map_name, "__cityKey2GeoPoint")
        self.assertEqual(node.default_expr, "'-'")

        inner = node.key_expr
        self.assertTrue(hasattr(inner, "map_name"))
        self.assertEqual(inner.map_name, "__cityName2Key")
        self.assertEqual(inner.key_expr, "LOWER([location_city])")

    def test_flatten_applymap_node_order(self):
        expr = "APPLYMAP('__cityKey2GeoPoint', APPLYMAP('__cityName2Key', LOWER([location_city])), '-')"
        node = parse_applymap_ast(expr)
        steps = flatten_applymap_node(node)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["map_name"], "__cityName2Key")
        self.assertEqual(steps[0]["key_expr"], "LOWER([location_city])")
        self.assertEqual(steps[1]["map_name"], "__cityKey2GeoPoint")
        self.assertEqual(steps[1]["key_expr"], "__cityName2Key_Result__")

    def test_single_applymap_m_conversion(self):
        registry = MappingTableRegistry()
        registry.register_mapping("RateMap", "RateMapTable", "Currency", "Rate")

        baseline_m = (
            "let\n"
            '    Source = Folder.Files("Data"),\n'
            '    #"Changed Type" = Table.TransformColumnTypes(Source, {{"Currency", type text}})\n'
            "in\n"
            '    #"Changed Type"'
        )
        qlik_q = "LOAD Currency, APPLYMAP('RateMap', Currency, 1.0) AS ExchangeRate FROM [Data.csv];"

        final_m, missing = registry.translate_applymap_to_m(baseline_m, qlik_q)
        self.assertFalse(missing)
        self.assertIn('Table.NestedJoin(#"Changed Type", {"Currency"}, RateMapTable, {"Currency"}, "__RateMap_Table__", JoinKind.LeftOuter)', final_m)
        self.assertIn('Table.ExpandTableColumn(#"Merged RateMap", "__RateMap_Table__", {"Rate"}, {"__RateMap_Val__"})', final_m)
        self.assertIn('Table.AddColumn(#"Expanded RateMap", "ExchangeRate", each if [__RateMap_Val__] <> null then [__RateMap_Val__] else 1.0, type text)', final_m)
        self.assertIn('in\n    #"Removed Temp RateMap"', final_m)

    def test_nested_applymap_m_conversion_requirements(self):
        """Verify nested APPLYMAP requirements (LOWER -> __cityName2Key -> __cityKey2GeoPoint -> '-')."""
        registry = MappingTableRegistry()
        registry.register_mapping("__cityName2Key", "__cityName2Key_Tbl", "City", "Key")
        registry.register_mapping("__cityKey2GeoPoint", "__cityKey2GeoPoint_Tbl", "Key", "GeoInfo")

        baseline_m = (
            "let\n"
            '    Source = Folder.Files("Data"),\n'
            '    #"Changed Type" = Table.TransformColumnTypes(Source, {{"location_city", type text}})\n'
            "in\n"
            '    #"Changed Type"'
        )
        qlik_q = "LOAD location_city, APPLYMAP('__cityKey2GeoPoint', APPLYMAP('__cityName2Key', LOWER([location_city])), '-') AS location_city_GeoInfo FROM [Locations.csv];"

        final_m, missing = registry.translate_applymap_to_m(baseline_m, qlik_q)
        self.assertFalse(missing)

        # 1. Check Key Transformation LOWER([location_city]) -> Text.Lower([location_city])
        self.assertIn('Text.Lower([location_city])', final_m)

        # 2. Check inner join __cityName2Key
        self.assertIn('Table.NestedJoin', final_m)
        self.assertIn('__cityName2Key_Tbl', final_m)

        # 3. Check outer join __cityKey2GeoPoint
        self.assertIn('__cityKey2GeoPoint_Tbl', final_m)

        # 4. Check fallback default '-'
        self.assertIn('else "-"', final_m)

        # 5. Check output column location_city_GeoInfo
        self.assertIn('location_city_GeoInfo', final_m)

    def test_applymap_key_scalar_functions(self):
        """Verify scalar key functions Date#(), TRIM(), UPPER() in lookup key."""
        registry = MappingTableRegistry()
        registry.register_mapping("FiscalYearMap", "FiscalTable", "DateVal", "FiscalYear")

        baseline_m = (
            "let\n"
            '    Source = Folder.Files("Data"),\n'
            '    #"Changed Type" = Table.TransformColumnTypes(Source, {{"DateKey", type text}})\n'
            "in\n"
            '    #"Changed Type"'
        )
        qlik_q = "LOAD DateKey, APPLYMAP('FiscalYearMap', Date#(TRIM([DateKey]), 'YYYY-MM-DD'), 2024) AS FiscalYear FROM [Tx.csv];"

        final_m, missing = registry.translate_applymap_to_m(baseline_m, qlik_q)
        self.assertFalse(missing)
        self.assertIn('Date.FromText(Text.Trim([DateKey]))', final_m)
        self.assertIn('FiscalYear', final_m)

    def test_missing_mapping_source_diagnostic(self):
        """Verify genuine missing mapping tables produce explicit diagnostic report."""
        registry = MappingTableRegistry()

        baseline_m = (
            "let\n"
            '    Source = Folder.Files("Data")\n'
            "in\n"
            '    Source'
        )
        qlik_q = "LOAD ItemID, APPLYMAP('MissingMap', ItemID, 'N/A') AS ItemCategory FROM [Items.csv];"

        final_m, missing = registry.translate_applymap_to_m(baseline_m, qlik_q)
        self.assertEqual(len(missing), 1)
        rep = missing[0]
        self.assertEqual(rep["mapping_name"], "MissingMap")
        self.assertEqual(rep["lookup_expression"], "APPLYMAP('MissingMap', ItemID, 'N/A')")
        self.assertIn("MissingMap", rep["missing_dependency"])
        self.assertIn("no matching MAPPING LOAD", rep["reason"])

    def test_coordinator_agent_integration(self):
        """Verify CoordinatorAgent processes resolved APPLYMAP tables with high confidence and clean M."""
        raw_tables = [
            {
                "name": "__cityName2Key",
                "is_mapping": True,
                "load_type": "mapping",
                "columns": [{"name": "City"}, {"name": "Key"}],
                "qlik_query": "MAPPING LOAD City, Key FROM [Cities.csv];"
            },
            {
                "name": "__cityKey2GeoPoint",
                "is_mapping": True,
                "load_type": "mapping",
                "columns": [{"name": "Key"}, {"name": "GeoPoint"}],
                "qlik_query": "MAPPING LOAD Key, GeoPoint FROM [Geo.csv];"
            },
            {
                "name": "StoreLocations",
                "load_type": "source",
                "columns": [{"name": "location_city"}, {"name": "Revenue"}],
                "qlik_query": "LOAD location_city, APPLYMAP('__cityKey2GeoPoint', APPLYMAP('__cityName2Key', LOWER([location_city])), '-') AS location_city_GeoInfo, Revenue FROM [Locations.csv];"
            }
        ]

        agent = CoordinatorAgent()
        processed = agent._process_tables(raw_tables, [])
        self.assertEqual(len(processed), 3)

        loc_table = next(t for t in processed if t["name"] == "StoreLocations")
        self.assertGreaterEqual(loc_table["confidence"]["score"], 0.85)
        m_expr = loc_table["m_query"]
        m_str = str(m_expr)
        self.assertIn("location_city_GeoInfo", m_str)
        self.assertTrue("Text.Lower" in m_str or "Lower" in m_str or "NestedJoin" in m_str)


if __name__ == "__main__":
    unittest.main()
