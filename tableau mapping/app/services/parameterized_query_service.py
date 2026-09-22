from concurrent.futures import ThreadPoolExecutor

from app.services.confidence_service import evaluate_dax_confidence
from app.services.cosmos_log_service import store_activity, store_error, store_log
from app.services.custom_sql_service import _get_m_connector, call_llm, map_power_bi_datatype

# =========================================================
# SQL → PARAMETERIZED POWER BI M QUERY (DATASOURCE-AWARE)
# =========================================================

def _convert_parameterized_sql_to_m_query_snowflake(server, database, sql_query, table_name, parameters: list, warehouse: str = "") -> str:
    """Snowflake-specific parameterized M Query generator."""
    wh = warehouse or database   # fallback: use database name if warehouse is not specified

    system_prompt = (
        "You are an expert in Power BI M language. "
        "Generate a parameterized Power Query M script for a Snowflake data source. "
        "Use the delimiter '|||' to separate each logical M step.\n\n"
        "STRICT RULES:\n"
        "- The SQL query contains Tableau parameters in the format <Parameters.ParameterName>.\n"
        "- You must build a dynamic M query where the SQL string concatenates with matching Power Query parameter variables.\n"
        "- IMPORTANT: Before connecting to the database, YOU MUST generate an M Query Parameter Definition step for each parameter in the 'Tableau Parameters List'.\n"
        "- Format for parameter step: <DefaultValue> meta [IsParameterQuery = true, IsParameterQueryRequired = false, Type = type <number/text/date/datetime>, List = {<values if any>}, DefaultValue = <DefaultValue>]\n"
        "- DO NOT include a variable name or equals sign (e.g. do NOT write `pYear = `). Just start directly with the value.\n"
        "- Ensure strings are quoted and numbers are not. Do this for EVERY parameter in the dictionary provided.\n"
        f"- The NEXT step MUST start with 'let' and connect to Snowflake:\n"
        f"  let Source = Snowflake.Databases(\"{server}\", \"{wh}\")\n\n"
        f"- The NEXT Step MUST drill into the specific database:\n"
        f"  DB = Source{{[Name=\"{database}\"]}}[Data]\n\n"
        "- The NEXT Step MUST build the parameterized SqlQuery string.\n"
        "- The NEXT Step MUST run the native query on DB. The alias name of this Step MUST be EXACTLY the provided Table Name.\n"
        "  <TableName> = Value.NativeQuery(DB, SqlQuery)\n\n"
        "- The FINAL Step MUST contain 'in <TableName>'.\n\n"
        "- Do NOT rename the table. Return ONLY M code separated by '|||'. Do NOT return markdown or explanations."
    )

    prompt = f"""
    Server: {server}
    Warehouse: {wh}
    Database: {database}
    Table Name: {table_name}
    Tableau Parameters JSON (Contains Metadata): {parameters}
    Parameterized SQL:
    {sql_query}
    """
    return call_llm(prompt, system_prompt)


