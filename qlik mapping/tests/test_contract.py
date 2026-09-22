"""Contract 2.0 shape, and the conversion defects found against it."""

import asyncio
import json
from pathlib import Path

import pytest

from agents import CoordinatorAgent
from services.dimension_mapper import DimensionMapper

# The 43 keys of Contract 2.0, in the order the reference response uses.
CONTRACT_KEYS = [
    "status", "message", "error_message", "contract_version", "summary", "workbook_metadata",
    "app_layout", "app_metadata", "datasources", "connections", "tables", "relationships",
    "measures", "dimensions", "calculated_columns", "custom_sql", "visuals",
    "filters", "limitations", "variables", "section_access", "stories",
    "bookmarks", "themes", "extensions", "master_item_tags", "hypercube_samples",
    "script", "data_load_editor", "fields", "rls", "data_model", "lineage",
    "limitations_summary", "object_inventory", "section_status", "extraction",
    "master_objects", "media", "snapshots", "data_files", "conversion_summary",
    "llm_status", "migration_status", "production_gate",
]



QLIK_MEASURE = {
    "qInfo": {"qId": "m1"},
    "qMeasure": {"qLabel": "Total Revenue", "qDef": "Sum(revenue)/1000000",
                 "qNumFormat": {"qFmt": "$#,##0.0M"}},
    "qMetaDef": {"title": "Total Revenue"},
    "tables": ["Loads"],
}

PAYLOAD = {
    "app_id": "app-1", "app_name": "FleetVision KSA", "run_id": "run-1",
    "metadata": {"tenant": "t", "report_version": "12.2881.0"},
    "tables": [
        {"table_name": "Loads", "fields": [{"name": "revenue", "dataType": "NUMERIC"}]},
        {"table_name": "Trips", "fields": [{"name": "driver_id", "dataType": "STRING"}]},
        {"table_name": "Drivers", "fields": [
            {"name": "driver_id", "dataType": "STRING"},
            {"name": "first_name", "dataType": "STRING"},
            {"name": "last_name", "dataType": "STRING"},
        ]},
    ],
    "measures": [QLIK_MEASURE],
    "dimensions": [{
        "qDim": {"title": "Driver", "qFieldDefs": ["=first_name & ' ' & last_name"]},
        "tables": ["Drivers"], "dataType": "STRING", "is_calculated": True,
    }],
    # The Qlik engine's own relationship shape.
    "relationships": [{
        "table1": "Trips", "field1": "driver_id",
        "table2": "Drivers", "field2": "driver_id",
        "cardinality": "many-to-one",
    }],
    "datasources": [], "visualizations": [], "sheets": [],
}


@pytest.fixture(scope="module")
def result():
    return asyncio.run(CoordinatorAgent().process_data(PAYLOAD, False, "app-1"))


def test_every_contract_key_is_emitted(result):
    assert list(result) == CONTRACT_KEYS
    assert result["contract_version"] == "2.0"
    assert result["status"] == "success"


def test_measures_are_not_silently_dropped(result):
    """29 measures once converted to 0 because of a top-level `name` guard."""
    assert len(result["measures"]) == len(PAYLOAD["measures"])
    measure = result["measures"][0]
    assert measure["name"] == "Total Revenue"
    assert measure["qlik_expression"] == "Sum(revenue)/1000000"
    assert "SUM(" in measure["fabric"]["dax_expression"]
    assert "lineage_tag" in measure["fabric"]


def test_relationships_resolve_engine_shape(result):
    """table1/field1/table2/field2 previously resolved to None, emitting ''.key."""
    assert len(result["relationships"]) == 1
    rel = result["relationships"][0]
    assert rel["source_table"] == "Trips" and rel["target_table"] == "Drivers"
    assert rel["source_column"] == "driver_id"
    assert rel["fabric"]["cardinality"] == "manyToOne"
    assert rel["fabric"]["from_table"] == "Trips"
    assert rel["fabric"]["to_table"] == "Drivers"


