import re

from app.services.cosmos_log_service import store_error


# ==========================================================
# UTILITY FUNCTIONS
# ==========================================================
def normalize(value):
    return (value or "").strip().lower()

def is_data_visual(name: str) -> bool:
    """Exclude non-data visuals like text headers or images."""
    name_norm = normalize(name)
    if not name_norm:
        return False
    if "text" in name_norm or "image" in name_norm:
        return False
    return True

def resolve_field_pbi_identity(field_name: str, tables: list) -> tuple:
    """
    Robustly resolves a Tableau field name to its (BI Table, BI Column) identity.
    Returns (None, None) if no match is found.
    """
    if not tables or not field_name:
        return None, None

    # 1. Clean up Tableau metadata strings (handles [datasource].[FieldName] or [none:FieldName:nk])
    clean_field = field_name
    match = re.search(r':([^:]+):nk\]?$', field_name)
    if match:
        clean_field = match.group(1)

    # Handle qualified names like [Table].[Field]
    if "." in clean_field:
        clean_field = clean_field.split(".")[-1]

    clean_field = clean_field.strip('[]')

    # 2. Normalize for fuzzy matching
    norm_field = re.sub(r'[^a-zA-Z0-9]', '', clean_field).lower()

    for table in tables:
        for col in table.get("columns", []):
            if not isinstance(col, dict): continue

            tab_name = col.get("tableau_column_name") or ""
            bi_name = col.get("bi_column_name") or ""
            renamed_name = col.get("tableau_renamed_column_name") or ""

            # Check all possible Tableau name sources
            potential_names = [tab_name, bi_name, renamed_name, col.get("name", "")]
            for p_name in potential_names:
                if not p_name: continue
                if re.sub(r'[^a-zA-Z0-9]', '', p_name).lower() == norm_field:
                    resolved_table = table.get("bi_table_name") or table.get("tableau_table_name") or table.get("table_name", "UnknownTable")
                    # Prioritize bi_column_name (which usually includes spaces/renames)
                    resolved_column = bi_name or renamed_name or tab_name
                    return resolved_table, resolved_column

    return "UnknownTable", clean_field

def resolve_parameter_pbi_identity(param_name: str, parameters: list) -> str:
    """Resolves a Tableau parameter name to its Power BI name from the parameters map."""
    if not parameters or not param_name:
        return param_name.strip('[]')

    clean_param = param_name.strip('[]')
    # Handle [Parameters].[ParamName]
    if "." in clean_param:
        clean_param = clean_param.split(".")[-1].strip('[]')

    norm_param = re.sub(r'[^a-zA-Z0-9]', '', clean_param).lower()

    for p in parameters:
        p_name = p.get("name", "")
        if re.sub(r'[^a-zA-Z0-9]', '', p_name).lower() == norm_param:
            return p_name # Use the name as defined in mapping

    return clean_param

def extract_drillthrough_fields(source_worksheet: str, sheets_visuals: list[dict]) -> list[str]:
    """Robustly extracts non-aggregated fields from a visual to use as Drillthrough filters."""
    source_worksheet_norm = normalize(source_worksheet)

    source_visual_def = None
    for v in sheets_visuals:
        v_name = v.get("sheet") or v.get("name") or v.get("worksheet_name")
        if normalize(v_name) == source_worksheet_norm:
            source_visual_def = v
            break

    drill_fields = []
    if source_visual_def:
        rows = source_visual_def.get("rows", [])
        columns = source_visual_def.get("columns", [])

        all_fields = []
        if isinstance(rows, list):
            all_fields.extend(rows)
        if isinstance(columns, list):
            all_fields.extend(columns)

        for field in all_fields:
            field_str = field.get("name", "") if isinstance(field, dict) else str(field)
            if field_str and "(" not in field_str:
                drill_fields.append(field_str)

    return drill_fields

# ==========================================================
# DASHBOARD VISUALS MAPPING
# ==========================================================
def build_visuals_from_dashboards(dashboards: list[dict]) -> list[dict]:
    visuals = []
    for dashboard in dashboards:
        dashboard_name = dashboard.get("dashboard_name") or dashboard.get("name")
        if not dashboard_name:
            continue

        objects = dashboard.get("dashboard_objects", [])
        for sheet_name in objects:
            if not sheet_name:
                continue
            visuals.append({
                "dashboard": dashboard_name,
                "sheet": sheet_name
            })
    return visuals

