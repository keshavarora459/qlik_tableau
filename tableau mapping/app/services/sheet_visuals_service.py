import colorsys
import copy
import re

from app.services.cosmos_log_service import store_error


# --------------------------------------------------
# Helper: Clean Tableau Field Names
# --------------------------------------------------
def extract_clean_filter_name(raw_name: str) -> str:
    """
    Cleans Tableau bracketed field names like [Table].[Field Name]
    or metadata strings like sum:Sales:ok
    """

    if not raw_name or not isinstance(raw_name, str):
        return raw_name

    try:
        # Handle [Table].[Field]
        if "].[" in raw_name:
            bracket_part = raw_name.split("].[")[-1]
        else:
            bracket_part = raw_name

        bracket_part = bracket_part.replace("[", "").replace("]", "")

        # Handle Tableau metadata prefix
        parts = bracket_part.split(":")
        if len(parts) >= 2:
            return parts[1].strip()

        return bracket_part.strip()

    except Exception as e:
        from app.core.structured_logger import get_structured_logger
        logger = get_structured_logger(__name__)
        logger.error(f"Failed to extract clean filter name for '{raw_name}': {e}", extra={"error_type": type(e).__name__})
        return raw_name

def get_legend_formatting(formatting, visual_info, skip=False):
    """
    Returns legend formatting only if:
    - legend exists
    - legend is not empty
    - not skipped (e.g., dual axis case)
    """

    if skip:
        return None

    legends = visual_info.get("legends")

    if not legends:  # handles None, [], missing
        return None

    return next(
        (f for f in formatting if f.get("applied_to") == "Legend"),
        None
    )

def extract_measure_from_function(field_str: str) -> str:
    """
    Strips Tableau aggregation functions like SUM(), AVG(), etc.
    returning the inner field name.
    """
    if not isinstance(field_str, str):
        return field_str
    # Matches common aggregations
    pattern = r'^(?:SUM|AVG|MIN|MAX|COUNT|DISTINCT|CNTD|MEDIAN|ATTR|STDEV|VAR|STDEVP|VARP|AGG|CNT|DISTINCTCOUNT)\((.+)\)$'
    match = re.match(pattern, field_str, re.IGNORECASE)
    if match:
        return match.group(1)
    return field_str


# --------------------------------------------------
# Filter Mapping Logic
# --------------------------------------------------
def map_tableau_filter_to_power_bi(filter_wrapper: dict) -> dict:
    """
    Maps Tableau filters (Quick, Range, Date) to Power BI instructions.
    """

    # Support both JSON structures
    inner_filter = filter_wrapper.get("filter_name") or filter_wrapper

    # Extract name
    if isinstance(inner_filter, dict):

        raw_name = (
            inner_filter.get("name")
            or inner_filter.get("filter_name")
            or inner_filter.get("field")
            or "Unknown Field"
        )

        f_type = (inner_filter.get("type") or "quick").lower()

    elif isinstance(inner_filter, str):

        raw_name = inner_filter
        f_type = "quick"

    else:

        raw_name = "Unknown Field"
        f_type = "quick"

    clean_name = extract_clean_filter_name(raw_name)

    # --------------------------------------------------
    # Detect Top N / Bottom N parameters from Tableau filter
    # --------------------------------------------------
    top_bottom = inner_filter.get("top_bottom_condition")
    if isinstance(top_bottom, dict):
        rank = top_bottom.get("rank", "").title()  # e.g., "Top" or "Bottom"
        value = top_bottom.get("value")
        field = top_bottom.get("field")
        aggregation = top_bottom.get("aggregation")
        # Build filter_type string like "Top N" or "Bottom N"
        filter_type = f"{rank} N" if rank else "Top N"
        # Construct instruction with aggregation if present
        if aggregation:
            instruction = (
                f"Add '{clean_name}' in filter pane for the visual and use top n filtering and apply {aggregation} aggregation."
            )
        else:
            instruction = (
                f"Add '{clean_name}' in filter pane for the visual and use top n filtering."
            )

        return {
            "tableau_original_name": clean_name,
            "filter_type": filter_type,
            "value": str(value) if value is not None else None,
            "By_value": field,
            "power_bi_instruction": instruction,
        }


    # --------------------------------------------------
    # Date Filters
    # --------------------------------------------------
    if f_type == "date":

        instruction = (
            f"Add '{clean_name}' in filter pane for the visual."
        )

    # --------------------------------------------------
    # Range Filters
    # --------------------------------------------------
    elif f_type == "range":

        instruction = f"Add '{clean_name}' in filter pane for the visual."

    # --------------------------------------------------
    # Quick Filters
    # --------------------------------------------------
    else:

        instruction = f"Add '{clean_name}' in filter pane for the visual."

    return {
        "tableau_original_name": clean_name,
        "tableau_type": f_type,
        "power_bi_instruction": instruction,
    }


