import sys
sys.path.append(r'c:\Users\Kesha\Desktop\Combining Mapping\qlik mapping')
from services.set_analysis_ast import SetAnalysisParser, SetAnalysisDAXEmitter
from services.dax_identifiers import build_column_index
from services.dax_converter import DAXConverter

c = DAXConverter()
tables = [{'name': 'Table1', 'columns': [{'name': 'DATE'}]}]
index = build_column_index(tables)
resolver = lambda x: c._table_of(x, index)

ast = SetAnalysisParser.parse('Sum({<Date={"X"}>} 1)')
emitter = SetAnalysisDAXEmitter(resolver)
dax = emitter.emit(ast, tables)
print('AST DAX:', dax)
