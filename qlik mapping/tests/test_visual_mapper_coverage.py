"""listbox/container/button/bookmarkobject had no mapping and silently fell
to the generic "default" -> tableEx bucket, which renders an unrelated full
data-table visual in place of what should be a slicer/group/button. Also
regression-tests that 'textimage' now reaches its own dict entry instead of
being folded into 'string-image' and permanently mislabeled "Input Box".
"""

from services.visual_mapper import VisualMapper

vm = VisualMapper()


def test_listbox_maps_to_slicer():
    m = vm.get_mapping("listbox")
    assert m["fabric_type"] == "slicer"
    assert m["bi_type"] != "Unsupported"


def test_container_maps_to_group():
    for qtype in ("container", "tabbed-container"):
        m = vm.get_mapping(qtype)
        assert m["fabric_type"] == "group"
        assert m["bi_type"] != "Unsupported"


def test_button_maps_to_action_button():
    for qtype in ("button", "action-button"):
        m = vm.get_mapping(qtype)
        assert m["fabric_type"] == "actionButton"
        assert m["bi_type"] != "Unsupported"


def test_bookmark_object_maps_to_action_button():
    m = vm.get_mapping("bookmarkobject")
    assert m["fabric_type"] == "actionButton"
    assert m["bi_type"] != "Unsupported"


def test_textimage_gets_its_own_label_not_input_box():
    m = vm.get_mapping("text-image")
    assert m["fabric_type"] == "textbox"
    assert m["bi_type"] == "Text Box / Image"


def test_variable_input_still_maps_to_input_box():
    m = vm.get_mapping("variableinput")
    assert m["bi_type"] == "Variable Slicer"


def test_sn_prefixed_native_types_map_correctly():
    """Modern Qlik Cloud/Sense objects use the 'sn-' (nebula) prefix. Before
    the fix, normalize_qlik_type never stripped it, so every sn-* type
    (the vast majority of visuals in a real Qlik Cloud app) silently fell to
    the generic "default" -> tableEx bucket instead of its real chart type.
    """
    cases = {
        "sn-barchart": "barChart",
        "sn-combochart": "lineClusteredColumnComboChart",
        "sn-kpi": "card",
        "sn-piechart": "pieChart",
        "sn-linechart": "lineChart",
        "sn-scatterplot": "scatterChart",
        "sn-pivot-table": "pivotTable",
        "sn-table": "tableEx",
        "sn-map": "map",
        "sn-treemap": "treemap",
    }
    for qlik_type, expected_fabric_type in cases.items():
        m = vm.get_mapping(qlik_type)
        assert m["fabric_type"] == expected_fabric_type, f"{qlik_type} -> {m}"
        assert m["bi_type"] != "Unsupported", f"{qlik_type} incorrectly marked unsupported"


def test_pack_bubble_and_mekko_are_mapped_not_dropped():
    m = vm.get_mapping("pack-bubble")
    assert m["fabric_type"] == "scatterChart"
    assert m["bi_type"] != "Unsupported"

    m = vm.get_mapping("mekko-chart")
    assert m["fabric_type"] == "treemap"
    assert m["bi_type"] != "Unsupported"


def test_unmapped_type_falls_back_to_default_with_flag_not_silently():
    m = vm.get_mapping("some-totally-unknown-extension")
    assert m["bi_type"] == "Unsupported"
    assert m["fabric_type"] == "tableEx"


def test_boxplot_and_sankey_flagged_as_requiring_custom_visual():
    m = vm.get_mapping("boxplot")
    assert m["fabric_type"] == "boxPlot"
    assert m["requires_custom_visual"] is True

    m = vm.get_mapping("sankey")
    assert m["fabric_type"] == "sankeyDiagram"
    assert m["requires_custom_visual"] is True

    m = vm.get_mapping("barchart")
    assert m["requires_custom_visual"] is False
