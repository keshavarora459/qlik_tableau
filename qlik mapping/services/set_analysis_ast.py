"""Lightweight AST parser and translator for Qlik Set Analysis expressions.

Supports:
- Set Identifiers: 1, $, 1-$, $1, $_1, bookmarks
- Operators: =, -=, +=, *=
- Value Types:
  * Simple equality: Status={'Open'}, Year={2020}
  * Multiple values: Status={'Open','Pending','Approved'}, Year={2020, 2021}
  * Exclusions: Status-={'Cancelled'}, Region-={'North', 'South'}
  * Numeric comparisons: Year={">2020"}, Year={">=2020"}, Sales={"<1000"}, Amount={"<=500"}
  * Date comparisons: Date={">2023-01-01"}, OrderDate={">=2024-01-01"}
  * Wildcards: Customer={'*Corp*'}, Code={'A?B'}, Prefix={'ABC*'}, Suffix={'*XYZ'}
  * Search expressions: Customer={"=Sum(Sales)>1000"}
  * Variable references: Year={$(vYear)}
  * Calculated dollar-sign expressions: Year={$(=Max(Year)-1)}, Date={$(=Today()-1)}, Date={$(=Max(Date))}
"""

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Callable, Dict, List, Optional, Tuple


class SetModifierType(Enum):
    EQUALITY = "equality"
    MULTI_VALUE = "multi_value"
    EXCLUSION = "exclusion"
    NUMERIC_COMPARISON = "numeric_comparison"
    DATE_COMPARISON = "date_comparison"
    WILDCARD = "wildcard"
    SEARCH_EXPRESSION = "search_expression"
    VARIABLE = "variable"
    CALCULATED_DOLLAR = "calculated_dollar"


@dataclass
class SetClause:
    field: str
    operator: str  # '=', '-=', '+=', '*='
    raw_values: str
    values: List[str] = field(default_factory=list)
    modifier_type: SetModifierType = SetModifierType.EQUALITY
    comparison_op: Optional[str] = None  # '>', '>=', '<', '<=', '<>'
    comparison_value: Optional[str] = None
    wildcard_pattern: Optional[str] = None
    calculated_expression: Optional[str] = None
    is_numeric: bool = False


@dataclass
class SetAnalysisExpression:
    aggregation: str  # e.g. SUM, COUNT, AVERAGE, etc.
    is_distinct: bool
    inner_expression: str
    set_identifier: str  # '$', '1', '1-$', etc.
    clauses: List[SetClause] = field(default_factory=list)
    total_dimensions: Optional[List[str]] = None
    is_total: bool = False


# Helper for bracket/parenthesis-aware splitting
def split_top_level(text: str, separator: str = ",", open_chars: str = "([{<", close_chars: str = ")]}>") -> List[str]:
    matching = dict(zip(open_chars, close_chars))
    close_to_open = dict(zip(close_chars, open_chars))
    
    parts, depth_stack, current = [], [], []
    quote = ""
    
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "'\"`":
            quote = char
            current.append(char)
            continue
            
        if char in matching:
            depth_stack.append(char)
            current.append(char)
        elif char in close_to_open:
            if depth_stack and depth_stack[-1] == close_to_open[char]:
                depth_stack.pop()
            current.append(char)
        elif char == separator and not depth_stack:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
        else:
            current.append(char)
            
    if current:
        part = "".join(current).strip()
        if part:
            parts.append(part)
    return parts


