"""Base Connector Abstraction for Universal Data Ingestion."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ConnectorState(str, Enum):
    LIVE = "LIVE"
    METADATA_ONLY = "METADATA_ONLY"
    OFFLINE_METADATA = "OFFLINE_METADATA"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass
class ConnectorCapabilities:
    supports_live_query: bool = True
    supports_schema_discovery: bool = True
    supports_native_sql: bool = False
    supports_dynamic_sheets: bool = False
    supports_custom_delimiters: bool = False
    supports_credential_reference: bool = True
    m_connector_function: str = ""


class SourceConnector(ABC):
    """Abstract base class for all enterprise source connectors."""

    connector_name: str = "generic"
    capabilities: ConnectorCapabilities = ConnectorCapabilities()

    def __init__(self, connection_details: Optional[Dict[str, Any]] = None):
        self.connection_details: Dict[str, Any] = connection_details or {}
        self.state: ConnectorState = ConnectorState.METADATA_ONLY

    @abstractmethod
    def discover(self) -> Dict[str, Any]:
        """Discover database, schema, sheets, or container contents."""
        pass

    @abstractmethod
    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        """Retrieve columns, types, and constraints for a specific object."""
        pass

    @abstractmethod
    def get_tables(self) -> List[str]:
        """List all available tables/sheets/files."""
        pass

    @abstractmethod
    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        """Generate Power Query M code for Microsoft Fabric."""
        pass

    def validate_connection(self) -> bool:
        """Validate connection details and required configuration."""
        return True
