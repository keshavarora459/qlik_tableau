"""Build the system/user prompts for LLM-assisted Qlik -> Fabric conversion.

This is the module that was missing. `rules/__init__.py` has always exported
ALL_RULES (DIMENSIONS_RULES + MEASURES_CORE_RULES + MEASURES_ADVANCED_RULES),
but nothing imported it, so ~70 hand-written conversion rules were dead code
and every measure/dimension was produced by regex alone.

Rules are filtered by `type` and truncated to a character budget before being
injected, because the full set comfortably exceeds Groq's payload limit.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from rules import ALL_RULES

logger = logging.getLogger(__name__)


def strip_qlik_comments(expression: str) -> str:
    """Remove Qlik `//` line comments and `/* */` blocks from an expression.

    A commented-out earlier attempt left in the expression is a common source
    of the model "correcting" toward the dead code instead of the live
    formula.
    """
    if not expression:
        return expression
    try:
        cleaned = re.sub(r"/\*.*?\*/", "", expression, flags=re.DOTALL)
        cleaned = re.sub(r"//[^\n]*", "", cleaned)
        cleaned = re.sub(r"\n\s*\n+", "\n", cleaned).strip()
        return cleaned or expression
    except re.error:
        return expression


def format_rules(rule_type: str, budget: Optional[int] = None) -> str:
    """Rules of one `type`, newline-joined, truncated to a char budget.

    Rules marked `scope: "output_format"` are skipped: they describe the
    response envelope, which this module owns per stage. Injecting one into a
    prompt that asks for a bare DAX expression puts two contradictory format
    instructions in the same message.
    """
    limit = budget or Config.LLM_RULES_CHAR_BUDGET
    applicable = [
        r for r in ALL_RULES
        if isinstance(r, dict)
        and r.get("type") == rule_type
        and r.get("scope") != "output_format"
    ]

    # Critical rules first, each group keeping its declaration order. Plain
    # first-come-first-served truncation would let a growing rule set silently
    # evict a correctness rule (cross-table RELATED(), "no MATCH in DAX",
    # dollar-sign expansion) while keeping a cosmetic one.
    ordered = (
        [r for r in applicable if r.get("priority") == "critical"]
        + [r for r in applicable if r.get("priority") != "critical"]
    )

    lines: List[str] = []
    used = 0
    dropped: List[str] = []
    dropped_critical: List[str] = []
    for rule in ordered:
        text = str(rule.get("rule") or rule.get("description") or "").strip()
        if not text:
            continue
        line = "- [{}] {}\n".format(rule.get("id", ""), text)
        if used + len(line) > limit:
            dropped.append(str(rule.get("id", "")))
            if rule.get("priority") == "critical":
                dropped_critical.append(str(rule.get("id", "")))
            continue
        lines.append(line)
        used += len(line)

    if dropped_critical:
        # This should be impossible: critical rules are emitted first, so
        # exhausting the budget on them means the budget is set below what
        # the stage needs to be correct at all.
        logger.error(
            "Rules budget (%d chars) dropped CRITICAL '%s' rule(s): %s. "
            "Conversion accuracy will suffer - raise LLM_RULES_CHAR_BUDGET.",
            limit, rule_type, ", ".join(dropped_critical),
        )
    elif dropped:
        logger.warning(
            "Rules budget (%d chars) dropped %d non-critical '%s' rule(s): %s.",
            limit, len(dropped), rule_type, ", ".join(dropped),
        )
    return "".join(lines)


# Guard rails that apply to every DAX-producing conversion, independent of
# the per-type rules. Kept separate so the budgeted rules block can be
# truncated without ever losing these.
_DAX_CORE_CONTRACT = """CRITICAL OUTPUT CONTRACT:
- Output ONLY the DAX expression. No explanation, no markdown, no code fences.
- Do NOT include the measure/column name or a leading '='. Return only the right-hand side.
- Use ONLY table and column names that appear in the SCHEMA below. Never invent
  names, and never emit placeholders like FACT_TABLE, UnknownTable or Table.
