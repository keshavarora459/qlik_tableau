"""Production Readiness Gate & Migration Status Engine for Enterprise Qlik -> Power BI Migration."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from services.validators.dax_validators import validate_dax_schema_binding
from services.connection_mapper import validate_m_query


class ProductionGateStatus(str, Enum):
    PRODUCTION_READY = "PRODUCTION_READY"
    PRODUCTION_READY_WITH_REVIEW = "PRODUCTION_READY_WITH_REVIEW"
    NOT_PRODUCTION_READY = "NOT_PRODUCTION_READY"


class DaxValidationResult(dict):
    """
    Validation result container that supports:
    1. Dict access: res["passed"], res["failures"]
    2. Attribute access: res.passed, res.failures
    3. String equality: res == "passed", res == "failed"
    4. Boolean evaluation: bool(res) is True if passed else False
    5. JSON serialization as dict: {"passed": bool, "failures": [...]}
    """
    def __init__(self, passed: bool, failures: Optional[List[Dict[str, Any]]] = None):
        super().__init__(passed=passed, failures=failures or [])

    @property
    def passed(self) -> bool:
        return self["passed"]

    @property
    def failures(self) -> List[Dict[str, Any]]:
        return self["failures"]

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            expected = "passed" if self["passed"] else "failed"
            return other.lower() == expected
        if isinstance(other, bool):
            return self["passed"] == other
        return super().__eq__(other)

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __bool__(self) -> bool:
        return bool(self["passed"])

    def __str__(self) -> str:
        return "passed" if self["passed"] else "failed"


@dataclass
class GateEvaluation:
    status: ProductionGateStatus
    score: float
    blocking_reasons: List[str] = field(default_factory=list)
    review_items: List[str] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)
    migration_status: Dict[str, Any] = field(default_factory=dict)
    dax_validation: DaxValidationResult = field(default_factory=lambda: DaxValidationResult(True))

    @property
    def deployable(self) -> bool:
        return bool(self.migration_status.get("deployable", False))

    @property
    def publish_ready(self) -> bool:
        return bool(self.migration_status.get("publish_ready", False))

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        if key in self.migration_status:
            return self.migration_status[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        if key in self.migration_status:
            return self.migration_status[key]
        return default

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key) or (key in self.migration_status)


class ProductionGate:
    """Evaluates whether an end-to-end migration artifact is production ready according to strict Quality Gates."""

    @classmethod
    def evaluate(cls, mapping_payload: Dict[str, Any]) -> GateEvaluation:
        blocking_reasons: List[str] = []
        review_items: List[str] = []
        checks: Dict[str, bool] = {}

        tables = mapping_payload.get("tables", [])
        measures = mapping_payload.get("measures", [])
        relationships = mapping_payload.get("relationships", [])
        visuals = (mapping_payload.get("visuals") or {}).get("sheet_visuals", [])

        # 1. Power Query M Validation
        m_passed = True
        has_tables = len(tables) > 0 and all(len(t.get("columns", [])) > 0 for t in tables)
        if not has_tables:
            m_passed = False
            blocking_reasons.append("No valid tables with columns mapped in model")

        for t in tables:
            m_raw = t.get("m_query") or t.get("m_expression") or ""
            if isinstance(m_raw, list):
                m_q = "\n".join(str(step.get("content") or step.get("expression") or "") for step in m_raw if isinstance(step, dict))
            else:
                m_q = str(m_raw or "")
            t_name = t.get("name") or "Unknown"

            # Validate M query generically
            if m_q:
                val_res = validate_m_query(m_q)
                if not val_res["passed"]:
                    m_passed = False
                    for err in val_res["errors"]:
                        blocking_reasons.append(f"Table '{t_name}': {err}")

            # Raw lib:// paths
            if "lib://" in m_q or "Folder.Files(\"'lib:" in m_q:
                m_passed = False
                blocking_reasons.append(f"Table '{t_name}' contains unresolved Qlik lib:// connection path in M query")

            # QVD treated as CSV
            if ".qvd" in m_q.lower() and "csv.document" in m_q.lower():
                m_passed = False
                blocking_reasons.append(f"Table '{t_name}' attempts to parse QVD file with Csv.Document")

            # Unresolved placeholder table
            if "#table({\"*\"}" in m_q or "#table({}, {})" in m_q:
                m_passed = False
                blocking_reasons.append(f"Table '{t_name}' contains unresolved empty table placeholder")

            # Plaintext credentials
            if any(kw in m_q.lower() for kw in ["password=", "pwd=", "secret=", "apikey="]):
                m_passed = False
                blocking_reasons.append(f"Table '{t_name}' contains exposed plaintext credentials in M query")

            # Check if table confidence requires review
            if t.get("confidence", {}).get("requires_review"):
                rat = t.get("confidence", {}).get("rationale") or "Requires manual review"
                review_items.append(f"Table '{t_name}': {rat}")

        checks["m_validation"] = m_passed

        # 2. Model & Relationship Validation
        model_passed = True
        if not tables:
            model_passed = False
        table_names = {str(t.get("name", "")).lower() for t in tables}

        for r in relationships:
            src_t = str(r.get("source_table") or r.get("from_table") or "").lower()
            tgt_t = str(r.get("target_table") or r.get("to_table") or "").lower()
            if src_t and src_t not in table_names:
                model_passed = False
                blocking_reasons.append(f"Relationship source table '{src_t}' does not exist in model")
            if tgt_t and tgt_t not in table_names:
                model_passed = False
                blocking_reasons.append(f"Relationship target table '{tgt_t}' does not exist in model")

        checks["model_validation"] = model_passed

        # 3. DAX & Individual Measure Validation
        dax_passed = True
        dax_failures: List[Dict[str, Any]] = []

        # 3a. Validate each individual child measure
        for m in measures:
            if not isinstance(m, dict):
                dax_passed = False
                blocking_reasons.append("Invalid measure format: measure item is not an object")
                dax_failures.append({
                    "measure": "Unknown",
                    "errors": ["Measure item is not an object"],
                })
                continue

            m_name = m.get("name") or m.get("qlik_name") or "UnnamedMeasure"
            val = m.get("validation")
            if val is None and isinstance(m.get("fabric"), dict):
                val = m.get("fabric", {}).get("validation")

            # Missing validation handling
            if val is None:
                dax_expr = m.get("dax_expression") or (m.get("fabric") if isinstance(m.get("fabric"), dict) else {}).get("dax_expression")
                # If validation was explicitly None, or measure has no DAX expression to validate: fail safely
                if "validation" in m or not dax_expr or dax_expr == "BLANK()":
                    dax_passed = False
                    err_msg = f"Measure '{m_name}' has no validation result (missing validation)"
                    blocking_reasons.append(err_msg)
                    dax_failures.append({
                        "measure": m_name,
                        "errors": ["Measure has no validation result (missing validation)"],
                    })
                    continue
                else:
                    # Dynamically run validators so missing validation is never silently marked as passed
                    fabric_meta = m.get("fabric") if isinstance(m.get("fabric"), dict) else {"dax_expression": dax_expr}
                    from services.validators.dax_validators import run_dax_validators
                    val = run_dax_validators(fabric_meta, tables=tables)

            if isinstance(val, dict):
                val_passed = val.get("passed")
                val_failures = val.get("failures") or []

                if val_passed is False or (val_passed is None and val_failures) or len(val_failures) > 0:
                    dax_passed = False
                    measure_errors: List[str] = []
                    for f in val_failures:
                        if isinstance(f, dict):
                            msg = f.get("message") or f.get("error") or f.get("name") or "DAX validation failure"
                            measure_errors.append(str(msg))
                        elif isinstance(f, str):
                            measure_errors.append(f)

                    # Inspect full results list in case failures was omitted
                    if not measure_errors and isinstance(val.get("results"), list):
                        for r in val.get("results", []):
                            if isinstance(r, dict) and r.get("passed") is False:
                                msg = r.get("message") or r.get("error") or r.get("name") or "DAX check failed"
                                measure_errors.append(str(msg))

                    if not measure_errors:
                        generic_msg = val.get("message") or val.get("error") or "Validation failed"
                        measure_errors.append(str(generic_msg))

                    for err in measure_errors:
                        blocking_reasons.append(f"Measure '{m_name}': {err}")

                    dax_failures.append({
                        "measure": m_name,
                        "errors": measure_errors,
                    })

        # 3b. Model Schema Binding Validation
        dax_val_result = validate_dax_schema_binding(measures, tables)
        if not dax_val_result["valid"]:
            dax_passed = False
            for err in dax_val_result.get("errors", []):
                err_desc = err.get("error", "Schema binding error")
                blocking_reasons.append(f"DAX Schema Binding Error: {err_desc}")
                dax_failures.append({
                    "measure": err.get("measure", "Unknown"),
                    "errors": [err_desc],
                })

        unresolved_measures = [
            m.get("name") for m in measures
            if isinstance(m, dict) and (m.get("dax_expression") == "BLANK()" or m.get("conversion_method") == "unresolved")
        ]
        if unresolved_measures:
            review_items.append(f"{len(unresolved_measures)} measure(s) unresolved: {unresolved_measures[:3]}")

        dax_val_obj = DaxValidationResult(passed=dax_passed, failures=dax_failures)
        checks["dax_validation"] = dax_passed

        # 4. Visual Validation
        visual_passed = True
        unsupported_visuals = [
            v.get("name") for v in visuals
            if not v.get("fabric", {}).get("supported", True)
        ]
        if unsupported_visuals:
            review_items.append(f"{len(unsupported_visuals)} visual(s) without direct Fabric equivalent: {unsupported_visuals[:3]}")

        unbound_visuals = [
            v.get("name") for v in visuals
            if not v.get("fabric", {}).get("field_roles") and v.get("fabric", {}).get("supported", True)
        ]
        if unbound_visuals:
            review_items.append(f"{len(unbound_visuals)} visual(s) have unpopulated field roles")

        checks["visual_validation"] = visual_passed

        # Overall Status Determination
        requires_review = bool(blocking_reasons or review_items)
        publish_ready = bool(
            m_passed
            and model_passed
            and dax_passed
            and visual_passed
            and not blocking_reasons
        )

        if not publish_ready and blocking_reasons:
            status = ProductionGateStatus.NOT_PRODUCTION_READY
            score = 0.4
            conversion_status = "partial" if (tables or measures) else "failed"
        elif requires_review:
            status = ProductionGateStatus.PRODUCTION_READY_WITH_REVIEW
            score = 0.85
            conversion_status = "converted"
        else:
            status = ProductionGateStatus.PRODUCTION_READY
            score = 1.0
            conversion_status = "converted"

        # Construct Requirement 30 compliant migration_status schema
        migration_status = {
            "conversion_status": conversion_status,
            "conversion_method": "hybrid" if mapping_payload.get("llm_status", {}).get("used") else "deterministic",
            "m_validation": "passed" if m_passed else "failed",
            "model_validation": "passed" if model_passed else "failed",
            "dax_validation": dax_val_obj,
            "visual_validation": "passed" if visual_passed else "failed",
            "runtime_desktop": mapping_payload.get("runtime_desktop", "not_tested"),
            "runtime_service": mapping_payload.get("runtime_service", "not_tested"),
            "reconciliation": mapping_payload.get("reconciliation", "not_tested"),
            "requires_review": requires_review,
            "publish_ready": publish_ready,
            "deployable": bool(publish_ready and not blocking_reasons),
            "blocking_reasons": blocking_reasons,
            "review_items": review_items,
            "dax_failures": dax_failures,
        }

        return GateEvaluation(
            status=status,
            score=score,
            blocking_reasons=blocking_reasons,
            review_items=review_items,
            checks=checks,
            migration_status=migration_status,
            dax_validation=dax_val_obj,
        )
