from __future__ import annotations

from dataclasses import replace

import pytest

from termharvest.cassette import load_cassette
from termharvest.errors import HarvestError
from termharvest.extract import extract_screen
from termharvest.extract_pages import extract_pages
from termharvest.screen import ScreenFrame, normalize_value, to_halfwidth_digits

@pytest.fixture()
def cassette_frames(demo_paths):
    cassette = load_cassette(demo_paths["replay_cassette"])
    return {item.step_id: item.frame for item in cassette.frames}

def test_screen_frame_requires_24_by_80(cassette_frames):
    frame = cassette_frames["open_customer"]
    assert len(frame.rows) == 24
    assert all(len(row) == 80 for row in frame.rows)
    assert len(frame.sha256) == 64
    assert frame.sha256 == ScreenFrame.parse(frame.text).sha256

def test_malformed_frame_dimensions_rejected():
    with pytest.raises(HarvestError) as caught:
        ScreenFrame.parse("too short")
    assert caught.value.code == "E_FRAME_MALFORMED"
    assert caught.value.context["rows"] == 1

def test_customer_absolute_label_multiline_and_sensitive_extraction(cassette_frames, demo_screens):
    frame = cassette_frames["open_customer"]
    result = extract_screen(frame, demo_screens["demo_customer"], 1)
    record = result.records[0]
    values = record["normalized_values"]
    assert values["customer_id"] == "C00042"
    assert values["customer_name"] == "NORTHWIND SYNTHETIC CO"
    assert values["mailing_address"] == "100 SAMPLE WAY / SUITE 200"
    assert values["notes"] == "SYNTHETIC CUSTOMER ONLY"
    assert values["region_code"] == "07"
    assert record["raw_values"]["region_code"] == "０７"
    assert "operator_token" not in values
    assert "operator_token" not in record["raw_values"]
    assert result.sensitive_skipped == ["operator_token"]
    assert record["primary_key"] == {"customer_id": "C00042"}
    assert len(record["record_key"]) == 64
    assert record["frame_sha256"] == frame.sha256

def test_list_page_and_facts(cassette_frames, demo_screens):
    page1 = cassette_frames["open_bills"]
    result = extract_screen(page1, demo_screens["demo_bill_list"], 2)
    assert [record["normalized_values"]["bill_id"] for record in result.records] == ["B000101", "B000102", "B000103"]
    assert result.facts == {"batch_count": "5", "batch_total": "550.00"}
    assert result.records[0]["normalized_values"]["amount"] == "100.00"

def test_pagination_accumulates_and_deduplicates(cassette_frames, demo_screens):
    bills = demo_screens["demo_bill_list"]
    page1 = cassette_frames["open_bills"]
    page2 = cassette_frames["next_page"]
    paged = extract_pages([(2, page1), (3, page2)], bills, max_pages=3, stop_marker="END OF LIST")
    assert paged.page_count == 2
    assert paged.stop_reason == "stop_marker"
    assert len(paged.extraction.records) == 5
    ids = [record["normalized_values"]["bill_id"] for record in paged.extraction.records]
    assert ids == ["B000101", "B000102", "B000103", "B000104", "B000105"]

def test_repeated_page_stops_without_duplicate_records(cassette_frames, demo_screens):
    page1 = cassette_frames["open_bills"]
    paged = extract_pages([(2, page1), (3, page1)], demo_screens["demo_bill_list"], max_pages=3, stop_marker="END OF LIST")
    assert paged.stop_reason == "repeated_page"
    assert len(paged.extraction.records) == 3

def test_contract_mismatch_is_not_silently_skipped(cassette_frames, demo_screens):
    with pytest.raises(HarvestError) as caught:
        extract_screen(cassette_frames["open_customer"], demo_screens["demo_bill_list"], 1)
    assert caught.value.code == "E_SCREEN_CONTRACT_MISMATCH"
    assert "BILL ID" in caught.value.context["missing"]

def test_fingerprint_mismatch_is_rejected(cassette_frames, demo_screens):
    contract = replace(demo_screens["demo_customer"], fingerprint="sha256:" + "0" * 64)
    with pytest.raises(HarvestError) as caught:
        extract_screen(cassette_frames["open_customer"], contract, 1)
    assert caught.value.code == "E_SCREEN_FINGERPRINT_MISMATCH"

def test_normalization_rules():
    assert to_halfwidth_digits("０１２３") == "0123"
    assert normalize_value("  A   B  ", ("trim", "collapse_space")) == "A B"
    assert normalize_value("０７", ("trim", "fullwidth_digits")) == "07"
    assert normalize_value("   ", ("trim",)) is None
