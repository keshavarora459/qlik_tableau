"""RangeSum()/Above()/Below() were completely unhandled: left as literal
Qlik syntax in the "converted" DAX, which is invalid and fails to evaluate
in Power BI. RangeSum has a faithful direct DAX rewrite; Above/Below don't
(they need an explicit row-window DAX has no literal form for), so those
must at least be caught by confidence scoring instead of silently passing.
"""

from services.confidence_evaluator import ConfidenceEvaluator
from services.dax_converter import DAXConverter

TABLES = [{"name": "T", "columns": [
    {"qlik_column_name": "a"}, {"qlik_column_name": "b"}, {"qlik_column_name": "c"},
]}]


def test_rangesum_converts_to_addition():
    dax = DAXConverter().qlik_to_dax("RangeSum(a, b, c)", TABLES)
    assert "RangeSum" not in dax
    assert dax == "('T'[a] + 'T'[b] + 'T'[c])"


def test_rangesum_handles_nested_expressions():
    dax = DAXConverter().qlik_to_dax("RangeSum(Sum(a), Sum(b))", TABLES)
    assert "RangeSum" not in dax
    assert dax == "(SUM('T'[a]) + SUM('T'[b]))"


def test_above_is_flagged_as_needing_review_not_silently_passed():
    ce = ConfidenceEvaluator()
    result = ce.evaluate_measure("Above(Sum(x), 1)", "Above(SUM('T'[x]), 1)", TABLES)
    banned = next(c for c in result["checks"] if c["id"] == "dax_no_banned_functions")
    assert banned["status"] == "fail"
    assert result["requires_review"] is True


def test_below_is_flagged_as_needing_review_not_silently_passed():
    ce = ConfidenceEvaluator()
    result = ce.evaluate_measure("Below(Sum(x), 1)", "Below(SUM('T'[x]), 1)", TABLES)
    banned = next(c for c in result["checks"] if c["id"] == "dax_no_banned_functions")
    assert banned["status"] == "fail"


def test_clean_dax_still_passes_the_banned_function_check():
    ce = ConfidenceEvaluator()
    result = ce.evaluate_measure("Sum(a)", "SUM('T'[a])", TABLES)
    banned = next(c for c in result["checks"] if c["id"] == "dax_no_banned_functions")
    assert banned["status"] == "pass"
