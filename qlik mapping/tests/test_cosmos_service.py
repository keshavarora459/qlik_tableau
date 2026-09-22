"""services.cosmos_service._is_not_found_body

The MongoDB API answers a miss with HTTP 200 and a body like
{"message": "No record found for folder '...'"} instead of a 404.
fetch_parsing_from_cosmos / fetch_mapping_from_cosmos previously treated
that as real data, so /api/mapping silently returned an empty 'success'
Contract 2.0 result for an app/run that was never parsed, instead of the
400 the caller needs to know nothing was found.
"""

from services.cosmos_service import _is_not_found_body


def test_a_not_found_message_body_is_recognized():
    assert _is_not_found_body({"message": "No record found for folder 'x'"}) is True


def test_a_real_parsing_result_is_not_mistaken_for_not_found():
    assert _is_not_found_body({"parsing_result": {"tables": []}, "message": "ok"}) is False


def test_a_real_document_carrying_tables_directly_is_not_mistaken_for_not_found():
    assert _is_not_found_body({"tables": [{"table_name": "Sales"}], "message": "ok"}) is False


def test_a_document_with_no_message_key_is_never_flagged():
    assert _is_not_found_body({"tables": []}) is False
