from __future__ import annotations

from dataclasses import replace

import pytest

from termharvest.clock import FixedClock
from termharvest.errors import HarvestError
from termharvest.mockhost import MockTerminalServer
from termharvest.session import TerminalSession

def test_unexpected_screen_saves_evidence_and_exhausts_retries(tmp_path, demo_session, demo_paths):
    server = MockTerminalServer(demo_paths["error"], port=0, frame_timeout=1.0)
    server.start()
    events = []
    clock = FixedClock("2026-07-01T00:00:00.000Z")

    def sink(level, event_type, code, message, context):
        events.append({"level": level, "event_type": event_type, "code": code, "message": message, "context": context})

    try:
        session = replace(demo_session, target_port=server.actual_port)
        runner = TerminalSession(session, event_sink=sink, clock=clock, evidence_dir=tmp_path / "evidence")
        with pytest.raises(HarvestError) as caught:
            runner.collect()
        error = caught.value
        assert error.code == "E_STEP_RETRY_EXHAUSTED"
        assert error.cause is not None
        assert error.cause.code == "E_SCREEN_UNEXPECTED"
        assert error.context["last_code"] == "E_SCREEN_UNEXPECTED"
        assert error.context["evidence"]
        evidence_files = list((tmp_path / "evidence").glob("*.txt"))
        assert evidence_files
        assert "SYNTHETIC ERROR SCREEN" in evidence_files[0].read_text(encoding="utf-8")
        assert any(item["event_type"] == "step_failed" for item in events)
        assert clock.sleeps == [0.1]
        for event in events:
            assert "SYNTHETIC ERROR SCREEN" not in str(event["context"])
    finally:
        server.stop()

def test_target_whitelist_runs_before_connection(demo_session):
    events = []

    def sink(level, event_type, code, message, context):
        events.append((level, event_type, code, context))

    session = replace(demo_session, target_host="192.0.2.55")
    runner = TerminalSession(session, event_sink=sink, clock=FixedClock())
    with pytest.raises(HarvestError) as caught:
        runner.collect()
    assert caught.value.code == "E_TARGET_NOT_ALLOWED"
    assert events == [("error", "target_rejected", "E_TARGET_NOT_ALLOWED", {"host": "192.0.2.55", "port": 19771})]

def test_session_failure_event_is_emitted(demo_session, demo_paths):
    server = MockTerminalServer(demo_paths["error"], port=0, frame_timeout=1.0)
    server.start()
    events = []
    try:
        session = replace(demo_session, target_port=server.actual_port)
        runner = TerminalSession(session, event_sink=lambda *args: events.append(args), clock=FixedClock())
        with pytest.raises(HarvestError):
            runner.collect()
        codes = [item[2] for item in events if item[1] == "session_failed"]
        assert codes == ["E_STEP_RETRY_EXHAUSTED"]
    finally:
        server.stop()

def test_malformed_replay_is_a_frame_error(demo_session, demo_paths, tmp_path):
    from termharvest.replay import collect_replay

    with pytest.raises(HarvestError) as caught:
        collect_replay(demo_paths["malformed"], demo_session, evidence_dir=tmp_path / "evidence")
    assert caught.value.code == "E_FRAME_MALFORMED"