# --------------------------------------------------
# Infer Power BI Visual Type
# --------------------------------------------------
def infer_power_bi_visual_type(sheet: dict, payload: dict = None) -> dict:
    mark_type = (sheet.get("mark_type") or "").strip().lower()
    chart_type = (sheet.get("chart_type") or "").strip().lower()
    combined_type = f"{mark_type} {chart_type}"

    # Create local transformed copies of shelves for visual type inference
    # Only replace Custom SQL placeholders here; aggregation mapping (CNTD→DISTINCTCOUNT)
    # is handled exclusively by resolve_visual_field inside get_resolved_fields
    rows = replace_custom_sql_fields(sheet.get("rows", []), payload or {})
    columns = replace_custom_sql_fields(sheet.get("columns", []), payload or {})

    multiple_marks = sheet.get("multiple_marks", [])

    def get_resolved_fields(items: list) -> list:
        """
        Expands 'Measure Values' into its actual measure fields
        returning a flattened list of strings with Power BI conversion applied.
        """
        resolved = []
        for item in items:
            if not isinstance(item, dict):
                continue
            field = item.get("field")
            if field == "Measure Values":
                raw_measures = item.get("measure_values", [])
                resolved.extend([
                    resolve_visual_field(m.get("field") if isinstance(m, dict) else m)
                    for m in raw_measures
                ])
            elif field and isinstance(field, str):
                resolved.append(resolve_visual_field(field))
        return resolved

    def get_resolved_fields_with_sort(items: list, all_visual_fields: list = None) -> list:
        """
        Like get_resolved_fields but preserves sort information with Power BI compatibility check.
        - If no sort info → returns plain string (e.g. "Stock Item Name")
        - If sort field is on the visual → direct sort instruction
        - If sort field is NOT on the visual (nested) → flags as unsupported, suggests closest measure
        """
        # Collect all resolved field names on the visual for comparison
        visual_field_names = set()
        if all_visual_fields:
            for f in all_visual_fields:
                if isinstance(f, dict):
                    fname = f.get("field", "")
                elif isinstance(f, str):
                    fname = f
                else:
                    continue
                # Store both raw and resolved versions for matching
                visual_field_names.add(fname.lower())
                visual_field_names.add(resolve_visual_field(fname).lower())
                # Also store the inner measure name (e.g. "Revenue" from "AGG(Revenue)")
                inner = extract_measure_from_function(resolve_visual_field(fname))
                if inner:
                    visual_field_names.add(inner.lower())

        # Find the first measure on the visual (for fallback suggestion)
        MEASURE_RE = re.compile(
            r'^(SUM|AVG|MIN|MAX|COUNT|DISTINCTCOUNT|CNTD|CNT|AGG|MEDIAN|ATTR)\(',
            re.IGNORECASE
        )
        fallback_measure = None
        if all_visual_fields:
            for f in all_visual_fields:
                fname = f.get("field", "") if isinstance(f, dict) else (f if isinstance(f, str) else "")
                resolved = resolve_visual_field(fname)
                if MEASURE_RE.match(resolved):
                    fallback_measure = resolved
                    break

        resolved = []
        for item in items:
            if not isinstance(item, dict):
                continue
            field = item.get("field")
            sort_info = item.get("sort")
            if field == "Measure Values":
                raw_measures = item.get("measure_values", [])
                resolved.extend([
                    resolve_visual_field(m.get("field") if isinstance(m, dict) else m)
                    for m in raw_measures
                ])
            elif field and isinstance(field, str):
                resolved_name = resolve_visual_field(field)
                if sort_info and isinstance(sort_info, dict):
                    sort_field = sort_info.get("field_name", "")
                    sort_order = sort_info.get("sort_order", "Ascending")
                    sort_by = sort_info.get("sort_by", "")

                    # Check if the sort field exists on the visual
                    sort_field_on_visual = sort_field.lower() in visual_field_names if sort_field else False

                    if sort_field_on_visual or sort_by.lower() not in ("nested",):
                        # Direct sort — field is on the visual
                        resolved.append({
                            "field": resolved_name,
                            "sort": sort_info,
                            "power_bi_sort_instruction": f"Sort by '{sort_field}' {sort_order}"
                        })
                    else:
                        # Nested sort — field NOT on the visual
                        if fallback_measure:
                            fallback_name = extract_measure_from_function(fallback_measure) or fallback_measure
                            resolved.append({
                                "field": resolved_name,
                                "sort": sort_info,
                                "power_bi_sort_instruction": f"Nested sort by '{sort_field}' is not supported in Power BI. Sort by '{fallback_name}' {sort_order} instead."
                            })
                        else:
                            resolved.append({
                                "field": resolved_name,
                                "sort": sort_info,
                                "power_bi_sort_instruction": f"Nested sort by '{sort_field}' is not supported in Power BI. Sort by {sort_order} on the available measure."
                            })
                else:
                    resolved.append(resolved_name)
        return resolved

    def get_field(source, index):
        try:
            item = source[index]
            return item.get("field") if isinstance(item, dict) else item
        except (IndexError, AttributeError):
            return None

    # ─── MATRIX MARK → Matrix ────
    if mark_type == "matrix":
        # ── Resolve columns shelf, stripping Tableau-only virtual fields ──
        # "Measure Names" is a Tableau virtual placeholder that groups measures;
        # in Power BI the measures go into the Values well, not as column headers.
        resolved_columns = [
            c for c in get_resolved_fields_with_sort(columns, rows + columns)
            if (c.get("field") if isinstance(c, dict) else c) != "Measure Names"
        ]

        visual_info = {
            "power_bi_visual_type": "Matrix",
            "rows": get_resolved_fields_with_sort(rows, rows + columns),
            "columns": resolved_columns,
            # values holds the actual measure field list from marks_text
            "values": get_resolved_fields(sheet.get("marks_text", [])),
        }

        # Extract from visual_properties.formatting
        formatting = sheet.get("visual_properties", {}).get("formatting", [])
        colors     = sheet.get("visual_properties", {}).get("colors", [])

        col_hdr_fmt = next((f for f in formatting if f.get("applied_to") == "Header (Columns)"), None)
        row_hdr_fmt = next((f for f in formatting if f.get("applied_to") == "Header (Rows)"),    None)
        title_fmt   = next((f for f in formatting if f.get("applied_to") == "Title"),             None)
        pane_fmt    = next((f for f in formatting if f.get("applied_to") == "Pane"),              None)
        mark_lbl_fmt = next((f for f in formatting if f.get("applied_to") == "Mark Labels"),      None)

        # column_headers from Header (Columns)
        if col_hdr_fmt:
            visual_info["column_headers"] = {
                "font":       col_hdr_fmt.get("font"),
                "font_color": col_hdr_fmt.get("font_color"),
                "font_size":  col_hdr_fmt.get("size"),
                "alignment":  col_hdr_fmt.get("alignment"),
                "bg_color":   col_hdr_fmt.get("bg_color"),
            }

        # row_headers from Header (Rows)
        if row_hdr_fmt:
            visual_info["row_headers"] = {
                "font":       row_hdr_fmt.get("font"),
                "font_color": row_hdr_fmt.get("font_color"),
                "font_size":  row_hdr_fmt.get("size"),
                "alignment":  row_hdr_fmt.get("alignment"),
                "bg_color":   row_hdr_fmt.get("bg_color"),
            }

        # values_formatting from colors array (kept separate from the field list in "values")
        # DISPONENT → alternate_text_color, OWNER → text_color
        if colors:
            values_fmt = {
                "font": (mark_lbl_fmt or {}).get("font"),
                "font_size": (mark_lbl_fmt or {}).get("size"),
                "font_color": (mark_lbl_fmt or {}).get("font_color"),
                "text_color": None,
                "alternate_text_color": None,
                "text_bg_color": (pane_fmt or {}).get("bg_color"),
                "alternate_bg_color": (pane_fmt or {}).get("bg_color"),
            }
            for c in colors:
                applied_to = (c.get("applied_to") or "").upper()
                if "DISPONENT" in applied_to:
                    values_fmt["alternate_text_color"] = c.get("color")
                elif "OWNER" in applied_to:
                    values_fmt["text_color"] = c.get("color")
                if c.get("bg_color"):
                    values_fmt["text_bg_color"] = c.get("bg_color")
                    values_fmt["alternate_bg_color"] = c.get("bg_color")
            # Store as values_formatting so the field list in "values" is preserved
            visual_info["values_formatting"] = values_fmt

        # title — visible only if visual_title exists
        visual_title = sheet.get("visual_title")

        # ✅ Always create object first
        title_obj = {
            "visible": True if visual_title else False,
            "text": visual_title or ""
        }

        # ✅ Always apply formatting (even if title OFF)
        if title_fmt:
            title_obj["font"]       = title_fmt.get("font")
            title_obj["font_color"] = title_fmt.get("font_color")
            title_obj["font_size"]  = title_fmt.get("size")
            title_obj["bold"]       = title_fmt.get("bold")
            title_obj["alignment"]  = title_fmt.get("alignment")
            title_obj["bg_color"]   = title_fmt.get("bg_color") or ""

        visual_info["title"] = title_obj

        return visual_info

    # ─── TEXT MARK → Card ────
    if mark_type == "text":
        marks_text = [m for m in sheet.get("marks_text", []) if m and m != "None"]
        resolved_fields = get_resolved_fields(marks_text)

        # ✅ Detect aggregation
        MEASURE_PATTERN = re.compile(
            r'^(SUM|AVG|MIN|MAX|COUNT|DISTINCTCOUNT|CNTD|CNT|AGG|MEDIAN|ATTR|STDEV|VAR|STDEVP|VARP)\(',
            re.IGNORECASE
        )

        has_measure = any(MEASURE_PATTERN.match(f or "") for f in resolved_fields)

        # ✅ 👉 YOUR CONDITION (NO AGGREGATION → TABLE)
        if resolved_fields and not has_measure:
            return {
                "power_bi_visual_type": "Table",
                "columns": [
                    {
                        "field": f,
                        "source": "marks_text"
                    }
                    for f in resolved_fields
                ]
            }

        visual_info = {
            "power_bi_visual_type": "Card",
            "fields": get_resolved_fields(marks_text),
            "category_label": "on",  # always on for Card
        }

        # Extract from visual_properties.formatting
        formatting = sheet.get("visual_properties", {}).get("formatting", [])

        pane_fmt  = next((f for f in formatting if f.get("applied_to") == "Mark Labels"),  None)
        title_fmt = next((f for f in formatting if f.get("applied_to") == "Title"), None)
        bg_fmt    = next((f for f in formatting if f.get("applied_to") == "Pane"), None)
        # callout_value + card_background from Pane
        if pane_fmt:
            visual_info["callout_value"] = {
                "font":       pane_fmt.get("font"),
                "font_color": pane_fmt.get("font_color"),
                "font_size":  pane_fmt.get("size"),
            }
            if bg_fmt and bg_fmt.get("bg_color"):
                visual_info["bg_color"] = bg_fmt.get("bg_color")

        # title — visible only if visual_title exists
        visual_title = sheet.get("visual_title")

        # ✅ Always create object first
        title_obj = {
            "visible": True if visual_title else False,
            "text": visual_title or ""
        }

        # ✅ Always apply formatting (even if title OFF)
        if title_fmt:
            title_obj["font"]       = title_fmt.get("font")
            title_obj["font_color"] = title_fmt.get("font_color")
            title_obj["font_size"]  = title_fmt.get("size")
            title_obj["bold"]       = title_fmt.get("bold")
            title_obj["alignment"]  = title_fmt.get("alignment")
            title_obj["bg_color"]   = title_fmt.get("bg_color") or ""

        visual_info["title"] = title_obj

        return visual_info

    # ─── TEXT TABLE MARK → Table ────
    if mark_type == "text table":
        resolved_rows  = get_resolved_fields(rows)
        resolved_cols  = get_resolved_fields(columns)
        resolved_marks = get_resolved_fields(
            [m for m in sheet.get("marks_text", []) if m and m != "None"]
        )

        formatting   = sheet.get("visual_properties", {}).get("formatting", [])
        header_fmt   = next((f for f in formatting if f.get("applied_to") == "Header"),      None)
        mark_lbl_fmt = next((f for f in formatting if f.get("applied_to") == "Mark Labels"), None)
        title_fmt    = next((f for f in formatting if f.get("applied_to") == "Title"),       None)
        pane_fmt     = next((f for f in formatting if f.get("applied_to") == "Pane"),        None)

        # ── Classify columns by whether they are measures or dimensions ──
        MEASURE_PATTERN = re.compile(
            r'^(SUM|AVG|MIN|MAX|COUNT|DISTINCTCOUNT|CNTD|CNT|AGG|MEDIAN|ATTR|STDEV|VAR|STDEVP|VARP)\(',
            re.IGNORECASE
        )

        def is_measure(field_str: str) -> bool:
            return bool(MEASURE_PATTERN.match(field_str or ""))

        all_columns     = resolved_rows + resolved_cols + resolved_marks
        text_columns    = [f for f in all_columns if not is_measure(f)]  # → Header fmt
        numeric_columns = [f for f in all_columns if is_measure(f)]      # → Mark Labels fmt

        # ── Build specific_columns entries (mirrors Power BI Specific Column panel) ──
        def _col_style(field: str, fmt: dict) -> dict:
            entry = {
                "series":          field,
                "apply_to_header": False,
                "apply_to_total":  False,
                "apply_to_values": True,
            }
            if fmt:
                entry["values"] = {
                    "font":       fmt.get("font"),
                    "font_color": fmt.get("font_color"),
                    "font_size":  fmt.get("size"),
                    "bg_color":   fmt.get("bg_color"),
                    "alignment":  fmt.get("alignment"),
                }
            return entry

        # ── Build a fmt lookup keyed by resolved field name ───────────
        fmt_by_field = {}
        if header_fmt:
            for field in text_columns:
                fmt_by_field[field] = {
                    "font":       header_fmt.get("font"),
                    "font_color": header_fmt.get("font_color"),
                    "font_size":  header_fmt.get("size"),
                    "bg_color":   header_fmt.get("bg_color"),
                    "alignment":  header_fmt.get("alignment"),
                }
        if mark_lbl_fmt:
            for field in numeric_columns:
                fmt_by_field[field] = {
                    "font":       header_fmt.get("font"),
                    "font_color": mark_lbl_fmt.get("font_color"),
                    "font_size":  header_fmt.get("size"),
                    "bg_color":   pane_fmt.get("bg_color"),
                    "alignment":  mark_lbl_fmt.get("alignment"),
                }

        # ── Build columns with formatting merged inline ────────────────
        raw_columns = build_table_field_mapping(sheet, payload)
        for col in raw_columns:
            field_name = col.get("field")
            # ── values (ALWAYS from Header) ──
            if header_fmt:
                col["values"] = {
                    "font": header_fmt.get("font"),
                    "font_size": header_fmt.get("size"),
                }
            if field_name in fmt_by_field:
                fmt = fmt_by_field[field_name]

                # ── specific column formatting ──
                col["specific_column"] = {
                    "series": field_name,
                    "font_color": fmt.get("font_color"),
                    "bg_color": fmt.get("bg_color"),
                    "alignment": fmt.get("alignment"),
                }

        # ── Title — visible only if visual_title exists on sheet ──────

        visual_title = sheet.get("visual_title")

        # ✅ Always create object first
        title_obj = {
            "visible": True if visual_title else False,
            "text": visual_title or ""
        }

        # ✅ Always apply formatting (even if title OFF)
        if title_fmt:
            title_obj["font"]       = title_fmt.get("font")
            title_obj["font_color"] = title_fmt.get("font_color")
            title_obj["font_size"]  = title_fmt.get("size")
            title_obj["bold"]       = title_fmt.get("bold")
            title_obj["alignment"]  = title_fmt.get("alignment")
            title_obj["bg_color"]   = title_fmt.get("bg_color") or ""

        return {
            "power_bi_visual_type": "Table",
            "title":                title_obj,
            "columns":              raw_columns,
        }

    # ─── BAR MARK → Bar Chart type selection ────
    if mark_type == "bar":
        resolved_rows = get_resolved_fields(rows)
        resolved_cols = get_resolved_fields(columns)
        all_visual = rows + columns
        resolved_rows_with_sort = get_resolved_fields_with_sort(rows, all_visual)
        resolved_cols_with_sort = get_resolved_fields_with_sort(columns, all_visual)

        # ─── DETECT MEASURE ───
        MEASURE_PATTERN = re.compile(
            r'^(SUM|AVG|MIN|MAX|COUNT|DISTINCTCOUNT|CNTD|CNT|AGG)\(',
            re.IGNORECASE
        )

        def is_measure(field):
            return bool(MEASURE_PATTERN.match(field or ""))

        row_has_measure = any(is_measure(f) for f in resolved_rows)
        col_has_measure = any(is_measure(f) for f in resolved_cols)

        # ─── DETECT MARKS COLOR (STACKING DRIVER) ───
        marks_color = [
            m for m in sheet.get("marks_color", [])
            if m and m != "None"
        ]
        has_mark_field = bool(marks_color)

        # ─── APPLY YOUR 4 RULES ───

        # 1. rows = dimension, columns = measure → BAR
        if not row_has_measure and col_has_measure:
            if has_mark_field:
                visual_type = "Stacked Bar Chart"
            else:
                visual_type = "Clustered Bar Chart"

        # 2. rows = measure, columns = dimension → COLUMN
        elif row_has_measure and not col_has_measure:
            if has_mark_field:
                visual_type = "Stacked Column Chart"
            else:
                visual_type = "Clustered Column Chart"

        # 3. Fallback — both or neither side has a measure
        else:
            visual_type = "Unknown"

        # ─── COMMON FORMATTING EXTRACTION ───
        formatting = sheet.get("visual_properties", {}).get("formatting", [])
        colors = sheet.get("visual_properties", {}).get("colors", [])

        title_fmt  = next((f for f in formatting if f.get("applied_to") == "Title"), None)
        header_fmt = next((f for f in formatting if f.get("applied_to") == "Header"), None)
        #legend_fmt = next((f for f in formatting if f.get("applied_to") == "Legend"), None)
        pane_fmt   = next((f for f in formatting if f.get("applied_to") == "Pane"), None)

        visual_info = {
            "power_bi_visual_type": visual_type,
            "rows": resolved_rows_with_sort,
            "columns": resolved_cols_with_sort,
        }

        # ─────────────────────────────────────────
        # ✅ TITLE (ALL BAR TYPES)
        # ─────────────────────────────────────────
        visual_title = sheet.get("visual_title")

        # ✅ Always create object first
        title_obj = {
            "visible": True if visual_title else False,
            "text": visual_title or ""
        }

        # ✅ Always apply formatting (even if title OFF)
        if title_fmt:
            title_obj["font"]       = title_fmt.get("font")
            title_obj["font_color"] = title_fmt.get("font_color")
            title_obj["font_size"]  = title_fmt.get("size")
            title_obj["bold"]       = title_fmt.get("bold")
            title_obj["alignment"]  = title_fmt.get("alignment")
            title_obj["bg_color"]   = title_fmt.get("bg_color") or ""

        visual_info["title"] = title_obj

        # ─────────────────────────────────────────
        # ✅ LEGENDS (ONLY STACKED)
        # ─────────────────────────────────────────
        if visual_type in ["Stacked Bar Chart", "Stacked Column Chart"]:
            visual_info["legends"] = resolve_legends_from_marks_color(marks_color, columns)  # ✅
            legend_fmt = get_legend_formatting(formatting, visual_info)
            if legend_fmt:
                visual_info["legend_text_formatting"] = [{
                    "font": legend_fmt.get("font"),
                    "font_color": legend_fmt.get("font_color"),
                    "font_size": legend_fmt.get("size"),
                    "bold": legend_fmt.get("bold"),
                }]

        # ─────────────────────────────────────────
        # ✅ HEADER → AXIS + BG (ALL 4)
        # ─────────────────────────────────────────
        if header_fmt:
            axis_values = {
                "font": header_fmt.get("font"),
                "font_color": header_fmt.get("font_color"),
                "font_size": header_fmt.get("size"),
            }

            visual_info["x_axis"] = {"values": axis_values}
            visual_info["y_axis"] = {"values": axis_values}

            # single bg color
            visual_info["bg_color"] = header_fmt.get("bg_color") or ""

        # ─────────────────────────────────────────
        # ✅ COLORS MAPPING
        # ─────────────────────────────────────────
        if colors:
            series_data = []
            gradient_data = []

            for c in colors:
                if c.get("palette"):
                    gradient_data.append(
                        generate_gradient_from_palette(c)
                    )

                elif c.get("color"):
                    series_data.append({
                        "series": c.get("applied_to"),
                        "color": c.get("color")
                    })

            # 🎯 STRICT MODE: ONLY ONE OUTPUT

            if gradient_data:

                if visual_type in ["Stacked Bar Chart", "Clustered Bar Chart"]:
                    visual_info["bars"] = gradient_data

                elif visual_type in ["Stacked Column Chart", "Clustered Column Chart"]:
                    visual_info["columns"] = gradient_data

            elif series_data:

                if visual_type in ["Stacked Bar Chart", "Clustered Bar Chart"]:
                    visual_info["bars"] = series_data

                elif visual_type in ["Stacked Column Chart", "Clustered Column Chart"]:
                    visual_info["series_colors"] = series_data

        # ─────────────────────────────────────────
        # ✅ LABELS (PANE MAPPING)
        # ─────────────────────────────────────────
        mark_labels_enabled = sheet.get("mark_labels_enabled", False)

        label_obj = {
            "show": "on" if mark_labels_enabled else "off",
        }

        if pane_fmt:
            label_obj.update({
                "font": pane_fmt.get("font"),
                "font_color": pane_fmt.get("font_color"),
                "font_size": pane_fmt.get("size"),
            })
            # ❌ DO NOT TAKE bg_color

        if visual_type in ["Stacked Bar Chart", "Stacked Column Chart"]:

            # ✅ Enable flag
            visual_info["total_labels_enabled"] = "on" if mark_labels_enabled else "off"
            label_obj = {}
            if pane_fmt:
                    label_obj.update({
                        "font": pane_fmt.get("font"),
                        "font_color": pane_fmt.get("font_color"),
                        "font_size": pane_fmt.get("size"),
                    })

            visual_info["total_labels"] = [label_obj]  # ← ARRAY

        else:
            # keep existing behavior for others
            label_obj = {
                "show": "on" if mark_labels_enabled else "off",
            }

            if pane_fmt:
                label_obj.update({
                    "font": pane_fmt.get("font"),
                    "font_color": pane_fmt.get("font_color"),
                    "font_size": pane_fmt.get("size"),
                })

            visual_info["data_labels"] = label_obj

        return visual_info

    # ─── LINE MARK → Line Chart ────
    if mark_type == "line":
        visual_info = {
            "power_bi_visual_type": "Line Chart",
            "rows": get_resolved_fields_with_sort(rows, rows + columns),
            "columns": get_resolved_fields_with_sort(columns, rows + columns),
        }
        # ─── LINE STYLE (from marks_path) ───
        marks_path = sheet.get("marks_path")
        if marks_path:
            visual_info.update(map_line_style_from_marks_path(marks_path))
        # legends from marks_color
        marks_color = [m for m in sheet.get("marks_color", []) if m and m != "None"]
        visual_info["legends"] = resolve_legends_from_marks_color(marks_color)  # ✅

        colors = sheet.get("visual_properties", {}).get("colors", [])
        # series colors mapping
        if colors:
            visual_info["series"] = [
                    {
                        "applied_to": c.get("applied_to"),
                        "color": c.get("color"),
                    }
                    for c in colors
            ]

        # Extract from visual_properties.formatting
        formatting = sheet.get("visual_properties", {}).get("formatting", [])
        header_fmt = next((f for f in formatting if f.get("applied_to") == "Header"), None)
        title_fmt  = next((f for f in formatting if f.get("applied_to") == "Title"),  None)

        legend_fmt = get_legend_formatting(formatting, visual_info)
        if legend_fmt:
            visual_info["legends_text_formatting"] = [
                {
                    "font": legend_fmt.get("font"),
                    "font_color": legend_fmt.get("font_color"),
                    "font_size": legend_fmt.get("size"),
                    "bold": legend_fmt.get("bold"),
                }
            ]

        # x_axis and y_axis values from Header (no bg_color inside)
        if header_fmt:
            axis_values = {
                "font":       header_fmt.get("font"),
                "font_color": header_fmt.get("font_color"),
                "font_size":  header_fmt.get("size"),
            }
            visual_info["x_axis"] = {"values": axis_values}
            visual_info["y_axis"] = {"values": axis_values}
            # bg_color once at top level
            visual_info["bg_color"] = header_fmt.get("bg_color") or ""

        # data_labels — show from mark_labels_enabled + formatting from Mark Labels
        mark_labels_enabled = sheet.get("mark_labels_enabled", False)
        mark_labels_fmt = next((f for f in formatting if f.get("applied_to") == "Mark Labels"), None)

        data_labels_obj = {
            "show": "on" if mark_labels_enabled else "off",
        }
        if mark_labels_fmt:
            data_labels_obj["font"]       = mark_labels_fmt.get("font")
            data_labels_obj["font_color"] = mark_labels_fmt.get("font_color")
            data_labels_obj["font_size"]  = mark_labels_fmt.get("size")
        visual_info["data_labels"] = data_labels_obj
        # title — visible only if visual_title exists
        visual_title = sheet.get("visual_title")

        # ✅ Always create object first
        title_obj = {
            "visible": True if visual_title else False,
            "text": visual_title or ""
        }

        # ✅ Always apply formatting (even if title OFF)
        if title_fmt:
            title_obj["font"]       = title_fmt.get("font")
            title_obj["font_color"] = title_fmt.get("font_color")
            title_obj["font_size"]  = title_fmt.get("size")
            title_obj["bold"]       = title_fmt.get("bold")
            title_obj["alignment"]  = title_fmt.get("alignment")
            title_obj["bg_color"]   = title_fmt.get("bg_color") or ""

        visual_info["title"] = title_obj

        return visual_info

    # ─── MULTIPLE MARKS → visual type + axis mapping ──────────────
    if mark_type == "multiple":
        types = {m.get("type", "").strip().lower() for m in multiple_marks}

        mark_field_map = {
            m.get("type", "").strip().lower(): m.get("field")
            for m in multiple_marks
        }

        if types == {"line", "area"}:
            visual_type = "Area Chart"
        elif types == {"bar", "line"}:
            visual_type = "Line and Clustered Column Chart"
        else:
            visual_type = "Unknown"

        # ─── Build base visual_info with merged field + formatting per axis ───
        x_field     = resolve_visual_field(get_field(columns, 0)) if get_field(columns, 0) else None
        y_field     = resolve_visual_field(mark_field_map.get("area") or mark_field_map.get("bar"))
        sec_y_field = resolve_visual_field(mark_field_map.get("line"))

        visual_info = {
            "power_bi_visual_type": visual_type,
            "x_axis":           {"field": x_field},
            "y_axis":           {"field": y_field},
            "secondary_y_axis": {"field": sec_y_field},
        }

        # ❗ ONLY APPLY FORMATTING FOR AREA CHART
        if visual_type == "Area Chart":

            formatting = sheet.get("visual_properties", {}).get("formatting", [])
            colors     = sheet.get("visual_properties", {}).get("colors", [])

            header_fmt = next((f for f in formatting if f.get("applied_to") == "Header"), None)
            title_fmt  = next((f for f in formatting if f.get("applied_to") == "Title"), None)
            pane_fmt   = next((f for f in formatting if f.get("applied_to") == "Pane"), None)

            is_dual_axis_area = (types == {"line", "area"})
            legend_fmt = get_legend_formatting(formatting, visual_info, skip=is_dual_axis_area)
            mark_labels_enabled = sheet.get("mark_labels_enabled", False)

            # ── Axis formatting merged into same x_axis / y_axis objects ──
            if header_fmt:
                axis_values = {
                    "font":       header_fmt.get("font"),
                    "font_color": header_fmt.get("font_color"),
                    "font_size":  header_fmt.get("size"),
                }

                visual_info["x_axis"]["values"]           = axis_values
                visual_info["y_axis"]["values"]           = axis_values
                visual_info["secondary_y_axis"]["values"] = axis_values

                visual_info["bg_color"] = header_fmt.get("bg_color") or ""

            # ───────────────────────────────
            # ✅ COLORS
            # ───────────────────────────────
            if colors:
                visual_info["lines_color"] = [
                    {
                        "applied_to": c.get("applied_to"),
                        "color": c.get("color")
                    }
                    for c in colors
                ]

            # ───────────────────────────────
            # ✅ LEGEND
            # ───────────────────────────────
            if legend_fmt:
                visual_info["legends_text_formatting"] = [{
                    "font": legend_fmt.get("font"),
                    "font_color": legend_fmt.get("font_color"),
                    "font_size": legend_fmt.get("size"),
                    "bold": legend_fmt.get("bold"),
                }]

            # ───────────────────────────────
            # ✅ DATA LABELS (APPEND + FIX)
            # ───────────────────────────────
            label_obj = {
                "show": "on" if mark_labels_enabled else "off"
            }

            if pane_fmt:
                label_obj.update({
                    "font": pane_fmt.get("font"),
                    "font_color": pane_fmt.get("font_color"),
                    "font_size": pane_fmt.get("size"),
                })

            if "data_labels" not in visual_info:
                visual_info["data_labels"] = []

            visual_info["data_labels"].append(label_obj)

            # ───────────────────────────────
            # ✅ TITLE
            # ───────────────────────────────
            visual_title = sheet.get("visual_title")

            title_obj = {
                "visible": True if visual_title else False,
                "text": visual_title or ""
            }

            if title_fmt:
                title_obj.update({
                    "font": title_fmt.get("font"),
                    "font_color": title_fmt.get("font_color"),
                    "font_size": title_fmt.get("size"),
                    "bold": title_fmt.get("bold"),
                    "alignment": title_fmt.get("alignment"),
                    "bg_color": title_fmt.get("bg_color") or ""
                })

            visual_info["title"] = title_obj

        return visual_info

    # ─── PIE MARK → visual type + values axis mapping ─────────────
    if mark_type == "pie":
        marks_text  = [m for m in sheet.get("marks_text",  []) if m and m != "None"]
        marks_angle = [m for m in sheet.get("marks_angle", []) if m and m != "None"]
        marks_color = [m for m in sheet.get("marks_color", []) if m and m != "None"]

        visual_info = {
            "power_bi_visual_type": "Pie Chart",
            "axis_mapping": {
                "values": list(dict.fromkeys(get_resolved_fields(marks_text + marks_angle)))
            },
            "legends": resolve_legends_from_marks_color(marks_color),  # ✅
        }

        # Extract from visual_properties.formatting
        formatting = sheet.get("visual_properties", {}).get("formatting", [])
        legend_fmt = get_legend_formatting(formatting, visual_info)
        if legend_fmt:
            visual_info["legends_text_formatting"] = [
                {
                    "font": legend_fmt.get("font"),
                    "font_color": legend_fmt.get("font_color"),
                    "font_size": legend_fmt.get("size"),
                    "bold": legend_fmt.get("bold"),
                }
            ]

        colors = sheet.get("visual_properties", {}).get("colors", [])
        mark_labels_enabled = sheet.get("mark_labels_enabled", False)

        mark_labels_fmt = next((f for f in formatting if f.get("applied_to") == "Mark Labels"), None)
        title_fmt = next((f for f in formatting if f.get("applied_to") == "Title"),        None)

        # detail_labels — show from mark_labels_enabled + formatting from Mark Labels
        detail_labels_obj = {
            "show": "on" if mark_labels_enabled else "off",
        }
        if mark_labels_fmt:
            detail_labels_obj["font"]       = mark_labels_fmt.get("font")
            detail_labels_obj["font_color"] = mark_labels_fmt.get("font_color")
            detail_labels_obj["font_size"]  = mark_labels_fmt.get("size")
            detail_labels_obj["alignment"]  = mark_labels_fmt.get("alignment")
        visual_info["detail_labels"] = detail_labels_obj

        # slices — series from colors array, applied_to kept as-is
        if colors:
            visual_info["slices"] = {
                "series": [
                    {
                        "applied_to": c.get("applied_to"),
                        "color":      c.get("color"),
                    }
                    for c in colors
                ]
            }
        pane_fmt = next((f for f in formatting if f.get("applied_to") == "Pane"), None)

        if pane_fmt and pane_fmt.get("bg_color"):
            visual_info["visual_bg"] = {

                "color": pane_fmt.get("bg_color")
            }
        # title — visible only if visual_title exists
        visual_title = sheet.get("visual_title")

        # ✅ Always create object first
        title_obj = {
            "visible": True if visual_title else False,
            "text": visual_title or ""
        }

        # ✅ Always apply formatting (even if title OFF)
        if title_fmt:
            title_obj["font"]       = title_fmt.get("font")
            title_obj["font_color"] = title_fmt.get("font_color")
            title_obj["font_size"]  = title_fmt.get("size")
            title_obj["bold"]       = title_fmt.get("bold")
            title_obj["alignment"]  = title_fmt.get("alignment")
            title_obj["bg_color"]   = title_fmt.get("bg_color") or ""

        visual_info["title"] = title_obj

        return visual_info
    # ─── SHAPE/CIRCLE MARK → Scatter Chart ────
    if mark_type in ["shape", "circle"]:
        resolved_rows = get_resolved_fields(rows)
        resolved_cols = get_resolved_fields(columns)

        MEASURE_PATTERN = re.compile(
            r'^(SUM|AVG|MIN|MAX|COUNT|DISTINCTCOUNT|CNTD|CNT|AGG|MEDIAN|ATTR)\(',
            re.IGNORECASE
        )

        def is_measure(field_str: str) -> bool:
            return bool(MEASURE_PATTERN.match(field_str or ""))

        cols_measures = [f for f in resolved_cols if is_measure(f)]
        cols_dimensions = [f for f in resolved_cols if not is_measure(f)]
        rows_measures = [f for f in resolved_rows if is_measure(f)]
        rows_dimensions = [f for f in resolved_rows if not is_measure(f)]

        x_axis_val = None
        y_axis_val = None
        size_val = None

        if cols_measures and rows_measures:
            x_axis_val = cols_measures[0]
            y_axis_val = rows_measures[0]
            remaining_measures = cols_measures[1:] + rows_measures[1:]
            if remaining_measures:
                size_val = remaining_measures[0]
        else:
            all_measures = cols_measures + rows_measures
            if len(all_measures) > 0:
                x_axis_val = all_measures[0]
            if len(all_measures) > 1:
                y_axis_val = all_measures[1]
            if len(all_measures) > 2:
                size_val = all_measures[2]

        marks_size = get_resolved_fields(sheet.get("marks_size", []))
        if marks_size:
            size_val = marks_size[0]

        dimensions = []
        for d in (cols_dimensions + rows_dimensions + get_resolved_fields(sheet.get("marks_detail", []))):
            if d not in dimensions:
                dimensions.append(d)

        visual_info = {
            "power_bi_visual_type": "Scatter Chart",
            "x_axis":  x_axis_val,
            "y_axis":  y_axis_val,
            "size":    size_val,
            "values":  dimensions,
        }

        formatting = sheet.get("visual_properties", {}).get("formatting", [])
        colors     = sheet.get("visual_properties", {}).get("colors", [])

        title_fmt  = next((f for f in formatting if f.get("applied_to") == "Title"),  None)
        header_fmt = next((f for f in formatting if f.get("applied_to") == "Header"), None)
        legend_fmt = get_legend_formatting(formatting, visual_info)
        pane_fmt   = next((f for f in formatting if f.get("applied_to") == "Pane"),   None)

        # ── Title ──────────────────────────────────────────────────────
        visual_title = sheet.get("visual_title")
        title_obj = {
            "visible": True if visual_title else False,
            "text":    visual_title or ""
        }
        if title_fmt:
            title_obj["font"]       = title_fmt.get("font")
            title_obj["font_color"] = title_fmt.get("font_color")
            title_obj["font_size"]  = title_fmt.get("size")
            title_obj["bold"]       = title_fmt.get("bold")
            title_obj["alignment"]  = title_fmt.get("alignment")
            title_obj["bg_color"]   = title_fmt.get("bg_color") or ""
        visual_info["title"] = title_obj

        # ── X / Y axis from Header, bg_color at top level ──────────────
        if header_fmt:
            axis_values = {
                "font":       header_fmt.get("font"),
                "font_color": header_fmt.get("font_color"),
                "font_size":  header_fmt.get("size"),
            }
            visual_info["x_axis"] = {"field": visual_info["x_axis"], "values": axis_values}
            visual_info["y_axis"] = {"field": visual_info["y_axis"], "values": axis_values}
            visual_info["bg_color"] = header_fmt.get("bg_color") or ""

        # ── Legends from marks_color ────────────────────────────────────
        marks_color = [m for m in sheet.get("marks_color", []) if m and m != "None"]
        if marks_color:
            visual_info["legends"] = resolve_legends_from_marks_color(marks_color, columns)  # ✅
            if legend_fmt:
                visual_info["legends_text_formatting"] = [{
                    "font":       legend_fmt.get("font"),
                    "font_color": legend_fmt.get("font_color"),
                    "font_size":  legend_fmt.get("size"),
                    "bold":       legend_fmt.get("bold"),
                }]

        # ── Marker series colors ────────────────────────────────────────
        if colors:
            visual_info["markers_series"] = [
                {
                    "applied_to": c.get("applied_to"),
                    "color":      c.get("color"),
                }
                for c in colors
            ]

        # ── Category labels from Pane (no bg_color) ────────────────────
        mark_labels_enabled = sheet.get("mark_labels_enabled", False)
        category_labels_obj = {
            "show": "on" if mark_labels_enabled else "off",
        }
        if pane_fmt:
            category_labels_obj["font"]       = pane_fmt.get("font")
            category_labels_obj["font_color"] = pane_fmt.get("font_color")
            category_labels_obj["font_size"]  = pane_fmt.get("size")
            # ❌ bg_color intentionally excluded
        visual_info["category_labels"] = category_labels_obj

        return visual_info

    # ─── MAP MARK → Map ────
    if mark_type == "map":
        resolved_rows = get_resolved_fields(rows)
        resolved_cols = get_resolved_fields(columns)

        visual_info = {
            "power_bi_visual_type": "Map",
            "latitude": resolved_rows[0] if len(resolved_rows) > 0 else None,
            "longitude": resolved_cols[0] if len(resolved_cols) > 0 else None,
            "location": get_resolved_fields(sheet.get("marks_detail", [])),
            "bubble_size": get_resolved_fields(sheet.get("marks_size", [])),
        }

        formatting = sheet.get("visual_properties", {}).get("formatting", [])
        colors     = sheet.get("visual_properties", {}).get("colors", [])
        title_fmt  = next((f for f in formatting if f.get("applied_to") == "Title"),  None)

        marks_color = [m for m in sheet.get("marks_color", []) if m and m != "None"]
        if marks_color:
            visual_info["legends"] = resolve_legends_from_marks_color(marks_color, columns)
            legend_fmt = get_legend_formatting(formatting, visual_info)
            if legend_fmt:
                visual_info["legends_text_formatting"] = [{
                    "font":       legend_fmt.get("font"),
                    "font_color": legend_fmt.get("font_color"),
                    "font_size":  legend_fmt.get("size"),
                    "bold":       legend_fmt.get("bold"),
                }]

        # ── Title ──────────────────────────────────────────────────────
        visual_title = sheet.get("visual_title")
        title_obj = {
            "visible": True if visual_title else False,
            "text":    visual_title or ""
        }
        if title_fmt:
            title_obj.update({
                "font":       title_fmt.get("font"),
                "font_color": title_fmt.get("font_color"),
                "font_size":  title_fmt.get("size"),
                "bold":       title_fmt.get("bold"),
                "alignment":  title_fmt.get("alignment"),
                "bg_color":   title_fmt.get("bg_color") or ""
            })
        visual_info["title"] = title_obj

        if colors:
            visual_info["colors"] = [
                {
                    "applied_to": c.get("applied_to"),
                    "color":      c.get("color"),
                }
                for c in colors
            ]

        return visual_info

    # ─── AREA MARK (single) → Area Chart ────
    if mark_type == "area":
        # columns → X-axis (dimension/date), rows → Y-axis (measure)
        x_field = resolve_visual_field(get_field(columns, 0)) if get_field(columns, 0) else None
        y_field = resolve_visual_field(get_field(rows, 0))    if get_field(rows, 0)    else None

        visual_info = {
            "power_bi_visual_type": "Area Chart",
            "x_axis": {"field": x_field},
            "y_axis": {"field": y_field},
        }

        formatting = sheet.get("visual_properties", {}).get("formatting", [])
        colors     = sheet.get("visual_properties", {}).get("colors", [])

        header_fmt      = next((f for f in formatting if f.get("applied_to") == "Header"),      None)
        title_fmt       = next((f for f in formatting if f.get("applied_to") == "Title"),       None)
        pane_fmt        = next((f for f in formatting if f.get("applied_to") == "Pane"),        None)
        mark_labels_fmt = next((f for f in formatting if f.get("applied_to") == "Mark Labels"), None)

        legend_fmt          = get_legend_formatting(formatting, visual_info)
        mark_labels_enabled = sheet.get("mark_labels_enabled", False)

        # ── Axis formatting merged into same x_axis / y_axis objects ──────
        if header_fmt:
            axis_values = {
                "font":       header_fmt.get("font"),
                "font_color": header_fmt.get("font_color"),
                "font_size":  header_fmt.get("size"),
            }
            visual_info["x_axis"]["values"] = axis_values
            visual_info["y_axis"]["values"] = axis_values
            visual_info["bg_color"] = header_fmt.get("bg_color") or ""

        # ── Series colors ────────────────────────────────────────────────
        if colors:
            visual_info["lines_color"] = [
                {"applied_to": c.get("applied_to"), "color": c.get("color")}
                for c in colors
            ]

        # ── Legends from marks_color ─────────────────────────────────────
        marks_color = [m for m in sheet.get("marks_color", []) if m and m != "None"]
        visual_info["legends"] = resolve_legends_from_marks_color(marks_color)
        if legend_fmt:
            visual_info["legends_text_formatting"] = [{
                "font":       legend_fmt.get("font"),
                "font_color": legend_fmt.get("font_color"),
                "font_size":  legend_fmt.get("size"),
                "bold":       legend_fmt.get("bold"),
            }]

        # ── Data labels ──────────────────────────────────────────────────
        label_obj = {"show": "on" if mark_labels_enabled else "off"}
        if pane_fmt:
            label_obj.update({
                "font":       pane_fmt.get("font"),
                "font_color": pane_fmt.get("font_color"),
                "font_size":  pane_fmt.get("size"),
            })
        if mark_labels_fmt:
            label_obj.update({
                "font":       mark_labels_fmt.get("font"),
                "font_color": mark_labels_fmt.get("font_color"),
                "font_size":  mark_labels_fmt.get("size"),
            })
        visual_info["data_labels"] = label_obj

        # ── Title ────────────────────────────────────────────────────────
        visual_title = sheet.get("visual_title")
        title_obj = {
            "visible": True if visual_title else False,
            "text":    visual_title or ""
        }
        if title_fmt:
            title_obj.update({
                "font":       title_fmt.get("font"),
                "font_color": title_fmt.get("font_color"),
                "font_size":  title_fmt.get("size"),
                "bold":       title_fmt.get("bold"),
                "alignment":  title_fmt.get("alignment"),
                "bg_color":   title_fmt.get("bg_color") or ""
            })
        visual_info["title"] = title_obj

        return visual_info

    # ─── BOX PLOT MARK → Custom Visual ────
    if "box" in combined_type:
        return {
            "power_bi_visual_type": "Custom Visual",
            "custom_visual_name": "Box Plot",
            "import_link": "https://learn.microsoft.com/en-us/power-bi/developer/visuals/import-visual"
        }

    # ─── HEATMAP MARK → Custom Visual ────
    elif "heatmap" in combined_type or mark_type in ["square", "density"]:
        return {
            "power_bi_visual_type": "Custom Visual",
            "custom_visual_name": "Heatmap",
            "import_link": "https://learn.microsoft.com/en-us/power-bi/developer/visuals/import-visual"
        }

    # ─── REFERENCE BAND MARK → Custom Visual ────
    elif "reference" in combined_type:
        return {
            "power_bi_visual_type": "Custom Visual",
            "custom_visual_name": "Reference Band",
            "import_link": "https://learn.microsoft.com/en-us/power-bi/developer/visuals/import-visual"
        }

    # ─── SINGLE MARK → only visual type, no axis mapping ──────────
    return {
        "power_bi_visual_type": "Custom Visual",
        "import_link": "https://learn.microsoft.com/en-us/power-bi/developer/visuals/import-visual"
    }


