"""Translate Qlik expressions into Power BI DAX.

The previous `_wrap_column_refs` looped over every known table and re-ran a
`\\bcolumn\\b` substitution for each one. Its only guard was
`f"'{tname}'[{c}]" not in expr`, which checks the *current* table, so a second
table sharing a column name matched the identifier **inside** the reference
already written and wrapped it again:

    revenue
    -> 'Loads'[revenue]                       (table Loads)
    -> 'Loads'['Loads-13'[revenue]]           (table Loads-13, same column)

Applied twice — once inside the Sum/Avg/Count substitution and once over the
whole expression — that produced four levels of nesting and invalidated 27 of
29 measures on a real app.

The rewrite resolves each column to exactly one table through an index built
once, and substitutes in a single pass that never revisits text it has
already written.
"""

import re
from typing import Any, Dict, List, Optional

from .confidence_evaluator import ConfidenceEvaluator
from .dax_identifiers import IDENTIFIER, RESERVED, SINGLE_QUOTED, DOUBLE_QUOTED, QUALIFIED_REF

# Any already-bracketed reference: a measure alias like [Total Cost], or a
# column reference the caller already qualified. Protected wholesale during
# qualification so nothing is rewritten inside the brackets.
BRACKETED_REF = re.compile(r"\[[^\]\[]*\]")
from .dax_identifiers import build_column_index as _build_column_index
from .qlik_patterns import (
    strip_num,
    translate_aggr,
    translate_applymap,
    translate_pick,
    translate_match,
    translate_rangesum,
    translate_set_analysis,
    translate_total_modifiers,
)
from .tmdl_generator import TMDLGenerator
from .validators.dax_validators import run_dax_validators


