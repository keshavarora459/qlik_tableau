import pytest
from services.input_normalizer import normalize_measures, get_measure_identity
from services.dax_converter import DAXConverter
from agents.coordinator_agent import _deduplicate_measures

def test_extract_measure_name_from_comments():
    raw = {
        "expression": "// Decayed GPA Quality Index\nSUM(GPA)"
    }
    m = normalize_measures([raw])[0]
    assert m["name"] == "Decayed GPA Quality Index"
    assert m["expression"] == "// Decayed GPA Quality Index\nSUM(GPA)"
    
    raw2 = {
        "expression": "/*   Another Measure   */ AVG(X)"
    }
    m2 = normalize_measures([raw2])[0]
    assert m2["name"] == "Another Measure"
    assert m2["expression"] == "/*   Another Measure   */ AVG(X)"

def test_canonicalize_expression():
    raw1 = {"expression": "SUM(Sales)"}
    raw2 = {"expression": "  sum( Sales )  "}
    raw3 = {"expression": "// my sum\nSUM(Sales)"}
    
    m1 = normalize_measures([raw1])[0]
    m2 = normalize_measures([raw2])[0]
    m3 = normalize_measures([raw3])[0]
    
    assert get_measure_identity(m1) == get_measure_identity(m2)
    assert get_measure_identity(m2) == get_measure_identity(m3)

def test_deduplicate_identical_expressions():
    measures = [
        {"name": "M1", "expression": "SUM(X)"},
        {"name": "M2", "expression": "sum( x )"}
    ]
    norm = normalize_measures(measures)
    assert len(norm) == 1
    
    deduped = _deduplicate_measures(norm)
    assert len(deduped) == 1
    assert deduped[0]["name"] == "M1"

def test_squash_identical_expressions_with_distinct_ids():
    measures = [
        {"qInfo": {"qId": "ID1"}, "name": "M1", "expression": "SUM(X)"},
        {"qInfo": {"qId": "ID2"}, "name": "M2", "expression": "SUM(X)"}
    ]
    # They should be squashed because the canonical expression is the same
    norm = normalize_measures(measures)
    assert len(norm) == 1
    
    deduped = _deduplicate_measures(norm)
    assert len(deduped) == 1

def test_different_expressions_separate_measures():
    measures = [
        {"name": "M1", "expression": "SUM(X)"},
        {"name": "M1", "expression": "SUM(Y)"}
    ]
    norm = normalize_measures(measures)
    assert len(norm) == 2
    
    deduped = _deduplicate_measures(norm)
    assert len(deduped) == 2
    assert deduped[0]["name"] == "M1"
    assert deduped[1]["name"] == "M1 1"

def test_target_table_resolution():
    converter = DAXConverter()
    
    m_item = {
        "name": "M1",
        "expression": "SUM(Orders[Amount])",
        "tables": []
    }
    
    # Fake known_tables so DAXConverter can map it
    known_tables = [{"name": "Orders", "columns": [{"name": "Amount"}]}]
    
    res = converter.convert_measure(m_item, known_tables)
    assert res["fabric"]["table"] == "Orders"
    
    m_item_no_table = {
        "name": "M2",
        "expression": "1 + 1",
        "tables": []
    }
    res_no_table = converter.convert_measure(m_item_no_table, known_tables)
    assert res_no_table["fabric"]["table"] == "_Measures"