def replace_custom_sql_fields(fields, payload):

    ds_block = payload.get("datasources_and_connections", {})
    custom_sql = ds_block.get("custom_sql", [])

    if not custom_sql:
        return fields

    table_name = (
        custom_sql[0].get("bi_table_name")
        or custom_sql[0].get("tableau_table_name")
        or custom_sql[0].get("table_name")
    )

    if not table_name:
        return fields


    updated = []
    for f in fields:
        if isinstance(f, str) and "(Custom SQL Query" in f:
            f = re.sub(r"\(Custom SQL Query.*?\)", f"({table_name})", f)
        elif isinstance(f, dict):
            # Deep copy so we never mutate the original sheet data
            f = copy.deepcopy(f)
            # Recurse for field name
            if isinstance(f.get("field"), str) and "(Custom SQL Query" in f["field"]:
                f["field"] = re.sub(r"\(Custom SQL Query.*?\)", f"({table_name})", f["field"])

            # Recurse for measure_values array (specific to Measure Values field)
            if "measure_values" in f and isinstance(f["measure_values"], list):
                updated_mvs = []
                for v in f["measure_values"]:
                    if isinstance(v, str) and "(Custom SQL Query" in v:
                        updated_mvs.append(re.sub(r"\(Custom SQL Query.*?\)", f"({table_name})", v))
                    elif isinstance(v, dict):
                        v = copy.deepcopy(v)
                        if isinstance(v.get("field"), str) and "(Custom SQL Query" in v["field"]:
                            v["field"] = re.sub(r"\(Custom SQL Query.*?\)", f"({table_name})", v["field"])
                        updated_mvs.append(v)
                    else:
                        updated_mvs.append(v)
                f["measure_values"] = updated_mvs

        updated.append(f)

    return updated


