"""Enterprise Database Connectors (SQL Server, PostgreSQL, MySQL, Oracle, Redshift, BigQuery, Snowflake, ODBC)."""

from typing import Any, Dict, List, Optional
from .base_connector import ConnectorCapabilities, SourceConnector
from ..connection_mapper import escape_m_identifier, escape_m_string


class SqlServerConnector(SourceConnector):
    connector_name = "sqlserver"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        supports_native_sql=True,
        m_connector_function="Sql.Database",
    )

    def discover(self) -> Dict[str, Any]:
        return {
            "type": "sqlserver",
            "server": self.connection_details.get("server"),
            "database": self.connection_details.get("database"),
        }

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", [])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        from ..connection_mapper import resolve_database_name, resolve_server_name
        server = self.connection_details.get("server") or resolve_server_name(self.connection_details, custom_sql=custom_sql)
        db = self.connection_details.get("database") or resolve_database_name(self.connection_details, custom_sql=custom_sql)
        schema = self.connection_details.get("schema") or "dbo"
        if not db:
            return (
                '// REVIEW_REQUIRED: Database/catalog could not be resolved from source connection metadata\n'
                '    Source = #table({"Status", "Reason"}, {{"REVIEW_REQUIRED", "Database/catalog could not be resolved from source connection metadata"}})'
            )
        if not server:
            return (
                '// REVIEW_REQUIRED: Server/host could not be resolved from source connection metadata\n'
                '    Source = #table({"Status", "Reason"}, {{"REVIEW_REQUIRED", "Server/host could not be resolved from source connection metadata"}})'
            )
        if custom_sql:
            sql_esc = escape_m_string(custom_sql)
            return f'    Source = Sql.Database("{server}", "{db}", [Query="{sql_esc}"])'
        return f'    Source = Sql.Database("{server}", "{db}"){{[Schema="{schema}", Item="{table_name}"]}}[Data]'


class PostgresConnector(SourceConnector):
    connector_name = "postgres"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        supports_native_sql=True,
        m_connector_function="PostgreSQL.Database",
    )

    def discover(self) -> Dict[str, Any]:
        return {
            "type": "postgres",
            "server": self.connection_details.get("server") or self.connection_details.get("host"),
            "database": self.connection_details.get("database"),
        }

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", [])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        from ..connection_mapper import resolve_database_name, resolve_server_name
        server = self.connection_details.get("server") or self.connection_details.get("host") or resolve_server_name(self.connection_details, custom_sql=custom_sql)
        db = self.connection_details.get("database") or resolve_database_name(self.connection_details, custom_sql=custom_sql)
        schema = self.connection_details.get("schema") or "public"
        if not db:
            return (
                '// REVIEW_REQUIRED: Database/catalog could not be resolved from source connection metadata\n'
                '    Source = #table({"Status", "Reason"}, {{"REVIEW_REQUIRED", "Database/catalog could not be resolved from source connection metadata"}})'
            )
        if not server:
            return (
                '// REVIEW_REQUIRED: Server/host could not be resolved from source connection metadata\n'
                '    Source = #table({"Status", "Reason"}, {{"REVIEW_REQUIRED", "Server/host could not be resolved from source connection metadata"}})'
            )
        if custom_sql:
            sql_esc = escape_m_string(custom_sql)
            return f'    Source = PostgreSQL.Database("{server}", "{db}", [Query="{sql_esc}"])'
        return f'    Source = PostgreSQL.Database("{server}", "{db}"){{[Schema="{schema}", Item="{table_name}"]}}[Data]'


class SnowflakeConnector(SourceConnector):
    connector_name = "snowflake"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        supports_native_sql=True,
        m_connector_function="Snowflake.Databases",
    )

    def discover(self) -> Dict[str, Any]:
        return {
            "type": "snowflake",
            "server": self.connection_details.get("server") or self.connection_details.get("account"),
            "warehouse": self.connection_details.get("warehouse"),
            "database": self.connection_details.get("database"),
        }

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", [])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        server = self.connection_details.get("server") or self.connection_details.get("account") or "account.snowflakecomputing.com"
        wh = self.connection_details.get("warehouse") or "COMPUTE_WH"
        db = self.connection_details.get("database") or "DB"
        schema = self.connection_details.get("schema") or "PUBLIC"
        if custom_sql:
            sql_esc = escape_m_string(custom_sql)
            return (
                f'    Source = Snowflake.Databases("{server}", "{wh}"),\n'
                f'    Database = Source{{[Name="{db}", Kind="Database"]}}[Data],\n'
                f'    Result = Value.NativeQuery(Database, "{sql_esc}")'
            )
        return (
            f'    Source = Snowflake.Databases("{server}", "{wh}"),\n'
            f'    Database = Source{{[Name="{db}", Kind="Database"]}}[Data],\n'
            f'    Schema = Database{{[Name="{schema}", Kind="Schema"]}}[Data],\n'
            f'    Result = Schema{{[Name="{table_name}", Kind="Table"]}}[Data]'
        )


