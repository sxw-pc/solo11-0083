"""End-to-end orchestration: collect, extract, validate, stage and publish.

A run publishes one *generation* atomically. The release order is:

1. collect frames, extract and validate (all in-memory);
2. stage every record into the run's private generation in the ledger and
   write every report file into a private staging directory (prepare);
3. one SQLite transaction publishes the generation and moves the ledger
   pointer, after verifying the report record hash matches the ledger hash;
4. the staged directory is renamed into place and the file-system pointer is
   switched (publish).

Any failure before step 3 leaves the previous published generation as the
sole effective snapshot; diagnostic evidence, frames and events of the failed
run remain stored and its generation is marked abandoned. Report/ledger
recovery on the next open is handled by
:func:`termharvest.report.recover_artifacts` and
:meth:`termharvest.store.Store.recover_stale_generations`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .cassette import write_cassette
from .clock import Clock, SystemClock
from .config_types import ScreenContract, SessionContract
from .errors import E_REPORT_ERROR, HarvestError
from .logging_util import safe_context
from .pipeline_steps import _collect, _extract_all, make_run_id
from .report import (
    build_report_payload,
    prepare_reports,
    publish_reports,
    recover_artifacts,
    write_failure_report,
)
from .store import Store, generation_id_for
from .validate import ValidationResult, validate_records
from .validate_config import ValidationConfig


def _failure_stats() -> dict[str, Any]:
    return {"records_in": 0, "records_valid": 0, "records_invalid": 0, "error_count": 0, "error_codes": []}


def _record_sha256(record: dict[str, Any]) -> str:
    import hashlib
    import json

    payload = json.dumps(
        {
            "screen_id": record.get("screen_id"),
            "normalized": record.get("normalized_values", {}),
            "raw": record.get("raw_values", {}),
            "primary": record.get("primary_key", {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_report_records(records: list[dict[str, Any]], duplicates: dict[str, int]) -> list[dict[str, Any]]:
    """Attach deterministic hashes and occurrence counts for the report."""
    canonical: list[dict[str, Any]] = []
    for record in records:
        row = dict(record)
        row["record_sha256"] = _record_sha256(record)
        row["occurrence_count"] = duplicates.get(record["record_key"], 1)
        canonical.append(row)
    return canonical


def run_pipeline(
    *,
    session: SessionContract,
    screens: dict[str, ScreenContract],
    validation_config: ValidationConfig,
    store: Store,
    output_dir: str | Path,
    clock: Clock | None = None,
    mode: str = "live",
    cassette_path: str | Path | None = None,
    evidence_dir: str | Path | None = None,
    host_override: str | None = None,
    port_override: int | None = None,
    record_cassette: str | Path | None = None,
    logger: Any = None,
) -> dict[str, Any]:
    clock = clock or SystemClock()
    output = Path(output_dir)
    evidence = Path(evidence_dir) if evidence_dir else output / "evidence"
    effective = session
    if host_override is not None or port_override is not None:
        effective = replace(session, target_host=host_override if host_override is not None else session.target_host, target_port=port_override if port_override is not None else session.target_port)
    started_at = clock.now_iso()
    run_id = make_run_id(effective, mode, started_at)
    gen_id = generation_id_for(run_id)

    # Coordinated recovery before opening a new run: complete-but-unpublished
    # artifacts are republished first (cut point 2), half-written staging
    # directories are quarantined and their generations abandoned (cut point
    # 1); any prepared generation left without artifacts is then abandoned by
    # the ledger-side sweep. Cut point 3 (pointer missing after DB switch) is
    # repaired inside recover_artifacts as well.
    for action in recover_artifacts(output, store):
        if logger:
            logger.info("event=recovery_action action=%s generation=%s", action.get("action"), action.get("generation_id"))
    for action in store.recover_stale_generations():
        if logger:
            logger.info("event=recovery_action action=%s generation=%s", action.get("action"), action.get("generation_id"))

    store.start_run(run_id, effective.session_id, mode, started_at)

    def sink(level: str, event_type: str, code: str | None, message: str, context: dict[str, Any]) -> None:
        store.event(run_id, clock, level, event_type, code=code, message=message, context=safe_context(**context))

    store.event(
        run_id,
        clock,
        "info",
        "run_started",
        message="pipeline run started",
        context={"session_id": effective.session_id, "mode": mode, "target_host": effective.target_host, "target_port": effective.target_port, "generation_id": gen_id},
    )
    try:
        frames = _collect(effective, mode=mode, cassette_path=cassette_path, event_sink=sink, clock=clock, evidence_dir=evidence, logger=logger)
        for item in frames:
            store.store_frame(run_id, item.frame_seq, item.expected_screen, item.step_id, item.frame, clock.now_iso())
            sink(
                "info",
                "frame_stored",
                None,
                "screen frame stored with hash",
                {"frame_seq": item.frame_seq, "screen_id": item.expected_screen, "step_id": item.step_id, "frame_sha256": item.frame.sha256},
            )
        if record_cassette is not None:
            write_cassette(
                record_cassette,
                session_id=effective.session_id,
                frames=[(item.frame_seq, item.step_id, item.expected_screen, item.frame) for item in frames],
                captured_by="live" if mode == "live" else "replay",
            )
        extractions, page_details = _extract_all(frames, screens, effective)
        records = [record for extraction in extractions for record in extraction.records]
        facts = {extraction.screen_id: extraction.facts for extraction in extractions}
        validation = validate_records(records, validation_config, facts_by_screen=facts)
        invalid_keys = {item["record_key"] for item in validation.errors if item.get("record_key")}

        # Explicit within-run identity: repeated keys across pages are counted
        # against one identity instead of leaking into statistics twice.
        duplicates: dict[str, int] = {}
        seen_keys: set[str] = set()
        for record in records:
            key = record.get("record_key")
            if key in seen_keys:
                duplicates[key] = duplicates.get(key, 1) + 1
            seen_keys.add(key)

        for record in records:
            record["validation_status"] = "invalid" if record.get("record_key") in invalid_keys else "valid"
            per_record_errors = [item for item in validation.errors if item.get("record_key") == record.get("record_key")]
            store.stage_record(record, validation_status=record["validation_status"], validation_errors=per_record_errors, run_id=run_id, clock=clock)

        status = "success" if validation.ok else "failed"
        finished_at = clock.now_iso()
        ledger_digest, _ = store.generation_digest(run_id)
        parent_generation = store.generation(gen_id)["parent_generation_id"]
        report_records = _canonical_report_records(records, duplicates)
        payload = build_report_payload(
            run_id=run_id,
            session_id=effective.session_id,
            mode=mode,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            frames=frames,
            extractions=extractions,
            page_details=page_details,
            validation=validation,
            records=report_records,
            errors=validation.errors,
            generation_id=gen_id,
            parent_generation_id=parent_generation,
            records_sha256=ledger_digest,
        )

        if not validation.ok:
            # Validation verdict is part of the generation contract: a failed
            # validation run does not publish; the previous generation stays
            # effective while this run's diagnostics remain reviewable.
            store.abandon_generation(run_id, reason=f"validation failed: {validation.errors[0]['code']}", abandoned_at=finished_at)
            store.finish_run(
                run_id,
                status="failed",
                finished_at=finished_at,
                record_count=len(records),
                error_count=len(validation.errors),
                error_code=validation.errors[0]["code"],
                summary=payload["summary"],
            )
            failure_files = write_failure_report(output, payload, run_id=run_id)
            sink("error", "run_completed", validation.errors[0]["code"], "pipeline run completed with validation failures", {"status": status, "record_count": len(records), "generation_id": gen_id})
            return {"run_id": run_id, "generation_id": gen_id, "payload": payload, **failure_files, "published": False}

        # Single ledger transaction: record set + per-key identity + pointer.
        # The manifest is attached afterwards once the final payload (including
        # the ledger-verified key relations) has been rendered, so the manifest
        # stored in the ledger always matches the bytes on disk.
        published = store.publish_generation(
            run_id,
            status=status,
            finished_at=finished_at,
            record_count=len(records),
            frame_count=len(frames),
            error_count=len(validation.errors),
            error_code=None,
            summary=payload["summary"],
            report_manifest={},
        )

        # Re-render with the ledger-verified relations; the digest cannot change
        # because key_changes only describe identity, not record content.
        payload = build_report_payload(
            run_id=run_id,
            session_id=effective.session_id,
            mode=mode,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            frames=frames,
            extractions=extractions,
            page_details=page_details,
            validation=validation,
            records=report_records,
            errors=validation.errors,
            generation_id=gen_id,
            parent_generation_id=parent_generation,
            records_sha256=published["records_sha256"],
            key_changes=published["key_changes"],
        )
        if payload["summary"]["records_sha256"] != published["records_sha256"]:
            raise HarvestError(
                E_REPORT_ERROR,
                "report record hash diverged from published generation",
                context={"generation_id": gen_id, "ledger_hash": published["records_sha256"], "report_hash": payload["summary"]["records_sha256"]},
            )

        # Prepare all artifacts (manifest written last) before any file-system
        # switch: cut point 1 is fully inside this staging directory.
        try:
            prepared = prepare_reports(output, payload, generation_id=gen_id)
        except OSError as exc:
            # The ledger transaction already committed; retreat the pointer to
            # the parent generation so no reader resolves an abandoned snapshot.
            store.abandon_generation(run_id, reason="report staging failed", abandoned_at=clock.now_iso(), retreat_pointer=True)
            raise HarvestError(E_REPORT_ERROR, "cannot stage report artifacts", context={"generation_id": gen_id}, cause=exc) from exc
        # Persist the exact manifest that covers the on-disk bytes.
        store.attach_report_manifest(gen_id, prepared["manifest"])
        sink("info", "run_completed", None, "pipeline run completed", {"status": status, "record_count": len(records), "generation_id": gen_id, "records_sha256": published["records_sha256"]})

        # File-system publish after the committed ledger switch (cut point 3
        # after this call is repaired by recover_artifacts on the next open).
        written = publish_reports(output, generation_id=gen_id)
        return {
            "run_id": run_id,
            "generation_id": gen_id,
            "payload": payload,
            "output_dir": written["output_dir"],
            "files": written["files"],
            "summary": payload["summary"],
            "manifest": prepared["manifest"],
            "key_changes": published["key_changes"],
            "published": True,
        }
    except HarvestError as exc:
        finished_at = clock.now_iso()
        sink("error", "run_failed", exc.code, exc.message, {"context": exc.context})
        failure_error = {"code": exc.code, "rule_id": "pipeline", "screen_id": exc.context.get("screen_id"), "record_key": None, "field": None, "message": exc.message}
        empty = ValidationResult(errors=[], stats=_failure_stats())
        payload = build_report_payload(
            run_id=run_id,
            session_id=effective.session_id,
            mode=mode,
            status="failed",
            started_at=started_at,
            finished_at=finished_at,
            frames=[],
            extractions=[],
            page_details=[],
            validation=empty,
            records=[],
            errors=[failure_error],
            generation_id=gen_id,
            parent_generation_id=store.generation(gen_id)["parent_generation_id"] if store.generation(gen_id) else None,
        )
        # Staged partial records must never become the effective snapshot.
        # A generation may already have been abandoned (e.g. staging failure
        # retreated the pointer); only touch it again if it is still prepared.
        generation = store.generation(gen_id)
        if generation is not None and generation["status"] == "prepared":
            store.abandon_generation(run_id, reason=exc.code, abandoned_at=finished_at)
        store.finish_run(run_id, status="failed", finished_at=finished_at, record_count=0, error_count=1, error_code=exc.code, summary=payload["summary"])
        write_failure_report(output, payload, run_id=run_id)
        raise
