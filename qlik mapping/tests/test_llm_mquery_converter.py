import pytest
from src.converters.mquery.converter import MQueryConverter, validate_mquery

class MockLLMClient:
    def __init__(self, response: str):
        self.response = response

    async def generate_text(self, system: str, user: str, *args, **kwargs) -> str:
        return self.response

    async def generate_structured_response(self, system: str, user: str, schema: dict, *args, **kwargs) -> dict:
        return {"m_query": self.response}

@pytest.mark.asyncio
async def test_llm_mquery_converter_refines_table():
    llm_output = (
        'let\n'
        '    Source = RawExamData,\n'
        '    #"Filtered Rows" = Table.SelectRows(Source, each ([numeric_score] <> null)),\n'
        '    #"Added letter_grade" = Table.AddColumn(#"Filtered Rows", "letter_grade", each if [numeric_score] >= 90 then "A" else "B", type text)\n'
        'in\n'
        '    #"Added letter_grade"'
    )
    converter = MQueryConverter(llm_client=MockLLMClient(llm_output))
    table = {
        "name": "GRADES",
        "load_type": "resident",
        "qlik_query": "LOAD student_id, numeric_score RESIDENT RawExamData WHERE IsNull(numeric_score) = 0;",
        "m_query": "let\n    Source = GRADES\nin\n    Source"
    }
    
    result = await converter.refine_one(table, table["m_query"], ["RawExamData"])
    assert result["m_expression"] == llm_output
    assert isinstance(result["m_query"], list)
    assert len(result["m_query"]) == 3
    assert result["m_query"][0]["step"] == 1
    assert "RawExamData" in result["m_query"][0]["content"]

def test_validate_mquery_passes_valid_expressions():
    expr = (
        'let\n'
        '    Source = Table.FromRows({{"101", "Math", "A"}}, {"ID", "Subject", "Grade"}),\n'
        '    #"Changed Type" = Table.TransformColumnTypes(Source, {{"ID", Int64.Type}})\n'
        'in\n'
        '    #"Changed Type"'
    )
    ok, problems = validate_mquery(expr, "GRADES", [])
    assert ok is True
    assert len(problems) == 0

def test_validate_mquery_catches_dollar_expansion():
    expr = 'let\n    Source = Sql.Database("$(Server)", "DB")\nin\n    Source'
    ok, problems = validate_mquery(expr, "T", [])
    assert ok is False
    assert any("dollar-sign" in p for p in problems)
