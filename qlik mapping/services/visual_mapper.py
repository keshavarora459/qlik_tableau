import logging
import uuid
import re
from typing import Dict, List, Any
from .confidence_evaluator import ConfidenceEvaluator

logger = logging.getLogger(__name__)


def as_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


class VisualMapper:
    """Maps Qlik visual objects into Fabric Power BI visual items and sheets structure."""

    VISUALIZATION_TYPE_MAPPING = {
        "kpi": {"bi_type": "Card", "bi_name_prefix": "Card_", "fabric_type": "card"},
        "barchart": {"bi_type": "Clustered Bar Chart", "bi_name_prefix": "BarChart_", "fabric_type": "barChart"},
        "combochart": {"bi_type": "Line and Clustered Column Chart", "bi_name_prefix": "ComboChart_", "fabric_type": "lineClusteredColumnComboChart"},
        "linechart": {"bi_type": "Line Chart", "bi_name_prefix": "LineChart_", "fabric_type": "lineChart"},
        "piechart": {"bi_type": "Pie Chart", "bi_name_prefix": "PieChart_", "fabric_type": "pieChart"},
        "donutchart": {"bi_type": "Donut Chart", "bi_name_prefix": "DonutChart_", "fabric_type": "donutChart"},
        "table": {"bi_type": "Table", "bi_name_prefix": "Table_", "fabric_type": "tableEx"},
        "filterpane": {"bi_type": "Slicer", "bi_name_prefix": "Slicer_", "fabric_type": "slicer"},
        "scatterplot": {"bi_type": "Scatter Chart", "bi_name_prefix": "ScatterChart_", "fabric_type": "scatterChart"},
        "areachart": {"bi_type": "Filled Area Chart", "bi_name_prefix": "AreaChart_", "fabric_type": "areaChart"},
        "treemap": {"bi_type": "Treemap", "bi_name_prefix": "Treemap_", "fabric_type": "treemap"},
        "pivottable": {"bi_type": "Matrix", "bi_name_prefix": "Matrix_", "fabric_type": "pivotTable"},
        "pivot table": {"bi_type": "Matrix", "bi_name_prefix": "Matrix_", "fabric_type": "pivotTable"},
        "pivot-table": {"bi_type": "Matrix", "bi_name_prefix": "Matrix_", "fabric_type": "pivotTable"},
        "gauge": {"bi_type": "Gauge", "bi_name_prefix": "Gauge_", "fabric_type": "gauge"},
        "map": {"bi_type": "Map", "bi_name_prefix": "Map_", "fabric_type": "map"},
        "boxplot": {"bi_type": "Box and Whisker Chart", "bi_name_prefix": "BoxWhisker_", "fabric_type": "boxPlot"},
        "histogram": {"bi_type": "Histogram", "bi_name_prefix": "Histogram_", "fabric_type": "columnChart"},
        "waterfall": {"bi_type": "Waterfall Chart", "bi_name_prefix": "WaterfallChart_", "fabric_type": "waterfallChart"},
        "waterfallchart": {"bi_type": "Waterfall Chart", "bi_name_prefix": "WaterfallChart_", "fabric_type": "waterfallChart"},
        "funnelchart": {"bi_type": "Funnel Chart", "bi_name_prefix": "FunnelChart_", "fabric_type": "funnel"},
        "funnel": {"bi_type": "Funnel Chart", "bi_name_prefix": "FunnelChart_", "fabric_type": "funnel"},
        "distributionplot": {"bi_type": "Scatter Chart", "bi_name_prefix": "Distribution_", "fabric_type": "scatterChart"},
        "sankey": {"bi_type": "Sankey Diagram", "bi_name_prefix": "Sankey_", "fabric_type": "sankeyDiagram"},
        "sankeychart": {"bi_type": "Sankey Diagram", "bi_name_prefix": "Sankey_", "fabric_type": "sankeyDiagram"},
        "string-image": {"bi_type": "Input Box", "bi_name_prefix": "InputBox_", "fabric_type": "textbox"},
        "variableinput": {"bi_type": "Variable Slicer", "bi_name_prefix": "VarSlicer_", "fabric_type": "slicer"},
        "textimage": {"bi_type": "Text Box / Image", "bi_name_prefix": "TextBox_", "fabric_type": "textbox"},
        "image": {"bi_type": "Image", "bi_name_prefix": "Image_", "fabric_type": "image"},
        "auto-chart": {"bi_type": "Unknown", "bi_name_prefix": "Vis_", "fabric_type": "tableEx"},
        "listbox": {"bi_type": "List Box / Slicer", "bi_name_prefix": "Slicer_", "fabric_type": "slicer"},
        "container": {"bi_type": "Container / Group", "bi_name_prefix": "Group_", "fabric_type": "group"},
        "button": {"bi_type": "Action Button", "bi_name_prefix": "Button_", "fabric_type": "actionButton"},
        # A Qlik bookmark surfaced as an object on a sheet (not the app-level
        # bookmark list) becomes an action button that navigates/applies a
        # Power BI bookmark - Power BI has no standalone "bookmark" visual.
        "bookmarkobject": {"bi_type": "Bookmark Button", "bi_name_prefix": "Bookmark_", "fabric_type": "actionButton"},
        # Qlik has no native "packed bubble" chart either - "pack-bubble" is
        # itself a Qlik extension object. The closest native Fabric/Power BI
        # equivalent is a scatter chart driven by a size field.
        "bubblechart": {"bi_type": "Bubble Chart", "bi_name_prefix": "BubbleChart_", "fabric_type": "scatterChart"},
        "mekkochart": {"bi_type": "Mekko Chart", "bi_name_prefix": "Mekko_", "fabric_type": "treemap"},

        # --- Types from Qlik's official object registry that previously fell
        # through to the generic Table visual. Audited against qlik.dev's
        # visualization list: 21 of 59 documented object types (classic,
        # nebula "sn-" and dashboard/visualization bundle) had no entry here.
        "bulletchart": {"bi_type": "Bullet Chart", "bi_name_prefix": "Bullet_", "fabric_type": "gauge"},
        "datepicker": {"bi_type": "Date Slicer", "bi_name_prefix": "DateSlicer_", "fabric_type": "dateSlicer"},
        "navigationmenu": {"bi_type": "Page Navigator", "bi_name_prefix": "Nav_", "fabric_type": "pageNavigator"},
        "multikpi": {"bi_type": "Multi Row Card", "bi_name_prefix": "MultiKPI_", "fabric_type": "multiRowCard"},
        "gridchart": {"bi_type": "Matrix (heat map)", "bi_name_prefix": "Grid_", "fabric_type": "pivotTable"},
        "smartpivot": {"bi_type": "Matrix", "bi_name_prefix": "Matrix_", "fabric_type": "pivotTable"},
        "orgchart": {"bi_type": "Organization Chart", "bi_name_prefix": "OrgChart_", "fabric_type": "tableEx"},
        "networkchart": {"bi_type": "Network Chart", "bi_name_prefix": "Network_", "fabric_type": "tableEx"},
        "radarchart": {"bi_type": "Radar Chart", "bi_name_prefix": "Radar_", "fabric_type": "lineChart"},
        "wordcloud": {"bi_type": "Word Cloud", "bi_name_prefix": "WordCloud_", "fabric_type": "treemap"},
        "nlgchart": {"bi_type": "Narrative / Smart Narrative", "bi_name_prefix": "Narrative_", "fabric_type": "textbox"},
        "layoutcontainer": {"bi_type": "Container / Group", "bi_name_prefix": "Group_", "fabric_type": "group"},
        "trelliscontainer": {"bi_type": "Small Multiples Container", "bi_name_prefix": "Trellis_", "fabric_type": "group"},
        "variancewaterfall": {"bi_type": "Waterfall Chart", "bi_name_prefix": "WaterfallChart_", "fabric_type": "waterfallChart"},
        "shape": {"bi_type": "Shape", "bi_name_prefix": "Shape_", "fabric_type": "shape"},
        "videoplayer": {"bi_type": "Video (unsupported)", "bi_name_prefix": "Video_", "fabric_type": "textbox"},
        "animator": {"bi_type": "Animator (unsupported)", "bi_name_prefix": "Animator_", "fabric_type": "textbox"},
        # A Qlik write-back table has no Power BI equivalent: Power BI is
        # read-only over its model, so the data can be shown but not edited.
        "writetable": {"bi_type": "Table (write-back not supported)", "bi_name_prefix": "Table_", "fabric_type": "tableEx"},

        "default": {"bi_type": "Unsupported", "bi_name_prefix": "Unknown_", "fabric_type": "tableEx"}
    }

    # Fabric/Power BI visual types this codebase can emit that are NOT part of
    # the stock/native visual set - they only render correctly if the target
    # report registers the matching AppSource custom visual package. Kept
    # separate from VISUALIZATION_TYPE_MAPPING (which must stay a plain
    # type->type table) so downstream consumers (report generation) can
    # decide whether to substitute a native chart or actually perform that
    # registration, instead of silently emitting a visual type the report
    # doesn't carry the resources for.
    CUSTOM_VISUAL_TYPES = {"boxPlot", "sankeyDiagram"}


    TYPE_MAPPINGS = {k: v["fabric_type"] for k, v in VISUALIZATION_TYPE_MAPPING.items()}

    def __init__(self):
        self.confidence_eval = ConfidenceEvaluator()

    def normalize_qlik_type(self, raw_type: str) -> str:
        if not raw_type:
            return "default"
        t = str(raw_type).lower().strip()
        # Handle extensions like qlik-funnel-chart-ext, qlik-treemap-ext
        if t.startswith("qlik-"):
            t = t[5:]
        elif t.startswith("qlik_"):
            t = t[5:]
        # Modern Qlik Cloud/Sense native ("nebula") object types are prefixed
        # "sn-" (e.g. sn-barchart, sn-combochart, sn-kpi, sn-pivot-table).
        # Without stripping this, every native chart type misses every alias
        # check below and silently collapses to the generic "default" ->
        # tableEx bucket, which is why real bar/line/combo/kpi charts were
        # rendering as a single giant Table visual instead of their real type.
        if t.startswith("sn-"):
            t = t[3:]
        elif t.startswith("sn_"):
            t = t[3:]
        # Only strip a *separated* "-ext"/"_ext" suffix. The bare
        # `endswith("ext")` this replaces truncated any type ending in those
        # three letters: "sn-text" -> "text" -> "t", so Qlik's Text bundle
        # object silently fell through to the generic Table visual.
        if t.endswith("-ext") or t.endswith("_ext"):
            t = t[:-4]

        # Clean spaces, hyphens, underscores
        cleaned = re.sub(r"[\s\-_]+", "", t)

        # Common aliases
        if cleaned in ["pivot", "pivottable", "matrix"]:
            return "pivottable"
        if cleaned in ["scatter", "scatterplot", "scatterchart"]:
            return "scatterplot"
        if cleaned in ["bar", "barchart", "column", "columnchart"]:
            return "barchart"
        if cleaned in ["combo", "combochart"]:
            return "combochart"
        if cleaned in ["line", "linechart"]:
            return "linechart"
        if cleaned in ["pie", "piechart"]:
            return "piechart"
        if cleaned in ["donut", "donutchart"]:
            return "donutchart"
        if cleaned in ["area", "areachart"]:
            return "areachart"
        if cleaned in ["tree", "treemap"]:
            return "treemap"
        if cleaned in ["box", "boxplot"]:
            return "boxplot"
        if cleaned in ["waterfall", "waterfallchart"]:
            return "waterfall"
        if cleaned in ["funnel", "funnelchart"]:
            return "funnelchart"
        if cleaned in ["filter", "filterpane", "slicer"]:
            return "filterpane"
        if cleaned in ["card", "kpi"]:
            return "kpi"
        if cleaned == "stringimage":
            return "string-image"
        if cleaned in ["variableinput", "variable"]:
            return "variableinput"
        if cleaned in ["text", "textimage"]:
            return "textimage"
        if cleaned == "image":
            return "image"
        if cleaned in ["table", "straighttable"]:
            return "table"
        if cleaned in ["auto", "autochart"]:
            return "auto-chart"
        if cleaned in ["distribution", "distributionplot"]:
            return "distributionplot"
        if cleaned in ["sankey", "sankeychart"]:
            return "sankey"
        if cleaned in ["packbubble", "packedbubble", "bubble", "bubblechart"]:
            return "bubblechart"
        if cleaned in ["mekko", "mekkochart", "marimekko", "marimekkochart"]:
            return "mekkochart"
        if cleaned in ["listbox", "list"]:
            return "listbox"
        if cleaned in ["container", "tabbedcontainer", "tabcontainer"]:
            return "container"
        if cleaned in ["button", "actionbutton"]:
            return "button"
        if cleaned in ["bookmark", "bookmarkobject", "bookmarkbutton"]:
            return "bookmarkobject"
        # Aliases for the object types added above. Qlik spells the same
        # chart several ways across the classic, nebula and bundle
        # registries (bulletchart / sn-bullet-chart, distributionplot /
        # sn-distplot), so each has to resolve to one normalized key.
        if cleaned in ["bullet", "bulletchart"]:
            return "bulletchart"
        if cleaned in ["distplot", "distributionplot", "distribution"]:
            return "distributionplot"
        if cleaned in ["datepicker", "date"]:
            return "datepicker"
        if cleaned in ["navigationmenu", "sheetnavigation", "navigation"]:
            return "navigationmenu"
        if cleaned in ["multikpi", "multiplekpi"]:
            return "multikpi"
        if cleaned in ["gridchart", "grid", "heatmap"]:
            return "gridchart"
        if cleaned in ["smartpivot", "pivotplus"]:
            return "smartpivot"
        if cleaned in ["orgchart", "organizationchart"]:
            return "orgchart"
        if cleaned in ["networkchart", "network"]:
            return "networkchart"
        if cleaned in ["radarchart", "radar", "spider", "spiderchart"]:
            return "radarchart"
        if cleaned in ["wordcloud", "wordcloudchart"]:
            return "wordcloud"
        if cleaned in ["nlgchart", "nlginsights", "narrative", "smartnarrative"]:
            return "nlgchart"
        if cleaned in ["layoutcontainer"]:
            return "layoutcontainer"
        if cleaned in ["trelliscontainer", "trellis"]:
            return "trelliscontainer"
        if cleaned in ["variancewaterfall"]:
            return "variancewaterfall"
        if cleaned in ["shape"]:
            return "shape"
        if cleaned in ["videoplayer", "video"]:
            return "videoplayer"
        if cleaned in ["animator"]:
            return "animator"
        if cleaned in ["writetable", "writebacktable"]:
            return "writetable"

        return cleaned if cleaned in self.VISUALIZATION_TYPE_MAPPING else "default"


    def get_mapping(self, qtype: str) -> Dict[str, Any]:
        normalized = self.normalize_qlik_type(qtype)
        entry = self.VISUALIZATION_TYPE_MAPPING.get(normalized)
        if entry is None:
            entry = self.VISUALIZATION_TYPE_MAPPING.get(str(qtype or "").lower().strip())
        if entry is None:
            entry = self.VISUALIZATION_TYPE_MAPPING["default"]
            # Falling back to the generic Table visual is a real quality loss
            # for whoever opens the report - log it with the exact raw type so
            # it's a traceable, fixable gap instead of a silent substitution.
            logger.warning(
                "No Fabric visual mapping for Qlik type %r (normalized to %r); "
                "falling back to a generic Table visual.", qtype, normalized,
            )
        result = dict(entry)
        result["requires_custom_visual"] = result["fabric_type"] in self.CUSTOM_VISUAL_TYPES
        return result

    # Minimum canvas heights per visual type (in pixels)
    MIN_HEIGHT_PX: Dict[str, int] = {
        "barChart": 300, "columnChart": 300, "clusteredBarChart": 300,
        "clusteredColumnChart": 300, "lineChart": 300, "lineClusteredColumnComboChart": 300,
        "pieChart": 280, "donutChart": 280, "treemap": 280, "scatterChart": 300,
        "waterfallChart": 300, "funnel": 260, "gauge": 200, "map": 300,
        "card": 120, "multiRowCard": 120, "slicer": 180, "tableEx": 200,
        "pivotTable": 200, "textbox": 80, "actionButton": 80,
    }

    def _infer_auto_chart_type(self, x_fields: List[str], y_fields: List[str]) -> str:
        """Pick a sensible Power BI visual type for Qlik 'auto-chart' objects
        based on how many dimension (X) and measure (Y) fields are present."""
        n_x = len(x_fields)
        n_y = len(y_fields)
        if n_y == 0 and n_x == 0:
            return "tableEx"
        if n_y >= 1 and n_x == 0:
            # Only measures → KPI / card
            return "card" if n_y == 1 else "tableEx"
        if n_y == 0 and n_x >= 1:
            # Only dimensions (list of values) → slicer/list
            return "slicer" if n_x == 1 else "tableEx"
        # Both x and y present
        if n_x == 1 and n_y == 1:
            return "barChart"
        if n_x == 1 and n_y == 2:
            return "lineClusteredColumnComboChart"
        if n_x >= 2 and n_y >= 1:
            return "scatterChart"
        return "clusteredColumnChart"

    def map_visuals_and_sheets(self, raw_visuals: List[Dict[str, Any]], raw_sheets: List[Dict[str, Any]], grid_columns: int = 24, grid_rows: int = 12) -> Dict[str, Any]:
        sheet_visuals = []
        sheet_map: Dict[str, List[Any]] = {}

        for idx, v in enumerate(raw_visuals):
            if not isinstance(v, dict):
                continue
            raw_qtype = v.get("qlik_type") or v.get("type") or "table"
            mapping = self.get_mapping(raw_qtype)
            ftype = mapping["fabric_type"]
            bi_type = mapping["bi_type"]
            prefix = mapping["bi_name_prefix"]

            # Collect axis fields early so we can infer auto-chart type
            x_fields = list(v.get("x_axis") or v.get("dimensions") or [])
            y_fields = list(v.get("y_axis") or v.get("measures") or [])
            z_fields = list(v.get("z_axis") or v.get("size") or [])  # scatter size

            # Smart auto-chart type inference
            normalized_qtype = self.normalize_qlik_type(raw_qtype)
            if normalized_qtype == "auto-chart":
                ftype = self._infer_auto_chart_type(x_fields, y_fields)
                bi_type = ftype

            sheet_title = v.get("sheet_name") or v.get("source") or "Main Sheet"

            col = int(v.get("col", 0))
            row = int(v.get("row", 0))
            colspan = int(v.get("colspan", 6))
            rowspan = int(v.get("rowspan", 4))

            # Fabric layout pixel math (1280px canvas, dynamic grid)
            col_width_px = 1280.0 / grid_columns
            row_height_px = 720.0 / grid_rows
            raw_h = int(rowspan * row_height_px)
            min_h = self.MIN_HEIGHT_PX.get(ftype, 120)
            final_h = max(raw_h, min_h)

            layout = {
                "col": col, "row": row, "colspan": colspan, "rowspan": rowspan,
                "x": int(col * col_width_px), "y": int(row * row_height_px),
                "width": max(int(colspan * col_width_px), 200),
                "height": final_h,
            }

            # Prefer an actual title carried on the visual itself; the sheet
            # name ("source") describes where the visual lives, not what it
            # is, so it must never be preferred over a real (even inferred)
            # title. Only fall back to it - and finally to the resolved
            # chart-type label rather than the generic word "Visual" - when
            # nothing else identifies this object.
            explicit_title = (as_dict(v.get("formatting")) or {}).get("title") or v.get("title")
            if explicit_title:
                v_title = explicit_title
                title_source = "explicit"
            else:
                measure_names = [m.get("name") if isinstance(m, dict) else str(m) for m in y_fields if m]
                dim_names = [d.get("name") if isinstance(d, dict) else str(d) for d in x_fields if d]
                label_parts = [n for n in (measure_names + dim_names) if n]
                if label_parts:
                    v_title = " by ".join(label_parts[:2])
                    title_source = "derived_from_fields"
                elif v.get("source"):
                    v_title = v["source"]
                    title_source = "sheet_name_fallback"
                else:
                    v_title = bi_type
                    title_source = "chart_type_fallback"
                logger.info(
                    "No explicit title for Qlik '%s' visual; using %s title %r.",
                    raw_qtype, title_source, v_title,
                )
            v_name = v.get("name") or v.get("qlik_name") or f"{prefix}{idx + 1}"

            v_item = {
                "name": v_name,
                "qlik_source": v,
                "fabric": {
                    "visual_type": ftype,
                    "bi_type": bi_type,
                    "supported": ftype not in ("unsupported",),
                    "status": "mapped",
                    "title": v_title,
                    "title_source": title_source,
                    "name": v_name,
                    "object_category": "standard",
                    "sheet_name": sheet_title,
                    "requires_custom_visual": mapping.get("requires_custom_visual", False),
                    "replacement_strategy": (
                        f"'{bi_type}' renders via the AppSource '{ftype}' custom visual, which "
                        "must be registered in the target report; without it Fabric/Power BI will "
                        "show a placeholder asking to add the visual."
                    ) if mapping.get("requires_custom_visual") else None,
                    "layout": layout,
                    "power_bi_visual_type": {
                        "visualType": ftype,
                        "title": {"text": v_title, "visible": True},
                        "general": {"x": layout["x"], "y": layout["y"],
                                    "width": layout["width"], "height": layout["height"]},
                    },
                    "rationale": f"Qlik '{raw_qtype}' mapped to Fabric '{ftype}'.",
                    "x_axis_fields": x_fields,
                    "y_axis_fields": y_fields,
                    "z_axis_fields": z_fields,
                    "style": as_dict(v.get("style")),
                    "formatting": as_dict(v.get("formatting")),
                    "kpi_styling": as_dict(v.get("kpi_styling")),
                    "custom_coloring": as_dict(v.get("custom_coloring") or v.get("coloring")),
                    "button_text": v.get("button_text") or v.get("label") or v_title,
                    "button_action": as_dict(v.get("button_action") or v.get("actions")),
                    "image_url": v.get("image_url") or v.get("url") or v.get("media_url"),
                },
                "confidence": self.confidence_eval.evaluate_visual(raw_qtype, ftype)
            }

            sheet_visuals.append(v_item)
            sheet_map.setdefault(sheet_title, []).append(v_item)

        sheets_list = []
        if raw_sheets:
            for s in raw_sheets:
                stitle = s.get("title") or s.get("name") or "Sheet"
                s_vis = sheet_map.get(stitle, sheet_visuals if len(raw_sheets) == 1 else [])
                sheets_list.append({
                    "sheet_id": s.get("sheet_id") or str(uuid.uuid4()),
                    "title": stitle,
                    "visualization_count": len(s_vis),
                    "visualizations": s_vis
                })
        else:
            for stitle, s_vis in sheet_map.items():
                sheets_list.append({
                    "sheet_id": str(uuid.uuid4()),
                    "title": stitle,
                    "visualization_count": len(s_vis),
                    "visualizations": s_vis
                })

        return {
            "sheet_visuals": sheet_visuals,
            "sheets": sheets_list
        }
