# Update: 2026-04-09 - Deployment trigger for Measures, Dimensions, and LODs
import re
from concurrent.futures import ThreadPoolExecutor

from app.core.structured_logger import get_structured_logger
from app.services.confidence_service import evaluate_dax_confidence

# 1️⃣ IMPORT YOUR COSMOS LOGGING WRAPPERS
from app.services.cosmos_log_service import store_activity, store_error, store_log
from app.services.llm_service import call_llm
from app.services.rules import ALL_PROMPTS_TABLEAU_TO_DAX
from app.utils.dax_column_fixer import validate_and_fix_dax_columns
from app.utils.prompt_builder import build_prompts

logger = get_structured_logger(__name__)
from app.utils.parameter_schema_helper import build_parameter_schema_lines
from app.utils.table_extractor import extract_table_from_dax

# --------------------------------------------------
# Pick rule prompt by LOD type
# --------------------------------------------------

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
def get_rule_prompt(lod_type: str):
    for rule in ALL_PROMPTS_TABLEAU_TO_DAX:
        if rule["type"] == lod_type.lower():
            return rule["prompt"]
    return None

def format_dimensions(table: str, dimensions: list) -> str:
    if not dimensions:
        return ""
    return ", ".join([f"'{table}'[{dim}]" for dim in dimensions])


# --------------------------------------------------
# MAIN: Convert LOD → Power BI DAX (RULE-DRIVEN)
# --------------------------------------------------

