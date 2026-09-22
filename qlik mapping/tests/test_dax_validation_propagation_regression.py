"""Generic regression tests for DAX validation propagation and final production gate evaluation.

Verifies:
TEST 1: All measure validations pass -> dax_validation.passed = true, deployable = true, publish_ready = true.
TEST 2: One measure validation fails -> dax_validation.passed = false, deployable = false, publish_ready = false.
TEST 3: Multiple measure validations fail -> dax_validation.passed = false, all failures preserved, deployable = false.
TEST 4: Measure has validation.passed = false but top-level summary previously says passed -> child failure wins, final result is failed.
TEST 5: Measure has no validation result -> fail safely (dax_validation.passed = false, deployable = false, publish_ready = false).
TEST 6: All DAX validations pass but another required validation fails -> deployable = false (confirms production gate checks all categories).
"""

import pytest
from services.production_gate import (
    DaxValidationResult,
    ProductionGate,
    ProductionGateStatus,
)


def _build_valid_table():
    return {
        "name": "EntityTable",
        "columns": [
            {"fabric_column_name": "Id", "type": "int64"},
            {"fabric_column_name": "Amount", "type": "double"},
            {"fabric_column_name": "Status", "type": "string"},
        ],
        "m_query": (
            "let\n"
            "    Source = Sql.Database(\"server.domain.net\", \"DatabaseName\"),\n"
            "    EntityTable = Source{[Schema=\"dbo\",Item=\"EntityTable\"]}[Data]\n"
            "in\n"
            "    EntityTable"
        ),
    }


def test_1_all_measure_validations_pass():
    """TEST 1:
    All measure validations pass.
    Expected:
        dax_validation.passed = true
        deployable = true
        publish_ready = true
        (assuming all other required validations also pass)
    """
    valid_table = _build_valid_table()
    measures = [
        {
            "name": "MetricOne",
            "dax_expression": "SUM('EntityTable'[Amount])",
            "validation": {
                "passed": True,
                "confidence_delta": 0.0,
                "failures": [],
                "results": [{"name": "dax_balanced", "passed": True, "message": ""}],
            },
        },
        {
            "name": "MetricTwo",
            "dax_expression": "COUNTROWS('EntityTable')",
            "validation": {
                "passed": True,
                "confidence_delta": 0.0,
                "failures": [],
                "results": [{"name": "dax_balanced", "passed": True, "message": ""}],
            },
        },
    ]

    payload = {
        "tables": [valid_table],
        "measures": measures,
        "relationships": [],
        "visuals": {
            "sheet_visuals": [
                {
                    "name": "MetricCard",
                    "fabric": {
                        "visual_type": "card",
                        "supported": True,
                        "field_roles": {"Values": ["MetricOne"]},
                    },
                }
            ]
        },
    }

    gate_res = ProductionGate.evaluate(payload)

    # DAX Validation
    assert gate_res.dax_validation.passed is True
    assert gate_res.dax_validation["passed"] is True
    assert gate_res.dax_validation == "passed"
    assert gate_res.checks["dax_validation"] is True
    assert gate_res.migration_status["dax_validation"].passed is True
    assert gate_res.migration_status["dax_validation"] == "passed"
    assert len(gate_res.dax_validation.failures) == 0

    # Production Gate
    assert gate_res.publish_ready is True
    assert gate_res.migration_status["publish_ready"] is True
    assert gate_res.deployable is True
    assert gate_res.migration_status["deployable"] is True
    assert gate_res.status == ProductionGateStatus.PRODUCTION_READY
    assert len(gate_res.blocking_reasons) == 0


