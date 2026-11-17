"""JSONL cassette recording and replay loading."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import E_CASSETTE_INCOMPLETE, E_CASSETTE_MISMATCH, E_CASSETTE_PARSE, HarvestError
from .screen import ScreenFrame

@dataclass(frozen=True)
class CassetteFrame:
    frame_seq: int
    step_id: str
    expected_screen: str
    frame: ScreenFrame

@dataclass(frozen=True)
class Cassette:
    session_id: str
    captured_by: str
    frames: tuple[CassetteFrame, ...]
    source_file: str

def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def write_cassette(path: str | Path, *, session_id: str, frames: list[tuple[int, str, str, ScreenFrame]], captured_by: str = "mock") -> None:
    """Write a stable JSONL cassette from (seq, step, screen, frame) rows."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [_canonical({"record_type": "header", "version": 1, "session_id": session_id, "captured_by": captured_by})]
    for seq, step_id, expected_screen, frame in frames:
        lines.append(_canonical({"record_type": "frame", "frame_seq": seq, "step_id": step_id, "expected_screen": expected_screen, "frame_sha256": frame.sha256, "text": frame.text}))
    lines.append(_canonical({"record_type": "trailer", "frame_count": len(frames)}))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

def load_cassette(path: str | Path) -> Cassette:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarvestError(E_CASSETTE_PARSE, f"cannot read cassette: {exc}", context={"file": str(source)}) from exc

    header: dict[str, Any] | None = None
    frames: list[CassetteFrame] = []
    trailer: dict[str, Any] | None = None
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HarvestError(E_CASSETTE_PARSE, "cassette line is not valid JSON", context={"file": str(source), "line": line_number}, cause=exc) from exc
        if not isinstance(record, dict):
            raise HarvestError(E_CASSETTE_PARSE, "cassette record must be an object", context={"file": str(source), "line": line_number})
        record_type = record.get("record_type")
        if record_type == "header":
            if header is not None:
                raise HarvestError(E_CASSETTE_MISMATCH, "cassette has multiple headers", context={"file": str(source), "line": line_number})
            header = record
        elif record_type == "frame":
            if header is None:
                raise HarvestError(E_CASSETTE_PARSE, "frame appears before header", context={"file": str(source), "line": line_number})
            seq = record.get("frame_seq")
            step_id = record.get("step_id")
            expected_screen = record.get("expected_screen")
            frame_text = record.get("text")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                raise HarvestError(E_CASSETTE_PARSE, "frame_seq must be a non-negative integer", context={"file": str(source), "line": line_number})
            if not isinstance(step_id, str) or not isinstance(expected_screen, str):
                raise HarvestError(E_CASSETTE_PARSE, "step_id and expected_screen must be strings", context={"file": str(source), "line": line_number})
            if not isinstance(frame_text, str):
                raise HarvestError(E_CASSETTE_PARSE, "frame text must be a string", context={"file": str(source), "line": line_number})
            frame = ScreenFrame.parse(frame_text)
            recorded_hash = record.get("frame_sha256")
            if recorded_hash is not None and recorded_hash != frame.sha256:
                raise HarvestError(E_CASSETTE_MISMATCH, "frame hash does not match cassette record", context={"file": str(source), "line": line_number})
            if len(frames) != seq:
                raise HarvestError(E_CASSETTE_MISMATCH, "frame_seq values must be contiguous from zero", context={"file": str(source), "line": line_number})
            frames.append(CassetteFrame(frame_seq=seq, step_id=step_id, expected_screen=expected_screen, frame=frame))
        elif record_type == "trailer":
            trailer = record
        else:
            raise HarvestError(E_CASSETTE_PARSE, f"unknown cassette record_type {record_type!r}", context={"file": str(source), "line": line_number})
    if header is None:
        raise HarvestError(E_CASSETTE_INCOMPLETE, "cassette has no header", context={"file": str(source)})
    if trailer is None:
        raise HarvestError(E_CASSETTE_INCOMPLETE, "cassette has no trailer", context={"file": str(source)})
    if trailer.get("frame_count") != len(frames):
        raise HarvestError(E_CASSETTE_INCOMPLETE, "cassette frame count does not match trailer", context={"file": str(source), "declared": trailer.get("frame_count"), "actual": len(frames)})
    session_id = header.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise HarvestError(E_CASSETTE_PARSE, "cassette header has no session_id", context={"file": str(source)})
    captured_by = str(header.get("captured_by", "unknown"))
    return Cassette(session_id=session_id, captured_by=captured_by, frames=tuple(frames), source_file=str(source))
