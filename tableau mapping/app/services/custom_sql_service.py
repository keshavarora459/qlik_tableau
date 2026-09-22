import time
from concurrent.futures import ThreadPoolExecutor

import requests

from app.core.config import settings
from app.services.confidence_service import evaluate_dax_confidence

# ✅ IMPORT ADDED: Bring in your Cosmos Loggers
from app.services.cosmos_log_service import store_activity, store_error, store_log

# =========================================================
# LLM BASE CALL
# =========================================================

from app.services.llm_service import LLMRateLimitError, call_llm as _service_call_llm

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

def call_llm(
    prompt: str,
    system_prompt: str,
    retries: int = 1,
    backoff: int = 1,
    temperature: float = 0.0
) -> str:
    """Delegates to unified LLM service with rate limit protection, token clamping, and caching."""
    return _service_call_llm(
        system_prompts=system_prompt,
        user_prompt=prompt,
        retries=retries,
        backoff=float(backoff)
    )



# =========================================================
# CONNECTOR MAPPING: Connection Type → Power BI M Function
# =========================================================

CONNECTOR_MAP = {
    "snowflake":    "Snowflake.Databases",
    "azure_sqldb":  "Sql.Database",
    "sqlserver":    "Sql.Database",
    "sqlproxy":     "Sql.Database",
    "redshift":     "AmazonRedshift.Database",
    "postgres":     "PostgreSQL.Database",
    "mysql":        "MySQL.Database",
    "oracle":       "Oracle.Database",
    "bigquery":     "GoogleBigQuery.Database",
    "databricks":   "Databricks.Catalogs",
}

SQL_DIALECT_MAP = {
    "snowflake":    "Snowflake SQL",
    "azure_sqldb":  "Azure SQL Database compatible SQL",
    "sqlserver":    "Microsoft SQL Server compatible SQL",
    "sqlproxy":     "Microsoft SQL Server compatible SQL",
    "redshift":     "Amazon Redshift compatible SQL",
    "postgres":     "PostgreSQL compatible SQL",
    "mysql":        "MySQL compatible SQL",
    "oracle":       "Oracle SQL compatible SQL",
    "bigquery":     "Google BigQuery StandardSQL",
    "databricks":   "Databricks SQL compatible SQL",
}

def _get_m_connector(connection_type: str) -> str:
    """Return the Power BI M connector function name for a given connection type."""
    key = (connection_type or "").lower().strip()
    return CONNECTOR_MAP.get(key, "Sql.Database")  # default fallback

def _get_sql_dialect(connection_type: str) -> str:
    """Return the target SQL dialect label for a given connection type."""
    key = (connection_type or "").lower().strip()
    return SQL_DIALECT_MAP.get(key, "Azure SQL Database compatible SQL")


# =========================================================
# TABLEAU SQL → TARGET DIALECT SQL
# =========================================================

def convert_tableau_sql(tableau_sql: str, connection_type: str = "") -> str:
    target_dialect = _get_sql_dialect(connection_type)

    system_prompt = (
        "You are an expert SQL migration engine. "
        f"Convert Tableau Custom SQL into {target_dialect}. "
        "Preserve all aliases and business logic exactly. "
        "Return only SQL. Do not explain."
    )

    prompt = f"""
Convert the following Tableau Custom SQL into {target_dialect}.

SQL:
{tableau_sql}
"""

    return call_llm(prompt, system_prompt)


# =========================================================
# SQL → POWER BI M QUERY (DATASOURCE-AWARE)
# =========================================================

