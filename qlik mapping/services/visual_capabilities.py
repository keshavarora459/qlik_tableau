"""Visual Semantic Capability Registry and Role Validation."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class VisualSupportLevel(str, Enum):
    SUPPORTED_NATIVE = "SUPPORTED_NATIVE"
    SUPPORTED_WITH_CONFIGURATION = "SUPPORTED_WITH_CONFIGURATION"
    SUPPORTED_WITH_CUSTOM_VISUAL = "SUPPORTED_WITH_CUSTOM_VISUAL"
    SUPPORTED_WITH_APPROXIMATION = "SUPPORTED_WITH_APPROXIMATION"
    UNSUPPORTED_REQUIRES_REVIEW = "UNSUPPORTED_REQUIRES_REVIEW"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass
class VisualCapability:
    visual_type: str
    fabric_visual_type: str
    support_level: VisualSupportLevel
    required_roles: List[str]
    optional_roles: List[str] = field(default_factory=list)
    fallback_type: Optional[str] = None
    fallback_reason: Optional[str] = None


class VisualCapabilityRegistry:
    """Registry defining required projection roles and support tiers for visual types."""

    _CAPABILITIES: Dict[str, VisualCapability] = {
        "card": VisualCapability("card", "card", VisualSupportLevel.SUPPORTED_NATIVE, ["Values"], ["Tooltips"]),
        "multirowcard": VisualCapability("multirowcard", "multiRowCard", VisualSupportLevel.SUPPORTED_NATIVE, ["Values"], ["Tooltips"]),
        "kpi": VisualCapability("kpi", "card", VisualSupportLevel.SUPPORTED_NATIVE, ["Values"], ["TrendAxis", "Target"]),
        "barchart": VisualCapability("barchart", "barChart", VisualSupportLevel.SUPPORTED_NATIVE, ["Category", "Y"], ["Series", "Tooltips"]),
        "columnchart": VisualCapability("columnchart", "columnChart", VisualSupportLevel.SUPPORTED_NATIVE, ["Category", "Y"], ["Series", "Tooltips"]),
        "linechart": VisualCapability("linechart", "lineChart", VisualSupportLevel.SUPPORTED_NATIVE, ["Category", "Y"], ["Series", "Tooltips"]),
        "areachart": VisualCapability("areachart", "areaChart", VisualSupportLevel.SUPPORTED_NATIVE, ["Category", "Y"], ["Series", "Tooltips"]),
        "combochart": VisualCapability("combochart", "comboChart", VisualSupportLevel.SUPPORTED_NATIVE, ["Category", "Y"], ["Series", "Y2", "Tooltips"]),
        "piechart": VisualCapability("piechart", "pieChart", VisualSupportLevel.SUPPORTED_NATIVE, ["Category", "Y"], ["Tooltips"]),
        "donutchart": VisualCapability("donutchart", "donutChart", VisualSupportLevel.SUPPORTED_NATIVE, ["Category", "Y"], ["Tooltips"]),
        "treemap": VisualCapability("treemap", "treeMap", VisualSupportLevel.SUPPORTED_NATIVE, ["Group", "Values"], ["Details", "Tooltips"]),
        "table": VisualCapability("table", "tableEx", VisualSupportLevel.SUPPORTED_NATIVE, ["Values"], ["Columns", "Rows", "Tooltips"]),
        "tableex": VisualCapability("tableex", "tableEx", VisualSupportLevel.SUPPORTED_NATIVE, ["Values"], ["Columns", "Rows", "Tooltips"]),
        "pivottable": VisualCapability("pivottable", "pivotTable", VisualSupportLevel.SUPPORTED_NATIVE, ["Values"], ["Columns", "Rows", "Tooltips"]),
        "matrix": VisualCapability("matrix", "pivotTable", VisualSupportLevel.SUPPORTED_NATIVE, ["Values"], ["Columns", "Rows", "Tooltips"]),
        "scatterchart": VisualCapability("scatterchart", "scatterChart", VisualSupportLevel.SUPPORTED_NATIVE, ["X", "Y"], ["Size", "Details", "Legend", "Tooltips"]),
        "slicer": VisualCapability("slicer", "slicer", VisualSupportLevel.SUPPORTED_NATIVE, ["Values"], ["Category"]),
        "filterpane": VisualCapability("filterpane", "slicer", VisualSupportLevel.SUPPORTED_NATIVE, ["Values"], ["Category"]),
        "boxplot": VisualCapability("boxplot", "tableEx", VisualSupportLevel.SUPPORTED_WITH_CUSTOM_VISUAL, ["Category", "Y"], ["Tooltips"], fallback_type="tableEx", fallback_reason="Native boxplot requires AppSource visual package"),
        "funnel": VisualCapability("funnel", "funnel", VisualSupportLevel.SUPPORTED_WITH_CONFIGURATION, ["Category", "Y"], ["Tooltips"]),
        "gauge": VisualCapability("gauge", "gauge", VisualSupportLevel.SUPPORTED_NATIVE, ["Values"], ["Target", "Minimum", "Maximum"]),
    }

    @classmethod
    def get_capability(cls, visual_type: str) -> VisualCapability:
        clean = (visual_type or "table").strip().lower().replace("-", "").replace("_", "")
        return cls._CAPABILITIES.get(clean, VisualCapability(
            visual_type=visual_type,
            fabric_visual_type="tableEx",
            support_level=VisualSupportLevel.UNSUPPORTED_REQUIRES_REVIEW,
            required_roles=["Values"],
            fallback_type="tableEx",
            fallback_reason=f"Unrecognized visual type '{visual_type}' fallback to tableEx",
        ))

    @classmethod
    def validate_projection(cls, visual_type: str, field_roles: List[Dict[str, Any]]) -> Dict[str, Any]:
        cap = cls.get_capability(visual_type)
        assigned_roles = {r.get("role") for r in field_roles if isinstance(r, dict) and r.get("field")}

        # Table and Slicers accept Category/Columns/Rows as alternative to Values
        missing_roles = []
        for req in cap.required_roles:
            if req not in assigned_roles:
                if req == "Values" and assigned_roles & {"Columns", "Rows", "Category"}:
                    continue
                if req == "Category" and assigned_roles & {"Columns", "Rows", "Values", "Group"}:
                    continue
                missing_roles.append(req)

        is_valid = len(missing_roles) == 0 and len(field_roles) > 0
        return {
            "is_valid": is_valid,
            "support_level": cap.support_level if is_valid else VisualSupportLevel.UNSUPPORTED_REQUIRES_REVIEW,
            "missing_roles": missing_roles,
            "assigned_roles": list(assigned_roles),
            "fabric_visual_type": cap.fabric_visual_type,
            "requires_review": not is_valid or cap.support_level == VisualSupportLevel.UNSUPPORTED_REQUIRES_REVIEW,
        }
