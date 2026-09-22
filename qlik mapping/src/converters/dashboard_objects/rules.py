"""LLM instructions and rules for the dashboard_objects converter."""

from services.visual_mapper import VisualMapper
from src.converters.custom_objects.converter import KNOWN_EXTENSION_MAPPINGS

# Qlik object_category -> Fabric visual type, for categories VisualMapper
# doesn't cover by chart-type name alone (kpi/button/container/etc).
CATEGORY_TO_VISUAL_TYPE = {
    "kpi": "card",
    "button": "actionButton",
    "action_button": "actionButton",
    "image": "image",
    "date_picker": "dateSlicer",
    "navigation_menu": "pageNavigator",
    "filter_pane": "slicer",
    "container": "group",
    "table": "tableEx",
    "pivot_table": "pivotTable",
    "bar_chart": "barChart",
    "line_chart": "lineChart",
    "combo_chart": "comboChart",
    "pie_chart": "pieChart",
    "scatter_plot": "scatterChart",
    "gauge": "gauge",
    "map": "map",
    "treemap": "treemap",
}

_visual_mapper = VisualMapper()

# The full set of Fabric visual type names this codebase actually knows how
# to produce. Shared between the LLM's output schema (as a steering `enum`)
# and the converter's post-call validation, so an LLM guess outside this set
# is never trusted as-is.
VALID_VISUAL_TYPES = sorted(
    set(_visual_mapper.TYPE_MAPPINGS.values())
    | set(CATEGORY_TO_VISUAL_TYPE.values())
    | {mapping["visual_type"] for mapping in KNOWN_EXTENSION_MAPPINGS.values()}
)

RULES = [
    {
        "type": "dashboard_objects",
        "id": "dash1",
        "rule": "Translate Qlik Sense dashboard object layout and pixel coordinates into Power BI report canvas "
                "placement. Preserve exact 1080p (and 720p) bounds (x, y, width, height) and grid alignments.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash2",
        "rule": "Preserve visual headers, card background fills, custom borders, and typography styling.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash3",
        "rule": "If a visual type has no Power BI equivalent, mark supported=false, status='unmapped', and provide "
                "a replacement_strategy describing how to rebuild it by hand.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash4",
        "priority": "critical",
        "rule": "ROLE ASSIGNMENT - Qlik hypercube dimensions take the categorical role and measures take the value "
                "role. Use the bucket the target visual actually has: Category/Y for bar, column, line, area, "
                "waterfall and funnel charts; Category/Y for pie and donut; Rows/Values for a matrix (pivotTable); "
                "Group/Values for a treemap; Values only for card, multiRowCard and slicer; Category/Size for a map.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash5",
        "priority": "critical",
        "rule": "COMBO CHART SPLIT - For lineClusteredColumnComboChart the FIRST measure belongs on Y (the columns) "
                "and every remaining measure on Y2 (the lines). Never place all measures on Y.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash6",
        "priority": "critical",
        "rule": "SCATTER AXES - For a scatterChart the first measure is the X axis, the second is the Y axis, and "
                "the third (when present) is Size. Dimensions become the Category/Details grouping. With only one "
                "measure, place it on Y and leave X empty.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash7",
        "rule": "SECOND DIMENSION IS THE SERIES - When a chart carries two or more dimensions, the first is the "
                "axis/category and the second is the legend (series) grouping. Do not stack both onto the axis.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash8",
        "rule": "AGGREGATION - A field that already resolves to a DAX measure must use aggregation 'None'; it is "
                "already aggregated. A raw numeric column used as a value needs an explicit aggregation: Sum for "
                "additive amounts, Avg for rates/ratios/percentages/scores, Count or DistinctCount for identifiers "
                "and codes. Never aggregate a column used as a category.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash9",
        "priority": "critical",
        "rule": "FIELD BINDING IS GROUNDED - Bind every field to an entity/property that exists in the supplied "
                "SCHEMA. Prefer an exact name match, then a case-insensitive match, then the base field of a Qlik "
                "expression (Sum(Revenue) -> Revenue). If nothing matches, return the field with entity null so it "
                "is reported as unbound. Never invent a table or column name.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash10",
        "priority": "critical",
        "rule": "COLOURS ARE PRESERVED, NEVER INVENTED - Carry through single_color, resolved_palette_color, the "
                "theme palette and any per-measure conditional colour exactly as supplied. When color.is_multicolor "
                "is true the visual is coloured by dimension, so emit a palette rather than a single fill. If no "
                "colour is supplied, return null and let the report theme decide - do not fabricate a hex value.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash11",
        "rule": "KPI STYLING - For a card, map kpi_styling.value_color/value_font_size/value_font_family onto the "
                "value label and label_color/label_font_size/label_font_family onto the category label. A Qlik KPI "
                "with a conditional colour expression becomes a conditional formatting rule, not a static fill.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash12",
        "rule": "DONUT DETECTION - A Qlik pie chart with a non-zero inner_radius is a donutChart in Power BI, not a "
                "pieChart. Preserve stroke_color as the slice border.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash13",
        "priority": "critical",
        "rule": "BUTTONS AND IMAGES CARRY THEIR TARGET - An action button must keep its navigation target "
                "(bookmark, sheet/page or URL) and its label text; an image visual must keep its image_url. A button "
                "with no resolvable target is still emitted, but flagged for review rather than left silently inert.",
    },
    {
        "type": "dashboard_objects",
        "id": "dash14",
        "rule": "LEGEND AND AXIS TITLES - Preserve legend visibility and dock position from formatting.legend. Keep "
                "axis titles when the source supplies them; never add titles the source did not have.",
    },
]