def _convert_sql_to_m_query_snowflake(server, database, sql_query, table_name, warehouse: str = "") -> str:
    """Snowflake requires: Snowflake.Databases(server, warehouse) → drill into DB → Value.NativeQuery(DB, sql)"""
    wh = warehouse or database   # fallback: use database name if warehouse is not specified

    system_prompt = (
    "You are an expert in Power BI M language. "
    "Generate a Power Query M script for a Snowflake data source. "
    "Use the delimiter '|||' to separate each logical M step.\n\n"

    "STRICT RULES:\n"

    "- Step 1 MUST start with 'let' and connect to Snowflake:\n"
    f"  let Source = Snowflake.Databases(\"{server}\", \"{wh}\")\n\n"

    "- Step 2 MUST drill into the specific database:\n"
    f"  DB = Source{{[Name=\"{database}\"]}}[Data]\n\n"

    "- Step 3 MUST run the native query on DB (NOT on Source).\n"
    "- The alias name of Step 3 MUST be EXACTLY the provided Table Name.\n"
    "- Format:\n"
    "  <TableName> = Value.NativeQuery(DB, \"SQL QUERY\")\n\n"

    "- The FINAL Step MUST contain 'in <TableName>'.\n\n"

    "- Do NOT rename the table.\n"
    "- Do NOT call Value.NativeQuery on Source directly.\n"
    "- Return ONLY M code separated by '|||'. "
    "Do NOT return markdown or explanations."
    )

    prompt = f"""
    Server: {server}
    Warehouse: {wh}
    Database: {database}
    Table Name: {table_name}
    SQL: {sql_query}
    """
    return call_llm(prompt, system_prompt)


def _convert_sql_to_m_query_default(server, database, sql_query, table_name, connection_type: str = "") -> str:
    """Default M query generator for SQL Server, Redshift, Postgres, etc."""
    m_connector = _get_m_connector(connection_type)

    system_prompt = (
    "You are an expert in Power BI M language. "
    "Generate a Power Query M script. "
    "Use the delimiter '|||' to separate each logical M step.\n\n"

    "STRICT RULES:\n"

    "- Step 1 MUST start with the keyword 'let'.\n"
    "- Step 1 MUST be exactly in this format:\n"
    f"  let Source = {m_connector}(\"{server}\", \"{database}\")\n"

    "- Step 2 MUST be a Value.NativeQuery step.\n"
    "- The alias name of Step 2 MUST be EXACTLY the provided Table Name.\n"
    "- NEVER invent another step name.\n"
    "- ALWAYS use the provided Table Name exactly as given.\n"

    "- Example Step 2 format:\n"
    "  <TableName> = Value.NativeQuery(Source, \"SQL QUERY\")\n"

    "- The FINAL Step MUST contain 'in <TableName>' inside the same step string.\n"

    "- Do NOT rename the table.\n"
    "- Do NOT generate extra step names unless necessary.\n"

    "- Return ONLY M code separated by '|||'. "
    "Do NOT return markdown or explanations."
    )

    prompt = f"""
    Server: {server}
    Database: {database}
    Table Name: {table_name}
    SQL: {sql_query}
    """
    return call_llm(prompt, system_prompt)


def _convert_sql_to_m_query_bigquery(google_cloud_project_id: str, sql_query: str, table_name: str) -> str:
    """BigQuery requires: GoogleBigQuery.Database([BillingProject="project_id"]) or GoogleBigQuery.Database()"""
    billing_part = f"[BillingProject=\"{google_cloud_project_id}\"]" if google_cloud_project_id else ""

    system_prompt = (
        "You are an expert in Power BI M language. "
        "Generate a Power Query M script for a Google BigQuery data source. "
        "Use the delimiter '|||' to separate each logical M step.\n\n"
        "STRICT RULES:\n"
        "- Step 1 MUST start with 'let' and connect to Google BigQuery using the project ID if available:\n"
        f"  let Source = GoogleBigQuery.Database({billing_part})\n\n"
        "- Step 2 MUST run the native query on Source. The alias name of the Value.NativeQuery step MUST be EXACTLY the provided Table Name.\n"
        "- Format:\n"
        "  <TableName> = Value.NativeQuery(Source, \"SQL QUERY\")\n\n"
        "- The FINAL Step MUST contain 'in <TableName>'.\n\n"
        "- Do NOT rename the table.\n"
        "- Return ONLY M code separated by '|||'. Do NOT return markdown or explanations."
    )

    prompt = f"""
    Google Cloud Project ID: {google_cloud_project_id}
    Table Name: {table_name}
    SQL: {sql_query}
    """
    return call_llm(prompt, system_prompt)


def convert_sql_to_m_query(server, database, sql_query, table_name, connection_type: str = "", warehouse: str = "", google_cloud_project_id: str = "") -> str:
    """Route to the correct M query generator based on connection type."""
    key = (connection_type or "").lower().strip()
    if key == "snowflake":
        return _convert_sql_to_m_query_snowflake(server, database, sql_query, table_name, warehouse)
    elif key == "bigquery":
        return _convert_sql_to_m_query_bigquery(google_cloud_project_id, sql_query, table_name)
    else:
        return _convert_sql_to_m_query_default(server, database, sql_query, table_name, connection_type)