def build_visual_interactions(action: dict, visuals: list[dict]) -> list[dict]:
    interactions = []
    source_dashboard = action.get("source_dashboard")
    source_sheet = action.get("source_worksheet")

    source_dashboard_norm = normalize(source_dashboard)
    source_sheet_norm = normalize(source_sheet)

    page_visuals = [
        v for v in visuals
        if normalize(v.get("dashboard")) == source_dashboard_norm
    ]

    if not page_visuals:
        return []

    if source_sheet_norm and source_sheet_norm != "all sheets":
        if not is_data_visual(source_sheet):
            return []

        for tgt in page_visuals:
            tgt_name = tgt.get("sheet")
            if normalize(tgt_name) == source_sheet_norm:
                continue
            if not is_data_visual(tgt_name):
                continue

            interactions.append({
                "source_visual": source_sheet,
                "target_visual": tgt_name,
                "type": "DataFilter"
            })
    else:
        data_visuals = [v for v in page_visuals if is_data_visual(v.get("sheet"))]

        for src in data_visuals:
            for tgt in data_visuals:
                if normalize(src.get("sheet")) == normalize(tgt.get("sheet")):
                    continue
                interactions.append({
                    "source_visual": src.get("sheet"),
                    "target_visual": tgt.get("sheet"),
                    "type": "DataFilter"
                })

    return interactions

