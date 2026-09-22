# Update: 2026-04-09 - Deployment trigger for Measures, Dimensions, and LODs
import re
from concurrent.futures import ThreadPoolExecutor

from app.core.structured_logger import get_structured_logger
from app.services.confidence_service import evaluate_dax_confidence
from app.services.cosmos_log_service import store_activity, store_error, store_log
from app.services.llm_service import call_llm
from app.utils.dax_column_fixer import validate_and_fix_dax_columns
from app.utils.parameter_schema_helper import build_parameter_schema_lines
from app.utils.prompt_builder import build_prompts
from app.utils.table_extractor import extract_table_from_dax

logger = get_structured_logger(__name__)


# ------------------------------------------------------
# Helper: Normalize Names
# ------------------------------------------------------

def normalize_name(name: str) -> str:
    if not name:
        return ""
    return name.lower().replace(" ", "_")

def _find_col_in_formula(col_name: str, formula: str) -> bool:
    if not col_name or not formula:
        return False
    search_term = col_name.lower()
    text = formula.lower()
    start = 0
    while True:
        idx = text.find(search_term, start)
        if idx == -1:
            return False

        valid_start = (idx == 0) or (not text[idx - 1].isalnum() and text[idx - 1] != "_")
        end_idx = idx + len(search_term)
        valid_end = (end_idx == len(text)) or (not text[end_idx].isalnum() and text[end_idx] != "_")

        if valid_start and valid_end:
            return True

        start = idx + 1


# ------------------------------------------------------
# Resolve Table From Columns
# ------------------------------------------------------

def resolve_table_for_measure(formula: str, tables: list, custom_sql: list = None):
    if not formula:
        return None

    all_tables = (tables or []) + (custom_sql or [])

    fields = re.findall(r"\[([^\]]+)\]", formula)

    for field in fields:
        norm_field_exact = normalize_name(field)
        norm_field_stripped = normalize_name(field.split(" (")[0])

        # 1. Exact Match against physical column names
        for table in all_tables:
            for col in table.get("columns", []):
                names_to_check = [
                    col.get("tableau_column_name"),
                    col.get("bi_column_name"),
                    col.get("tableau_renamed_column_name"),
                    col.get("name")
                ]
                for raw_col in names_to_check:
                    if not raw_col:
                        continue
                    if normalize_name(raw_col) == norm_field_exact and norm_field_exact != "":
                        return table.get("bi_table_name") or table.get("tableau_table_name")

        # 2. Stripped match against physical column names
        for table in all_tables:
            for col in table.get("columns", []):
                names_to_check = [
                    col.get("tableau_column_name"),
                    col.get("bi_column_name"),
                    col.get("tableau_renamed_column_name"),
                    col.get("name")
                ]
                for raw_col in names_to_check:
                    if not raw_col:
                        continue
                    norm_c_stripped = normalize_name(raw_col.split(" (")[0])
                    if norm_c_stripped == norm_field_stripped and norm_field_stripped != "":
                        return table.get("bi_table_name") or table.get("tableau_table_name")

    # 3. Fallback: substring check for unbracketed formulas (e.g. SUM(LineAmount))
    # Collect all matches and choose the one with the longest matching column name to avoid false positives on shorter names
    fallback_matches = []
    for table in all_tables:
        for col in table.get("columns", []):
            names_to_check = [
                col.get("bi_column_name"),
                col.get("tableau_column_name"),
                col.get("tableau_renamed_column_name"),
                col.get("name")
            ]
            for col_name in names_to_check:
                if not col_name:
                    continue

                # Strip outer brackets if present (since the formula is unbracketed here)
                cleaned_col = col_name
                if cleaned_col.startswith("[") and cleaned_col.endswith("]"):
                    cleaned_col = cleaned_col[1:-1]

                tbl = table.get("bi_table_name") or table.get("tableau_table_name")
                if _find_col_in_formula(cleaned_col, formula):
                    fallback_matches.append((len(cleaned_col), tbl))

                if cleaned_col and " (" in cleaned_col:
                    clean_col = cleaned_col.split(" (")[0].strip()
                    if _find_col_in_formula(clean_col, formula):
                        fallback_matches.append((len(clean_col), tbl))

    if fallback_matches:
        fallback_matches.sort(key=lambda x: x[0], reverse=True)
        return fallback_matches[0][1]

    return None


