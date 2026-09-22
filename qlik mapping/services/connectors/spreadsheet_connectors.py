"""Spreadsheet Connectors (Google Sheets)."""

from typing import Any, Dict, List, Optional
from .base_connector import ConnectorCapabilities, SourceConnector


class GoogleSheetsConnector(SourceConnector):
    connector_name = "googlesheets"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        supports_dynamic_sheets=True,
        m_connector_function="GoogleSheets.Contents",
    )

    def discover(self) -> Dict[str, Any]:
        return {
            "type": "googlesheets",
            "spreadsheet_id": self.connection_details.get("spreadsheet_id") or self.connection_details.get("sheet_id"),
            "worksheet": self.connection_details.get("worksheet") or self.connection_details.get("tab"),
        }

    def get_tables(self) -> List[str]:
        return [self.connection_details.get("worksheet") or "Sheet1"]

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        sheet_url = self.connection_details.get("spreadsheet_url") or f"https://docs.google.com/spreadsheets/d/{self.connection_details.get('spreadsheet_id', 'sheet_id')}"
        sheet_tab = self.connection_details.get("worksheet") or table_name
        return (
            f'    Source = GoogleSheets.Contents("{sheet_url}"),\n'
            f'    SheetData = Source{{[Item="{sheet_tab}", Kind="Sheet"]}}[Data],\n'
            f'    Result = Table.PromoteHeaders(SheetData, [PromoteAllScalars=true])'
        )
