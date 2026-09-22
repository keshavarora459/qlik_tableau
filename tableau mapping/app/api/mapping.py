
import asyncio
import re
import traceback

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import AuthContext, require_auth
from app.core.structured_logger import get_structured_logger
from app.models.request_models import MeasureRequest
from app.services.actions_service import convert_actions_to_powerbi
from app.services.agent_logger import clear_seen_activities
from app.services.calculated_fields_service import convert_calculated_fields_to_powerbi
from app.services.cosmos_log_service import store_activity, store_error, store_log
from app.services.cosmos_mapping_writer import store_mapping_in_cosmos
from app.services.custom_sql_service import build_custom_sql_tables
from app.services.dashboard_stories_service import extract_dashboards_and_stories
from app.services.datasource_service import extract_datasources

# from app.services.bookmarks import convert_parameter_bookmarks_to_navigation
from app.services.dax_service import convert_measures_to_dax
from app.services.lod_service import convert_lod_to_powerbi
from app.services.parameterized_query_service import build_parameterized_custom_sql_tables
from app.services.parameters_sets_service import extract_and_convert_parameters, extract_and_convert_sets
from app.services.parsing_service import fetch_parsing_result
from app.services.relationships_service import convert_relationships
from app.services.sheet_visuals_service import build_sheet_visuals
from app.services.tables_columns_service import build_tables_from_payload

router = APIRouter()
logger = get_structured_logger(__name__)

# --------------------------------------------------
# Helper functions
# --------------------------------------------------

def extract_metadata_for_mapping(
    payload: dict,
    tables: list,
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str,
    skip_llm: bool = False
) -> dict:
    """Coordinates metadata extraction ensuring SQL lookups are available."""
    # 1. Build Custom SQL first to provide context for metadata matching
    custom_sql_tables = build_custom_sql_tables(
        payload,
        project_id=project_id,
        workbook_id=workbook_id,
        run_id=run_id,
        auth_header=auth_header,
        skip_llm=skip_llm
    ) or [] # ✅ Ensure it's not None

    param_custom_sql_tables = build_parameterized_custom_sql_tables(
        payload,
        project_id=project_id,
        workbook_id=workbook_id,
        run_id=run_id,
        auth_header=auth_header,
        skip_llm=skip_llm
    ) or [] # ✅ Ensure it's not None

    combined_custom_sql = (custom_sql_tables or []) + (param_custom_sql_tables or [])

    # 2. Parameters - Correctly passes all 5 required arguments
    parameters = extract_and_convert_parameters(
        payload,
        project_id,
        workbook_id,
        run_id,
        auth_header,
        tables=tables or [],
        custom_sql=combined_custom_sql or [],
        skip_llm=skip_llm
    ) or [] # ✅ Ensure it's not None

    # 3. Sets - Uses both physical tables and custom SQL for field resolution
    sets = extract_and_convert_sets(payload, tables or [], combined_custom_sql or [], skip_llm=skip_llm) or []

    return {
        "parameters": parameters,
        "sets": sets,
        "custom_sql": combined_custom_sql
    }


def extract_measures(calculations: dict) -> list:
    measures = []
    if not calculations:
        return measures

    calcs_dict = calculations.get("calculated_fields")
    if not isinstance(calcs_dict, dict) or not calcs_dict:
        calcs_dict = calculations

    for name, meta in calcs_dict.items():
        if not isinstance(meta, dict): continue
        calc_type = (meta.get("type") or "").lower()

        if calc_type not in ["measure", "table_calculation"]:
            continue

        measures.append({
            "name": name,
            "datatype": meta.get("datatype", ""),
            "default_aggregation": meta.get("default_aggregation"),
            "tableau_formula": meta.get("formula"),
            "original_type": calc_type
        })

    return measures


def resolve_table_for_measure(formula: str, logical_tables: list):
    if not formula or not logical_tables:
        return None

    fields = re.findall(r"\[([^\]]+)\]", formula)

    for table in logical_tables:
        if not table: continue
        column_names = {c.get("bi_column_name") or c.get("tableau_column_name") or c.get("name") for c in table.get("columns", []) if c}
        for field in fields:
            if field in column_names:
                return table.get("bi_table_name") or table.get("tableau_table_name") or table.get("table_name")

    return None


