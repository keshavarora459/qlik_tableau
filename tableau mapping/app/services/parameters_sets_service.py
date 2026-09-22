import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.services.confidence_service import evaluate_dax_confidence
from app.services.cosmos_log_service import store_activity, store_error

from .llm_service import call_llm_with_rule

logger = logging.getLogger(__name__)

# --------------------------------------------------
# Helper: Normalize Strings for Matching
# --------------------------------------------------
def normalize_string(text: str) -> str:
    """Removes brackets, spaces, underscores, and case for fuzzy matching."""
    if not text:
        return ""
    return re.sub(r'[^a-zA-Z0-9]', '', text).lower()

# --------------------------------------------------
# Helper: Detect Field Association for Free Params
# --------------------------------------------------
def find_associated_column(param_name: str, calculations: dict, all_sources: list = None) -> str | None:
    """Dynamically scans calculated fields to find any related mapped column for a given parameter."""
    calc_fields = calculations.get("calculated_fields", {})
    all_sources = all_sources or []

    # 1. Build dictionary of known schema columns
    known_columns = {}
    for source in all_sources:
        for col in source.get("columns", []):
            if isinstance(col, dict):
                col_name = col.get("tableau_column_name") or col.get("bi_column_name") or col.get("name", "")
                if col_name:
                    if col_name.startswith("[") and col_name.endswith("]"):
                        col_name = col_name[1:-1]
                    known_columns[normalize_string(col_name)] = col_name

    param_esc = re.escape(param_name)
    operators = r'(?:==|=|!=|<>|>|<|>=|<=)'

    # High confidence equality and string matching patterns
    combined_pattern = (
        rf'\[([^\]]+)\]\s*{operators}\s*\[{param_esc}\]|'
        rf'\[{param_esc}\]\s*{operators}\s*\[([^\]]+)\]|'
        rf'(?:CONTAINS|STARTSWITH|ENDSWITH)\s*\(\s*\[([^\]]+)\]\s*,\s*\[{param_esc}\]\s*\)|'
        rf'(?:CONTAINS|STARTSWITH|ENDSWITH)\s*\(\s*\[{param_esc}\]\s*,\s*\[([^\]]+)\]\s*\)'
    )

    if isinstance(calc_fields, dict):
        # Sort calculations to prioritize "dimension" types
        fields_list = list(calc_fields.values())
        fields_list.sort(key=lambda x: 0 if isinstance(x, dict) and x.get("type", "").lower() == "dimension" else 1)

        for meta in fields_list:
            if not isinstance(meta, dict): continue

            formula = meta.get("formula", "")
            tableau_formula = meta.get("tableau_formula", "")
            dax_formula = meta.get("dax_formula", "")
            full_search_text = f"{formula} \n {tableau_formula} \n {dax_formula}"

            # Phase 1: High confidence pattern matching in formula
            match = re.search(combined_pattern, full_search_text, re.IGNORECASE)
            if match:
                for i in range(1, 5):
                    candidate = match.group(i)
                    if candidate and normalize_string(candidate) != normalize_string(param_name):
                        if not all_sources or normalize_string(candidate) in known_columns:
                            return candidate

            # Phase 2: Dynamic search if parameter is mentioned in this definition
            # We enforce lowercasing and normalization for bulletproof matching
            if param_name.lower() in full_search_text.lower() or normalize_string(param_name) in normalize_string(full_search_text):

                # A: Extract bracketed columns (The Tableau Standard)
                bracketed_items = re.findall(r'\[([^\]]+)\]', full_search_text)
                for item in bracketed_items:
                    norm = normalize_string(item)
                    if norm in known_columns and norm != normalize_string(param_name):
                        return known_columns[norm]

                # B: Check exact literal text occurrences if brackets weren't used
                text_lower = full_search_text.lower()
                for norm_col, raw_col in known_columns.items():
                    if len(raw_col.strip()) > 2 and raw_col.lower() in text_lower and norm_col != normalize_string(param_name):
                        return raw_col

                # C: Check fully normalized strings (Bulletproof for spaces/syntax weirdness)
                norm_text = normalize_string(full_search_text)
                for norm_col, raw_col in known_columns.items():
                    if len(norm_col) > 4 and norm_col in norm_text and norm_col != normalize_string(param_name):
                        return raw_col

    return None

