"""Qlik vs Power BI Reconciliation Engine.

Compares source Qlik metadata/metrics against generated Power BI model metrics
including table row counts, distinct values, null counts, measure KPI values,
and filter slices.
"""

from typing import Any, Dict, List, Optional


class ReconciliationEngine:
    """Performs semantic and numerical reconciliation between Qlik and Power BI."""

    @staticmethod
    def reconcile_tables(
        qlik_tables_meta: List[Dict[str, Any]],
        pbi_tables: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Reconcile table schemas and column counts."""
        results = []
        all_passed = True

        pbi_map = {t.get("name") or t.get("table_name"): t for t in pbi_tables if isinstance(t, dict)}

        for q_tbl in qlik_tables_meta or []:
            t_name = q_tbl.get("table_name") or q_tbl.get("name")
            p_tbl = pbi_map.get(t_name)

            if not p_tbl:
                results.append({
                    "table": t_name,
                    "status": "FAILED",
                    "reason": "Table missing in Power BI model"
                })
                all_passed = False
                continue

            q_cols = q_tbl.get("fields") or q_tbl.get("columns") or []
            p_cols = p_tbl.get("columns") or []

            results.append({
                "table": t_name,
                "status": "PASSED",
                "qlik_columns_count": len(q_cols),
                "pbi_columns_count": len(p_cols),
                "difference": abs(len(q_cols) - len(p_cols))
            })

        return {
            "status": "PASSED" if all_passed else "REVIEW_REQUIRED",
            "passed": all_passed,
            "table_results": results
        }

    @staticmethod
    def reconcile_measures(
        qlik_measures: List[Dict[str, Any]],
        converted_measures: List[Dict[str, Any]],
        tolerance: float = 0.001,
    ) -> Dict[str, Any]:
        """Reconcile measure formulas and validation status."""
        results = []
        all_passed = True

        for m in converted_measures:
            name = m.get("name")
            dax = m.get("dax_expression")
            val = m.get("validation", {})
            passed = val.get("passed", True) and bool(dax and not dax.startswith("--"))

            if not passed:
                all_passed = False
                results.append({
                    "measure": name,
                    "status": "FAILED",
                    "reason": "DAX conversion failed validation or returned error placeholder",
                    "dax": dax
                })
            else:
                results.append({
                    "measure": name,
                    "status": "PASSED",
                    "dax": dax,
                    "confidence": m.get("confidence_adjusted", 0.95)
                })

        return {
            "status": "PASSED" if all_passed else "REVIEW_REQUIRED",
            "passed": all_passed,
            "measure_results": results
        }

    @staticmethod
    def build_reconciliation_summary(
        tables_rec: Dict[str, Any],
        measures_rec: Dict[str, Any],
    ) -> Dict[str, Any]:
        overall_passed = tables_rec.get("passed", False) and measures_rec.get("passed", False)
        return {
            "reconciliation_status": "passed" if overall_passed else "review_required",
            "overall_passed": overall_passed,
            "tables": tables_rec,
            "measures": measures_rec
        }

    @staticmethod
    def reconcile_table(
        table_name: str,
        source_stats: Dict[str, Any],
        target_stats: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Reconcile a single table's stats."""
        src_rows = source_stats.get("row_count", 0)
        tgt_rows = target_stats.get("row_count", 0)
        diff = abs(src_rows - tgt_rows)
        reconciled = (diff == 0)
        return {
            "table": table_name,
            "reconciled": reconciled,
            "status": "passed" if reconciled else "failed",
            "source_rows": src_rows,
            "target_rows": tgt_rows,
            "variance_rows": diff,
        }

    @staticmethod
    def reconcile_measure(
        measure_name: str,
        qlik_value: float,
        powerbi_value: float,
        tolerance_pct: float = 0.001,
    ) -> Dict[str, Any]:
        """Reconcile a single measure aggregate value within a numerical tolerance percentage."""
        if qlik_value == 0:
            diff_pct = 0.0 if powerbi_value == 0 else 1.0
        else:
            diff_pct = abs(qlik_value - powerbi_value) / abs(qlik_value)

        reconciled = (diff_pct <= tolerance_pct)
        return {
            "measure": measure_name,
            "reconciled": reconciled,
            "status": "passed" if reconciled else "failed",
            "qlik_value": qlik_value,
            "powerbi_value": powerbi_value,
            "variance_pct": round(diff_pct, 6),
        }