# 2️⃣ UPDATE SIGNATURE TO ACCEPT TABLES AND TRACKING IDs
def _process_lod(lod, schema_context, tables, project_id, workbook_id, run_id, auth_header, skip_llm, known_measures=None, parameters=None):
    from app.services.calculated_fields_service import normalize_name

    tableau_expr = lod.get("tableau_formula")
    dimensions = lod.get("dimensions", [])
    datatype = lod.get("datatype", "")
    pbi_datatype = map_power_bi_datatype(datatype)

    # ✅ USE PRE-RESOLVED TABLE NAME FROM mapping.py (resolve_table_for_lod)
    resolved_table = lod.get("table_name") or lod.get("table") or ""

    # Fallback: if mapping.py didn't resolve it, try locally with normalized matching
    if not resolved_table:
        involved_tables = set()
        for dim in dimensions:
            norm_dim = normalize_name(dim)
            for t in tables:
                for col in t.get("columns", []):
                    names_to_check = [
                        col.get("bi_column_name"),
                        col.get("tableau_column_name"),
                        col.get("tableau_renamed_column_name"),
                        col.get("name")
                    ]
                    found = False
                    for raw_c in names_to_check:
                        if not raw_c:
                            continue
                        # Strip brackets to handle raw names
                        if raw_c.startswith("[") and raw_c.endswith("]"):
                            raw_c = raw_c[1:-1]
                        if normalize_name(raw_c) == norm_dim and norm_dim != "":
                            involved_tables.add(t.get("bi_table_name") or t.get("tableau_table_name") or t.get("table_name"))
                            found = True
                            break
                    if found:
                        break
        resolved_table = list(involved_tables)[0] if involved_tables else ""

    # ✅ Second Fallback: If dimensions didn't yield a table, scan the formula itself
    if not resolved_table and tableau_expr:
        fields_in_formula = re.findall(r"\[([^\]]+)\]", tableau_expr)
        for f in fields_in_formula:
            norm_f = normalize_name(f)
            for t in tables:
                for col in t.get("columns", []):
                    raw_c = col.get("bi_column_name") or col.get("tableau_column_name") or col.get("name") or ""
                    if normalize_name(raw_c) == norm_f:
                        resolved_table = t.get("bi_table_name") or t.get("tableau_table_name") or t.get("table_name")
                        break
                if resolved_table: break
            if resolved_table: break

    # ✅ Third Fallback: Substring check for unbracketed formulas
    if not resolved_table and tableau_expr:
        from app.services.dax_service import _find_col_in_formula
        for t in tables:
            for col in t.get("columns", []):
                raw_c = col.get("bi_column_name") or col.get("tableau_column_name") or col.get("name") or ""
                if _find_col_in_formula(raw_c, tableau_expr):
                    resolved_table = t.get("bi_table_name") or t.get("tableau_table_name") or t.get("table_name")
                    break
            if resolved_table: break

    # Pass table hint so LLM generates correct table references
    table_name_hint = resolved_table or "Unknown"
    lod_name = lod.get("name", "Unknown_LOD")

    role = (lod.get("role") or "").lower()
    calc_type = (lod.get("type") or "").lower()

    # DYNAMIC TYPE SWITCH: Check for parameter dependencies or user functions
    parameter_names = {p.get("name") for p in (parameters or []) if p.get("name")}
    has_parameter_dependency = any(dep in parameter_names for dep in dimensions)

    formula_upper = (tableau_expr or "").upper()
    has_user_function = any(fn in formula_upper for fn in [
        "USERNAME()", "USERPRINCIPALNAME()", "CUSTOMDATA()", "USERCULTURE()"
    ])

    is_m = True
    if role == "measure":
        is_m = True
    elif role == "dimension":
        is_m = False
    else:
        if calc_type == "dimension":
            is_m = False

    # Force to measure if it has parameter dependencies or user functions
    if has_parameter_dependency or has_user_function:
        is_m = True

    if skip_llm:
        return {
            "name": lod_name,
            "lod_type": lod.get("lod_type"),
            "dimensions": dimensions,
            "tableau_datatype": datatype,
            "power_bi_datatype": pbi_datatype,
            "table_name": resolved_table,
            "tableau_formula": tableau_expr,
            "dax_formula": "-- SKIPPED BY USER",
            "confidence_score": 100,
            "review_notes": "Skipped LLM conversion",
            "type": "measure" if is_m else "dimension",
            "role": role
        }

    try:
        # ✅ PASS SCHEMA CONTEXT TO PROMPT
        current_schema = schema_context
        if not is_m:
            current_schema += "\nCRITICAL CONVERSION HINT: This LOD expression must be converted as a Power BI CALCULATED COLUMN (Dimension), not a measure. Therefore, do NOT apply Measure-specific rules (like wrapping table column references in SELECTEDVALUE() as in Rule L8). Translate it as a row-level calculated column."

        sys_prompt, usr_prompt = build_prompts(
            calc_type="lods",
            item_name=lod_name,
            tableau_formula=tableau_expr,
            table_name=table_name_hint,
            schema_context=current_schema
        )

        dax = call_llm(system_prompts=sys_prompt, user_prompt=usr_prompt)

        # ✅ Validate: fix any column that doesn't exist as bi_column_name in its table
        dax = validate_and_fix_dax_columns(dax, tables, known_measures)

    except Exception as e:
        error_msg = f"Error converting LOD '{lod_name}'"
        print(f"{error_msg}: {e}")

        try:
            store_error(
                project_id=project_id,
                workbook_id=workbook_id,
                run_id=run_id,
                error_msg=error_msg,
                technical_details=str(e),
                auth_header=auth_header
            )
        except Exception as store_e:
            logger.error(f"Failed to log LOD error to Cosmos: {e}. Store error: {store_e}", extra={"error_type": type(store_e).__name__})
        dax = f"-- ERROR CONVERTING: {str(e)}"

    # ✅ Evaluate DAX confidence using second LLM pass
    confidence = evaluate_dax_confidence(
        tableau_formula=tableau_expr,
        dax_formula=dax.strip() if dax else "",
        schema_context=schema_context,
        calc_type="lod",
        calc_name=lod_name
    )

    # ✅ FINAL RESOLUTION: Extract from DAX first to ensure alignment, fallback to resolved table
    dax_table = extract_table_from_dax(dax, tables)
    final_table_name = resolved_table
    if dax_table and dax_table != "UNKNOWN_TABLE":
        final_table_name = dax_table
    elif not final_table_name or final_table_name == "UNKNOWN_TABLE":
        final_table_name = dax_table

    return {
        "name": lod_name,
        "lod_type": lod.get("lod_type"),
        "dimensions": dimensions,
        "tableau_datatype": datatype,
        "power_bi_datatype": pbi_datatype,
        "table_name": final_table_name,
        "tableau_formula": tableau_expr,
        "dax_formula": dax.strip() if dax else "",
        "confidence_score": confidence.get("confidence_score", -1),
        "review_notes": confidence.get("review_notes", ""),
        "type": "measure" if is_m else "dimension",
        "role": role
    }

