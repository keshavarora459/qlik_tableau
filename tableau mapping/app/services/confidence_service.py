# app/services/confidence_service.py

import json
import logging
import random
import time

from app.services.llm_service import call_llm

logger = logging.getLogger(__name__)

# Maximum retries for confidence evaluation: single attempt to avoid blocking
_CONFIDENCE_MAX_RETRIES = 1


def evaluate_dax_confidence(
    tableau_formula: str,
    dax_formula: str,
    schema_context: str = "",
    calc_type: str = "measure",
    connection_type: str = "",
    calc_name: str = ""
) -> dict:
    """
    Uses an LLM call to validate a generated DAX formula
    and return a confidence score (0-100%) with review notes.
    Fails fast with heuristic scoring if LLM is rate-limited or unavailable.

    Returns:
        {
            "confidence_score": int (0-100),
            "review_notes": str
        }
    """

    # Skip evaluation for error formulas or skipped formulas
    if not dax_formula or dax_formula.startswith("-- ERROR") or dax_formula.startswith("-- SKIPPED"):
        return {
            "confidence_score": 0 if not dax_formula or dax_formula.startswith("-- ERROR") else 100,
            "review_notes": "DAX generation failed — no formula to evaluate." if not dax_formula or dax_formula.startswith("-- ERROR") else "Skipped LLM conversion"
        }

    # Build prompts
    system_prompt, user_prompt = _build_confidence_prompts(
        calc_type, tableau_formula, dax_formula, schema_context, connection_type, calc_name
    )

    try:
        raw_response = call_llm(
            system_prompts=system_prompt,
            user_prompt=user_prompt,
            retries=0,  # fail fast, no redundant retries
        )
        return _parse_confidence_response(raw_response)

    except Exception as e:
        logger.warning(
            f"Confidence evaluation LLM call skipped/failed ({type(e).__name__}: {e}); using heuristic fallback."
        )
        # Graceful heuristic fallback: non-empty valid draft gets 85 score
        return {
            "confidence_score": 85 if (dax_formula and not dax_formula.startswith("-- ERROR")) else 0,
            "review_notes": f"Automated confidence assessment (fallback: {type(e).__name__})"
        }



