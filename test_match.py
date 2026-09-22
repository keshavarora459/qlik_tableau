import sys
sys.path.append(r'c:\Users\Kesha\Desktop\Combining Mapping\qlik mapping')
from services.dax_converter import DAXConverter

TABLES = [
    {
        'name': 'GRADES',
        'columns': [
            {'qlik_column_name': 'GRADE'},
        ]
    }
]

tests = [
    "Match(Upper(Trim([GRADE])), 'A+', 'A', 'A-')",
    "Match([GRADE], 'A', 'B')"
]

for t in tests:
    converter = DAXConverter()
    dax = converter.qlik_to_dax(t, TABLES)
    print(f'Original: {t}')
    print(f'DAX: {dax}')
    print(f'Functions: {converter.function_mapping}')
    print('-' * 40)
