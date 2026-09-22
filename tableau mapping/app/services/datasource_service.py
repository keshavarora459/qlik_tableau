# Update: 2026-04-09 - Deployment trigger for Measures, Dimensions, and LODs
from urllib.parse import urlparse

from app.services.cosmos_log_service import store_activity, store_error, store_log


# HELPER: Extract Google Drive file ID and build mapping URL
def build_gdrive_mapping_url(url: str) -> str | None:
    """
    Extracts file ID from a Google Drive API URL and returns a Sheets viewer URL.

    Input:  https://www.googleapis.com/drive/v3/files/FILE_ID/export?...
    Output: https://docs.google.com/spreadsheets/d/FILE_ID/edit
    """
    try:
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        files_index = path_parts.index("files")
        file_id = path_parts[files_index + 1]
        return f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
    except (ValueError, IndexError):
        return None


# HELPER: Find matching detailed connection by friendly_name, url, or database
def find_detailed_connection(conn: dict, detailed_connections: list) -> dict | None:
    if not detailed_connections:
        return None

    # Try exact match on friendly_name
    conn_friendly = conn.get("friendly_name")
    if conn_friendly:
        for dc in detailed_connections:
            if dc.get("friendly_name") == conn_friendly:
                return dc

    # Try exact match on url
    conn_url = conn.get("url")
    if conn_url:
        for dc in detailed_connections:
            if dc.get("url") == conn_url:
                return dc

    # Try exact match on database
    conn_db = conn.get("database")
    if conn_db:
        for dc in detailed_connections:
            if dc.get("database") == conn_db:
                return dc

    # Fallback to the first detailed connection if there is only one
    if len(detailed_connections) == 1:
        return detailed_connections[0]

    return None


# MAIN FUNCTION
def extract_datasources(
    full_scan_json: dict,
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str
) -> dict:

    # LOG ACTIVITY: Agent starts datasource extraction
    store_activity(
        project_id=project_id,
        workbook_id=workbook_id,
        run_id=run_id,
        technical_message="Extracting datasource connections and database details from payload.",
        auth_header=auth_header
    )

    try:
        datasources = full_scan_json.get("datasources", [])

        if not datasources:
            store_log(
                project_id=project_id,
                project_name="Unknown",
                workbook_id=workbook_id,
                run_id=run_id,
                function_name="extract_datasources",
                log_level="INFO",
                message="No datasources found in the parsing payload.",
                auth_header=auth_header
            )
            return {
                "has_datasources": False,
                "connections": [],
                "databases": [],
                "files": [],
                "has_databases": False,
                "has_files": False,
                "extraction_method": None
            }

        # LOOP THROUGH EACH DATASOURCE and enrich its connections
        enriched_datasources = []
        all_connections = []

        for ds in datasources:
            enriched_ds = dict(ds)
            enriched_connections = []
            for conn in ds.get("connections", []):
                enriched_conn = dict(conn)          # shallow copy to avoid mutating original

                conn_type = conn.get("type", "")
                conn_url = conn.get("url", "")

                detailed_connections = full_scan_json.get("detailed_connections", [])
                real = find_detailed_connection(enriched_conn, detailed_connections)

                if real:
                    # Override server/database if proxy or empty
                    if conn_type == "sqlproxy" or not enriched_conn.get("server"):
                        enriched_conn["server"] = real.get("server")
                    if conn_type == "sqlproxy" or not enriched_conn.get("database"):
                        enriched_conn["database"] = real.get("database")

                    # Copy additional attributes from the detailed connection if available
                    for key in ["schema", "username", "port", "warehouse", "service", "google_cloud_project_id", "url", "tables", "worksheets"]:
                        if key in real:
                            enriched_conn[key] = real.get(key)

                    # Override connection type and datasource type if sqlproxy
                    if conn_type == "sqlproxy":
                        if "type" in real:
                            enriched_conn["type"] = real.get("type")
                            enriched_ds["connection_type"] = real.get("type")

                        store_log(
                            project_id=project_id,
                            project_name="Unknown",
                            workbook_id=workbook_id,
                            run_id=run_id,
                            function_name="extract_datasources",
                            log_level="INFO",
                            message=f"sqlproxy detected — overriding server/database/type from detailed_connections for '{conn.get('friendly_name', 'Unknown')}'",
                            auth_header=auth_header
                        )

                    # Update local variables for subsequent checks
                    conn_type = enriched_conn.get("type", "")
                    conn_url = enriched_conn.get("url", "")

                # if type contains "cloudfile" and url exists → build mapping_url
                if "cloudfile" in conn_type and conn_url:
                    mapping_url = build_gdrive_mapping_url(conn_url)
                    if mapping_url:
                        enriched_conn["mapping_url"] = mapping_url

                        store_log(
                            project_id=project_id,
                            project_name="Unknown",
                            workbook_id=workbook_id,
                            run_id=run_id,
                            function_name="extract_datasources",
                            log_level="INFO",
                            message=f"Built mapping_url for Google Drive connection '{conn.get('friendly_name', 'Unknown')}': {mapping_url}",
                            auth_header=auth_header
                        )

                enriched_connections.append(enriched_conn)
                all_connections.append(enriched_conn)

            enriched_ds["connections"] = enriched_connections
            enriched_datasources.append(enriched_ds)

        # STANDARD LOG: Successfully extracted
        store_log(
            project_id=project_id,
            project_name="Unknown",
            workbook_id=workbook_id,
            run_id=run_id,
            function_name="extract_datasources",
            log_level="INFO",
            message=f"Successfully extracted {len(all_connections)} connections.",
            auth_header=auth_header
        )

        return {
            "has_datasources": True,
            "datasources": enriched_datasources,
            "connections": all_connections,
            "databases": [],
            "files": [],
            "has_databases": False,
            "has_files": False,
            "extraction_method": None
        }

    except Exception as e:
        store_error(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            error_msg="Failed to extract datasources from payload.",
            technical_details=str(e),
            auth_header=auth_header
        )
        return {
            "has_datasources": False,
            "connections": [],
            "databases": [],
            "files": [],
            "has_databases": False,
            "has_files": False,
            "extraction_method": None
        }