def _build_confidence_prompts(calc_type, tableau_formula, dax_formula, schema_context, connection_type="", calc_name=""):
    """Builds the system and user prompts for confidence evaluation."""

    # Default prompts for DAX
    system_prompt = """You are an expert Power BI DAX code reviewer.

Your task is to evaluate a DAX formula that was auto-generated from a Tableau formula.
You are a fair grader. Start at 100 and deduct points reasonably for any syntax or severe logic errors found below. Do not be overly harsh if the DAX relies on standard Power BI data model features (e.g., implicit type conversion, indirect relationships).

EVALUATION CRITERIA (Deduct points for each violation):
1. SYNTAX: Is the DAX syntactically valid? Are all parentheses closed? Is VAR/RETURN correct?
   - CRITICAL SYNTAX ERROR: If the DAX contains `RELATEDTABLE('Table')[Column]` (appending a column directly to a table function), this is FATALLY invalid DAX. Deduct 80 points!
   - INVALID FUNCTION NAMES: DAX does NOT support the function name `STDEV` (must be `STDEV.S` or `STDEV.P`). Using `VAR` as an aggregator function is a fatal crash because `VAR` is reserved for declaring variables (must be `VAR.S` or `VAR.P`). If either is used, deduct 80 points!
2. COLUMN REFERENCES: Do table/column references match the provided schema context?
3. FUNCTION MAPPING: Are Tableau functions correctly mapped to DAX equivalents?
   - EXPLICIT CASTING: If the Tableau formula explicitly casts data types (e.g., DATEPARSE, INT, FLOAT) and DAX drops them, do NOT deduct points. Power BI handles data types in the model, so dropping DATEVALUE or similar functions is often correct.
   - LOOKUP → Temporal shifts must use CALCULATE+FILTER+ALL, Categorical shifts must use OFFSET.
4. AGGREGATION CONTEXT & CROSS-TABLE:
   - Inside Measures: Are naked columns properly wrapped in aggregators? (Deduct 30 points if naked)
   - CROSS-TABLE INVALID RELATED(): If DAX uses `RELATED('ChildTable'[Column])` inside an aggregation on a Parent table (One-to-Many), deduct 60 points! `RELATED()` only works Many-to-One.
   - Inside Dimensions: If referencing a different table, it MUST use RELATED() for Many-to-One, or MAXX(RELATEDTABLE()) for One-to-Many.
5. SEMANTIC ACCURACY: Does the DAX logic preserve the intent of the original Tableau formula?
   - LOD CALCULATIONS: If a FIXED LOD uses ALLEXCEPT or REMOVEFILTERS, do NOT penalize heavily (deduct max 10 points) for indirect relationships as long as the DAX syntax is valid.
   - LOD CONSISTENCY: Simple FIXED LODs on the same grain (e.g., High, Low, Avg) must be structurally consistent. Do not mix ALLEXCEPT and SUMX patterns in the same workbook. If any of them require SUMX (like Avg), all of them must use SUMX. Deduct 30 points for mixed patterns.
6. TIME INTELLIGENCE GRAIN VALIDATION (CRITICAL):
   a) YEAR-LEVEL indicators (Name contains 'PY', 'LY', 'YoY'):
      - VALID: FILTER checks YEAR() ONLY. No EDATE().
      - INVALID (Deduct 60 points): FILTER includes && MONTH() or uses EDATE().
   b) MONTH-LEVEL indicators (Name contains 'PM', 'LM', 'MoM'):
      - VALID: Uses EDATE() and FILTER checks BOTH YEAR() AND MONTH().
      - INVALID (Deduct 60 points): FILTER checks YEAR() only, or missing EDATE().
   c) COALESCE WRAPPER: Missing COALESCE(..., 0) on temporal LOOKUP measures (Deduct 10 points).

RESPONSE FORMAT — Return ONLY a valid JSON object, no markdown, no explanation:
{"confidence_score": <integer 0-100>, "review_notes": "<brief explanation of any deductions>"}

SCORING GUIDE:
- 90-100: Flawless, perfectly correct DAX. Minor logic shifts acceptable.
- 70-89: Minor cosmetic issues (e.g., missing COALESCE, slight LOD structural mismatch).
- 50-69: Has structural issues but core logic is sound.
- 30-49: Invalid cross-table references.
- 0-29: Fundamentally broken syntax (like RELATEDTABLE()[Col]), hallucinated DAX, or severe logic mismatches."""

    user_prompt_template = """Evaluate this DAX conversion:

Calculation Name: {calc_name}
Calculation Type: {calc_type}
{schema_info}

ORIGINAL TABLEAU FORMULA:
{tableau_formula}

GENERATED DAX FORMULA:
{dax_formula}

Return ONLY the JSON evaluation:"""

    # Override prompts for M Query (datasource-aware)
    if calc_type in ["sql_to_m_query", "parameterized_sql_to_m_query"]:
        # Determine the expected connector for validation
        from app.services.custom_sql_service import _get_m_connector
        expected_connector = _get_m_connector(connection_type)
        ds_label = (connection_type or "SQL").replace("_", " ").title()

        # Snowflake needs drill-down validation
        key = (connection_type or "").lower().strip()
        if key == "snowflake":
            connection_rule = (
                "3. CONNECTION: Does it use `Snowflake.Databases(server, warehouse)` "
                "followed by drilling into the database with `Source{[Name=\"...\"]}[Data]` "
                "and then `Value.NativeQuery` on the database reference (NOT on Source directly)?"
            )
        else:
            connection_rule = (
                f"3. CONNECTION: Does it use `{expected_connector}(server, database)` "
                f"correctly in the Source step?"
            )

        system_prompt = f"""You are a senior Power BI M Query code reviewer.

Your task is to evaluate an M Query script that was auto-generated to import and wrap a {ds_label} query.

EVALUATION CRITERIA:
1. SYNTAX: Is the M Query syntactically valid? Does it use `let ... in ...` correctly?
2. NATIVE QUERY: Does it use `Value.NativeQuery(...)` correctly?
{connection_rule}
4. ESCAPING: Are quotes properly escaped inside the native SQL string?
5. SEMANTIC ACCURACY: Does the M logic cleanly preserve and execute the original SQL?

RESPONSE FORMAT — Return ONLY a valid JSON object, no markdown, no explanation:
{{"confidence_score": <integer 0-100>, "review_notes": "<brief explanation of any issues or confirmation of correctness>"}}

SCORING GUIDE:
- 90-100: Perfect or near-perfect M Query script
- 70-89: Minor cosmetic issues but functionally correct
- 50-69: Has issues but core logic is sound
- 30-49: Significant issues likely to produce a syntax error in Power Query
- 0-29: Fundamentally broken, missing let/in, or completely incorrect"""

        user_prompt_template = """Evaluate this M Query conversion:

Calculation Type: {calc_type}
Datasource Type: """ + ds_label + """

ORIGINAL SQL QUERY:
{tableau_formula}

GENERATED M QUERY SCRIPT:
{dax_formula}

Return ONLY the JSON evaluation:"""

    schema_info = f"\n\nAVAILABLE SCHEMA:\n{schema_context}" if schema_context else ""

    user_prompt = user_prompt_template.format(
        calc_name=calc_name or "Unknown",
        calc_type=calc_type,
        schema_info=schema_info,
        tableau_formula=tableau_formula,
        dax_formula=dax_formula
    )

    return system_prompt, user_prompt


def _parse_confidence_response(raw_response: str) -> dict:
    """Parses the LLM confidence response into a structured dict."""
    try:
        # Clean potential markdown code blocks
        clean_json = raw_response.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean_json)

        score = parsed.get("confidence_score", -1)
        notes = parsed.get("review_notes", "")

        # Clamp score to 0-100 range
        if isinstance(score, (int, float)):
            score = max(0, min(100, int(score)))
        else:
            score = -1

        return {
            "confidence_score": score,
            "review_notes": str(notes)
        }

    except (json.JSONDecodeError, ValueError):
        logger.warning(f"Confidence LLM returned non-JSON: {raw_response[:200]}")
        return {
            "confidence_score": -1,
            "review_notes": f"Evaluation returned non-JSON response: {raw_response[:200]}"
        }
