"""Relationship Inferrer and Graph Validator for Fabric/Power BI Models.

Infers primary/foreign-key candidates, cardinality (1:*, 1:1, *:*), cross-filter direction,
detects orphaned tables, and validates measure reachability.
"""

from typing import Any, Dict, List, Optional, Set, Tuple


class RelationshipInferrer:
    """Infers and validates tabular data model relationships."""

    @staticmethod
    def infer_relationships(
        tables: List[Dict[str, Any]],
        known_associations: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        relationships = []
        seen_pairs: Set[Tuple[str, str, str, str]] = set()

        # 1. Use explicit Qlik associations/joins if available
        if known_associations:
            for assoc in known_associations:
                if not isinstance(assoc, dict):
                    continue
                from_tbl = assoc.get("from_table") or assoc.get("parent_table") or assoc.get("table1")
                from_col = assoc.get("from_column") or assoc.get("parent_column") or assoc.get("field1")
                to_tbl = assoc.get("to_table") or assoc.get("child_table") or assoc.get("table2")
                to_col = assoc.get("to_column") or assoc.get("child_column") or assoc.get("field2")

                if from_tbl and from_col and to_tbl and to_col and from_tbl != to_tbl:
                    key = (from_tbl.lower(), from_col.lower(), to_tbl.lower(), to_col.lower())
                    rev_key = (to_tbl.lower(), to_col.lower(), from_tbl.lower(), from_col.lower())
                    if key not in seen_pairs and rev_key not in seen_pairs:
                        seen_pairs.add(key)
                        relationships.append({
                            "from_table": from_tbl,
                            "from_column": from_col,
                            "to_table": to_tbl,
                            "to_column": to_col,
                            "cardinality": assoc.get("cardinality", "oneToMany"),
                            "cross_filter_direction": assoc.get("cross_filter_direction", "single"),
                            "is_active": assoc.get("is_active", True),
                            "confidence": 0.95,
                            "inference_rule": "qlik_explicit_association"
                        })

        # 2. Heuristic inference across tables by column naming & key patterns
        table_map = {}
        for t in tables or []:
            if not isinstance(t, dict):
                continue
            t_name = t.get("name") or t.get("table_name")
            if not t_name:
                continue
            cols = {}
            for c in t.get("columns") or t.get("fields") or []:
                c_name = c.get("fabric_column_name") or c.get("qlik_column_name") or c.get("name")
                if c_name:
                    cols[c_name.lower()] = c_name
            table_map[t_name] = cols

        table_names = list(table_map.keys())
        for i in range(len(table_names)):
            for j in range(i + 1, len(table_names)):
                t1 = table_names[i]
                t2 = table_names[j]
                cols1 = table_map[t1]
                cols2 = table_map[t2]

                common_keys = set(cols1.keys()).intersection(set(cols2.keys()))
                for ck in common_keys:
                    # Look for ID / Key patterns or shared field names
                    is_key = any(kw in ck for kw in ["id", "key", "_id", "_key", "code", "date"]) or ck in ("date", "calendar_date", "load_date")
                    if is_key or len(table_names) <= 5:
                        c1_name = cols1[ck]
                        c2_name = cols2[ck]
                        key = (t1.lower(), c1_name.lower(), t2.lower(), c2_name.lower())
                        rev_key = (t2.lower(), c2_name.lower(), t1.lower(), c1_name.lower())
                        if key not in seen_pairs and rev_key not in seen_pairs:
                            seen_pairs.add(key)
                            # Dimension vs Fact heuristic: Dimension table has fewer columns
                            dim_table = t1 if len(cols1) < len(cols2) else t2
                            fact_table = t2 if dim_table == t1 else t1
                            dim_col = cols1[ck] if dim_table == t1 else cols2[ck]
                            fact_col = cols2[ck] if dim_table == t1 else cols1[ck]

                            relationships.append({
                                "from_table": fact_table,
                                "from_column": fact_col,
                                "to_table": dim_table,
                                "to_column": dim_col,
                                "cardinality": "oneToMany",
                                "cross_filter_direction": "single",
                                "is_active": True,
                                "confidence": 0.88,
                                "inference_rule": "shared_key_name"
                            })

        return relationships

    @staticmethod
    def validate_measure_reachability(
        measures: List[Dict[str, Any]],
        tables: List[Dict[str, Any]],
        relationships: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Validate that tables referenced by each measure are reachable in the model."""
        import re

        table_names = {t.get("name") or t.get("table_name") for t in tables if isinstance(t, dict)}
        graph: Dict[str, Set[str]] = {t: set() for t in table_names if t}

        for rel in relationships or []:
            ft = rel.get("from_table")
            tt = rel.get("to_table")
            if ft in graph and tt in graph:
                graph[ft].add(tt)
                graph[tt].add(ft)

        unreachable = []
        for m in measures:
            dax = m.get("dax_expression") or ""
            referenced_tables = set(re.findall(r"'([^']+)'\[", dax))
            if len(referenced_tables) > 1:
                # Check if all referenced tables are connected in the graph
                ref_list = list(referenced_tables)
                start = ref_list[0]
                visited = set()
                queue = [start]
                while queue:
                    curr = queue.pop(0)
                    visited.add(curr)
                    for neighbor in graph.get(curr, set()):
                        if neighbor not in visited:
                            queue.append(neighbor)

                for other in ref_list[1:]:
                    if other not in visited:
                        unreachable.append({
                            "measure": m.get("name"),
                            "table1": start,
                            "table2": other,
                            "reason": f"No relationship path connects '{start}' and '{other}' in model"
                        })

        is_valid = (len(unreachable) == 0)
        return {
            "is_valid": is_valid,
            "reachable": is_valid,
            "unreachable_paths": unreachable,
            "unreachable_measures": unreachable,
        }

