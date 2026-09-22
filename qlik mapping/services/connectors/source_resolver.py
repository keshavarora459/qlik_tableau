"""Dedicated Source Resolution Layer for Qlik-to-Power-BI migrations.

Resolves Qlik `lib://` paths, QVD files, Excel/CSV files, cloud stores,
and relational database connections to valid, executable Power Query M connector calls.
"""

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class SourceType(Enum):
    DATABASE = "database"
    EXCEL = "excel"
    CSV = "csv"
    QVD = "qvd"
    FOLDER = "folder"
    SHAREPOINT = "sharepoint"
    BLOB = "blob"
    ONELAKE = "onelake"
    INLINE = "inline"
    RESIDENT = "resident"
    REST = "rest"
    UNKNOWN = "unknown"


@dataclass
class ResolvedSource:
    source_type: SourceType
    connector_function: str
    connection_m_expression: str
    physical_path: str
    file_name: Optional[str] = None
    file_extension: Optional[str] = None
    is_qvd: bool = False
    requires_review: bool = False
    review_reason: Optional[str] = None
    gateway_required: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def connector_type(self) -> str:
        return self.source_type.value

    @property
    def m_expression(self) -> str:
        return self.connection_m_expression

    @property
    def blocking_reason(self) -> str:
        return self.review_reason or ""


