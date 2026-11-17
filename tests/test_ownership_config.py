from __future__ import annotations

import pytest

from termharvest.errors import HarvestError
from termharvest.validate_config import load_validation_config

VALID = """
version: 1
ownership_checks:
  - id: exposure_a
    screen_id: demo_synth_bills
    target_bucket: A
    purpose: compliance
    credit_limit: 50
    bucket_footers:
      - {bucket: A, count_fact: a_count, total_fact: a_total}
      - {bucket: B, count_fact: b_count, total_fact: b_total}
"""


def write(tmp_path, text):
    path = tmp_path / "rules.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_ownership_config_parses_defaults(tmp_path):
    config = load_validation_config(write(tmp_path, VALID))
    check = config.ownership_checks[0]
    assert check.check_id == "exposure_a"
    assert check.buckets == ("A", "B")
    assert check.target_bucket == "A"
    assert check.mask_field == "customer_ref"
    assert check.open_statuses == ("OPEN",)
    assert check.purpose == "compliance"
    assert check.credit_limit_value.__float__() == 50.0
    assert check.max_bills > 0 and check.search_budget > 0


def test_manual_review_purpose_and_credit_limit_field(tmp_path):
    text = VALID.replace("purpose: compliance", "purpose: manual_review").replace(
        "credit_limit: 50", "credit_limit_field: {screen_id: demo_customer, field: credit_limit}"
    )
    config = load_validation_config(write(tmp_path, text))
    check = config.ownership_checks[0]
    assert check.purpose == "manual_review"
    assert check.credit_limit_value is None
    assert (check.credit_limit_screen, check.credit_limit_field) == ("demo_customer", "credit_limit")


def test_unknown_purpose_rejected(tmp_path):
    text = VALID.replace("purpose: compliance", "purpose: autopilot")
    with pytest.raises(HarvestError) as caught:
        load_validation_config(write(tmp_path, text))
    assert caught.value.code == "E_VALIDATION_CONFIG"
    assert "purpose" in caught.value.context["field"]


def test_duplicate_ownership_check_id_rejected(tmp_path):
    text = VALID + """
  - id: exposure_a
    screen_id: demo_synth_bills
    target_bucket: A
    credit_limit: 10
    bucket_footers:
      - {bucket: A, total_fact: a_total}
      - {bucket: B, total_fact: b_total}
"""
    with pytest.raises(HarvestError) as caught:
        load_validation_config(write(tmp_path, text))
    assert "duplicate check id" in caught.value.message


def test_credit_limit_requires_exactly_one_source(tmp_path):
    both = VALID + "\n"  # add a field ref alongside the literal
    both = both.replace("credit_limit: 50", "credit_limit: 50\n    credit_limit_field: {screen_id: demo_customer, field: credit_limit}")
    with pytest.raises(HarvestError):
        load_validation_config(write(tmp_path, both))
    neither = VALID.replace("    credit_limit: 50\n", "")
    with pytest.raises(HarvestError):
        load_validation_config(write(tmp_path, neither))


def test_target_bucket_must_be_declared_in_footers(tmp_path):
    text = VALID.replace("target_bucket: A", "target_bucket: Z")
    with pytest.raises(HarvestError) as caught:
        load_validation_config(write(tmp_path, text))
    assert "target_bucket" in caught.value.context["field"]


def test_footer_requires_at_least_one_fact(tmp_path):
    text = VALID.replace("- {bucket: A, count_fact: a_count, total_fact: a_total}", "- {bucket: A}")
    with pytest.raises(HarvestError):
        load_validation_config(write(tmp_path, text))


def test_non_positive_budget_rejected(tmp_path):
    text = VALID.replace("credit_limit: 50", "credit_limit: 50\n    search_budget: 0")
    with pytest.raises(HarvestError):
        load_validation_config(write(tmp_path, text))


def test_legacy_config_without_ownership_section_still_loads():
    config = load_validation_config("/workspace/contracts/validators/demo_rules.yaml")
    assert config.ownership_checks == ()
    assert len(config.batch_checks) == 6