def replace_cntd_with_distinct(fields):
    updated = []
    for f in fields:
        if isinstance(f, str) and f.startswith("CNTD("):
            f = re.sub(r"^CNTD\(", "DISTINCTCOUNT(", f)
        elif isinstance(f, dict):
            # Recurse for field name
            if isinstance(f.get("field"), str) and f["field"].startswith("CNTD("):
                f["field"] = re.sub(r"^CNTD\(", "DISTINCTCOUNT(", f["field"])

            # Recurse for nested measure_values
            if "measure_values" in f and isinstance(f["measure_values"], list):
                updated_mvs = []
                for v in f["measure_values"]:
                    if isinstance(v, str) and v.startswith("CNTD("):
                        updated_mvs.append(re.sub(r"^CNTD\(", "DISTINCTCOUNT(", v))
                    elif isinstance(v, dict) and isinstance(v.get("field"), str) and v["field"].startswith("CNTD("):
                        v["field"] = re.sub(r"^CNTD\(", "DISTINCTCOUNT(", v["field"])
                        updated_mvs.append(v)
                    else:
                        updated_mvs.append(v)
                f["measure_values"] = updated_mvs

        updated.append(f)
    return updated



FORMAT_TYPE_MAPPING = {
    "Automatic":           { "format": "General"      },
    "Number (Standard)":   { "format": "Whole number" },
    "Percentage":          { "format": "Percentage"   },
    "Scientific":          { "format": "Scientific"   },
    "Custom":              { "format": "Custom"       },
    "Currency (Standard)": { "format": "Currency"     },
}

