"""FilterMapper: filters previously passed through raw, with no field resolution."""

from services.filter_mapper import FilterMapper

TABLES = [
    {"name": "Drivers", "columns": [
        {"qlik_column_name": "first_name"}, {"qlik_column_name": "last_name"}]},
]


def test_filter_resolves_field_to_table_column():
    mapper = FilterMapper()
    result = mapper.map_filters(
        [{"title": "Driver Filter", "field": "first_name", "sheet_name": "Overview"}], TABLES
    )
    assert len(result) == 1
    f = result[0]
    assert f["fabric"]["target_table"] == "Drivers"
    assert f["fabric"]["target_column"] == "first_name"
    assert f["fabric"]["filter_scope"] == "page"
    assert f["confidence"]["requires_review"] is False


def test_filter_with_no_sheet_is_report_scoped():
    mapper = FilterMapper()
    result = mapper.map_filters([{"title": "App Filter", "field": "last_name"}], TABLES)
    assert result[0]["fabric"]["filter_scope"] == "report"


def test_unresolvable_filter_is_flagged_not_dropped():
    mapper = FilterMapper()
    result = mapper.map_filters([{"title": "Ghost Filter", "field": "does_not_exist"}], TABLES)
    assert len(result) == 1
    f = result[0]
    assert f["fabric"]["target_table"] is None
    assert f["confidence"]["requires_review"] is True
    assert f["confidence"]["score"] < 0.5
    # Unresolved is never a silent dead end - a concrete next step is given.
    assert f["fabric"]["replacement_strategy"]
    assert "does_not_exist" in f["fabric"]["replacement_strategy"]


def test_resolved_filter_has_no_replacement_strategy():
    mapper = FilterMapper()
    result = mapper.map_filters([{"title": "Driver Filter", "field": "first_name"}], TABLES)
    assert result[0]["fabric"]["replacement_strategy"] is None


def test_engine_shaped_filter_is_normalized():
    """Accepts the Qlik qListObjectDef envelope, not just the flat shape."""
    mapper = FilterMapper()
    raw = [{"qListObjectDef": {"qDef": {"qFieldDefs": ["first_name"]}}, "qMetaDef": {"title": "Name"}}]
    result = mapper.map_filters(raw, TABLES)
    assert result[0]["fabric"]["target_column"] == "first_name"
