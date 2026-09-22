import sys, re
sys.path.append(r'c:\Users\Kesha\Desktop\Combining Mapping\qlik mapping')
from services.connection_mapper import MQuerySchemaTracker, build_type_step

qlik_query = """
LOAD INSTRUCTOR_ID,
     FIRST_NAME AS [INSTRUCTORS.FIRST_NAME],
     LAST_NAME AS [INSTRUCTORS.LAST_NAME],
     EMAIL AS [INSTRUCTORS.EMAIL],
     HIRE_DATE;
"""

rename_pairs = []
# Case 1: [old] AS [new]
for match in re.finditer(r'\[([^\]]+)\]\s+AS\s+\[([^\]]+)\]', qlik_query, re.IGNORECASE):
    old_col, new_col = match.group(1), match.group(2)
    if old_col.lower() != new_col.lower():
        rename_pairs.append((old_col, new_col))
# Case 2: word AS [alias with dots]
for match in re.finditer(r'\b([A-Za-z0-9_]+)\s+AS\s+\[([^\]]+)\]', qlik_query, re.IGNORECASE):
    old_col, new_col = match.group(1), match.group(2)
    if old_col.lower() not in ('load','select','from','where','group','by','as','resident') and old_col.lower() != new_col.lower():
        if not any(old.lower() == old_col.lower() for old, _ in rename_pairs):
            rename_pairs.append((old_col, new_col))
# Case 3: word AS word
for match in re.finditer(r'\b([A-Za-z0-9_]+)\s+AS\s+([A-Za-z0-9_]+)\b', qlik_query, re.IGNORECASE):
    old_col, new_col = match.group(1), match.group(2)
    if old_col.lower() not in ('load','select','from','where','group','by','as','resident') and old_col.lower() != new_col.lower():
        if not any(old.lower() == old_col.lower() for old, _ in rename_pairs):
            rename_pairs.append((old_col, new_col))

print('Extracted rename_pairs:', rename_pairs)

# columns metadata as returned by the mapping agent (fabric_column_name uses underscore)
columns = [
    {'qlik_column_name': 'INSTRUCTOR_ID',         'fabric_column_name': 'INSTRUCTOR_ID',         'fabric_datatype': 'string'},
    {'qlik_column_name': 'INSTRUCTORS.FIRST_NAME', 'fabric_column_name': 'INSTRUCTORS_FIRST_NAME', 'fabric_datatype': 'string'},
    {'qlik_column_name': 'INSTRUCTORS.LAST_NAME',  'fabric_column_name': 'INSTRUCTORS_LAST_NAME',  'fabric_datatype': 'string'},
    {'qlik_column_name': 'INSTRUCTORS.EMAIL',      'fabric_column_name': 'INSTRUCTORS_EMAIL',      'fabric_datatype': 'string'},
    {'qlik_column_name': 'HIRE_DATE',              'fabric_column_name': 'HIRE_DATE',              'fabric_datatype': 'dateTime'},
]

def _strip(s): return re.sub(r'[^a-z0-9]', '', s.lower())

final_cols = [c.get('fabric_column_name') or c.get('qlik_column_name') or c.get('name') for c in columns]
renamed_to_old_stripped = {_strip(n): o for o, n in rename_pairs}
initial_cols = []
for f in final_cols:
    f_stripped = _strip(f)
    if f_stripped in renamed_to_old_stripped:
        initial_cols.append(renamed_to_old_stripped[f_stripped])
    else:
        initial_cols.append(f)
for o, n in rename_pairs:
    if not any(_strip(ic) == _strip(o) for ic in initial_cols):
        initial_cols.append(o)

print('Initial schema:', initial_cols)

tracker = MQuerySchemaTracker(initial_cols)
tracker.apply_rename(rename_pairs, step_name='Renamed Columns')
print('Schema after rename:', tracker.active_columns)

prev_step = '#"Renamed Columns"'
type_step = build_type_step(prev_step, columns, tracker)
print('\nChanged Type step:')
print(type_step)

# Validate: no underscore-version of renamed columns should appear
for c in columns:
    qname = c.get('qlik_column_name','')
    fname = c.get('fabric_column_name','')
    if qname != fname:
        # The qname (with dot) should appear in the type step, not fname (with underscore)
        assert qname in type_step, f'FAIL: Expected {qname!r} in type step'
        assert fname not in type_step or fname == qname, f'FAIL: Unexpected underscore name {fname!r} in type step'

print('\nALL ASSERTIONS PASSED')
