from __future__ import annotations

import json

import pytest

from termharvest.cassette import load_cassette
from termharvest.clock import FixedClock
from termharvest.errors import HarvestError
from termharvest.pipeline import run_pipeline
from termharvest.store import Store

def test_live_and_replay_produce_identical_records(tmp_path, live_session, demo_session, demo_screens, demo_validation, demo_paths):
    live_store = Store(tmp_path / "live.sqlite")
    replay_store = Store(tmp_path / "replay.sqlite")
    try:
        live = run_pipeline(
            session=live_session,
            screens=demo_screens,
            validation_config=demo_validation,
            store=live_store,
            output_dir=tmp_path / "live_report",
            clock=FixedClock("2026-06-01T00:00:00.000Z"),
            evidence_dir=tmp_path / "live_evidence",
            record_cassette=tmp_path / "recorded.jsonl",
        )
        replay = run_pipeline(
            session=demo_session,
            screens=demo_screens,
            validation_config=demo_validation,
            store=replay_store,
            output_dir=tmp_path / "replay_report",
            clock=FixedClock("2026-06-02T00:00:00.000Z"),
            mode="replay",
            cassette_path=demo_paths["replay_cassette"],
            evidence_dir=tmp_path / "replay_evidence",
        )
        assert live["payload"]["records"] == replay["payload"]["records"]
        for key in ("record_count", "frame_count", "error_count", "error_codes", "records_by_screen", "screens", "pagination", "validation", "sensitive_fields_skipped"):
            assert live["summary"][key] == replay["summary"][key]
        assert live["summary"]["mode"] == "live"
        assert replay["summary"]["mode"] == "replay"

        recorded = load_cassette(tmp_path / "recorded.jsonl")
        fixture = load_cassette(demo_paths["replay_cassette"])
        assert recorded.session_id == fixture.session_id
        assert recorded.captured_by == "live"
        assert [item.frame.text for item in recorded.frames] == [item.frame.text for item in fixture.frames]
    finally:
        live_store.close()
        replay_store.close()

def test_malformed_cassette_fails_with_frame_error(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    store = Store(tmp_path / "bad.sqlite")
    try:
        with pytest.raises(HarvestError) as caught:
            run_pipeline(
                session=demo_session,
                screens=demo_screens,
                validation_config=demo_validation,
                store=store,
                output_dir=tmp_path / "report",
                clock=FixedClock(),
                mode="replay",
                cassette_path=demo_paths["malformed"],
                evidence_dir=tmp_path / "evidence",
            )
        assert caught.value.code == "E_FRAME_MALFORMED"
        assert store.runs()[0]["status"] == "failed"
        assert store.runs()[0]["error_code"] == "E_FRAME_MALFORMED"
    finally:
        store.close()

def test_cassette_with_wrong_session_is_rejected(tmp_path, demo_session, demo_paths):
    lines = demo_paths["replay_cassette"].read_text(encoding="utf-8").splitlines()
    lines[0] = json.dumps({"captured_by": "wrong", "record_type": "header", "session_id": "other_session", "version": 1}, sort_keys=True)
    path = tmp_path / "wrong_session.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    store = Store(tmp_path / "db.sqlite")
    try:
        with pytest.raises(HarvestError) as caught:
            from termharvest.pipeline import run_pipeline

            run_pipeline(
                session=demo_session,
                screens={},
                validation_config=__import__("termharvest.validate_config", fromlist=["ValidationConfig"]).ValidationConfig(),
                store=store,
                output_dir=tmp_path / "report",
                mode="replay",
                cassette_path=path,
            )
        assert caught.value.code == "E_CASSETTE_MISMATCH"
    finally:
        store.close()
