"""Tests for the LLM-assisted conversion layer.

Covers the pieces that decide whether a model's answer is trusted: variable
expansion, the DAX guard, M-query validation, evidence-based confidence, and
the visual-type table audited against Qlik's published object registry.

Nothing here calls Groq.
"""

import pytest

from services import dax_guard
from services.confidence_evaluator import ConfidenceEvaluator
from services.dax_converter import DAXConverter
from services.variable_expander import build_variable_index, expand, expand_in_place
from services.visual_mapper import VisualMapper
from src.converters.mquery.converter import validate_mquery
from src.converters.variables.converter import VariableConverter


TABLES = [
    {
        "name": "Sales",
        "columns": [
            {"qlik_column_name": "Amount", "fabric_column_name": "Amount", "fabric_datatype": "double"},
            {"qlik_column_name": "Cost", "fabric_column_name": "Cost", "fabric_datatype": "double"},
        ],
    }
]


# ── Qlik dollar-sign expansion ────────────────────────────────────────────

@pytest.fixture
def variable_index():
    return build_variable_index([
        {"name": "vSales", "definition": "=Sum(Amount)"},
        {"name": "vTarget", "definition": "1000"},
        {"name": "vYear", "definition": "2024"},
        {"name": "vNested", "definition": "$(vSales) / $(vTarget)"},
        {"name": "vLoopA", "definition": "$(vLoopB)"},
        {"name": "vLoopB", "definition": "$(vLoopA)"},
        {"name": "vParam", "definition": "Sum($1) * $2"},
    ])


@pytest.mark.parametrize("expression,expected", [
    ("$(vSales)", "Sum(Amount)"),
    ("Sum(Amount) / $(vTarget)", "Sum(Amount) / 1000"),
    # Inside set analysis the old behaviour produced a string literal that
    # silently matched no rows.
    ("Sum({<Year={$(vYear)}>} Amount)", "Sum({<Year={2024}>} Amount)"),
    ("$(vNested)", "Sum(Amount) / 1000"),
    ("$(vParam(Amount, 2))", "Sum(Amount) * 2"),
    ("Sum(Amount)", "Sum(Amount)"),
])
def test_expansion(expression, expected, variable_index):
    result, unresolved = expand(expression, variable_index)
    assert result == expected
    assert not unresolved


def test_expansion_reports_missing_variable(variable_index):
    result, unresolved = expand("$(vMissing)", variable_index)
    assert unresolved == ["vMissing"]
    assert "UNRESOLVED_VARIABLE" in result
    assert "$(" not in result


def test_expansion_breaks_cycles(variable_index):
    result, unresolved = expand("$(vLoopA)", variable_index)
    assert unresolved == ["vLoopA"]
    assert "CIRCULAR_VARIABLE" in result


def test_expand_in_place_preserves_the_original(variable_index):
    items = [{"qlik_expression": "Sum(Amount) / $(vTarget)"}]
    changed = expand_in_place(items, variable_index)
    assert changed == 1
    assert items[0]["qlik_expression"] == "Sum(Amount) / 1000"
    # The mapping output must still show what the Qlik app contained.
    assert items[0]["qlik_expression_raw"] == "Sum(Amount) / $(vTarget)"


# ── DAX guard ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("expression,valid", [
    ("SUM('Sales'[Amount])", True),
    ("[Total Cost] - [Total Amount]", True),
    ("VAR _t = SUM('Sales'[Amount]) RETURN DIVIDE(_t, [Total Cost], 0)", True),
    ("SUM('Sales'[Amount]", False),                       # unbalanced
    ("MATCH('Sales'[Amount], 1)", False),                 # not a DAX function
    ("SUM('FACT_TABLE'[Amount])", False),                 # placeholder table
    ("SUM('Nope'[Amount])", False),                       # unknown table
    ("VAR _x = SUM('Sales'[Amount])", False),             # VAR without RETURN
    ("VAR VALUE = 1 RETURN VALUE", False),                # reserved VAR name
    ("SUM(Amount)", False),                               # unqualified column
    ("[Total 'Sales'[Cost]] - 1", False),                 # nested brackets
    ("DIVIDE(SUM('Sales'[Amount]), $(vTarget), 0)", False),  # unexpanded $()
])
def test_dax_guard(expression, valid):
    ok, problems = dax_guard.validate(expression, TABLES, is_measure=True)
    assert ok is valid, problems


def test_guard_keeps_baseline_when_candidate_is_invalid():
    baseline = "SUM('Sales'[Amount])"
    chosen, used_llm, problems = dax_guard.choose(baseline, "MATCH(1)", TABLES)
    assert chosen == baseline
    assert used_llm is False
    assert problems


