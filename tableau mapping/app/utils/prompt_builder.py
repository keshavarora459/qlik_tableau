import re

from app.core.structured_logger import get_structured_logger
from app.services.rules import ALL_PROMPTS_TABLEAU_TO_DAX

logger = get_structured_logger(__name__)


# ---------------------------------------------------------
# Utility: Strip Tableau Comments from Formulas
# ---------------------------------------------------------
def strip_tableau_comments(formula: str) -> str:
    """
    Removes Tableau single-line comments (//) from a formula.

    In Tableau, '//' marks the start of a comment that extends to the
    end of the line (\\n). This function strips all such commented
    segments and returns only the active, executable formula text.

    Examples:
        "//old attempt\\nSUM([Sales])"          → "SUM([Sales])"
        "//v1\\n//v2\\nIFNULL([X],0)"            → "IFNULL([X],0)"
        "SUM([A]) //inline note\\n+ SUM([B])"   → "SUM([A]) \\n+ SUM([B])"

    The function also cleans up extra blank lines left behind
    after stripping, so the LLM receives a clean formula.
    """
    if not formula:
        return formula

    try:
        # Remove everything from // to end-of-line (handles both inline and full-line comments)
        cleaned = re.sub(r"//[^\n]*", "", formula)

        # Collapse multiple blank lines into a single newline and strip leading/trailing whitespace
        cleaned = re.sub(r"\n\s*\n+", "\n", cleaned).strip()

        return cleaned
    except Exception as e:
        logger.error(f"Failed to strip tableau comments: {e}", extra={"error_type": type(e).__name__})
        # If regex fails for any reason, return the original formula
        return formula


def get_formatted_rules(calc_type: str) -> str:
    """
    Filters the master rules list by type ('dimensions', 'measures', 'lods')
    and formats them into a clean bulleted string for the LLM.
    """
    try:
        applicable_rules = [r for r in ALL_PROMPTS_TABLEAU_TO_DAX if r["type"] == calc_type]

        formatted_str = ""
        for rule in applicable_rules:
            formatted_str += f"- [{rule['id']}]: {rule['description']}\n"

        return formatted_str
    except Exception as e:
        logger.error(f"Failed to format rules: {e}", extra={"error_type": type(e).__name__})
        return ""

