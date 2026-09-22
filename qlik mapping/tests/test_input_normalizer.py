"""Input normalization — the fix for measures being silently dropped.

Each test names the defect it guards, so a regression fails loudly instead of
quietly emitting an empty section with `failed: 0`.
"""

from services.input_normalizer import (
    normalize_dimension,
    normalize_dimensions,
    normalize_measure,
    normalize_measures,
    resolve_app_metadata,
)

# Exactly what the parsing agent emits for a Qlik master measure.
QLIK_MEASURE = {
    "qInfo": {"qId": "AjfgE", "qType": "measure"},
    "qMeasure": {
        "qLabel": "Total Revenue",
        "qDef": "Sum(revenue)/1000000",
        "qNumFormat": {"qFmt": "$#,##0.0M"},
    },
    "qMetaDef": {"title": "Total Revenue", "description": "Revenue in millions"},
    "tables": ["Loads"],
}

QLIK_DIMENSION = {
    "qInfo": {"qId": "abc", "qType": "dimension"},
    "qDim": {
        "title": "Driver",
        "qFieldDefs": ["=first_name & ' ' & last_name"],
        "qGrouping": "N",
    },
    "qMetaDef": {"title": "Driver"},
    "tables": ["Drivers"],
    "dataType": "STRING",
    "is_calculated": True,
}


def test_raw_qlik_measure_is_no_longer_dropped():
    """The old guard required a top-level `name`; Qlik keeps it in qMeasure."""
    assert "name" not in QLIK_MEASURE  # the condition that used to fail
    result = normalize_measure(QLIK_MEASURE)
    assert result["name"] == "Total Revenue"
    assert result["expression"] == "Sum(revenue)/1000000"
    assert result["qlik_number_format"] == {"qFmt": "$#,##0.0M"}
    assert result["tables"] == ["Loads"]


def test_already_flat_measures_still_work():
    result = normalize_measure({"name": "Revenue", "expression": "Sum(x)"})
    assert result["name"] == "Revenue"
    assert result["expression"] == "Sum(x)"


def test_measures_accept_a_list_or_a_wrapper():
    assert len(normalize_measures([QLIK_MEASURE, QLIK_MEASURE])) == 2
    assert len(normalize_measures({"measures": [QLIK_MEASURE]})) == 1
    assert normalize_measures(None) == []


def test_nameless_and_empty_measures_are_skipped():
    assert normalize_measure({}) == {}
    assert normalize_measure({"qInfo": {}}) == {}
    assert normalize_measures([{}, None, "junk", QLIK_MEASURE]) != []
    assert len(normalize_measures([{}, None, QLIK_MEASURE])) == 1


def test_measure_falls_back_to_the_qlik_id():
    result = normalize_measure({"qInfo": {"qId": "X1"}, "qMeasure": {"qDef": "Sum(a)"}})
    assert result["name"] == "X1"
    assert result["expression"] == "Sum(a)"


def test_calculated_dimension_is_flattened_and_tagged():
    result = normalize_dimension(QLIK_DIMENSION)
    assert result["name"] == "Driver"
    # The leading "=" is stripped, matching the target contract.
    assert result["qlik_expression"] == "first_name & ' ' & last_name"
    assert result["is_calculated"] is True
    assert result["qlik_datatype"] == "STRING (CALCULATED)"


def test_drilldown_detected_from_grouping():
    result = normalize_dimension({
        "qDim": {"title": "Geo", "qFieldDefs": ["Country", "City"], "qGrouping": "H"},
        "tables": ["Geo"],
    })
    assert result["is_drilldown"] is True
    assert result["field_defs"] == ["Country", "City"]


def test_plain_dimension_is_not_marked_calculated():
    result = normalize_dimension({
        "qDim": {"title": "Status", "qFieldDefs": ["trip_status"]}, "tables": ["Trips"],
    })
    assert result["is_calculated"] is False
    assert result["qlik_datatype"] == "STRING"


def test_dimensions_accept_list_or_wrapper():
    assert len(normalize_dimensions([QLIK_DIMENSION])) == 1
    assert len(normalize_dimensions({"dimensions": [QLIK_DIMENSION]})) == 1
    assert normalize_dimensions(None) == []


def test_app_metadata_prefers_the_richest_source():
    """The parsing contract trims `metadata` to five keys."""
    thin = {"qlik_version": None, "owner_name": "x", "status": None,
            "last_modified": "t", "report_version": "v"}
    rich = {**thin, "space_id": "s", "tenant": "t", "owner_email": "e",
            "has_section_access": True, "published": False}

    assert resolve_app_metadata({"metadata": thin, "enrichment": {"app_metadata": rich}}) == rich
    assert resolve_app_metadata({"metadata": thin}) == thin
    assert resolve_app_metadata({}) == {}
