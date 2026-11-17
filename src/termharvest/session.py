"""Read-only live terminal session with target whitelist, retries and evidence."""

from __future__ import annotations

import hashlib
import socket
from pathlib import Path
from typing import Any, Callable

from .capture import CapturedFrame
from .clock import Clock, SystemClock
from .config_types import Expectation, SessionContract
from .config_util import is_loopback_host
from .errors import E_CONNECTION_FAILED, E_SCREEN_UNEXPECTED, E_STEP_RETRY_EXHAUSTED, E_TARGET_NOT_ALLOWED, HarvestError
from .logging_util import safe_context
from .protocol import FrameReader, send_key
from .screen import DEFAULT_INPUT_MODE, INPUT_MODES, ScreenFrame

EventSink = Callable[[str, str, str | None, str, dict[str, Any]], None]

def ensure_loopback_target(host: str, port: int) -> None:
    """Runtime whitelist check, independent from configuration parsing."""
    if not is_loopback_host(host) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise HarvestError(E_TARGET_NOT_ALLOWED, "connection target is not an allowed loopback endpoint", context={"host": host, "port": port})

def _write_evidence(directory: Path | None, label: str, text: str) -> str | None:
    if directory is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    path = directory / f"{label}_{digest}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)

class TerminalSession:
    """Drives a session script over a socket; it can only send key tokens."""

    def __init__(
        self,
        session: SessionContract,
        *,
        event_sink: EventSink | None = None,
        clock: Clock | None = None,
        evidence_dir: str | Path | None = None,
        logger: Any = None,
        input_mode: str = DEFAULT_INPUT_MODE,
    ) -> None:
        if input_mode not in INPUT_MODES:
            raise HarvestError(
                E_TARGET_NOT_ALLOWED,
                "unknown screen input contract",
                context={"input_mode": input_mode, "allowed": list(INPUT_MODES)},
            )
        self.session = session
        self.event_sink = event_sink
        self.clock = clock or SystemClock()
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.logger = logger
        self.input_mode = input_mode
        self.sock: socket.socket | None = None
        self.reader: FrameReader | None = None
        self.last_frame: ScreenFrame | None = None
        self.last_raw: str | None = None

    def _emit(self, level: str, event_type: str, code: str | None, message: str, **context: Any) -> None:
        if self.event_sink is not None:
            self.event_sink(level, event_type, code, message, safe_context(**context))
        if self.logger is not None:
            log = self.logger.info if level == "info" else self.logger.error
            log("event=%s code=%s %s", event_type, code or "-", message)

    def _close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.reader = None

    def _open(self) -> None:
        try:
            self.sock = socket.create_connection((self.session.target_host, self.session.target_port), timeout=self.session.retry.timeout_seconds)
            self.sock.settimeout(self.session.retry.timeout_seconds)
        except OSError as exc:
            raise HarvestError(E_CONNECTION_FAILED, "could not connect to loopback terminal", context={"host": self.session.target_host, "port": self.session.target_port}, cause=exc) from exc
        self.reader = FrameReader(self.sock)
        self._emit("info", "connection_open", None, "connected to loopback terminal", host=self.session.target_host, port=self.session.target_port)

    def _read_checked(self, expectation: Expectation, step_id: str, *, evidence_label: str) -> ScreenFrame:
        assert self.reader is not None
        raw = self.reader.read_frame()
        self.last_raw = raw
        try:
            frame = ScreenFrame.parse(raw, input_mode=self.input_mode)
        except HarvestError as exc:
            path = _write_evidence(self.evidence_dir, "malformed", raw)
            raise HarvestError(exc.code, exc.message, context={**exc.context, "step_id": step_id, "input_mode": self.input_mode, "evidence": path}, cause=exc) from exc
        self.last_frame = frame
        self._emit(
            "info",
            "frame_received",
            None,
            "received screen frame",
            step_id=step_id,
            screen_id=expectation.screen_id,
            frame_sha256=frame.sha256,
            visible_sha256=frame.visible_sha256,
            input_mode=frame.input_mode,
            bytes=len(raw.encode("utf-8")),
        )
        visible = frame.visible_text
        missing = [item for item in expectation.contains if item not in visible]
        if missing:
            path = _write_evidence(self.evidence_dir, evidence_label, raw)
            raise HarvestError(
                E_SCREEN_UNEXPECTED,
                "received screen does not match the session expectation",
                context={
                    "step_id": step_id,
                    "screen_id": expectation.screen_id,
                    "missing": missing,
                    "frame_sha256": frame.sha256,
                    "visible_sha256": frame.visible_sha256,
                    "input_mode": frame.input_mode,
                    "evidence": path,
                },
            )
        return frame

    def _prepare(self, step_index: int) -> None:
        """Open a fresh connection and replay prior accepted navigation steps."""
        self._close()
        self._open()
        self._read_checked(self.session.start, "start", evidence_label="unexpected_start")
        for prior in self.session.steps[:step_index]:
            assert self.sock is not None
            send_key(self.sock, prior.key)
            self._emit("info", "key_sent", None, "sent navigation key", step_id=prior.step_id, key=prior.key, replay=True)
            self._read_checked(prior.expect, prior.step_id, evidence_label="unexpected_replay")

    def _run_step(self, step_index: int) -> ScreenFrame:
        step = self.session.steps[step_index]
        last_error: HarvestError | None = None
        for attempt in range(1, self.session.retry.attempts + 1):
            try:
                if self.sock is None:
                    self._prepare(step_index)
                assert self.sock is not None
                send_key(self.sock, step.key)
                self._emit("info", "key_sent", None, "sent navigation key", step_id=step.step_id, key=step.key, attempt=attempt)
                return self._read_checked(step.expect, step.step_id, evidence_label=f"unexpected_{step.step_id}")
            except HarvestError as exc:
                last_error = exc
                self._emit(
                    "warning", "step_failed", exc.code, "session step failed", step_id=step.step_id, attempt=attempt, max_attempts=self.session.retry.attempts, evidence=exc.context.get("evidence")
                )
                self._close()
                if attempt < self.session.retry.attempts:
                    self.clock.sleep((self.session.retry.backoff_ms / 1000.0) * attempt)
        context = {
            "step_id": step.step_id,
            "attempts": self.session.retry.attempts,
            "last_code": last_error.code if last_error else None,
            "evidence": last_error.context.get("evidence") if last_error else None,
        }
        raise HarvestError(E_STEP_RETRY_EXHAUSTED, "step retries exhausted", context=context, cause=last_error)

    def collect(self) -> list[CapturedFrame]:
        try:
            ensure_loopback_target(self.session.target_host, self.session.target_port)
        except HarvestError as exc:
            self._emit("error", "target_rejected", exc.code, exc.message, host=exc.context.get("host"), port=exc.context.get("port"))
            raise
        self._emit("info", "target_validated", None, "target is loopback", host=self.session.target_host, port=self.session.target_port)
        frames: list[CapturedFrame] = []
        try:
            self._close()
            self._open()
            start = self._read_checked(self.session.start, "start", evidence_label="unexpected_start")
            frames.append(CapturedFrame(frame_seq=0, step_id="start", expected_screen=self.session.start.screen_id, frame=start))
            for index, step in enumerate(self.session.steps):
                frame = self._run_step(index)
                frames.append(CapturedFrame(frame_seq=len(frames), step_id=step.step_id, expected_screen=step.expect.screen_id, frame=frame))
            return frames
        except HarvestError as exc:
            self._emit("error", "session_failed", exc.code, exc.message, evidence=exc.context.get("evidence"))
            raise
        finally:
            self._close()
            self._emit("info", "connection_closed", None, "terminal session closed", frames_captured=len(frames))
