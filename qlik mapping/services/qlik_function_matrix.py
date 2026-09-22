"""Qlik Function to Power Query M and DAX Semantic Translation Matrix.

Handles Qlik date/time parsers (Date#, Time#, Timestamp, AddMonths, etc.),
string functions, summarize_by heuristics, and semantic data type mappings.
"""

import re
from typing import Any, Dict, List, Optional, Tuple


class QlikFunctionMatrix:
    """Translates Qlik scalar functions to Power Query M or DAX equivalents."""

    # -------------------------------------------------------------
    # 1. Summarize-By Semantic Heuristics
    # -------------------------------------------------------------
    @staticmethod
    def infer_summarize_by(column_name: str, datatype: str) -> str:
        """Infer summarize_by property based on column name and datatype."""
        col_clean = (column_name or "").lower().strip()
        dtype = (datatype or "").lower().strip()

        # Non-numeric or boolean -> none
        if dtype in ("string", "text", "date", "datetime", "boolean", "logical"):
            return "none"

        # Identifiers, codes, keys, year, month numbers, dates, flags -> none
        if any(kw in col_clean for kw in [
            "id", "_id", "key", "_key", "code", "_cd", "num", "number", "account",
            "postal", "zip", "phone", "year", "month", "day", "quarter", "rank",
            "sequence", "seq", "flag", "status", "version"
        ]):
            # Exception for count of items or actual amounts containing num
            if not any(amt_kw in col_clean for amt_kw in ["amount", "revenue", "cost", "sales", "qty", "quantity"]):
                return "none"

        # Rates, percentages, ratios, prices, averages -> average or none
        if any(kw in col_clean for kw in ["rate", "percent", "pct", "ratio", "margin", "price", "unit_price", "discount"]):
            return "average"

        # Metrics, financial amounts, counts -> sum
        if any(kw in col_clean for kw in [
            "amount", "amt", "revenue", "rev", "cost", "sales", "qty", "quantity",
            "volume", "pnl", "profit", "loss", "balance", "total", "fee", "tax", "load"
        ]) or dtype in ("double", "int64", "decimal", "number", "numeric"):
            return "sum"

        return "none"

    # -------------------------------------------------------------
    # 2. Semantic Data Type Mapping
    # -------------------------------------------------------------
    @staticmethod
    def map_datatype(qlik_type: str, sample_val: Optional[Any] = None) -> Tuple[str, str]:
        """Map Qlik datatype to (fabric_datatype, m_type_literal)."""
        raw = str(qlik_type or "").upper().strip()

        if "INT" in raw or raw in ("INTEGER", "LONG", "BIGINT"):
            return "int64", "Int64.Type"
        elif any(kw in raw for kw in ["NUM", "DOUBLE", "FLOAT", "DECIMAL", "REAL", "CURRENCY", "MONEY"]):
            return "double", "type number"
        elif "DATETIME" in raw or "TIMESTAMP" in raw:
            return "dateTime", "type datetime"
        elif "DATE" in raw:
            return "dateTime", "type date"
        elif "TIME" in raw:
            return "string", "type time"
        elif any(kw in raw for kw in ["BOOL", "BOOLEAN", "BIT"]):
            return "boolean", "type logical"
        else:
            return "string", "type text"

    @staticmethod
    def _transform_qlik_date_time_hash(func_name: str, m_target_func: str, text: str) -> str:
        """Replace Date#(expr, fmt) or Time#(expr, fmt) with m_target_func(expr) preserving nested parens."""
        pattern = re.compile(rf"\b{re.escape(func_name)}\s*\(", re.IGNORECASE)
        while True:
            match = pattern.search(text)
            if not match:
                break
            start_idx = match.start()
            arg_start = match.end()
            depth = 1
            curr = arg_start
            comma_idx = -1
            while curr < len(text) and depth > 0:
                ch = text[curr]
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        break
                elif ch == ',' and depth == 1:
                    if comma_idx == -1:
                        comma_idx = curr
                curr += 1
            
            if depth != 0:
                break
            
            arg_end = curr
            if comma_idx != -1:
                first_arg = text[arg_start:comma_idx].strip()
            else:
                first_arg = text[arg_start:arg_end].strip()
            
            replacement = f"{m_target_func}({first_arg})"
            text = text[:start_idx] + replacement + text[arg_end + 1:]
        return text

    # -------------------------------------------------------------
    # 3. Date / Time / String Function M Translations
    # -------------------------------------------------------------
    @classmethod
    def translate_qlik_expression_to_m(cls, expr: str) -> str:
        """Convert scalar Qlik date/string expressions to Power Query M."""
        if not expr:
            return ""

        res = expr.strip()

        # Date#([col], 'YYYY-MM-DD') -> Date.FromText([col])
        res = cls._transform_qlik_date_time_hash("Date#", "Date.FromText", res)

        # Time#([col], 'hh:mm TT') -> Time.FromText([col])
        res = cls._transform_qlik_date_time_hash("Time#", "Time.FromText", res)

        # AddMonths([col], n) -> Date.AddMonths([col], n)
        res = re.sub(
            r"AddMonths\s*\(",
            r"Date.AddMonths(",
            res,
            flags=re.IGNORECASE,
        )

        # Month([col]) -> Date.Month([col])
        res = re.sub(
            r"\bMonth\s*\(",
            r"Date.Month(",
            res,
            flags=re.IGNORECASE,
        )

        # Year([col]) -> Date.Year([col])
        res = re.sub(
            r"\bYear\s*\(",
            r"Date.Year(",
            res,
            flags=re.IGNORECASE,
        )

        # SubField([col], delimiter, index) -> Text.Split([col], delimiter){index - 1}
        sub_match = re.search(r"SubField\s*\(\s*([^,]+)\s*,\s*('[^']+'|\"[^\"]+\")\s*,\s*(\d+)\s*\)", res, re.IGNORECASE)
        if sub_match:
            col_part = sub_match.group(1).strip()
            delim = sub_match.group(2)
            idx = int(sub_match.group(3)) - 1
            res = f"Text.Split({col_part}, {delim}){{{idx}}}"

        # Lower([col]) -> Text.Lower([col]), Upper([col]) -> Text.Upper([col]), Trim, Len
        res = re.sub(r"\bLower\s*\(", r"Text.Lower(", res, flags=re.IGNORECASE)
        res = re.sub(r"\bUpper\s*\(", r"Text.Upper(", res, flags=re.IGNORECASE)
        res = re.sub(r"\bTrim\s*\(", r"Text.Trim(", res, flags=re.IGNORECASE)
        res = re.sub(r"\bLen\s*\(", r"Text.Length(", res, flags=re.IGNORECASE)

        # Ensure bare column references inside function args like Text.Lower(col) become Text.Lower([col])
        def _ensure_col_brackets(m):
            fn_name = m.group(1)
            arg = m.group(2).strip()
            if arg and not arg.startswith("[") and not arg.startswith('"') and not arg.startswith("'") and not re.match(r"^\d", arg):
                arg = f"[{arg}]"
            return f"{fn_name}({arg})"

        res = re.sub(r"\b(Text\.Lower|Text\.Upper|Text\.Trim|Text\.Length|Date\.FromText|Time\.FromText)\s*\(\s*([A-Za-z0-9_#]+)\s*\)", _ensure_col_brackets, res)

        # If bare column identifier without brackets
        if re.match(r"^[A-Za-z_][A-Za-z0-9_#]*$", res):
            res = f"[{res}]"

        # If(cond, then, else) -> if cond then val1 else val2
        if_match = re.match(r"^\s*If\s*\((.*)\)\s*$", res, re.IGNORECASE | re.DOTALL)
        if if_match:
            args = if_match.group(1).split(",")
            if len(args) == 3:
                cond = args[0].strip()
                then_b = args[1].strip()
                else_b = args[2].strip()
                return f"if {cond} then {then_b} else {else_b}"

        return res

    @staticmethod
    def translate_qlik_expression_to_dax(expr: str) -> str:
        """Convert scalar Qlik date/string expressions to DAX."""
        if not expr:
            return ""
        res = expr.strip()
        # Date#([col], 'YYYY-MM-DD') -> DATEVALUE([col])
        res = re.sub(
            r"Date#\s*\(\s*(\[[^\]]+\]|[A-Za-z0-9_]+)\s*(?:,\s*'[^']*')?\s*\)",
            r"DATEVALUE(\1)",
            res,
            flags=re.IGNORECASE,
        )
        # Time#([col], 'hh:mm TT') -> TIMEVALUE([col])
        res = re.sub(
            r"Time#\s*\(\s*(\[[^\]]+\]|[A-Za-z0-9_]+)\s*(?:,\s*'[^']*')?\s*\)",
            r"TIMEVALUE(\1)",
            res,
            flags=re.IGNORECASE,
        )
        # AddMonths([col], n) -> EDATE([col], n)
        res = re.sub(
            r"AddMonths\s*\(",
            r"EDATE(",
            res,
            flags=re.IGNORECASE,
        )
        # Month([col]) -> MONTH([col])
        res = re.sub(r"\bMonth\s*\(", r"MONTH(", res, flags=re.IGNORECASE)
        # Year([col]) -> YEAR([col])
        res = re.sub(r"\bYear\s*\(", r"YEAR(", res, flags=re.IGNORECASE)
        # Len([col]) -> LEN([col])
        res = re.sub(r"\bLen\s*\(", r"LEN(", res, flags=re.IGNORECASE)
        return res

    @classmethod
    def qlik_date_to_m(cls, expr: str) -> str:
        return cls.translate_qlik_expression_to_m(expr)

    @classmethod
    def qlik_date_to_dax(cls, expr: str) -> str:
        return cls.translate_qlik_expression_to_dax(expr)


