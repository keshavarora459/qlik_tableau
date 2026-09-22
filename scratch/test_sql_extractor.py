import sys
import re
sys.path.append(r'c:\Users\Kesha\Desktop\Combining Mapping\qlik mapping')

def extract_sql_object_identifiers(sql):
    if not sql or not isinstance(sql, str):
        return {"database": None, "schema": None, "table": None}
    
    # Normalize repeated double quotes from Qlik literal escaping (e.g. """MEDISPHERE""" -> "MEDISPHERE")
    normalized_sql = re.sub(r'"{2,}', '"', sql)
    
    _SQL_TABLE_REF_PATTERN = re.compile(
        r'\b(?:FROM|JOIN)\s+((?:(?:"[^"]+"|\[[^\]]+\]|`[^`]+`|[a-zA-Z0-9_#$]+)(?:\s*\.\s*(?:"[^"]+"|\[[^\]]+\]|`[^`]+`|[a-zA-Z0-9_#$]+))*))',
        re.IGNORECASE
    )
    m = _SQL_TABLE_REF_PATTERN.search(normalized_sql)
    if not m:
        return {"database": None, "schema": None, "table": None}
    full_ref = m.group(1).strip()
    
    parts = re.findall(r'"[^"]+"|\[[^\]]+\]|`[^`]+`|[a-zA-Z0-9_#$]+', full_ref)
    clean_parts = []
    for p in parts:
        clean = p.strip()
        if clean.startswith("[") and clean.endswith("]"):
            clean = clean[1:-1]
        clean = clean.strip('"\'` ')
        if clean:
            clean_parts.append(clean)
    if len(clean_parts) >= 3:
        return {"database": clean_parts[0], "schema": clean_parts[1], "table": clean_parts[2]}
    elif len(clean_parts) == 2:
        return {"database": None, "schema": clean_parts[0], "table": clean_parts[1]}
    elif len(clean_parts) == 1:
        return {"database": None, "schema": None, "table": clean_parts[0]}
    return {"database": None, "schema": None, "table": None}

print("Learning Analytics:", extract_sql_object_identifiers('SELECT "COURSE_ID" FROM "LEARNING_ANALYTICS"."PUBLIC"."COURSES"'))
print("Medisphere:", extract_sql_object_identifiers('SELECT "BILL_ID" FROM """MEDISPHERE"""."""PUBLIC"""."""BILLS"""'))
print("Sales DW:", extract_sql_object_identifiers('SELECT * FROM [Sales DW].[dbo].[Orders]'))
print("Special chars:", extract_sql_object_identifiers('SELECT * FROM "My Special DB (East)"."dbo"."Orders"'))
print("Simple 2-part:", extract_sql_object_identifiers('SELECT * FROM public.orders'))
print("Simple 1-part:", extract_sql_object_identifiers('SELECT * FROM orders'))