def extract_lod_expressions(calculations: dict) -> list:
    results = []
    if not calculations:
        return results

    # 1. Standard LOD expressions bucket
    lods = calculations.get("lod_expressions")
    if isinstance(lods, dict):
        for name, meta in lods.items():
            if not meta: continue
            results.append({
                "name": name,
                "lod_type": meta.get("lod_type", "fixed").lower(),
                "dimensions": meta.get("dependencies", []),
                "tableau_formula": meta.get("formula"),
                "datatype": meta.get("datatype", ""),
                "type": meta.get("type", "lod"),
                "role": meta.get("role", ""),
                "table_name": meta.get("table_name") or meta.get("table") or ""
            })

    # 2. Extract LODs categorized inside 'calculated_fields'
    calcs = calculations.get("calculated_fields")
    if isinstance(calcs, dict):
        for name, meta in calcs.items():
            if not meta: continue
            if meta.get("type", "").lower() in ["lod", "nested_lod"]:
                results.append({
                    "name": name,
                    "lod_type": meta.get("lod_type", "fixed").lower(),
                    "dimensions": meta.get("dependencies", []),
                    "tableau_formula": meta.get("formula"),
                    "datatype": meta.get("datatype", ""),
                    "type": meta.get("type", "lod"),
                    "role": meta.get("role", ""),
                    "table_name": meta.get("table_name") or meta.get("table") or ""
                })
    return results


def _resolve_table_for_field_on_the_fly(cf, tables):
    from app.services.calculated_fields_service import normalize_name
    formula = cf.get("tableau_formula") or cf.get("formula") or ""
    dependencies = cf.get("dependencies") or []

    # 1. Match bracketed columns
    fields_in_formula = re.findall(r"\[([^\]]+)\]", formula)
    for f in fields_in_formula:
        norm_f = normalize_name(f)
        for table in tables:
            for c in table.get("columns", []):
                raw_c = c.get("tableau_column_name") or c.get("name") or ""
                if normalize_name(raw_c) == norm_f:
                    return table.get("bi_table_name") or table.get("tableau_table_name") or table.get("table_name")

    # 2. Match dependencies against physical columns
    for dep in dependencies:
        norm_dep = normalize_name(dep)
        for table in tables:
            for c in table.get("columns", []):
                raw_c = c.get("tableau_column_name") or c.get("name") or ""
                if normalize_name(raw_c) == norm_dep:
                    return table.get("bi_table_name") or table.get("tableau_table_name") or table.get("table_name")

    return ""

def resolve_table_for_lod(lod_formula: str, dimensions: list, tables: list, calculated_fields: list = None, lod_list: list = None):
    from app.services.calculated_fields_service import _find_col_in_formula, normalize_name

    if not tables:
        return ""

    # Normalize dimensions once for reuse
    norm_dimensions = {normalize_name(d): d for d in dimensions if d}

    # Extract bracketed fields from formula
    fields_in_formula = re.findall(r"\[([^\]]+)\]", lod_formula or "")
    norm_formula_fields = {normalize_name(f) for f in fields_in_formula if f}

    # Helper lookup for calculated fields / LODs resolved tables
    cf_table_map = {}
    all_resolved = (calculated_fields or []) + (lod_list or [])
    for cf in all_resolved:
        if cf and cf.get("name"):
            tbl_resolved = cf.get("table_name")
            if not tbl_resolved:
                tbl_resolved = _resolve_table_for_field_on_the_fly(cf, tables)
            if tbl_resolved:
                cf_table_map[normalize_name(cf["name"])] = tbl_resolved

    best_table = None
    best_score = -1

    for table in tables:
        if not table: continue
        t_name = table.get("bi_table_name") or table.get("tableau_table_name") or table.get("table_name")
        if not t_name: continue

        score = 0

        # Build normalized column name sets for this table
        exact_cols = set()
        stripped_cols = set()
        for c in table.get("columns", []):
            if not c: continue
            raw_c = c.get("bi_column_name") or c.get("tableau_column_name") or c.get("name") or ""
            exact_cols.add(normalize_name(raw_c))
            # Also add stripped version (before parentheses)
            stripped_cols.add(normalize_name(raw_c.split(" (")[0]))

        # 1. Match dimensions (physical columns or resolved calculated fields)
        for nd in norm_dimensions:
            if nd in exact_cols:
                score += 10
            elif nd in stripped_cols:
                score += 5
            elif nd in cf_table_map and cf_table_map[nd] == t_name:
                score += 10

        # 2. Match bracketed fields in formula
        for nf in norm_formula_fields:
            if nf in exact_cols:
                score += 2
            elif nf in stripped_cols:
                score += 1
            elif nf in cf_table_map and cf_table_map[nf] == t_name:
                score += 2

        # 3. Substring check for physical columns in formula (unbracketed)
        for c in table.get("columns", []):
            if not c: continue
            raw_c = c.get("bi_column_name") or c.get("tableau_column_name") or c.get("name") or ""
            names_to_check = [
                (raw_c, 0.5),
                (raw_c.split(" (")[0], 0.25) if " (" in raw_c else (None, 0)
            ]
            for name, pts in names_to_check:
                if not name: continue
                # Strip brackets if present
                if name.startswith("[") and name.endswith("]"):
                    name = name[1:-1]
                if _find_col_in_formula(name, lod_formula):
                    score += pts
                    break

        if score > best_score:
            best_score = score
            best_table = t_name
        elif score == best_score and best_score > 0:
            # Tie breaker: Prefer table with more columns (typically the fact table)
            current_best_table_obj = next((tbl for tbl in tables if (tbl.get("bi_table_name") or tbl.get("tableau_table_name") or tbl.get("table_name")) == best_table), None)
            current_cols_count = len(current_best_table_obj.get("columns", [])) if current_best_table_obj else 0
            candidate_cols_count = len(table.get("columns", []))
            if candidate_cols_count > current_cols_count:
                best_table = t_name

    return best_table or ""