SYSTEM_PROMPT = """
You convert Qlik Sense dashboard objects into Power BI / Fabric report visual layout definitions.

Preserve canvas coordinates, header styling, card styling, and map visual types to Fabric.
Assign every dimension and measure an explicit role, and bind each one to a table/column that
exists in the supplied schema. Preserve colours exactly as given; never invent a hex value.
Always include a `confidence` field between 0.0 and 1.0 and a short `rationale`.
""".strip()

OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["visual_type", "supported", "confidence"],
    "properties": {
        "visual_type": {
            "type": "string",
            "enum": VALID_VISUAL_TYPES,
            "description": "Must be one of the listed Fabric/Power BI visual type names.",
        },
        "supported": {"type": "boolean"},
        "status": {"type": "string", "enum": ["mapped", "unmapped", "partial"]},
        "title": {"type": "string"},
        "object_category": {"type": "string"},
        "sheet_name": {"type": "string"},
        "replacement_strategy": {
            "type": "string",
            "description": "How to recreate this visual by hand when supported is false.",
        },
        "position": {
            "type": "object",
            "properties": {
                "x": {"type": "number"}, "y": {"type": "number"},
                "width": {"type": "number"}, "height": {"type": "number"},
            },
        },
        # ── field bindings ────────────────────────────────────────────────
        # The single most important addition. Previously the model only ever
        # returned a visual type, so which field landed on which axis was
        # decided downstream in the generation agent by a static ROLES table
        # plus difflib fuzzy matching against column names.
        "field_roles": {
            "type": "array",
            "description": "One entry per dimension/measure on the visual.",
            "items": {
                "type": "object",
                "required": ["field", "role"],
                "properties": {
                    "field": {
                        "type": "string",
                        "description": "The field name exactly as it appears on the Qlik visual.",
                    },
                    "role": {
                        "type": "string",
                        "enum": [
                            "Category", "Series", "Y", "Y2", "X", "Size",
                            "Values", "Rows", "Columns", "Group", "Details",
                            "Tooltips", "Unbound",
                        ],
                    },
                    "entity": {
                        "type": ["string", "null"],
                        "description": "Table name from the SCHEMA, or null when unresolvable.",
                    },
                    "property": {
                        "type": ["string", "null"],
                        "description": "Column or measure name from the SCHEMA.",
                    },
                    "is_measure": {"type": "boolean"},
                    "aggregation": {
                        "type": "string",
                        "enum": ["Sum", "Avg", "Count", "DistinctCount", "Min", "Max", "None"],
                    },
                },
            },
        },
        # ── colour and formatting ─────────────────────────────────────────
        "colors": {
            "type": "object",
            "properties": {
                "single_color": {"type": ["string", "null"]},
                "palette": {"type": "array", "items": {"type": "string"}},
                "is_multicolor": {"type": "boolean"},
                "background_color": {"type": ["string", "null"]},
                "border_color": {"type": ["string", "null"]},
                "value_color": {"type": ["string", "null"]},
                "label_color": {"type": ["string", "null"]},
                "conditional_rules": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "expression": {"type": "string"},
                            "color": {"type": "string"},
                            "operator": {"type": "string"},
                            "value": {"type": ["string", "number", "null"]},
                        },
                    },
                },
            },
        },
        "formatting": {
            "type": "object",
            "properties": {
                "legend_show": {"type": "boolean"},
                "legend_position": {
                    "type": "string",
                    "enum": ["Top", "Bottom", "Left", "Right", "Auto"],
                },
                "data_labels_show": {"type": "boolean"},
                "x_axis_title": {"type": ["string", "null"]},
                "y_axis_title": {"type": ["string", "null"]},
                "number_format": {"type": ["string", "null"]},
            },
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "rationale": {"type": "string"},
    },
}

EXAMPLES = [
    {
        "qlik_dashboard_object": {
            "title": "Net Margin KPI",
            "object_category": "kpi",
            "chart_type": "kpi",
            "sheet_name": "Executive Overview",
            "measures": [{"name": "Net Margin"}],
            "layout": {"pixel_bounds_1080p": {"left": 100, "top": 50, "width": 350, "height": 180}},
        },
        "output": {
            "visual_type": "card",
            "supported": True,
            "status": "mapped",
            "title": "Net Margin KPI",
            "object_category": "kpi",
            "sheet_name": "Executive Overview",
            "position": {"x": 100, "y": 50, "width": 350, "height": 180},
            "field_roles": [
                {
                    "field": "Net Margin",
                    "role": "Values",
                    "entity": "Sales",
                    "property": "Net Margin",
                    "is_measure": True,
                    "aggregation": "None",
                }
            ],
            "confidence": 0.95,
            "rationale": "Direct equivalent; mapped Qlik KPI to Power BI canvas card visual.",
        },
    },
    {
        "qlik_dashboard_object": {
            "title": "Revenue and Margin by Month",
            "chart_type": "combochart",
            "sheet_name": "Trends",
            "dimensions": [{"name": "Month"}],
            "measures": [{"name": "Revenue"}, {"name": "Margin %"}],
        },
        "output": {
            "visual_type": "lineClusteredColumnComboChart",
            "supported": True,
            "status": "mapped",
            "title": "Revenue and Margin by Month",
            "field_roles": [
                {"field": "Month", "role": "Category", "entity": "Calendar",
                 "property": "Month", "is_measure": False, "aggregation": "None"},
                {"field": "Revenue", "role": "Y", "entity": "Sales",
                 "property": "Revenue", "is_measure": True, "aggregation": "None"},
                {"field": "Margin %", "role": "Y2", "entity": "Sales",
                 "property": "Margin %", "is_measure": True, "aggregation": "None"},
            ],
            "confidence": 0.93,
            "rationale": "Combo chart: first measure on the column axis, second on the line axis.",
        },
    },
]

VALIDATORS = []
