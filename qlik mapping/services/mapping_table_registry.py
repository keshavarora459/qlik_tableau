"""Generic Mapping Table Registry and Power Query M Converter for Qlik ApplyMap() Semantics."""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from .qlik_function_matrix import QlikFunctionMatrix


@dataclass
class MappingTableDefinition:
    mapping_name: str
    key_expression: str
    value_expression: str
    default_expression: Optional[str] = None
    source_table: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    lineage: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ApplyMapNode:
    map_name: str
    key_expr: Union[str, "ApplyMapNode"]
    default_expr: Optional[str] = None
    output_alias: Optional[str] = None


def parse_applymap_ast(expr: str) -> Optional[ApplyMapNode]:
    """Parse single or nested ApplyMap('Map', key, [default]) call into an ApplyMapNode tree."""
    if not expr:
        return None
    expr_clean = expr.strip()
    match = re.search(r"\bApplyMap\s*\(", expr_clean, re.IGNORECASE)
    if not match:
        return None

    start_idx = match.end() - 1  # '(' position
    depth = 0
    in_sq = False
    in_dq = False
    end_idx = -1

    for i in range(start_idx, len(expr_clean)):
        char = expr_clean[i]
        if char == "'" and not in_dq:
            in_sq = not in_sq
        elif char == '"' and not in_sq:
            in_dq = not in_dq
        elif not in_sq and not in_dq:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break

    if end_idx == -1:
        return None

    args_str = expr_clean[start_idx + 1:end_idx]
    args = []
    current_arg = []
    depth = 0
    in_sq = False
    in_dq = False

    for char in args_str:
        if char == "'" and not in_dq:
            in_sq = not in_sq
            current_arg.append(char)
        elif char == '"' and not in_sq:
            in_dq = not in_sq
            current_arg.append(char)
        elif not in_sq and not in_dq:
            if char in "([{":
                depth += 1
                current_arg.append(char)
            elif char in ")]}":
                depth -= 1
                current_arg.append(char)
            elif char == "," and depth == 0:
                args.append("".join(current_arg).strip())
                current_arg = []
            else:
                current_arg.append(char)
        else:
            current_arg.append(char)

    if current_arg:
        args.append("".join(current_arg).strip())

    if len(args) < 2:
        return None

    map_name = args[0].strip("'\" ")
    raw_key = args[1].strip()
    default_expr = args[2].strip() if len(args) >= 3 else None

    nested_key = parse_applymap_ast(raw_key)
    key_val = nested_key if nested_key else raw_key

    return ApplyMapNode(
        map_name=map_name,
        key_expr=key_val,
        default_expr=default_expr,
    )


def flatten_applymap_node(node: ApplyMapNode) -> List[Dict[str, Any]]:
    """Flatten an ApplyMapNode tree into inside-out execution steps."""
    steps: List[Dict[str, Any]] = []

    def _visit(n: ApplyMapNode) -> str:
        if isinstance(n.key_expr, ApplyMapNode):
            prev_result_var = _visit(n.key_expr)
            key_source = prev_result_var
        else:
            key_source = n.key_expr

        step_id = len(steps) + 1
        clean_map_name = re.sub(r'[^A-Za-z0-9_]', '_', n.map_name).strip('_')
        output_var = f"__{clean_map_name}_Result__"
        steps.append({
            "step_id": step_id,
            "map_name": n.map_name,
            "key_expr": key_source,
            "default_expr": n.default_expr,
            "output_var": output_var,
        })
        return output_var

    _visit(node)
    return steps