def build_datatable_dax(param_name: str, values: list[Any], data_type: str) -> str:
    if not values:
        return None
    if data_type == "integer":
        column_type = "INTEGER"
        formatted_rows = ", ".join([f"{{{val}}}" for val in values])
    else:
        column_type = "STRING"
        formatted_rows = ", ".join([f'{{"{val}"}}' for val in values])
    return f'{param_name} = DATATABLE("Value", {column_type}, {{{formatted_rows}}})'

# --------------------------------------------------
# Parameters Logic
# --------------------------------------------------
def extract_and_convert_parameters(
    payload: dict, project_id: str, workbook_id: str, run_id: str, auth_header: str,
    tables: list[dict] = None, custom_sql: list[dict] = None, skip_llm: bool = False
) -> list[dict]:
    if custom_sql is None:
        custom_sql = []
    if tables is None:
        tables = []
    param_sets = payload.get("parameters_and_sets") or payload.get("parameters") or {}
    calculations = payload.get("calculations_and_lods") or payload.get("calculations") or payload.get("calculated_fields") or {}
    
    if isinstance(param_sets, dict) and "parameters" in param_sets:
        raw_params = param_sets.get("parameters", {})
    elif isinstance(param_sets, dict):
        raw_params = {k: v for k, v in param_sets.items() if k != "sets" and isinstance(v, dict)}
    else:
        raw_params = {}

    if not isinstance(raw_params, dict):
        return []

    store_activity(project_id, workbook_id, run_id, f"Converting {len(raw_params)} Parameters.", auth_header)
    all_sources = tables + custom_sql

    def _process_param(name, meta):
        try:
            allowable_config = meta.get("allowable", {})
            allowable_type = allowable_config.get("type", "all")
            data_type = meta.get("datatype", "string").lower()
            pbi_kind, dax_out, filter_logic = "Manual Parameter", None, None

            if allowable_type == "list":
                allowable_values = [str(v.get("value")) for v in allowable_config.get("values", [])]
                pbi_kind = "Slicer Table (DATATABLE)"
                dax_out = build_datatable_dax(name, allowable_values, data_type)
            elif allowable_type == "all":
                pbi_kind = "Dynamic Slicer (DISTINCT)"
                raw_col_match = find_associated_column(name, calculations, all_sources)
                if raw_col_match:
                    norm_col_match = normalize_string(raw_col_match)
                    target_table, actual_column_name = "Table", raw_col_match
                    for source in all_sources:
                        found = False
                        for col in source.get("columns", []):
                            if not isinstance(col, dict): continue

                            # Check all possible identities of the column in Tableau
                            tab_name = normalize_string(col.get("tableau_column_name") or "")
                            renamed_name = normalize_string(col.get("tableau_renamed_column_name") or "")
                            bi_name = normalize_string(col.get("bi_column_name") or "")

                            if norm_col_match in [tab_name, renamed_name, bi_name] and norm_col_match != "":
                                target_table = source.get("bi_table_name") or source.get("tableau_table_name") or "Table"
                                # Return the bi_column_name if available, else original Tableau name
                                actual_column_name = col.get("bi_column_name") or col.get("tableau_renamed_column_name") or col.get("tableau_column_name")
                                # Strip outer brackets from mapping if they exist to avoid double-bracketed DAX
                                if actual_column_name.startswith("[") and actual_column_name.endswith("]"):
                                    actual_column_name = actual_column_name[1:-1]
                                found = True
                                break
                        if found: break

                    # Space stripping removed (actual_column_name.replace(" ", "")) as Power BI supports spaces
                    dax_out = f"{name}_Table = DISTINCT('{target_table}'[{actual_column_name}])"
                    filter_logic = (f"{name}_Filter = IF(ISBLANK(SELECTEDVALUE('{name}_Table'[{actual_column_name}])) "
                                   f"|| '{target_table}'[{actual_column_name}] = SELECTEDVALUE('{name}_Table'[{actual_column_name}]), 1, 0)")
                else:
                    pbi_kind = "Manual Input Parameter"
                    # Provide a generic fallback instead of null to prevent confidence evaluation failure
                    if data_type in ["integer", "float", "real", "double", "numeric"]:
                        dax_out = build_datatable_dax(name, [0], "integer")
                    elif data_type == "boolean":
                        dax_out = build_datatable_dax(name, ["TRUE", "FALSE"], "string")
                    elif data_type == "datetime" or data_type == "date":
                        # Simplistic fallback for date types, handled as string placeholder by default
                        dax_out = build_datatable_dax(name, ["2023-01-01"], "string")
                    else:
                        dax_out = build_datatable_dax(name, ["Enter value..."], "string")
                    filter_logic = "// Fallback: No direct associated column found. Tie this parameter manually to the appropriate visual or logic."

            elif allowable_type == "range":
                min_val = allowable_config.get("min")
                max_val = allowable_config.get("max")
                step = allowable_config.get("step") or 1

                # Convert to correct type
                try:
                    min_val = int(min_val)
                    max_val = int(max_val)
                    step = int(step)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Failed to parse range values for {name}: {e}. Using defaults.")
                    min_val, max_val, step = 1, 100, 1  # fallback safety

                pbi_kind = "Numeric Parameter (GENERATESERIES)"
                dax_out = f"{name} = GENERATESERIES({min_val}, {max_val}, {step})"

            confidence = {"confidence_score": 100} if skip_llm else evaluate_dax_confidence(
                tableau_formula=f"Parameter '{name}'", dax_formula=dax_out or "", schema_context="", calc_type="parameter"
            )

            return {
                "name": name,
                "tableau": {
                    "data_type": data_type,
                    "current_value": meta.get("current_value"),
                    "parameter_kind": (
                        "List" if allowable_type == "list"
                        else "Range" if allowable_type == "range"
                        else "All / Free"
                    )
                },
                "powerbi": {
                    "data_type": data_type,
                    "current_value": meta.get("current_value"),
                    "parameter_kind": pbi_kind,
                    "dax": dax_out,
                    "filter_logic": filter_logic,
                    "confidence_score": confidence.get("confidence_score", -1),
                    "review_notes": confidence.get("review_notes", "")
                }
            }
        except Exception as e:
            store_error(project_id, workbook_id, run_id, f"Parameter '{name}' failed.", str(e), auth_header)
            return None

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda p: _process_param(*p), raw_params.items()))
    return [r for r in results if r]