def convert_lod_to_powerbi(
    lod_expressions: list,
    tables: list,
    parameters: list,
    relationships: list,
    calculated_fields: list,   # ✅ Added: Calculated field names for schema context
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str,
    skip_llm: bool = False
) -> list:
    converted = []

    # 3️⃣ LOG ACTIVITY: Indicate the agent is starting work on LODs
    try:
        store_activity(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            technical_message=f"Starting LLM conversion for {len(lod_expressions)} LOD expressions.",
            auth_header=auth_header
        )
    except Exception as e:
        logger.error(f"Failed to log LOD activity: {e}", extra={"error_type": type(e).__name__})

    try:
        # ✅ BUILD SCHEMA CONTEXT TO PREVENT "UNKNOWN TABLE"
        schema_lines = []
        for t in tables:
            t_name = t.get("bi_table_name") or t.get("tableau_table_name", "Unknown")
            col_entries = []
            for c in t.get("columns", []):
                tab_name = c.get("tableau_column_name")
                bi_name = c.get("bi_column_name")
                if tab_name:
                    if bi_name and bi_name != tab_name:
                        col_entries.append(f"{bi_name} (Source: {tab_name})")
                    else:
                        col_entries.append(tab_name)
            schema_lines.append(f"- Table '{t_name}': [{', '.join(col_entries)}]")

        # ✅ Append Parameters to the Schema Context for the LLM
        if parameters:
            schema_lines.extend(build_parameter_schema_lines(parameters))

        # ✅ FIX 1: EXPLICITLY LABEL FACT vs DIM TABLES FOR THE LLM
        if relationships:
            schema_lines.append("\nAVAILABLE RELATIONSHIPS:")
            for r in relationships:
                pbi_rel = r.get("power_bi", {})
                if pbi_rel:
                    from_table_node = pbi_rel.get("fromTable", "")
                    to_table_node = pbi_rel.get("toTable", "")
                    cardinality_node = pbi_rel.get("cardinality", "")
                    if from_table_node and to_table_node:
                        from_side_node = "Many" if "ManyTo" in cardinality_node else "One"
                        to_side_node = "Many" if "ToMany" in cardinality_node else "One"
                        schema_lines.append(f"- {from_table_node} (Side: {from_side_node}) to {to_table_node} (Side: {to_side_node})")

        # ✅ Inject calculated fields as virtual columns so the LLM
        #    recognises names like 'Order Date' instead of substituting 'InvoiceDate'
        # ✅ Inject calculated fields as virtual columns for the LLM
        if calculated_fields:
            cf_by_table = {}
            referenced_cf_defs = []

            # Identify which fields are actually referenced in the LOD formulas
            all_lod_text = " ".join([l.get("tableau_formula", "") for l in lod_expressions])
            referenced_names = set(re.findall(r"\[([^\]]+)\]", all_lod_text))

            for cf in calculated_fields:
                cf_name = cf.get("name")
                cf_type = cf.get("type", "dimension")
                tbl = cf.get("table_name", "")

                # 1. Add to table-specific list for dimensions
                if cf_type == "dimension" and tbl:
                    cf_by_table.setdefault(tbl, []).append(cf_name)

                # 2. Add definition if referenced in any LOD
                if cf_name in referenced_names:
                    f = cf.get("tableau_formula") or cf.get("formula")
                    if f:
                        referenced_cf_defs.append(f"- [{cf_name}]: {f}")

            # Append dimensions to table lists
            for tbl_name, cf_names in cf_by_table.items():
                schema_lines.append(
                    f"- Table '{tbl_name}' (Calculated Columns): [{', '.join(cf_names)}]"
                )

            # Append referenced definitions
            if referenced_cf_defs:
                schema_lines.append("\nREFERENCED CALCULATED FIELDS (DEFINITIONS):")
                schema_lines.extend(referenced_cf_defs)

        schema_context = "\n".join(schema_lines)

        # Collect known measures
        all_measure_names = []
        if calculated_fields:
            for cf in calculated_fields:
                if cf.get("type", "dimension").lower() != "dimension":
                    all_measure_names.append(cf.get("name"))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(
                    _process_lod,
                    lod,
                    schema_context,
                    tables,
                    project_id,
                    workbook_id,
                    run_id,
                    auth_header,
                    skip_llm,
                    all_measure_names,
                    parameters
                )
                for lod in lod_expressions
            ]
            for future in futures:
                converted.append(future.result())

        # 5️⃣ LOG STANDARD: Successfully finished all LOD conversions
        try:
            store_log(
                project_id=project_id,
                project_name="Unknown",
                workbook_id=workbook_id,
                run_id=run_id,
                function_name="convert_lod_to_powerbi",
                log_level="INFO",
                message=f"Successfully processed and converted {len(converted)} LOD expressions.",
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log LOD completion: {e}", extra={"error_type": type(e).__name__})

        return converted

    except Exception as e:
        # 🚨 OUTER SAFETY NET: Catch any unexpected fatal error
        try:
            store_error(
                project_id=project_id,
                workbook_id=workbook_id,
                run_id=run_id,
                error_msg="Critical failure in convert_lod_to_powerbi.",
                technical_details=str(e),
                auth_header=auth_header
            )
        except Exception as store_e:
            logger.error(f"Critical failure in convert_lod_to_powerbi and store_error failed: {e}. Store error: {store_e}")
        return converted