class SetAnalysisParser:
    """Parses Qlik Set Analysis into an AST."""

    # Regex to find Aggregation({<...>} inner) or Aggregation(TOTAL {<...>} inner) or Aggregation(TOTAL <Dim> {<...>} inner)
    SET_ANALYSIS_REGEX = re.compile(
        r"\b(?P<agg>SUM|COUNT|AVERAGE|AVG|MIN|MAX|DISTINCTCOUNT)\s*\(\s*"
        r"(?P<total>TOTAL(?:\s*<(?P<total_dims>[^>]+)>)?\s+)?"
        r"\{\s*(?P<set_id>1-\$|\$1|\$_1|\$|1)?\s*<(?P<modifiers>.*?)>\s*\}\s*"
        r"(?P<inner>.*?)\)",
        re.IGNORECASE | re.DOTALL,
    )

    # Regex for a single set clause: Field op {Values} or Field op "Value" or Field op Value
    CLAUSE_REGEX = re.compile(
        r"(?P<field>'[^']+'\[[^\]]+\]|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)\s*"
        r"(?P<op>-=|\+=|\*=|=(?!=))\s*"
        r"(?:\{(?P<bracket_values>.*?)\}|(?P<quoted_value>\"[^\"]*\"|'[^']*')|(?P<bare_value>[^,;>]+))",
        re.DOTALL,
    )

    @classmethod
    def parse_clauses(cls, modifiers_text: str) -> List[SetClause]:
        clauses: List[SetClause] = []
        # Split modifiers by comma or semicolon at top level (outside nested { } or ( ))
        raw_clauses = split_top_level(modifiers_text, separator=",")
        
        for raw_clause in raw_clauses:
            raw_clause = raw_clause.strip()
            if not raw_clause:
                continue
            
            match = cls.CLAUSE_REGEX.search(raw_clause)
            if not match:
                continue
            
            field_name = match.group("field").strip()
            op = match.group("op").strip()
            
            if match.group("bracket_values") is not None:
                val_text = match.group("bracket_values").strip()
            elif match.group("quoted_value") is not None:
                val_text = match.group("quoted_value").strip()
            elif match.group("bare_value") is not None:
                val_text = match.group("bare_value").strip()
            else:
                val_text = ""
                
            clause = cls._parse_single_clause(field_name, op, val_text)
            clauses.append(clause)
            
        return clauses

    @classmethod
    def _parse_single_clause(cls, field_name: str, op: str, val_text: str) -> SetClause:
        # Check for calculated dollar-sign expression: $(=...) or $(vVar)
        if val_text.startswith("$(") and val_text.endswith(")"):
            inner_dollar = val_text[2:-1].strip()
            if inner_dollar.startswith("="):
                calc_expr = inner_dollar[1:].strip()
                return SetClause(
                    field=field_name,
                    operator=op,
                    raw_values=val_text,
                    modifier_type=SetModifierType.CALCULATED_DOLLAR,
                    calculated_expression=calc_expr,
                )
            else:
                return SetClause(
                    field=field_name,
                    operator=op,
                    raw_values=val_text,
                    modifier_type=SetModifierType.VARIABLE,
                    values=[val_text],
                )

        # Check for search expression: "=Sum(Sales)>1000" or {"=..."}
        stripped_val = val_text.strip("'\"")
        if stripped_val.startswith("="):
            return SetClause(
                field=field_name,
                operator=op,
                raw_values=val_text,
                modifier_type=SetModifierType.SEARCH_EXPRESSION,
                calculated_expression=stripped_val[1:].strip(),
            )

        # Split items in value list
        raw_items = split_top_level(val_text, separator=",")
        cleaned_items = [item.strip().strip("'\"") for item in raw_items if item.strip()]

        if not cleaned_items and val_text:
            cleaned_items = [val_text.strip().strip("'\"")]

        # Check if single item is comparison or wildcard
        if len(cleaned_items) == 1:
            item = cleaned_items[0]
            
            # Check numeric/date comparison operator: >, >=, <, <=
            comp_match = re.match(r"^(>=|<=|>|<|<>)\s*(.+)$", item)
            if comp_match:
                comp_op, comp_val = comp_match.group(1), comp_match.group(2).strip()
                # Check if comp_val is a date (YYYY-MM-DD)
                if re.match(r"^\d{4}-\d{2}-\d{2}$", comp_val):
                    return SetClause(
                        field=field_name,
                        operator=op,
                        raw_values=val_text,
                        values=[item],
                        modifier_type=SetModifierType.DATE_COMPARISON,
                        comparison_op=comp_op,
                        comparison_value=comp_val,
                    )
                return SetClause(
                    field=field_name,
                    operator=op,
                    raw_values=val_text,
                    values=[item],
                    modifier_type=SetModifierType.NUMERIC_COMPARISON,
                    comparison_op=comp_op,
                    comparison_value=comp_val,
                )

            # Check wildcard pattern: contains *, ?
            if "*" in item or "?" in item:
                return SetClause(
                    field=field_name,
                    operator=op,
                    raw_values=val_text,
                    values=[item],
                    modifier_type=SetModifierType.WILDCARD,
                    wildcard_pattern=item,
                )

            # Check exclusion operator -=
            if op == "-=":
                is_num = item.replace(".", "", 1).isdigit()
                return SetClause(
                    field=field_name,
                    operator=op,
                    raw_values=val_text,
                    values=[item],
                    modifier_type=SetModifierType.EXCLUSION,
                    is_numeric=is_num,
                )

            # Check if numeric
            is_num = item.replace(".", "", 1).isdigit()
            return SetClause(
                field=field_name,
                operator=op,
                raw_values=val_text,
                values=[item],
                modifier_type=SetModifierType.EQUALITY,
                is_numeric=is_num,
            )

        # Multiple items
        if op == "-=":
            return SetClause(
                field=field_name,
                operator=op,
                raw_values=val_text,
                values=cleaned_items,
                modifier_type=SetModifierType.EXCLUSION,
            )

        return SetClause(
            field=field_name,
            operator=op,
            raw_values=val_text,
            values=cleaned_items,
            modifier_type=SetModifierType.MULTI_VALUE,
        )


