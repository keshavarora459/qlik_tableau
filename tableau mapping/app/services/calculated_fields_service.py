# Update: 2026-04-09 - Deployment trigger for Measures, Dimensions, and LODs
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor

# Import your Cosmos DB loggers
from app.services.agent_logger import send_activity_to_api
from app.services.confidence_service import evaluate_dax_confidence
from app.services.cosmos_log_service import store_error
from app.services.llm_service import call_llm
from app.utils.dax_column_fixer import validate_and_fix_dax_columns
from app.utils.parameter_schema_helper import build_parameter_schema_lines
from app.utils.prompt_builder import build_prompts
from app.utils.table_extractor import extract_table_from_dax


# ---------------------------------------------------------
# Helper to run async logs safely from a synchronous function
# ---------------------------------------------------------
def _fire_log_safely(coro):
    """Safely executes async logging whether in an active event loop or not."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)  # Fire and forget if already in an async context
    except RuntimeError:
        try:
            asyncio.run(coro)       # Run synchronously if in a standard thread
        except Exception as e:
            print(f"Error in _fire_log_safely asyncio.run: {e}")
    except Exception as e:
        print(f"Error in _fire_log_safely: {e}")

# ---------------------------------------------------------
# Extract Function
# ---------------------------------------------------------
def extract_calculated_fields(payload: dict) -> list:
    try:
        calculations = payload.get("calculations_and_lods") or payload.get("calculations") or {}
        calculated_fields = calculations.get("calculated_fields", {})

        results = []
        if isinstance(calculated_fields, dict):
            for name, field in calculated_fields.items():
                if not isinstance(field, dict): continue
                results.append({
                    "name": name,
                    "tableau_formula": field.get("formula"),
                    "dependencies": field.get("dependencies", []),
                    "usage_count": field.get("usage_count", 0),
                    "type": field.get("type", "dimension"),
                    "role": field.get("role", "")
                })
        return results
    except Exception as e:
        print(f"Failed to extract calculated fields: {e}")
        return []

# ---------------------------------------------------------
# Table Resolution Helpers
# ---------------------------------------------------------
def normalize_name(name: str) -> str:
    """
    Robust normalization: removes spaces, underscores, and parentheses
    to match Tableau formula references with schema column names.
    """
    try:
        if not name:
            return ""
        # Converts "Date (Loan)" or "Client Id" to "dateloan" or "clientid"
        return name.lower().replace(" ", "").replace("_", "").replace("(", "").replace(")", "")
    except Exception as e:
        print(f"Error in normalize_name: {e}")
        return str(name) if name else ""

def _find_col_in_formula(col_name: str, formula: str) -> bool:
    try:
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
    except Exception as e:
        print(f"Error in _find_col_in_formula: {e}")
        return False

# gemini/app/services/calculated_fields_service.py

def _resolve_table_for_field_unsafe(field, all_fields, combined_tables, converted_lods=None, depth=0):
    """
    Resolves table names by checking physical columns,
    inherited Calculated Field dependencies (dimensions/measures), and LOD dependencies.
    Uses recursion to resolve dependencies that haven't been resolved yet.
    """
    if depth > 10:
        return ""

    if field.get("table_name") and field.get("table_name") not in ["", "UNKNOWN_TABLE"]:
        return field.get("table_name")

    formula = field.get("tableau_formula", "")
    dependencies = field.get("dependencies", [])

    # 1. Match against physical columns in the schema
    fields_in_formula = re.findall(r"\[([^\]]+)\]", formula)

    for f in fields_in_formula:
        norm_f_exact = normalize_name(f)
        norm_f_stripped = normalize_name(f.split(" (")[0])

        # 1a. Exact Match against physical column names
        for table in combined_tables:
            for c in table.get("columns", []):
                names_to_check = [
                    c.get("tableau_column_name"),
                    c.get("bi_column_name"),
                    c.get("tableau_renamed_column_name"),
                    c.get("name")
                ]
                for raw_c in names_to_check:
                    if not raw_c:
                        continue
                    if normalize_name(raw_c) == norm_f_exact and norm_f_exact != "":
                        return table.get("tableau_table_name") or table.get("table_name") or table.get("bi_table_name")

        # 1b. Stripped Match against physical column names
        for table in combined_tables:
            for c in table.get("columns", []):
                names_to_check = [
                    c.get("tableau_column_name"),
                    c.get("bi_column_name"),
                    c.get("tableau_renamed_column_name"),
                    c.get("name")
                ]
                for raw_c in names_to_check:
                    if not raw_c:
                        continue
                    norm_c_stripped = normalize_name(raw_c.split(" (")[0])
                    if norm_c_stripped == norm_f_stripped and norm_f_stripped != "":
                        return table.get("tableau_table_name") or table.get("table_name") or table.get("bi_table_name")

    # 1c. Match explicit dependencies against physical columns
    for dep in dependencies:
        norm_dep = normalize_name(dep)
        for table in combined_tables:
            for c in table.get("columns", []):
                names_to_check = [
                    c.get("tableau_column_name"),
                    c.get("bi_column_name"),
                    c.get("tableau_renamed_column_name"),
                    c.get("name")
                ]
                for raw_c in names_to_check:
                    if not raw_c:
                        continue
                    if normalize_name(raw_c) == norm_dep and norm_dep != "":
                        return table.get("tableau_table_name") or table.get("table_name") or table.get("bi_table_name")

    # 1d. Fallback for physical columns without brackets (using substring match)
    # Collect all matches and choose the one with the longest matching column name to avoid false positives on shorter names
    fallback_matches = []
    for table in combined_tables:
        for c in table.get("columns", []):
            names_to_check = [
                c.get("bi_column_name"),
                c.get("tableau_column_name"),
                c.get("tableau_renamed_column_name"),
                c.get("name")
            ]
            for raw_c in names_to_check:
                if not raw_c:
                    continue

                cleaned_c = raw_c
                if cleaned_c.startswith("[") and cleaned_c.endswith("]"):
                    cleaned_c = cleaned_c[1:-1]

                tbl = table.get("tableau_table_name") or table.get("table_name") or table.get("bi_table_name")
                if _find_col_in_formula(cleaned_c, formula):
                    fallback_matches.append((len(cleaned_c), tbl))
                if cleaned_c and " (" in cleaned_c:
                    clean_c = cleaned_c.split(" (")[0].strip()
                    if _find_col_in_formula(clean_c, formula):
                        fallback_matches.append((len(clean_c), tbl))

    if fallback_matches:
        fallback_matches.sort(key=lambda x: x[0], reverse=True)
        return fallback_matches[0][1]

    # 2. Inherit from Parent Calculated Fields
    for other_field in all_fields:
        if other_field == field:
            continue

        f_name = other_field.get("name")
        if f_name in dependencies or _find_col_in_formula(f_name, formula):
            dep_table = other_field.get("table_name")
            if not dep_table or dep_table in ["", "UNKNOWN_TABLE"]:
                dep_table = resolve_table_for_field(other_field, all_fields, combined_tables, converted_lods, depth + 1)
                other_field["table_name"] = dep_table

            if dep_table and dep_table not in ["", "UNKNOWN_TABLE"]:
                return dep_table

    # 3. Inherit from Parent LODs
    if converted_lods:
        for lod in converted_lods:
            lod_name = lod.get("name")
            if lod_name in dependencies or _find_col_in_formula(lod_name, formula):
                lod_table = lod.get("table_name")
                if lod_table:
                    return lod_table

    return ""

def resolve_table_for_field(field, all_fields, combined_tables, converted_lods=None, depth=0):
    try:
        return _resolve_table_for_field_unsafe(field, all_fields, combined_tables, converted_lods, depth)
    except Exception as e:
        print(f"Error in resolve_table_for_field: {e}")
        return ""
def map_power_bi_datatype(tableau_type: str) -> str:
    try:
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
    except Exception as e:
        print(f"Error in map_power_bi_datatype: {e}")
        return "Unknown"

# ---------------------------------------------------------
# Main Conversion Function
# ---------------------------------------------------------
# gemini/app/services/calculated_fields_service.py

def _process_calculated_field(field, final_type, final_table_name, current_schema, tables, skip_llm, known_measures, project_id, workbook_id, run_id, auth_header):
    datatype = field.get("datatype", "")
    pbi_datatype = map_power_bi_datatype(datatype)

    # REMOVE ORIGINAL KEY
    field = field.copy()
    field.pop("datatype", None)

    name = field.get("name", "Unknown")
    try:
        from app.services.cosmos_log_service import store_log
        store_log(
            project_id=project_id,
            project_name="Unknown",
            workbook_id=workbook_id,
            run_id=run_id,
            function_name="_process_calculated_field",
            log_level="INFO",
            message=f"Converting calculated field '{name}' to DAX (Target Table: {final_table_name})",
            auth_header=auth_header
        )
    except Exception as e:
        print(f"Failed to log calculated field processing: {e}")

    if skip_llm:
        return {
            **field,
            "type": final_type,
            "table_name": final_table_name,
            "dax_formula": "-- SKIPPED BY USER",
            "confidence_score": 100,
            "review_notes": "Skipped LLM conversion"
        }

    sys_prompt, usr_prompt = build_prompts(
        calc_type=f"{final_type}s",
        item_name=field["name"],
        tableau_formula=field['tableau_formula'],
        table_name=final_table_name,
        schema_context=current_schema
    )

    try:
        dax = call_llm(system_prompts=sys_prompt, user_prompt=usr_prompt)

        # ✅ Validate: fix any column that doesn't exist as bi_column_name in its table
        dax = validate_and_fix_dax_columns(dax, tables, known_measures)

        confidence = evaluate_dax_confidence(
            tableau_formula=field['tableau_formula'],
            dax_formula=dax.strip(),
            schema_context=current_schema,
            calc_type=final_type,
            calc_name=field.get('name', '')
        )
        # ✅ FINAL RESOLUTION: Extract from DAX first to ensure alignment, fallback to resolved table
        dax_table = extract_table_from_dax(dax, tables)
        if dax_table and dax_table != "UNKNOWN_TABLE":
            final_table_name = dax_table
        elif not final_table_name or final_table_name == "UNKNOWN_TABLE":
            final_table_name = dax_table

        try:
            from app.services.cosmos_log_service import store_log
            store_log(
                project_id=project_id,
                project_name="Unknown",
                workbook_id=workbook_id,
                run_id=run_id,
                function_name="_process_calculated_field",
                log_level="INFO",
                message=f"Successfully converted calculated field '{name}' to DAX",
                auth_header=auth_header
            )
        except Exception as e:
            print(f"Failed to log calculated field success: {e}")

        return {
            **field,
            "type": final_type,
            "table_name": final_table_name,
            "tableau_datatype": datatype,
            "power_bi_datatype": pbi_datatype,
            "dax_formula": dax.strip(),
            "confidence_score": confidence.get("confidence_score", -1),
            "review_notes": confidence.get("review_notes", "")
        }
    except Exception as item_err:
        return {
            **field,
            "type": final_type,
            "table_name": final_table_name,
            "tableau_datatype": datatype,
            "power_bi_datatype": pbi_datatype,
            "dax_formula": f"-- ERROR: {str(item_err)}",
            "confidence_score": 0,
            "review_notes": f"Conversion failed: {str(item_err)}"
        }

def convert_calculated_fields_to_powerbi(
    calculated_fields: list,
    tables: list,
    parameters: list,
    sets: list,
    converted_lods: list,
    relationships: list,
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str,
    skip_llm: bool = False
) -> tuple[list, bool]:

    converted = []

    # 📝 LOG ACTIVITY: Agent starts conversion
    _fire_log_safely(send_activity_to_api(
        project_id=project_id,
        workbook_id=workbook_id,
        run_id=run_id,
        agent_name="CalculatedFieldsAgent",
        technical_message=f"Starting dynamic conversion for {len(calculated_fields)} fields.",
        auth_header=auth_header
    ))

    # Build parameter lookup for dynamic type switching
    parameter_names = {p.get("name") for p in parameters if p.get("name")}

    # Build parameter_name -> Power BI table name lookup
    # DATATABLE params: table name = param name itself
    # DISTINCT params:  table name = {name}_Table
    parameter_table_lookup = {}
    for p in parameters:
        p_name = p.get("name", "")
        pbi_dax = (p.get("powerbi", {}).get("dax") or "")
        if not p_name:
            continue
        if "DATATABLE" in pbi_dax:
            parameter_table_lookup[p_name] = p_name
        elif "DISTINCT(" in pbi_dax:
            parameter_table_lookup[p_name] = f"{p_name}_Table"

    # Build Schema Context for LLM
    schema_lines = []
    for t in tables:
        t_name = t.get("tableau_table_name", "Unknown")
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

    if parameters:
        schema_lines.extend(build_parameter_schema_lines(parameters))

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
                    # Restore dynamic set column resolution
                    cols = powerbi_data.get("calculated_columns", [])
                    if cols:
                        for c in cols:
                            cname = c.get("name", "")
                            if "In_Out" in cname or "Category" in cname:
                                calc_col = cname
                        if calc_col == f"{set_name}_In_Out":
                            calc_col = cols[-1].get("name", "")

                    # For dynamic sets, we just instruct the LLM to deduce the base column
                    base_col = "<BaseColumnOfSet>"
                    set_dax = "Dynamic set evaluated via multiple calculated columns."

                schema_lines.append(f"- Set [{set_name}] on table '{target_table}'. Definition: {set_dax}")
                schema_lines.append(f"  CRITICAL RULE FOR SETS: If comparing a DIFFERENT table's column to this set, you MUST dynamically resolve the set values using CALCULATETABLE. Use this exact pattern: 'OtherTable'[Col] IN CALCULATETABLE(VALUES('{target_table}'[{base_col}]), REMOVEFILTERS('{target_table}'), '{target_table}'[{calc_col}] = \"In\")")
                schema_lines.append(f"  If referencing the same table, you can use '{target_table}'[{calc_col}] = \"In\".")


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

    schema_context = "\n".join(schema_lines)

    # Compile a list of known measures so the column fixer can strip hallucinated table prefixes
    all_measure_names = []
    if converted_lods:
        all_measure_names.extend([lod.get("name") for lod in converted_lods if lod.get("name")])
    for field in calculated_fields:
        if field.get("type", "dimension").lower() != "dimension":
            all_measure_names.append(field.get("name"))

    # ✅ Track calculated fields per table so subsequent CFs see prior ones in schema
    cf_virtual_columns = {}  # { table_name: [cf_name, ...] }
    tasks = []

    try:
        for field in calculated_fields:
            role = (field.get("role") or "").lower()
            calc_type = (field.get("type") or "").lower()

            is_m = False
            if role == "measure":
                is_m = True
            elif role == "dimension":
                is_m = False
            else:
                is_m = calc_type in ["measure", "table_calculation", "lod", "nested_lod"]

            if is_m:
                continue

            # DYNAMIC TYPE SWITCH: Parameters in dependencies require a Measure
            field_dependencies = field.get("dependencies") or []
            has_parameter_dependency = any(dep in parameter_names for dep in field_dependencies)

            # USERNAME(), USERPRINCIPALNAME(), CUSTOMDATA(), USERCULTURE() are NOT allowed
            # in Power BI calculated columns — they can ONLY be used in Measures or RLS.
            formula_upper = (field.get("tableau_formula") or "").upper()
            has_user_function = any(fn in formula_upper for fn in [
                "USERNAME()", "USERPRINCIPALNAME()", "CUSTOMDATA()", "USERCULTURE()"
            ])

            final_type = "measure" if (has_parameter_dependency or has_user_function) else "dimension"

            # DYNAMIC TABLE RESOLUTION: No static fallbacks
            # Since we iterate sequentially here, 'calculated_fields' has updated 'table_name' for prior fields.
            final_table_name = resolve_table_for_field(field, calculated_fields, tables, converted_lods)

            # Fallback: if table_name is still empty, check if any dependency is a parameter
            if not final_table_name:
                for dep in field.get("dependencies", []):
                    param_table = parameter_table_lookup.get(dep)
                    if param_table:
                        final_table_name = param_table
                        break

            # Save it onto the field dict so subsequent loop iterations find it when resolving dependencies.
            field["table_name"] = final_table_name

            # ✅ Build per-call schema context with virtual columns from prior CFs
            current_schema = schema_context
            if cf_virtual_columns:
                extra_lines = []
                for tbl_name, cf_names in cf_virtual_columns.items():
                    extra_lines.append(
                        f"- Table '{tbl_name}' (Calculated Columns): [{', '.join(cf_names)}]"
                    )
                current_schema = schema_context + "\n" + "\n".join(extra_lines)

            # ✅ NEW: DYNAMIC RELATIONSHIP-AWARE HINTS
            # Detect cross-table references and suggest MAXX(RELATEDTABLE) or RELATED()
            formula = field.get("tableau_formula", "")
            referenced_cols = re.findall(r"\[([^\]]+)\]", formula)
            for ref_col in referenced_cols:
                source_table = ""
                # Simple lookup to find the table of the referenced column
                for t in tables:
                    for c in t.get("columns", []):
                        if c.get("tableau_column_name") == ref_col:
                            source_table = t.get("tableau_table_name") or t.get("bi_table_name")
                            break
                    if source_table: break

                if source_table and source_table != final_table_name:
                    # Look up relationship between final_table_name and source_table
                    for r in (relationships or []):
                        pbi_rel = r.get("power_bi", {})
                        from_t = pbi_rel.get("fromTable")
                        to_t = pbi_rel.get("toTable")
                        card = pbi_rel.get("cardinality", "")

                        # CASE 1: Base Table is 'One', Source Table is 'Many'
                        if (from_t == final_table_name and to_t == source_table and "OneTo" in card) or \
                           (from_t == source_table and to_t == final_table_name and "ManyTo" in card):
                            current_schema += f"\nCRITICAL CONVERSION HINT: Field '{field['name']}' is on table '{final_table_name}' (One side) but references column '{ref_col}' from table '{source_table}' (Many side). You MUST wrap this reference in MAXX(RELATEDTABLE('{source_table}'), '{source_table}'[{ref_col}]) according to Rule D10."
                            break

                        # CASE 2: Base Table is 'Many', Source Table is 'One'
                        if (from_t == final_table_name and to_t == source_table and "ManyTo" in card) or \
                           (from_t == source_table and to_t == final_table_name and "OneTo" in card):
                            current_schema += f"\nCRITICAL CONVERSION HINT: Field '{field['name']}' is on table '{final_table_name}' (Many side) and references column '{ref_col}' from table '{source_table}' (One side). You MUST use RELATED('{source_table}'[{ref_col}]) according to Rule D12."
                            break

            tasks.append((field, final_type, final_table_name, current_schema, tables))

            # ✅ After processing, register this CF as a virtual column for subsequent fields
            if final_table_name and final_type == "dimension":
                cf_virtual_columns.setdefault(final_table_name, []).append(field["name"])

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(_process_calculated_field, t[0], t[1], t[2], t[3], t[4], skip_llm, all_measure_names, project_id, workbook_id, run_id, auth_header)
                for t in tasks
            ]
            for future in futures:
                converted.append(future.result())

        return converted, False

    except Exception as e:
        store_error(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            error_msg="Critical failure in convert_calculated_fields_to_powerbi.",
            technical_details=str(e),
            auth_header=auth_header
        )
        return calculated_fields, True
