"""SQLite ledger: runs, generations, immutable record versions and events.

Publishing model
----------------
Every pipeline run owns exactly one *generation* (see
:mod:`termharvest.store_schema`). While a run is in progress its records are
staged into :table:`record_versions`, partitioned by ``generation_id``, so no
previously published row is ever overwritten. A run becomes externally
visible only through :meth:`Store.publish_generation`, which flips the
generation to ``published`` and moves the single-row :table:`ledger_state`
pointer in one transaction. Read-only snapshot queries (:meth:`Store.records`,
:meth:`Store.count_records`, ...) always follow that pointer.

A failed run calls :meth:`Store.abandon_generation`: its frames, events and
staged rows stay reviewable, but the pointer keeps addressing the previous
complete generation. When no generation has ever been published, snapshot
queries return an explicit "no published snapshot" state instead of partial
data.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .clock import Clock
from .errors import E_STORE_ERROR, HarvestError
from .screen import ScreenFrame
from .store_schema import MIGRATIONS, SCHEMA, SCHEMA_USER_VERSION

# Generation lifecycle states.
GEN_PREPARED = "prepared"
GEN_PUBLISHED = "published"
GEN_ABANDONED = "abandoned"

# Identity of a key in a generation relative to its parent generation.
REL_INSERTED = "inserted"
REL_UPDATED = "updated"
REL_UNCHANGED = "unchanged"
REL_RETIRED = "retired"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def generation_id_for(run_id: str) -> str:
    """Deterministic generation id derived from the run id."""
    return run_id.replace("run-", "gen-", 1) if run_id.startswith("run-") else f"gen-{run_id}"


class Store:
    def __init__(self, path: str | Path, *, auto_recover: bool = False) -> None:
        target = Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(target))
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.executescript(SCHEMA)
            self._apply_migrations()
            self.connection.commit()
            # Recovery is a coordinated two-side protocol (ledger + report
            # files). Callers that only want the ledger side reconciled can
            # opt in with auto_recover; the pipeline drives the full protocol
            # via recover_artifacts() + recover_stale_generations().
            if auto_recover:
                self.recover_stale_generations()
        except (OSError, sqlite3.Error) as exc:
            raise HarvestError(E_STORE_ERROR, "cannot open SQLite ledger", context={"file": str(target)}, cause=exc) from exc

    # ------------------------------------------------------------------ setup
    def _apply_migrations(self) -> None:
        """Upgrade ledger files created by earlier releases, once per version."""
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        for step in range(version, SCHEMA_USER_VERSION):
            for statement in MIGRATIONS.get(step + 1, ()):  # pragma: no branch - migration table is exhaustive
                try:
                    self.connection.execute(statement)
                except sqlite3.OperationalError as exc:  # column already exists on partially upgraded files
                    if "duplicate column" not in str(exc).lower():
                        raise
            self.connection.execute(f"PRAGMA user_version = {step + 1}")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -------------------------------------------------------------------- runs
    def start_run(self, run_id: str, session_id: str, mode: str, started_at: str) -> None:
        """Register a run and its (prepared) generation.

        Re-running the same deterministic ``run_id`` is idempotent: the run row
        is reset, stale staged rows for its generation are cleared and, if that
        generation was the current published one, the ledger pointer retreats to
        its parent for the duration of the rerun. Readers therefore only ever
        see a complete generation, never a half-restaged one.
        """
        gen_id = generation_id_for(run_id)
        try:
            with self.connection:  # single transaction
                self.connection.execute(
                    """INSERT INTO runs(run_id, session_id, mode, started_at, status, generation_id)
                       VALUES (?, ?, ?, ?, 'running', ?)
                       ON CONFLICT(run_id) DO UPDATE SET
                         started_at=excluded.started_at, status='running',
                         finished_at=NULL, error_code=NULL, generation_id=excluded.generation_id""",
                    (run_id, session_id, mode, started_at, gen_id),
                )
                existing = self.connection.execute(
                    "SELECT generation_id, status FROM generations WHERE run_id=?", (run_id,)
                ).fetchone()
                if existing is None:
                    parent = self.current_generation_id()
                    self.connection.execute(
                        """INSERT INTO generations(generation_id, run_id, session_id, mode,
                           parent_generation_id, status, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (gen_id, run_id, session_id, mode, parent, GEN_PREPARED, started_at),
                    )
                else:
                    self.connection.execute(
                        "DELETE FROM record_versions WHERE generation_id=?", (gen_id,)
                    )
                    self.connection.execute(
                        "DELETE FROM generation_key_changes WHERE generation_id=?", (gen_id,)
                    )
                    if existing["status"] == GEN_PUBLISHED:
                        row = self.connection.execute(
                            "SELECT parent_generation_id FROM generations WHERE generation_id=?",
                            (gen_id,),
                        ).fetchone()
                        self.connection.execute(
                            "UPDATE ledger_state SET current_generation_id=?, updated_at=? WHERE singleton_id=1",
                            (row["parent_generation_id"], started_at),
                        )
                    self.connection.execute(
                        """UPDATE generations SET status=?, parent_generation_id=?,
                           record_count=0, frame_count=0, error_count=0, records_sha256=NULL,
                           report_manifest_json='{}', published_at=NULL,
                           abandoned_at=NULL, abandon_reason=NULL, created_at=?
                           WHERE generation_id=?""",
                        (GEN_PREPARED, self.current_generation_id(), started_at, gen_id),
                    )
        except sqlite3.Error as exc:
            raise HarvestError(E_STORE_ERROR, "cannot start run", context={"run_id": run_id}, cause=exc) from exc

    def finish_run(self, run_id: str, *, status: str, finished_at: str, record_count: int, error_count: int, error_code: str | None, summary: dict[str, Any]) -> None:
        """Update run bookkeeping without moving the published pointer.

        Used by the failure path; successful runs finish atomically through
        :meth:`publish_generation`.
        """
        self.connection.execute(
            """UPDATE runs SET status=?, finished_at=?, record_count=?,
               error_count=?, error_code=?, summary_json=? WHERE run_id=?""",
            (status, finished_at, record_count, error_count, error_code, _json(summary), run_id),
        )
        self.connection.commit()

    def event(self, run_id: str, clock: Clock, level: str, event_type: str, *, code: str | None = None, message: str, context: dict[str, Any] | None = None) -> None:
        """Record an audit event; context must not contain secrets."""
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

    # ------------------------------------------------------------- staging rows
    @staticmethod
    def _record_sha(record: dict[str, Any]) -> str:
        normalized = record.get("normalized_values", {})
        raw = record.get("raw_values", {})
        primary = record.get("primary_key", {})
        payload = _json({"screen_id": record.get("screen_id"), "normalized": normalized, "raw": raw, "primary": primary})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _generation_for_run(self, run_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT generation_id, status FROM generations WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise HarvestError(E_STORE_ERROR, "run has no prepared generation", context={"run_id": run_id})
        return row

    def stage_record(self, record: dict[str, Any], *, validation_status: str, validation_errors: list[dict[str, Any]], run_id: str, clock: Clock) -> str:
        """Stage one record into the run's unpublished generation.

        Records are partitioned per generation, so staging never touches rows
        of a published generation. Re-appearance of the same ``record_key`` in
        the run (e.g. the same screen repeated across pages) is recognised
        deterministically:

        * ``inserted``  - first occurrence of the key in this generation;
        * ``unchanged`` - repeated occurrence with identical content/status;
        * ``updated``   - repeated occurrence whose content differs; the first
          occurrence remains the canonical row and the variant hash is kept.
        """
        key = str(record.get("record_key"))
        if not key:
            raise HarvestError(E_STORE_ERROR, "record is missing record_key", context={"screen_id": record.get("screen_id")})
        gen_row = self._generation_for_run(run_id)
        gen_id = gen_row["generation_id"]
        record_sha = self._record_sha(record)
        now = clock.now_iso()
        normalized = record.get("normalized_values", {})
        raw = record.get("raw_values", {})
        primary = record.get("primary_key", {})
        existing = self.connection.execute(
            "SELECT record_sha256, validation_status, occurrence_count, variant_sha_json FROM record_versions WHERE generation_id=? AND record_key=?",
            (gen_id, key),
        ).fetchone()
        if existing is None:
            self.connection.execute(
                """INSERT INTO record_versions(generation_id, record_key, screen_id,
                   frame_seq, frame_sha256, primary_key_json, raw_values_json,
                   normalized_values_json, record_sha256, validation_status,
                   validation_errors_json, first_seen_at, staged_at,
                   first_run_id, run_id, occurrence_seq, occurrence_count, variant_sha_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, '[]')""",
                (
                    gen_id,
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
            outcome = REL_INSERTED
        else:
            variants = json.loads(existing["variant_sha_json"])
            if existing["record_sha256"] == record_sha and existing["validation_status"] == validation_status:
                outcome = "unchanged"
            else:
                outcome = REL_UPDATED
                if record_sha not in variants:
                    variants.append(record_sha)
            self.connection.execute(
                """UPDATE record_versions SET occurrence_count=?, variant_sha_json=?,
                   staged_at=?, run_id=? WHERE generation_id=? AND record_key=?""",
                (existing["occurrence_count"] + 1, _json(variants), now, run_id, gen_id, key),
            )
        self.connection.commit()
        return outcome

    # Backwards-compatible alias for callers/tests using the historical name.
    def upsert_record(self, record: dict[str, Any], *, validation_status: str, validation_errors: list[dict[str, Any]], run_id: str, clock: Clock) -> str:
        return self.stage_record(
            record,
            validation_status=validation_status,
            validation_errors=validation_errors,
            run_id=run_id,
            clock=clock,
        )

    # ---------------------------------------------------------------- publishing
    def generation(self, generation_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM generations WHERE generation_id=?", (generation_id,)).fetchone()
        return dict(row) if row else None

    def generation_for_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM generations WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def generations(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM generations ORDER BY created_at, generation_id").fetchall()
        return [dict(row) for row in rows]

    def current_generation_id(self) -> str | None:
        row = self.connection.execute(
            "SELECT current_generation_id FROM ledger_state WHERE singleton_id=1"
        ).fetchone()
        return row[0] if row is not None else None

    def snapshot_status(self) -> dict[str, Any]:
        """State of the externally visible snapshot."""
        current = self.current_generation_id()
        return {
            "published": current is not None,
            "current_generation_id": current,
            "prepared_generations": [
                row["generation_id"]
                for row in self.connection.execute(
                    "SELECT generation_id FROM generations WHERE status=? ORDER BY created_at",
                    (GEN_PREPARED,),
                ).fetchall()
            ],
            "abandoned_generations": [
                row["generation_id"]
                for row in self.connection.execute(
                    "SELECT generation_id, status, abandon_reason FROM generations WHERE status=? ORDER BY created_at",
                    (GEN_ABANDONED,),
                ).fetchall()
            ],
        }

    def _canonical_rows(self, generation_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM record_versions WHERE generation_id=? AND occurrence_seq=0 ORDER BY screen_id, record_key",
            (generation_id,),
        ).fetchall()

    def generation_digest(self, run_id: str) -> tuple[str, int]:
        """SHA-256 over the canonical record set of a run's generation."""
        gen_id = self._generation_for_run(run_id)["generation_id"]
        return self._digest_for_generation(gen_id)

    def _digest_for_generation(self, gen_id: str) -> tuple[str, int]:
        rows = self._canonical_rows(gen_id)
        hasher = hashlib.sha256()
        for row in rows:
            line = "|".join(
                [row["record_key"], row["record_sha256"], row["validation_status"], str(row["occurrence_count"])]
            )
            hasher.update(line.encode("utf-8"))
            hasher.update(b"\n")
        return hasher.hexdigest(), len(rows)

    def _build_key_changes(self, gen_id: str, parent_id: str | None) -> list[dict[str, Any]]:
        current_rows = self._canonical_rows(gen_id)
        parent_by_key: dict[str, sqlite3.Row] = {}
        if parent_id is not None:
            for row in self._canonical_rows(parent_id):
                parent_by_key[row["record_key"]] = row
        changes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in current_rows:
            key = row["record_key"]
            seen.add(key)
            parent = parent_by_key.get(key)
            if parent is None:
                relation = REL_INSERTED
            elif parent["record_sha256"] == row["record_sha256"] and parent["validation_status"] == row["validation_status"]:
                relation = REL_UNCHANGED
            else:
                relation = REL_UPDATED
            changes.append(
                {
                    "generation_id": gen_id,
                    "record_key": key,
                    "screen_id": row["screen_id"],
                    "relation": relation,
                    "parent_generation_id": parent_id,
                    "parent_record_sha256": parent["record_sha256"] if parent else None,
                    "record_sha256": row["record_sha256"],
                    "occurrence_count": row["occurrence_count"],
                }
            )
        for key, parent in parent_by_key.items():
            if key in seen:
                continue
            # The key disappeared from this run: it must not leak into the new
            # generation's statistics, and its retirement is recorded explicitly.
            changes.append(
                {
                    "generation_id": gen_id,
                    "record_key": key,
                    "screen_id": parent["screen_id"],
                    "relation": REL_RETIRED,
                    "parent_generation_id": parent_id,
                    "parent_record_sha256": parent["record_sha256"],
                    "record_sha256": parent["record_sha256"],
                    "occurrence_count": 0,
                }
            )
        return changes

    def key_changes(self, generation_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM generation_key_changes WHERE generation_id=? ORDER BY relation, record_key",
            (generation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def publish_generation(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: str,
        record_count: int,
        frame_count: int,
        error_count: int,
        error_code: str | None,
        summary: dict[str, Any],
        report_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically publish a run's generation and switch the ledger pointer.

        The record set, the per-key identity relationship against the parent
        generation and the pointer switch all commit in one transaction. Either
        the whole generation is visible or the previous published generation
        remains untouched.
        """
        gen_row = self._generation_for_run(run_id)
        gen_id = gen_row["generation_id"]
        if gen_row["status"] != GEN_PREPARED:
            raise HarvestError(
                E_STORE_ERROR,
                "generation is not prepared for publish",
                context={"run_id": run_id, "generation_id": gen_id, "status": gen_row["status"]},
            )
        digest, canonical_count = self._digest_for_generation(gen_id)
        if summary.get("records_sha256") not in (None, digest):
            raise HarvestError(
                E_STORE_ERROR,
                "report record hash does not match ledger generation",
                context={"run_id": run_id, "report_hash": summary.get("records_sha256"), "ledger_hash": digest},
            )
        # An empty manifest means the ledger switch happens before the final
        # report bytes are staged; attach_report_manifest() records the exact
        # on-disk manifest once prepare_reports() finishes.
        if report_manifest and report_manifest.get("records_sha256") not in (None, digest):
            raise HarvestError(
                E_STORE_ERROR,
                "manifest hash does not match ledger generation",
                context={"run_id": run_id, "manifest_hash": report_manifest.get("records_sha256"), "ledger_hash": digest},
            )
        parent_id = self.connection.execute(
            "SELECT parent_generation_id FROM generations WHERE generation_id=?", (gen_id,)
        ).fetchone()["parent_generation_id"]
        changes = self._build_key_changes(gen_id, parent_id)
        try:
            with self.connection:
                for change in changes:
                    self.connection.execute(
                        """INSERT INTO generation_key_changes(generation_id, record_key,
                           screen_id, relation, parent_generation_id, parent_record_sha256,
                           record_sha256, occurrence_count)
                           VALUES (:generation_id, :record_key, :screen_id, :relation,
                           :parent_generation_id, :parent_record_sha256, :record_sha256,
                           :occurrence_count)""",
                        change,
                    )
                self.connection.execute(
                    """UPDATE runs SET status=?, finished_at=?, record_count=?,
                       error_count=?, error_code=?, summary_json=? WHERE run_id=?""",
                    (status, finished_at, record_count, error_count, error_code, _json(summary), run_id),
                )
                self.connection.execute(
                    """UPDATE generations SET status=?, published_at=?, record_count=?,
                       frame_count=?, error_count=?, records_sha256=?,
                       report_manifest_json=? WHERE generation_id=?""",
                    (GEN_PUBLISHED, finished_at, canonical_count, frame_count, error_count, digest, _json(report_manifest), gen_id),
                )
                self.connection.execute(
                    "UPDATE ledger_state SET current_generation_id=?, updated_at=? WHERE singleton_id=1",
                    (gen_id, finished_at),
                )
        except sqlite3.Error as exc:
            raise HarvestError(E_STORE_ERROR, "cannot publish generation", context={"run_id": run_id}, cause=exc) from exc
        return {"generation_id": gen_id, "records_sha256": digest, "key_changes": changes}

    def attach_report_manifest(self, generation_id: str, manifest: dict[str, Any]) -> None:
        """Record the exact manifest covering the published report bytes."""
        row = self.connection.execute(
            "SELECT records_sha256 FROM generations WHERE generation_id=?", (generation_id,)
        ).fetchone()
        if row is None:
            raise HarvestError(E_STORE_ERROR, "unknown generation", context={"generation_id": generation_id})
        if manifest.get("records_sha256") != row["records_sha256"]:
            raise HarvestError(
                E_STORE_ERROR,
                "manifest hash does not match published generation",
                {"generation_id": generation_id, "manifest_hash": manifest.get("records_sha256"), "ledger_hash": row["records_sha256"]},
            )
        self.connection.execute(
            "UPDATE generations SET report_manifest_json=? WHERE generation_id=?",
            (_json(manifest), generation_id),
        )
        self.connection.commit()

    def abandon_generation(self, run_id: str, *, reason: str, abandoned_at: str, retreat_pointer: bool = False) -> dict[str, Any]:
        """Mark a run's generation abandoned.

        By default the ledger pointer is untouched (the generation was never
        published). When a post-publish step fails and the pointer already
        addresses this generation, pass ``retreat_pointer=True`` to atomically
        move it back to the parent generation so readers never resolve an
        abandoned snapshot.
        """
        gen_row = self._generation_for_run(run_id)
        gen_id = gen_row["generation_id"]
        with self.connection:
            self.connection.execute(
                "UPDATE generations SET status=?, abandoned_at=?, abandon_reason=? WHERE generation_id=?",
                (GEN_ABANDONED, abandoned_at, reason, gen_id),
            )
            if retreat_pointer:
                parent_id = self.connection.execute(
                    "SELECT parent_generation_id FROM generations WHERE generation_id=?", (gen_id,)
                ).fetchone()["parent_generation_id"]
                self.connection.execute(
                    "UPDATE ledger_state SET current_generation_id=?, updated_at=? WHERE singleton_id=1",
                    (parent_id, abandoned_at),
                )
        return {"generation_id": gen_id, "reason": reason, "current_generation_id": self.current_generation_id()}

    def recover_stale_generations(self) -> list[dict[str, Any]]:
        """Abandon generations left in ``prepared`` by a dead writer process.

        A new Store opening the ledger means the previous single writer is no
        longer active, so every prepared generation is an interrupted publish.
        Their diagnostic rows stay in place; the pointer is never changed here.
        """
        actions: list[dict[str, Any]] = []
        rows = self.connection.execute(
            """SELECT g.generation_id, g.run_id, g.abandon_reason, r.status AS run_status
               FROM generations g LEFT JOIN runs r ON r.run_id = g.run_id
               WHERE g.status=?""",
            (GEN_PREPARED,),
        ).fetchall()
        for row in rows:
            reason = f"interrupted before publish (run status={row['run_status'] or 'unknown'})"
            self.connection.execute(
                "UPDATE generations SET status=?, abandoned_at=?, abandon_reason=? WHERE generation_id=?",
                (GEN_ABANDONED, None, reason, row["generation_id"]),
            )
            actions.append({"generation_id": row["generation_id"], "run_id": row["run_id"], "action": "abandoned", "reason": reason})
        self.connection.commit()
        return actions

    def republish_recovered_generation(self, generation_id: str, *, finished_at: str, report_manifest: dict[str, Any]) -> dict[str, Any]:
        """Finish publishing a generation whose files are complete but whose
        ledger transaction never committed (crash between file finalize and
        commit). Staged rows and manifest are verified before the switch.
        """
        gen_row = self.connection.execute(
            "SELECT * FROM generations WHERE generation_id=?", (generation_id,)
        ).fetchone()
        if gen_row is None:
            raise HarvestError(E_STORE_ERROR, "unknown generation", context={"generation_id": generation_id})
        if gen_row["status"] != GEN_PREPARED:
            raise HarvestError(
                E_STORE_ERROR,
                "generation cannot be republished",
                context={"generation_id": generation_id, "status": gen_row["status"]},
            )
        digest, canonical_count = self._digest_for_generation(generation_id)
        manifest_hash = report_manifest.get("records_sha256")
        if manifest_hash != digest:
            raise HarvestError(
                E_STORE_ERROR,
                "recovered manifest hash does not match staged records",
                {"generation_id": generation_id, "manifest_hash": manifest_hash, "ledger_hash": digest},
            )
        changes = self._build_key_changes(generation_id, gen_row["parent_generation_id"])
        with self.connection:
            for change in changes:
                self.connection.execute(
                    """INSERT OR REPLACE INTO generation_key_changes(generation_id, record_key,
                       screen_id, relation, parent_generation_id, parent_record_sha256,
                       record_sha256, occurrence_count)
                       VALUES (:generation_id, :record_key, :screen_id, :relation,
                       :parent_generation_id, :parent_record_sha256, :record_sha256,
                       :occurrence_count)""",
                    change,
                )
            self.connection.execute(
                """UPDATE runs SET status='success', finished_at=?, record_count=?,
                   error_count=0, error_code=NULL WHERE run_id=?""",
                (finished_at, canonical_count, gen_row["run_id"]),
            )
            self.connection.execute(
                """UPDATE generations SET status=?, published_at=?, record_count=?,
                   records_sha256=?, report_manifest_json=? WHERE generation_id=?""",
                (GEN_PUBLISHED, finished_at, canonical_count, digest, _json(report_manifest), generation_id),
            )
            self.connection.execute(
                "UPDATE ledger_state SET current_generation_id=?, updated_at=? WHERE singleton_id=1",
                (generation_id, finished_at),
            )
        return {"generation_id": generation_id, "records_sha256": digest, "key_changes": changes}

    # ------------------------------------------------------------- legacy import
    def import_legacy_records(self) -> dict[str, Any] | None:
        """Publish pre-generation ledger rows (``records`` table) as one
        immutable legacy generation so old successful harvests stay readable.
        """
        if self.current_generation_id() is not None:
            return None
        legacy_rows = self.connection.execute("SELECT * FROM records ORDER BY screen_id, record_key").fetchall()
        if not legacy_rows:
            return None
        gen_id = "gen-legacy"
        created_at = min(row["first_seen_at"] for row in legacy_rows)
        with self.connection:
            self.connection.execute(
                """INSERT INTO generations(generation_id, run_id, session_id, mode,
                   parent_generation_id, status, record_count, created_at, published_at)
                   VALUES (?, ?, 'legacy', 'legacy', NULL, ?, ?, ?, ?)""",
                (gen_id, "run-legacy", GEN_PUBLISHED, len(legacy_rows), created_at, created_at),
            )
            for row in legacy_rows:
                self.connection.execute(
                    """INSERT INTO record_versions(generation_id, record_key, screen_id,
                       frame_seq, frame_sha256, primary_key_json, raw_values_json,
                       normalized_values_json, record_sha256, validation_status,
                       validation_errors_json, first_seen_at, staged_at,
                       first_run_id, run_id, occurrence_seq, occurrence_count, variant_sha_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, '[]')""",
                    (
                        gen_id,
                        row["record_key"],
                        row["screen_id"],
                        row["frame_seq"],
                        row["frame_sha256"],
                        row["primary_key_json"],
                        row["raw_values_json"],
                        row["normalized_values_json"],
                        row["record_sha256"],
                        row["validation_status"],
                        row["validation_errors_json"],
                        row["first_seen_at"],
                        row["last_seen_at"],
                        row["first_run_id"],
                        row["last_run_id"],
                    ),
                )
            digest, count = self._digest_for_generation(gen_id)
            self.connection.execute(
                "UPDATE generations SET records_sha256=? WHERE generation_id=?", (digest, gen_id)
            )
            self.connection.execute(
                "UPDATE ledger_state SET current_generation_id=?, updated_at=? WHERE singleton_id=1",
                (gen_id, created_at),
            )
        return {"generation_id": gen_id, "record_count": count, "records_sha256": digest}

    # ------------------------------------------------------------- read-only API
    @staticmethod
    def _version_row_to_record(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        # Present the historical column names so existing readers keep working.
        data["last_seen_at"] = data.pop("staged_at")
        data["last_run_id"] = data.pop("run_id")
        return data

    def records(self, screen_id: str | None = None, *, generation_id: str | None = None) -> list[dict[str, Any]]:
        """Return canonical records of the current published generation.

        With ``generation_id`` given, any stored generation can be reviewed.
        Returns an empty list when no generation is published; callers can use
        :meth:`snapshot_status` to distinguish "no snapshot" from "empty run".
        """
        gen_id = generation_id or self.current_generation_id()
        if gen_id is None:
            return []
        if screen_id is None:
            rows = self.connection.execute(
                "SELECT * FROM record_versions WHERE generation_id=? AND occurrence_seq=0 ORDER BY screen_id, record_key",
                (gen_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM record_versions WHERE generation_id=? AND occurrence_seq=0 AND screen_id=? ORDER BY record_key",
                (gen_id, screen_id),
            ).fetchall()
        return [self._version_row_to_record(row) for row in rows]

    def count_records(self, *, generation_id: str | None = None) -> int:
        gen_id = generation_id or self.current_generation_id()
        if gen_id is None:
            return 0
        row = self.connection.execute(
            "SELECT COUNT(*) FROM record_versions WHERE generation_id=? AND occurrence_seq=0",
            (gen_id,),
        ).fetchone()
        return int(row[0])

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

    def runs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM runs ORDER BY started_at, run_id").fetchall()
        return [dict(row) for row in rows]