# =========================================================
# MAIN BUILDER FUNCTION
# =========================================================

# ✅ FIX APPLIED: Added missing tracking arguments to the function signature
def build_custom_sql_tables(
    payload: dict,
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str,
    skip_llm: bool = False
) -> list:
    """
    Converts Tableau SQL to Azure SQL and packages it into a dynamic
    array of objects with flattened (single-line) content.
    """
    # ✅ FIX APPLIED: Added a try/except block to catch failures and log them securely
    try:
        ds_block = payload.get("datasources_and_connections", {})

        # ✅ Filter out parameterized custom SQL, since those are handled by parameterized_query_service.py
        all_custom_sql_list = ds_block.get("custom_sql", [])
        custom_sql_list = [cs for cs in all_custom_sql_list if not cs.get("parameters")]

        datasources = ds_block.get("datasources", [])

        if custom_sql_list:
            # ✅ FIX APPLIED: Log the start of the activity
            store_activity(
                project_id=project_id,
                workbook_id=workbook_id,
                run_id=run_id,
                technical_message=f"Starting syntax conversion for {len(custom_sql_list)} Custom SQL tables.",
                auth_header=auth_header
            )

        detailed_conns = ds_block.get("detailed_connections", [])

        # Create connection lookup for server, database, connection type, schema, warehouse, google_cloud_project_id
        connection_lookup = {}
        for ds in datasources:
            conn_list = ds.get("connections", [{}])
            conn = conn_list[0] if conn_list else {}

            c_type = ds.get("connection_type") or conn.get("type") or ""
            server = conn.get("server")
            database = conn.get("database")
            schema = conn.get("schema", "")
            warehouse = conn.get("warehouse", "")
            google_cloud_project_id = conn.get("google_cloud_project_id", "")
            url = conn.get("url", "")

            # If the connection type is sqlproxy, override with detailed connections if available
            if c_type == "sqlproxy" and detailed_conns:
                real = detailed_conns[0]
                server = real.get("server")
                database = real.get("database")
                schema = real.get("schema", "")
                warehouse = real.get("warehouse", "")
                c_type = real.get("type") or c_type
                if "google_cloud_project_id" in real:
                    google_cloud_project_id = real.get("google_cloud_project_id")
                if "url" in real:
                    url = real.get("url")

            connection_lookup[ds.get("name")] = {
                "server": server,
                "database": database,
                "schema": schema,
                "warehouse": warehouse,
                "connection_type": c_type,
                "google_cloud_project_id": google_cloud_project_id,
                "url": url
            }

        def _process_sql(cs):
            ds_name = cs.get("datasource")
            conn = connection_lookup.get(ds_name)

            # Fallback 1: If the lookup failed and there is only 1 datasource, use that one
            if not conn and len(connection_lookup) == 1:
                conn = list(connection_lookup.values())[0]

            # Fallback 2: If we still don't have a connection but detailed_connections exist, use the first detailed connection
            if not conn and detailed_conns:
                real = detailed_conns[0]
                conn = {
                    "server": real.get("server"),
                    "database": real.get("database"),
                    "schema": real.get("schema", ""),
                    "warehouse": real.get("warehouse", ""),
                    "connection_type": real.get("type", ""),
                    "google_cloud_project_id": real.get("google_cloud_project_id", ""),
                    "url": real.get("url", "")
                }

            if not conn:
                conn = {}

            server = conn.get("server", "unknown-server")
            database = conn.get("database", "unknown-db")
            connection_type = conn.get("connection_type", "")
            warehouse = conn.get("warehouse", "")
            google_cloud_project_id = conn.get("google_cloud_project_id", "")
            table_name = cs.get("table_name")

            if skip_llm:
                azure_sql = "-- SKIPPED BY USER"
                sanitized_text = "-- SKIPPED BY USER"
            else:
                clean_table_name = (table_name or "Table").replace(" ", "_")
                try:
                    # Step 1: SQL Conversion (dialect-aware)
                    azure_sql = convert_tableau_sql(cs.get("query", ""), connection_type)

                    # Step 2: Get Multi-Part M-Query string from LLM (connector-aware)
                    raw_m_text = convert_sql_to_m_query(
                        server=server,
                        database=database,
                        sql_query=azure_sql,
                        table_name=clean_table_name,
                        connection_type=connection_type,
                        warehouse=warehouse,
                        google_cloud_project_id=google_cloud_project_id
                    )

                    # Step 3: Sanitize Markdown and backticks
                    sanitized_text = (
                        raw_m_text.replace("```powerquery", "")
                        .replace("```m", "")
                        .replace("```", "")
                        .strip()
                    )
                except Exception as llm_err:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"Custom SQL LLM conversion failed ({llm_err}); using deterministic M-query fallback."
                    )
                    azure_sql = cs.get("query", "")
                    m_conn = _get_m_connector(connection_type)
                    escaped_sql = azure_sql.replace('"', '""')
                    sanitized_text = (
                        f"let Source = {m_conn}(\"{server}\", \"{database}\") ||| "
                        f"{clean_table_name} = Value.NativeQuery(Source, \"{escaped_sql}\") in {clean_table_name}"
                    )

            # Step 4: Logic to merge 'in' statement if it arrives as a separate step
            raw_parts = [p.strip() for p in sanitized_text.split("|||") if p.strip()]

            merged_parts = []
            for p in raw_parts:
                if p.lower().startswith("in ") and len(merged_parts) > 0:
                    merged_parts[-1] = f"{merged_parts[-1]} {p}"
                else:
                    merged_parts.append(p)

            # Step 5: Dynamic Array Generation with Flattening
            m_query_array = []
            for index, content in enumerate(merged_parts):
                flat_content = content.replace("\n", " ").replace("\r", " ").replace("\t", " ")

                while "  " in flat_content:
                    flat_content = flat_content.replace("  ", " ")

                m_query_array.append({
                    "step": index + 1,
                    "content": flat_content.strip()
                })

            enhanced_columns = []

            for col in cs.get("columns", []):
                tableau_col_name = col.get("name")
                tableau_datatype = col.get("datatype")

                enhanced_columns.append({
                    "tableau_column_name": tableau_col_name,
                    "tableau_datatype": tableau_datatype,
                    "renamed_column_name": col.get("renamed_column_name"),
                    "bi_column_name": (col.get("renamed_column_name") or tableau_col_name),
                    "bi_datatype": map_power_bi_datatype(tableau_datatype)
                })

            if skip_llm:
                m_query_confidence = {"confidence_score": 100, "review_notes": "Skipped LLM conversion"}
            else:
                # ✅ Evaluate confidence on M Query conversion (datasource-aware)
                evaluation_m_query = sanitized_text.replace("|||", ",")

                m_query_confidence = evaluate_dax_confidence(
                    tableau_formula=azure_sql,
                    dax_formula=evaluation_m_query,
                    schema_context="",
                    calc_type="sql_to_m_query",
                    connection_type=connection_type
                )

            return {
                "datasource": ds_name,

                # 🔹 NEW STRUCTURE
                "tableau_table_name": table_name,
                "bi_table_name": table_name,

                "relation_type": cs.get("relation_type"),
                "schema_source": cs.get("schema_source"),
                "query": cs.get("query"),
                "parameters": cs.get("parameters", []),
                "m_query": m_query_array,

                # 🔹 MAPPED COLUMNS
                "columns": enhanced_columns,

                # 🔹 CONFIDENCE SCORES
                "m_query_confidence_score": m_query_confidence.get("confidence_score", -1),
                "m_query_review_notes": m_query_confidence.get("review_notes", "")
            }

        result = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(_process_sql, cs) for cs in custom_sql_list]
            for future in futures:
                result.append(future.result())

        if custom_sql_list:
            # ✅ FIX APPLIED: Log successful completion
            store_log(
                project_id=project_id,
                project_name="Unknown",
                workbook_id=workbook_id,
                run_id=run_id,
                function_name="build_custom_sql_tables",
                log_level="INFO",
                message=f"Successfully processed {len(result)} Custom SQL queries.",
                auth_header=auth_header
            )

        return result

    except Exception as e:
        # ✅ FIX APPLIED: Log the error safely so it doesn't crash the entire API silently
        print(f"❌ Custom SQL ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        store_error(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            error_msg="Failed to process Custom SQL tables.",
            technical_details=str(e),
            auth_header=auth_header
        )
        return []