def _convert_parameterized_sql_to_m_query_default(server, database, sql_query, table_name, parameters: list, connection_type: str = "") -> str:
    """Default M query generator for SQL Server, Redshift, Postgres, etc. dealing with parameters."""
    m_connector = _get_m_connector(connection_type)

    system_prompt = (
        "You are an expert in Power BI M language. "
        "Generate a parameterized Power Query M script. "
        "Use the delimiter '|||' to separate each logical M step.\n\n"
        "STRICT RULES:\n"
        "- The SQL query contains Tableau parameters in the format <Parameters.ParameterName>.\n"
        "- You must build a dynamic M query where the SQL string concatenates with the Power Query parameter variables.\n"
        "- IMPORTANT: Before connecting to the database, YOU MUST generate an M Query Parameter Definition step for each parameter in the 'Tableau Parameters List'.\n"
        "- Format for parameter step: <DefaultValue> meta [IsParameterQuery = true, IsParameterQueryRequired = false, Type = type <number/text/date/datetime>, List = {<values if any>}, DefaultValue = <DefaultValue>]\n"
        "- DO NOT include a variable name or equals sign (e.g. do NOT write `pYear = `). Just start directly with the value.\n"
        "- Ensure strings are quoted and numbers are not. Do this for EVERY parameter in the dictionary provided.\n"
        f"- The NEXT step MUST start with the keyword 'let' and be exactly in this format:\n"
        f"  let Source = {m_connector}(\"{server}\", \"{database}\")\n"
        "- The NEXT Step MUST be defined as `SqlQuery = \"...\"` constructing the dynamic SQL string with parameter concatenation outside the quotes.\n"
        "- The NEXT Step MUST be a Value.NativeQuery step.\n"
        "- The alias name of the Value.NativeQuery step MUST be EXACTLY the provided Table Name.\n"
        "- Example format:\n"
        "  <TableName> = Value.NativeQuery(Source, SqlQuery)\n"
        "- The FINAL Step MUST contain 'in <TableName>' inside the same step string.\n"
        "- Do NOT rename the table. Return ONLY M code separated by '|||'. Do NOT return markdown or explanations."
    )

    prompt = f"""
    Server: {server}
    Database: {database}
    Table Name: {table_name}
    Tableau Parameters JSON (Contains Metadata): {parameters}
    Parameterized SQL:
    {sql_query}
    """
    return call_llm(prompt, system_prompt)


def _convert_parameterized_sql_to_m_query_bigquery(google_cloud_project_id: str, sql_query: str, table_name: str, parameters: list) -> str:
    """BigQuery parameterized M Query generator."""
    billing_part = f"[BillingProject=\"{google_cloud_project_id}\"]" if google_cloud_project_id else ""

    system_prompt = (
        "You are an expert in Power BI M language. "
        "Generate a parameterized Power Query M script for a Google BigQuery data source. "
        "Use the delimiter '|||' to separate each logical M step.\n\n"
        "STRICT RULES:\n"
        "- The SQL query contains Tableau parameters in the format <Parameters.ParameterName>.\n"
        "- You must build a dynamic M query where the SQL string concatenates with matching Power Query parameter variables.\n"
        "- IMPORTANT: Before connecting to the database, YOU MUST generate an M Query Parameter Definition step for each parameter in the 'Tableau Parameters List'.\n"
        "- Format for parameter step: <DefaultValue> meta [IsParameterQuery = true, IsParameterQueryRequired = false, Type = type <number/text/date/datetime>, List = {<values if any>}, DefaultValue = <DefaultValue>]\n"
        "- DO NOT include a variable name or equals sign (e.g. do NOT write `pYear = `). Just start directly with the value.\n"
        "- Ensure strings are quoted and numbers are not. Do this for EVERY parameter in the dictionary provided.\n"
        f"- The NEXT step MUST start with 'let' and connect to Google BigQuery:\n"
        f"  let Source = GoogleBigQuery.Database({billing_part})\n\n"
        "- The NEXT Step MUST build the parameterized SqlQuery string.\n"
        "- The NEXT Step MUST run the native query on Source. The alias name of this Step MUST be EXACTLY the provided Table Name.\n"
        "  <TableName> = Value.NativeQuery(Source, SqlQuery)\n\n"
        "- The FINAL Step MUST contain 'in <TableName>'.\n\n"
        "- Do NOT rename the table. Return ONLY M code separated by '|||'. Do NOT return markdown or explanations."
    )

    prompt = f"""
    Google Cloud Project ID: {google_cloud_project_id}
    Table Name: {table_name}
    Tableau Parameters JSON (Contains Metadata): {parameters}
    Parameterized SQL:
    {sql_query}
    """
    return call_llm(prompt, system_prompt)


def convert_parameterized_sql_to_m_query(server, database, sql_query, table_name, parameters: list, connection_type: str = "", warehouse: str = "", google_cloud_project_id: str = "") -> str:
    """Route to the correct Parameterized M query generator based on connection type."""
    key = (connection_type or "").lower().strip()
    if key == "snowflake":
        return _convert_parameterized_sql_to_m_query_snowflake(server, database, sql_query, table_name, parameters, warehouse)
    elif key == "bigquery":
        return _convert_parameterized_sql_to_m_query_bigquery(google_cloud_project_id, sql_query, table_name, parameters)
    else:
        return _convert_parameterized_sql_to_m_query_default(server, database, sql_query, table_name, parameters, connection_type)