# ==========================================================
# MAIN CONVERSION ORCHESTRATOR
# ==========================================================
def convert_actions_to_powerbi(
    actions: list,
    tables: list,
    project_id: str,
    workbook_id: str,
    run_id: str,
    auth_header: str,
    dashboards: list = None,
    sheets_visuals: list = None,
    parameters: list = None
) -> list:
    dashboards = dashboards or []
    sheets_visuals = sheets_visuals or []

    try:
        visuals_map = build_visuals_from_dashboards(dashboards)
    except Exception as e:
        store_error(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            error_msg="Failed to build visuals map from dashboards.",
            technical_details=str(e),
            auth_header=auth_header
        )
        visuals_map = []

    power_bi_actions = []

    try:
        for action in actions:
            try:
                action_type = action.get("type", "").lower()
                action_name = action.get("name", "Unknown Action")
                source_viz = action.get("source_worksheet", "Unknown Visual")

                structured_action = {
                    "action_id": action_name,
                    "tableau_action": action.copy(),
                    "powerbi_equivalent": {}
                }

                # ---------------------------------------------------------
                # 1. URL Actions
                # ---------------------------------------------------------
                if action_type == "url":

                    safe_name = action_name.replace(" ", "_")
                    url_expression = action.get("url_expression", "")
                    source_dash = action.get("source_dashboard", "N/A")

                    fields = re.findall(r'<\[(.*?)\]>', url_expression)
                    # Start building the base URL string for the DAX variable
                    dax_url_base = f'"{url_expression}"'

                    for field in fields:
                        tableau_placeholder = f"<[{field}]>"
                        res_table, res_column = resolve_field_pbi_identity(field, tables)

                        # Format properly with table name and field for the internal URL variable
                        dax_dynamic_insert = f'" & SELECTEDVALUE(\'{res_table}\'[{res_column}]) & "'
                        dax_url_base = dax_url_base.replace(tableau_placeholder, dax_dynamic_insert)

                    # Clean up potential empty string concatenations
                    dax_url_base = dax_url_base.replace(' & ""', '').replace('"" & ', '')

                    # Construct the updated Iframe DAX structure
                    dax_code = (
                        f"{safe_name}_WebPage =\n"
                        f"VAR url1 = {dax_url_base}\n"
                        f"RETURN\n"
                        f"\"<iframe src='\" & url1 & \"' width='100%' height='600'></iframe>\""
                    )

                    structured_action["powerbi_equivalent"].update({
                        "implementation_type": "HTML Content Viewer (Iframe Embedding)",
                        "source_dashboard": source_dash,
                        "source_visual": source_viz,
                        "dax_measure_name": f"{safe_name}_WebPage",
                        "dax_expression": dax_url_base,
                        "dax_code": dax_code,
                        "implementation_notes": (
                            f"1. Create a new measure using the provided DAX code.\n"
                            f"2. Add the 'HTML Content' custom visual to your report.\n"
                            f"3. Place this measure into the visual to display the embedded page for '{source_viz}'."
                        )
                    })
        # ...


                # ---------------------------------------------------------
                # 2. Navigation Actions
                # ---------------------------------------------------------
                elif action_type == "navigation":
                    target_dash = action.get("target_dashboard")
                    target_sheet = action.get("target_sheet")
                    target_page = target_dash if target_dash and target_dash.upper() != "N/A" else target_sheet

                    source_dash = action.get("source_dashboard")
                    source_pbi_page = source_dash if source_dash and source_dash.upper() != "N/A" else source_viz

                    structured_action["powerbi_equivalent"].update({
                        "implementation_type": "Page Navigation Action (via Button Overlay)",
                        "source_page": source_pbi_page,
                        "source_visual": source_viz,
                        "target_page": target_page,
                        "implementation_notes": (
                            f"Insert a 'Blank Button' over the '{source_viz}' visual on the '{source_pbi_page}' page. "
                            f"Make the button transparent, set Action to 'Page Navigation', Destination: '{target_page}'."
                        )
                    })

                # ---------------------------------------------------------
                # 3. Filter Actions
                # ---------------------------------------------------------
                elif action_type in ["filter", "filter_action"]:
                    source_dashboard = action.get("source_dashboard")
                    source_worksheet = action.get("source_worksheet")
                    target_dashboard = action.get("target_dashboard")

                    if normalize(source_dashboard) == normalize(target_dashboard):
                        interactions = build_visual_interactions(action, visuals_map)
                        structured_action["powerbi_equivalent"].update({
                            "implementation_type": "edit_interactions",
                            "visualInteractions": interactions
                        })
                    else:
                        drill_fields = extract_drillthrough_fields(source_worksheet, sheets_visuals)
                        structured_action["powerbi_equivalent"].update({
                            "implementation_type": "drillthrough",
                            "source_page": source_dashboard,
                            "source_visual": source_worksheet,
                            "target_page": target_dashboard,
                            "drillthrough_fields": drill_fields,
                            "keep_all_filters": True
                        })

                # ---------------------------------------------------------
                # 4. Highlight Actions
                # ---------------------------------------------------------
                elif action_type in ["highlight", "highlight_action"]:
                    target_dash = action.get("target_dashboard")
                    target_sheet = action.get("target_sheet")
                    target_page = target_dash if target_dash and target_dash.upper() != "N/A" else target_sheet
                    target_visual = target_sheet if target_sheet and target_sheet.upper() != "N/A" else f"All visuals on '{target_page}'"

                    structured_action["powerbi_equivalent"].update({
                        "implementation_type": "visual_interaction_highlight",
                        "source_visual": source_viz,
                        "target_page": target_page,
                        "target_visual": target_visual,
                        "type": "HighlightFilter",
                        "implementation_notes": f"In Power BI, go to Format -> Edit Interactions. Select '{source_viz}' and set the interaction on '{target_visual}' to 'Highlight'."
                    })

                # ---------------------------------------------------------
                # 5. Parameter Actions
                # ---------------------------------------------------------
                elif action_type == "parameter":
                    param_logic = action.get("parameter_logic", {})
                    source_field_raw = param_logic.get("source-field", "")
                    target_param_raw = param_logic.get("target-parameter", "")

                    res_table, res_column = resolve_field_pbi_identity(source_field_raw, tables)
                    res_parameter = resolve_parameter_pbi_identity(target_param_raw, parameters)

                    structured_action["powerbi_equivalent"].update({
                        "implementation_type": "Parameter Action (Button/Slicer Sync)",
                        "source_visual": source_viz,
                        "target_parameter": res_parameter,
                        "source_field": res_column,
                        "target_table": res_table,
                        "dax_expression": f"SELECTEDVALUE('{res_table}'[{res_column}])",
                        "implementation_notes": f"Create a DAX measure to update the parameter '{res_parameter}' using SELECTEDVALUE('{res_table}'[{res_column}]). Bind this to the '{source_viz}' interaction."
                    })

                # ---------------------------------------------------------
                # 6. Fallback
                # ---------------------------------------------------------
                else:
                    structured_action["powerbi_equivalent"].update({
                        "implementation_type": "Native Power BI Behavior / Requires Manual Evaluation"
                    })

                power_bi_actions.append(structured_action)

            except Exception as e:
                # Per-action error handling: skip bad action, continue processing
                store_error(
                    project_id=project_id,
                    workbook_id=workbook_id,
                    run_id=run_id,
                    error_msg=f"Failed to convert action '{action.get('name', 'Unknown')}'.",
                    technical_details=str(e),
                    auth_header=auth_header
                )
                power_bi_actions.append({
                    "action_id": action.get("name", "Unknown Action"),
                    "tableau_action": action.copy() if isinstance(action, dict) else {},
                    "powerbi_equivalent": {
                        "implementation_type": "Error - Manual Review Required",
                        "error": str(e)
                    }
                })

    except Exception as e:
        # 🚨 OUTER SAFETY NET
        store_error(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            error_msg="Critical failure in convert_actions_to_powerbi.",
            technical_details=str(e),
            auth_header=auth_header
        )

    return power_bi_actions