class SourceResolver:
    """Canonical resolver for Qlik data sources, `lib://` connections, and file types."""

    _UNSAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    _LIB_PATTERN = re.compile(r"^lib://([^/]+)/(.*)$", re.IGNORECASE)
    _RESERVED_M_KEYWORDS = {
        "as", "each", "else", "error", "false", "if", "in", "is", "let",
        "meta", "otherwise", "section", "shared", "then", "true", "try",
        "type", "table"
    }

    def __init__(self, connection_catalog: Optional[Dict[str, Any]] = None):
        self.connection_catalog = connection_catalog or {}

    @classmethod
    def escape_identifier(cls, name: str) -> str:
        """Centralized Power Query identifier escaping."""
        if not name:
            return ""
        name_str = str(name).strip()
        if cls._UNSAFE_IDENTIFIER.match(name_str) and name_str.lower() not in cls._RESERVED_M_KEYWORDS:
            return name_str
        # Strip existing #"..." if already present to avoid double wrapping
        if name_str.startswith('#"') and name_str.endswith('"'):
            name_str = name_str[2:-1]
        escaped = name_str.replace('"', '""')
        return f'#"{escaped}"'

    @classmethod
    def escape_m_string(cls, value: str) -> str:
        """Escape value for embedding in an M double-quoted literal."""
        return (value or "").replace('"', '""')

    def parse_lib_path(self, raw_path: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract (connection_name, relative_path) from lib://Connection/path/file.ext."""
        if not raw_path or not isinstance(raw_path, str):
            return None, None
        clean_path = raw_path.strip("[]'\" ")
        match = self._LIB_PATTERN.match(clean_path)
        if match:
            return match.group(1).strip(), match.group(2).strip()
        return None, clean_path

    def resolve_source(
        self,
        raw_path_or_query: str,
        connection_details: Optional[Dict[str, Any]] = None,
        table_name: Optional[str] = None,
    ) -> ResolvedSource:
        """Resolve a raw Qlik script path or connection into a Power BI physical source."""
        conn = connection_details or {}
        driver = (conn.get("driver") or conn.get("connector_type") or "").lower()
        connector = (conn.get("source_connector") or conn.get("connector_type") or "").lower()
        server = conn.get("server") or conn.get("host") or ""
        conn_name = conn.get("name") or conn.get("lib_name") or ""

        # 1. Database Connections
        if any(db_kw in f"{driver} {connector} {conn_name.lower()}" for db_kw in [
            "redshift", "snowflake", "bigquery", "gbq", "postgres", "mysql", "mariadb",
            "oracle", "sqlserver", "sql", "azure_sql", "databricks", "hana", "synapse", "teradata"
        ]) and (server or "bigquery" in (driver + connector) or "gbq" in (driver + connector)):
            return self._resolve_database(driver, connector, conn, table_name)

        # 2. Extract lib:// connection or relative path
        lib_conn_name, rel_path = self.parse_lib_path(raw_path_or_query)
        effective_path = rel_path or raw_path_or_query or ""
        effective_conn_name = lib_conn_name or conn_name

        if lib_conn_name and lib_conn_name in self.connection_catalog:
            cat_entry = self.connection_catalog[lib_conn_name]
            if isinstance(cat_entry, dict):
                conn = {**cat_entry, **conn}
                driver = (conn.get("driver") or conn.get("connector_type") or conn.get("type") or "").lower()
                connector = (conn.get("source_connector") or conn.get("connector_type") or conn.get("type") or "").lower()
                server = conn.get("server") or conn.get("host") or ""

        # Extract filename & extension
        file_name = None
        file_ext = None
        if effective_path:
            file_name = os.path.basename(effective_path.replace("\\", "/"))
            if "." in file_name:
                file_ext = "." + file_name.split(".")[-1].lower()

        # Check physical store mappings
        physical_dir = conn.get("path") or conn.get("folder_path") or effective_conn_name or "DataFiles"

        # Check for QVD files
        if file_ext == ".qvd" or (file_name and file_name.lower().endswith(".qvd")):
            # Check if upstream database SQL is available or Lakehouse Parquet location is configured
            lakehouse_table = conn.get("lakehouse_table") or conn.get("parquet_path")
            upstream_sql = conn.get("upstream_sql") or conn.get("custom_sql")
            
            if lakehouse_table:
                return ResolvedSource(
                    source_type=SourceType.ONELAKE,
                    connector_function="Lakehouse.Tables",
                    connection_m_expression=f'Lakehouse.Tables("{lakehouse_table}")',
                    physical_path=lakehouse_table,
                    file_name=file_name,
                    file_extension=".qvd",
                    is_qvd=True,
                    gateway_required=False,
                    metadata={"qvd_strategy": "lakehouse_parquet"}
                )
            elif upstream_sql and server:
                return self._resolve_database(driver, connector, conn, table_name)
            else:
                # QVD without resolved physical direct store -> REVIEW_REQUIRED
                return ResolvedSource(
                    source_type=SourceType.QVD,
                    connector_function="QVD.Unresolved",
                    connection_m_expression=f'// REVIEW_REQUIRED: QVD file "{file_name}" requires extraction to Parquet/Lakehouse or direct upstream database connection.',
                    physical_path=effective_path or (table_name or "table") + ".qvd",
                    file_name=file_name,
                    file_extension=".qvd",
                    is_qvd=True,
                    requires_review=True,
                    review_reason=f'QVD source "{file_name}" could not be resolved to upstream database or Lakehouse/Parquet store. Do not treat QVD as CSV.',
                    gateway_required=True,
                    metadata={"qvd_strategy": "manual_review"}
                )

        # Check for Cloud Storage (Azure Blob / SharePoint / OneLake)
        if any(kw in (driver + connector) for kw in ["azure", "blob", "azure_blob", "adls"]):
            account = conn.get("account") or server or "storageaccount"
            container = conn.get("container") or "data"
            fn_doc = "Csv.Document" if file_ext == ".csv" else "Excel.Workbook" if file_ext in (".xlsx", ".xls") else "File.Contents"
            return ResolvedSource(
                source_type=SourceType.BLOB,
                connector_function="AzureStorage.Blobs",
                connection_m_expression=f'let Source = AzureStorage.Blobs("{account}"), Container = Source{{[Name="{container}"]}}[Data], File = Container{{[Name="{file_name or "data.csv"}"]}}[Content], Parsed = {fn_doc}(File) in Parsed',
                physical_path=f"{account}/{container}/{file_name or ''}",
                file_name=file_name,
                file_extension=file_ext or ".csv",
                gateway_required=False
            )

        if "sharepoint" in (driver + connector) or "sharepoint" in conn_name.lower():
            site_url = conn.get("site_url") or server or "https://company.sharepoint.com/sites/Data"
            return ResolvedSource(
                source_type=SourceType.SHAREPOINT,
                connector_function="SharePoint.Files",
                connection_m_expression=f'SharePoint.Files("{site_url}", [ApiVersion = 15])',
                physical_path=site_url,
                file_name=file_name,
                file_extension=file_ext,
                gateway_required=False
            )

        # Check for Excel
        if file_ext in (".xlsx", ".xls") or raw_path_or_query.lower().endswith(".xlsx") or raw_path_or_query.lower().endswith(".xls"):
            is_direct_file = "\\" in effective_path or "/" in effective_path or ":" in effective_path
            m_expr = f'Excel.Workbook(File.Contents("{effective_path}"), null, true)' if is_direct_file else f'Folder.Files("{physical_dir}")'
            return ResolvedSource(
                source_type=SourceType.EXCEL,
                connector_function="Excel.Workbook",
                connection_m_expression=m_expr,
                physical_path=effective_path if is_direct_file else physical_dir,
                file_name=file_name,
                file_extension=file_ext or ".xlsx",
                gateway_required=True,
                metadata={"excel_format": "ooxml" if file_ext == ".xlsx" else "biff"}
            )

        # Check for CSV / Text
        if file_ext in (".csv", ".txt", ".tsv", ".dat"):
            is_direct_file = "\\" in effective_path or "/" in effective_path or ":" in effective_path
            m_expr = f'Csv.Document(File.Contents("{effective_path}"), [Delimiter=",", QuoteStyle=QuoteStyle.None])' if is_direct_file else f'Folder.Files("{physical_dir}")'
            return ResolvedSource(
                source_type=SourceType.CSV,
                connector_function="Csv.Document",
                connection_m_expression=m_expr,
                physical_path=effective_path if is_direct_file else physical_dir,
                file_name=file_name,
                file_extension=file_ext,
                gateway_required=True
            )

        # Generic folder fallback
        if any(kw in (driver + connector) for kw in ["folder", "datafiles", "file", "qix-datafiles"]):
            return ResolvedSource(
                source_type=SourceType.FOLDER,
                connector_function="Folder.Files",
                connection_m_expression=f'Folder.Files("{physical_dir}")',
                physical_path=physical_dir,
                file_name=file_name or f"{table_name or 'data'}.csv",
                file_extension=file_ext or ".csv",
                gateway_required=True
            )

        # Unresolved fallback
        return ResolvedSource(
            source_type=SourceType.UNKNOWN,
            connector_function="Unknown.Connector",
            connection_m_expression=f'// REVIEW_REQUIRED: Unresolved source connection "{conn_name}"',
            physical_path=effective_path,
            file_name=file_name,
            requires_review=True,
            review_reason=f'Source path "{raw_path_or_query}" and connection "{conn_name}" could not be resolved.',
            gateway_required=True
        )

    def _resolve_database(self, driver: str, connector: str, conn: Dict[str, Any], table_name: Optional[str]) -> ResolvedSource:
        tag = f"{driver} {connector} {conn.get('name', '').lower()}"
        server = conn.get("server") or conn.get("host") or ""
        port = conn.get("port")
        db = conn.get("database") or conn.get("db") or ""
        proj = conn.get("project")

        if "redshift" in tag:
            p = port or "5439"
            clean_server = server.split(":")[0] if ":" in server else server
            return ResolvedSource(
                source_type=SourceType.DATABASE,
                connector_function="AmazonRedshift.Database",
                connection_m_expression=f'AmazonRedshift.Database("{clean_server}:{p}", "{db or "dev"}")',
                physical_path=f"{clean_server}:{p}/{db or 'dev'}",
                gateway_required=False
            )
        elif "bigquery" in tag or "gbq" in tag:
            expr = f'GoogleBigQuery.Database([BillingProject="{proj}"]' + ')' if proj else 'GoogleBigQuery.Database()'
            return ResolvedSource(
                source_type=SourceType.DATABASE,
                connector_function="GoogleBigQuery.Database",
                connection_m_expression=expr,
                physical_path=proj or "GoogleBigQuery",
                gateway_required=False
            )
        elif "snowflake" in tag:
            wh = conn.get("warehouse") or "COMPUTE_WH"
            return ResolvedSource(
                source_type=SourceType.DATABASE,
                connector_function="Snowflake.Databases",
                connection_m_expression=f'Snowflake.Databases("{server}", "{wh}")',
                physical_path=f"{server}/{wh}",
                gateway_required=False
            )
        elif "sql" in tag:
            return ResolvedSource(
                source_type=SourceType.DATABASE,
                connector_function="Sql.Database",
                connection_m_expression=f'Sql.Database("{server}", "{db}")',
                physical_path=f"{server}/{db}",
                gateway_required=True
            )
        elif "postgres" in tag:
            return ResolvedSource(
                source_type=SourceType.DATABASE,
                connector_function="PostgreSQL.Database",
                connection_m_expression=f'PostgreSQL.Database("{server}", "{db or "postgres"}")',
                physical_path=f"{server}/{db or 'postgres'}",
                gateway_required=True
            )
        else:
            return ResolvedSource(
                source_type=SourceType.DATABASE,
                connector_function="Sql.Database",
                connection_m_expression=f'Sql.Database("{server}", "{db}")',
                physical_path=f"{server}/{db}",
                gateway_required=True
            )