LCID_CURRENCY_MAPPING = {
    "1033": "$ US Dollar",
    "16393": "₹ Indian Rupee",
    "3081": "A$ Australian Dollar",
    "2057": "£ British Pound",
    "1036": "€ Euro",
    "1031": "€ Euro (German)"
}
DISPLAY_UNITS_MAPPING = {
    "Normal":    "",
    "None":      "",
    "Thousands": ",",
    "Millions":  ",,",
    "Billions":  ",,,",
}
NEGATIVE_FORMAT_MAPPING = {
    "Automatic": {"brackets": False, "trailing_minus": False},
    "-1234":     {"brackets": False, "trailing_minus": False},
    "(1234)":    {"brackets": True,  "trailing_minus": False},
    "1234-":     {"brackets": False, "trailing_minus": True },
}

DECIMAL_ONLY_TYPES = {"Percentage", "Scientific"}
PASSTHROUGH_TYPES  = {"Custom"}
FULL_CUSTOM_TYPES  = {"Number (Custom)", "Currency (Custom)"}


def build_format_string(
    decimal_places: int = 2,
    thousands_separator: str = "on",
    prefix: str = "",
    suffix: str = "",
    negative_format: str = "-1234",
    display_units: str = "Normal",
) -> str:

    # ─── Number pattern ─────────────────────────────────────────
    comma    = "," if thousands_separator == "on" else ""
    decimals = f".{'0' * decimal_places}" if decimal_places > 0 else ""
    scale    = DISPLAY_UNITS_MAPPING.get(display_units, "")
    number   = f"#{comma}##0{decimals}{scale}"

    # ─── Prefix / Suffix literals ────────────────────────────────
    pre = f'"{prefix}"' if prefix else ""
    suf = f'"{suffix}"' if suffix else ""

    # ─── Positive section ────────────────────────────────────────
    positive = f"{pre}{number}{suf}"

    # ─── Negative section ────────────────────────────────────────
    neg_info = NEGATIVE_FORMAT_MAPPING.get(
        negative_format,
        {"brackets": False, "trailing_minus": False}
    )

    if neg_info["brackets"]:
        negative = f"{pre}({number}){suf}"
    elif neg_info["trailing_minus"]:
        negative = f"{pre}{number}{suf}-"
    else:
        negative = f"-{pre}{number}{suf}"

    return f"{positive};{negative}"


