import pytest
from services.dax_converter import DAXConverter
from agents.mapping_agent import MappingAgent
from agents.coordinator_agent import CoordinatorAgent
from src.converters.measures.converter import MeasureConverter

# Known tables fixture with COURSES and GRADES tables
KNOWN_TABLES = [
    {
        "name": "COURSES",
        "columns": [
            {"name": "CREDITS", "datatype": "numeric", "fabric_column_name": "CREDITS"},
            {"name": "COURSE_ID", "datatype": "text", "fabric_column_name": "COURSE_ID"},
            {"name": "COURSE_NAME", "datatype": "text", "fabric_column_name": "COURSE_NAME"},
        ],
    },
    {
        "name": "GRADES",
        "columns": [
            {"name": "GRADE", "datatype": "text", "fabric_column_name": "GRADE"},
            {"name": "STUDENT_ID", "datatype": "text", "fabric_column_name": "STUDENT_ID"},
        ],
    },
]

class MockFailingLLMClient:
    """Simulates LLM unavailable, rate-limited, or throwing exceptions."""
    async def generate_text(self, *args, **kwargs) -> str:
        raise RuntimeError("Rate limited (Groq 429: Too Many Requests)")

    async def generate_structured_response(self, *args, **kwargs) -> dict:
        raise RuntimeError("Rate limited (Groq 429: Too Many Requests)")

class MockEmptyLLMClient:
    """Simulates LLM returning empty or no usable response."""
    async def generate_text(self, *args, **kwargs) -> str:
        return ""

    async def generate_structured_response(self, *args, **kwargs) -> dict:
        return {}

class MockValidLLMClient:
    """Simulates LLM successfully returning valid DAX for an unsupported expression."""
    def __init__(self, dax: str):
        self.dax = dax

    async def generate_text(self, *args, **kwargs) -> str:
        return self.dax

    async def generate_structured_response(self, *args, **kwargs) -> dict:
        return {"dax_expression": self.dax, "explanation": "Converted via LLM"}


def test_1_avg_credits_deterministic_conversion():
    """TEST 1:
    Input: Avg(CREDITS)
    Expected:
        dax: AVERAGE('COURSES'[CREDITS])
        conversion_method: deterministic_rule
        conversion_status: converted
        unresolved_columns: []
        validation.passed: true
    """
    converter = DAXConverter()
    measure_input = {
        "name": "Average Credits",
        "expression": "Avg(CREDITS)",
        "tables": ["COURSES"],
    }
    result = converter.convert_measure(measure_input, KNOWN_TABLES)

    assert result["dax_expression"] == "AVERAGE('COURSES'[CREDITS])"
    assert result["conversion_method"] == "deterministic_rule"
    assert result["conversion_status"] == "converted"
    assert result["status"] == "converted"
    assert result["unresolved_columns"] == []
    assert result["unconverted_qlik_functions"] == []
    assert result["unconverted_qlik_syntax"] == []
    assert result["validation"]["passed"] is True
    assert result["confidence_score"] >= 80
    assert result["confidence"]["score"] >= 0.8
    assert result["fabric"]["table"] == "COURSES"
    assert result["fabric"]["dax_expression"] == "AVERAGE('COURSES'[CREDITS])"


def test_2_sum_credits_deterministic_conversion():
    """TEST 2:
    Input: Sum(CREDITS)
    Expected:
        dax: SUM('COURSES'[CREDITS])
        conversion_status: converted
    """
    converter = DAXConverter()
    measure_input = {
        "name": "Total Credits",
        "expression": "Sum(CREDITS)",
        "tables": ["COURSES"],
    }
    result = converter.convert_measure(measure_input, KNOWN_TABLES)

    assert result["dax_expression"] == "SUM('COURSES'[CREDITS])"
    assert result["conversion_status"] == "converted"
    assert result["conversion_method"] == "deterministic_rule"
    assert result["unresolved_columns"] == []
    assert result["validation"]["passed"] is True
    assert result["fabric"]["table"] == "COURSES"