class SetAnalysisDAXEmitter:
    """Converts a SetAnalysisExpression AST into a Power BI DAX expression."""

    def __init__(self, table_resolver: Optional[Callable[[str], Optional[str]]] = None):
        self.table_resolver = table_resolver or (lambda f: None)

    def qualify_field(self, field_name: str) -> str:
        """Ensure field is in 'Table'[Field] format if table can be resolved."""
        field_name = field_name.strip()
        if re.match(r"^'[^']+'\[[^\]]+\]$", field_name):
            return field_name
        clean_field = field_name.strip("[]'\"")
        table = self.table_resolver(clean_field)
        if table:
            return f"'{table}'[{clean_field}]"
        return f"[{clean_field}]" if not field_name.startswith("[") else field_name

    def clause_to_dax(self, clause: SetClause, base_table: Optional[str] = None) -> Optional[str]:
        field_ref = self.qualify_field(clause.field)
        
        # 1. Calculated dollar-sign expression: e.g. Year={$(=Max(Year)-1)}
        if clause.modifier_type == SetModifierType.CALCULATED_DOLLAR:
            calc_expr = clause.calculated_expression or ""
            dax_calc = self._translate_calculated_expression(calc_expr, clause.field, base_table)
            if clause.operator == "-=":
                return f"{field_ref} <> {dax_calc}"
            return f"{field_ref} = {dax_calc}"

        # 2. Variable reference: e.g. Year={$(vYear)}
        if clause.modifier_type == SetModifierType.VARIABLE:
            var_token = clause.values[0] if clause.values else clause.raw_values
            if clause.operator == "-=":
                return f"{field_ref} <> {var_token}"
            return f"{field_ref} = {var_token}"

        # 3. Numeric comparison: e.g. Year={">2020"}
        if clause.modifier_type == SetModifierType.NUMERIC_COMPARISON:
            op = clause.comparison_op or "="
            val = clause.comparison_value or "0"
            return f"{field_ref} {op} {val}"

        # 4. Date comparison: e.g. Date={">2023-01-01"}
        if clause.modifier_type == SetModifierType.DATE_COMPARISON:
            op = clause.comparison_op or "="
            val = clause.comparison_value or ""
            # In DAX date string comparison or DATE(Y, M, D)
            return f'{field_ref} {op} "{val}"'

        # 5. Wildcard pattern: e.g. Customer={'*Corp*'}, Code={'A?B'}
        if clause.modifier_type == SetModifierType.WILDCARD:
            pattern = clause.wildcard_pattern or ""
            if "?" in pattern:
                # DAX SEARCH supports '?' as single character wildcard natively
                predicate = f'SEARCH("{pattern}", {field_ref}, 1, 0) > 0'
            elif pattern.startswith("*") and pattern.endswith("*") and len(pattern) > 2:
                inner_text = pattern[1:-1]
                predicate = f'CONTAINSSTRING({field_ref}, "{inner_text}")'
            elif pattern.startswith("*") and len(pattern) > 1:
                suffix = pattern[1:]
                predicate = f'RIGHT({field_ref}, {len(suffix)}) = "{suffix}"'
            elif pattern.endswith("*") and len(pattern) > 1:
                prefix = pattern[:-1]
                predicate = f'LEFT({field_ref}, {len(prefix)}) = "{prefix}"'
            else:
                predicate = f'SEARCH("{pattern}", {field_ref}, 1, 0) > 0'
                
            if clause.operator == "-=":
                return f"NOT({predicate})"
            return predicate

        # 6. Search expression: e.g. Customer={"=Sum(Sales)>1000"}
        if clause.modifier_type == SetModifierType.SEARCH_EXPRESSION:
            calc = clause.calculated_expression or ""
            # Format: FILTER(ALL(field_ref), calc)
            return f"CALCULATE({calc})"

        # 7. Exclusion: Status-={'Cancelled'} or Status-={'Cancelled', 'Pending'}
        if clause.modifier_type == SetModifierType.EXCLUSION or clause.operator == "-=":
            if len(clause.values) == 1:
                val = clause.values[0]
                val_repr = f'"{val}"'
                return f"{field_ref} <> {val_repr}"
            else:
                formatted_vals = ", ".join(f'"{v}"' for v in clause.values)
                return f"NOT({field_ref} IN {{{formatted_vals}}})"

        # 8. Multi-value: Status={'Open', 'Pending', 'Approved'}
        if clause.modifier_type == SetModifierType.MULTI_VALUE:
            formatted_vals = ", ".join(f'"{v}"' for v in clause.values)
            return f"{field_ref} IN {{{formatted_vals}}}"

        # 9. Single equality: Status={'Open'} or Year={2020}
        if len(clause.values) == 1:
            val = clause.values[0]
            val_repr = f'"{val}"'
            return f"{field_ref} = {val_repr}"

        return None

    def _translate_calculated_expression(self, calc: str, field_name: str, base_table: Optional[str]) -> str:
        """Translate calculated dollar-sign expressions like Max(Year)-1, Today()-1, Max(Date)."""
        calc_clean = calc.strip()
        
        # Today() or Today()-1 or Today()+30
        today_match = re.match(r"(?i)^Today\s*\(\s*\)\s*([+-]\s*\d+)?$", calc_clean)
        if today_match:
            offset = today_match.group(1)
            if offset:
                op = offset.strip()[0]
                val = offset.strip()[1:].strip()
                return f"TODAY() {op} {val}"
            return "TODAY()"
            
        # Year(Today()) or Year(Today())-1
        year_today_match = re.match(r"(?i)^Year\s*\(\s*Today\s*\(\s*\)\s*\)\s*([+-]\s*\d+)?$", calc_clean)
        if year_today_match:
            offset = year_today_match.group(1)
            if offset:
                op = offset.strip()[0]
                val = offset.strip()[1:].strip()
                return f"YEAR(TODAY()) {op} {val}"
            return "YEAR(TODAY())"

        # Max(Field) or Max(Field)-1 or Min(Field)
        max_min_match = re.match(
            r"(?i)^(Max|Min)\s*\(\s*(?P<col>[A-Za-z0-9_\[\]'\s]+)\s*\)\s*(?P<offset>[+-]\s*\d+)?$",
            calc_clean,
        )
        if max_min_match:
            fn = max_min_match.group(1).upper()
            col = max_min_match.group("col").strip()
            offset = max_min_match.group("offset")
            col_ref = self.qualify_field(col)
            table_match = re.match(r"'([^']+)'", col_ref)
            tbl = table_match.group(1) if table_match else base_table
            all_clause = f", ALL('{tbl}')" if tbl else ", ALL()"
            if offset:
                op = offset.strip()[0]
                val = offset.strip()[1:].strip()
                offset_str = f" {op} {val}"
            else:
                offset_str = ""
            return f"CALCULATE({fn}({col_ref}){all_clause}){offset_str}"

        # General expression fallback
        return calc_clean

    def emit(self, ast: SetAnalysisExpression, known_tables: Optional[List[Dict[str, Any]]] = None) -> str:
        agg = ast.aggregation.upper()
        if agg == "AVG":
            agg = "AVERAGE"
        if ast.is_distinct and agg == "COUNT":
            agg = "DISTINCTCOUNT"

        inner = ast.inner_expression.strip()
        if agg == "COUNT" and re.match(r"(?i)^DISTINCT\s+", inner):
            agg = "DISTINCTCOUNT"
            inner = re.sub(r"(?i)^DISTINCT\s+", "", inner).strip()

        # Find base table for qualify
        base_table = None
        if known_tables:
            for t in known_tables:
                t_name = t.get("name") or t.get("table_name")
                if t_name:
                    base_table = t_name
                    break

        predicates: List[str] = []

        # Handle TOTAL / TOTAL <Dims>
        if ast.is_total:
            if ast.total_dimensions:
                dim_refs = [self.qualify_field(d) for d in ast.total_dimensions]
                table_match = re.match(r"'([^']+)'", dim_refs[0]) if dim_refs else None
                tbl = table_match.group(1) if table_match else base_table
                if tbl:
                    predicates.append(f"ALLEXCEPT('{tbl}', {', '.join(dim_refs)})")
                else:
                    predicates.append(f"ALLSELECTED({', '.join(dim_refs)})")
            else:
                predicates.append("ALLSELECTED()")

        # Handle Set Identifier '1' (ignore all user selections)
        if ast.set_identifier == "1":
            tbl_for_all = base_table
            if not tbl_for_all and ast.clauses:
                f_ref = self.qualify_field(ast.clauses[0].field)
                tm = re.match(r"'([^']+)'", f_ref)
                if tm:
                    tbl_for_all = tm.group(1)
            if tbl_for_all:
                predicates.append(f"ALL('{tbl_for_all}')")
            else:
                predicates.append("ALL()")

        # Translate each clause to DAX
        for clause in ast.clauses:
            dax_pred = self.clause_to_dax(clause, base_table)
            if dax_pred:
                predicates.append(dax_pred)

        # Qualify inner expression
        inner_qualified = self.qualify_field(inner) if re.match(r"^[A-Za-z0-9_]+$", inner) else inner

        if not predicates:
            return f"{agg}({inner_qualified})"

        return f"CALCULATE({agg}({inner_qualified}), {', '.join(predicates)})"