def apply_bi_format(fields: list) -> list:
    for field in fields:
        if not isinstance(field, dict):
            continue

        # ─── Recurse into measure_values if present ───────────────
        if "measure_values" in field and isinstance(field["measure_values"], list):
            field["measure_values"] = apply_bi_format(field["measure_values"])

        fmt = field.get("format")
        if not isinstance(fmt, dict):
            continue

        format_type = fmt.get("format_type")
        if not format_type:
            continue

        # ─── STEP 2: FULL CUSTOM (before mapping lookup!) ────────────
        if format_type in FULL_CUSTOM_TYPES:
            decimal_places      = int(fmt.get("decimal_places") or 2)
            thousands_separator = "on" if fmt.get("thousands_separator", True) else "off"
            prefix              = fmt.get("prefix") or ""
            suffix              = fmt.get("suffix") or ""
            negative_format     = fmt.get("negative_values") or "-1234"
            display_units       = fmt.get("display_units") or "Normal"

            format_string = build_format_string(
                decimal_places=decimal_places,
                thousands_separator=thousands_separator,
                prefix=prefix,
                suffix=suffix,
                negative_format=negative_format,
                display_units=display_units,
            )

            field["format"]["bi_format"] = {
                "format":        "Custom",
                "format_string": format_string,
            }
            continue

        # ─── STEP 3: mapping lookup (only for remaining types) ────────
        mapping = FORMAT_TYPE_MAPPING.get(format_type)
        if not mapping:
            continue

        # ─── STEP 4: DECIMAL ONLY ────────────────────────────────────
        if format_type in DECIMAL_ONLY_TYPES:
            decimal_places = int(fmt.get("decimal_places") or 2)

            field["format"]["bi_format"] = {
                "format":         mapping["format"],
                "format_string":  None,
                "decimal_places": decimal_places,
            }

        # ─── STEP 5: PASSTHROUGH ─────────────────────────────────────
        elif format_type in PASSTHROUGH_TYPES:
            field["format"]["bi_format"] = {
                "format":        "Custom",
                "format_string": fmt.get("raw_code") or fmt.get("format_string") or "",
            }

        # ─── STEP 6: STATIC ──────────────────────────────────────────
        else:
            bi_format = {
                "format":        mapping["format"],
                "format_string": None,
            }

            if format_type == "Currency (Standard)":
                locale_id = str(fmt.get("locale", ""))
                if locale_id.startswith("C"):
                    locale_id = locale_id[1:]

                currency_str = LCID_CURRENCY_MAPPING.get(locale_id, "$ US Dollar")
                bi_format["currency_symbol"] = currency_str

            field["format"]["bi_format"] = bi_format

    return fields


def build_table_field_mapping(sheet: dict, payload: dict = None) -> list:
    """
    For Table visual — flattens all fields into single columns array.
    Each entry shows field name + source.
    Aggregation mapping (CNTD→DISTINCTCOUNT) done by resolve_visual_field.
    """
    rows         = sheet.get("rows", [])
    columns      = sheet.get("columns", [])

    marks_text   = [m for m in sheet.get("marks_text", []) if m and m != "None"]
    marks_detail = [m for m in sheet.get("marks_detail", []) if m and m != "None"]
    marks_size   = [m for m in sheet.get("marks_size", []) if m and m != "None"]

    all_columns = []

    for r in rows:
        field_value = r.get("field") if isinstance(r, dict) else r
        if field_value and field_value != "None":
            all_columns.append({
                "field":  resolve_visual_field(field_value),
                "source": "rows"
            })

    for c in columns:
        field_value = c.get("field") if isinstance(c, dict) else c
        if field_value and field_value != "None":
            all_columns.append({
                "field":  resolve_visual_field(field_value),
                "source": "columns"
            })

    for m in marks_text:
        field_value = m.get("field") if isinstance(m, dict) else m
        if field_value and field_value != "None":
            all_columns.append({
                "field":  resolve_visual_field(field_value),
                "source": "marks_text"
            })

    for m in marks_detail:
        field_value = m.get("field") if isinstance(m, dict) else m
        if field_value and field_value != "None":
            all_columns.append({
                "field":  resolve_visual_field(field_value),
                "source": "marks_detail"
            })

    for m in marks_size:
        field_value = m.get("field") if isinstance(m, dict) else m
        if field_value and field_value != "None":
            all_columns.append({
                "field":  resolve_visual_field(field_value),
                "source": "marks_size"
            })

    return all_columns



def normalize_string(text: str) -> str:
    if not text: return ""
    return re.sub(r'[^a-zA-Z0-9]', '', text).lower()

