# rules/__init__.py
# Single registry of every LLM conversion rule in the service.
#
# ALL_RULES is consumed by services/prompt_builder.py, which filters it by
# `type` when building each system prompt. Until that module existed nothing
# imported ALL_RULES at all, so every rule was dead code.
#
# Rule types and the stage that consumes them:
#
#   measures           src/converters/measures/converter.py   (Qlik expr -> DAX measure)
#   dimensions         src/converters/dimensions/converter.py (Qlik dim  -> DAX column)
#   variables          src/converters/variables/converter.py  (Qlik var  -> measure/parameter/format)
#   columns            column typing, summarizeBy and format strings
#   mquery             Qlik load script / connection -> Power Query M
#   dashboard_objects  src/converters/dashboard_objects/converter.py (visual + field roles + colour)
#
# A rule carrying "scope": "output_format" describes the response envelope
# rather than the conversion. prompt_builder owns the output contract per
# stage, so those are excluded from prompts to avoid two contradictory format
# instructions in one message.

from src.converters.columns.rules import COLUMNS_RULES
from src.converters.dimensions.rules import DIMENSIONS_RULES
from src.converters.measures.rules import MEASURES_CORE_RULES
from src.converters.mquery.rules import MQUERY_RULES
from src.converters.variables.rules import VARIABLES_RULES

from .measures_advanced_rules import MEASURES_ADVANCED_RULES

_STATIC_RULES = (
    DIMENSIONS_RULES
    + MEASURES_CORE_RULES
    + MEASURES_ADVANCED_RULES
    + VARIABLES_RULES
    + COLUMNS_RULES
    + MQUERY_RULES
)


def _dashboard_rules():
    """Imported lazily to keep this package importable on its own.

    src/converters/dashboard_objects/rules.py builds VALID_VISUAL_TYPES by
    instantiating VisualMapper and reading the custom-object registry, so
    importing it eagerly here would drag half the service into any module
    that only wanted the DAX rules.
    """
    from src.converters.dashboard_objects.rules import RULES as DASHBOARD_OBJECTS_RULES

    return DASHBOARD_OBJECTS_RULES


class _AllRules(list):
    """ALL_RULES with the dashboard rules resolved on first access."""

    _resolved = False

    def _ensure(self):
        if not self._resolved:
            self._resolved = True
            try:
                self.extend(_dashboard_rules())
            except Exception:  # noqa: BLE001 - never break DAX conversion
                pass

    def __iter__(self):
        self._ensure()
        return list.__iter__(self)

    def __len__(self):
        self._ensure()
        return list.__len__(self)

    def __getitem__(self, item):
        self._ensure()
        return list.__getitem__(self, item)


ALL_RULES = _AllRules(_STATIC_RULES)

# Every rule type the prompt builder knows how to inject.
RULE_TYPES = (
    "measures",
    "dimensions",
    "variables",
    "columns",
    "mquery",
    "dashboard_objects",
)

__all__ = [
    "ALL_RULES",
    "RULE_TYPES",
    "COLUMNS_RULES",
    "DIMENSIONS_RULES",
    "MEASURES_CORE_RULES",
    "MEASURES_ADVANCED_RULES",
    "MQUERY_RULES",
    "VARIABLES_RULES",
]