def test_relationships_with_unresolvable_ends_are_skipped():
    payload = {**PAYLOAD, "relationships": [{"key_field": "orphan"}]}
    out = asyncio.run(CoordinatorAgent().process_data(payload, False, "app-1"))
    assert out["relationships"] == []
    # And the summary must own up to the loss.
    assert out["conversion_summary"]["by_section"]["relationships"]["failed"] == 1


def test_dimensions_are_converted_not_passed_through(result):
    assert len(result["dimensions"]) == 1
    dim = result["dimensions"][0]
    assert dim["name"] == "Driver"
    assert dim["fabric"]["is_calculated"] is True
    # Each identifier qualified, Qlik literals converted to DAX quotes.
    assert dim["fabric"]["dax_expression"] == (
        "'Drivers'[first_name] & \" \" & 'Drivers'[last_name]"
    )
    assert dim["confidence"]["requires_review"] is True


def test_conversion_summary_reports_real_failures():
    """`failed: 0` used to be hardcoded, hiding the dropped measures."""
    payload = {**PAYLOAD, "measures": [QLIK_MEASURE, {"junk": True}]}
    out = asyncio.run(CoordinatorAgent().process_data(payload, False, "app-1"))
    section = out["conversion_summary"]["by_section"]["measures"]
    assert section["converted"] == 1
    assert section["total"] == 1  # the junk entry normalizes away


def test_llm_status_names_the_model_actually_used(result):
    """It previously reported azure-openai-gpt4o even when Groq was in use."""
    model = result["llm_status"]["model"]
    assert model.startswith(("groq:", "azure:", "none:"))


def test_app_metadata_prefers_the_richer_source():
    rich = {"tenant": "t", "space_id": "s", "owner_email": "e", "published": True}
    payload = {**PAYLOAD, "enrichment": {"app_metadata": rich}}
    out = asyncio.run(CoordinatorAgent().process_data(payload, False, "app-1"))
    assert out["app_metadata"] == rich


def test_real_theme_and_media_are_forwarded_not_dropped():
    """app_layout/media used to be hardcoded to {} regardless of what parsing
    found, so a generated report could never carry the source app's real
    theme colors or images even when unified-parsing successfully extracted
    them - this asserts the enrichment data actually reaches the output.
    """
    theme = {
        "theme_name": "FleetVision KSA", "color_palette": ["#0B7285", "#F76707"],
        "primary_color": "#0B7285", "source": "qlik_theme",
    }
    media = {"media_files": [{"name": "logo.png", "url": "media/content/logo.png"}]}
    payload = {**PAYLOAD, "enrichment": {"theme_and_styling": theme, "media": media}}
    out = asyncio.run(CoordinatorAgent().process_data(payload, False, "app-1"))
    assert out["app_layout"]["theme"] == theme
    assert out["media"] == media


def test_missing_theme_still_yields_empty_app_layout_not_a_crash():
    out = asyncio.run(CoordinatorAgent().process_data(PAYLOAD, False, "app-1"))
    assert out["app_layout"] == {}
    assert out["media"] == {}


# --- column type inference -------------------------------------------------

def test_derived_date_part_columns_do_not_inherit_the_parent_dates_type():
    """Qlik reports Year(Date) as Date_year / QuarterName(Date) as
    Date_quarterLabel with the source Date field's own dataType ("DATE"),
    even though the real values are an int year or a text label. Trusting
    that declared type verbatim casts these to `type datetime` in Power
    Query, which corrupts the column on refresh and produces PBI Desktop's
    "Something's wrong with one or more fields" on any visual bound to it."""
    payload = {
        **PAYLOAD,
        "tables": [{
            "table_name": "Sales",
            "fields": [
                {"name": "Date", "dataType": "DATE"},
                {"name": "Date_year", "dataType": "DATE", "format": "###0"},
                {"name": "Date_quarterLabel", "dataType": "DATE", "format": "YYYY-MM-DD"},
                {"name": "TotalAmount", "dataType": "NUMBER"},
            ],
        }],
        "relationships": [],
    }
    out = asyncio.run(CoordinatorAgent().process_data(payload, False, "app-1"))
    cols = {c["qlik_column_name"]: c for c in out["tables"][0]["columns"]}

    assert cols["Date"]["fabric_datatype"] == "dateTime"
    assert cols["Date_year"]["fabric_datatype"] == "int64"
    assert cols["Date_year"]["summarize_by"] == "none"
    assert cols["Date_quarterLabel"]["fabric_datatype"] == "string"
    assert cols["Date_quarterLabel"]["format_string"] != "YYYY-MM-DD"