def test_2_one_measure_validation_fails():
    """TEST 2:
    One measure validation fails.
    Expected:
        dax_validation.passed = false
        deployable = false
        publish_ready = false
    """
    valid_table = _build_valid_table()
    measures = [
        {
            "name": "MetricPassing",
            "dax_expression": "SUM('EntityTable'[Amount])",
            "validation": {
                "passed": True,
                "failures": [],
            },
        },
        {
            "name": "MetricFailing",
            "dax_expression": "AVERAGE([UnqualifiedField])",
            "validation": {
                "passed": False,
                "confidence_delta": -0.2,
                "failures": [
                    {
                        "name": "dax_no_bare_columns",
                        "passed": False,
                        "message": "unqualified columns: ['UnqualifiedField']",
                    }
                ],
            },
        },
    ]

    payload = {
        "tables": [valid_table],
        "measures": measures,
        "relationships": [],
        "visuals": {
            "sheet_visuals": [
                {
                    "name": "MetricCard",
                    "fabric": {
                        "visual_type": "card",
                        "supported": True,
                        "field_roles": {"Values": ["MetricPassing"]},
                    },
                }
            ]
        },
    }

    gate_res = ProductionGate.evaluate(payload)

    # DAX Validation must fail
    assert gate_res.dax_validation.passed is False
    assert gate_res.dax_validation["passed"] is False
    assert gate_res.dax_validation == "failed"
    assert gate_res.checks["dax_validation"] is False
    assert gate_res.migration_status["dax_validation"].passed is False
    assert gate_res.migration_status["dax_validation"] == "failed"

    # Production Gate must block deployment and publishing
    assert gate_res.publish_ready is False
    assert gate_res.migration_status["publish_ready"] is False
    assert gate_res.deployable is False
    assert gate_res.migration_status["deployable"] is False
    assert gate_res.status == ProductionGateStatus.NOT_PRODUCTION_READY

    # Blocking reasons and failure details preserved
    assert len(gate_res.blocking_reasons) >= 1
    assert any("MetricFailing" in r and "UnqualifiedField" in r for r in gate_res.blocking_reasons)
    assert len(gate_res.dax_validation.failures) == 1
    assert gate_res.dax_validation.failures[0]["measure"] == "MetricFailing"
    assert "unqualified columns: ['UnqualifiedField']" in gate_res.dax_validation.failures[0]["errors"][0]


def test_3_multiple_measure_validations_fail():
    """TEST 3:
    Multiple measure validations fail.
    Expected:
        dax_validation.passed = false
        all failures are preserved in the final validation output
        deployable = false
    """
    valid_table = _build_valid_table()
    measures = [
        {
            "name": "MetricFail1",
            "dax_expression": "CALCULATE(SUM('EntityTable'[Amount]), BANNED_FN())",
            "validation": {
                "passed": False,
                "failures": [
                    {
                        "name": "dax_no_banned_functions",
                        "passed": False,
                        "message": "BANNED_FN is not supported in Fabric DAX",
                    }
                ],
            },
        },
        {
            "name": "MetricPass",
            "dax_expression": "SUM('EntityTable'[Amount])",
            "validation": {"passed": True, "failures": []},
        },
        {
            "name": "MetricFail2",
            "dax_expression": "AVERAGE([BareColumnA])",
            "validation": {
                "passed": False,
                "failures": [
                    {
                        "name": "dax_no_bare_columns",
                        "passed": False,
                        "message": "unqualified columns: ['BareColumnA']",
                    }
                ],
            },
        },
    ]

    payload = {
        "tables": [valid_table],
        "measures": measures,
        "relationships": [],
        "visuals": {},
    }

    gate_res = ProductionGate.evaluate(payload)

    # DAX Validation must fail
    assert gate_res.dax_validation.passed is False
    assert gate_res.deployable is False
    assert gate_res.publish_ready is False

    # All failures must be preserved
    failures = gate_res.dax_validation.failures
    failing_measure_names = {f["measure"] for f in failures}
    assert "MetricFail1" in failing_measure_names
    assert "MetricFail2" in failing_measure_names
    assert len(failures) == 2

    # Check blocking reasons preserve both failures
    assert any("MetricFail1" in r for r in gate_res.blocking_reasons)
    assert any("MetricFail2" in r for r in gate_res.blocking_reasons)