# --------------------------------------------------
# Sets Logic
# --------------------------------------------------
def extract_and_convert_sets(payload: dict, tables: list, custom_sql: list = None, skip_llm: bool = False) -> list:
    try:
        param_sets = payload.get("parameters_and_sets") or payload.get("parameters") or {}
        raw_sets = {}
        if isinstance(param_sets, dict) and "sets" in param_sets:
            raw_sets = param_sets.get("sets", {})
        elif "sets" in payload:
            s_val = payload.get("sets", {})
            raw_sets = s_val.get("sets", {}) if isinstance(s_val, dict) and "sets" in s_val else (s_val if isinstance(s_val, dict) else {})

        if not raw_sets or not isinstance(raw_sets, dict): return []
        normalized_sets = [{"name": n, "tableau": d} for n, d in raw_sets.items()]
        return ParametersSetsService().process_all_sets(normalized_sets, tables, custom_sql, skip_llm=skip_llm)
    except Exception as e:
        logger.error(f"Set extraction failed: {e}")
        return []

class ParametersSetsService:
    def _get_actual_metadata(self, field_name_raw: str, tables: list, custom_sql: list = None) -> dict[str, str]:
        if not field_name_raw: return {"table": "Table", "field": "Field"}
        clean_field = field_name_raw.split('.')[-1]
        match = re.search(r':([^:]+):[a-z0-9]+\]?$', clean_field, re.IGNORECASE)
        if match: clean_field = match.group(1)
        clean_field = clean_field.replace('[', '').replace(']', '')

        # ✅ Extract parenthesized table hint: "DESCRIPTION (procedures)" → field="DESCRIPTION", hint="procedures"
        table_hint = ""
        paren_match = re.match(r'^(.+?)\s*\(([^)]+)\)\s*$', clean_field)
        if paren_match:
            clean_field_no_hint = paren_match.group(1).strip()
            table_hint = normalize_string(paren_match.group(2).strip())
        else:
            clean_field_no_hint = clean_field

        all_tables = (tables or []) + (custom_sql or [])

        # PASS 1: If table hint exists, match field + table together
        if table_hint:
            search_norm = normalize_string(clean_field_no_hint)
            for table in all_tables:
                t_name = table.get("bi_table_name") or table.get("tableau_table_name") or table.get("table_name", "Table")
                if normalize_string(t_name) == table_hint:
                    for col in table.get("columns", []):
                        if not isinstance(col, dict): continue
                        tab_name = normalize_string(col.get("tableau_column_name") or "")
                        renamed_name = normalize_string(col.get("tableau_renamed_column_name") or "")
                        bi_name = normalize_string(col.get("bi_column_name") or "")

                        if search_norm in [tab_name, renamed_name, bi_name] and search_norm != "":
                            final_field = col.get("bi_column_name") or col.get("tableau_renamed_column_name") or col.get("tableau_column_name")
                            if final_field.startswith("[") and final_field.endswith("]"):
                                final_field = final_field[1:-1]
                            return {"table": t_name, "field": final_field}
                    # Table matched but column not found — still use this table with the clean field name
                    return {"table": t_name, "field": clean_field_no_hint}

        # PASS 2: Standard match (no hint or hint didn't match)
        search_norm = normalize_string(clean_field_no_hint)
        for table in all_tables:
            t_name = table.get("bi_table_name") or table.get("tableau_table_name") or table.get("table_name", "Table")
            for col in table.get("columns", []):
                if not isinstance(col, dict): continue
                tab_name = normalize_string(col.get("tableau_column_name") or "")
                renamed_name = normalize_string(col.get("tableau_renamed_column_name") or "")
                bi_name = normalize_string(col.get("bi_column_name") or "")

                if search_norm in [tab_name, renamed_name, bi_name] and search_norm != "":
                    final_field = col.get("bi_column_name") or col.get("tableau_renamed_column_name") or col.get("tableau_column_name")
                    if final_field.startswith("[") and final_field.endswith("]"):
                        final_field = final_field[1:-1]
                    return {"table": t_name, "field": final_field}

        # PASS 3: Try original full name (with parentheses) as fallback
        search_norm_full = normalize_string(clean_field)
        for table in all_tables:
            t_name = table.get("bi_table_name") or table.get("tableau_table_name") or table.get("table_name", "Table")
            for col in table.get("columns", []):
                if not isinstance(col, dict): continue
                tab_name = normalize_string(col.get("tableau_column_name") or "")
                renamed_name = normalize_string(col.get("tableau_renamed_column_name") or "")
                bi_name = normalize_string(col.get("bi_column_name") or "")

                if search_norm_full in [tab_name, renamed_name, bi_name] and search_norm_full != "":
                    final_field = col.get("bi_column_name") or col.get("tableau_renamed_column_name") or col.get("tableau_column_name")
                    if final_field.startswith("[") and final_field.endswith("]"):
                        final_field = final_field[1:-1]
                    return {"table": t_name, "field": final_field}

        return {"table": "Table", "field": clean_field_no_hint}

    def _resolve_expression_tables(self, expression: str, tables: list, custom_sql: list) -> str:
        field_pattern = r'(?:\[([^\]]+)\]\.)?(\[([^\]]+)\])'
        def replace_field(m):
            if m.group(1) == "Parameters": return m.group(0)
            meta = self._get_actual_metadata(m.group(2), tables, custom_sql)
            return f"'{meta['table']}'[{meta['field']}]"
        return re.sub(field_pattern, replace_field, expression or "")

    def convert_static_set(self, set_name: str, tableau_data: dict, tables: list, custom_sql: list) -> str:
        meta = self._get_actual_metadata(tableau_data.get("base_field", ""), tables, custom_sql)
        members = ", ".join([f'"{m}"' for m in tableau_data.get("selected_members", [])])
        return call_llm_with_rule(
            rule_type="set_static_column", name=re.sub(r'\W', '_', set_name),
            table=meta["table"], base_field=meta["field"], members_list=members
        )

    def convert_dynamic_set(self, set_name: str, tableau_data: dict, tables: list, custom_sql: list) -> dict:
        meta_base = self._get_actual_metadata(tableau_data.get("base_field", ""), tables, custom_sql)
        resolved_expr = self._resolve_expression_tables(tableau_data.get("expression", ""), tables, custom_sql)

        schema_context = "\n".join([
            f"- Table '{t.get('bi_table_name', 'Table')}': [{', '.join([c.get('bi_column_name', '') for c in t.get('columns', [])])}]"
            for t in (tables or []) + (custom_sql or [])
        ])

        # Call the new multi-column rule
        raw_response = call_llm_with_rule(
            rule_type="set_dynamic_calculated_columns",
            set_name=re.sub(r'\W', '_', set_name),
            mode=(tableau_data.get("mode") or "top").lower(),
            order="DESC" if (tableau_data.get("mode") or "top").lower() == "top" else "ASC",
            count=tableau_data.get("count") or "10",
            expression=resolved_expr,
            table=meta_base["table"],
            base_field=meta_base["field"],
            schema_context=schema_context
        )

        try:
            # Extract JSON from LLM response
            json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
            column_data = json.loads(json_match.group(0)) if json_match else {}

            return {
                "name": set_name,
                "target_table": meta_base["table"],
                "calculated_columns": column_data.get("calculated_columns", [])
            }
        except Exception as e:
            return {"error": str(e), "raw": raw_response}

    def process_all_sets(self, sets_data: list, tables: list, custom_sql: list = None, skip_llm: bool = False) -> list:
        def _process_set(s):
            name, data = s.get("name", "Set"), s.get("tableau", {})
            stype = data.get("type")
            try:
                if stype == "static":
                    meta = self._get_actual_metadata(data.get("base_field", ""), tables, custom_sql)
                    dax = "-- SKIPPED" if skip_llm else self.convert_static_set(name, data, tables, custom_sql)
                    conf = evaluate_dax_confidence(f"Static set '{name}'", dax, "", "set_static") if not skip_llm else {"confidence_score": 100}
                    return {"name": name, "type": "static", "powerbi": {
                        "target_table": meta["table"], "dax": dax, "data_type": "text", "confidence_score": conf.get("confidence_score", -1), "review_notes": conf.get("review_notes", "")
                    }}
                elif stype == "dynamic":
                    if skip_llm: return {"name": name, "type": "dynamic", "powerbi": {"dax": "-- SKIPPED"}}

                    pbi_result = self.convert_dynamic_set(name, data, tables, custom_sql)

                    if "error" not in pbi_result:
                        cols = pbi_result.get("calculated_columns", [])
                        # Evaluate confidence based on the primary logic (the In/Out column)
                        if cols:
                            primary_dax = cols[-1].get("dax", "")
                            conf = evaluate_dax_confidence(f"Dynamic set '{name}'", primary_dax, "", "set_dynamic")
                            pbi_result["confidence_score"] = conf.get("confidence_score", -1)
                            pbi_result["review_notes"] = conf.get("review_notes", "")

                    return {"name": name, "type": "dynamic", "powerbi": pbi_result}
            except Exception as e:
                return {"name": name, "type": "error", "powerbi": {"error": str(e)}}

        with ThreadPoolExecutor(max_workers=5) as executor:
            return [r for r in executor.map(_process_set, sets_data) if r]