def resolve_visual_field(field_str: str, tables: list = None, custom_sql: list = None) -> str:
    """
    Maps only aggregation prefixes for Power BI compatibility.
    CNTD → DISTINCTCOUNT, CNT → COUNT.
    Inner field name is preserved exactly as-is from parsing.
    """
    # if not isinstance(field_str, str) or field_str == "Measure Values":
    #     return field_str
    if not field_str or not isinstance(field_str, str) or field_str == "Measure Values":
        return field_str if isinstance(field_str, str) else ""
    pattern = r'^([A-Z0-9]+)\((.+)\)$'
    match = re.match(pattern, field_str, re.IGNORECASE)

    if match:
        agg_func = match.group(1).upper()
        inner_content = match.group(2)

        if agg_func == "CNTD":
            agg_func = "DISTINCTCOUNT"
        elif agg_func == "CNT":
            agg_func = "COUNT"

        return f"{agg_func}({inner_content})"

    return field_str

def resolve_visual_field_inner(field: str, tables: list, custom_sql: list = None) -> str:
    """
    Internal helper to resolve a field name after stripping aggregation.
    Supports names with and without table hints: 'Field (Table)' or 'Field'.
    Handles custom_sql columns which use 'renamed_column_name' instead of
    'tableau_renamed_column_name'.
    """
    clean_field = field
    ds_hint = ""

    # Detect hints in parentheses: 'CustomerID (Custom SQL Query)'
    paren_match = re.match(r'^(.+?)\s*\(([^)]+)\)\s*$', field)
    # Detect spaces hints in field_id: 'CustomerID Custom SQL Query'
    space_hint_match = re.match(r'^(.+?)\s+([A-Z0-9 ]+Query|.+Table)\s*$', field, re.IGNORECASE) if not paren_match else None

    if paren_match:
        clean_field = paren_match.group(1).strip()
        ds_hint = paren_match.group(2).strip()
    elif space_hint_match:
        clean_field = space_hint_match.group(1).strip()
        ds_hint = space_hint_match.group(2).strip()

    norm_field = normalize_string(clean_field)
    norm_hint = normalize_string(ds_hint) if ds_hint else ""

    all_sources = (tables or []) + (custom_sql or [])

    def _col_name_matches(col: dict, norm: str) -> bool:
        """Check if a column matches the normalized field name."""
        return (
            normalize_string(col.get("tableau_column_name") or "") == norm or
            normalize_string(col.get("tableau_renamed_column_name") or "") == norm or
            normalize_string(col.get("renamed_column_name") or "") == norm
        )

    def _col_bi_name(col: dict) -> str:
        """Return the best BI column name from a column dict."""
        return (
            col.get("bi_column_name")
            or col.get("tableau_renamed_column_name")
            or col.get("renamed_column_name")
            or col.get("tableau_column_name")
        )

    def _source_matches_hint(source: dict, norm_h: str, raw_hint: str) -> bool:
        """Check if a source table matches the datasource hint."""
        t_name = source.get("tableau_table_name") or ""
        ds_name = source.get("datasource") or ""
        if normalize_string(t_name) == norm_h or normalize_string(ds_name) == norm_h:
            return True
        # Custom SQL sources: hint like 'Custom SQL Query' matches any custom_sql source
        if "custom sql query" in raw_hint.lower() and source.get("relation_type") == "custom_sql":
            return True
        # Substring match: hint may be a prefix of a longer datasource name
        if ds_name and norm_h and norm_h in normalize_string(ds_name):
            return True
        return False

    # PASS 1: Attempt to match with Hint
    if ds_hint:
        for source in all_sources:
            if _source_matches_hint(source, norm_hint, ds_hint):
                for col in source.get("columns", []):
                    if _col_name_matches(col, norm_field):
                        return _col_bi_name(col)

    # PASS 2: Universal match (if no hint or hint didn't resolve)
    for source in all_sources:
        for col in source.get("columns", []):
            if _col_name_matches(col, norm_field):
                return _col_bi_name(col)

    return clean_field # Fallback to cleaned original if not found

def resolve_legends_from_marks_color(marks_color: list, columns: list = None) -> list:
    """
    Returns legend fields only if marks_color contains dimensions/calculated fields.
    If marks_color is a measure (SUM, AGG, etc.), legends must be empty.
    """
    MEASURE_PATTERN = re.compile(
        r'^(SUM|AVG|MIN|MAX|COUNT|DISTINCTCOUNT|CNTD|CNT|AGG|MEDIAN|ATTR|STDEV|VAR|STDEVP|VARP)\(',
        re.IGNORECASE
    )

    legends = []
    for m in marks_color:
        field = m.get("field") if isinstance(m, dict) else m
        if not field or field == "None":
            continue
        # ✅ Resolve "Measure Names" using columns as reference
        if field.strip() == "Measure Names":
            if columns:
                for col in columns:
                    col_field = col.get("field") if isinstance(col, dict) else col

                    # Handle Measure Values wrapper
                    if isinstance(col, dict) and col_field == "Measure Values":
                        for mv in col.get("measure_values", []):
                            mv_field = mv.get("field") if isinstance(mv, dict) else mv
                            if not mv_field:
                                continue
                            if MEASURE_PATTERN.match(mv_field.strip()):
                                continue  # measure → skip ❌
                            legends.append(resolve_visual_field(mv_field))  # dimension → add ✅

                    # Handle direct field
                    elif col_field:
                        if MEASURE_PATTERN.match((col_field or "").strip()):
                            continue  # measure → skip ❌
                        legends.append(resolve_visual_field(col_field))  # dimension → add ✅
            continue  # Done with Measure Names, move on

        # Normal flow — skip measures
        if MEASURE_PATTERN.match(field.strip()):
            continue

        # Dimension → add to legends ✅
        legends.append(resolve_visual_field(field))

    return legends

def map_line_style_from_marks_path(marks_path: str) -> dict:
    style = (marks_path or "").strip().lower()

    mapping = {
        "linear (solid)":  {"interpolation_type": "linear",  "line_style": "solid"},
        "linear (dashed)": {"interpolation_type": "linear",  "line_style": "dashed"},
        "linear (dotted)": {"interpolation_type": "linear",  "line_style": "dotted"},

        "jump (solid)":    {"interpolation_type": "linear",  "line_style": "solid"},
        "jump (dashed)":   {"interpolation_type": "linear",  "line_style": "solid"},
        "jump (dotted)":   {"interpolation_type": "linear",  "line_style": "solid"},

        "step (solid)":    {"interpolation_type": "stepped", "line_style": "solid"},
        "step (dashed)":   {"interpolation_type": "stepped", "line_style": "dashed"},
        "step (dotted)":   {"interpolation_type": "stepped", "line_style": "dotted"},
    }

    return {
        "line_styling": mapping.get(style, {"interpolation_type": "linear", "line_style": "solid"})
    }

def generate_gradient_from_palette(color_config: dict) -> dict:

    applied_to = color_config.get("applied_to", "")
    reverse = color_config.get("reverse", False)
    palette = (color_config.get("palette") or "").lower()

    # 🎯 Step 1: Extract clean field
    match = re.match(r'^[A-Z]+\((.+)\)$', applied_to)
    based_on = match.group(1) if match else applied_to

    # 🎯 Step 2: Assign hue (NO fallback allowed)
    if "red" in palette:
        h = 0.0
    elif "orange" in palette:
        h = 0.08
    elif "yellow" in palette:
        h = 0.13
    elif "green" in palette:
        h = 0.33
    elif "blue" in palette:
        h = 0.6
    elif "purple" in palette:
        h = 0.78
    else:
        raise ValueError(f"Unsupported palette: {palette}")   # ❗ STRICT

    # 🎯 Step 3: Fixed saturation
    s = 0.7

    # 🎯 Step 4: Light → Dark gradient
    min_l = 0.80   # light
    max_l = 0.50   # dark

    if reverse:
        min_l, max_l = max_l, min_l

    def to_hex(h, l, s):
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        return f'#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}'

    return {
        "basedOn": based_on,
        "minColor": to_hex(h, min_l, s),
        "maxColor": to_hex(h, max_l, s),
        "format_style": "gradient"
    }