# =========================================================
# MAIN BUILDER FUNCTION FOR PARAMETERIZED QUERIES
# =========================================================

def build_parameterized_custom_sql_tables(
    payload: dict,
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str,
    skip_llm: bool = False
) -> list:
    """
    Converts Tableau parameterized SQL to Azure SQL logic and packages it into a dynamic
    array of objects with flattened (single-line) content for M Query.
    """
    try:
        ds_block = payload.get("datasources_and_connections", {})
        custom_sql_list = ds_block.get("custom_sql", [])
        datasources = ds_block.get("datasources", [])
        global_params = payload.get("parameters_and_sets", {}).get("parameters", {})

        # Filter strictly for custom SQL that contains parameters
        parameterized_sqls = [cs for cs in custom_sql_list if cs.get("parameters")]

        if parameterized_sqls:
            store_activity(
                project_id=project_id,
                workbook_id=workbook_id,
                run_id=run_id,
                technical_message=f"Starting syntax conversion for {len(parameterized_sqls)} Parameterized Custom SQL tables.",
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
            parameters_list = cs.get("parameters", [])
            query_str = cs.get("query", "")

            used_params = {}
            for p in parameters_list:
                if p in global_params:
                    used_params[p] = global_params[p]
                else:
                    used_params[p] = {"datatype": "string", "current_value": ""}

            if skip_llm:
                sanitized_text = "-- SKIPPED BY USER"
            else:
                raw_m_text = convert_parameterized_sql_to_m_query(
                    server=server,
                    database=database,
                    sql_query=query_str, # Will convert dialect inside prompt if instructed, or pass as is
                    table_name=table_name.replace(" ", "_"),
                    parameters=used_params,
                    connection_type=connection_type,
                    warehouse=warehouse,
                    google_cloud_project_id=google_cloud_project_id
                )

                sanitized_text = (
                    raw_m_text.replace("```powerquery", "")
                    .replace("```m", "")
                    .replace("```", "")
                    .strip()
                )

            raw_parts = [p.strip() for p in sanitized_text.split("|||") if p.strip()]

            merged_parts = []
            for p in raw_parts:
                if p.lower().startswith("in ") and len(merged_parts) > 0:
                    merged_parts[-1] = f"{merged_parts[-1]} {p}"
                else:
                    merged_parts.append(p)

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
                evaluation_m_query = sanitized_text.replace("|||", ",")
                m_query_confidence = evaluate_dax_confidence(
                    tableau_formula=query_str,
                    dax_formula=evaluation_m_query,
                    schema_context="",
                    calc_type="parameterized_sql_to_m_query",
                    connection_type=connection_type
                )

            return {
                "datasource": ds_name,
                "tableau_table_name": table_name,
                "bi_table_name": table_name,
                "relation_type": cs.get("relation_type"),
                "schema_source": cs.get("schema_source"),
                "query": query_str,
                "parameters": parameters_list,
                "m_query": m_query_array,
                "columns": enhanced_columns,
                "m_query_confidence_score": m_query_confidence.get("confidence_score", -1),
                "m_query_review_notes": m_query_confidence.get("review_notes", "")
            }

        result = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(_process_sql, cs) for cs in parameterized_sqls]
            for future in futures:
                result.append(future.result())

        if parameterized_sqls:
            store_log(
                project_id=project_id,
                project_name="Unknown",
                workbook_id=workbook_id,
                run_id=run_id,
                function_name="build_parameterized_custom_sql_tables",
                log_level="INFO",
                message=f"Successfully processed {len(result)} Parameterized Custom SQL queries.",
                auth_header=auth_header
            )

        return result

    except Exception as e:
        print(f"❌ Parameterized Custom SQL ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        store_error(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            error_msg="Failed to process Parameterized Custom SQL tables.",
            technical_details=str(e),
            auth_header=auth_header
        )
        return []
