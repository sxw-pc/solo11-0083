from __future__ import annotations

import copy
import json

from termharvest.validate import validate_records

FACTS = {"demo_bill_list": {"batch_count": "5", "batch_total": "550.00"}}

def load_records(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return copy.deepcopy(data["records"])

def codes(result):
    return [error["code"] for error in result.errors]

def find(records, screen_id):
    return next(item for item in records if item["screen_id"] == screen_id)

def test_expected_records_pass_all_rules(demo_validation, demo_paths):
    records = load_records(demo_paths["expected_records"])
    result = validate_records(records, demo_validation, facts_by_screen=FACTS)
    assert result.ok
    assert result.stats["records_in"] == 6
    assert result.stats["records_valid"] == 6
    assert result.stats["records_invalid"] == 0

def test_field_required_pattern_enum_and_range(demo_validation, demo_paths):
    records = load_records(demo_paths["expected_records"])
    customer = find(records, "demo_customer")
    customer["normalized_values"]["customer_name"] = None
    customer["normalized_values"]["customer_id"] = "X00042"
    customer["normalized_values"]["status"] = "ARCHIVED"
    result = validate_records(records, demo_validation, facts_by_screen=FACTS)
    assert "E_FIELD_REQUIRED" in codes(result)
    assert "E_FIELD_PATTERN" in codes(result)
    assert "E_FIELD_ENUM" in codes(result)
    assert result.stats["records_invalid"] == 1

def test_decimal_range_and_date_format(demo_validation, demo_paths):
    records = load_records(demo_paths["expected_records"])
    bill = find(records, "demo_bill_list")
    bill["normalized_values"]["amount"] = "-0.01"
    bill["normalized_values"]["bill_date"] = "2026-1-5"
    result = validate_records(records, demo_validation, facts_by_screen=FACTS)
    assert "E_FIELD_RANGE" in codes(result)
    assert "E_FIELD_FORMAT" in codes(result)

def test_invalid_date_type(demo_validation, demo_paths):
    records = load_records(demo_paths["expected_records"])
    customer = find(records, "demo_customer")
    customer["normalized_values"]["period_end"] = "2026-13-40"
    result = validate_records(records, demo_validation, facts_by_screen=FACTS)
    assert "E_FIELD_TYPE" in codes(result)

def test_cross_field_consistency(demo_validation, demo_paths):
    records = load_records(demo_paths["expected_records"])
    customer = find(records, "demo_customer")
    customer["normalized_values"]["balance"] = "6000.00"
    customer["normalized_values"]["period_end"] = "2025-12-31"
    result = validate_records(records, demo_validation, facts_by_screen=FACTS)
    consistency = [item for item in result.errors if item["code"] == "E_RECORD_CONSISTENCY"]
    assert {item["rule_id"] for item in consistency} == {"balance_within_credit_limit", "period_starts_before_end"}

def test_duplicate_key_and_duplicate_record(demo_validation, demo_paths):
    records = load_records(demo_paths["expected_records"])
    duplicate = copy.deepcopy(find(records, "demo_bill_list"))
    duplicate["record_key"] = "f" * 64
    records.append(duplicate)
    result = validate_records(records, demo_validation, facts_by_screen=FACTS)
    assert "E_BATCH_DUPLICATE_KEY" in codes(result)
    assert "E_BATCH_DUPLICATE_RECORD" in codes(result)

def test_sum_mismatch(demo_validation, demo_paths):
    records = load_records(demo_paths["expected_records"])
    bill = find(records, "demo_bill_list")
    bill["normalized_values"]["amount"] = "101.00"
    result = validate_records(records, demo_validation, facts_by_screen=FACTS)
    assert "E_BATCH_SUM_MISMATCH" in codes(result)
    assert "E_BATCH_COUNT_MISMATCH" not in codes(result)

def test_empty_batch(demo_validation):
    result = validate_records([], demo_validation, facts_by_screen={})
    assert "E_BATCH_EMPTY" in codes(result)
    assert result.stats["records_in"] == 0

def test_missing_fact_is_explained(demo_validation, demo_paths):
    records = load_records(demo_paths["expected_records"])
    result = validate_records(records, demo_validation, facts_by_screen={})
    assert "E_BATCH_SUM_MISMATCH" in codes(result)
    assert any("missing" in error["message"] for error in result.errors)