def find_top_level_applymaps(qlik_query: str) -> List[Tuple[str, Optional[str]]]:
    """Find all top-level ApplyMap(...) expressions and their aliases in qlik_query."""
    results = []
    if not qlik_query or "applymap" not in qlik_query.lower():
        return results

    matches = list(re.finditer(r"\bApplyMap\s*\(", qlik_query, re.IGNORECASE))
    processed_spans: List[Tuple[int, int]] = []

    for m in matches:
        start_idx = m.start()
        if any(span[0] <= start_idx < span[1] for span in processed_spans):
            continue

        open_paren_idx = m.end() - 1
        depth = 0
        in_sq = False
        in_dq = False
        end_idx = -1

        for i in range(open_paren_idx, len(qlik_query)):
            char = qlik_query[i]
            if char == "'" and not in_dq:
                in_sq = not in_sq
            elif char == '"' and not in_sq:
                in_dq = not in_dq
            elif not in_sq and not in_dq:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        end_idx = i
                        break

        if end_idx != -1:
            processed_spans.append((start_idx, end_idx))
            expr = qlik_query[start_idx:end_idx + 1].strip()

            rem_text = qlik_query[end_idx + 1:]
            alias_match = re.match(r"^\s+AS\s+\[?([A-Za-z0-9_#]+)\]?", rem_text, re.IGNORECASE)
            alias = alias_match.group(1) if alias_match else None
            results.append((expr, alias))

    return results