# --------------------------------------------------
# Main Function
# --------------------------------------------------
def build_sheet_visuals_unsafe(
    payload: dict,
    tables: list = None,
    custom_sql: list = None,
    project_id: str | None = None,
    workbook_id: str | None = None,
    run_id: str | None = None,
    auth_header: str | None = None,
    global_sets: list = None
) -> list[dict]:

    """
    Converts Tableau sheet visuals into Power BI instructions.
    Uses metadata (tables, custom_sql) to resolve field names accurately.
    """

    sheets = []

    if isinstance(payload, dict):
        if "worksheets" in payload and isinstance(payload["worksheets"], list):
            sheets = payload["worksheets"]
        elif "sheets" in payload:
            s_val = payload["sheets"]
            sheets = s_val.get("sheets", []) if isinstance(s_val, dict) else (s_val if isinstance(s_val, list) else [])
        elif "visuals" in payload and isinstance(payload["visuals"], dict):
            sheets = payload["visuals"].get("sheet_visuals", [])
        elif "sheets_and_visuals" in payload:
            sheets = payload["sheets_and_visuals"].get("sheets", [])
    elif isinstance(payload, list):
        sheets = payload


    for sheet in sheets:
        # Resolve all primary fields - Keep raw parsing info at top level
        for shelf in ["rows", "columns", "marks_color", "marks_text", "marks_detail", "marks_size", "marks_angle"]:
            items = sheet.get(shelf, [])
            resolved_items = []
            for item in items:
                # Removal of "None" strings and preserve original dicts/strings
                v = item.get("field") if isinstance(item, dict) else item
                # if v and v != "None":
                if v and isinstance(v, str) and v != "None":
                    resolved_items.append(item)
            sheet[shelf] = resolved_items

        # Apply BI formatting logic to all shelves that can carry format info
        for _shelf in ["rows", "columns", "marks_text", "marks_detail", "marks_size", "marks_color", "marks_angle"]:
            sheet[_shelf] = apply_bi_format(sheet.get(_shelf, []))

        # Map filters
        original_filters = sheet.get("filters", [])
        filtered_filters = []

        for f in original_filters:
            # Extract raw name
            if isinstance(f, dict):
                raw_name = (
                    f.get("filter_name") or
                    f.get("name") or
                    f.get("field") or ""
                )
            else:
                raw_name = str(f)

            clean_name = raw_name.strip() if isinstance(raw_name, str) else ""

            # ❌ Skip unwanted filters
            if clean_name.lower().startswith("action ("):
                continue

            if clean_name.lower() == "measure names":
                continue


            # ───────────────────────────────
            # ✅ Normal filters
            # ───────────────────────────────
            filtered_filters.append(map_tableau_filter_to_power_bi(f))

        # Final assignment
        sheet["filters"] = filtered_filters

        # Sync Axis metadata (field_id) - Keep as is to preserve raw parsing context
        sheet.get("visual_properties", {})
        # Note: axes field_id is typically already raw from parser, skipping resolution to maintain context

        # Build visual type info
        visual_info = infer_power_bi_visual_type(sheet, payload)
        # ─── Inject axes_title into power_bi_visual_type ──────────────
        VISUALS_WITHOUT_AXES = {
            "Pie Chart", "Donut Chart", "Card", "KPI", "Gauge",
            "Table", "Matrix", "Funnel Chart", "Treemap"
        }

        visual_type = visual_info.get("power_bi_visual_type") if isinstance(visual_info, dict) else None

        if isinstance(visual_info, dict) and visual_type and visual_type not in VISUALS_WITHOUT_AXES:
            axes = sheet.get("visual_properties", {}).get("axes", [])
            axes_title = []
            axis_role_map = {}

            # ───────────────────────────────
            # ✅ BUILD ROLE MAP (UNIVERSAL)
            # ───────────────────────────────

            # Case 1: Standard visuals (bar, line, scatter etc.)
            if isinstance(visual_info, dict):

                # x_axis
                x = visual_info.get("x_axis")
                if isinstance(x, dict):
                    x = x.get("field")
                if not x:
                    raw_cols = sheet.get("columns", [])
                    first_col = raw_cols[0] if raw_cols else None
                    x = first_col.get("field") if isinstance(first_col, dict) else first_col
                x_resolved = resolve_visual_field(x) if x else None
                if x_resolved and isinstance(x_resolved, str):
                    axis_role_map[x_resolved] = "x_axis"

                # y_axis
                y = visual_info.get("y_axis")
                if isinstance(y, dict):
                    y = y.get("field")
                if not y:
                    raw_rows = sheet.get("rows", [])
                    first_row = raw_rows[0] if raw_rows else None
                    y = first_row.get("field") if isinstance(first_row, dict) else first_row
                y_resolved = resolve_visual_field(y) if y else None
                if y_resolved and isinstance(y_resolved, str):
                    axis_role_map[y_resolved] = "y_axis"

                # secondary y
                sec = visual_info.get("secondary_y_axis")
                sec_resolved = resolve_visual_field(sec) if sec else None
                if sec_resolved and isinstance(sec_resolved, str):
                    axis_role_map[sec_resolved] = "secondary_y_axis"

                # size (scatter)
                size = visual_info.get("size")
                size_resolved = resolve_visual_field(size) if size else None
                if size_resolved and isinstance(size_resolved, str):
                    axis_role_map[size_resolved] = "size"

            # Case 2: axis_mapping (area, pie, combo etc.)
            axis_mapping = visual_info.get("axis_mapping", {}) if isinstance(visual_info, dict) else {}

            for role, field in axis_mapping.items():
                if isinstance(field, list):
                    for f in field:
                        axis_role_map[resolve_visual_field(f)] = role
                elif field:
                    axis_role_map[resolve_visual_field(field)] = role


            # ───────────────────────────────
            # ✅ BUILD AXES TITLE
            # ───────────────────────────────

            for ax in axes:
                axis_title_block = ax.get("axis_title", {})
                custom = axis_title_block.get("custom", False)
                field_value = ax.get("field", "")

                # ✅ If Measure Values → EMPTY TEXT
                if field_value == "Measure Values":
                    text = ""
                else:
                    if custom:
                        text = axis_title_block.get("text", "")
                    else:
                        text = extract_measure_from_function(field_value)
                axis_field = resolve_visual_field(ax.get("field"))
                # 🔥 CORE LOGIC
                target = axis_role_map.get(axis_field)
                axes_title.append({
                    "custom": custom,
                    "text":   text,
                    "target": target
                })
            visual_info["axes_title"] = axes_title
        marks_color = sheet.get("marks_color", [])

        all_fields = sheet.get("rows", []) + sheet.get("columns", [])
        has_column_width = any(isinstance(f, dict) and f.get("column_width") for f in all_fields)

        VISUALS_WITH_COLUMN_HEADERS = {"Matrix", "Table"}
        VISUALS_WITHOUT_DATA_LABELS = {"Card", "Table", "Matrix", "Scatter Chart"}
        VISUALS_WITH_CUSTOM_DATA_LABELS = {"Pie Chart", "Line Chart"}
        mark_labels_enabled = sheet.get("mark_labels_enabled", False)

        if isinstance(visual_info, dict):
            if visual_info.get("power_bi_visual_type") == "Matrix":
                # Only replace Custom SQL; aggregation mapping handled by resolve_visual_field
                local_marks_color = replace_custom_sql_fields(marks_color, payload or {})
                unwrapped_marks_color = [
                    resolve_visual_field(m.get("field") if isinstance(m, dict) else m)
                    for m in local_marks_color
                ]
                # Deduplicate: only append marks_color fields not already present in rows
                # (e.g. "Type" used on both rows shelf and marks_color must not appear twice)
                existing_rows = visual_info.get("rows", [])
                existing_row_names = {
                    (r.get("field") if isinstance(r, dict) else r or "").lower()
                    for r in existing_rows
                }
                new_color_rows = [
                    f for f in unwrapped_marks_color
                    if (f or "").lower() not in existing_row_names
                ]
                visual_info["rows"] = existing_rows + new_color_rows

            VISUALS_WITH_LEGENDS = {"Bar Chart", "Stacked Bar Chart", "Clustered Bar Chart", "Stacked Column Chart", "Line Chart", "Area Chart", "Line and Clustered Column Chart", "Scatter Chart", "Pie Chart"}
            if visual_info.get("power_bi_visual_type") in VISUALS_WITH_LEGENDS:
                if "legends" not in visual_info:
                    visual_info["legends"] = []
            # ✅ LEGEND POSITION (only if legend exists)
            if visual_info.get("legends"):
                visual_info["legend_position"] = "top right stacked"
            # ← Only Matrix and Table
            if has_column_width and visual_info.get("power_bi_visual_type") in VISUALS_WITH_COLUMN_HEADERS:
                visual_info["column_headers_auto_size"] = "enabled"

            visual_type = visual_info.get("power_bi_visual_type")

            if visual_type not in VISUALS_WITHOUT_DATA_LABELS and \
            visual_type not in VISUALS_WITH_CUSTOM_DATA_LABELS:
                # ❗ Skip stacked charts (they use total_labels)
                if visual_type not in ["Stacked Bar Chart", "Stacked Column Chart"]:

                    # ✅ Do NOT override if already created (like clustered charts)
                    if "data_labels" not in visual_info:
                        visual_info["data_labels"] = {
                            "show": "on" if mark_labels_enabled else "off"
                        }

            sheet["power_bi_visual_type"] = visual_info
        else:
            pbi_type_obj = {"power_bi_visual_type": visual_info}
            if visual_info == "Table": pbi_type_obj["columns"] = build_table_field_mapping(sheet, payload)

            if visual_info not in VISUALS_WITHOUT_DATA_LABELS and \
               visual_info not in VISUALS_WITH_CUSTOM_DATA_LABELS:
                    pbi_type_obj["data_labels"] = "on" if mark_labels_enabled else "off"

             # ← Only Matrix and Table
            if has_column_width and visual_info in VISUALS_WITH_COLUMN_HEADERS:
                pbi_type_obj["column_headers_auto_size"] = "enabled"
            sheet["power_bi_visual_type"] = pbi_type_obj


        # Build slicers
        slicers = []
        parameters = sheet.get("parameters", [])
        sets = sheet.get("sets", [])

        for p in parameters:
            p_val = p.get("name", "") if isinstance(p, dict) else p
            if isinstance(p_val, str) and p_val.strip().startswith("Action"):
                continue

            clean_name = extract_clean_filter_name(p)

            slicers.append({
                "name": clean_name,
                "source": "parameter",
                "instruction": f"Create a slicer using the {clean_name}"
            })

        for s in sets:
            s_val = s.get("name", "") if isinstance(s, dict) else s
            if isinstance(s_val, str) and s_val.strip().startswith("Action"):
                continue

            clean_name = extract_clean_filter_name(s_val)

            mapped_name = clean_name
            local_global_sets = global_sets if global_sets is not None else (payload.get("sets", []) if isinstance(payload, dict) else [])
            for gs in local_global_sets:
                if gs.get("name") == clean_name:
                    pbi = gs.get("powerbi", {})
                    # For dynamic sets
                    calc_cols = pbi.get("calculated_columns", [])
                    for cc in calc_cols:
                        cname = cc.get("name", "")
                        if cname.endswith("_In_Out") or cname.endswith("_Category"):
                            mapped_name = cname
                            break
                    # For static sets
                    if mapped_name == clean_name and "dax" in pbi and "=" in pbi["dax"]:
                        possible_name = pbi["dax"].split("=")[0].strip()
                        if possible_name:
                            mapped_name = possible_name

            if isinstance(s, dict):
                slicer_name_obj = copy.deepcopy(s)
                slicer_name_obj["name"] = mapped_name
            else:
                slicer_name_obj = mapped_name

            slicers.append({
                "name": slicer_name_obj,
                "source": "set",
                "instruction": f"Create a slicer using {slicer_name_obj}"
            })

        sheet["slicers"] = slicers

    return sheets

def build_sheet_visuals(
    payload: dict,
    tables: list = None,
    custom_sql: list = None,
    project_id: str | None = None,
    workbook_id: str | None = None,
    run_id: str | None = None,
    auth_header: str | None = None,
    global_sets: list = None
) -> list[dict]:
    try:
        return build_sheet_visuals_unsafe(
            payload, tables, custom_sql, project_id, workbook_id, run_id, auth_header, global_sets
        )
    except Exception as e:
        store_error(
            project_id=project_id,
            workbook_id=workbook_id,
            run_id=run_id,
            error_msg="Critical failure in build_sheet_visuals.",
            technical_details=str(e),
            auth_header=auth_header
        )
        return []
