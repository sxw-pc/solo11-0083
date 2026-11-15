from __future__ import annotations

import json

import pytest

from termharvest.clock import FixedClock
from termharvest.errors import HarvestError
from termharvest.pipeline import run_pipeline
from termharvest.store import Store

def test_live_pipeline_writes_ledger_and_reports(tmp_path, live_session, demo_screens, demo_validation, demo_paths):
    clock = FixedClock("2026-05-01T09:00:00.000Z")
    store = Store(tmp_path / "ledger.sqlite")
    try:
        result = run_pipeline(
            session=live_session, screens=demo_screens, validation_config=demo_validation, store=store, output_dir=tmp_path / "report", clock=clock, evidence_dir=tmp_path / "evidence"
        )
        summary = result["summary"]
        expected = json.loads(demo_paths["expected_summary"].read_text(encoding="utf-8"))
        for key, value in expected.items():
            assert summary[key] == value, key
        assert store.count_records() == 6
        assert store.count_frames(result["run_id"]) == 4
        assert store.count_frames() == 4
        assert {item.name for item in (tmp_path / "report").iterdir()} == {"report.html", "report.json", "report.md", "report_summary.json", "records.csv"}
        event_types = {event["event_type"] for event in store.events()}
        assert {"target_validated", "connection_open", "key_sent", "frame_received", "frame_stored", "run_completed"} <= event_types
        assert all(len(event["event_sha256"]) == 64 for event in store.events())
        record_rows = store.records()
        db_text = json.dumps(record_rows, ensure_ascii=False)
        assert "SYNTH-PLACEHOLDER" not in db_text
        assert "operator_token" not in db_text
        report_text = (tmp_path / "report" / "report.json").read_text(encoding="utf-8")
        assert "SYNTH-PLACEHOLDER" not in report_text
    finally:
        store.close()

def test_rerun_is_idempotent_for_records(tmp_path, live_session, demo_screens, demo_validation):
    clock = FixedClock("2026-05-01T09:00:00.000Z")
    store = Store(tmp_path / "ledger.sqlite")
    kwargs = dict(session=live_session, screens=demo_screens, validation_config=demo_validation, store=store, output_dir=tmp_path / "report", clock=clock, evidence_dir=tmp_path / "evidence")
    try:
        first = run_pipeline(**kwargs)
        second = run_pipeline(**kwargs)
        assert first["run_id"] == second["run_id"]
        assert store.count_records() == 6
        assert store.count_frames() == 4
        assert len(store.runs()) == 1
    finally:
        store.close()

def test_expected_records_match_pipeline_records(tmp_path, live_session, demo_screens, demo_validation, demo_paths):
    store = Store(tmp_path / "ledger.sqlite")
    try:
        result = run_pipeline(
            session=live_session,
            screens=demo_screens,
            validation_config=demo_validation,
            store=store,
            output_dir=tmp_path / "report",
            clock=FixedClock("2026-05-02T09:00:00.000Z"),
            evidence_dir=tmp_path / "evidence",
        )
        expected = json.loads(demo_paths["expected_records"].read_text(encoding="utf-8"))
        actual = result["payload"]["records"]
        assert len(actual) == expected["record_count"]
        for wanted, got in zip(expected["records"], actual):
            for field_name in ("screen_id", "frame_seq", "record_key", "primary_key", "raw_values", "normalized_values", "frame_sha256", "validation_status"):
                assert got[field_name] == wanted[field_name]
    finally:
        store.close()

def test_non_loopback_override_is_audited(tmp_path, live_session, demo_screens, demo_validation):
    clock = FixedClock("2026-05-03T09:00:00.000Z")
    store = Store(tmp_path / "ledger.sqlite")
    try:
        with pytest.raises(HarvestError) as caught:
            run_pipeline(
                session=live_session,
                screens=demo_screens,
                validation_config=demo_validation,
                store=store,
                output_dir=tmp_path / "report",
                clock=clock,
                evidence_dir=tmp_path / "evidence",
                host_override="192.0.2.10",
            )
        assert caught.value.code == "E_TARGET_NOT_ALLOWED"
        event_codes = [event["code"] for event in store.events()]
        assert "E_TARGET_NOT_ALLOWED" in event_codes
        assert store.runs()[0]["status"] == "failed"
        summary = json.loads((tmp_path / "report" / "report_summary.json").read_text(encoding="utf-8"))
        assert summary["status"] == "failed"
        assert summary["error_codes"] == ["E_TARGET_NOT_ALLOWED"]
    finally:
        store.close()