class MappingTableRegistry:
    """Canonical registry for Qlik mapping tables and ApplyMap resolution."""

    def __init__(self):
        self._mappings: Dict[str, MappingTableDefinition] = {}

    def register(self, definition: MappingTableDefinition) -> None:
        self._mappings[definition.mapping_name.lower()] = definition

    def register_mapping(
        self,
        map_name: str,
        source_table: str,
        key_column: str,
        value_column: str,
        default_value: Optional[str] = None,
    ) -> None:
        """Helper to register a mapping table definition directly."""
        self.register(
            MappingTableDefinition(
                mapping_name=map_name,
                key_expression=key_column,
                value_expression=value_column,
                default_expression=default_value,
                source_table=source_table,
                dependencies=[key_column, value_column],
                lineage={"source": "manual_or_test", "name": map_name},
            )
        )

    def get(self, mapping_name: str) -> Optional[MappingTableDefinition]:
        return self._mappings.get((mapping_name or "").lower())

    def generate_missing_mapping_report(self, map_name: str, qlik_expr: str) -> Dict[str, Any]:
        """Generate structured diagnostic report for genuinely missing mapping sources."""
        return {
            "mapping_name": map_name,
            "lookup_expression": qlik_expr,
            "expected_source": f"Mapping table '{map_name}' in script or data files",
            "missing_dependency": map_name,
            "reason": f"Mapping table '{map_name}' is referenced by ApplyMap() but no matching MAPPING LOAD or mapping file was found in the Qlik application context."
        }

    def discover_from_script(self, qlik_script: str) -> None:
        """Parse MAPPING LOAD statements from Qlik script and register them dynamically."""
        if not qlik_script:
            return

        # Pattern: [MapName:] MAPPING LOAD [expr AS] KeyField, [expr AS] ValueField
        pattern = re.compile(
            r"(?:([A-Za-z0-9_#]+)\s*:\s*)?MAPPING\s+LOAD\s+(.*?)(?=\;|\bLOAD\b|\bSELECT\b|$)",
            re.IGNORECASE | re.DOTALL,
        )

        for match in pattern.finditer(qlik_script):
            map_name = match.group(1).strip() if match.group(1) else None
            block_body = match.group(2).strip()

            header_part = re.split(r"\b(FROM|RESIDENT|INLINE|AUTOGENERATE)\b", block_body, maxsplit=1, flags=re.IGNORECASE)[0].strip()
            fields = [f.strip() for f in header_part.split(",") if f.strip()]

            key_field = "Key"
            val_field = "Value"
            if len(fields) >= 2:
                key_match = re.search(r"\bAS\s+\[?([A-Za-z0-9_#]+)\]?", fields[0], re.IGNORECASE)
                if key_match:
                    key_field = key_match.group(1)
                else:
                    key_field = fields[0].strip("[] ")

                val_match = re.search(r"\bAS\s+\[?([A-Za-z0-9_#]+)\]?", fields[1], re.IGNORECASE)
                if val_match:
                    val_field = val_match.group(1)
                else:
                    val_field = fields[1].strip("[] ")

            res_match = re.search(r"\bRESIDENT\s+([A-Za-z0-9_#]+)", block_body, re.IGNORECASE)
            src_tbl = res_match.group(1).strip() if res_match else (map_name or "MappingSource")

            if not map_name and res_match:
                map_name = res_match.group(1).strip()

            if map_name:
                self.register(
                    MappingTableDefinition(
                        mapping_name=map_name,
                        key_expression=key_field,
                        value_expression=val_field,
                        source_table=src_tbl or map_name,
                        dependencies=[key_field, val_field],
                        lineage={"source": "script_mapping_load", "name": map_name},
                    )
                )

    def discover_from_tables(self, tables: List[Dict[str, Any]]) -> None:
        """Scan tables list for mapping tables or scripts."""
        for t in tables or []:
            if not isinstance(t, dict):
                continue
            t_name = t.get("name") or t.get("table_name") or ""
            q_script = t.get("qlik_query") or t.get("load_statement") or ""
            if q_script:
                self.discover_from_script(q_script)

            if t.get("is_mapping") or t.get("load_type") == "mapping":
                cols = t.get("columns") or t.get("fields") or []
                if len(cols) >= 2:
                    k_col = cols[0].get("fabric_column_name") or cols[0].get("qlik_column_name") or cols[0].get("name") if isinstance(cols[0], dict) else str(cols[0])
                    v_col = cols[1].get("fabric_column_name") or cols[1].get("qlik_column_name") or cols[1].get("name") if isinstance(cols[1], dict) else str(cols[1])
                    self.register(
                        MappingTableDefinition(
                            mapping_name=t_name,
                            key_expression=str(k_col),
                            value_expression=str(v_col),
                            source_table=t_name,
                            dependencies=[str(k_col), str(v_col)],
                            lineage={"source": "table_metadata", "name": t_name},
                        )
                    )

    def resolve_applymap_dax(
        self,
        map_name: str,
        key_expr: str,
        default_expr: Optional[str] = None,
        known_tables: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, bool]:
        """Convert ApplyMap(map_name, key, default) to DAX LOOKUPVALUE / COALESCE."""
        mapping_def = self.get(map_name)
        target_col = "Value"
        source_col = "Key"
        table_name = map_name
        is_found = False

        if mapping_def:
            source_col = mapping_def.key_expression
            target_col = mapping_def.value_expression
            table_name = mapping_def.source_table or map_name
            is_found = True
        elif known_tables:
            for tbl in known_tables:
                if (tbl.get("name") or tbl.get("table_name") or "").lower() == map_name.lower():
                    cols = tbl.get("columns", [])
                    if len(cols) >= 2:
                        source_col = cols[0].get("fabric_column_name") or cols[0].get("qlik_column_name") or "Key"
                        target_col = cols[1].get("fabric_column_name") or cols[1].get("qlik_column_name") or "Value"
                    elif len(cols) == 1:
                        target_col = cols[0].get("fabric_column_name") or cols[0].get("qlik_column_name") or "Value"
                    table_name = tbl.get("name") or map_name
                    is_found = True
                    break

        if not is_found:
            review_note = f"REVIEW_REQUIRED: Mapping table '{map_name}' is referenced by ApplyMap but does not exist in model or script."
            fallback = f"/* {review_note} */ LOOKUPVALUE('{table_name}'[{target_col}], '{table_name}'[{source_col}], {key_expr})"
            if default_expr:
                return f"COALESCE({fallback}, {default_expr})", False
            return fallback, False

        lookup = f"LOOKUPVALUE('{table_name}'[{target_col}], '{table_name}'[{source_col}], {key_expr})"
        if default_expr:
            return f"COALESCE({lookup}, {default_expr})", True
        return lookup, True

    def translate_applymap_expression(
        self,
        qlik_expr: str,
        known_tables: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Parse and translate single or nested ApplyMap expressions into DAX inside-out."""
        applymap_pattern = re.compile(
            r"ApplyMap\s*\(\s*'([^']+)'\s*,\s*([^,()]+|\([^)]+\)|ApplyMap\([^)]+\))\s*(?:,\s*('[^']*'|[^)]+?))?\s*\)",
            re.IGNORECASE,
        )
        current = qlik_expr
        for _ in range(5):
            match = applymap_pattern.search(current)
            if not match:
                break
            map_name = match.group(1)
            key_expr = match.group(2).strip()
            def_expr = match.group(3).strip() if match.group(3) else None
            dax_lookup, _ = self.resolve_applymap_dax(map_name, key_expr, def_expr, known_tables)
            current = current[:match.start()] + dax_lookup + current[match.end():]
        return current

    def translate_applymap_to_m(
        self,
        baseline_m: str,
        qlik_query: str,
        known_tables: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Convert ApplyMap() expressions in Qlik LOAD into Power Query M steps.

        Returns: (updated_m_expression, list_of_missing_mapping_reports)
        """
        if not baseline_m or "applymap" not in (qlik_query or "").lower():
            return baseline_m, []

        missing_reports: List[Dict[str, Any]] = []
        applymap_matches = find_top_level_applymaps(qlik_query)
        if not applymap_matches:
            return baseline_m, []

        # Extract last step from baseline_m
        m_text = baseline_m.strip()
        in_match = re.search(r"\bin\b\s+(#\"[^\"]+\"|[A-Za-z0-9_#]+)\s*$", m_text, re.IGNORECASE)
        if not in_match:
            return baseline_m, []

        last_step = in_match.group(1).strip()
        let_body = m_text[:in_match.start()].strip()

        new_steps: List[str] = []
        current_step = last_step

        for raw_expr, target_alias in applymap_matches:

            ast_node = parse_applymap_ast(raw_expr)
            if not ast_node:
                continue

            if not target_alias:
                target_alias = f"{ast_node.map_name}_Result"

            # Check for missing mappings
            flat_steps = flatten_applymap_node(ast_node)
            for s in flat_steps:
                map_n = s["map_name"]
                m_def = self.get(map_n)
                if not m_def and known_tables:
                    for tbl in known_tables:
                        if (tbl.get("name") or tbl.get("table_name") or "").lower() == map_n.lower():
                            m_def = MappingTableDefinition(
                                mapping_name=map_n,
                                key_expression="Key",
                                value_expression="Value",
                                source_table=tbl.get("name") or map_n
                            )
                            break
                if not m_def:
                    missing_reports.append(self.generate_missing_mapping_report(map_n, raw_expr))

            # Build M step sequence inside-out
            prev_result_var: Optional[str] = None
            for idx, s in enumerate(flat_steps):
                is_final = (idx == len(flat_steps) - 1)
                map_n = s["map_name"]
                key_src = s["key_expr"]
                def_src = s["default_expr"]

                m_def = self.get(map_n)
                if m_def:
                    key_col = m_def.key_expression
                    val_col = m_def.value_expression
                    map_table_m = m_def.source_table or map_n
                else:
                    key_col = "Key"
                    val_col = "Value"
                    map_table_m = map_n

                map_table_ident = f'#"{map_table_m}"' if re.search(r"[^A-Za-z0-9_]", map_table_m) else map_table_m

                temp_key_col: Optional[str] = None
                if prev_result_var and key_src == prev_result_var:
                    join_col = prev_result_var
                else:
                    m_key_expr = QlikFunctionMatrix.translate_qlik_expression_to_m(str(key_src))
                    bare_match = re.match(r"^\s*\[([A-Za-z0-9_#]+)\]\s*$", m_key_expr)
                    if bare_match:
                        join_col = bare_match.group(1)
                    else:
                        temp_key_col = f"__{map_n}_Key__"
                        step_var = f'#"Added {map_n}_Key"'
                        new_steps.append(f'    {step_var} = Table.AddColumn({current_step}, "{temp_key_col}", each {m_key_expr}, type text)')
                        current_step = step_var
                        join_col = temp_key_col

                temp_table_col = f"__{map_n}_Table__"
                step_merge = f'#"Merged {map_n}"'
                new_steps.append(f'    {step_merge} = Table.NestedJoin({current_step}, {{"{join_col}"}}, {map_table_ident}, {{"{key_col}"}}, "{temp_table_col}", JoinKind.LeftOuter)')
                current_step = step_merge

                temp_val_col = f"__{map_n}_Val__"
                step_expand = f'#"Expanded {map_n}"'
                new_steps.append(f'    {step_expand} = Table.ExpandTableColumn({current_step}, "{temp_table_col}", {{"{val_col}"}}, {{"{temp_val_col}"}})')
                current_step = step_expand

                if def_src is None or def_src == "":
                    def_m = "null"
                elif def_src.startswith("'") and def_src.endswith("'"):
                    def_m = f'"{def_src[1:-1]}"'
                elif def_src.startswith('"') and def_src.endswith('"'):
                    def_m = def_src
                else:
                    def_m = def_src

                if not is_final:
                    res_col = s["output_var"]
                    prev_result_var = res_col
                    step_add_res = f'#"Added {map_n}_Result"'
                    new_steps.append(f'    {step_add_res} = Table.AddColumn({current_step}, "{res_col}", each if [{temp_val_col}] <> null then [{temp_val_col}] else {def_m})')
                    current_step = step_add_res

                    remove_cols = [c for c in [temp_key_col, temp_table_col, temp_val_col] if c]
                    if remove_cols:
                        rem_str = ", ".join(f'"{c}"' for c in remove_cols)
                        step_rem = f'#"Removed Temp {map_n}"'
                        new_steps.append(f'    {step_rem} = Table.RemoveColumns({current_step}, {{{rem_str}}})')
                        current_step = step_rem
                else:
                    res_col = target_alias
                    step_add_final = f'#"Added {res_col}"'
                    new_steps.append(f'    {step_add_final} = Table.AddColumn({current_step}, "{res_col}", each if [{temp_val_col}] <> null then [{temp_val_col}] else {def_m}, type text)')
                    current_step = step_add_final

                    remove_cols = [c for c in [temp_key_col, temp_table_col, temp_val_col, prev_result_var] if c]
                    if remove_cols:
                        rem_str = ", ".join(f'"{c}"' for c in remove_cols)
                        step_rem = f'#"Removed Temp {map_n}"'
                        new_steps.append(f'    {step_rem} = Table.RemoveColumns({current_step}, {{{rem_str}}})')
                        current_step = step_rem

        if not new_steps:
            return baseline_m, missing_reports

        # Assemble final M expression
        final_m = let_body + ",\n" + ",\n".join(new_steps) + f"\nin\n    {current_step}"
        return final_m, missing_reports

    def build_mapping_table_m(self, mapping_name: str, key_col: str, val_col: str, rows: Optional[List[Tuple[Any, Any]]] = None) -> str:
        """Build executable Power Query M for a mapping lookup table with real rows."""
        if rows:
            row_items = []
            for k, v in rows:
                k_val = f'"{k}"' if isinstance(k, str) else str(k)
                v_val = f'"{v}"' if isinstance(v, str) else str(v)
                row_items.append(f"{{{k_val}, {v_val}}}")
            rows_str = "{\n        " + ",\n        ".join(row_items) + "\n    }"
            return (
                f'let\n'
                f'    Source = #table({{"{key_col}", "{val_col}"}}, {rows_str}),\n'
                f'    #"Changed Type" = Table.TransformColumnTypes(Source, {{ {{"{key_col}", type text}}, {{"{val_col}", type text}} }})\n'
                f'in\n'
                f'    #"Changed Type"'
            )
        return (
            f'let\n'
            f'    Source = #table({{"{key_col}", "{val_col}"}}, {{}}),\n'
            f'    #"Changed Type" = Table.TransformColumnTypes(Source, {{ {{"{key_col}", type text}}, {{"{val_col}", type text}} }})\n'
            f'in\n'
            f'    #"Changed Type"'
        )