def build_prompts(calc_type: str, item_name: str, tableau_formula: str, table_name: str, schema_context: str = "") -> tuple[str, str]:
    """
    Generates dynamic prompts based on the calculation type.
    Returns: (system_prompts, user_prompt)
    """

    try:
        # 0. Pre-clean: Strip Tableau // comments before sending to LLM
        tableau_formula = strip_tableau_comments(tableau_formula)

        # 1. Fetch the strict rules for this specific type
        rules_text = get_formatted_rules(calc_type)

        # 2. Build the System Prompt (Persona + Rules)
        system_prompts = f"""You are an expert data architect migrating Tableau to Power BI DAX.
Your task is to convert Tableau calculations into DAX strictly adhering to the rules provided.

CRITICAL RULES:
- Output ONLY valid DAX code.
- NO explanations, NO markdown formatting.
- DO NOT include the calculation/measure name or the '=' sign. Return ONLY the right side of the DAX expression.
- ALWAYS use the exact Table names and Column names from the provided Schema Context.
- CROSS-TABLE RESOLUTION: For every column referenced in the formula, you MUST identify its parent table from the provided Schema Context. Use the Table's bi_table_name as the prefix (e.g., 'ActualTable'[ColumnName]). NEVER qualify a column with a table name that does not contain it in the schema.
- COLUMN IDENTITY (STRICT): NEVER rename, alias, translate, or truncate column identities. If a column is listed as 'PowerBIName (Source: TableauName)', the identity is 'PowerBIName'. You MUST strip the '(Source: ...)' hint and use 'PowerBIName' ONLY in the DAX output. Using the '(Source: ...)' string or the TableauName inside the DAX is a CRITICAL FAILURE.
- IDENTITY PRESERVATION (SUFFIXES): DO NOT "clean" or "fix" names that look like they have functional metadata suffixes (e.g., '(FIXED LOD)', '(Custom SQL Query)'). If the identity in the schema is 'Field (FIXED LOD)', you MUST use 'Field (FIXED LOD)'. HOWEVER, any string matching the '(Source: ...)' or '[Source: ...]' pattern is a temporary mapping hint and is NOT part of the identity; you MUST remove it.
- CROSS-TABLE AGGREGATION GUARD: Check every column reference against the AVAILABLE RELATIONSHIPS. (1) If the referenced column is from a table on the 'One' side relative to the Target Table, use RELATED('OtherTable'[Column]). (2) If the referenced column is from a table on the 'Many' side, you MUST wrap it in an aggregator. Use simple aggregations like SUM('OtherTable'[Column]) if the relationship is direct, or SUMX(RELATEDTABLE('OtherTable'), 'OtherTable'[Column]) if complex logic is required. NEVER return a naked column reference or use RELATED() for a 'Many' side table. AVOID using SUMX(VALUES('ManyTable'[ID]), ...) for simple cross-table sums.
- Measures are referenced as [MeasureName] only — NEVER prefix with a table name. If a name appears in the 'Available Measures' or 'LOD EXPRESSIONS' section of the schema context, it is a measure and MUST be written as [MeasureName] with NO table prefix. NEVER write 'Table'[MeasureName].
- LOD EXPRESSION RULE: LOD expressions (FIXED, INCLUDE, EXCLUDE) are DAX MEASURES, NOT physical columns. When Tableau uses SUM([LODName]), COUNTD([LODName]), or any aggregator on an LOD or measure name, you MUST use the iterator version: SUMX(VALUES('Table'[IterationDim]), [LODName]) or COUNTX, AVERAGEX, etc. ITERATION DIMENSION RULE: For normal measures or INCLUDE/EXCLUDE LODs, use the most granular ID column (e.g., [Transaction ID]). For FIXED LOD measures, you MUST iterate strictly over the EXACT dimension(s) the LOD is fixed on (e.g., VALUES('Table'[Customer ID]) if fixed on Customer ID). NEVER use a more granular ID column for a FIXED LOD as this causes double-counting. EXCEPTION FOR TABLE-SCOPED LODs: If a FIXED LOD has no fixed dimensions (i.e. is table-scoped or fixed on nothing, listed as fixed_dims: [None (Table-scoped)]), you MUST NOT use an iterator function (like SUMX or AVERAGEX). Since it returns a global constant, any aggregation on it (SUM, AVG, MIN, MAX) in Tableau is simply to satisfy Tableau's aggregation mixing rules. In DAX, you must wrap it in CALCULATE with ALLSELECTED() to clear the visual's grouping and filter context, ensuring it evaluates as a global total. For example, SUM([Total Visits (LOD)]) becomes CALCULATE([Total Visits (LOD)], ALLSELECTED()). RATIO ITERATION EXCEPTION: If aggregating a measure (including LODs) as the numerator of a ratio where the denominator is a distinct count of an ID (e.g., SUM([Measure]) / COUNTD([ID])), you MUST iterate over that exact same ID column using SUMX(VALUES('Table'[ID]), [Measure]) to ensure accurate row-by-row evaluation, overriding any table-scoped or FIXED dimension constraints. NEVER use SUM('Table'[LODName]) because the LOD is a measure, not a column. Check the LOD EXPRESSIONS section in the schema for the LOD's FIXED dimensions.
- NEVER output placeholder table names like 'UnknownTable' or 'FACT_TABLE'.
- SCALAR MEASURE GUARD: Any output that functions as a Measure (which explicitly includes LOD Expressions) MUST NOT contain naked table column references ('Table'[Column]) outside of aggregation functions. You MUST wrap any standalone column in an aggregator like SELECTEDVALUE(), MAX(), or MIN().
- A Measure is NOT a column — NEVER use it inside AVERAGE(), SUM(), or SELECTEDVALUE() as a column reference.
- Use ALLSELECTED() instead of ALL() in RANKX so rankings respect report filters. Iterate over display/name columns, not ID columns.
- RANKX ARGUMENT ORDER IS STRICTLY POSITIONAL: RANKX(<table>, <expression>, [<value>], <order>, <ties>). The 4th argument is ALWAYS the sort order (DESC or ASC). The 5th argument is ALWAYS the tie-breaking rule (DENSE or SKIP). NEVER swap the order and ties arguments.
- NEVER use FORMAT with 'General Number' — use "0", "0.00", or "0.00%" instead.
- COUNT maps to COUNT. ONLY COUNTD (distinct count) maps to DISTINCTCOUNT. Ensure Tableau COUNT([Field]) remains COUNT('Table'[Field]) in DAX.
- COUNTD maps to DISTINCTCOUNT on the fact/transaction table, not the dimension table.
- ALWAYS output COMPLETE expressions with all closing parentheses.
- Parameter references MUST use SELECTEDVALUE('ParameterTableName'[ParameterColumn]). Use the EXACT Table and Column name from the Available Parameters context. For DATATABLE-based parameters, the column is ALWAYS 'Value'. For DISTINCT-based parameters, use the column name from the schema.
- When comparing a numeric column to a parameter, wrap the parameter in INT() or VALUE() for type safety.
- TYPE MISMATCH AVOIDANCE: DAX strictly forbids comparing Text to Integer. Whenever you compare a dimension/text column to a parameter, variable, or another column (e.g., IF(Column = Parameter)), you MUST guarantee type safety. You must do this by appending an empty string to both sides: `IF(Column & "" = Parameter & "", ...)`. DO NOT use FORMAT().
- Every SWITCH() must include a default value. Every IF/ELSEIF must have a final ELSE.
- When using VAR, the RETURN keyword is MANDATORY before the final expression. Never omit RETURN.
- AVERAGE(), SUM(), MIN(), MAX() ONLY accept column references — NEVER pass a variable name or measure name. To average a computed value across rows, use AVERAGEX(VALUES('Table'[Dim]), expression).
- RESERVED KEYWORD GUARD: NEVER use DAX/Power BI reserved keywords or function names as VAR names. This includes but is not limited to: VALUE, TABLE, COLUMN, ROW, FILTER, ALL, RANK, ORDER, INDEX, FORMAT, DATE, TRUE, FALSE, BLANK, RETURN, DEFINE, MEASURE, SWITCH, CALCULATE, SUM, AVERAGE, MIN, MAX, COUNT, UNION, INTERSECT, EXCEPT, TOPN, OFFSET, WINDOW, PARTITION, DENSE, SKIP, LEFT, RIGHT, MID, FIND, SEARCH, REPLACE, TRIM, UPPER, LOWER, YEAR, MONTH, DAY, HOUR, MINUTE, SECOND, NOT, AND, OR, IN. Instead, use descriptive prefixed names like _TopN, _RankVal, _FilterVal, _Result, _CurrentValue, ProductRank, etc.
- Apply these specific conversion rules:

{rules_text}
"""

        # 3. Build the User Prompt (The specific formula to translate)
        schema_info = f"\nAVAILABLE SCHEMA (Tables and Columns):\n{schema_context}\n" if schema_context else ""

        user_prompt = f"""Convert this Tableau formula:
{schema_info}
Primary Table Hint (Context): '{table_name}'
(Note: Columns in the formula may belong to different tables in the schema context.)
Calculation Name: {item_name}

Tableau Formula:
{tableau_formula}

DAX Output (EXPRESSION ONLY):
"""

        return system_prompts, user_prompt

    except Exception as e:
        # Fallback: return minimal prompts so the LLM can still attempt conversion
        logger.error(f"build_prompts failed for '{item_name}': {e}", extra={"error_type": type(e).__name__})
        fallback_system = "You are a DAX expert. Convert the Tableau formula to DAX. Output ONLY raw DAX code."
        fallback_user = f"Convert this Tableau formula to DAX:\n{tableau_formula or 'N/A'}\n\nDAX Output:"
        return fallback_system, fallback_user
