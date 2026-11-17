from __future__ import annotations

import json

from termharvest.cassette import load_cassette
from termharvest.clock import FixedClock
from termharvest.store import Store

def frame_from_cassette(demo_paths):
    cassette = load_cassette(demo_paths["replay_cassette"])
    return cassette.frames[1].frame

def sample_record():
    return {
        "screen_id": "demo_customer",
        "frame_seq": 1,
        "frame_sha256": "a" * 64,
        "primary_key": {"customer_id": "C00042"},
        "raw_values": {"customer_id": "C00042", "region_code": "０７"},
        "normalized_values": {"customer_id": "C00042", "region_code": "07"},
        "record_key": "b" * 64,
    }

def test_record_upsert_is_idempotent(tmp_path, demo_paths):
    clock = FixedClock("2026-04-01T00:00:00.000Z")
    store = Store(tmp_path / "ledger.sqlite")
    try:
        store.start_run("run-1", "demo_session", "live", clock.now_iso())
        first = store.upsert_record(sample_record(), validation_status="valid", validation_errors=[], run_id="run-1", clock=clock)
        second = store.upsert_record(sample_record(), validation_status="valid", validation_errors=[], run_id="run-1", clock=clock)
        assert first == "inserted"
        assert second == "unchanged"
        assert store.count_records() == 1
        row = store.records()[0]
        assert json.loads(row["normalized_values_json"])["region_code"] == "07"
        assert row["first_seen_at"] == row["last_seen_at"]
        assert row["validation_status"] == "valid"
    finally:
        store.close()

def test_changed_payload_updates_without_duplicating(tmp_path):
    clock = FixedClock()
    store = Store(tmp_path / "ledger.sqlite")
    try:
        store.start_run("run-1", "demo_session", "live", clock.now_iso())
        record = sample_record()
        store.upsert_record(record, validation_status="valid", validation_errors=[], run_id="run-1", clock=clock)
        changed = sample_record()
        changed["normalized_values"]["region_code"] = "08"
        outcome = store.upsert_record(changed, validation_status="invalid", validation_errors=[{"code": "E_TEST"}], run_id="run-2", clock=clock)
        assert outcome == "updated"
        assert store.count_records() == 1
        row = store.records()[0]
        assert row["last_run_id"] == "run-2"
        assert json.loads(row["validation_errors_json"])[0]["code"] == "E_TEST"
    finally:
        store.close()

def test_frames_events_and_runs_are_retained(tmp_path, demo_paths):
    clock = FixedClock()
    store = Store(tmp_path / "ledger.sqlite")
    try:
        store.start_run("run-1", "demo_session", "live", clock.now_iso())
        frame = frame_from_cassette(demo_paths)
        store.store_frame("run-1", 1, "demo_customer", "open_customer", frame, clock.now_iso())
        store.event("run-1", clock, "info", "frame_received", message="received screen frame", context={"frame_sha256": frame.sha256})
        store.finish_run("run-1", status="success", finished_at=clock.now_iso(), record_count=1, error_count=0, error_code=None, summary={"status": "success"})
        assert store.count_frames("run-1") == 1
        events = store.events("run-1")
        assert events[0]["event_type"] == "frame_received"
        assert len(events[0]["event_sha256"]) == 64
        runs = store.runs()
        assert runs[0]["status"] == "success"
        assert runs[0]["record_count"] == 1
    finally:
        store.close()

def test_sensitive_value_is_not_part_of_record(tmp_path):
    store = Store(tmp_path / "ledger.sqlite")
    clock = FixedClock()
    try:
        store.start_run("run-1", "demo_session", "live", clock.now_iso())
        store.upsert_record(sample_record(), validation_status="valid", validation_errors=[], run_id="run-1", clock=clock)
        dump = json.dumps(store.records(), ensure_ascii=False)
        assert "SYNTH-PLACEHOLDER" not in dump
        assert "operator_token" not in dump
    finally:
        store.close()