def test_4_child_failure_overrides_stale_top_level_passed_summary():
    """TEST 4:
    A measure has:
        validation.passed = false
    but the top-level summary previously says passed.
    Expected:
        the child failure wins.
        The final result MUST be failed.
    """
    valid_table = _build_valid_table()
    measures = [
        {
            "name": "StaleMeasure",
            "dax_expression": "SUM([UnqualifiedCol])",
            "validation": {
                "passed": False,
                "failures": [
                    {
                        "name": "dax_no_bare_columns",
                        "passed": False,
                        "message": "unqualified columns: ['UnqualifiedCol']",
                    }
                ],
            },
        }
    ]

    # Payload includes stale top-level claims that DAX passed
    payload = {
        "tables": [valid_table],
        "measures": measures,
        "relationships": [],
        "dax_validation": "passed",
        "migration_status": {
            "dax_validation": "passed",
            "publish_ready": True,
            "deployable": True,
        },
    }

    gate_res = ProductionGate.evaluate(payload)

    # Child failure must win: top-level becomes failed
    assert gate_res.dax_validation.passed is False
    assert gate_res.dax_validation == "failed"
    assert gate_res.deployable is False
    assert gate_res.publish_ready is False
    assert gate_res.status == ProductionGateStatus.NOT_PRODUCTION_READY
    assert len(gate_res.blocking_reasons) >= 1


def test_5_missing_validation_fails_safely():
    """TEST 5:
    A measure has no validation result.
    Expected:
        do not silently mark DAX validation as passed.
        Follow the project's defined policy or fail safely.
    """
    valid_table = _build_valid_table()
    measures = [
        {
            "name": "UnvalidatedMeasure",
            "dax_expression": "SUM('EntityTable'[Amount])",
            "validation": None,
        }
    ]

    payload = {
        "tables": [valid_table],
        "measures": measures,
        "relationships": [],
    }

    gate_res = ProductionGate.evaluate(payload)

    # Missing validation must fail safely
    assert gate_res.dax_validation.passed is False
    assert gate_res.dax_validation == "failed"
    assert gate_res.deployable is False
    assert gate_res.publish_ready is False
    assert gate_res.status == ProductionGateStatus.NOT_PRODUCTION_READY

    # Must contain a clear validation failure message
    assert any("no validation result" in r or "missing validation" in r for r in gate_res.blocking_reasons)
    assert len(gate_res.dax_validation.failures) == 1
    assert gate_res.dax_validation.failures[0]["measure"] == "UnvalidatedMeasure"


def test_6_all_dax_validations_pass_but_another_required_validation_fails():
    """TEST 6:
    All DAX validations pass but another required validation fails.
    Expected:
        deployable = false
    This confirms the final production gate is based on ALL required validation categories.
    """
    # Create a table with invalid M query (contains unresolved placeholder)
    invalid_table = {
        "name": "BrokenTable",
        "columns": [{"fabric_column_name": "Id", "type": "int64"}],
        "m_query": "let Source = #table({\"*\"}, {}) in Source",
    }

    # DAX measure is completely valid
    measures = [
        {
            "name": "ValidMeasure",
            "dax_expression": "SUM('BrokenTable'[Id])",
            "validation": {
                "passed": True,
                "failures": [],
            },
        }
    ]

    payload = {
        "tables": [invalid_table],
        "measures": measures,
        "relationships": [],
    }

    gate_res = ProductionGate.evaluate(payload)

    # DAX passes
    assert gate_res.dax_validation.passed is True
    assert gate_res.dax_validation == "passed"
    assert gate_res.checks["dax_validation"] is True

    # But M validation fails
    assert gate_res.checks["m_validation"] is False
    assert gate_res.migration_status["m_validation"] == "failed"

    # Final gate must still block deployment
    assert gate_res.deployable is False
    assert gate_res.migration_status["deployable"] is False
    assert gate_res.publish_ready is False
    assert gate_res.status == ProductionGateStatus.NOT_PRODUCTION_READY
