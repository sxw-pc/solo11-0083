"""End-to-end orchestration: collect, extract, validate, store and report."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from .cassette import write_cassette
from .clock import Clock, SystemClock
from .config_types import ScreenContract, SessionContract
from .errors import HarvestError
from .extract import Extraction
from .logging_util import safe_context
from .pipeline_steps import _collect, _extract_all, make_run_id
from .report import build_report_payload, write_reports
from .store import Store
from .validate import ValidationResult, validate_records
from .validate_config import ValidationConfig

def _failure_stats() -> dict[str, Any]:
    return {"records_in": 0, "records_valid": 0, "records_invalid": 0, "error_count": 0, "error_codes": []}

# Stop reasons under which a list screen's records are proven complete.
# Anything else (max_pages, frames_exhausted) means the list may be
# truncated, so sum_equals must stay unknown rather than sum a partial list.
COMPLETE_STOP_REASONS = frozenset({"single_frame", "stop_marker", "empty_page", "repeated_page"})

def _screen_completeness(extractions: list[Extraction], page_details: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    pages_by_screen = {item["screen_id"]: item for item in page_details}
    completeness: dict[str, dict[str, Any]] = {}
    for extraction in extractions:
        page = pages_by_screen.get(extraction.screen_id)
        if page is None:
            completeness[extraction.screen_id] = {"complete": True, "stop_reason": "single_frame", "page_count": 1}
        else:
            completeness[extraction.screen_id] = {
                "complete": page["stop_reason"] in COMPLETE_STOP_REASONS,
                "stop_reason": page["stop_reason"],
                "page_count": page["page_count"],
            }
    return completeness

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
    store.start_run(run_id, effective.session_id, mode, started_at)

    def sink(level: str, event_type: str, code: str | None, message: str, context: dict[str, Any]) -> None:
        store.event(run_id, clock, level, event_type, code=code, message=message, context=safe_context(**context))

    store.event(
        run_id,
        clock,
        "info",
        "run_started",
        message="pipeline run started",
        context={"session_id": effective.session_id, "mode": mode, "target_host": effective.target_host, "target_port": effective.target_port},
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
        completeness = _screen_completeness(extractions, page_details)
        validation = validate_records(records, validation_config, facts_by_screen=facts, screen_completeness=completeness)
        invalid_keys = {item["record_key"] for item in validation.errors if item.get("record_key")}
        unknown_keys = {item["record_key"] for item in validation.unknowns if item.get("record_key")}
        for record in records:
            record_key = record.get("record_key")
            if record_key in invalid_keys:
                record["validation_status"] = "invalid"
            elif record_key in unknown_keys:
                record["validation_status"] = "unknown"
            else:
                record["validation_status"] = "valid"
            per_record_errors = [item for item in validation.errors if item.get("record_key") == record_key]
            store.upsert_record(record, validation_status=record["validation_status"], validation_errors=per_record_errors, run_id=run_id, clock=clock)
        store.store_evidence(run_id, validation.evidence)
        status = validation.run_status()
        finished_at = clock.now_iso()
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
            records=records,
            errors=validation.errors,
        )
        store.finish_run(
            run_id,
            status=status,
            finished_at=finished_at,
            record_count=len(records),
            error_count=len(validation.errors),
            error_code=validation.errors[0]["code"] if validation.errors else None,
            summary=payload["summary"],
        )
        sink(
            "info" if validation.ok else ("error" if validation.errors else "warning"),
            "run_completed",
            validation.errors[0]["code"] if validation.errors else None,
            "pipeline run completed",
            {"status": status, "record_count": len(records), "evidence": payload["summary"]["evidence"]},
        )
        written = write_reports(output, payload)
        return {"run_id": run_id, "payload": payload, **written}
    except HarvestError as exc:
        finished_at = clock.now_iso()
        sink("error", "run_failed", exc.code, exc.message, {"context": exc.context})
        failure_error = {"code": exc.code, "rule_id": "pipeline", "screen_id": exc.context.get("screen_id"), "record_key": None, "field": None, "message": exc.message}
        empty = ValidationResult(errors=[], stats=_failure_stats(), policy=validation_config.policy)
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
        )
        store.finish_run(run_id, status="failed", finished_at=finished_at, record_count=0, error_count=1, error_code=exc.code, summary=payload["summary"])
        write_reports(output, payload)
        raise
