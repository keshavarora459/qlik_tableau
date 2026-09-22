"""REST and Web API Connectors."""

from typing import Any, Dict, List, Optional
from .base_connector import ConnectorCapabilities, SourceConnector


class RestApiConnector(SourceConnector):
    connector_name = "rest"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        m_connector_function="Web.Contents",
    )

    def discover(self) -> Dict[str, Any]:
        return {"type": "rest", "url": self.connection_details.get("url") or self.connection_details.get("endpoint")}

    def get_tables(self) -> List[str]:
        return ["ApiData"]

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        url = self.connection_details.get("url") or self.connection_details.get("endpoint") or "https://api.example.com/v1/data"
        return (
            f'    Source = Json.Document(Web.Contents("{url}")),\n'
            f'    Result = Table.FromRecords(Source)'
        )