def test_3_count_grade_deterministic_conversion():
    """TEST 3:
    Input: Count(GRADE)
    Expected:
        dax: COUNT('GRADES'[GRADE])
        conversion_status: converted
    """
    converter = DAXConverter()
    measure_input = {
        "name": "Grade Count",
        "expression": "Count(GRADE)",
        "tables": ["GRADES"],
    }
    result = converter.convert_measure(measure_input, KNOWN_TABLES)

    assert result["dax_expression"] == "COUNT('GRADES'[GRADE])"
    assert result["conversion_status"] == "converted"
    assert result["conversion_method"] == "deterministic_rule"
    assert result["unresolved_columns"] == []
    assert result["validation"]["passed"] is True
    assert result["fabric"]["table"] == "GRADES"


def test_additional_aggregations_min_max():
    """Verify Min(CREDITS) and Max(CREDITS) also convert deterministically."""
    converter = DAXConverter()
    
    # Min
    min_res = converter.convert_measure(
        {"name": "Min Credits", "expression": "Min(CREDITS)", "tables": ["COURSES"]},
        KNOWN_TABLES
    )
    assert min_res["dax_expression"] == "MIN('COURSES'[CREDITS])"
    assert min_res["conversion_status"] == "converted"
    assert min_res["validation"]["passed"] is True

    # Max
    max_res = converter.convert_measure(
        {"name": "Max Credits", "expression": "Max(CREDITS)", "tables": ["COURSES"]},
        KNOWN_TABLES
    )
    assert max_res["dax_expression"] == "MAX('COURSES'[CREDITS])"
    assert max_res["conversion_status"] == "converted"
    assert max_res["validation"]["passed"] is True


@pytest.mark.asyncio
async def test_4_llm_rate_limited_preserves_valid_deterministic_conversion():
    """TEST 4:
    Simulate LLM unavailable/rate-limited while deterministic conversion succeeds.
    Expected:
        conversion_status = converted
    NOT:
        failed_to_convert
    """
    converter = DAXConverter()
    measure_input = {
        "name": "Average Credits",
        "expression": "Avg(CREDITS)",
        "tables": ["COURSES"],
    }
    initial = converter.convert_measure(measure_input, KNOWN_TABLES)
    assert initial["conversion_status"] == "converted"

    # Now pass it into MeasureConverter with a failing LLM client (429 Rate Limit)
    mc = MeasureConverter(llm_client=MockFailingLLMClient())
    refined = await mc.refine_one(initial, KNOWN_TABLES, relationships=[])

    assert refined["conversion_status"] == "converted"
    assert refined["status"] == "converted"
    assert refined["conversion_method"] == "deterministic_rule"
    assert refined["dax_expression"] == "AVERAGE('COURSES'[CREDITS])"
    assert refined["confidence_score"] >= 80
    assert "no usable model response" not in (refined.get("review_notes") or "")

    # Also test refine_all with failing LLM
    refined_all = await mc.refine_all([initial], KNOWN_TABLES, relationships=[])
    assert len(refined_all) == 1
    assert refined_all[0]["conversion_status"] == "converted"
    assert refined_all[0]["status"] == "converted"


@pytest.mark.asyncio
async def test_4b_llm_empty_response_preserves_valid_deterministic_conversion():
    """Simulate LLM returning empty response when baseline is valid."""
    converter = DAXConverter()
    measure_input = {
        "name": "Average Credits",
        "expression": "Avg(CREDITS)",
        "tables": ["COURSES"],
    }
    initial = converter.convert_measure(measure_input, KNOWN_TABLES)
    
    mc = MeasureConverter(llm_client=MockEmptyLLMClient())
    refined = await mc.refine_one(initial, KNOWN_TABLES, relationships=[])

    assert refined["conversion_status"] == "converted"
    assert refined["status"] == "converted"
    assert refined["conversion_method"] == "deterministic_rule"
    assert refined["confidence_score"] >= 80


