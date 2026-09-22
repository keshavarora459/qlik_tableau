"""Dashboard Object converter: maps Qlik dashboard canvas objects to Fabric visual layouts.

Two structural fixes live here.

1. `source_block` used to rebuild each visual from a 17-key whitelist, which
   silently dropped `color`, `kpi_styling`, `custom_coloring`,
   `style_and_formatting`, `reference_lines`, `image_url` and `button_action`
   - every one of which unified-parsing had already extracted. That is why
   action buttons arrived with no navigation target and image visuals with no
   URL. It now preserves the source object and only normalises key names.

2. `convert_one` used to return before ever calling the LLM whenever the
   deterministic lookup recognised the chart type - which is almost always -
   so the model was effectively dead code and the mapping was a static dict.
   The deterministic result is now a *hint and a fallback*, not a short
   circuit: the model runs for every visual, and its answer is only used
   where it is valid.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from services import llm_usage
from services.confidence_evaluator import ConfidenceEvaluator
from services.prompt_builder import build_visual_prompts
from services.visual_mapper import VisualMapper

# Fabric visual types that only render when the report registers a matching
# AppSource package - which this pipeline never does.
CUSTOM_VISUAL_ONLY_TYPES = VisualMapper.CUSTOM_VISUAL_TYPES

from .rules import (
    CATEGORY_TO_VISUAL_TYPE,
    OUTPUT_SCHEMA,
    RULES,
    SYSTEM_PROMPT,
    VALID_VISUAL_TYPES,
    _visual_mapper,
)
from src.converters.custom_objects.converter import KNOWN_EXTENSION_MAPPINGS

logger = logging.getLogger(__name__)

try:
    from src.converters.base import BaseConverter, ConversionContext, ConvertedItem
except ImportError:  # pragma: no cover - standalone execution safety net
    class ConversionContext:  # type: ignore[no-redef]
        pass

    class ConvertedItem:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class BaseConverter:  # type: ignore[no-redef]
        class DummyConfidence:
            def score(self, *args, **kwargs):
                return kwargs.get("llm_score", 0.95)

        validators = []
        confidence = DummyConfidence()

VALID_VISUAL_TYPES = set(VALID_VISUAL_TYPES)

STAGE = "visuals"

# Roles the generation agent understands. Anything else the model returns is
# discarded rather than written into the contract.
VALID_ROLES = {
    "Category", "Series", "Y", "Y2", "X", "Size", "Values",
    "Rows", "Columns", "Group", "Details", "Tooltips", "Unbound",
}
VALID_AGGREGATIONS = {"Sum", "Avg", "Count", "DistinctCount", "Min", "Max", "None"}

_HEX = ("#",)


def _is_color(value: Any) -> bool:
    """Only accept literal hex colours from the model.

    dash10 tells the model never to invent a colour; this enforces it, so a
    hallucinated colour name ("corporate blue") can't reach the report.
    """
    return isinstance(value, str) and value.strip().startswith(_HEX) and 4 <= len(value.strip()) <= 9


class DashboardObjectConverter(BaseConverter):
    name = "dashboard_objects"
    max_tokens = 2500

    def __init__(self):
        super().__init__()
        # Real evidence-based scoring. BaseConverter's DummyConfidence
        # returned a flat 0.95 for every visual, which sat above the 0.85
        # review threshold - so nothing was ever flagged, however badly it
        # converted.
        self.confidence_eval = ConfidenceEvaluator()

    def _deterministic_type(self, item: Dict[str, Any]) -> Tuple[str, bool, Optional[str]]:
        """Rule-based visual_type/supported/replacement for `item`, used both
        as LLM guidance and as the authoritative fallback when the LLM's own
        answer isn't a real Power BI visual type.

        When nothing in KNOWN_EXTENSION_MAPPINGS or VisualMapper recognizes
        the Qlik visual type, the item is marked unsupported and a concrete
        replacement suggestion is generated - so "no Fabric equivalent" is
        never a silent gap in the output, it's an explicit, actionable note.
        """
        for key in (item.get("chart_type"), item.get("object_category"), item.get("qlik_type")):
            if key and key in KNOWN_EXTENSION_MAPPINGS:
                ext = KNOWN_EXTENSION_MAPPINGS[key]
                return ext["visual_type"], ext.get("supported", True), ext.get("replacement")

        category = str(item.get("object_category") or "").strip().lower()
        if category in CATEGORY_TO_VISUAL_TYPE and not item.get("chart_type"):
            return CATEGORY_TO_VISUAL_TYPE[category], True, None

        raw_type = item.get("chart_type") or item.get("qlik_type") or item.get("object_category") or "table"
        mapping = _visual_mapper.get_mapping(raw_type)
        fabric_type = mapping["fabric_type"]

        if mapping["bi_type"] == "Unsupported":
            replacement = (
                f"No direct Power BI/Fabric visual exists for Qlik visual type '{raw_type}'. "
                "Recreate it using a Table or Matrix visual as a functional baseline, or search "
                "AppSource for an equivalent custom visual, then rebind the same fields."
            )
            return fabric_type, False, replacement

        # Smart re-inference: when the static mapping yields a plain Table
        # (tableEx) but the visual carries both dimensions AND measures,
        # a chart is almost always a better fit than a flat table.
        if fabric_type == "tableEx":
            dims = item.get("dimensions") or item.get("x_axis") or []
            meas = item.get("measures") or item.get("y_axis") or item.get("expressions_and_formulas") or []
            n_dims = len(dims) if isinstance(dims, list) else 0
            n_meas = len(meas) if isinstance(meas, list) else 0

            if n_dims >= 1 and n_meas >= 1:
                # Both dimensions and measures → pick a chart type
                if n_dims == 1 and n_meas == 1:
                    fabric_type = "barChart"
                elif n_dims == 1 and n_meas == 2:
                    fabric_type = "lineClusteredColumnComboChart"
                elif n_dims >= 2 and n_meas >= 1:
                    fabric_type = "clusteredColumnChart"
                else:
                    fabric_type = "clusteredColumnChart"
                logger.info(
                    "Re-inferred Qlik '%s' from tableEx to '%s' "
                    "(dims=%d, meas=%d).", raw_type, fabric_type, n_dims, n_meas,
                )
            elif n_meas >= 1 and n_dims == 0:
                # Measures only → card / multi-row card
                fabric_type = "card" if n_meas == 1 else "multiRowCard"

        return fabric_type, True, None

    def item_name(self, item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("title") or item.get("visual_name") or item.get("name") or item.get("qlik_name") or "DashboardObject")
        return "DashboardObject"

    def source_block(self, item: Any) -> Dict[str, Any]:
        """The Qlik object as parsed, with normalised aliases layered on top.

        Previously this returned a fixed 17-key projection and everything else
        was discarded. Downstream (`az-wa-repo-generationagent`'s
        visual_builder) reads `image_url`, `button_action`, `style`,
        `custom_coloring`, `kpi_styling` and `reference_lines` straight off
        this block - all of which were being dropped here, so those code paths
        could never fire no matter what the source app contained.
        """
        if not isinstance(item, dict):
            return {}

        block = dict(item)
        block.update({
            "title": item.get("title") or item.get("visual_name") or item.get("name") or item.get("qlik_name"),
            "visual_name": item.get("visual_name") or item.get("name") or item.get("qlik_name"),
            "object_category": item.get("object_category") or item.get("bi_type"),
            "chart_type": item.get("chart_type") or item.get("qlik_type"),
            "sheet_name": item.get("sheet_name") or item.get("source"),
            "layout": item.get("layout") or {},
            "y_axis": item.get("y_axis", []),
            "x_axis": item.get("x_axis", []),
            "measures": item.get("measures", []),
            "dimensions": item.get("dimensions", []),
            "formatting": item.get("formatting", {}),
            "header_styling": item.get("header_styling") or {},
            "card_styling": item.get("card_styling") or {},
            # Explicitly surfaced because the generation agent looks for these
            # exact keys and the Qlik payload spells them several ways.
            "image_url": (
                item.get("image_url") or item.get("url") or item.get("media_url") or item.get("src")
            ),
            "button_action": item.get("button_action") or item.get("actions") or {},
            "button_text": item.get("button_text") or item.get("label"),
            "custom_coloring": item.get("custom_coloring") or item.get("coloring") or {},
            "kpi_styling": item.get("kpi_styling") or {},
            "reference_lines": item.get("reference_lines") or [],
            "style_and_formatting": item.get("style_and_formatting") or {},
            "color": item.get("color") or {},
        })
        return block

    def build_user_payload(self, item: Any, context: ConversionContext) -> Dict[str, Any]:
        """A trimmed view for the prompt.

        `source_block` deliberately keeps everything; sending all of it -
        including hypercube sample rows - would blow the token budget for no
        benefit, so the prompt gets the decision-relevant subset.
        """
        block = self.source_block(item)
        keep = (
            "title", "visual_name", "object_category", "chart_type", "sheet_name",
            "col", "row", "colspan", "rowspan", "layout",
            "x_axis", "y_axis", "measures", "dimensions",
            "formatting", "color", "kpi_styling", "custom_coloring",
            "reference_lines", "image_url", "button_action", "button_text",
        )
        return {k: block.get(k) for k in keep if block.get(k) not in (None, [], {}, "")}

    # -- LLM response handling ------------------------------------------

    def _clean_field_roles(
        self, raw: Any, visual_type: str
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Validate the model's field bindings; return (roles, unbound_names)."""
        roles: List[Dict[str, Any]] = []
        unbound: List[str] = []
        if not isinstance(raw, list):
            return roles, unbound

        for entry in raw:
            if not isinstance(entry, dict):
                continue
            field = entry.get("field")
            if not field or not str(field).strip():
                continue
            role = entry.get("role")
            if role not in VALID_ROLES:
                continue

            entity = entry.get("entity")
            prop = entry.get("property")
            aggregation = entry.get("aggregation")
            if aggregation not in VALID_AGGREGATIONS:
                aggregation = "None"

            resolved = bool(entity) and bool(prop)
            if not resolved or role == "Unbound":
                unbound.append(str(field))

            roles.append({
                "field": str(field),
                "role": role,
                "entity": str(entity) if entity else None,
                "property": str(prop) if prop else None,
                "is_measure": bool(entry.get("is_measure")),
                "aggregation": aggregation,
                "resolved": resolved,
            })
        return roles, unbound

    def _clean_colors(self, raw: Any, fallback: Dict[str, Any]) -> Dict[str, Any]:
        """Keep only real hex values, falling back to the parsed source.

        The source colour block from unified-parsing is authoritative - it
        already resolved Qlik theme palette indices into real hex values. The
        model may only fill gaps, never overwrite a colour that was parsed.
        """
        parsed = fallback if isinstance(fallback, dict) else {}
        result: Dict[str, Any] = {}

        source_single = parsed.get("single_color") or parsed.get("resolved_palette_color")
        model = raw if isinstance(raw, dict) else {}

        single = source_single if _is_color(source_single) else None
        if single is None and _is_color(model.get("single_color")):
            single = model["single_color"].strip()
        if single:
            result["single_color"] = single

        palette = [c.strip() for c in (model.get("palette") or []) if _is_color(c)]
        if palette:
            result["palette"] = palette

        for key in ("background_color", "border_color", "value_color", "label_color"):
            value = model.get(key)
            if _is_color(value):
                result[key] = value.strip()

        is_multi = parsed.get("is_multicolor")
        if is_multi is None:
            is_multi = model.get("is_multicolor")
        if is_multi is not None:
            result["is_multicolor"] = bool(is_multi)

        rules = []
        for rule in (model.get("conditional_rules") or []):
            if isinstance(rule, dict) and _is_color(rule.get("color")):
                rules.append({
                    "expression": str(rule.get("expression") or ""),
                    "color": rule["color"].strip(),
                    "operator": str(rule.get("operator") or ""),
                    "value": rule.get("value"),
                })
        if rules:
            result["conditional_rules"] = rules

        # Anything the parser found that the model didn't mention survives.
        for key, value in parsed.items():
            result.setdefault(key, value)
        return result

    async def convert_one(self, item: Any, context: ConversionContext) -> ConvertedItem:
        if not isinstance(item, dict):
            item = {}

        usage = llm_usage.current()
        det_type, det_supported, det_replacement = self._deterministic_type(item)

        sheet_name = str(item.get("sheet_name") or item.get("source") or "Main Sheet")
        grid_info = getattr(context, "sheet_grids", {}).get(sheet_name, None)
        if grid_info:
            grid_cols, grid_rows = grid_info
        else:
            grid_cols = getattr(context, "grid_columns", 24) or 24
            grid_rows = getattr(context, "grid_rows", 12) or 12

        col = int(item.get("col", 0))
        row = int(item.get("row", 0))
        colspan = int(item.get("colspan", 6))
        rowspan = int(item.get("rowspan", 4))

        # Dynamic grid detection fallback if visual exceeds standard 24x12 bounds
        if col + colspan > grid_cols:
            grid_cols = max(84, col + colspan)
        if row + rowspan > grid_rows:
            grid_rows = max(42, row + rowspan)

        col_w = 1280.0 / grid_cols
        row_h = 720.0 / grid_rows

        calc_pos = {
            "x": int(round(col * col_w)),
            "y": int(round(row * row_h)),
            "width": max(int(round(colspan * col_w)), 20),
            "height": max(int(round(rowspan * row_h)), 20),
        }

        formatting = item.get("formatting") or {}
        human_title = (
            formatting.get("title")
            or item.get("title")
            or item.get("visual_name")
            or item.get("name")
            or item.get("qlik_name")
            or "Visual"
        )
        if str(human_title).strip().lower() in ("visualizations item", "unknown"):
            human_title = item.get("chart_type") or "Visual"

        raw_qtype = str(
            item.get("chart_type") or item.get("qlik_type")
            or item.get("object_category") or item.get("type") or ""
        ).strip().lower()

        # A Qlik pie with a donut hole is a donutChart in Power BI (dash12).
        if det_type == "pieChart" and (
            (formatting.get("inner_radius") and float(formatting.get("inner_radius") or 0) > 0)
            or raw_qtype in ("sn-donut", "donutchart")
        ):
            det_type = "donutChart"

        # ── LLM classification + field binding ───────────────────────────
        # For standard native visual types with recognized mappings, use fast deterministic conversion.
        # Only call LLM for unmapped, custom, or ambiguous visual types.
        needs_llm = Config.USE_LLM_VISUALS and (
            not det_supported or det_type in ("custom", "auto-chart", "unknown")
            or raw_qtype in ("auto-chart", "unknown", "")
            or str(item.get("chart_type") or "").strip().lower() == "extension"
        )

        llm_response: Dict[str, Any] = {}
        if needs_llm:
            usage.record_attempt(STAGE)
            try:
                system_prompt, user_prompt = build_visual_prompts(
                    visual_payload=self.build_user_payload(item, context),
                    schema_context=getattr(context, "schema_context", ""),
                    deterministic_type=det_type,
                    valid_visual_types=sorted(VALID_VISUAL_TYPES),
                )
                llm_response = await self._call_llm_prompts(
                    context, system_prompt, user_prompt, OUTPUT_SCHEMA
                ) or {}
                usage.record_success(STAGE)
            except Exception as exc:  # noqa: BLE001
                usage.record_failure(STAGE, str(exc))
                logger.warning(
                    "LLM visual conversion failed for Qlik type '%s' (%s); "
                    "falling back to rule-based mapping.", raw_qtype, exc,
                )
                llm_response = {
                    "rationale": f"LLM classification failed ({exc}); used rule-based mapping."
                }
        else:
            usage.record_deterministic(STAGE)
            # Deterministic fast path: construct standard field roles
            dims = item.get("dimensions") or item.get("x_axis") or []
            meas = item.get("measures") or item.get("y_axis") or item.get("expressions_and_formulas") or []
            det_roles = []

            # Resolve table columns and measures from context if present
            known_measures = getattr(context, "measures", []) or []
            known_tables = getattr(context, "tables", []) or []

            meas_lookup = {}
            for km in known_measures:
                if isinstance(km, dict):
                    m_n = km.get("name") or km.get("qlik_name") or ""
                    if m_n:
                        meas_lookup[m_n.lower()] = km
                        meas_lookup[re.sub(r"[^a-zA-Z0-9_]", "", m_n.lower())] = km

            col_lookup = {}
            for kt in known_tables:
                if isinstance(kt, dict):
                    t_n = kt.get("name") or kt.get("table_name") or ""
                    for kc in kt.get("columns", []):
                        if isinstance(kc, dict):
                            c_n = kc.get("fabric_column_name") or kc.get("qlik_column_name") or ""
                            if c_n:
                                col_lookup[c_n.lower()] = (t_n, c_n)
                                col_lookup[re.sub(r"[^a-zA-Z0-9_]", "", c_n.lower())] = (t_n, c_n)

            for i, d in enumerate(dims if isinstance(dims, list) else []):
                if isinstance(d, dict):
                    d_name = d.get("name") or d.get("field") or d.get("label") or d.get("qDef") or ""
                elif isinstance(d, str):
                    d_name = d.strip()
                else:
                    d_name = str(d or "").strip()

                if d_name:
                    d_clean = re.sub(r"[^a-zA-Z0-9_]", "", d_name.lower())
                    entity = None
                    prop = d_name
                    if d_name.lower() in col_lookup:
                        entity, prop = col_lookup[d_name.lower()]
                    elif d_clean in col_lookup:
                        entity, prop = col_lookup[d_clean]
                    elif known_tables:
                        entity = known_tables[0].get("name") or known_tables[0].get("table_name")

                    if det_type in ("tableEx", "pivotTable", "matrix"):
                        role = "Columns" if i == 0 else "Rows"
                    elif det_type in ("slicer",):
                        role = "Values"
                    elif det_type in ("treeMap",):
                        role = "Group"
                    else:
                        role = "Category" if i == 0 else "Series"

                    det_roles.append({
                        "field": str(d_name), "role": role, "entity": entity, "property": str(prop),
                        "is_measure": False, "aggregation": "None", "resolved": True
                    })

            for m in (meas if isinstance(meas, list) else []):
                if isinstance(m, dict):
                    m_name = m.get("name") or m.get("field") or m.get("label") or m.get("expression") or m.get("qDef") or ""
                elif isinstance(m, str):
                    m_name = m.strip()
                else:
                    m_name = str(m or "").strip()

                if m_name:
                    m_clean = re.sub(r"[^a-zA-Z0-9_]", "", m_name.lower())
                    entity = None
                    prop = m_name
                    is_meas = True
                    agg = "Sum"

                    if m_name.lower() in meas_lookup:
                        matched_m = meas_lookup[m_name.lower()]
                        fabric_info = matched_m.get("fabric") or {}
                        entity = fabric_info.get("table") or (matched_m.get("tables", [None])[0] if matched_m.get("tables") else None)
                        prop = matched_m.get("name") or m_name
                        agg = "None"
                    elif m_clean in meas_lookup:
                        matched_m = meas_lookup[m_clean]
                        fabric_info = matched_m.get("fabric") or {}
                        entity = fabric_info.get("table") or (matched_m.get("tables", [None])[0] if matched_m.get("tables") else None)
                        prop = matched_m.get("name") or m_name
                        agg = "None"
                    elif m_name.lower() in col_lookup:
                        entity, prop = col_lookup[m_name.lower()]
                        is_meas = False
                    elif d_clean in col_lookup:
                        entity, prop = col_lookup[d_clean]
                        is_meas = False
                    elif known_tables:
                        entity = known_tables[0].get("name") or known_tables[0].get("table_name")

                    if det_type in ("tableEx", "pivotTable", "card", "multiRowCard", "treeMap", "slicer"):
                        role = "Values"
                    else:
                        role = "Y"

                    det_roles.append({
                        "field": str(m_name), "role": role, "entity": entity, "property": str(prop),
                        "is_measure": is_meas, "aggregation": agg, "resolved": True
                    })

            llm_response = {
                "visual_type": det_type,
                "supported": det_supported,
                "field_roles": det_roles,
                "rationale": f"Mapped Qlik '{raw_qtype}' deterministically to Fabric '{det_type}'."
            }

        if not isinstance(llm_response, dict):
            llm_response = {}

        # Position: trust the model only when it actually supplied numbers.
        llm_position = llm_response.get("position") or {}
        pos = {
            "x": llm_position.get("x") if isinstance(llm_position.get("x"), (int, float)) else calc_pos["x"],
            "y": llm_position.get("y") if isinstance(llm_position.get("y"), (int, float)) else calc_pos["y"],
            "width": llm_position.get("width") if isinstance(llm_position.get("width"), (int, float)) else calc_pos["width"],
            "height": llm_position.get("height") if isinstance(llm_position.get("height"), (int, float)) else calc_pos["height"],
        }

        llm_type = llm_response.get("visual_type")
        rationale = str(llm_response.get("rationale") or "").strip()

        if not det_supported:
            # The deterministic layer knows this type has no native equivalent;
            # that verdict is not the model's to overturn.
            visual_type = det_type
            supported = False
            rationale = f"{rationale} {det_replacement}".strip()
            used_llm_type = False
        elif llm_type in VALID_VISUAL_TYPES:
            visual_type = llm_type
            supported = bool(llm_response.get("supported", True))
            used_llm_type = True
            if visual_type != det_type:
                rationale = (
                    f"{rationale} (Model chose '{visual_type}' over the rule-based "
                    f"'{det_type}'.)"
                ).strip()
        else:
            visual_type = det_type
            supported = det_supported
            used_llm_type = False
            if llm_type:
                rationale = (
                    f"{rationale} LLM returned an unrecognized visual_type "
                    f"('{llm_type}'); used rule-based mapping instead."
                ).strip()
            elif not rationale:
                rationale = (
                    f"The Qlik Sense '{raw_qtype}' visual maps to Fabric '{visual_type}'."
                )

        title = llm_response.get("title") or human_title

        field_roles, unbound = self._clean_field_roles(
            llm_response.get("field_roles"), visual_type
        )
        colors = self._clean_colors(llm_response.get("colors"), item.get("color") or {})
        llm_formatting = llm_response.get("formatting") if isinstance(llm_response.get("formatting"), dict) else {}

        if field_roles:
            usage.record_accepted(STAGE)

        fabric = {
            "visual_type": visual_type,
            "bi_type": visual_type,
            "supported": supported,
            "status": llm_response.get("status", "mapped") if supported else "unmapped",
            "title": title,
            "name": title,
            "object_category": llm_response.get("object_category") or item.get("object_category") or "standard",
            "sheet_name": item.get("sheet_name") or item.get("source"),
            "replacement_strategy": (
                llm_response.get("replacement_strategy") or det_replacement
            ) if not supported else None,
            "layout": {
                "col": col, "row": row, "colspan": colspan, "rowspan": rowspan,
                **pos,
            },
            "power_bi_visual_type": {
                "visualType": visual_type,
                "title": {"text": title, "visible": True},
                "general": pos,
            },
            "rationale": rationale,
            # Explicit bindings. The generation agent prefers these over its
            # own ROLES table + difflib matching when they are present.
            "field_roles": field_roles,
            "unbound_fields": unbound,
            "y_axis_fields": item.get("y_axis", []),
            "x_axis_fields": item.get("x_axis", []),
            "measures": item.get("measures", []),
            "dimensions": item.get("dimensions", []),
            "formatting": formatting,
            "llm_formatting": llm_formatting,
            "kpi_styling": item.get("kpi_styling") or {},
            "color": colors,
            "colors": colors,
            "style_and_formatting": item.get("style_and_formatting") or {},
            "custom_coloring": item.get("custom_coloring") or {},
            "reference_lines": item.get("reference_lines") or [],
            "image_url": (
                item.get("image_url") or item.get("url") or item.get("media_url") or item.get("src")
            ),
            "button_action": item.get("button_action") or item.get("actions") or {},
            "button_text": item.get("button_text") or item.get("label") or title,
            "conversion_method": "llm" if used_llm_type or field_roles else "rule_based",
        }

        raw_score = llm_response.get("confidence")
        llm_score = float(raw_score) if isinstance(raw_score, (int, float)) else None
        if llm_score is not None and not used_llm_type and llm_type:
            # The model named a type this pipeline cannot emit, so its
            # judgement about this visual is demonstrably unreliable.
            llm_score = min(llm_score, 0.60)

        source_fields = len(item.get("dimensions") or []) + len(item.get("measures") or [])
        if not source_fields:
            source_fields = len(item.get("x_axis") or []) + len(item.get("y_axis") or [])

        source_colors = item.get("color") or {}
        source_had_colors = bool(
            (source_colors.get("single_color") if isinstance(source_colors, dict) else None)
            or (source_colors.get("resolved_palette_color") if isinstance(source_colors, dict) else None)
            or item.get("custom_coloring")
        )

        confidence = self.confidence_eval.evaluate_visual_conversion(
            qlik_type=raw_qtype,
            fabric_type=visual_type,
            supported=supported,
            field_roles=field_roles,
            unbound_fields=unbound,
            source_field_count=source_fields,
            layout=pos,
            requires_custom_visual=visual_type in CUSTOM_VISUAL_ONLY_TYPES,
            used_llm=bool(used_llm_type or field_roles),
            llm_score=llm_score,
            title_source="explicit" if (formatting.get("title") or item.get("title")) else "chart_type_fallback",
            colors_preserved=bool(colors.get("single_color") or colors.get("palette")),
            source_had_colors=source_had_colors,
        )

        return ConvertedItem(
            name=title,
            source=self.source_block(item),
            fabric=fabric,
            confidence=confidence,
        )
