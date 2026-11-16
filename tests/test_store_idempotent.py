from __future__ import annotations

import json

from termharvest.cassette import load_cassette
from termharvest.clock import FixedClock
from termharvest.store import (
    GEN_ABANDONED,
    GEN_PREPARED,
    GEN_PUBLISHED,
    REL_INSERTED,
    REL_UNCHANGED,
    REL_UPDATED,
    Store,
)

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

def test_staged_record_is_idempotent_within_generation(tmp_path, demo_paths):
    clock = FixedClock("2026-04-01T00:00:00.000Z")
    store = Store(tmp_path / "ledger.sqlite")
    try:
        store.start_run("run-1", "demo_session", "live", clock.now_iso())
        first = store.stage_record(sample_record(), validation_status="valid", validation_errors=[], run_id="run-1", clock=clock)
        second = store.stage_record(sample_record(), validation_status="valid", validation_errors=[], run_id="run-1", clock=clock)
        assert first == REL_INSERTED
        assert second == REL_UNCHANGED
        # Nothing is visible before publish.
        assert store.count_records() == 0
        assert store.records() == []
        status = store.snapshot_status()
        assert status["published"] is False
        assert status["prepared_generations"] == [store.generation_for_run("run-1")["generation_id"]]
        manifest = {"schema_version": 1, "generation_id": store.generation_for_run("run-1")["generation_id"], "records_sha256": store.generation_digest("run-1")[0], "files": {}}
        store.publish_generation(
            "run-1", status="success", finished_at=clock.now_iso(), record_count=1,
            frame_count=0, error_count=0, error_code=None, summary={"status": "success"},
            report_manifest=manifest,
        )
        assert store.count_records() == 1
        row = store.records()[0]
        assert json.loads(row["normalized_values_json"])["region_code"] == "07"
        assert row["first_seen_at"] == row["last_seen_at"]
        assert row["validation_status"] == "valid"
        assert row["occurrence_count"] == 2
    finally:
        store.close()

def test_changed_payload_in_new_generation_keeps_both_versions(tmp_path):
    clock = FixedClock()
    store = Store(tmp_path / "ledger.sqlite")
    try:
        store.start_run("run-1", "demo_session", "live", clock.now_iso())
        record = sample_record()
        assert store.stage_record(record, validation_status="valid", validation_errors=[], run_id="run-1", clock=clock) == REL_INSERTED
        gen1 = store.generation_for_run("run-1")["generation_id"]
        manifest1 = {"records_sha256": store.generation_digest("run-1")[0], "files": {}}
        store.publish_generation("run-1", status="success", finished_at=clock.now_iso(), record_count=1, frame_count=0, error_count=0, error_code=None, summary={"status": "success"}, report_manifest=manifest1)

        store.start_run("run-2", "demo_session", "live", clock.now_iso())
        changed = sample_record()
        changed["normalized_values"]["region_code"] = "08"
        outcome = store.stage_record(changed, validation_status="invalid", validation_errors=[{"code": "E_TEST"}], run_id="run-2", clock=clock)
        assert outcome == REL_INSERTED  # first occurrence inside the new generation
        gen2 = store.generation_for_run("run-2")["generation_id"]
        manifest2 = {"records_sha256": store.generation_digest("run-2")[0], "files": {}}
        published = store.publish_generation("run-2", status="success", finished_at=clock.now_iso(), record_count=1, frame_count=0, error_count=1, error_code=None, summary={"status": "success"}, report_manifest=manifest2)
        assert store.current_generation_id() == gen2

        # Identity relationship against the parent generation is explicit.
        changes = {item["record_key"]: item["relation"] for item in published["key_changes"]}
        assert changes[sample_record()["record_key"]] == REL_UPDATED
        current = store.records()[0]
        assert current["validation_status"] == "invalid"
        assert json.loads(current["validation_errors_json"])[0]["code"] == "E_TEST"
        # Old immutable generation is still fully reviewable.
        previous = store.records(generation_id=gen1)[0]
        assert json.loads(previous["normalized_values_json"])["region_code"] == "07"
        assert previous["validation_status"] == "valid"
    finally:
        store.close()

