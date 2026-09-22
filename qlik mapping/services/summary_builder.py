from typing import Any, Dict, List, Optional

from config import Config


class SummaryBuilder:
    """Calculates conversion summary metrics and formats metadata pass-through sections."""

    def build_summary(
        self,
        tables: List[Dict[str, Any]],
        measures: List[Dict[str, Any]],
        relationships: List[Dict[str, Any]],
        expected_tables: Optional[int] = None,
        expected_measures: Optional[int] = None,
        expected_relationships: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Summarize the conversion.

        `expected_*` are the inbound counts. Without them a section that
        silently dropped everything still reported `failed: 0` — which is
        exactly how 29 measures converting to 0 went unnoticed.
        """
        dax_scores = [m.get("confidence", {}).get("score", 0.95) for m in measures]
        tbl_scores = [t.get("confidence", {}).get("score", 0.95) for t in tables]
        rel_scores = [r.get("confidence", {}).get("score", 0.95) for r in relationships]

        sections = {
            "tables": self._section(tables, expected_tables),
            "measures": self._section(measures, expected_measures),
            "relationships": self._section(relationships, expected_relationships),
        }

        all_scores = dax_scores + tbl_scores + rel_scores
        avg_score = round(sum(all_scores) / len(all_scores), 2) if all_scores else 0.0
        confidence_percent = int(round(avg_score * 100)) if avg_score <= 1.0 else int(round(avg_score))
        total = sum(s["total"] for s in sections.values())
        converted = sum(s["converted"] for s in sections.values())
        failed = sum(s["failed"] for s in sections.values())

        return {
            "total_items": total,
            "converted": converted,
            "failed": failed,
            "confidence": avg_score,
            "confidence_score": confidence_percent,
            "confidence_percentage": f"{confidence_percent}%",
            "score_out_of_100": confidence_percent,
            "requires_review": bool(failed) or any(s < 0.70 for s in all_scores),
            "by_section": sections,
            "dax_confidence_scores": dax_scores,
            "dax_count": len(measures),
        }

    @staticmethod
    def _section(converted: List[Any], expected: Optional[int]) -> Dict[str, int]:
        total = len(converted) if expected is None else max(expected, len(converted))
        return {
            "total": total,
            "converted": len(converted),
            "failed": max(0, total - len(converted)),
        }

    def build_llm_status(self, converted: Optional[bool] = None, reason: Optional[str] = None) -> Dict[str, Any]:
        """Report what the LLM actually did during this run.

        This used to hardcode `converted: True` and name the configured Groq
        model unconditionally - so a mapping produced entirely by the regex
        and lookup-table fallbacks still advertised itself as LLM-converted,
        and there was no way to tell a real conversion from a silent
        fallback. `converted` is now derived from the run's own counters.
        """
        from services import llm_usage

        if Config.USE_GROQ:
            model = f"groq:{Config.GROQ_MODEL}"
        elif getattr(Config, "AZURE_OPENAI_DEPLOYMENT", None):
            model = f"azure:{Config.AZURE_OPENAI_DEPLOYMENT}"
        else:
            model = "none:rule-based"

        usage = llm_usage.current()
        stats = usage.as_dict()
        actually_converted = usage.succeeded > 0 if converted is None else converted

        stages_enabled = {
            "measures": Config.USE_LLM_MEASURES,
            "dimensions": Config.USE_LLM_DIMENSIONS,
            "visuals": Config.USE_LLM_VISUALS,
        }

        if not actually_converted:
            default_reason = (
                "No LLM call succeeded during this run; every item used its "
                "deterministic (rule-based) conversion."
            )
        else:
            default_reason = (
                f"{usage.succeeded}/{usage.attempted} LLM call(s) succeeded; "
                f"{usage.accepted} result(s) accepted over the deterministic "
                f"baseline, {usage.rejected} rejected by validation."
            )

        return {
            "converted": actually_converted,
            "model": model if actually_converted else "none:rule-based",
            "configured_model": model,
            "prompt_version": "3.0",
            "stages_enabled": stages_enabled,
            "usage": stats,
            "reason": reason or default_reason,
        }

    def format_passthrough(
        self, raw_data: Dict[str, Any], key: str, with_not_found: bool = False
    ) -> Dict[str, Any]:
        val = raw_data.get(key)
        items = val if isinstance(val, list) else (
            val.get("converted_items", []) if isinstance(val, dict) else []
        )
        res = {"count": len(items), "converted_items": items}
        if with_not_found:
            res["not_found_msg"] = None if items else f"No {key} found"
        return res

    def extract_parsing_summary(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract or compute parsing summary statistics (tables, measures, dimensions, etc.) from parsing result."""
        if not isinstance(data, dict):
            return {
                "tables": 0, "dimensions": 0, "measures": 0, "relationships": 0,
                "sheets": 0, "visualizations": 0, "empty_keys": 0, "populated_keys": 0,
                "view": "compact"
            }

        # 1. Direct summary field
        if isinstance(data.get("summary"), dict) and data["summary"]:
            return dict(data["summary"])

        # 2. Summary inside _meta
        meta = data.get("_meta")
        if isinstance(meta, dict) and isinstance(meta.get("summary"), dict) and meta["summary"]:
            return dict(meta["summary"])

        # 3. Summary inside parsing_result
        pr = data.get("parsing_result")
        if isinstance(pr, dict):
            if isinstance(pr.get("summary"), dict) and pr["summary"]:
                return dict(pr["summary"])
            pr_meta = pr.get("_meta")
            if isinstance(pr_meta, dict) and isinstance(pr_meta.get("summary"), dict) and pr_meta["summary"]:
                return dict(pr_meta["summary"])

        # 4. Dynamic fallback from data lists
        tables_cnt = len(data.get("tables") or [])
        dims_cnt = len(data.get("dimensions") or [])
        measures_cnt = len(data.get("measures") or [])
        rels_cnt = len(data.get("relationships") or [])
        sheets_cnt = len(data.get("sheets") or [])
        vis_cnt = len(data.get("visualizations") or data.get("sheet_visuals") or [])

        return {
            "tables": tables_cnt,
            "dimensions": dims_cnt,
            "measures": measures_cnt,
            "relationships": rels_cnt,
            "sheets": sheets_cnt,
            "visualizations": vis_cnt,
            "empty_keys": 0,
            "populated_keys": 0,
            "view": "compact"
        }

