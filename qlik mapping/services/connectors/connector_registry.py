"""Connector Registry and Resolver for Enterprise Ingestion."""

import re
from typing import Any, Dict, List, Optional, Type
from .base_connector import ConnectorCapabilities, ConnectorState, SourceConnector
from .file_connectors import CsvConnector, ExcelConnector, JsonConnector, ParquetConnector, QvdConnector
from .database_connectors import (
    BigQueryConnector, MySqlConnector, OdbcConnector, OracleConnector,
    PostgresConnector, RedshiftConnector, SnowflakeConnector, SqlServerConnector
)
from .spreadsheet_connectors import GoogleSheetsConnector
from .cloud_connectors import AzureBlobConnector, GcsConnector, S3Connector, SharePointConnector
from .api_connectors import RestApiConnector


class ConnectorRegistry:
    """Registry mapping connection types, connector strings, and driver names to connectors."""

    _REGISTRY: Dict[str, Type[SourceConnector]] = {
        # File formats
        "csv": CsvConnector,
        "tsv": CsvConnector,
        "text": CsvConnector,
        "excel": ExcelConnector,
        "xlsx": ExcelConnector,
        "xls": ExcelConnector,
        "parquet": ParquetConnector,
        "json": JsonConnector,
        "qvd": QvdConnector,

        # Relational & DW Databases
        "sqlserver": SqlServerConnector,
        "mssql": SqlServerConnector,
        "sql_server": SqlServerConnector,
        "postgres": PostgresConnector,
        "postgresql": PostgresConnector,
        "snowflake": SnowflakeConnector,
        "redshift": RedshiftConnector,
        "amazon_redshift": RedshiftConnector,
        "bigquery": BigQueryConnector,
        "google_bigquery": BigQueryConnector,
        "mysql": MySqlConnector,
        "oracle": OracleConnector,
        "odbc": OdbcConnector,

        # Spreadsheets & Cloud & APIs
        "googlesheets": GoogleSheetsConnector,
        "google_sheets": GoogleSheetsConnector,
        "s3": S3Connector,
        "amazon_s3": S3Connector,
        "azure_blob": AzureBlobConnector,
        "adls": AzureBlobConnector,
        "gcs": GcsConnector,
        "sharepoint": SharePointConnector,
        "rest": RestApiConnector,
        "api": RestApiConnector,
    }

    @classmethod
    def register(cls, type_name: str, connector_cls: Type[SourceConnector]) -> None:
        cls._REGISTRY[type_name.lower()] = connector_cls

    @classmethod
    def get_connector_class(cls, type_name: str) -> Optional[Type[SourceConnector]]:
        return cls._REGISTRY.get((type_name or "").lower())


class ConnectorResolver:
    """Resolves arbitrary connection definitions into a concrete SourceConnector instance."""

    @staticmethod
    def resolve(connection_details: Optional[Dict[str, Any]] = None, qlik_query: Optional[str] = None) -> SourceConnector:
        details = dict(connection_details or {})
        raw_type = (
            details.get("type")
            or details.get("source_type")
            or details.get("connector")
            or details.get("driver")
            or ""
        ).lower()

        # If not explicitly named, infer from connection string or Qlik LOAD script
        if not raw_type and qlik_query:
            q_lower = qlik_query.lower()
            if "snowflake" in q_lower:
                raw_type = "snowflake"
            elif "redshift" in q_lower:
                raw_type = "redshift"
            elif "bigquery" in q_lower:
                raw_type = "bigquery"
            elif "postgres" in q_lower:
                raw_type = "postgres"
            elif "sqlserver" in q_lower or "sql server" in q_lower or "sqloledb" in q_lower:
                raw_type = "sqlserver"
            elif "oracle" in q_lower:
                raw_type = "oracle"
            elif "mysql" in q_lower:
                raw_type = "mysql"
            elif ".xlsx" in q_lower or ".xls" in q_lower:
                raw_type = "excel"
            elif ".csv" in q_lower or ".txt" in q_lower:
                raw_type = "csv"
            elif ".parquet" in q_lower:
                raw_type = "parquet"
            elif ".qvd" in q_lower:
                raw_type = "qvd"
            elif "docs.google.com/spreadsheets" in q_lower:
                raw_type = "googlesheets"

        conn_cls = ConnectorRegistry.get_connector_class(raw_type)
        if conn_cls:
            return conn_cls(details)

        # Default fallback to Postgres or SqlServer if host exists, otherwise CSV/generic
        if "server" in details or "host" in details:
            return PostgresConnector(details)
        return CsvConnector(details)
