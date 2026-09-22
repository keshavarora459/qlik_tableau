"""Resolve Qlik dollar-sign expansion before any DAX conversion runs.

Per Qlik's documentation, `$(vName)` is *textual substitution performed before
the expression is parsed* - it is not a value reference. Nothing in this
pipeline expanded it, so `$(...)` travelled straight through the regex
converter into the emitted DAX:

    $(vSales)                              -> $(vSales)
    Sum(Amount) / $(vTarget)               -> DIVIDE(SUM('Sales'[Amount]), $(vTarget), 0)
    Sum({<Year={$(vCurrentYear)}>} Amount) -> CALCULATE(SUM(...), Year = "$(vCurrentYear)")

The first two are invalid DAX. The third is worse: the token becomes a string
literal, so the filter silently matches nothing and the measure returns a
plausible-looking wrong number.

Expansion happens here, against the app's own variable inventory, before the
expression reaches DAXConverter or the LLM. Variables may reference other
variables, so expansion is recursive with a depth cap and cycle detection.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# $(vName) and $(vName(param1, param2)) - Qlik allows parameterised variables.
# The inner group stops at the first ')' that closes the expansion, which is
# resolved by scanning rather than by regex so nested $() works.
DOLLAR_START = re.compile(r"\$\(")

MAX_DEPTH = 10


def _find_expansions(text: str) -> List[Tuple[int, int, str]]:
    """Locate every top-level `$(...)` as (start, end_exclusive, inner).

    Scanned rather than regex-matched because a Qlik expansion can itself
    contain parentheses - `$(vSales(2024))` and nested `$($(vName))` are both
    legal, and a non-greedy regex truncates them at the wrong place.
    """
    found: List[Tuple[int, int, str]] = []
    for match in DOLLAR_START.finditer(text):
        start = match.start()
        depth = 0
        index = match.end() - 1  # position of '('
        while index < len(text):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    found.append((start, index + 1, text[match.end():index]))
                    break
            index += 1

    # Drop expansions nested inside an earlier one; the outer expansion is
    # re-scanned after substitution.
    top_level: List[Tuple[int, int, str]] = []
    for span in found:
        if not any(other[0] < span[0] and span[1] <= other[1] for other in found):
            top_level.append(span)
    return top_level


def build_variable_index(variables: List[Dict[str, Any]]) -> Dict[str, str]:
    """name (lowercased) -> definition, for every variable in the app."""
    index: Dict[str, str] = {}
    for variable in variables or []:
        if not isinstance(variable, dict):
            continue
        name = str(variable.get("name") or "").strip()
        if not name:
            continue
        definition = variable.get("definition")
        if definition is None:
            definition = variable.get("value")
        index[name.lower()] = "" if definition is None else str(definition)
    return index


def _strip_leading_equals(definition: str) -> str:
    """A Qlik variable definition may start with '=' to force evaluation.

    The '=' is Qlik's marker that the variable holds an expression; it is not
    part of the expression itself and must not survive into the substituted
    text, where it would produce `Sum(x) / =Sum(y)`.
    """
    text = definition.strip()
    return text[1:].strip() if text.startswith("=") else text


def _expand_recursive(
    text: str,
    variable_index: Dict[str, str],
    call_stack: Tuple[str, ...],
    unresolved: List[str],
    max_depth: int = MAX_DEPTH,
) -> str:
    if not text or "$(" not in text or len(call_stack) >= max_depth:
        return text

    spans = _find_expansions(text)
    if not spans:
        return text

    changed = False
    # Right to left so character offsets stay valid
    for start, end, inner in sorted(spans, key=lambda s: s[0], reverse=True):
        token = inner.strip()

        # Calculated dollar-sign expression: $(=Expression).
        # This is dynamic runtime evaluation in Qlik, not a variable name lookup.
        # Preserve it intact so Set Analysis AST and DAX converters can translate it.
        if token.startswith("="):
            continue

        # Parameterised variable: $(vName(a, b)). The parameter values
        # are positional substitutions inside the definition ($1, $2).
        params: List[str] = []
        name = token
        paren = token.find("(")
        if paren != -1 and token.endswith(")"):
            name = token[:paren].strip()
            params = [p.strip() for p in token[paren + 1:-1].split(",")]

        key = name.lower()

        if key in call_stack:
            # Genuine cycle detected along the current expansion path
            logger.warning("Circular Qlik variable reference at '%s' (path: %s).", name, " -> ".join(call_stack + (key,)))
            if name not in unresolved:
                unresolved.append(name)
            text = text[:start] + f"/* CIRCULAR_VARIABLE: {name} */" + text[end:]
            changed = True
            continue

        if key not in variable_index:
            if name not in unresolved:
                unresolved.append(name)
            text = text[:start] + f"/* UNRESOLVED_VARIABLE: {name} */" + text[end:]
            changed = True
            continue

        raw_def = variable_index[key]
        replacement = _strip_leading_equals(raw_def)
        for position, value in enumerate(params, start=1):
            replacement = replacement.replace(f"${position}", value)

        # Recursively expand the replacement with key added to call_stack
        expanded_replacement = _expand_recursive(
            replacement, variable_index, call_stack + (key,), unresolved, max_depth
        )

        text = text[:start] + expanded_replacement + text[end:]
        changed = True

    return text


def expand(
    expression: str,
    variable_index: Dict[str, str],
    max_depth: int = MAX_DEPTH,
) -> Tuple[str, List[str]]:
    """Expand every `$(...)` in `expression`.

    Returns (expanded_expression, unresolved_names). An unresolved reference
    is left in place *as a marker comment* rather than silently deleted, so
    the guard downstream can reject the expression instead of shipping a
    wrong number.
    """
    if not expression or "$(" not in str(expression):
        return str(expression or ""), []

    unresolved: List[str] = []
    text = _expand_recursive(str(expression), variable_index, (), unresolved, max_depth)
    return text, unresolved


def expand_in_place(
    items: List[Dict[str, Any]],
    variable_index: Dict[str, str],
    fields: Tuple[str, ...] = ("qlik_expression", "expression"),
) -> int:
    """Expand `$(...)` across a list of measure/dimension dicts.

    The original text is preserved under `<field>_raw` so the mapping output
    still shows what the Qlik app actually contained - the expansion is a
    conversion step, not a rewrite of the source.

    Returns the number of items that changed.
    """
    changed = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for field in fields:
            original = item.get(field)
            if not original or "$(" not in str(original):
                continue
            expanded, unresolved = expand(str(original), variable_index)
            if expanded == original:
                continue
            item[f"{field}_raw"] = original
            item[field] = expanded
            if unresolved:
                existing = item.get("unresolved_variables") or []
                item["unresolved_variables"] = sorted(set(existing) | set(unresolved))
            changed += 1
    return changed