def test_guard_accepts_a_valid_improvement():
    chosen, used_llm, _ = dax_guard.choose(
        "SUM('Sales'[Amount])", "AVERAGE('Sales'[Amount])", TABLES
    )
    assert chosen == "AVERAGE('Sales'[Amount])"
    assert used_llm is True


@pytest.mark.parametrize("raw,expected", [
    ("```dax\nSUM('Sales'[Amount])\n```", "SUM('Sales'[Amount])"),
    ("= SUM('Sales'[Amount])", "SUM('Sales'[Amount])"),
])
def test_guard_strips_model_formatting(raw, expected):
    assert dax_guard.strip_model_formatting(raw) == expected


# ── measure aliases ───────────────────────────────────────────────────────

def test_measure_alias_is_not_qualified_as_a_column():
    """Qlik treats a measure label inside an expression as an alias for that
    measure. Qualifying inside the brackets produced `[Total 'Sales'[Cost]]`,
    which balances but is not valid DAX."""
    converted = DAXConverter().qlik_to_dax("[Total Revenue] - [Total Cost]", TABLES)
    assert converted == "[Total Revenue] - [Total Cost]"
    assert "'Sales'[Cost]]" not in converted


def test_columns_are_still_qualified_alongside_measure_refs():
    converted = DAXConverter().qlik_to_dax("Sum(Amount) - [Total Cost]", TABLES)
    assert converted == "SUM('Sales'[Amount]) - [Total Cost]"


# ── visual types, audited against Qlik's object registry ──────────────────

@pytest.mark.parametrize("qlik_type,expected", [
    # classic
    ("barchart", "barChart"), ("bulletchart", "gauge"),
    ("pivot-table", "pivotTable"), ("text-image", "textbox"),
    # nebula "sn-" forms of the same charts
    ("sn-bar-chart", "barChart"), ("sn-bullet-chart", "gauge"),
    ("sn-distplot", "scatterChart"), ("sn-navigation-menu", "pageNavigator"),
    ("sn-kpi", "card"), ("sn-waterfall", "waterfallChart"),
    # bundle extensions
    ("qlik-date-picker", "dateSlicer"), ("qlik-multi-kpi", "multiRowCard"),
    ("qlik-smart-pivot", "pivotTable"), ("qlik-radar-chart", "lineChart"),
    ("sn-word-cloud", "treemap"), ("sn-nlg-chart", "textbox"),
    ("sn-funnel-chart", "funnel"), ("qlik-variable-input", "slicer"),
])
def test_official_qlik_types_resolve(qlik_type, expected):
    assert VisualMapper().get_mapping(qlik_type)["fabric_type"] == expected


def test_ext_suffix_strip_does_not_eat_real_names():
    """A bare endswith('ext') truncated 'sn-text' to 't', so Qlik's Text
    bundle object silently became a generic Table."""
    mapper = VisualMapper()
    assert mapper.normalize_qlik_type("sn-text") == "textimage"
    assert mapper.get_mapping("sn-text")["fabric_type"] == "textbox"
    # A genuinely suffixed extension still normalises.
    assert mapper.normalize_qlik_type("qlik-funnel-chart-ext") == "funnelchart"


# ── variable classification ───────────────────────────────────────────────

@pytest.mark.parametrize("variable,expected_kind", [
    ({"name": "ThousandSep", "definition": ",", "is_reserved": True}, "reserved"),
    ({"name": "DateFormat", "definition": "M/D/YYYY"}, "reserved"),
    ({"name": "vSales", "definition": "=Sum(Amount)"}, "expression"),
    ({"name": "vRatio", "definition": "Sum(A)/Sum(B)"}, "expression"),
    ({"name": "vTaxRate", "definition": "0.2"}, "literal"),
    ({"name": "vTopN", "definition": "10"}, "parameter"),
])
def test_variable_classification(variable, expected_kind):
    kind, _reason = VariableConverter().classify(variable, interactive={})
    assert kind == expected_kind


def test_variable_bound_to_an_input_object_is_a_parameter():
    kind, _ = VariableConverter().classify(
        {"name": "vAnything", "definition": "5"}, interactive={"vAnything": [1, 2, 3]}
    )
    assert kind == "parameter"


def test_reserved_variables_produce_no_parameter():
    fabric = VariableConverter()._deterministic_fabric(
        {"name": "ThousandSep", "definition": ",", "is_reserved": True}, "reserved", {}
    )
    assert fabric["emit_as_parameter"] is False
    assert fabric["format_target"] == "decimal_separator_group"


