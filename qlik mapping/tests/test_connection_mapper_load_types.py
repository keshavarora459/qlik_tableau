"""build_table_mquery: INLINE, REST/JSON, and file (CSV/Excel) loads each had
a genuine crash-risk or silent-data-loss bug found by a dedicated audit:

  - INLINE: an extra Table.PromoteHeaders after Table.FromRows (which already
    sets real headers) promoted the first *data* row into column names and
    discarded it, then TransformColumnTypes referenced headers that no
    longer existed - an Expression.Error on every INLINE-loaded table.
  - REST/JSON: the column-type list was a hardcoded Binance ticker schema
    applied to every REST source regardless of its real columns - any other
    API would error with "column not found".
  - CSV/Excel: no Table.TransformColumnTypes step at all, so every column
    silently imported as text regardless of its real type.
  - An unmatched connector + custom_sql built Value.NativeQuery against
    Folder.Files(...), which can't execute SQL - silently-wrong M that looks
    valid but fails at evaluation time.
"""

from services.connection_mapper import ConnectionMapper

mapper = ConnectionMapper()


def test_inline_load_does_not_double_promote_headers():
    query = "LOAD * INLINE [\nCol1, Col2\napple, 1\nbanana, 2\n];"
    mquery = mapper.build_table_mquery("Fruits", "inline", None, None, qlik_query=query)
    assert "Table.PromoteHeaders" not in mquery
    assert 'Table.TransformColumnTypes(Source,' in mquery
    assert '"Col1"' in mquery and '"Col2"' in mquery


def test_rest_load_uses_real_column_types_not_a_hardcoded_schema():
    query = 'LOAD * FROM Json.Document(Web.Contents("https://example.com/api/data"));'
    columns = [
        {"qlik_column_name": "price", "fabric_datatype": "double"},
        {"qlik_column_name": "symbol", "fabric_datatype": "string"},
    ]
    mquery = mapper.build_table_mquery(
        "Ticker", "source", None, {"driver": "rest"}, qlik_query=query, columns=columns
    )
    assert "binance" not in mquery.lower()
    assert '"price", type number' in mquery
    assert '"symbol", type text' in mquery


def test_rest_hint_with_no_discoverable_url_does_not_fabricate_one():
    mquery = mapper.build_table_mquery(
        "Ticker", "source", None, {"driver": "rest"}, qlik_query="LOAD * FROM some_rest_source;"
    )
    assert "Web.Contents" not in mquery
    assert mquery == "let\n    Source = Ticker\nin\n    Source"


def test_csv_load_casts_real_column_types():
    query = "LOAD * FROM [lib://DataFiles/sales.csv];"
    columns = [
        {"qlik_column_name": "Amount", "fabric_datatype": "double"},
        {"qlik_column_name": "OrderDate", "fabric_datatype": "dateTime"},
    ]
    mquery = mapper.build_table_mquery(
        "Sales", "source", None, {"driver": "datafiles"}, qlik_query=query, columns=columns
    )
    assert "Table.TransformColumnTypes" in mquery
    assert '"Amount", type number' in mquery
    assert '"OrderDate", type datetime' in mquery


def test_excel_load_casts_real_column_types():
    query = "LOAD * FROM [lib://DataFiles/sales.xlsx];"
    columns = [{"qlik_column_name": "Amount", "fabric_datatype": "double"}]
    mquery = mapper.build_table_mquery(
        "Sales", "source", None, {"driver": "datafiles"}, qlik_query=query, columns=columns
    )
    assert "Excel.Workbook" in mquery
    assert "Table.TransformColumnTypes" in mquery
    assert '"Amount", type number' in mquery


def test_unmatched_connector_with_custom_sql_does_not_query_folder_files():
    mquery = mapper.build_table_mquery(
        "Mystery", "source", None, {"driver": "totally_unknown"}, custom_sql="SELECT * FROM x"
    )
    assert "Value.NativeQuery" not in mquery
    assert mquery == "let\n    Source = Mystery\nin\n    Source"
