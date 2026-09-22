"""ConfidenceEvaluator: several checks used to be hardcoded to "pass"
regardless of input, so broken output (a bare column, a placeholder M query
that never reaches the real connection) was still reported as high
confidence. These checks must now actually inspect the DAX/M text.
"""

from services.confidence_evaluator import ConfidenceEvaluator

TABLES = [
    {"name": "Drivers", "columns": [{"qlik_column_name": "first_name"}]},
]


def test_measure_with_bare_unresolved_column_fails_columns_exist():
    ev = ConfidenceEvaluator()
    result = ev.evaluate_measure("Sum(revenue)", "SUM(revenue)", TABLES)
    checks = {c["id"]: c["status"] for c in result["checks"]}
    assert checks["dax_columns_exist"] == "fail"
    assert result["requires_review"] is True


def test_measure_with_fully_qualified_column_passes():
    ev = ConfidenceEvaluator()
    result = ev.evaluate_measure("Sum(first_name)", "SUM('Drivers'[first_name])", TABLES)
    checks = {c["id"]: c["status"] for c in result["checks"]}
    assert checks["dax_columns_exist"] == "pass"
    assert checks["dax_no_bare_columns"] == "pass"
    assert result["requires_review"] is False


def test_table_placeholder_fallback_fails_and_is_flagged():
    """`let Source = TableName in Source` for a non-resident load means the
    real connection was lost - this must never score as high confidence."""
    ev = ConfidenceEvaluator()
    placeholder_mquery = "let\n    Source = COURSES\nin\n    Source"
    result = ev.evaluate_table(
        "COURSES", "source", placeholder_mquery, unresolved=[],
        expected_source_function="Snowflake.Databases",
    )
    checks = {c["id"]: c["status"] for c in result["checks"]}
    assert checks["m_no_placeholder_fallback"] == "fail"
    assert checks["m_source_matches_driver"] == "fail"
    assert result["requires_review"] is True
    assert result["band"] != "high"


def test_table_with_real_connector_call_passes():
    ev = ConfidenceEvaluator()
    mquery = (
        'let\n    Source = Snowflake.Databases("srv", "wh"),\n'
        '    Result = Value.NativeQuery(Source, "SELECT 1", null, [EnableFolding=false])\n'
        'in\n    Result'
    )
    result = ev.evaluate_table(
        "COURSES", "source", mquery, unresolved=[],
        expected_source_function="Snowflake.Databases",
    )
    checks = {c["id"]: c["status"] for c in result["checks"]}
    assert checks["m_no_placeholder_fallback"] == "pass"
    assert checks["m_source_matches_driver"] == "pass"
    assert result["requires_review"] is False
    assert result["band"] == "high"


def test_resident_load_is_exempt_from_placeholder_check():
    ev = ConfidenceEvaluator()
    mquery = "let\n    Source = UpstreamTable\nin\n    Source"
    result = ev.evaluate_table("Derived", "resident", mquery, unresolved=[], upstream_table="UpstreamTable")
    checks = {c["id"]: c["status"] for c in result["checks"]}
    assert checks["m_no_placeholder_fallback"] == "skip"
    assert result["requires_review"] is False