def test_repeated_key_within_one_generation_is_one_identity(tmp_path):
    clock = FixedClock()
    store = Store(tmp_path / "ledger.sqlite")
    try:
        store.start_run("run-1", "demo_session", "live", clock.now_iso())
        record = sample_record()
        assert store.stage_record(record, validation_status="valid", validation_errors=[], run_id="run-1", clock=clock) == REL_INSERTED
        variant = sample_record()
        variant["frame_seq"] = 2
        variant["normalized_values"]["region_code"] = "09"
        assert store.stage_record(variant, validation_status="valid", validation_errors=[], run_id="run-1", clock=clock) == REL_UPDATED
        digest, count = store.generation_digest("run-1")
        assert count == 1  # one canonical identity, never counted twice
        row = store.connection.execute(
            "SELECT occurrence_count, variant_sha_json FROM record_versions WHERE generation_id=? AND record_key=?",
            (store.generation_for_run("run-1")["generation_id"], record["record_key"]),
        ).fetchone()
        assert row["occurrence_count"] == 2
        assert len(json.loads(row["variant_sha_json"])) == 1
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
        gen_id = store.generation_for_run("run-1")["generation_id"]
        store.publish_generation("run-1", status="success", finished_at=clock.now_iso(), record_count=1, frame_count=1, error_count=0, error_code=None, summary={"status": "success"}, report_manifest={"records_sha256": store.generation_digest("run-1")[0], "files": {}})
        assert store.count_frames("run-1") == 1
        events = store.events("run-1")
        assert events[0]["event_type"] == "frame_received"
        assert len(events[0]["event_sha256"]) == 64
        runs = store.runs()
        assert runs[0]["status"] == "success"
        assert runs[0]["record_count"] == 1
        assert store.generation(gen_id)["status"] == GEN_PUBLISHED
    finally:
        store.close()

def test_sensitive_value_is_not_part_of_record(tmp_path):
    store = Store(tmp_path / "ledger.sqlite")
    clock = FixedClock()
    try:
        store.start_run("run-1", "demo_session", "live", clock.now_iso())
        store.stage_record(sample_record(), validation_status="valid", validation_errors=[], run_id="run-1", clock=clock)
        gen_id = store.generation_for_run("run-1")["generation_id"]
        store.publish_generation("run-1", status="success", finished_at=clock.now_iso(), record_count=1, frame_count=0, error_count=0, error_code=None, summary={}, report_manifest={"records_sha256": store.generation_digest("run-1")[0], "files": {}})
        dump = json.dumps(store.records(), ensure_ascii=False)
        assert "SYNTH-PLACEHOLDER" not in dump
        assert "operator_token" not in dump
    finally:
        store.close()

def test_abandoned_generation_keeps_evidence_but_hides_rows(tmp_path):
    store = Store(tmp_path / "ledger.sqlite")
    clock = FixedClock()
    try:
        store.start_run("run-1", "demo_session", "live", clock.now_iso())
        store.stage_record(sample_record(), validation_status="valid", validation_errors=[], run_id="run-1", clock=clock)
        gen_id = store.generation_for_run("run-1")["generation_id"]
        store.abandon_generation("run-1", reason="E_TEST", abandoned_at=clock.now_iso())
        assert store.snapshot_status()["published"] is False
        assert store.records() == []
        generation = store.generation(gen_id)
        assert generation["status"] == GEN_ABANDONED
        assert generation["abandon_reason"] == "E_TEST"
        # Staged diagnostic rows remain reviewable via the generation id.
        assert store.count_records(generation_id=gen_id) == 1
    finally:
        store.close()

def test_reopen_abandons_prepared_generation_and_preserves_published(tmp_path):
    clock = FixedClock()
    store = Store(tmp_path / "ledger.sqlite")
    store.start_run("run-1", "demo_session", "live", clock.now_iso())
    store.stage_record(sample_record(), validation_status="valid", validation_errors=[], run_id="run-1", clock=clock)
    gen1 = store.generation_for_run("run-1")["generation_id"]
    store.publish_generation("run-1", status="success", finished_at=clock.now_iso(), record_count=1, frame_count=0, error_count=0, error_code=None, summary={}, report_manifest={"records_sha256": store.generation_digest("run-1")[0], "files": {}})
    store.start_run("run-2", "demo_session", "live", clock.now_iso())
    gen2 = store.generation_for_run("run-2")["generation_id"]
    assert store.generation(gen2)["status"] == GEN_PREPARED
    store.close()

    reopened = Store(tmp_path / "ledger.sqlite", auto_recover=True)
    try:
        assert reopened.current_generation_id() == gen1
        assert reopened.count_records() == 1
        assert reopened.generation(gen2)["status"] == GEN_ABANDONED
        assert "interrupted" in reopened.generation(gen2)["abandon_reason"]
    finally:
        reopened.close()