# ------------------------------------------------------
# MAIN FUNCTION
# ------------------------------------------------------
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

def _process_measure(m, schema_context, all_tables, project_id, workbook_id, run_id, auth_header, skip_llm, known_measures=None):
    name = m.get("name")
    tableau_formula = m.get("tableau_formula")
    table_name = m.get("table_name") or "UNKNOWN_TABLE"
    original_type = m.get("original_type", "measure")
    datatype = m.get("datatype", "")
    pbi_datatype = map_power_bi_datatype(datatype)

    try:
        store_log(
            project_id=project_id,
            project_name="Unknown",
            workbook_id=workbook_id,
            run_id=run_id,
            function_name="_process_measure",
            log_level="INFO",
            message=f"Converting measure '{name}' to DAX (Target Table: {table_name})",
            auth_header=auth_header
        )
    except Exception as e:
        logger.error(f"Failed to log measure processing: {e}")

    if skip_llm:
        return {
            "name": name,
            "table": table_name,
            "tableau_datatype": datatype,
            "power_bi_datatype": pbi_datatype,
            "tableau_formula": tableau_formula,
            "dax_formula": "-- SKIPPED BY USER",
            "confidence_score": 100,
            "review_notes": "Skipped LLM conversion"
        }

    try:
        sys_prompt, usr_prompt = build_prompts(
            calc_type="measures",
            item_name=name,
            tableau_formula=tableau_formula,
            table_name=table_name,
            schema_context=schema_context
        )

        dax_formula = call_llm(
            system_prompts=sys_prompt,
            user_prompt=usr_prompt
        )

        # ✅ Validate: fix any column that doesn't exist as bi_column_name in its table
        dax_formula = validate_and_fix_dax_columns(dax_formula, all_tables, known_measures)

        try:
            store_log(
                project_id=project_id,
                project_name="Unknown",
                workbook_id=workbook_id,
                run_id=run_id,
                function_name="_process_measure",
                log_level="INFO",
                message=f"Successfully converted measure '{name}' to DAX",
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log measure success: {e}")

    except Exception as e:
        friendly_type = original_type.replace('_', ' ').title() if original_type else "Measure"
        error_msg = f"Error converting {friendly_type} '{name}' to DAX"
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
            logger.error(f"Failed to log error to Cosmos: {store_e}", extra={"error_type": type(store_e).__name__})
        dax_formula = f"-- ERROR: {str(e)}"

    confidence = evaluate_dax_confidence(
        tableau_formula=tableau_formula,
        dax_formula=dax_formula,
        schema_context=schema_context,
        calc_type=original_type or "measure",
        calc_name=name
    )

    # ✅ FINAL RESOLUTION: Extract from DAX first to ensure alignment, fallback to resolved table
    dax_table = extract_table_from_dax(dax_formula, all_tables)
    final_table_name = table_name
    if dax_table and dax_table != "UNKNOWN_TABLE":
        final_table_name = dax_table
    elif not final_table_name or final_table_name == "UNKNOWN_TABLE":
        final_table_name = dax_table

    return {
        "name": name,
        "table": final_table_name,
        "table_name": final_table_name, # ✅ Added for consistency with LODs/dimensions
        "tableau_datatype": datatype,
        "power_bi_datatype": pbi_datatype,
        "tableau_formula": tableau_formula,
        "dax_formula": dax_formula,
        "confidence_score": confidence.get("confidence_score", -1),
        "review_notes": confidence.get("review_notes", "")
    }


def convert_measures_to_dax(
    calculations: dict,
    tables: list,
    custom_sql: list,
    parameters: list,
    sets: list,
    converted_lods: list,               # ✅ Added
    converted_calculated_fields: list,  # ✅ Added
    relationships: list,                # ✅ Added
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str,
    skip_llm: bool = False
) -> list:

    import os
    debug_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dax_service_debug.log")
    try:
        with open(debug_log_path, "w", encoding="utf-8") as f:
            f.write("--- convert_measures_to_dax started ---\n")
            f.write(f"calculations type: {type(calculations)}\n")
            if isinstance(calculations, dict):
                f.write(f"calculations keys: {list(calculations.keys())}\n")
                cfs = calculations.get("calculated_fields", {})
                f.write(f"calculated_fields type: {type(cfs)}\n")
                if isinstance(cfs, dict):
                    f.write(f"calculated_fields keys: {list(cfs.keys())}\n")
            f.write(f"tables count: {len(tables) if tables else 0}\n")
            f.write(f"converted_lods count: {len(converted_lods) if converted_lods else 0}\n")
            f.write(f"converted_calculated_fields count: {len(converted_calculated_fields) if converted_calculated_fields else 0}\n")
    except Exception as log_ex:
        logger.error(f"Failed to write start debug log: {log_ex}", extra={"error_type": type(log_ex).__name__})

    enriched_measures = []
    calculated_fields = calculations.get("calculated_fields", {})

    if not isinstance(calculated_fields, dict):
        try:
            with open(debug_log_path, "a", encoding="utf-8") as f:
                f.write("FAIL: calculated_fields is not a dictionary\n")
        except Exception as e:
            logger.error(f"Failed to write debug log: {e}")
        return []

    try:
        store_activity(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            technical_message="Building schema context and starting LLM conversion for Measures and Table Calculations.",
            auth_header=auth_header
        )
    except Exception as e:
        logger.error(f"Failed to log activity for measures: {e}", extra={"error_type": type(e).__name__})

    try:
        # ------------------------------------------------------
        # Build Schema Context
        # ------------------------------------------------------

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

        if sets:
            schema_lines.append("\nAVAILABLE SETS:")
            for s in sets:
                set_name = s.get("name", "")
                if set_name:
                    powerbi_data = s.get("powerbi", {})
                    target_table = powerbi_data.get("target_table", "Unknown")
                    stype = s.get("type", "")

                    calc_col = f"{set_name}_In_Out"
                    base_col = "UnknownColumn"
                    set_dax = powerbi_data.get("dax", "")

                    if stype == "static":
                        if set_dax:
                            base_col_match = re.search(r"'[^']+'\[(.*?)\]", set_dax)
                            base_col = base_col_match.group(1) if base_col_match else "UnknownColumn"
                            calc_col_match = re.search(r"^(.*?)\s*=", set_dax)
                            calc_col = calc_col_match.group(1).strip() if calc_col_match else f"{set_name}_Category"
                    elif stype == "dynamic":
                        cols = powerbi_data.get("calculated_columns", [])
                        if cols:
                            for c in cols:
                                cname = c.get("name", "")
                                if "In_Out" in cname or "Category" in cname:
                                    calc_col = cname
                            if calc_col == f"{set_name}_In_Out":
                                calc_col = cols[-1].get("name", "")

                        base_col = "<BaseColumnOfSet>"
                        set_dax = "Dynamic set evaluated via multiple calculated columns."

                    schema_lines.append(f"- Set [{set_name}] on table '{target_table}'. Definition: {set_dax}")
                    schema_lines.append(f"  CRITICAL RULE FOR SETS: If comparing a DIFFERENT table's column to this set, you MUST dynamically resolve the set values using CALCULATETABLE. Use this exact pattern: SELECTEDVALUE('OtherTable'[Col]) IN CALCULATETABLE(VALUES('{target_table}'[{base_col}]), REMOVEFILTERS('{target_table}'), '{target_table}'[{calc_col}] = \"In\")")
                    schema_lines.append(f"  If referencing the same table, you can use '{target_table}'[{calc_col}] = \"In\".")


        # ✅ Inject converted calculated fields as virtual columns so the LLM
        #    recognises names like 'Order Date' instead of substituting 'InvoiceDate'
        cf_by_table = {}
        if converted_calculated_fields:
            for cf in converted_calculated_fields:
                if cf and cf.get("type") == "dimension":
                    tbl = cf.get("table_name", "")
                    if tbl:
                        cf_by_table.setdefault(tbl, []).append(cf.get("name"))
        if converted_lods:
            for lod in converted_lods:
                if lod and lod.get("type") == "dimension":
                    tbl = lod.get("table_name", "")
                    if tbl:
                        cf_by_table.setdefault(tbl, []).append(lod.get("name"))

        for tbl_name, cf_names in cf_by_table.items():
            schema_lines.append(
                f"- Table '{tbl_name}' (Calculated Columns): [{', '.join(cf_names)}]"
            )

        if relationships:
            schema_lines.append("\nAVAILABLE RELATIONSHIPS:")
            for r in relationships:
                pbi_rel = r.get("power_bi", {})
                if pbi_rel:
                    from_table = pbi_rel.get("fromTable", "")
                    to_table = pbi_rel.get("toTable", "")
                    cardinality = pbi_rel.get("cardinality", "")
                    if from_table and to_table:
                        from_side = "Many" if "ManyTo" in cardinality else "One"
                        to_side = "Many" if "ToMany" in cardinality else "One"
                        schema_lines.append(f"- {from_table} (Side: {from_side}) to {to_table} (Side: {to_side})")

        if parameters:
            param_lines = build_parameter_schema_lines(parameters)
            if param_lines:
                schema_lines.extend(param_lines)

        # NOTE: schema_context is finalized AFTER measure extraction below
        #       so we can inject measure names into the context

        # ------------------------------------------------------
        # Extract Measures
        # ------------------------------------------------------

        raw_measures = []

        for name, meta in calculated_fields.items():
            if not isinstance(meta, dict):
                continue

            role = (meta.get("role") or "").lower()
            calc_type = (meta.get("type") or "").lower()

            is_m = False
            if role == "measure":
                is_m = True
            elif role == "dimension":
                is_m = False
            else:
                is_m = calc_type in ["measure", "table_calculation", "lod", "nested_lod"]

            if not is_m:
                continue

            tableau_formula = meta.get("formula")

            if not tableau_formula:
                continue

            raw_measures.append({
                "name": name,
                "tableau_formula": tableau_formula,
                "original_type": calc_type,
                "datatype": meta.get("datatype", ""),
                "role": role,
                "table_name": meta.get("table_name") or meta.get("table")
            })

        # ------------------------------------------------------
        # Inject Available Measures into Schema Context
        # so LLM knows which names are MEASURES (not columns)
        # and should NEVER be table-qualified
        # ------------------------------------------------------
        all_measure_names = [m["name"] for m in raw_measures]

        # Include calculated fields that were converted as measures
        for cf in converted_calculated_fields:
            if not cf: continue
            cf_name = cf.get("name")
            if cf.get("type") == "measure" and cf_name and cf_name not in all_measure_names:
                all_measure_names.append(cf_name)

        if all_measure_names:
            schema_lines.append("\nAvailable Measures (NEVER table-qualify these — reference as [MeasureName] only):")
            for mn in all_measure_names:
                schema_lines.append(f"- [{mn}]")

        # ✅ Explicitly list LOD expressions with their DAX and FIXED dimensions
        #    so the LLM uses SUMX iterators, NOT SUM('Table'[LODName])
        lods_as_measures = [lod for lod in converted_lods if lod and lod.get("type", "measure") == "measure"]
        if lods_as_measures:
            schema_lines.append("\nLOD EXPRESSIONS (these are PRE-EXISTING MEASURES, NOT physical columns):")
            schema_lines.append("CRITICAL: LODs are MEASURES. NEVER reference as 'Table'[LODName].")
            schema_lines.append("When Tableau uses SUM([LODName]) or AVG([LODName]), and the LOD is FIXED, you MUST use an iterator like SUMX over the EXACT FIXED dimension(s) of the LOD.")
            schema_lines.append("CRITICAL: Use SUMX(VALUES('Table'[FixedDim]), [LODName]). NEVER iterate over a more granular ID (like Transaction ID) if the LOD is fixed at a higher grain (like Customer ID), as it causes double-counting.")
            schema_lines.append("Use the FIXED dimension(s) listed below as the iteration column.")
            for lod in lods_as_measures:
                lod_name = lod.get("name", "")
                lod_dax = lod.get("dax_formula", "")
                lod_type = lod.get("lod_type", "")
                lod_table = lod.get("table_name", "")
                tableau_formula = lod.get("tableau_formula", "")

                # Extract actual FIXED dimensions from the Tableau formula
                # Pattern: { FIXED [dim1], [dim2] : aggregation }
                fixed_dims_parsed = []
                if tableau_formula and isinstance(tableau_formula, str):
                    fixed_match = re.search(r'FIXED\s+(.*?)\s*:', tableau_formula, re.IGNORECASE | re.DOTALL)
                    if fixed_match:
                        fixed_dims_parsed = re.findall(r'\[([^\]]+)\]', fixed_match.group(1))

                if lod_name:
                    if fixed_dims_parsed:
                        dims_hint = ", ".join(fixed_dims_parsed)
                    elif lod_type == "fixed" or not lod_type:
                        # Table-scoped FIXED (no explicit dims) — it has NO fixed dimensions
                        dims_hint = "None (Table-scoped)"
                    else:
                        # INCLUDE/EXCLUDE or other types — use dimensions from JSON
                        dims_hint = ", ".join(lod.get("dimensions", [])) or "N/A"
                    schema_lines.append(f"- [{lod_name}] (type: {lod_type}, table: {lod_table}, fixed_dims: [{dims_hint}]) = {lod_dax}")

        schema_context = "\n".join(schema_lines)

        # ------------------------------------------------------
        # PASS 1: Resolve table from columns
        # ------------------------------------------------------

        for m in raw_measures:
            if not m.get("table_name") or m.get("table_name") == "UNKNOWN_TABLE":
                m["table_name"] = resolve_table_for_measure(
                    m.get("tableau_formula"),
                    tables,
                    custom_sql
                )

        # ------------------------------------------------------
        # PASS 2: Resolve table via dependent measures
        # ------------------------------------------------------

        measure_lookup = {m["name"]: m for m in raw_measures}

        for m in raw_measures:

            if m.get("table_name"):
                continue

            formula = m.get("tableau_formula", "")
            fields = re.findall(r"\[([^\]]+)\]", formula)

            resolved = False
            for field in fields:
                dep_measure = measure_lookup.get(field)
                if dep_measure and dep_measure.get("table_name"):
                    m["table_name"] = dep_measure["table_name"]
                    resolved = True
                    break

            if not resolved:
                # Fallback: substring check for unbracketed formulas
                for m_name, dep_measure in measure_lookup.items():
                    if _find_col_in_formula(m_name, formula) and dep_measure.get("table_name") and m_name != m["name"]:
                        m["table_name"] = dep_measure["table_name"]
                        break

        # ------------------------------------------------------
        # PASS 3: Resolve table via calculated dimensions
        # ------------------------------------------------------

        calculated_lookup = {}

        # ✅ Read directly from the newly passed-in calculated fields
        for cf in converted_calculated_fields:
            if not cf: continue
            name = cf.get("name")
            table_name = cf.get("table_name") or cf.get("table")
            if name and table_name:
                calculated_lookup[normalize_name(name)] = table_name

        # ✅ Read directly from the newly passed-in LODs
        for lod in converted_lods:
            if not lod: continue
            name = lod.get("name")
            table_name = lod.get("table_name") or lod.get("table")
            if name and table_name:
                calculated_lookup[normalize_name(name)] = table_name

        for m in raw_measures:

            if m.get("table_name"):
                continue

            formula = m.get("tableau_formula", "")
            fields = re.findall(r"\[([^\]]+)\]", formula)

            resolved = False
            for field in fields:

                # Clean Tableau suffix like "(Custom SQL Query)"
                clean_field = field.split(" (")[0]

                # Normalize name for matching
                norm_field = normalize_name(clean_field)

                calc_table = calculated_lookup.get(norm_field)

                if calc_table:

                    m["table_name"] = calc_table
                    resolved = True
                    break

            if not resolved:
                # Fallback: substring match on unbracketed formulas
                all_cfs = (converted_calculated_fields or []) + (converted_lods or [])
                for cf in all_cfs:
                    if not cf: continue
                    cf_name = cf.get("name", "")
                    if _find_col_in_formula(cf_name, formula):
                        tbl = cf.get("table_name") or cf.get("table")
                        if tbl:
                            m["table_name"] = tbl
                        break

        # ------------------------------------------------------
        # Convert to DAX using LLM
        # ------------------------------------------------------

        all_tables = (tables or []) + (custom_sql or [])
        measure_tasks = []

        for m in raw_measures:
            target_table_name = m.get("table_name") or "UNKNOWN_TABLE"
            formula = m.get("tableau_formula", "")
            referenced_cols = re.findall(r"\[([^\]]+)\]", formula)

            measure_hints = []
            for ref_col in referenced_cols:
                source_table = ""
                # Lookup table for the referenced column
                for t in all_tables:
                    for c in t.get("columns", []):
                        col_tab_name = c.get("tableau_column_name")
                        if col_tab_name == ref_col:
                            source_table = t.get("bi_table_name") or t.get("tableau_table_name") or t.get("table_name")
                            break
                    if source_table: break

                if source_table and source_table != target_table_name:
                    # Look up relationship
                    for r in (relationships or []):
                        pbi_rel = r.get("power_bi", {})
                        from_t = pbi_rel.get("fromTable")
                        to_t = pbi_rel.get("toTable")
                        card = pbi_rel.get("cardinality", "")

                        # CASE 1: Base Table is 'One', Source Table is 'Many'
                        if (from_t == target_table_name and to_t == source_table and "OneTo" in card) or \
                           (from_t == source_table and to_t == target_table_name and "ManyTo" in card):
                            measure_hints.append(f"\nCRITICAL CONVERSION HINT: Measure '{m['name']}' is on table '{target_table_name}' (One side) but references column '{ref_col}' from table '{source_table}' (Many side). You MUST wrap this reference in an aggregator. Prefer a simple aggregation like SUM('{source_table}'[{ref_col}]) if the logic is direct, or SUMX(RELATEDTABLE('{source_table}'), '{source_table}'[{ref_col}]) if complex logic is required. AVOID using SUMX(VALUES('{source_table}'[ID]), ...) for simple cross-table sums.")
                            break

                        # CASE 2: Base Table is 'Many', Source Table is 'One'
                        if (from_t == target_table_name and to_t == source_table and "ManyTo" in card) or \
                           (from_t == source_table and to_t == target_table_name and "OneTo" in card):
                            measure_hints.append(f"\nCRITICAL CONVERSION HINT: Measure '{m['name']}' is on table '{target_table_name}' (Many side) and references column '{ref_col}' from table '{source_table}' (One side). You MUST use RELATED('{source_table}'[{ref_col}]) if referencing the column directly in a scalar context, or simple aggregations like SUM('{source_table}'[{ref_col}]) which will filter correctly via the relationship.")
                            break

            # Combine global context with measure-specific hints
            specific_schema = schema_context + "".join(measure_hints)
            measure_tasks.append((m, specific_schema))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(
                    _process_measure,
                    task[0],
                    task[1],
                    all_tables,
                    project_id,
                    workbook_id,
                    run_id,
                    auth_header,
                    skip_llm,
                    all_measure_names
                )
                for task in measure_tasks
            ]

            for future in futures:
                enriched_measures.append(future.result())

        try:
            store_log(
                project_id=project_id,
                project_name="Unknown",
                workbook_id=workbook_id,
                run_id=run_id,
                function_name="convert_measures_to_dax",
                log_level="INFO",
                message=f"Successfully processed {len(enriched_measures)} measures.",
                auth_header=auth_header
            )
        except Exception as e:
            logger.error(f"Failed to log measures completion: {e}", extra={"error_type": type(e).__name__})

        return enriched_measures

    except Exception as e:
        # 🚨 OUTER SAFETY NET: Catch any unexpected fatal error
        import traceback
        try:
            with open(debug_log_path, "a", encoding="utf-8") as f:
                f.write(f"Exception occurred in outer try: {e}\n")
                f.write(traceback.format_exc() + "\n")
        except Exception as e_log:
            logger.error(f"Failed to write debug log: {e_log}")
        try:
            store_error(
                project_id=project_id,
                workbook_id=workbook_id,
                run_id=run_id,
                error_msg="Critical failure in convert_measures_to_dax.",
                technical_details=str(e),
                auth_header=auth_header
            )
        except Exception as store_e:
            logger.error(f"Critical failure in convert_measures_to_dax and store_error failed: {e}. Store error: {store_e}")
        return enriched_measures
