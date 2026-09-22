"""Qlik variable -> Fabric artifact conversion.

Previously `variables` was a raw passthrough: coordinator_agent emitted
`summary_builder.format_passthrough(data, "variables")` and nothing ever
decided what a variable should become. The generation agent then swept every
variable into one shared Parameters table, including the ~25 Qlik reserved
locale variables (ThousandSep, DateFormat, MoneyFormat) that a typical app
carries - none of which are report parameters.

This converter classifies each variable into one of four kinds and emits the
artifact that kind actually needs. See src/converters/variables/rules.py.

A deterministic classifier runs first and is always correct for the reserved
set (that list is fixed and documented by Qlik). The model is only consulted
for the genuinely ambiguous decisions - literal vs parameter vs expression -
and its answer is validated before use.
"""

import asyncio
import datetime
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from services import dax_guard, llm_usage
from services.prompt_builder import build_variable_prompts
from services.schema_context import build_schema_context

from .rules import VARIABLE_KINDS, VARIABLES_OUTPUT_SCHEMA

logger = logging.getLogger(__name__)

STAGE = "variables"

# Qlik reserved/system variables. Set by the engine or by SET statements in
# the load script, these control locale and formatting - they are not report
# parameters. Documented under Qlik's "System variables" and
# "Number interpretation variables".
RESERVED_VARIABLES = {
    "thousandsep", "decimalsep", "moneythousandsep", "moneydecimalsep",
    "moneyformat", "timeformat", "dateformat", "timestampformat",
    "monthnames", "daynames", "longmonthnames", "longdaynames",
    "firstweekday", "brokenweeks", "referenceday", "firstmonthofyear",
    "collationlocale", "numericalabbreviation", "createsearchindexonreload",
    "hideprefix", "hidesuffix", "include", "mustinclude", "openurltimeout",
    "stripcomments", "verbatim", "nullvalue", "nulldisplay", "nullinterpret",
    "errormode", "scripterrorcount", "scripterrorlist", "scripterrordetails",
    "qvpath", "qvroot", "qvworkpath", "qvworkroot", "wintask", "winpath",
    "winroot", "cd", "floppy", "now", "today", "version",
}

# Which model setting each reserved variable feeds.
FORMAT_TARGETS = {
    "thousandsep": "decimal_separator_group",
    "decimalsep": "decimal_separator",
    "moneythousandsep": "currency_group_separator",
    "moneydecimalsep": "currency_decimal_separator",
    "moneyformat": "currency_symbol",
    "dateformat": "date_format",
    "timeformat": "time_format",
    "timestampformat": "datetime_format",
    "firstmonthofyear": "fiscal_year_start",
    "firstweekday": "week_start_day",
    "collationlocale": "culture",
    "monthnames": "month_names",
    "daynames": "day_names",
}

# A definition that computes rather than stores.
EXPRESSION_MARKER = re.compile(
    r"^\s*=|\b(sum|count|avg|average|min|max|only|aggr|rangesum|firstsortedvalue)\s*\(|\{\s*<",
    re.IGNORECASE,
)

# Qlik/Excel day 0. Serial 44562 -> 2022-01-01.
QLIK_EPOCH = datetime.date(1899, 12, 30)

# Names that signal a user-facing choice even without an explicit binding.
PARAMETER_NAME_HINTS = (
    "topn", "selected", "chosen", "show", "toggle", "measure", "dimension",
    "threshold", "target", "filter", "switch", "mode", "scenario",
)


def _is_reserved(variable: Dict[str, Any]) -> bool:
    if variable.get("is_reserved") is True:
        return True
    name = str(variable.get("name") or "").strip().lower()
    return name in RESERVED_VARIABLES


def _looks_like_expression(definition: str) -> bool:
    return bool(definition) and bool(EXPRESSION_MARKER.search(definition))


def _serial_to_iso(value: str) -> Optional[str]:
    """Qlik date serial -> ISO date (var15), or None when not a plain serial."""
    try:
        serial = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    # Below ~20000 a bare integer is far more likely a count than a date.
    if not (20000 <= serial <= 80000):
        return None
    try:
        return (QLIK_EPOCH + datetime.timedelta(days=serial)).isoformat()
    except (OverflowError, ValueError):
        return None


