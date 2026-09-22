import asyncio
import pytest
from rules import (
    ALL_RULES,
    COLUMNS_RULES,
    DIMENSIONS_RULES,
    MEASURES_ADVANCED_RULES,
    MEASURES_CORE_RULES,
    MQUERY_RULES,
    RULE_TYPES,
    VARIABLES_RULES,
)
from agents import MappingAgent, CoordinatorAgent
from services.visual_mapper import VisualMapper

def test_rules_loading():
    """Every rule set loads and is reachable through the ALL_RULES registry."""
    assert len(ALL_RULES) > 0
    assert len(DIMENSIONS_RULES) == 20
    assert len(MEASURES_CORE_RULES) + len(MEASURES_ADVANCED_RULES) > 30

    # ALL_RULES aggregates the static sets plus the lazily-resolved dashboard
    # rules, so it is strictly larger than the three DAX sets alone.
    static_total = (
        len(DIMENSIONS_RULES)
        + len(MEASURES_CORE_RULES)
        + len(MEASURES_ADVANCED_RULES)
        + len(VARIABLES_RULES)
        + len(COLUMNS_RULES)
        + len(MQUERY_RULES)
    )
    assert len(ALL_RULES) >= static_total

    # Every declared rule type must actually be represented, otherwise a
    # stage's prompt would silently carry no rules at all.
    present = {r.get("type") for r in ALL_RULES}
    for rule_type in RULE_TYPES:
        assert rule_type in present, f"no rules registered for '{rule_type}'"


def test_every_rule_is_well_formed():
    """A malformed rule is silently skipped by the prompt builder, so the
    shape is asserted here rather than discovered as a missing rule."""
    seen_ids = {}
    for rule in ALL_RULES:
        assert isinstance(rule, dict), f"non-dict rule: {rule!r}"
        assert rule.get("type") in RULE_TYPES, f"unknown type on {rule.get('id')!r}"
        assert str(rule.get("id") or "").strip(), f"rule without an id: {rule!r}"
        assert str(rule.get("rule") or "").strip(), f"empty rule body: {rule.get('id')!r}"

        # Ids only need to be unique within their own type.
        key = (rule["type"], rule["id"])
        assert key not in seen_ids, f"duplicate rule id {key}"
        seen_ids[key] = True


def test_critical_rules_survive_the_prompt_budget():
    """Rules marked critical must always reach the prompt.

    format_rules truncates to a character budget; ordering is priority-first
    so a correctness rule can never be evicted by a cosmetic one.
    """
    from services.prompt_builder import format_rules

    for rule_type in RULE_TYPES:
        applicable = [
            r for r in ALL_RULES
            if r.get("type") == rule_type and r.get("scope") != "output_format"
        ]
        critical = [r for r in applicable if r.get("priority") == "critical"]
        non_critical = [r for r in applicable if r.get("priority") != "critical"]
        if not critical:
            continue

        # Deliberately squeeze the budget to force truncation.
        rendered = format_rules(rule_type, budget=1200)
        kept_critical = [r["id"] for r in critical if f"[{r['id']}]" in rendered]
        kept_non_critical = [r["id"] for r in non_critical if f"[{r['id']}]" in rendered]

        assert kept_critical, f"no critical '{rule_type}' rule survived truncation"

        # The guarantee: a non-critical rule may only be included once every
        # critical one already is. Packing is greedy (a short rule can fit
        # where a longer one did not), so this is the real invariant rather
        # than the kept set being a strict prefix.
        if len(kept_critical) < len(critical):
            assert not kept_non_critical, (
                f"'{rule_type}': non-critical rules {kept_non_critical} were kept "
                f"while critical rules were dropped"
            )

    # With the configured budget, nothing should be dropped at all.
    for rule_type in RULE_TYPES:
        applicable = [
            r for r in ALL_RULES
            if r.get("type") == rule_type and r.get("scope") != "output_format"
        ]
        rendered = format_rules(rule_type)
        missing = [r["id"] for r in applicable if f"[{r['id']}]" not in rendered]
        assert not missing, (
            f"'{rule_type}' rules dropped at the configured budget: {missing}. "
            "Raise Config.LLM_RULES_CHAR_BUDGET."
        )


def test_output_format_rules_are_never_injected():
    """A rule describing the response envelope must not reach a prompt whose
    contract is set by prompt_builder, or the model gets two contradictory
    format instructions."""
    from services.prompt_builder import format_rules

    excluded = [r for r in ALL_RULES if r.get("scope") == "output_format"]
    assert excluded, "expected at least one output_format rule"
    for rule in excluded:
        rendered = format_rules(rule["type"])
        assert f"[{rule['id']}]" not in rendered

def test_coordinator_agent():
    """Test CoordinatorAgent process_data output format."""
    coordinator = CoordinatorAgent()
    sample_data = {
        "app_id": "app_test_123",
        "tables": [{"table_name": "Drivers", "fields": [{"name": "driver_id", "dataType": "String"}]}],
        "visualizations": [],
        "measures": []
    }
    result = asyncio.run(coordinator.process_data(sample_data, app_id="app_test_123"))
    assert result["status"] == "success"
    # Contract 2.0 carries identity in workbook_metadata, not at the root.
    assert result["workbook_metadata"]["app_id"] == "app_test_123"
    assert result["contract_version"] == "2.0"
    # `table_details` was the pre-2.0 wrapper; tables are top-level now.
    assert len(result["tables"]) == 1
    assert result["tables"][0]["name"] == "Drivers"

def test_visual_mapper():
    """Test VisualMapper type mapping and normalization."""
    mapper = VisualMapper()
    raw_visuals = [
        {"qlik_type": "scatterplot", "title": "Scatter"},
        {"qlik_type": "qlik-funnel-chart-ext", "title": "Funnel Ext"},
        {"qlik_type": "pivot table", "title": "Matrix"},
        {"qlik_type": "boxplot", "title": "Box"},
        {"qlik_type": "treemap", "title": "Tree"}
    ]
    res = mapper.map_visuals_and_sheets(raw_visuals, [])
    items = res["sheet_visuals"]
    assert items[0]["fabric"]["visual_type"] == "scatterChart"
    assert items[0]["fabric"]["bi_type"] == "Scatter Chart"
    assert items[1]["fabric"]["visual_type"] == "funnel"
    assert items[1]["fabric"]["bi_type"] == "Funnel Chart"
    assert items[2]["fabric"]["visual_type"] == "pivotTable"
    assert items[2]["fabric"]["bi_type"] == "Matrix"
    assert items[3]["fabric"]["visual_type"] == "boxPlot"
    assert items[3]["fabric"]["bi_type"] == "Box and Whisker Chart"
    assert items[4]["fabric"]["visual_type"] == "treemap"
    assert items[4]["fabric"]["bi_type"] == "Treemap"