- Qualify every column as 'TableName'[ColumnName] - single quotes on the table,
  square brackets on the column.
- Measures are referenced as [MeasureName] with NO table prefix. If a name appears
  under AVAILABLE MEASURES it is a measure, not a column.
- CROSS-TABLE RULE: check each column against RELATIONSHIPS. Toward the One side use
  RELATED('OtherTable'[Column]). Toward the Many side you MUST aggregate -
  SUMX(RELATEDTABLE('OtherTable'), ...) or SUM('OtherTable'[Column]). Never leave a
  naked column from another table.
- A measure must not contain a naked column reference outside an aggregator. Wrap
  standalone columns in SUM/MIN/MAX/SELECTEDVALUE as appropriate.
- Every VAR block MUST end with RETURN. Every SWITCH needs a default; every IF chain
  needs a final ELSE.
- Never use a DAX reserved word as a VAR name (VALUE, TABLE, FILTER, ALL, DATE,
  FORMAT, RANK, ORDER, COUNT, ...). Prefix variables with an underscore instead.
- There is no MATCH function in DAX; never emit it.
- Division must use DIVIDE(numerator, denominator, 0), never the / operator.
- Always close every parenthesis. Return a complete, syntactically valid expression.
"""


def _clamp_system(system: str) -> str:
    if len(system) > Config.LLM_SYSTEM_PROMPT_LIMIT:
        return system[: Config.LLM_SYSTEM_PROMPT_LIMIT]
    return system


def build_measure_prompts(
    name: str,
    qlik_expression: str,
    schema_context: str,
    baseline_dax: str = "",
    table_hint: str = "",
) -> Tuple[str, str]:
    """(system, user) for converting one Qlik measure to DAX.

    `baseline_dax` is the deterministic DAXConverter output. Handing it to the
    model as a draft to correct - rather than asking for a conversion from
    scratch - keeps the model anchored to a result that already parses, and
    makes its job review rather than translation.
    """
    system = _clamp_system(
        "You are an expert BI migration engineer converting Qlik Sense "
        "expressions into Power BI / Fabric DAX.\n\n"
        + _DAX_CORE_CONTRACT
        + "\nQLIK-SPECIFIC CONVERSION RULES:\n"
        + format_rules("measures")
    )

    draft_block = ""
    if baseline_dax:
        draft_block = (
            "\nDETERMINISTIC DRAFT (regex-converted; correct it, or return it "
            "unchanged if already correct):\n" + baseline_dax + "\n"
        )
    hint_block = "\nPrimary table hint: '{}'\n".format(table_hint) if table_hint else ""

    user = (
        "SCHEMA:\n" + schema_context + "\n"
        + hint_block
        + "\nMeasure name: " + str(name)
        + "\n\nQlik expression:\n" + strip_qlik_comments(qlik_expression)
        + "\n" + draft_block
        + "\nDAX expression only:"
    )
    return system, user


def build_dimension_prompts(
    name: str,
    qlik_expression: str,
    schema_context: str,
    baseline_dax: str = "",
    table_hint: str = "",
) -> Tuple[str, str]:
    """(system, user) for converting one calculated Qlik dimension into a DAX
    calculated column."""
    system = _clamp_system(
        "You are an expert BI migration engineer converting Qlik Sense "
        "calculated dimensions into Power BI / Fabric DAX calculated columns.\n\n"
        + _DAX_CORE_CONTRACT
        + "- This is a CALCULATED COLUMN, evaluated row by row - not a measure. Do NOT\n"
          "  wrap the whole expression in an aggregator, and do not reference measures.\n"
          "- Columns from the same table are referenced directly as 'Table'[Column];\n"
          "  columns from a related One-side table need RELATED().\n"
        + "\nQLIK-SPECIFIC DIMENSION RULES:\n"
        + format_rules("dimensions")
    )

    draft_block = ""
    if baseline_dax:
        draft_block = (
            "\nDETERMINISTIC DRAFT (correct it, or return unchanged if already "
            "correct):\n" + baseline_dax + "\n"
        )
    hint_block = "\nOwning table: '{}'\n".format(table_hint) if table_hint else ""

    user = (
        "SCHEMA:\n" + schema_context + "\n"
        + hint_block
        + "\nCalculated dimension name: " + str(name)
        + "\n\nQlik expression:\n" + strip_qlik_comments(qlik_expression)
        + "\n" + draft_block
        + "\nDAX calculated-column expression only:"
    )
    return system, user


def build_variable_prompts(
    variable: Dict[str, Any],
    schema_context: str,
    interactive_names: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """(system, user) for classifying one Qlik variable and converting it.

    Returns JSON rather than a bare expression, because a variable's outcome
    is a *kind* decision (reserved / literal / parameter / expression) plus
    different payloads per kind - not a single value.
    """
    system = _clamp_system(
        "You convert Qlik Sense variables into Power BI / Fabric semantic-model "
        "artifacts.\n\n"
        "A Qlik variable is NOT automatically a Power BI parameter. Decide which of the "
        "four kinds it is, then emit only the artifact that kind requires.\n\n"
        + _DAX_CORE_CONTRACT
        + "\nQLIK VARIABLE RULES:\n"
        + format_rules("variables")
    )

    bound = ", ".join(interactive_names or []) or "(none reported)"
    user = (
        "SCHEMA:\n" + schema_context
        + "\n\nVARIABLES BOUND TO AN INTERACTIVE variable-input OBJECT:\n" + bound
        + "\n\nQLIK VARIABLE:\n"
        + json.dumps(variable, indent=2, default=str)[:3000]
        + "\n\nReturn the JSON object described by the schema."
    )
    return system, user


def build_mquery_prompts(
    table: Dict[str, Any],
    baseline_mquery: str,
    connection_context: str,
    upstream_tables: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """(system, user) for reviewing one table's Power Query M expression.

    Baseline-first, like the DAX prompts: connection_mapper's deterministic
    template is the draft, and the model repairs the parts a template cannot
    express (preceding loads, resident chains, CROSSTABLE, ApplyMap).
    """
    system = _clamp_system(
        "You are an expert data engineer converting Qlik Sense load scripts into "
        "Power Query M for a Microsoft Fabric semantic model.\n\n"
        "CRITICAL OUTPUT CONTRACT:\n"
        "- Output ONLY a JSON object containing the 'm_query' key.\n"
        "- The 'm_query' value MUST be the M expression, starting with 'let' and ending with the 'in'\n"
        "  clause. No markdown, no fences, no commentary.\n"
        "- M is case-sensitive. Text.Upper is valid; TEXT.UPPER is not.\n"
        "- Never invent a server, database, schema, path or credential. Use only what\n"
        "  the CONNECTION section supplies.\n"
        "- A '$(' dollar-sign expansion is Qlik syntax and is not valid M. Never emit one.\n"
        "\nQLIK LOAD SCRIPT CONVERSION RULES:\n"
        + format_rules("mquery")
    )

    upstream = ", ".join(upstream_tables or []) or "(none)"
    user = (
        "CONNECTION:\n" + connection_context
        + "\n\nOTHER QUERIES AVAILABLE TO REFERENCE (for resident loads):\n" + upstream
        + "\n\nTABLE: " + str(table.get("name") or table.get("table_name") or "")
        + "\nLOAD TYPE: " + str(table.get("load_type") or "")
        + "\n\nQLIK LOAD STATEMENT:\n"
        + strip_qlik_comments(str(table.get("qlik_query") or ""))[:3000]
        + "\n\nDETERMINISTIC DRAFT (repair it, or return it unchanged if already correct):\n"
        + str(baseline_mquery)[:3000]
        + "\n\nReturn the JSON object described by the schema."
    )
    return system, user


def build_column_prompts(
    table_name: str,
    columns: List[Dict[str, Any]],
    sample_rows: Optional[List[Dict[str, Any]]] = None,
    app_subject: str = "",
) -> Tuple[str, str]:
    """(system, user) for typing one table's columns.

    Batched per table rather than per column: typing decisions depend on the
    surrounding columns (col3), and one call per column would be both worse
    and far more expensive.
    """
    system = _clamp_system(
        "You assign Power BI / Fabric column types, summarization behaviour and format "
        "strings to fields imported from Qlik Sense.\n\n"
        "CRITICAL OUTPUT CONTRACT:\n"
        "- Return one entry per column supplied, in the same order, using the exact\n"
        "  column names given. Never add, drop, rename or re-case a column.\n"
        "- fabric_datatype must be one of: int64, double, decimal, dateTime, boolean, string.\n"
        "- summarize_by must be one of: sum, average, count, distinctCount, min, max, none.\n"
        "\nCOLUMN TYPING RULES:\n"
        + format_rules("columns")
    )

    subject = f"\nAPP SUBJECT (context for ambiguous field names): {app_subject}\n" if app_subject else ""
    samples = ""
    if sample_rows:
        samples = (
            "\nSAMPLE ROWS (strongest evidence after an explicit type):\n"
            + json.dumps(sample_rows[:5], indent=2, default=str)[:2000]
            + "\n"
        )

    user = (
        "TABLE: " + str(table_name)
        + subject
        + "\nCOLUMNS (with whatever the engine reported):\n"
        + json.dumps(columns, indent=2, default=str)[:4000]
        + samples
        + "\nReturn the JSON object described by the schema."
    )
    return system, user


def build_visual_prompts(
    visual_payload: Dict[str, Any],
    schema_context: str,
    deterministic_type: str,
    valid_visual_types: List[str],
) -> Tuple[str, str]:
    """(system, user) for classifying one Qlik visual and binding its fields.

    Unlike the DAX prompts this one returns JSON, because a visual carries
    several independent decisions (type, per-field role, aggregation, colour)
    that downstream code consumes separately.
    """
    system = _clamp_system(
        "You convert Qlik Sense visual objects into Power BI / Fabric visual "
        "definitions.\n\n"
        "RULES:\n"
        "- `visual_type` MUST be one of the ALLOWED VISUAL TYPES listed below. If\n"
        "  nothing fits, choose the closest native type, set supported=false, and\n"
        "  give a replacement_strategy.\n"
        "- Assign every dimension and measure a role. Dimensions belong on Category\n"
        "  (or Rows / Group / Details depending on the visual); measures belong on\n"
        "  Y / Values / Size / Y2.\n"
        "- For a combo chart the first measure goes to Y and the rest to Y2.\n"
        "- For a scatter chart: measure 1 -> X, measure 2 -> Y, measure 3 -> Size.\n"
        "- Bind every field to a real entity/property from the SCHEMA. If a field\n"
        "  cannot be matched, still return it with entity set to null so it can be\n"
        "  reported as unbound - never invent a table or column name.\n"
        "- `aggregation` applies only to raw columns used as values; a field that is\n"
        "  already a measure must use aggregation \"None\".\n"
        "- Preserve colours exactly as supplied. Never invent a hex value that was\n"
        "  not present in the input.\n"
        "- Keep the supplied pixel geometry unless it is missing.\n"
        "\nQLIK-SPECIFIC VISUAL RULES:\n"
        + format_rules("dashboard_objects")
    )

    user = (
        "SCHEMA:\n" + schema_context
        + "\n\nALLOWED VISUAL TYPES:\n" + ", ".join(valid_visual_types)
        + "\n\nRULE-BASED SUGGESTION (from a lookup table; override only if clearly "
          "wrong):\n" + str(deterministic_type)
        + "\n\nQLIK VISUAL OBJECT:\n"
        + json.dumps(visual_payload, indent=2, default=str)[:6000]
        + "\n\nReturn the JSON object described by the schema."
    )
    return system, user