# --- dimension DAX translation -------------------------------------------

def test_dax_qualifies_identifiers_against_real_columns():
    mapper = DimensionMapper()
    mapper.index_columns([
        {"name": "Drivers", "columns": [
            {"qlik_column_name": "first_name"}, {"qlik_column_name": "last_name"}]}
    ])
    assert mapper.to_dax("first_name & ' ' & last_name", "Drivers") == (
        "'Drivers'[first_name] & \" \" & 'Drivers'[last_name]"
    )


def test_dax_leaves_function_names_alone():
    mapper = DimensionMapper()
    mapper.index_columns([{"name": "Loads", "columns": [{"qlik_column_name": "load_date"}]}])
    assert mapper.to_dax("Month(load_date)", "Loads") == "Month('Loads'[load_date])"


def test_dax_does_not_qualify_text_inside_literals():
    mapper = DimensionMapper()
    mapper.index_columns([{"name": "T", "columns": [{"qlik_column_name": "city"}]}])
    # "city" inside the literal must stay literal text.
    assert mapper.to_dax("'city' & city", "T") == "\"city\" & 'T'[city]"


def test_summary_extracted_from_parsing_metadata():
    payload = dict(PAYLOAD)
    payload["_meta"] = {
        "summary": {
            "tables": 5,
            "dimensions": 3,
            "measures": 10,
            "relationships": 4,
            "sheets": 14,
            "visualizations": 68,
            "empty_keys": 0,
            "populated_keys": 8239,
            "view": "compact"
        }
    }
    out = asyncio.run(CoordinatorAgent().process_data(payload, False, "app-1"))
    assert "summary" in out
    assert out["summary"]["tables"] == 5
    assert out["summary"]["dimensions"] == 3
    assert out["summary"]["measures"] == 10
    assert out["summary"]["relationships"] == 4
    assert out["summary"]["sheets"] == 14
    assert out["summary"]["visualizations"] == 68
    assert out["summary"]["empty_keys"] == 0
    assert out["summary"]["populated_keys"] == 8239
    assert out["summary"]["view"] == "compact"


def test_datasources_structured_format():
    payload = dict(PAYLOAD)
    payload["datasources"] = [
        {
            "datasource_id": "ds_1",
            "name": "FleetVisionRedshift",
            "connector_type": "redshift",
            "server": "fleetvision-wg.881226714470.ap-southeast-2.redshift-serverless.amazonaws.com",
            "database": "dev",
            "schema": "PUBLIC",
            "username": "VECTORLAB",
            "warehouse": "COMPUTE_WH"
        }
    ]
    out = asyncio.run(CoordinatorAgent().process_data(payload, False, "app-1"))
    assert "datasources" in out
    assert len(out["datasources"]) == 1
    ds = out["datasources"][0]
    assert ds["id"] == "conn.redshift.fleetvisionredshift"
    assert ds["name"] == "FleetVisionRedshift"
    assert ds["inline"] is True
    assert ds["mode"] == "extract"
    assert ds["connection_type"] == "redshift"
    assert len(ds["connections"]) == 1
    conn = ds["connections"][0]
    assert conn["server"] == "fleetvision-wg.881226714470.ap-southeast-2.redshift-serverless.amazonaws.com"
    assert conn["database"] == "dev"
    assert conn["schema"] == "PUBLIC"
    assert conn["username"] == "VECTORLAB"
    assert len(conn["tables"]) == 3
    assert conn["tables"][0] == "dev.PUBLIC.Loads"
    assert len(ds["embedded_credentials"]) == 1
    cred = ds["embedded_credentials"][0]
    assert cred["username"] == "VECTORLAB"
    assert cred["authentication"] == "Username Password"