def extract_formatting_and_styling(payload: dict) -> dict:
    return (payload or {}).get("formatting_and_styling") or (payload or {}).get("formatting") or {}

def extract_actions(payload: dict) -> list:
    actions = (payload or {}).get("actions", [])
    if actions and isinstance(actions, list):
        return actions
    dashboards = (payload or {}).get("dashboards", {})
    d_list = dashboards.get("dashboards", []) if isinstance(dashboards, dict) else (dashboards if isinstance(dashboards, list) else [])
    found_actions = []
    for d in d_list:
        if isinstance(d, dict) and d.get("actions") and isinstance(d["actions"], list):
            found_actions.extend(d["actions"])
    return found_actions

def extract_embedded_assets(payload: dict) -> dict:
    return (payload or {}).get("embedded_assets") or (payload or {}).get("extracted_files") or {}

def extract_permissions(payload: dict) -> dict:
    return (payload or {}).get("permissions", {})

def extract_artifacts(payload: dict) -> dict:
    return (payload or {}).get("artifacts") or (payload or {}).get("extracted_files") or {}

def extract_security(payload: dict) -> dict:
    return (payload or {}).get("security", {})



# --------------------------------------------------
# Mapping API (Parallelized for Speed)
# --------------------------------------------------

# Limit concurrent mapping operations to prevent resource exhaustion
# when 10+ workbooks are submitted simultaneously.
# Only 4 process at a time; the rest wait in queue.
_mapping_semaphore = asyncio.Semaphore(4)

@router.post("/mapping")
async def get_mapping(body: MeasureRequest, auth: AuthContext = Depends(require_auth)):
    auth_header = auth.auth_header

    # Initialize Context for structured logging and DLQ
    from app.core.structured_logger import CorrelationContext
    CorrelationContext.set(
        run_id=body.run_id,
        workbook_id=body.workbook_id,
        project_id=body.project_id
    )

    # --- Initial Validation ---
    if not body.run_id:
        raise HTTPException(
            status_code=400,
            detail="run_id is required"
        )

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            async with _mapping_semaphore:
                result = await _get_mapping_internal(body, auth_header)
            return result

        except HTTPException as e:
            # Client errors (4xx) should not be retried
            if e.status_code < 500:
                raise e
            if attempt == max_attempts:
                raise e
            logger.warning(
                f"HTTPException {e.status_code} during mapping run (attempt {attempt}/{max_attempts}). Retrying...",
                extra={"attempt": attempt, "run_id": body.run_id}
            )
            await asyncio.sleep(2 ** attempt)

        except Exception as e:
            if attempt == max_attempts:
                traceback.print_exc()
                raise HTTPException(
                    status_code=500,
                    detail={"message": "Internal Orchestration Error after retries", "reason": str(e), "traceback": traceback.format_exc()}
                )
            logger.warning(
                f"Unexpected error {type(e).__name__} during mapping run (attempt {attempt}/{max_attempts}): {e}. Retrying...",
                extra={"attempt": attempt, "run_id": body.run_id, "error_type": type(e).__name__}
            )
            await asyncio.sleep(2 ** attempt)