def test_date_serial_is_resolved():
    fabric = VariableConverter()._deterministic_fabric(
        {"name": "vMinDate", "definition": "44562"}, "literal", {}
    )
    assert fabric["iso_date"] == "2022-01-01"
    assert fabric["data_type"] == "dateTime"


# ── M-query validation ────────────────────────────────────────────────────

@pytest.mark.parametrize("expression,valid", [
    ('let Source = Sql.Database("s", "d") in Source', True),
    ('Source = Sql.Database("s","d")', False),                     # no let/in
    ('let Source = Sql.Database("s","d" in Source', False),        # unbalanced
    ('let Source = TableName in Source', False),                   # placeholder
    ('let Source = Sql.Database("s","d") RESIDENT Other in Source', False),
    ('let Source = Csv.Document(File.Contents("C:\\\\data.csv")) in Source', False),
    ('let Source = TEXT.Upper(Sql.Database("s","d")) in Source', False),
])
def test_mquery_validation(expression, valid):
    ok, problems = validate_mquery(expression, "T", [])
    assert ok is valid, problems


# ── evidence-based visual confidence ──────────────────────────────────────

def _score(**kwargs):
    defaults = dict(
        qlik_type="barchart", fabric_type="barChart", supported=True,
        field_roles=[{"role": "Category", "resolved": True},
                     {"role": "Y", "resolved": True}],
        unbound_fields=[], source_field_count=2,
        layout={"width": 400, "height": 300},
    )
    defaults.update(kwargs)
    return ConfidenceEvaluator().evaluate_visual_conversion(**defaults)


def test_clean_conversion_scores_high():
    result = _score()
    assert result["score"] == 1.0
    assert result["band"] == "high"
    assert result["requires_review"] is False


def test_unbound_fields_lower_the_score_and_flag_review():
    result = _score(unbound_fields=["ghost"], source_field_count=3,
                    field_roles=[{"role": "Category", "resolved": True},
                                 {"role": "Y", "resolved": True},
                                 {"role": "Unbound", "resolved": False}])
    assert result["score"] < 1.0
    assert result["requires_review"] is True
    assert any(c["id"] == "fields_bound_to_model" and c["status"] == "fail"
               for c in result["checks"])


def test_missing_required_role_is_penalised_hardest():
    """A barChart with nothing on Y renders as a blank rectangle."""
    empty_y = _score(field_roles=[{"role": "Category", "resolved": True}])
    assert empty_y["score"] < _score()["score"]
    assert any(c["id"] == "required_roles_populated" and c["status"] == "fail"
               for c in empty_y["checks"])


def test_fallback_to_generic_table_scores_low():
    result = _score(qlik_type="sn-unknown-thing", fabric_type="tableEx",
                    field_roles=[{"role": "Values", "resolved": True}],
                    source_field_count=1)
    assert result["band"] in ("low", "medium")
    assert result["requires_review"] is True


def test_dataless_visuals_are_not_penalised_for_having_no_fields():
    """A textbox or button legitimately binds nothing."""
    result = _score(qlik_type="text-image", fabric_type="textbox",
                    field_roles=[], source_field_count=0)
    assert result["requires_review"] is False
    assert any(c["id"] == "fields_bound_to_model" and c["status"] == "skip"
               for c in result["checks"])


def test_custom_visual_requirement_is_penalised():
    result = _score(qlik_type="boxplot", fabric_type="boxPlot",
                    requires_custom_visual=True)
    assert result["score"] < 1.0
    assert any(c["id"] == "visual_renders_natively" and c["status"] == "fail"
               for c in result["checks"])


def test_model_confidence_can_lower_but_never_raise_the_score():
    """The model's self-reported confidence is treated as doubt, not proof.

    A model claiming high confidence on a visual that failed a structural
    check must not be able to lift the score; a model expressing doubt about
    a structurally clean one legitimately lowers it.
    """
    evidence_only = _score()["score"]

    # A confident claim never pushes the score above what the checks support.
    assert _score(used_llm=True, llm_score=0.99)["score"] <= evidence_only

    # Doubt is respected.
    assert _score(used_llm=True, llm_score=0.40)["score"] == 0.40

    # And it cannot rescue a visual that failed a structural check: an empty
    # required role stays penalised however confident the model claims to be.
    broken = _score(field_roles=[{"role": "Category", "resolved": True}],
                    used_llm=True, llm_score=1.0)
    assert broken["score"] < evidence_only
    assert broken["requires_review"] is True