@pytest.mark.asyncio
async def test_5_unsupported_qlik_expression():
    """TEST 5:
    Give an actually unsupported Qlik expression.
    Expected:
        deterministic conversion fails
        LLM fallback is attempted if appropriate
        final status is converted only if validated DAX is produced
        otherwise failed_to_convert
    """
    converter = DAXConverter()
    unsupported_input = {
        "name": "Running Total",
        "expression": "RangeSum(Above(Sum(CREDITS), 0, RowNo()))",
        "tables": ["COURSES"],
    }
    initial = converter.convert_measure(unsupported_input, KNOWN_TABLES)

    # Deterministic conversion cannot fully convert RangeSum/Above/RowNo
    # So it should be marked as failed or having unconverted functions
    assert initial["conversion_status"] == "failed_to_convert" or not initial["validation"]["passed"]

    # Case A: LLM fails / unavailable -> final status MUST be failed_to_convert
    mc_failing = MeasureConverter(llm_client=MockFailingLLMClient())
    res_failing = await mc_failing.refine_one(dict(initial), KNOWN_TABLES, relationships=[])
    assert res_failing["conversion_status"] == "failed_to_convert"
    assert res_failing["status"] == "failed to convert"
    assert res_failing["confidence_score"] == 0

    # Case B: LLM returns valid DAX -> final status becomes converted
    valid_dax = "CALCULATE(SUM('COURSES'[CREDITS]))"
    mc_valid = MeasureConverter(llm_client=MockValidLLMClient(valid_dax))
    res_valid = await mc_valid.refine_one(dict(initial), KNOWN_TABLES, relationships=[])
    assert res_valid["conversion_status"] == "converted"
    assert res_valid["status"] == "converted"
    assert res_valid["conversion_method"] == "llm_refined"
    assert res_valid["dax_expression"] == valid_dax
    assert res_valid["validation"]["passed"] is True


@pytest.mark.asyncio
async def test_full_pipeline_coordinator_agent():
    """Test full CoordinatorAgent process_data pipeline for Avg(CREDITS)
    verifying exactly one measure mapping with consistent Fabric metadata."""
    coordinator = CoordinatorAgent()
    coordinator.measure_converter = MeasureConverter(llm_client=MockFailingLLMClient())

    payload = {
        "app_id": "test-app",
        "name": "Test App",
        "tables": KNOWN_TABLES,
        "measures": [
            {
                "name": "Average Credits",
                "expression": "Avg(CREDITS)",
                "tables": ["COURSES"],
            }
        ],
        "visualizations": [
            {
                "name": "Visual 1",
                "type": "barchart",
                "measures": [
                    {"name": "Avg(CREDITS)", "expression": "Avg(CREDITS)"}
                ],
            }
        ],
    }

    result = await coordinator.process_data(payload)
    measures = result.get("measures", [])
    
    # Verify deduplication: exactly 1 measure for Avg(CREDITS)
    credits_measures = [m for m in measures if "CREDITS" in str(m.get("qlik_expression", ""))]
    assert len(credits_measures) == 1, f"Expected 1 measure, got {len(credits_measures)}: {credits_measures}"

    m = credits_measures[0]
    assert m["dax_expression"] == "AVERAGE('COURSES'[CREDITS])"
    assert m["conversion_status"] == "converted"
    assert m["status"] == "converted"
    assert m["conversion_method"] == "deterministic_rule"
    assert m["unresolved_columns"] == []
    assert m["validation"]["passed"] is True
    assert m["confidence_score"] >= 80
    assert m["fabric"]["table"] == "COURSES"
    assert m["fabric"]["dax_expression"] == "AVERAGE('COURSES'[CREDITS])"
    assert m["fabric"]["format_string"] == "#,##0.00"
