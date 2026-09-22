# Update: 2026-04-09 - Deployment trigger for Measures, Dimensions, and LODs
# 1️⃣ IMPORT THE LOGGERS
from app.services.cosmos_log_service import store_activity, store_error, store_log


# 🆕 ADDED: Power BI datatype mapping function
def map_power_bi_datatype(tableau_type: str) -> str:
    mapping = {
        "string": "Text",
        "integer": "Decimal Number",
        "real": "Decimal Number",
        "boolean": "True/False",
        "date": "Date",
        "datetime": "Date/Time",
        "spatial": "Text"
    }
    return mapping.get((tableau_type or "").lower(), "Unknown")

# 2️⃣ UPDATE SIGNATURE TO ACCEPT TRACKING IDs
def build_tables_from_payload(
    payload: dict,
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str
) -> list:
    """
    Extracts tables and their columns from parsing payload
    and attaches datasource_id.
    """

    # 3️⃣ LOG ACTIVITY: Agent starts extracting tables
    store_activity(
        project_id=project_id,
        workbook_id=workbook_id,
        run_id=run_id,
        technical_message="Extracting table schemas and matching with datasources.",
        auth_header=auth_header
    )

    try:
        ds_block = payload.get("datasources_and_connections", {})
        datasources = ds_block.get("datasources", [])

        raw_tables = (
            payload
            .get("datasources_and_connections", {})
            .get("tables", [])
        )

        tables = []

        for table in raw_tables:
            ds_name = table.get("datasource")

            # Match datasource_id
            datasource_id = None
            for ds in datasources:
                if ds.get("name") == ds_name:
                    datasource_id = ds.get("id")
                    break

            enhanced_columns = []
            for col in table.get("columns", []):
                tableau_col_name = col.get("name")
                tableau_datatype = col.get("datatype")

                renamed = col.get("renamed_column_name")
                enhanced_columns.append({
                    "tableau_column_name": tableau_col_name,
                    "tableau_datatype": tableau_datatype,
                    "tableau_renamed_column_name": renamed,
                    "bi_column_name": renamed or tableau_col_name,
                    "bi_datatype": map_power_bi_datatype(tableau_datatype)
                })

            tables.append({
                "datasource_id": datasource_id,
                "tableau_table_name": table.get("table_name"),
                "bi_table_name": table.get("table_name"),
                "schema_name": table.get("schema_name"),
                "relation_type": table.get("relation_type"),
                "schema_source": table.get("schema_source"),
                "columns": enhanced_columns
            })

            try:
                store_log(
                    project_id=project_id,
                    project_name="Unknown",
                    workbook_id=workbook_id,
                    run_id=run_id,
                    function_name="build_tables_from_payload",
                    log_level="INFO",
                    message=f"Processed table '{table.get('table_name')}' with {len(enhanced_columns)} columns.",
                    auth_header=auth_header
                )
            except Exception as e:
                print(f"Failed to log table processing: {e}")

        # 📝 STANDARD LOG: Successfully finished
        store_log(
            project_id=project_id,
            project_name="Unknown",
            workbook_id=workbook_id,
            run_id=run_id,
            function_name="build_tables_from_payload",
            log_level="INFO",
            message=f"Successfully extracted {len(tables)} tables and their columns.",
            auth_header=auth_header
        )

        return tables

    except Exception as e:
        # 🚨 LOG ERROR: Catch unexpected extraction failures
        store_error(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            error_msg="Failed to extract tables and columns.",
            technical_details=str(e),
            auth_header=auth_header
        )

        # Return an empty list so mapping can safely continue
        return []
