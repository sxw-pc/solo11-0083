"""SQLite ledger: runs, screens, records, events and idempotent upserts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .clock import Clock
from .errors import E_STORE_ERROR, HarvestError
from .screen import ScreenFrame
from .store_schema import SCHEMA

def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

class Store:
    def __init__(self, path: str | Path) -> None:
        target = Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(target))
            self.connection.row_factory = sqlite3.Row
            self.connection.executescript(SCHEMA)
            self.connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise HarvestError(E_STORE_ERROR, "cannot open SQLite ledger", context={"file": str(target)}, cause=exc) from exc

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start_run(self, run_id: str, session_id: str, mode: str, started_at: str) -> None:
        self.connection.execute("DELETE FROM validation_evidence WHERE run_id=?", (run_id,))
        self.connection.execute(
            """INSERT INTO runs(run_id, session_id, mode, started_at, status)
               VALUES (?, ?, ?, ?, 'running')
               ON CONFLICT(run_id) DO UPDATE SET
                 started_at=excluded.started_at, status='running',
                 finished_at=NULL, error_code=NULL""",
            (run_id, session_id, mode, started_at),
        )
        self.connection.commit()

    def finish_run(self, run_id: str, *, status: str, finished_at: str, record_count: int, error_count: int, error_code: str | None, summary: dict[str, Any]) -> None:
        self.connection.execute(
            """UPDATE runs SET status=?, finished_at=?, record_count=?,
               error_count=?, error_code=?, summary_json=? WHERE run_id=?""",
            (status, finished_at, record_count, error_count, error_code, _json(summary), run_id),
        )
        self.connection.commit()

    def event(self, run_id: str, clock: Clock, level: str, event_type: str, *, code: str | None = None, message: str, context: dict[str, Any] | None = None) -> None:
        """Record an audit event; context must not contain secrets."""
        import hashlib

        timestamp = clock.now_iso()
        context_json = _json(context or {})
        canonical = "|".join([run_id, timestamp, level, event_type, code or "", message, context_json])
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.connection.execute(
            """INSERT INTO events(run_id, ts, level, event_type, code, message,
               context_json, event_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, timestamp, level, event_type, code, message, context_json, digest),
        )
        self.connection.commit()

    def store_frame(self, run_id: str, frame_seq: int, screen_id: str, step_id: str, frame: ScreenFrame, captured_at: str) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO frames(run_id, frame_seq, screen_id,
               step_id, frame_sha256, raw_text, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, frame_seq, screen_id, step_id, frame.sha256, frame.text, captured_at),
        )
        self.connection.commit()

    def upsert_record(self, record: dict[str, Any], *, validation_status: str, validation_errors: list[dict[str, Any]], run_id: str, clock: Clock) -> str:
        import hashlib

        key = str(record.get("record_key"))
        if not key:
            raise HarvestError(E_STORE_ERROR, "record is missing record_key", context={"screen_id": record.get("screen_id")})
        normalized = record.get("normalized_values", {})
        raw = record.get("raw_values", {})
        primary = record.get("primary_key", {})
        record_sha = hashlib.sha256(_json({"screen_id": record.get("screen_id"), "normalized": normalized, "raw": raw, "primary": primary}).encode("utf-8")).hexdigest()
        existing = self.connection.execute("SELECT record_sha256, validation_status FROM records WHERE record_key=?", (key,)).fetchone()
        now = clock.now_iso()
        if existing is None:
            self.connection.execute(
                """INSERT INTO records(record_key, screen_id, frame_seq,
                   frame_sha256, primary_key_json, raw_values_json,
                   normalized_values_json, record_sha256, validation_status,
                   validation_errors_json, first_seen_at, last_seen_at,
                   first_run_id, last_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key,
                    record.get("screen_id"),
                    record.get("frame_seq"),
                    record.get("frame_sha256"),
                    _json(primary),
                    _json(raw),
                    _json(normalized),
                    record_sha,
                    validation_status,
                    _json(validation_errors),
                    now,
                    now,
                    run_id,
                    run_id,
                ),
            )
            self.connection.commit()
            return "inserted"
        if existing["record_sha256"] == record_sha and existing["validation_status"] == validation_status:
            self.connection.execute("UPDATE records SET last_seen_at=?, last_run_id=? WHERE record_key=?", (now, run_id, key))
            self.connection.commit()
            return "unchanged"
        self.connection.execute(
            """UPDATE records SET frame_seq=?, frame_sha256=?, primary_key_json=?,
               raw_values_json=?, normalized_values_json=?, record_sha256=?,
               validation_status=?, validation_errors_json=?, last_seen_at=?,
               last_run_id=? WHERE record_key=?""",
            (record.get("frame_seq"), record.get("frame_sha256"), _json(primary), _json(raw), _json(normalized), record_sha, validation_status, _json(validation_errors), now, run_id, key),
        )
        self.connection.commit()
        return "updated"

    def store_evidence(self, run_id: str, evidence: list[dict[str, Any]]) -> None:
        """Replace the run's rule-evidence rows; idempotent across reruns."""
        rows = [
            (
                run_id,
                str(item.get("rule_id")),
                str(item.get("kind")),
                str(item.get("screen_id")),
                item.get("record_key"),
                str(item.get("status")),
                _json(item),
            )
            for item in evidence
        ]
        self.connection.execute("DELETE FROM validation_evidence WHERE run_id=?", (run_id,))
        self.connection.executemany(
            """INSERT INTO validation_evidence(run_id, rule_id, rule_kind,
               screen_id, record_key, status, detail_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self.connection.commit()

    def evidence(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id is None:
            rows = self.connection.execute("SELECT * FROM validation_evidence ORDER BY evidence_id").fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM validation_evidence WHERE run_id=? ORDER BY evidence_id", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def count_records(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])

    def count_frames(self, run_id: str | None = None) -> int:
        if run_id is None:
            row = self.connection.execute("SELECT COUNT(*) FROM frames").fetchone()
        else:
            row = self.connection.execute("SELECT COUNT(*) FROM frames WHERE run_id=?", (run_id,)).fetchone()
        return int(row[0])

    def events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id is None:
            rows = self.connection.execute("SELECT * FROM events ORDER BY event_id").fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM events WHERE run_id=? ORDER BY event_id", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def records(self, screen_id: str | None = None) -> list[dict[str, Any]]:
        if screen_id is None:
            rows = self.connection.execute("SELECT * FROM records ORDER BY screen_id, record_key").fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM records WHERE screen_id=? ORDER BY record_key", (screen_id,)).fetchall()
        return [dict(row) for row in rows]

    def runs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM runs ORDER BY started_at, run_id").fetchall()
        return [dict(row) for row in rows]
