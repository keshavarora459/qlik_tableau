from services.production_gate import ProductionGate, ProductionGateStatus

# Payload with an intentionally failed DAX validation on an individual measure
payload = {
    "tables": [
        {
            "name": "CourseInstructors",
            "columns": [{"fabric_column_name": "ID"}, {"fabric_column_name": "AssignedDate"}],
            "m_query": 'let Source = Sql.Database("srv", "db") in Source'
        }
    ],
    "measures": [
        {
            "name": "Assignment Duration",
            "qlik_expression": "Avg(Today() - [ASSIGNED_DATE])",
            "dax_expression": "AVERAGE(Today() - [ASSIGNED_DATE])",
            "validation": {
                "passed": False,
                "confidence_delta": -0.2,
                "failures": [
                    {
                        "name": "dax_no_bare_columns",
                        "passed": False,
                        "message": "unqualified columns: ['ASSIGNED_DATE']",
                        "confidence_delta": -0.2
                    }
                ]
            }
        }
    ],
    "relationships": [],
    "visuals": {}
}

gate_res = ProductionGate.evaluate(payload)

print("=== INDIVIDUAL MEASURE ===")
print("validation.passed:", payload["measures"][0]["validation"]["passed"])

print("\n=== GLOBAL RESULT ===")
print("dax_validation.passed:", gate_res.dax_validation.passed)
print("dax_validation string:", str(gate_res.dax_validation))
print("dax_validation == 'failed':", gate_res.dax_validation == "failed")
print("failures:", gate_res.dax_validation.failures)

print("\n=== FINAL PRODUCTION GATE ===")
print("publish_ready:", gate_res.publish_ready)
print("deployable:", gate_res.deployable)
print("status:", gate_res.status.value)
print("blocking_reasons:", gate_res.blocking_reasons)

assert payload["measures"][0]["validation"]["passed"] is False
assert gate_res.dax_validation.passed is False
assert gate_res.dax_validation == "failed"
assert gate_res.publish_ready is False
assert gate_res.deployable is False
assert gate_res.status == ProductionGateStatus.NOT_PRODUCTION_READY
print("\n>>> ALL FINAL VERIFICATIONS PASSED SUCCESSFULLY! <<<")
