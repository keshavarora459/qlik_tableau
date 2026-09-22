"""Enterprise File Connectors (CSV, TSV, Excel, Parquet, JSON, QVD)."""

import os
from typing import Any, Dict, List, Optional
from .base_connector import ConnectorCapabilities, ConnectorState, SourceConnector


class CsvConnector(SourceConnector):
    connector_name = "csv"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        supports_custom_delimiters=True,
        m_connector_function="Csv.Document",
    )

    def __init__(self, connection_details: Optional[Dict[str, Any]] = None):
        super().__init__(connection_details)
        self.file_path = self.connection_details.get("file_path") or self.connection_details.get("path") or "data.csv"
        self.delimiter = self.connection_details.get("delimiter") or ","
        self.encoding = self.connection_details.get("encoding") or 65001

    def discover(self) -> Dict[str, Any]:
        return {
            "type": "csv",
            "file_path": self.file_path,
            "delimiter": self.delimiter,
            "encoding": self.encoding,
        }

    def get_tables(self) -> List[str]:
        base_name = os.path.splitext(os.path.basename(self.file_path))[0] or "Data"
        return [base_name]

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        # Never embed hardcoded local machine paths without parameterization
        clean_path = self.file_path.replace("\\", "/")
        delimit_char = '","' if self.delimiter == "," else f'"{self.delimiter}"'
        steps = [
            f'Source = Csv.Document(File.Contents("{clean_path}"), [Delimiter={delimit_char}, Columns=null, Encoding={self.encoding}, QuoteStyle=QuoteStyle.None])',
            'PromoteHeaders = Table.PromoteHeaders(Source, [PromoteAllScalars=true])',
        ]
        if columns:
            from ..connection_mapper import type_transforms_from_columns
            transforms = type_transforms_from_columns(columns)
            if transforms:
                steps.append(f"ChangedType = Table.TransformColumnTypes(PromoteHeaders, {{{transforms}}})")
        return "\n".join(f"    {step}," if i < len(steps) - 1 else f"    {step}" for i, step in enumerate(steps))


class ExcelConnector(SourceConnector):
    connector_name = "excel"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        supports_dynamic_sheets=True,
        m_connector_function="Excel.Workbook",
    )

    def __init__(self, connection_details: Optional[Dict[str, Any]] = None):
        super().__init__(connection_details)
        self.file_path = self.connection_details.get("file_path") or self.connection_details.get("path") or "data.xlsx"
        self.sheets = self.connection_details.get("sheets") or ["Sheet1"]

    def discover(self) -> Dict[str, Any]:
        return {
            "type": "excel",
            "file_path": self.file_path,
            "sheets": self.sheets,
        }

    def get_tables(self) -> List[str]:
        return list(self.sheets)

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        clean_path = self.file_path.replace("\\", "/")
        sheet = self.connection_details.get("sheet_name") or table_name
        steps = [
            f'Source = Excel.Workbook(File.Contents("{clean_path}"), null, true)',
            f'SheetData = Source{{[Item="{sheet}", Kind="Sheet"]}}[Data]',
            'PromoteHeaders = Table.PromoteHeaders(SheetData, [PromoteAllScalars=true])',
        ]
        if columns:
            from ..connection_mapper import type_transforms_from_columns
            transforms = type_transforms_from_columns(columns)
            if transforms:
                steps.append(f"ChangedType = Table.TransformColumnTypes(PromoteHeaders, {{{transforms}}})")
        return "\n".join(f"    {step}," if i < len(steps) - 1 else f"    {step}" for i, step in enumerate(steps))


class ParquetConnector(SourceConnector):
    connector_name = "parquet"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        m_connector_function="Parquet.Document",
    )

    def discover(self) -> Dict[str, Any]:
        return {"type": "parquet", "path": self.connection_details.get("file_path")}

    def get_tables(self) -> List[str]:
        return ["ParquetData"]

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        clean_path = str(self.connection_details.get("file_path") or "").replace("\\", "/")
        return f'    Source = Parquet.Document(File.Contents("{clean_path}"))'


class JsonConnector(SourceConnector):
    connector_name = "json"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        m_connector_function="Json.Document",
    )

    def discover(self) -> Dict[str, Any]:
        return {"type": "json", "path": self.connection_details.get("file_path")}

    def get_tables(self) -> List[str]:
        return ["JsonData"]

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        clean_path = str(self.connection_details.get("file_path") or "").replace("\\", "/")
        return f'    Source = Table.FromRecords(Json.Document(File.Contents("{clean_path}")))'


class QvdConnector(SourceConnector):
    connector_name = "qvd"
    capabilities = ConnectorCapabilities(
        supports_live_query=False,
        supports_schema_discovery=True,
        m_connector_function="Qvd.Document",
    )

    def discover(self) -> Dict[str, Any]:
        return {"type": "qvd", "path": self.connection_details.get("file_path")}

    def get_tables(self) -> List[str]:
        return ["QvdData"]

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        # In Microsoft Fabric, QVDs are ingested via OneLake/Lakehouse shortcut or converted to Parquet/Delta
        clean_path = str(self.connection_details.get("file_path") or "").replace("\\", "/")
        return f'    // QVD Source: Ingested through Fabric Lakehouse Shortcut or Parquet converter\n    Source = Parquet.Document(File.Contents("{clean_path}.parquet"))'
