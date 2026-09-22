import sys
import os
import re

sys.path.append(r'c:\Users\Kesha\Desktop\Combining Mapping\qlik mapping')
from services.connection_mapper import MQuerySchemaTracker, MQuerySchemaReferenceError, build_type_step

def test_pattern_a():
    print("TEST A: A -> B -> type conversion")
    tracker = MQuerySchemaTracker(["A"])
    tracker.apply_rename([("A", "B")], step_name="Rename")
    columns_meta = [{"fabric_column_name": "B", "fabric_datatype": "int64"}]
    type_m = build_type_step("prev", columns_meta, tracker)
    assert '{"B", Int64.Type}' in type_m
    print("PASS A")

def test_pattern_b():
    print("TEST B: A -> B -> C -> type conversion")
    tracker = MQuerySchemaTracker(["A"])
    tracker.apply_rename([("A", "B")], step_name="Rename1")
    tracker.apply_rename([("B", "C")], step_name="Rename2")
    columns_meta = [{"fabric_column_name": "C", "fabric_datatype": "int64"}]
    type_m = build_type_step("prev", columns_meta, tracker)
    assert '{"C", Int64.Type}' in type_m
    print("PASS B")

def test_pattern_c():
    print("TEST C: Multiple columns renamed simultaneously")
    tracker = MQuerySchemaTracker(["A", "B"])
    tracker.apply_rename([("A", "A_new"), ("B", "B_new")], step_name="Rename")
    columns_meta = [{"fabric_column_name": "A_new", "fabric_datatype": "int64"}, {"fabric_column_name": "B_new", "fabric_datatype": "double"}]
    type_m = build_type_step("prev", columns_meta, tracker)
    assert '{"A_new", Int64.Type}' in type_m
    assert '{"B_new", type number}' in type_m
    print("PASS C")

def test_pattern_d():
    print("TEST D: Rename + remove + type conversion")
    tracker = MQuerySchemaTracker(["A", "B"])
    tracker.apply_rename([("A", "A_new")], step_name="Rename")
    tracker.apply_remove(["B"], step_name="Remove")
    columns_meta = [{"fabric_column_name": "A_new", "fabric_datatype": "int64"}, {"fabric_column_name": "B", "fabric_datatype": "double"}]
    type_m = build_type_step("prev", columns_meta, tracker)
    assert '{"A_new", Int64.Type}' in type_m
    assert '{"B"' not in type_m
    print("PASS D")

def test_pattern_j():
    print("TEST J: Columns containing dots, underscores, spaces, and special characters")
    tracker = MQuerySchemaTracker(["STUDENTS.FIRST_NAME", "LAST NAME!"])
    tracker.apply_rename([("STUDENTS.FIRST_NAME", "First_Name")], step_name="Rename")
    # type resolution should ignore special characters when matching
    columns_meta = [{"fabric_column_name": "first_name", "fabric_datatype": "int64"}]
    type_m = build_type_step("prev", columns_meta, tracker)
    assert '{"First_Name", Int64.Type}' in type_m
    print("PASS J")

def test_validation_error():
    print("TEST VALIDATION ERROR")
    tracker = MQuerySchemaTracker(["A"])
    try:
        tracker.apply_rename([("B", "C")], step_name="Rename")
        assert False, "Should have thrown MQuerySchemaReferenceError"
    except MQuerySchemaReferenceError as e:
        d = e.to_dict()
        assert d["error"] == "M_QUERY_SCHEMA_REFERENCE_ERROR"
        assert d["column"] == "B"
    print("PASS VALIDATION ERROR")


if __name__ == "__main__":
    test_pattern_a()
    test_pattern_b()
    test_pattern_c()
    test_pattern_d()
    test_pattern_j()
    test_validation_error()
    print("ALL TESTS PASSED")
