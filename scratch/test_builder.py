import sys
sys.path.append(r'c:\Users\Kesha\Desktop\Combining Mapping\qlik mapping')
from services.connection_mapper import ConnectionMapper
mapper = ConnectionMapper()
sql = 'SELECT "BILL_ID" FROM "MEDISPHERE"."PUBLIC"."BILLS"'
print(mapper._build_raw_table_mquery('BILLS', conn_details={'name': 'Medisphere', 'connector_type': 'Database'}, custom_sql=sql))