async def _get_mapping_internal(body: MeasureRequest, auth_header: str):
    # Reset deduplication cache so reusing a run_id in Postman still generates all 12 activities
    clear_seen_activities()

    function_name = "get_mapping"
    run_id = body.run_id

    cosmosdb_status = {"stored": False}
    logs_status = {"stored": True}

    # Initialize defaults to allow partial payload returns on error
    parsing_response = {}
    project_name = "Unknown"
    workbook_metadata = {
        "project_id": body.project_id,
        "workbook_id": body.workbook_id,
        "created_at": None,
        "name": "Unknown",
        "version": None,
        "source_build": None,
        "file_type": None,
        "site": None,
        "timestamp": None
    }
    datasources = []
    tables = []
    custom_sql = []
    param_custom_sql_tables = []
    dashboards_and_stories = {"dashboards": [], "stories": []}
    sheets_visuals = []
    relationships = []
    measures = []
    parameters = []
    sets = []
    converted_calculated_fields = []
    converted_lods = []
    formatting_and_styling = {}
    actions = []
    embedded_assets = {}
    permissions = {}
    artifacts = {}
    security = {}
    calc_llm_skipped = False

    mapping_status = "success"
    mapping_message = "Mapping completed successfully"
    error_message = None

    try:

        # 1. Fetch parsing results
        parsing_response = await asyncio.to_thread(
            fetch_parsing_result,
            body.project_id or "",
            body.workbook_id or "",
            body.run_id,
            auth_header,
            getattr(body, "payload", None) or getattr(body, "parsing_result", None)
        ) or {} # ✅ Safeguard

        payload = parsing_response.get("payload")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=500, detail="Invalid parsing response")

        # --- NORMALIZE PAYLOAD SCHEMA (Support both nested and flat layouts) ---
        if "datasources_and_connections" not in payload:
            logical_model = payload.get("logical_model", {})
            ds_val = payload.get("datasources", [])
            
            if isinstance(ds_val, dict):
                actual_datasources = ds_val.get("datasources", [])
                actual_detailed_conns = ds_val.get("detailed_connections", [])
                actual_custom_sql = ds_val.get("custom_sql", [])
            else:
                actual_datasources = ds_val
                actual_detailed_conns = payload.get("detailed_connections", [])
                actual_custom_sql = payload.get("custom_sql", [])

            # Check logical_model for custom_sql if not found in datasources
            if not actual_custom_sql and isinstance(logical_model, dict):
                actual_custom_sql = logical_model.get("custom_sql", [])

            actual_tables = (
                logical_model.get("tables", []) if isinstance(logical_model, dict) and logical_model.get("tables")
                else payload.get("tables", [])
            )
            actual_relationships = (
                payload.get("relationships", [])
                or (logical_model.get("relationships", []) if isinstance(logical_model, dict) else [])
            )

            payload["datasources_and_connections"] = {
                "datasources": actual_datasources,
                "detailed_connections": actual_detailed_conns,
                "custom_sql": actual_custom_sql,
                "tables": actual_tables,
                "relationships": actual_relationships
            }
        if "worksheets" not in payload:
            sheets_data = payload.get("sheets", {})
            if isinstance(sheets_data, dict):
                payload["worksheets"] = sheets_data.get("sheets", [])
            elif isinstance(sheets_data, list):
                payload["worksheets"] = sheets_data
            elif "worksheets" in payload and isinstance(payload["worksheets"], list):
                pass
            else:
                payload["worksheets"] = []

        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
            
        file_info = metadata.get("file_info", {})
        if not isinstance(file_info, dict):
            file_info = {}

        wb_name = file_info.get("name") or metadata.get("name") or payload.get("workbook_name") or "Unknown"
        wb_version = file_info.get("version") or metadata.get("version") or "Unknown"
        wb_source_build = file_info.get("source_build") or metadata.get("source_build") or "Unknown"
        wb_file_type = file_info.get("file_type") or metadata.get("file_type") or "Unknown"
        wb_site = file_info.get("site") or metadata.get("site") or "Unknown"
        wb_timestamp = metadata.get("timestamp") or metadata.get("created_at") or parsing_response.get("created_at")

        resolved_project_id = parsing_response.get("project_id") or payload.get("project_id") or body.project_id or ""
        resolved_workbook_id = parsing_response.get("workbook_id") or payload.get("workbook_id") or body.workbook_id or ""
        resolved_project_name = parsing_response.get("project_name") or payload.get("project_name")
        if not resolved_project_name or resolved_project_name == "Unknown":
            resolved_project_name = wb_name if wb_name != "Unknown" else "Unknown"
        project_name = resolved_project_name

        def _log_request_received():
            try:
                store_log(
                    project_id=resolved_project_id,
                    project_name=resolved_project_name,
                    workbook_id=resolved_workbook_id,
                    run_id=run_id,
                    function_name=function_name,
                    log_level="INFO",
                    message="Mapping request received",
                    auth_header=auth_header
                )
                store_log(
                    project_id=resolved_project_id,
                    project_name=resolved_project_name,
                    workbook_id=resolved_workbook_id,
                    run_id=run_id,
                    function_name=function_name,
                    log_level="INFO",
                    message="Parsing results fetched successfully",
                    auth_header=auth_header
                )
                store_activity(
                    project_id=resolved_project_id,
                    workbook_id=resolved_workbook_id,
                    run_id=run_id,
                    technical_message="Successfully downloaded parsing JSON from upstream API.",
                    auth_header=auth_header
                )
            except Exception as e:
                logger.error(f"Log failed: {e}", extra={"error_type": type(e).__name__, "error_message": str(e)})
                logs_status["stored"] = False

        _log_request_received()

        workbook_metadata = {
            "project_id": resolved_project_id,
            "workbook_id": resolved_workbook_id,
            "created_at": parsing_response.get("created_at") or wb_timestamp,
            "name": wb_name,
            "version": wb_version,
            "source_build": wb_source_build,
            "file_type": wb_file_type,
            "site": wb_site,
            "timestamp": wb_timestamp
        }

        ds_block = payload.get("datasources_and_connections")
        if not ds_block:
            ds_block = payload.get("datasources", {})

        datasources_result = extract_datasources(
            full_scan_json=ds_block,
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            auth_header=auth_header
        ) or {}
        datasources = datasources_result.get("datasources", [])

        joins = ds_block.get("relationships", []) or []
        relationships = convert_relationships(
            joins=joins,
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            auth_header=auth_header
        ) or []

        # 🚀 PARALLEL PHASE 1
        tables_task = asyncio.to_thread(
            build_tables_from_payload,
            payload,
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            auth_header=auth_header
        )

        custom_sql_task = asyncio.to_thread(
            build_custom_sql_tables,
            payload,
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            auth_header=auth_header,
            skip_llm=body.skip_llm
        )

        param_custom_sql_task = asyncio.to_thread(
            build_parameterized_custom_sql_tables,
            payload,
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            auth_header=auth_header,
            skip_llm=body.skip_llm
        )

        dashboards_task = asyncio.to_thread(
            extract_dashboards_and_stories,
            payload,
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            auth_header=auth_header
        )

        # Await first set of parallel tasks
        tables, custom_sql_tables, param_custom_sql_tables, dashboards_and_stories = await asyncio.gather(
            tables_task, custom_sql_task, param_custom_sql_task, dashboards_task
        )

        store_activity(
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            technical_message="Phase 1 complete: Tables, Custom SQL, and Dashboards compiled successfully.",
            auth_header=auth_header
        )

        try:
            store_log(
                project_id=workbook_metadata["project_id"],
                project_name=project_name,
                workbook_id=workbook_metadata["workbook_id"],
                run_id=run_id,
                function_name=function_name,
                log_level="INFO",
                message="Phase 1 complete: Tables, Custom SQL, and Dashboards compiled successfully.",
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log Phase 1 completion: {e}", extra={"error_type": type(e).__name__})

        # ✅ CRITICAL FIX: The reason for thecrash is likely here if gather returns None entries
        tables = tables or []
        custom_sql_tables = custom_sql_tables or []
        param_custom_sql_tables = param_custom_sql_tables or []
        dashboards_and_stories = dashboards_and_stories or {"dashboards": [], "stories": []}

        combined_custom_sql_tables = custom_sql_tables + param_custom_sql_tables

        combined_tables = tables + combined_custom_sql_tables

        # Prepare tables_columns for slicer generation
        {
            "tables": {
                (table.get("bi_table_name") or table.get("tableau_table_name") or table.get("table_name", "UnknownTable")): {
                    "columns": table.get("columns", [])
                }
                for table in combined_tables
                if table and (table.get("bi_table_name") or table.get("tableau_table_name") or table.get("table_name"))
            }
        }

        formatting_and_styling = extract_formatting_and_styling(payload)
        embedded_assets = extract_embedded_assets(payload)
        permissions = extract_permissions(payload)
        artifacts = extract_artifacts(payload)
        security = extract_security(payload)

        raw_actions = extract_actions(payload) or []
        dashboards_list = dashboards_and_stories.get("dashboards", []) or []

        # 🚀 PARALLEL PHASE 2
        store_activity(
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            technical_message="Starting Phase 2 computations: Converting native Tableau Parameters and Sets.",
            auth_header=auth_header
        )

        try:
            store_log(
                project_id=workbook_metadata["project_id"],
                project_name=project_name,
                workbook_id=workbook_metadata["workbook_id"],
                run_id=run_id,
                function_name=function_name,
                log_level="INFO",
                message="Starting Phase 2 computations: Converting native Tableau Parameters and Sets.",
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log Phase 2 start: {e}", extra={"error_type": type(e).__name__})

        params_task = asyncio.to_thread(
            extract_and_convert_parameters,
            payload,
            body.project_id,
            body.workbook_id,
            run_id,
            auth_header,
            tables=tables,
            custom_sql=combined_custom_sql_tables,
            skip_llm=body.skip_llm
        )

        sets_task = asyncio.to_thread(
            extract_and_convert_sets,
            payload, tables, combined_custom_sql_tables, skip_llm=body.skip_llm
        )

        # PREPARE LLM INPUTS
        calculations = payload.get("calculations_and_lods") or payload.get("calculations") or payload.get("calculated_fields") or {}
        raw_calculated_fields_dict = calculations.get("calculated_fields", calculations) or {}
        raw_calculated_fields = []

        if isinstance(raw_calculated_fields_dict, dict):
            for name, meta in raw_calculated_fields_dict.items():
                if not isinstance(meta, dict): continue
                if meta.get("type", "").lower() in ["lod", "nested_lod"]:
                    continue

                raw_calculated_fields.append({
                    "name": name,
                    "tableau_formula": meta.get("formula"),
                    "dependencies": meta.get("dependencies", []),
                    "usage_count": meta.get("usage_count", 0),
                    "type": meta.get("type", "dimension"),
                    "datatype": meta.get("datatype", ""),
                    "role": meta.get("role", "")
                })

        lod_expressions = extract_lod_expressions(calculations) or []
        for lod in lod_expressions:
            if not lod: continue
            pre_resolved = lod.get("table_name") or lod.get("table") or ""
            if not pre_resolved or pre_resolved == "UNKNOWN_TABLE":
                lod["table_name"] = resolve_table_for_lod(lod.get("tableau_formula", ""), lod.get("dimensions", []), combined_tables, raw_calculated_fields, lod_expressions)
            else:
                lod["table_name"] = pre_resolved

        from app.services.calculated_fields_service import resolve_table_for_field
        cf_with_tables = []

        for field in raw_calculated_fields:
            if not field: continue
            table_name = resolve_table_for_field(field, raw_calculated_fields, combined_tables, lod_expressions)
            field["table_name"] = table_name
            cf_with_tables.append(field)

        for lod in lod_expressions:
            if not lod: continue
            pre_resolved = lod.get("table_name") or lod.get("table") or ""
            if not pre_resolved or pre_resolved == "UNKNOWN_TABLE":
                lod["table_name"] = resolve_table_for_lod(lod.get("tableau_formula", ""), lod.get("dimensions", []), combined_tables, raw_calculated_fields, lod_expressions)
            else:
                lod["table_name"] = pre_resolved

        # Await Parameters + Sets
        parameters, sets = await asyncio.gather(params_task, sets_task)
        parameters = parameters or []
        sets = sets or []
        custom_sql = combined_custom_sql_tables

        store_activity(
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            technical_message="Starting Sheet Visual computations for Power BI structural layout.",
            auth_header=auth_header
        )

        try:
            store_log(
                project_id=workbook_metadata["project_id"],
                project_name=project_name,
                workbook_id=workbook_metadata["workbook_id"],
                run_id=run_id,
                function_name=function_name,
                log_level="INFO",
                message="Starting Sheet Visual computations for Power BI structural layout.",
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log Sheet Visual computations start: {e}", extra={"error_type": type(e).__name__})

        sheets_visuals = await asyncio.to_thread(
            build_sheet_visuals,
            payload,
            tables=tables,
            custom_sql=combined_custom_sql_tables,
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            auth_header=auth_header,
            global_sets=sets
        ) or [] # ✅ Ensure it's not None

        store_activity(
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            technical_message="Extracting UI Actions and translating map/filter interactions.",
            auth_header=auth_header
        )

        try:
            store_log(
                project_id=workbook_metadata["project_id"],
                project_name=project_name,
                workbook_id=workbook_metadata["workbook_id"],
                run_id=run_id,
                function_name=function_name,
                log_level="INFO",
                message="Extracting UI Actions and translating map/filter interactions.",
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log UI actions extraction: {e}", extra={"error_type": type(e).__name__})

        actions = convert_actions_to_powerbi(
            actions=raw_actions,
            tables=combined_tables,
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            auth_header=auth_header,
            dashboards=dashboards_list,
            sheets_visuals=sheets_visuals,
            parameters=parameters
        ) or []

        # 🚀 PARALLEL PHASE 3
        if raw_calculated_fields:
            cf_task = asyncio.to_thread(
                convert_calculated_fields_to_powerbi,
                raw_calculated_fields,
                combined_tables,
                parameters,
                sets,
                lod_expressions,
                relationships,
                workbook_metadata["project_id"],
                workbook_metadata["workbook_id"],
                run_id,
                auth_header,
                body.skip_llm
            )
        else:
            async def mock_cf(): return ([], False)
            cf_task = mock_cf()

        if lod_expressions:
            lod_task = asyncio.to_thread(
                convert_lod_to_powerbi,
                lod_expressions,
                combined_tables,
                parameters,
                relationships,
                cf_with_tables,
                workbook_metadata["project_id"],
                workbook_metadata["workbook_id"],
                run_id,
                auth_header,
                body.skip_llm
            )
        else:
            async def mock_lod(): return []
            lod_task = mock_lod()

        cf_result, converted_lods = await asyncio.gather(cf_task, lod_task)

        store_activity(
            project_id=workbook_metadata["project_id"],
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            technical_message="Successfully resolved Complex Calculated Fields and LOD Expressions.",
            auth_header=auth_header
        )

        try:
            store_log(
                project_id=workbook_metadata["project_id"],
                project_name=project_name,
                workbook_id=workbook_metadata["workbook_id"],
                run_id=run_id,
                function_name=function_name,
                log_level="INFO",
                message="Successfully resolved Complex Calculated Fields and LOD Expressions.",
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log resolution of calculated fields and LODs: {e}", extra={"error_type": type(e).__name__})

        # Destructuring safety
        converted_calculated_fields, calc_llm_skipped = cf_result if cf_result else ([], False)
        converted_lods = converted_lods or []

        measures = await asyncio.to_thread(
            convert_measures_to_dax,
            calculations,
            combined_tables,
            combined_custom_sql_tables,
            parameters,
            sets,
            converted_lods,
            converted_calculated_fields,
            relationships,
            workbook_metadata["project_id"],
            workbook_metadata["workbook_id"],
            run_id,
            auth_header,
            body.skip_llm
        ) or []

        try:
            store_log(
                project_id=workbook_metadata["project_id"],
                project_name=project_name,
                workbook_id=workbook_metadata["workbook_id"],
                run_id=run_id,
                function_name=function_name,
                log_level="INFO",
                message=f"Extracted {len(measures)} measures and {len(relationships)} relationships",
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log extraction count: {e}", extra={"error_type": type(e).__name__})

        project_name = resolved_project_name or wb_name or "Unknown"

    except Exception as e:
        logger.error(f"Mapping failed with error: {e}", exc_info=True)

        # Translate technical errors into human-friendly messages
        error_msg = str(e)
        user_friendly_reason = error_msg
        if "Circuit breaker OPEN" in error_msg:
            user_friendly_reason = (
                "The database or downstream service is currently offline or unreachable. "
                "Please try again in a few minutes."
            )
        elif "401" in error_msg or "unauthorized" in error_msg.lower():
            user_friendly_reason = (
                "Authentication failed or token has expired. Please check your credentials or log in again."
            )
        elif "403" in error_msg or "forbidden" in error_msg.lower():
            user_friendly_reason = (
                "Access denied. You do not have permission to access this resource."
            )
        elif "404" in error_msg or "not found" in error_msg.lower():
            user_friendly_reason = (
                "The requested resource was not found. Please verify the project, workbook, and run IDs."
            )
        elif "Connection" in type(e).__name__ or "connect" in error_msg.lower() or "timeout" in error_msg.lower():
            user_friendly_reason = (
                "Unable to connect to the database or downstream service. "
                "Please check the network connection and try again."
            )

        # Log error to CosmosDB so that the failure details are recorded in Cosmos and visible to the UI/logs
        try:
            store_error(
                project_id=body.project_id,
                workbook_id=body.workbook_id,
                run_id=run_id,
                error_msg=f"Mapping failed: {user_friendly_reason}",
                technical_details=traceback.format_exc(),
                auth_header=auth_header
            )
            store_log(
                project_id=body.project_id,
                project_name=project_name,
                workbook_id=body.workbook_id,
                run_id=run_id,
                function_name=function_name,
                log_level="ERROR",
                message=f"Mapping failed: {error_msg}",
                auth_header=auth_header,
                details={"error_details": traceback.format_exc()}
            )
        except Exception as log_exc:
            logger.error(f"Failed to log mapping failure to Cosmos: {log_exc}", exc_info=True)

        raise HTTPException(
            status_code=500,
            detail={"message": "Mapping failed with internal error.", "reason": user_friendly_reason}
        )

    mapping_result = {
        "status": mapping_status,
        "message": mapping_message,
        "error_message": error_message,
        "project_name": project_name,
        "workbook_metadata": workbook_metadata,
        "datasources": datasources,
        "tables": tables,
        "visuals": {
            "sheet_visuals": sheets_visuals,
            "dashboards": dashboards_and_stories.get("dashboards", []) or [],
            "stories": dashboards_and_stories.get("stories", []) or [],
        },
        "relationships": relationships,
        "measures": measures,
        "parameters": parameters,
        "sets": sets,
        "custom_sql": custom_sql,
        "Calculated Fields & LODs":[
            {"calculated_fields": converted_calculated_fields},
            {"lod_expressions": converted_lods}
        ],
        "formatting_and_styling": formatting_and_styling,
        "actions": actions,
        "embedded_assets": embedded_assets,
        "permissions": permissions,
        "artifacts": artifacts,
        "security": security,
        "llm_status": {
            "calculated_fields_converted": not calc_llm_skipped,
            "reason": ("LLM rate limit reached, conversion skipped" if calc_llm_skipped else "Converted successfully")
        }
    }

    store_activity(
        project_id=workbook_metadata["project_id"],
        workbook_id=workbook_metadata["workbook_id"],
        run_id=run_id,
        technical_message="Assembling final mapping payload for Cosmos DB persistence.",
        auth_header=auth_header
    )

    try:
        store_log(
            project_id=workbook_metadata["project_id"],
            project_name=project_name,
            workbook_id=workbook_metadata["workbook_id"],
            run_id=run_id,
            function_name=function_name,
            log_level="INFO",
            message="Assembling final mapping payload for Cosmos DB persistence.",
            auth_header=auth_header
        )
    except Exception as e:
        logger.error(f"Failed to log final payload assembly: {e}", extra={"error_type": type(e).__name__})

    try:
        mapping_result["original_request_payload"] = body.model_dump()
        await asyncio.to_thread(
            store_mapping_in_cosmos,
            {
                "project_id": workbook_metadata["project_id"],
                "project_name": project_name,
                "workbook_id": workbook_metadata["workbook_id"],
                "run_id": run_id,
                "payload": mapping_result
            },
            auth_header
        )
        cosmosdb_status.update({"stored": True, "message": "Results successfully stored in Cosmos DB"})
    except Exception as exc:
        cosmosdb_status.update({"stored": False, "error": str(exc)})
        try:
            store_error(
                project_id=workbook_metadata["project_id"],
                workbook_id=workbook_metadata["workbook_id"],
                run_id=run_id,
                error_msg="Failed to write final mapping payload to CosmosDB.",
                technical_details=str(exc),
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log error to Cosmos: {e}", extra={"error_type": type(e).__name__})

        # Translate technical errors into human-friendly messages
        error_msg = str(exc)
        user_friendly_reason = error_msg
        if "Circuit breaker OPEN" in error_msg:
            user_friendly_reason = (
                "The Cosmos DB service is currently offline or unreachable. "
                "Please try again in a few minutes."
            )
        elif "401" in error_msg or "unauthorized" in error_msg.lower():
            user_friendly_reason = (
                "Authentication failed when communicating with Cosmos DB. Please verify the credentials."
            )
        elif "403" in error_msg or "forbidden" in error_msg.lower():
            user_friendly_reason = (
                "Access denied to Cosmos DB. Please verify that your account has read/write permissions."
            )
        elif "404" in error_msg or "not found" in error_msg.lower():
            user_friendly_reason = (
                "The Cosmos DB resource or container could not be found. Please check database configuration."
            )
        elif "Connection" in type(exc).__name__ or "connect" in error_msg.lower() or "timeout" in error_msg.lower():
            user_friendly_reason = (
                "Unable to connect to the Cosmos DB service. "
                "Please check the network connection and try again."
            )
        logger.warning("Could not save to Cosmos DB API, but mapping was successful. Returning mapping_result anyway.")

    # Even if CosmosDB storage fails, we still want to return the 3-minute hard work!
    return {
        "status": "success",
        "cosmos_db": cosmosdb_status,
        "logs": logs_status,
        "payload": mapping_result
    }
