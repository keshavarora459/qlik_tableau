import json
import sys

try:
    with open(r'C:\Users\Kesha\Desktop\learning analytics mapping.txt', 'r', encoding='utf-8') as f:
        data = json.load(f)

    if 'mapping_result' in data:
        data = data['mapping_result']

    print('Connections:')
    for c in data.get('connections', []):
        print(f"  Server: {c.get('server')}")
        print(f"  Database: {c.get('database')}")
        print(f"  Username: {c.get('username')}")
        fabric = c.get('fabric', {})
        print(f"  M Expression: {fabric.get('m_expression')}")

    print('\nMeasures with validation errors:')
    for m in data.get('measures', []):
        val = m.get('validation', {})
        if not val.get('passed', True):
            print(f"  {m.get('name')}: {val.get('errors')}")

    print('\nColumns with validation errors:')
    for t in data.get('tables', []):
        for c in t.get('columns', []):
            if c.get('is_calculated'):
                val = c.get('validation', {})
                if not val.get('passed', True):
                    print(f"  {t.get('name')}.{c.get('name')}: {val.get('errors')}")
except Exception as e:
    print("Error:", e)