def _infer_data_type(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "string"
    if re.fullmatch(r"-?\d+", text):
        return "int64"
    if re.fullmatch(r"-?\d*\.\d+", text):
        return "double"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([ T].*)?", text):
        return "dateTime"
    return "string"


def collect_interactive_variables(visuals: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
    """Variable names bound to a variable-input object, with any alternatives.

    A Qlik variable-input object is the strongest signal that a value variable
    is a what-if parameter rather than a constant (var4).
    """
    found: Dict[str, List[Any]] = {}
    for visual in visuals or []:
        if not isinstance(visual, dict):
            continue
        source = visual.get("qlik_source") if isinstance(visual.get("qlik_source"), dict) else visual
        chart_type = str(
            source.get("chart_type") or source.get("qlik_type") or source.get("type") or ""
        ).strip().lower()
        if chart_type not in (
            "qlik-variable-input", "variable-input", "variableinput", "variable",
        ):
            continue
        name = (
            source.get("variable_name")
            or source.get("variable")
            or source.get("title")
            or source.get("name")
        )
        if not name:
            continue
        props = source.get("custom_properties") or source.get("properties") or {}
        alternatives = []
        if isinstance(props, dict):
            alternatives = (
                props.get("alternatives") or props.get("options") or props.get("values") or []
            )
        found[str(name)] = list(alternatives) if isinstance(alternatives, list) else []
    return found


class VariableConverter:
    """Classifies Qlik variables and converts each to its Fabric artifact."""

    def __init__(self, llm_client=None):
        self._llm_client = llm_client

    @property
    def llm_client(self):
        if self._llm_client is None:
            from src.converters.llm_client import GroqLLMClient

            self._llm_client = GroqLLMClient()
        return self._llm_client

    # -- deterministic classification ------------------------------------

    def classify(
        self, variable: Dict[str, Any], interactive: Dict[str, List[Any]]
    ) -> Tuple[str, str]:
        """(kind, reason) from the rules that need no judgement.

        Reserved detection is exact - Qlik's system-variable list is fixed -
        so it never goes to the model. Expression detection is a syntax test.
        The literal/parameter split is the only genuinely ambiguous one.
        """
        name = str(variable.get("name") or "")
        definition = str(variable.get("definition") or variable.get("value") or "")

        if _is_reserved(variable):
            return "reserved", "Qlik system/locale variable (var2)."

        if _looks_like_expression(definition):
            return "expression", "Definition computes rather than stores a value (var3)."

        if name in interactive:
            return "parameter", "Bound to a variable-input object (var4)."

        lowered = name.lower()
        if any(hint in lowered for hint in PARAMETER_NAME_HINTS):
            return "parameter", "Name signals an interactive choice (var4)."

        return "literal", "Plain constant value (var3/var4)."

    def _deterministic_fabric(
        self,
        variable: Dict[str, Any],
        kind: str,
        interactive: Dict[str, List[Any]],
    ) -> Dict[str, Any]:
        """The baseline artifact, built without the model."""
        name = str(variable.get("name") or "Variable")
        definition = str(variable.get("definition") or variable.get("value") or "")
        fabric: Dict[str, Any] = {"kind": kind}

        if kind == "reserved":
            fabric["format_target"] = FORMAT_TARGETS.get(name.lower())
            fabric["value"] = definition
            fabric["emit_as_parameter"] = False
            return fabric

        if kind == "expression":
            fabric["dax_expression"] = ""
            fabric["home_table"] = None
            return fabric

        data_type = _infer_data_type(definition)
        fabric["data_type"] = data_type
        fabric["value"] = definition

        iso = _serial_to_iso(definition) if data_type == "int64" else None
        if iso:
            fabric["iso_date"] = iso
            fabric["data_type"] = "dateTime"

        if kind == "parameter":
            alternatives = interactive.get(name) or []
            fabric["table"] = name
            fabric["column"] = name
            fabric["selection_measure"] = f"{name} Value"
            fabric["selection_measure_dax"] = (
                f"SELECTEDVALUE('{name}'[{name}], {_dax_literal(definition, fabric['data_type'])})"
            )
            fabric["values"] = alternatives
            fabric["generation_strategy"] = "datatable" if alternatives else "generateseries"
        return fabric

    # -- LLM refinement ---------------------------------------------------

    async def refine_one(
        self,
        variable: Dict[str, Any],
        kind: str,
        tables: List[Dict[str, Any]],
        schema_context: str,
        interactive: Dict[str, List[Any]],
    ) -> Optional[Dict[str, Any]]:
        """Ask the model to convert one variable; returns its validated answer."""
        usage = llm_usage.current()
        usage.record_attempt(STAGE)
        try:
            system, user = build_variable_prompts(
                variable=variable,
                schema_context=schema_context,
                interactive_names=sorted(interactive),
            )
            answer = await self.llm_client.generate_structured_response(
                system, user, VARIABLES_OUTPUT_SCHEMA
            )
            usage.record_success(STAGE)
        except Exception as exc:  # noqa: BLE001
            usage.record_failure(STAGE, str(exc))
            logger.warning(
                "LLM variable conversion failed for '%s' (%s); keeping rule-based result.",
                variable.get("name"), exc,
            )
            return None

        if not isinstance(answer, dict):
            usage.record_rejected(STAGE, "response was not an object")
            return None

        llm_kind = answer.get("kind")
        if llm_kind not in VARIABLE_KINDS:
            usage.record_rejected(STAGE, f"unknown kind {llm_kind!r}")
            return None

        # The reserved verdict is not the model's to overturn: Qlik's system
        # variable list is fixed, and promoting one to a parameter is exactly
        # the bug this converter exists to fix.
        if kind == "reserved" and llm_kind != "reserved":
            usage.record_rejected(
                STAGE, f"tried to reclassify reserved variable as {llm_kind}"
            )
            logger.info(
                "Ignoring LLM attempt to reclassify reserved variable '%s' as '%s'.",
                variable.get("name"), llm_kind,
            )
            return None

        # Any DAX the model produced must survive the same guard as a measure.
        for field, is_measure in (
            ("dax_expression", True),
            ("selection_measure_dax", True),
            ("switch_measure_dax", True),
        ):
            expr = answer.get(field) or (answer.get("parameter") or {}).get(field)
            if not expr:
                continue
            ok, problems = dax_guard.validate(
                dax_guard.strip_model_formatting(str(expr)), tables, is_measure=is_measure
            )
            if not ok:
                usage.record_rejected(STAGE, f"{field}: {'; '.join(problems[:2])}")
                logger.info(
                    "Rejected LLM %s for variable '%s': %s",
                    field, variable.get("name"), "; ".join(problems[:2]),
                )
                return None

        usage.record_accepted(STAGE)
        return answer

    # -- entry point ------------------------------------------------------

    async def convert_all(
        self,
        variables: List[Dict[str, Any]],
        tables: List[Dict[str, Any]],
        visuals: Optional[List[Dict[str, Any]]] = None,
        measures: Optional[List[Dict[str, Any]]] = None,
        relationships: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Classify and convert every variable, returning enriched copies."""
        if not variables:
            return []

        interactive = collect_interactive_variables(visuals or [])
        results: List[Dict[str, Any]] = []
        needs_llm: List[Tuple[Dict[str, Any], str]] = []

        for raw in variables:
            if not isinstance(raw, dict):
                continue
            kind, reason = self.classify(raw, interactive)
            item = dict(raw)
            item["kind"] = kind
            item["fabric"] = self._deterministic_fabric(raw, kind, interactive)
            item["classification_reason"] = reason
            item["conversion_method"] = "rule_based"
            results.append(item)

            # Reserved variables never need the model: the list is exact, and
            # there is nothing to convert - they feed format settings.
            if kind != "reserved":
                needs_llm.append((item, kind))

        if not Config.USE_LLM_VARIABLES or not needs_llm:
            return results

        schema_context = build_schema_context(
            tables, measures=measures, relationships=relationships
        )
        logger.info(
            "Converting %d variable(s) with the LLM (%d reserved skipped)",
            len(needs_llm), len(results) - len(needs_llm),
        )

        answers = await asyncio.gather(
            *(
                self.refine_one(item, kind, tables, schema_context, interactive)
                for item, kind in needs_llm
            ),
            return_exceptions=True,
        )

        for (item, _kind), answer in zip(needs_llm, answers):
            if isinstance(answer, Exception) or not answer:
                continue
            fabric = item["fabric"]
            fabric["kind"] = answer.get("kind", fabric.get("kind"))
            item["kind"] = fabric["kind"]
            for key in ("data_type", "literal_value", "iso_date", "home_table", "note"):
                if answer.get(key) is not None:
                    fabric[key] = answer[key]
            if answer.get("dax_expression"):
                fabric["dax_expression"] = dax_guard.strip_model_formatting(
                    str(answer["dax_expression"])
                )
            param = answer.get("parameter")
            if isinstance(param, dict) and fabric["kind"] == "parameter":
                for src_key, dst_key in (
                    ("table_name", "table"),
                    ("column_name", "column"),
                    ("selection_measure_name", "selection_measure"),
                    ("selection_measure_dax", "selection_measure_dax"),
                    ("generation_strategy", "generation_strategy"),
                    ("values", "values"),
                    ("min", "min"),
                    ("max", "max"),
                    ("step", "step"),
                    ("default_value", "default_value"),
                    ("switch_measure_dax", "switch_measure_dax"),
                ):
                    if param.get(src_key) is not None:
                        fabric[dst_key] = param[src_key]
            item["conversion_method"] = "llm"
            item["requires_review"] = bool(answer.get("requires_review"))
            item["rationale"] = answer.get("rationale") or item.get("classification_reason")

        return results


def _dax_literal(value: str, data_type: str) -> str:
    """Render a default value as a DAX literal for SELECTEDVALUE's 2nd arg."""
    text = str(value or "").strip()
    if not text:
        return "BLANK()"
    if data_type in ("int64", "double", "decimal"):
        return text
    escaped = text.replace('"', '""')
    return f'"{escaped}"'
