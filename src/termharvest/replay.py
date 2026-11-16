"""Deterministic cassette replay with the same screen expectations as live mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .capture import CapturedFrame
from .cassette import Cassette, load_cassette
from .clock import Clock, SystemClock
from .config_types import SessionContract
from .errors import E_CASSETTE_INCOMPLETE, E_CASSETTE_MISMATCH, E_SCREEN_UNEXPECTED, HarvestError
from .logging_util import safe_context
from .session import EventSink, _write_evidence

def collect_replay(
    cassette_path: str | Path, session: SessionContract, *, event_sink: EventSink | None = None, clock: Clock | None = None, evidence_dir: str | Path | None = None, logger: Any = None
) -> list[CapturedFrame]:
    return verify_replay(load_cassette(cassette_path), session, event_sink=event_sink, clock=clock, evidence_dir=evidence_dir, logger=logger)

def verify_replay(
    cassette: Cassette, session: SessionContract, *, event_sink: EventSink | None = None, clock: Clock | None = None, evidence_dir: str | Path | None = None, logger: Any = None
) -> list[CapturedFrame]:
    """Verify a parsed cassette against the session contract and return frames."""
    clock = clock or SystemClock()
    evidence = Path(evidence_dir) if evidence_dir else None

    def emit(level: str, event_type: str, code: str | None, message: str, **context: Any) -> None:
        if event_sink is not None:
            event_sink(level, event_type, code, message, safe_context(**context))
        if logger is not None:
            log = logger.info if level == "info" else logger.error
            log("event=%s code=%s %s", event_type, code or "-", message)

    if cassette.session_id != session.session_id:
        raise HarvestError(E_CASSETTE_MISMATCH, "cassette session_id does not match session contract", context={"cassette_session_id": cassette.session_id, "session_id": session.session_id})
    expected = [("start", session.start.screen_id, session.start.contains)]
    expected.extend((step.step_id, step.expect.screen_id, step.expect.contains) for step in session.steps)
    if len(cassette.frames) != len(expected):
        raise HarvestError(E_CASSETTE_INCOMPLETE, "cassette frame count does not match session steps", context={"expected": len(expected), "actual": len(cassette.frames)})
    captured: list[CapturedFrame] = []
    for index, (record, expectation) in enumerate(zip(cassette.frames, expected)):
        step_id, screen_id, fragments = expectation
        if record.frame_seq != index or record.step_id != step_id:
            raise HarvestError(E_CASSETTE_MISMATCH, "cassette frame order does not match session steps", context={"index": index, "expected_step": step_id, "record_step": record.step_id})
        if record.expected_screen != screen_id:
            raise HarvestError(E_CASSETTE_MISMATCH, "cassette expected_screen does not match contract", context={"index": index, "expected_screen": screen_id, "record_screen": record.expected_screen})
        missing = [fragment for fragment in fragments if fragment not in record.frame.text]
        if missing:
            path = _write_evidence(evidence, f"unexpected_{step_id}", record.frame.text)
            emit(
                "error",
                "screen_unexpected",
                E_SCREEN_UNEXPECTED,
                "replayed frame does not match session expectation",
                step_id=step_id,
                missing=missing,
                frame_sha256=record.frame.sha256,
                evidence=path,
            )
            raise HarvestError(
                E_SCREEN_UNEXPECTED,
                "replayed frame does not match session expectation",
                context={"step_id": step_id, "screen_id": screen_id, "missing": missing, "frame_sha256": record.frame.sha256, "evidence": path},
            )
        emit("info", "replay_frame", None, "loaded replay frame", step_id=step_id, screen_id=screen_id, frame_sha256=record.frame.sha256)
        captured.append(CapturedFrame(frame_seq=index, step_id=step_id, expected_screen=screen_id, frame=record.frame))
    emit("info", "replay_complete", None, "cassette replay completed", frames=len(captured), captured_by=cassette.captured_by, at=clock.now_iso())
    return captured
