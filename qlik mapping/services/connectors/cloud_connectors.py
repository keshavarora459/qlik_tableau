"""Cloud Storage Connectors (Amazon S3, Azure Blob / ADLS Gen2, Google Cloud Storage, SharePoint)."""

from typing import Any, Dict, List, Optional
from .base_connector import ConnectorCapabilities, SourceConnector


class S3Connector(SourceConnector):
    connector_name = "s3"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        m_connector_function="AmazonS3.Contents",
    )

    def discover(self) -> Dict[str, Any]:
        return {"type": "s3", "bucket": self.connection_details.get("bucket")}

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", ["S3Data"])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        url = self.connection_details.get("url") or f"https://s3.amazonaws.com/{self.connection_details.get('bucket', 'bucket-name')}"
        return f'    Source = AmazonS3.Contents("{url}")'


class AzureBlobConnector(SourceConnector):
    connector_name = "azure_blob"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        m_connector_function="AzureStorage.Blobs",
    )

    def discover(self) -> Dict[str, Any]:
        return {"type": "azure_blob", "account": self.connection_details.get("account")}

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", ["BlobData"])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        account = self.connection_details.get("account") or "storageaccount"
        container = self.connection_details.get("container") or "container"
        return f'    Source = AzureStorage.Blobs("https://{account}.blob.core.windows.net/{container}")'


class GcsConnector(SourceConnector):
    connector_name = "gcs"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        m_connector_function="GoogleCloudStorage.Contents",
    )

    def discover(self) -> Dict[str, Any]:
        return {"type": "gcs", "bucket": self.connection_details.get("bucket")}

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", ["GcsData"])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        bucket = self.connection_details.get("bucket") or "gcs-bucket"
        return f'    Source = GoogleCloudStorage.Contents("https://storage.googleapis.com/{bucket}")'


class SharePointConnector(SourceConnector):
    connector_name = "sharepoint"
    capabilities = ConnectorCapabilities(
        supports_live_query=True,
        supports_schema_discovery=True,
        m_connector_function="SharePoint.Files",
    )

    def discover(self) -> Dict[str, Any]:
        return {"type": "sharepoint", "site_url": self.connection_details.get("site_url")}

    def get_tables(self) -> List[str]:
        return self.connection_details.get("tables", ["SharePointData"])

    def get_schema(self, table_or_file: str) -> List[Dict[str, Any]]:
        return self.connection_details.get("columns", [])

    def build_fabric_source(self, table_name: str, custom_sql: Optional[str] = None, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        site_url = self.connection_details.get("site_url") or "https://contoso.sharepoint.com/sites/analytics"
        return f'    Source = SharePoint.Files("{site_url}", [ApiVersion = 15])'