class RedshiftConnector(SourceConnector):
    connector_name = "redshift"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        supports_native_sql=True,
        m_connector_function="AmazonRedshift.Database",
    )

    def discover(self) -> Dict[str, Any]:
        return {
            "type": "redshift",
            "server": self.connection_details.get("server") or self.connection_details.get("cluster"),
            "database": self.connection_details.get("database"),
        }

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", [])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        server = self.connection_details.get("server") or "redshift-cluster.redshift.amazonaws.com"
        db = self.connection_details.get("database") or "dev"
        schema = self.connection_details.get("schema") or "public"
        if custom_sql:
            sql_esc = escape_m_string(custom_sql)
            return f'    Source = AmazonRedshift.Database("{server}", "{db}", [Query="{sql_esc}"])'
        return f'    Source = AmazonRedshift.Database("{server}", "{db}"){{[Schema="{schema}", Item="{table_name}"]}}[Data]'


class BigQueryConnector(SourceConnector):
    connector_name = "bigquery"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        supports_native_sql=True,
        m_connector_function="GoogleBigQuery.Database",
    )

    def discover(self) -> Dict[str, Any]:
        return {
            "type": "bigquery",
            "project_id": self.connection_details.get("project_id") or self.connection_details.get("project"),
            "dataset": self.connection_details.get("dataset"),
        }

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", [])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        project = self.connection_details.get("project_id") or self.connection_details.get("project") or "gcp-project-id"
        dataset = self.connection_details.get("dataset") or "dataset"
        if custom_sql:
            sql_esc = escape_m_string(custom_sql)
            return (
                f'    Source = GoogleBigQuery.Database([BillingProject="{project}"]),\n'
                f'    Result = Value.NativeQuery(Source, "{sql_esc}")'
            )
        return f'    Source = GoogleBigQuery.Database([BillingProject="{project}"]){{[Schema="{dataset}", Item="{table_name}"]}}[Data]'


class MySqlConnector(SourceConnector):
    connector_name = "mysql"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        supports_native_sql=True,
        m_connector_function="MySQL.Database",
    )

    def discover(self) -> Dict[str, Any]:
        return {
            "type": "mysql",
            "server": self.connection_details.get("server") or self.connection_details.get("host"),
            "database": self.connection_details.get("database"),
        }

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", [])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        from ..connection_mapper import resolve_database_name, resolve_server_name
        server = self.connection_details.get("server") or self.connection_details.get("host") or resolve_server_name(self.connection_details, custom_sql=custom_sql)
        db = self.connection_details.get("database") or resolve_database_name(self.connection_details, custom_sql=custom_sql)
        if not db:
            return (
                '// REVIEW_REQUIRED: Database/catalog could not be resolved from source connection metadata\n'
                '    Source = #table({"Status", "Reason"}, {{"REVIEW_REQUIRED", "Database/catalog could not be resolved from source connection metadata"}})'
            )
        if not server:
            return (
                '// REVIEW_REQUIRED: Server/host could not be resolved from source connection metadata\n'
                '    Source = #table({"Status", "Reason"}, {{"REVIEW_REQUIRED", "Server/host could not be resolved from source connection metadata"}})'
            )
        if custom_sql:
            sql_esc = escape_m_string(custom_sql)
            return f'    Source = MySQL.Database("{server}", "{db}", [Query="{sql_esc}"])'
        return f'    Source = MySQL.Database("{server}", "{db}"){{[Item="{table_name}"]}}[Data]'


class OracleConnector(SourceConnector):
    connector_name = "oracle"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        supports_native_sql=True,
        m_connector_function="Oracle.Database",
    )

    def discover(self) -> Dict[str, Any]:
        return {
            "type": "oracle",
            "server": self.connection_details.get("server") or self.connection_details.get("host"),
            "database": self.connection_details.get("database") or self.connection_details.get("sid"),
        }

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", [])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        server = self.connection_details.get("server") or "oracle-server"
        schema = self.connection_details.get("schema") or "ADMIN"
        if custom_sql:
            sql_esc = escape_m_string(custom_sql)
            return f'    Source = Oracle.Database("{server}", [Query="{sql_esc}"])'
        return f'    Source = Oracle.Database("{server}"){{[Schema="{schema}", Item="{table_name}"]}}[Data]'


class OdbcConnector(SourceConnector):
    connector_name = "odbc"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        supports_native_sql=True,
        m_connector_function="Odbc.DataSource",
    )

    def discover(self) -> Dict[str, Any]:
        return {
            "type": "odbc",
            "dsn": self.connection_details.get("dsn"),
        }

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", [])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        dsn = self.connection_details.get("dsn") or "DSN=EnterpriseDB"
        if custom_sql:
            sql_esc = escape_m_string(custom_sql)
            return f'    Source = Odbc.Query("dsn={dsn}", "{sql_esc}")'
        return f'    Source = Odbc.DataSource("dsn={dsn}"){{[Item="{table_name}"]}}[Data]'