class DAXConverter:
    """Translates Qlik Sense expressions to Power BI DAX."""

    def __init__(self):
        self.tmdl_gen = TMDLGenerator()
        self.confidence_eval = ConfidenceEvaluator()
        # Set by qlik_to_dax when Num() carried a format string.
        self.last_format: Optional[str] = None
        self.unresolved_columns: List[str] = []
        self.column_mapping: Dict[str, Any] = {}
        self.function_mapping: Dict[str, str] = {}

    # -- column index ------------------------------------------------------

    @staticmethod
    def build_column_index(known_tables: List[Dict[str, Any]]) -> Dict[str, str]:
        """column name -> owning table, first table wins.

        Qlik apps routinely carry near-duplicate tables (`Loads`, `Loads-13`,
        `Loads_Raw`). A column must resolve to exactly one of them or the
        reference is ambiguous, so the first occurrence is authoritative.
        Delegates to services.dax_identifiers so ConfidenceEvaluator can run
        the identical resolution without importing DAXConverter (which would
        be circular, since this module imports ConfidenceEvaluator).
        """
        return _build_column_index(known_tables)

    # -- expression translation -------------------------------------------

    def qualify_columns(self, expr: str, index: Dict[str, str]) -> str:
        """Qualify bare column names in one pass.

        String literals are stashed first so their contents are never treated
        as identifiers, and the identifier pattern refuses to match inside an
        existing `Table[Column]` reference — which is what allowed the old
        version to nest.
        """
        if not expr:
            return ""

        qualified_refs: List[str] = []

        def stash_qualified(match: re.Match) -> str:
            qualified_refs.append(match.group(0))
            return f"\x02{len(qualified_refs) - 1}\x02"

        working = QUALIFIED_REF.sub(stash_qualified, expr)

        literals: List[str] = []

        def stash_literal(match: re.Match) -> str:
            literals.append(match.group(0))
            return f"\x00{len(literals) - 1}\x00"

        working = DOUBLE_QUOTED.sub(stash_literal, working)
        working = SINGLE_QUOTED.sub(stash_literal, working)

        brackets: List[str] = []

        def stash_bracket(match: re.Match) -> str:
            token = match.group(0)
            inner = token[1:-1]
            if inner.lower() in index:
                table = index[inner.lower()]
                resolved_token = f"'{table}'[{inner}]"
                self.column_mapping[inner] = {
                    "table": table,
                    "column": inner,
                    "dax_reference": resolved_token
                }
                brackets.append(resolved_token)
            else:
                # If it's a bracketed reference that isn't in our column index, it might be
                # a measure reference like [Total Revenue]. We only treat it as an unresolved 
                # physical column if it has no spaces or hyphens, matching the DAX validator heuristic.
                if not any(c in inner for c in (" ", "-")) and not inner.startswith("@"):
                    self.unresolved_columns.append(inner)
                brackets.append(token)
            return f"\x01{len(brackets) - 1}\x01"

        working = BRACKETED_REF.sub(stash_bracket, working)

        SYNTAX_KEYWORDS = {
            "and", "or", "not", "in", "true", "false", "blank", "var", "return", "then", "else"
        }

        def qualify(match: re.Match) -> str:
            token = match.group(1)
            if token.lower() in SYNTAX_KEYWORDS or token.startswith("__CalcVar_"):
                return token
            table = index.get(token.lower())
            if table:
                resolved_token = f"'{table}'[{token}]"
                self.column_mapping[token] = {
                    "table": table,
                    "column": token,
                    "dax_reference": resolved_token
                }
                return resolved_token
            
            # Unqualified bare identifier that is not in the syntax keywords and not in the index.
            self.unresolved_columns.append(token)
            return token

        working = IDENTIFIER.sub(qualify, working)

        for position, bracket in enumerate(brackets):
            working = working.replace(f"\x01{position}\x01", bracket)

        for position, qref in enumerate(qualified_refs):
            working = working.replace(f"\x02{position}\x02", qref)

        for position, literal in enumerate(literals):
            working = working.replace(f"\x00{position}\x00", literal)
        return working

    def qlik_to_dax(
        self,
        qlik_expr: str,
        known_tables: List[Dict[str, Any]],
        relationships: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Convert a Qlik expression to DAX.

        Columns are qualified exactly once, up front, so no later rewrite can
        touch an already-written reference.
        """
        self.unresolved_columns = []
        self.column_mapping = {}
        self.function_mapping = {}

        if not qlik_expr or not str(qlik_expr).strip():
            return "BLANK()"

        index = self.build_column_index(known_tables)
        table_resolver = lambda ref: self._table_of(ref, index)

        # Qlik Num() formatting wrapper must be stripped before processing expressions
        dax, self.last_format, _ = strip_num(str(qlik_expr).strip())

        # 0. Translate Qlik $() expressions into DAX VAR
        from .variable_expander import _find_expansions
        spans = _find_expansions(dax)
        self.unconverted_qlik_syntax = getattr(self, 'unconverted_qlik_syntax', [])
        
        dax_vars = []
        if spans:
            for i, (start, end, inner) in enumerate(sorted(spans, key=lambda s: s[0], reverse=True)):
                token = inner.strip()
                if token.startswith("="):
                    calc_expr = token[1:].strip()
                    sub_converter = DAXConverter()
                    converted_inner = sub_converter.qlik_to_dax(calc_expr, known_tables, relationships)
                    self.unresolved_columns.extend(sub_converter.unresolved_columns)
                    self.column_mapping.update(sub_converter.column_mapping)
                    self.function_mapping.update(sub_converter.function_mapping)
                    
                    var_name = f"__CalcVar_{len(spans)-i}"
                    dax_vars.insert(0, (var_name, converted_inner))
                    dax = dax[:start] + var_name + dax[end:]
                else:
                    name = token.strip()
                    paren = name.find("(")
                    if paren != -1 and name.endswith(")"):
                        name = name[:paren].strip()
                    self.unconverted_qlik_syntax.append(f"$({name})")

        # 1. Translate Qlik Match, Pick and ApplyMap constructs
        dax, changed = translate_match(dax)
        if changed:
            self.function_mapping["MATCH"] = "SWITCH"
            
        dax, changed = translate_pick(dax)
        if changed:
            self.function_mapping["PICK"] = "SWITCH"
            
        dax, changed = translate_applymap(dax, known_tables)
        if changed:
            self.function_mapping["APPLYMAP"] = "LOOKUPVALUE"

        # 2. Translate Qlik TOTAL modifiers (<Dim1, Dim2> and plain TOTAL)
        dax, _ = translate_total_modifiers(dax, table_resolver, known_tables)

        # 3. Translate Qlik-specific set analysis (with AST) and rangesum before qualifying columns
        dax, _ = translate_set_analysis(dax, table_resolver, known_tables)
        dax, _ = translate_rangesum(dax)

        # 4. Distinct counts and standard aggregation names
        dax = re.sub(r"\bCount\s*\(\s*distinct\s+", "DISTINCTCOUNT(", dax, flags=re.IGNORECASE)
        dax = re.sub(r"\bSum\s*\(", "SUM(", dax, flags=re.IGNORECASE)
        dax = re.sub(r"\bAvg\s*\(", "AVERAGE(", dax, flags=re.IGNORECASE)
        dax = re.sub(r"\bCount\s*\(", "COUNT(", dax, flags=re.IGNORECASE)
        dax = re.sub(r"\bMin\s*\(", "MIN(", dax, flags=re.IGNORECASE)
        dax = re.sub(r"\bMax\s*\(", "MAX(", dax, flags=re.IGNORECASE)
        dax = re.sub(r"\bFabs\s*\(", "ABS(", dax, flags=re.IGNORECASE)

        # 5. Qualify bare columns
        dax = self.qualify_columns(dax, index)

        # 6. Aggr and Top-level divide
        dax, aggr_changed = translate_aggr(dax, table_resolver, known_tables, relationships)
        if aggr_changed:
            self.function_mapping["AGGR"] = "SUMX/AVERAGEX/MAXX/MINX/SUMMARIZE as appropriate"
        dax = self._to_divide(dax)

        # 7. Sanitize empty or malformed table prefixes
        dax = re.sub(r"'\s*'\[([^\]]+)\]", r"[\1]", dax)
        dax = re.sub(r"''\[([^\]]+)\]", r"[\1]", dax)

        if dax_vars:
            var_lines = []
            for var_name, var_expr in dax_vars:
                var_lines.append(f"VAR {var_name} = {var_expr}")
            var_lines.append(f"RETURN {dax}")
            dax = "\n".join(var_lines)

        return dax

    @staticmethod
    def _table_of(reference: str, index: Dict[str, str]) -> Optional[str]:
        """Owning table for a reference like `'Customers'[customer_name]`."""
        reference = (reference or "").strip()
        qualified = re.match(r"'([^']+)'\[", reference)
        if qualified:
            return qualified.group(1)
        return index.get(reference.strip("[]").lower())

    @staticmethod
    def _to_divide(dax: str) -> str:
        """`a / b` -> DIVIDE(a, b, 0), but only at the top level.

        Splitting on a `/` inside brackets would tear an expression in half,
        so the split point is found by tracking depth.
        """
        if dax.startswith("DIVIDE") or "/" not in dax:
            return dax

        depth = 0
        for position, char in enumerate(dax):
            if char in "([":
                depth += 1
            elif char in ")]":
                depth -= 1
            elif char == "/" and depth == 0:
                left = dax[:position].strip()
                right = dax[position + 1:].strip()
                if left and right:
                    return f"DIVIDE({left}, {right}, 0)"
                return dax
        return dax

    # -- measures ----------------------------------------------------------

    def convert_measure(
        self,
        m_item: Dict[str, Any],
        known_tables: List[Dict[str, Any]],
        relationships: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        name = m_item.get("name") or m_item.get("qlik_name") or "Measure"
        original_qlik_expr = m_item.get("expression") or m_item.get("qlik_expression") or ""
        qlik_expr = original_qlik_expr
        
        # Generic fixes for Qlik syntax
        qlik_expr = re.sub(r"\bIf\s*\(", "IF(", qlik_expr, flags=re.IGNORECASE)
        qlik_expr = re.sub(
            r"\bMax\s*\(\s*\{1\}\s*TOTAL\s+([A-Za-z0-9_]+)\s*\)",
            r"CALCULATE(MAX([\1]), ALL())",
            qlik_expr,
            flags=re.IGNORECASE
        )
        qlik_expr = re.sub(
            r"\bIsNum\s*\(\s*([A-Za-z0-9_]+)\s*\)", 
            r"NOT ISERROR(VALUE(\1))", 
            qlik_expr, 
            flags=re.IGNORECASE
        )
        qlik_expr = re.sub(r"\bRangeMax\s*\(", "MAX(", qlik_expr, flags=re.IGNORECASE)
        qlik_expr = re.sub(r"\bRangeMin\s*\(", "MIN(", qlik_expr, flags=re.IGNORECASE)

        qfmt = m_item.get("qlik_number_format") or m_item.get("number_format") or {}
        self.last_format = None
        dax_expr = self.qlik_to_dax(qlik_expr, known_tables, relationships)
        # A format lifted out of Num() is more specific than the app default.
        fmt_str = self.last_format or self.extract_format_string(qfmt)

        fabric_meta = self.tmdl_gen.generate_measure_tmdl(name, dax_expr, fmt_str)
        # Pass the original qlik expression to confidence evaluator!
        conf = self.confidence_eval.evaluate_measure(original_qlik_expr, dax_expr, known_tables)

        validation = run_dax_validators(fabric_meta, tables=known_tables)
        conf_score = conf.get("score", 0.8) if isinstance(conf, dict) else (conf or 0.8)
        adjusted_score = max(0.0, min(1.0, round(conf_score + validation.get("confidence_delta", 0.0), 2)))

        from .validators.dax_validators import _FUNCTION_CALL, BANNED_FUNCTIONS, _strip_strings
        expr_no_str = _strip_strings(dax_expr)
        unconverted = []
        for m in _FUNCTION_CALL.finditer(expr_no_str):
            fname = m.group(1).upper()
            if fname in BANNED_FUNCTIONS:
                unconverted.append(fname)
        unconverted = sorted(list(set(unconverted)))
        
        unconverted_syntax = sorted(list(set(getattr(self, 'unconverted_qlik_syntax', []))))

        if self.unresolved_columns or unconverted or unconverted_syntax:
            conversion_status = "failed"
        else:
            conversion_status = "converted"

        table_counts = {}
        for col_info in self.column_mapping.values():
            if col_info.get("table"):
                tbl = col_info["table"]
                table_counts[tbl] = table_counts.get(tbl, 0) + 1
                
        target_table = "_Measures"
        if table_counts:
            target_table = sorted(table_counts.items(), key=lambda x: (-x[1], x[0]))[0][0]
            
        fabric_meta["table"] = target_table

        return {
            "name": name,
            "qlik_expression": original_qlik_expr,
            "dax_expression": dax_expr,
            "conversion_method": "deterministic_rule",
            "conversion_status": conversion_status,
            "unresolved_columns": list(set(self.unresolved_columns)),
            "unconverted_qlik_functions": unconverted,
            "unconverted_qlik_syntax": unconverted_syntax,
            "column_mapping": dict(self.column_mapping),
            "function_mapping": dict(self.function_mapping),
            "qlik_number_format": qfmt,
            "tables": m_item.get("tables", []),
            "fabric": fabric_meta,
            "confidence": conf,
            "confidence_adjusted": adjusted_score,
            "validation": validation,
        }

    def extract_format_string(self, qfmt: Optional[Dict[str, Any]]) -> str:
        if isinstance(qfmt, dict):
            fmt = qfmt.get("qFmt") or qfmt.get("format")
            if fmt:
                return str(fmt)
        return "#,##0.00"