def translate_set_analysis_ast(
    expression: str,
    table_resolver: Optional[Callable[[str], Optional[str]]] = None,
    known_tables: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, bool]:
    """Translate Set Analysis expressions using the AST parser. Returns (translated_str, changed_bool)."""
    if "{" not in expression or "<" not in expression:
        return expression, False

    parser = SetAnalysisParser()
    emitter = SetAnalysisDAXEmitter(table_resolver)
    changed = False

    def replacer(match: re.Match) -> str:
        nonlocal changed
        agg = match.group("agg").upper()
        total_str = match.group("total")
        total_dims_str = match.group("total_dims")
        set_id = match.group("set_id") or "$"
        modifiers_text = match.group("modifiers")
        inner = match.group("inner").strip()

        is_total = bool(total_str)
        total_dims = [d.strip() for d in total_dims_str.split(",")] if total_dims_str else None

        clauses = parser.parse_clauses(modifiers_text)
        
        ast = SetAnalysisExpression(
            aggregation=agg,
            is_distinct=False,
            inner_expression=inner,
            set_identifier=set_id,
            clauses=clauses,
            total_dimensions=total_dims,
            is_total=is_total,
        )

        dax_result = emitter.emit(ast, known_tables)
        changed = True
        return dax_result

    result = parser.SET_ANALYSIS_REGEX.sub(replacer, expression)
    return result, changed
