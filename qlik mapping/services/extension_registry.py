"""Dynamic Extension Registry for Qlik and Third-Party Plugins."""

from dataclasses import dataclass
from typing import Any, Dict, Optional
from .visual_capabilities import VisualSupportLevel


@dataclass
class ExtensionClassification:
    extension_name: str
    category: str
    target_visual: str
    support_level: VisualSupportLevel
    fallback_strategy: str
    requires_review: bool = False


class ExtensionRegistry:
    """Classifies Qlik Sense extensions dynamically."""

    _KNOWN = {
        "qlik-variable-input": ExtensionClassification(
            "qlik-variable-input", "input_control", "slicer",
            VisualSupportLevel.SUPPORTED_WITH_CONFIGURATION,
            "Mapped to Fabric Slicer / What-If Parameter control.",
        ),
        "sn-image": ExtensionClassification(
            "sn-image", "media", "image",
            VisualSupportLevel.SUPPORTED_NATIVE,
            "Mapped to Fabric Image visual.",
        ),
        "sn-button": ExtensionClassification(
            "sn-button", "action", "actionButton",
            VisualSupportLevel.SUPPORTED_NATIVE,
            "Mapped to Fabric Action Button with bookmark/URL navigation.",
        ),
        "qlik-funnel-chart-ext": ExtensionClassification(
            "qlik-funnel-chart-ext", "chart", "funnel",
            VisualSupportLevel.SUPPORTED_WITH_CONFIGURATION,
            "Mapped to Fabric Funnel chart visual.",
        ),
        "qlik-bullet-chart": ExtensionClassification(
            "qlik-bullet-chart", "kpi", "gauge",
            VisualSupportLevel.SUPPORTED_WITH_APPROXIMATION,
            "Mapped to Fabric Gauge / Card visual.",
            requires_review=True,
        ),
    }

    @classmethod
    def classify(cls, extension_name: str) -> ExtensionClassification:
        clean = (extension_name or "").strip().lower()
        if clean in cls._KNOWN:
            return cls._KNOWN[clean]

        # Dynamic classification for unknown extension
        return ExtensionClassification(
            extension_name=extension_name,
            category="custom_extension",
            target_visual="tableEx",
            support_level=VisualSupportLevel.UNSUPPORTED_REQUIRES_REVIEW,
            fallback_strategy=(
                f"Extension '{extension_name}' is not registered in the native Fabric visual catalog. "
                "Recreate manually using a custom visual from AppSource or a standard Table baseline."
            ),
            requires_review=True,
        )
