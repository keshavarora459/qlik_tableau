import sys
sys.path.append(r"c:\Users\Kesha\Desktop\Combining Mapping\qlik mapping")

from services.connection_mapper import ConnectionMapper, validate_m_query
from services.production_gate import ProductionGate

mapper = ConnectionMapper()
sql = 'SELECT "COURSE_ID", "COURSE_NAME" FROM "COURSES"'
conn = {'name': 'Mystery', 'driver': 'sqlserver', 'server': 'sqlsrv01'}

m = mapper.build_table_mquery("COURSES", "source", None, conn, sql)
print("=== Generated M for Missing DB ===")
print(m)

val = validate_m_query(m)
print("\n=== validate_m_query result ===")
print(val)

payload = {
    "tables": [
        {
            "name": "COURSES",
            "columns": [{"name": "COURSE_ID", "type": "string"}],
            "m_query": m,
        }
    ]
}
gate = ProductionGate.evaluate(payload)
print("\n=== ProductionGate evaluation ===")
print("Gate status:", gate.status)
print("m_validation:", gate.migration_status.get("m_validation"))
print("publish_ready:", gate.migration_status.get("publish_ready"))
print("deployable:", gate.migration_status.get("deployable"))
print("blocking_reasons:", gate.blocking_reasons)
