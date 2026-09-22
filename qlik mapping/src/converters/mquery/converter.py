"""LLM-assisted repair of generated Power Query M.

services/connection_mapper.py builds each table's M expression from a
per-connector template and deterministic parsing.

Optimization Workflow:
1. Validate baseline M-query deterministically.
2. If already valid, keep deterministic result and SKIP LLM completely.
3. Only if baseline is invalid/placeholder (e.g. resident chains, complex CROSSTABLE),
   invoke compact LLM request with minimal context and caching.
4. On LLM rate-limit / failure, preserve deterministic/regex fallback.
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from services import llm_usage
from services.prompt_builder import build_mquery_prompts

logger = logging.getLogger(__name__)

STAGE = "mquery"

# Connector calls that mean the query actually reaches a real source rather
# than the `let Source = TableName in Source` placeholder.
SOURCE_MARKERS = (
    ".Database(", ".Databases(", ".Files(", ".Catalogs(", ".Contents(",
    "NativeQuery(", "Table.FromRows(", "Json.Document(", "#table(",
    "Csv.Document(", "Excel.Workbook(", "Parquet.Document(",
    "Table.SelectRows(", "Table.AddColumn(", "Table.Group(",
    "Table.TransformColumns(", "Table.Combine(", "Table.NestedJoin(",
    "Table.SelectColumns(", "Table.Distinct(", "List.Dates(",
)

CODE_FENCE = re.compile(r"```[a-zA-Z]*\s*(.*?)```", re.DOTALL)


def strip_fences(text: str) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if "```" in cleaned:
        blocks = CODE_FENCE.findall(cleaned)
        if blocks:
            cleaned = max(blocks, key=len)
    return cleaned.strip()


def _balanced(expr: str) -> bool:
    """Parens/brackets/braces balanced outside string literals."""
    depth = 0
    in_string = False
    index = 0
    while index < len(expr):
        char = expr[index]
        if char == '"':
            if in_string and index + 1 < len(expr) and expr[index + 1] == '"':
                index += 2
                continue
            in_string = not in_string
        elif not in_string:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if depth < 0:
                    return False
        index += 1
    return depth == 0 and not in_string


def validate_mquery(
    candidate: str,
    table_name: str,
    known_queries: Optional[List[str]] = None,
) -> Tuple[bool, List[str]]:
    """Structural checks on a candidate M expression."""
    problems: List[str] = []
    if not candidate or not candidate.strip():
        return False, ["expression is empty"]

    text = candidate.strip()

    if not re.match(r"^\s*let\b", text, re.IGNORECASE):
        problems.append("does not start with 'let'")
    if not re.search(r"\bin\b", text):
        problems.append("has no 'in' clause")
    if not _balanced(text):
        problems.append("unbalanced parentheses, brackets or quotes")
    if "$(" in text:
        problems.append(
            "contains an unexpanded Qlik dollar-sign expansion, which is not valid M"
        )
    has_known_upstream = False
    if known_queries:
        for kq in known_queries:
            if kq and kq.lower() != table_name.lower() and (
                f"Source = {kq}" in text
                or f'Source = #"{kq}"' in text
                or f"Source = {kq}_Raw" in text
                or f'Source = #"{kq}_Raw"' in text
            ):
                has_known_upstream = True
                break

    if not any(marker in text for marker in SOURCE_MARKERS) and not has_known_upstream:
        problems.append("no real connector call - looks like an unresolved placeholder")

    # Qlik syntax that should have been translated away.
    for leftover in ("RESIDENT ", "AUTOGENERATE", "ApplyMap(", "CROSSTABLE(", "INLINE ["):
        if re.search(re.escape(leftover), text, re.IGNORECASE):
            problems.append(f"still contains untranslated Qlik syntax: {leftover.strip()}")

    # Local file paths never resolve in the Fabric service.
    if re.search(r'"[A-Za-z]:\\\\', text) or "lib://" in text:
        problems.append("references a local or lib:// path that will not resolve in Fabric")

    # Unresolved placeholders check
    for ph in ("<DATABASE>", "<SERVER>", "<HOST>", "<SCHEMA>", "<TABLE>"):
        if ph in text:
            problems.append(f"contains unresolved placeholder: {ph}")

    if "Sql.Database" in text:
        sql_match = re.search(r'Sql\.Database\s*\(\s*([^,\)]+)(?:\s*,\s*([^,\)]+))?', text)
        if sql_match:
            s_arg = (sql_match.group(1) or "").strip('"\') ')
            d_arg = (sql_match.group(2) or "").strip('"\') ') if sql_match.group(2) else ""
            if not s_arg or s_arg in ("null", "undefined", "<SERVER>", "<HOST>", "UnknownServer"):
                problems.append("Sql.Database server is unresolved or a placeholder")
            if not d_arg or d_arg in ("null", "undefined", "<DATABASE>", "<SERVER>"):
                problems.append("Sql.Database database is unresolved or a placeholder")

    # M is case-sensitive; these are the common mis-cased forms.
    for wrong in ("TEXT.", "TABLE.", "LIST.", "DATE.", "NUMBER."):
        if wrong in text:
            problems.append(f"mis-cased M function prefix '{wrong}' (M is case-sensitive)")
            break

    return (not problems), problems


class MQueryConverter:
    """Reviews and repairs generated Power Query M against the Qlik source."""

    def __init__(self, llm_client=None):
        self._llm_client = llm_client

    @property
    def llm_client(self):
        if self._llm_client is None:
            from src.converters.llm_client import get_llm_client
            self._llm_client = get_llm_client()
        return self._llm_client

    @staticmethod
    def _connection_context(table: Dict[str, Any]) -> str:
        connection = table.get("connection")
        if not isinstance(connection, dict):
            return "(no connection metadata supplied)"
        keep = (
            "name", "type", "provider", "driver", "server", "host",
            "database", "schema", "path", "url",
        )
        lines = [f"- {k}: {connection[k]}" for k in keep if connection.get(k)]
        return "\n".join(lines) or "(no connection metadata supplied)"

    async def refine_one(
        self,
        table: Dict[str, Any],
        baseline: str,
        known_queries: List[str],
    ) -> Dict[str, Any]:
        usage = llm_usage.current()
        name = str(table.get("name") or table.get("table_name") or "")

        usage.record_attempt(STAGE)
        try:
            system, user = build_mquery_prompts(
                table=table,
                baseline_mquery=baseline,
                connection_context=self._connection_context(table),
                upstream_tables=known_queries,
            )
            schema = {
                "type": "object",
                "properties": {
                    "m_query": {
                        "type": "string",
                        "description": "The converted Power Query M expression starting with 'let' and ending with the 'in' clause."
                    }
                },
                "required": ["m_query"]
            }
            answer_dict = await self.llm_client.generate_structured_response(system, user, schema, stage=STAGE)
            answer = answer_dict.get("m_query", "")
            usage.record_success(STAGE)
        except Exception as exc:  # noqa: BLE001
            usage.record_failure(STAGE, str(exc))
            logger.warning(
                "LLM M-query review failed for '%s' (%s); keeping generated query.",
                name, exc,
            )
            table["conversion_method"] = "regex_fallback"
            table["llm_status"] = "rate_limited" if "rate" in str(exc).lower() else "failed"
            
            # If the baseline is empty, we must provide a fallback so m_query isn't null.
            if not str(baseline).strip():
                from services.connection_mapper import ConnectionMapper
                fallback_m = f'let\n    Source = {name}\nin\n    Source'
                table["m_expression"] = fallback_m
                table["m_query"] = ConnectionMapper().parse_mquery_to_steps(fallback_m)
            return table

        if answer is None:
            answer = ""
        elif not isinstance(answer, str):
            answer = str(answer)

        candidate = strip_fences(answer)
        if not candidate:
            usage.record_rejected(STAGE, "model returned nothing usable")
            table["conversion_method"] = "regex_fallback"
            
            # If the baseline is empty, we must provide a fallback so m_query isn't null.
            if not str(baseline).strip():
                from services.connection_mapper import ConnectionMapper
                fallback_m = f'let\n    Source = {name}\nin\n    Source'
                table["m_expression"] = fallback_m
                table["m_query"] = ConnectionMapper().parse_mquery_to_steps(fallback_m)
            return table

        ok, problems = validate_mquery(candidate, name, known_queries)
        if not ok:
            usage.record_rejected(STAGE, "; ".join(problems[:3]))
            logger.info(
                "Rejected LLM M-query for '%s' (%s); kept generated query.",
                name, "; ".join(problems[:3]),
            )
            table["conversion_method"] = "regex_fallback"
            
            # If the baseline is empty, retaining the rejected LLM candidate is better than returning null.
            if not str(baseline).strip():
                from services.connection_mapper import ConnectionMapper
                table["m_expression"] = candidate
                table["m_query"] = ConnectionMapper().parse_mquery_to_steps(candidate)
                
            return table

        if candidate.strip() == str(baseline).strip():
            table["conversion_method"] = "deterministic_rule"
            return table

        usage.record_accepted(STAGE)
        table["baseline_m_expression"] = baseline
        table["m_expression"] = candidate
        from services.connection_mapper import ConnectionMapper
        table["m_query"] = ConnectionMapper().parse_mquery_to_steps(candidate)
        table["conversion_method"] = "llm_refined"
        table["llm_status"] = "success"
        return table

    async def refine_all(self, tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not tables:
            return tables

        usage = llm_usage.current()
        known = [
            str(t.get("name") or t.get("table_name") or "")
            for t in tables if isinstance(t, dict)
        ]

        targets = []
        for table in tables:
            if not isinstance(table, dict):
                continue
            raw_m = table.get("m_query") or table.get("m_expression") or table.get("mquery")
            if isinstance(raw_m, list):
                steps = []
                for s in raw_m:
                    if isinstance(s, dict) and "content" in s:
                        steps.append(str(s["content"]))
                    else:
                        steps.append(str(s))
                baseline = "\n".join(steps)
            else:
                baseline = str(raw_m or "")

            t_name = str(table.get("name") or table.get("table_name") or "")

            # Deterministic first: if baseline M-query is already structurally valid, skip LLM!
            is_valid, _ = validate_mquery(baseline, t_name, known)
            if is_valid or not Config.USE_LLM_MQUERY:
                usage.record_deterministic(STAGE)
                table["conversion_method"] = "deterministic_rule"
                table["llm_status"] = "not_needed"
                
                if not baseline.strip():
                    from services.connection_mapper import ConnectionMapper
                    fallback_m = f'let\n    Source = {t_name}\nin\n    Source'
                    table["m_expression"] = fallback_m
                    table["m_query"] = ConnectionMapper().parse_mquery_to_steps(fallback_m)
            else:
                targets.append((table, str(baseline)))

        if not targets or not Config.USE_LLM_MQUERY:
            return tables

        logger.info("Reviewing %d / %d unvalidated M-query expression(s) with LLM", len(targets), len(tables))

        # Process in batches of Config.LLM_MAX_BATCH_SIZE with controlled concurrency
        batch_size = max(1, Config.LLM_MAX_BATCH_SIZE)
        for i in range(0, len(targets), batch_size):
            batch = targets[i : i + batch_size]
            await asyncio.gather(
                *(self.refine_one(table, baseline, known) for table, baseline in batch),
                return_exceptions=True,
            )

        return tables
