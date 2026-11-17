from __future__ import annotations

from pathlib import Path

import pytest

from termharvest.config import load_screen, load_screens_dir, load_session, load_yaml_document
from termharvest.errors import HarvestError

def write_yaml(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path

def minimal_screen(fields: str = "", list_region: str = "") -> str:
    return f"""
version: 1
screen_id: test_screen
match:
  - TEST SCREEN
primary_key: [code]
fields:
{fields or '''
  - name: code
    locator: {kind: absolute, row: 1, col: 2, length: 10}
'''}
{list_region}
"""

def minimal_session(host: str = "127.0.0.1") -> str:
    return f"""
version: 1
session_id: test_session
target: {{host: "{host}", port: 12345}}
start:
  screen_id: menu
  contains: [MENU]
retry: {{attempts: 2, timeout_seconds: 1, backoff_ms: 0}}
steps:
  - id: open
    key: ENTER
    expect: {{screen_id: screen, contains: [DATA]}}
"""

def test_demo_contracts_load_with_expected_shapes(demo_screens, demo_session):
    assert set(demo_screens) == {"demo_customer", "demo_bill_list", "demo_synth_bills"}
    customer = demo_screens["demo_customer"]
    bills = demo_screens["demo_bill_list"]
    assert customer.primary_key == ("customer_id",)
    assert any(field.sensitive for field in customer.fields)
    assert bills.list_region is not None
    assert [column.name for column in bills.list_region.columns] == ["bill_id", "bill_date", "amount", "status"]
    synth = demo_screens["demo_synth_bills"]
    assert [column.name for column in synth.list_region.columns] == ["bill_id", "bill_date", "amount", "status", "customer_ref"]
    assert {field.name for field in synth.summary_fields} == {"a_count", "a_total", "b_count", "b_total"}
    assert len(demo_session.steps) == 3
    assert demo_session.paging is not None
    assert demo_session.paging.max_pages == 3

def test_screens_directory_is_sorted_and_loaded(demo_screens):
    assert list(demo_screens) == ["demo_bill_list", "demo_customer", "demo_synth_bills"]

def test_yaml_parse_error_has_file_and_line(tmp_path):
    path = write_yaml(tmp_path, "bad.yaml", "screen_id: [unterminated\n")
    with pytest.raises(HarvestError) as caught:
        load_yaml_document(path)
    assert caught.value.code == "E_CONFIG_PARSE"
    assert caught.value.context["file"].endswith("bad.yaml")
    assert "line" in caught.value.context

def test_schema_error_points_to_field(tmp_path):
    path = write_yaml(tmp_path, "screen.yaml", "match: [X]\nfields: []\n")
    with pytest.raises(HarvestError) as caught:
        load_screen(path)
    assert caught.value.code == "E_CONFIG_SCHEMA"
    assert caught.value.context["field"] == "screen_id"
    assert caught.value.context["file"].endswith("screen.yaml")

def test_duplicate_field_is_rejected(tmp_path):
    fields = """
  - name: code
    locator: {kind: absolute, row: 1, col: 2, length: 10}
  - name: code
    locator: {kind: absolute, row: 2, col: 2, length: 10}
"""
    path = write_yaml(tmp_path, "dup.yaml", minimal_screen(fields))
    with pytest.raises(HarvestError) as caught:
        load_screen(path)
    assert caught.value.code == "E_CONFIG_DUPLICATE_FIELD"
    assert "duplicate" in caught.value.message

def test_bad_locator_fails_fast(tmp_path):
    fields = """
  - name: code
    locator: {kind: telepathy, row: 1, col: 2, length: 10}
"""
    path = write_yaml(tmp_path, "badloc.yaml", minimal_screen(fields))
    with pytest.raises(HarvestError) as caught:
        load_screen(path)
    assert caught.value.code == "E_CONFIG_BAD_LOCATOR"
    assert caught.value.context["field"].endswith("locator")

def test_absolute_locator_outside_width_rejected(tmp_path):
    fields = """
  - name: code
    locator: {kind: absolute, row: 1, col: 75, length: 10}
"""
    path = write_yaml(tmp_path, "wide.yaml", minimal_screen(fields))
    with pytest.raises(HarvestError) as caught:
        load_screen(path)
    assert caught.value.code == "E_CONFIG_BAD_LOCATOR"

def test_non_loopback_session_target_rejected(tmp_path):
    path = write_yaml(tmp_path, "target.yaml", minimal_session("198.51.100.7"))
    with pytest.raises(HarvestError) as caught:
        load_session(path)
    assert caught.value.code == "E_CONFIG_BAD_TARGET"
    assert caught.value.context["field"] == "target.host"

def test_bad_port_is_config_bad_target(tmp_path):
    text = minimal_session().replace("port: 12345", "port: 0")
    path = write_yaml(tmp_path, "port.yaml", text)
    with pytest.raises(HarvestError) as caught:
        load_session(path)
    assert caught.value.code == "E_CONFIG_BAD_TARGET"

def test_duplicate_step_id_rejected(tmp_path):
    text = minimal_session().replace("  - id: open\n    key: ENTER", "  - id: open\n    key: ENTER")
    text = text.replace("steps:\n", "steps:\n  - id: open\n    key: PF3\n    expect: {screen_id: menu, contains: [MENU]}\n")
    path = write_yaml(tmp_path, "steps.yaml", text)
    with pytest.raises(HarvestError) as caught:
        load_session(path)
    assert caught.value.code == "E_CONFIG_SCHEMA"
    assert "duplicate step" in caught.value.message

def test_screens_directory_without_yaml_errors(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(HarvestError) as caught:
        load_screens_dir(empty)
    assert caught.value.code == "E_CONFIG_PARSE"
