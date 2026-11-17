"""End-to-end orchestration: collect, extract, validate, store and report."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .cassette import compute_contract_digest, load_cassette, write_cassette
from .clock import Clock, SystemClock
from .config_types import ScreenContract, SessionContract
from .errors import E_TRUST_REQUIRED, E_TRUST_UNSIGNED, HarvestError
from .logging_util import safe_context
from .pipeline_steps import _collect, _extract_all, make_run_id
from .report import build_report_payload, write_reports
from .store import Store
from .trust import (
    POLICY_ANY_TRUSTED,
    TrustStore,
    normalize_policy,
    not_applicable_provenance,
    rejected_provenance,
    unauthenticated_provenance,
    verification_error,
    verify_loaded_cassette,
)
from .validate import ValidationResult, validate_records
from .validate_config import ValidationConfig

def _failure_stats() -> dict[str, Any]:
    return {"records_in": 0, "records_valid": 0, "records_invalid": 0, "error_count": 0, "error_codes": []}

def _verify_replay_cassette(
    *,
    cassette_path: str | Path,
    session: SessionContract,
    contract_digest: str,
    trust_store: TrustStore | None,
    trust_policy: str | None,
    require_trusted: bool,
    clock: Clock,
    accept: bool = True,
) -> tuple[Any, dict[str, Any], Any]:
    """Structure/frame hashes first, then whole-package signature verification.

    Nothing is extracted until this returns.  In strict mode, unsigned v1 data
    and any package that cannot be attributed to a trusted recorder are
    refused outright; in legacy mode they remain replayable but are reported as
    ``unauthenticated``.

    ``accept=False`` verifies without advancing anti-rollback state; the caller
    passes ``accept=True`` only after extraction has succeeded.  The third
    return value is the :class:`TrustVerification` (or ``None``).
    """
    cassette = load_cassette(cassette_path)
    policy = normalize_policy(trust_policy) if trust_policy else None

    if not cassette.is_signed:
        if require_trusted:
            raise HarvestError(
                E_TRUST_UNSIGNED,
                "strict trusted replay refuses an unsigned v1 cassette",
                context={"cassette_version": cassette.version, "file": cassette.source_file},
            )
        return cassette, unauthenticated_provenance(cassette), None

    if trust_store is None:
        if require_trusted:
            raise HarvestError(
                E_TRUST_REQUIRED,
                "strict trusted replay requires a configured trust store",
                context={"cassette_version": cassette.version, "file": cassette.source_file},
            )
        return cassette, unauthenticated_provenance(
            cassette,
            reason="signed package was not verified because no trust store was configured",
        ), None

    verification = verify_loaded_cassette(
        cassette,
        trust_store=trust_store,
        policy=policy or POLICY_ANY_TRUSTED,
        session_id=session.session_id,
        contract_digest=contract_digest,
        clock=clock,
        accept=accept,
    )
    if not verification.trusted:
        raise verification_error(verification)
    return cassette, verification.to_provenance(), verification

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
    trust_store: TrustStore | None = None,
    trust_policy: str | None = None,
    require_trusted: bool = False,
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
    provenance = not_applicable_provenance()
    cassette = None
    pending_verification = None
    contract_digest = compute_contract_digest(effective, screens)

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
        if mode == "replay":
            if cassette_path is None:
                raise HarvestError(E_TRUST_REQUIRED, "replay mode requires a cassette path")
            try:
                cassette, provenance, pending_verification = _verify_replay_cassette(
                    cassette_path=cassette_path,
                    session=effective,
                    contract_digest=contract_digest,
                    trust_store=trust_store,
                    trust_policy=trust_policy,
                    require_trusted=require_trusted,
                    clock=clock,
                    accept=False,
                )
            except HarvestError as exc:
                provenance = rejected_provenance(
                    error_code=exc.code,
                    reason=exc.message,
                    policy=normalize_policy(trust_policy) if trust_policy else None,
                )
                raise
            store.record_provenance(run_id, provenance)
            sink(
                "info",
                "replay_verified",
                None,
                "cassette structure, frame hashes and package signatures verified",
                {"trust_status": provenance["trust_status"], "policy": provenance["policy"], "record_seq": provenance["record_seq"]},
            )
        frames = _collect(
            effective,
            mode=mode,
            cassette_path=cassette_path,
            event_sink=sink,
            clock=clock,
            evidence_dir=evidence,
            logger=logger,
            cassette=cassette,
            trust_provenance=provenance if mode == "replay" else None,
        )
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
        if pending_verification is not None:
            # Full extraction succeeded: now atomically re-verify and advance
            # the per-session anti-rollback sequence.  A failure here (for
            # example a concurrent higher-sequence recording) rolls back and
            # aborts the replay, leaving the stored sequence untouched.
            accepted = trust_store.verify_and_accept(
                cassette,
                policy=trust_policy or POLICY_ANY_TRUSTED,
                session_id=effective.session_id,
                contract_digest=contract_digest,
                clock=clock,
                accept=True,
            )
            if not accepted.trusted:
                raise verification_error(accepted)
            provenance = accepted.to_provenance()
            store.record_provenance(run_id, provenance)
        records = [record for extraction in extractions for record in extraction.records]
        facts = {extraction.screen_id: extraction.facts for extraction in extractions}
        validation = validate_records(records, validation_config, facts_by_screen=facts)
        invalid_keys = {item["record_key"] for item in validation.errors if item.get("record_key")}
        for record in records:
            record["validation_status"] = "invalid" if record.get("record_key") in invalid_keys else "valid"
            per_record_errors = [item for item in validation.errors if item.get("record_key") == record.get("record_key")]
            store.upsert_record(record, validation_status=record["validation_status"], validation_errors=per_record_errors, run_id=run_id, clock=clock)
        status = "success" if validation.ok else "failed"
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
            provenance=provenance,
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
        sink("info" if validation.ok else "error", "run_completed", None if validation.ok else validation.errors[0]["code"], "pipeline run completed", {"status": status, "record_count": len(records)})
        written = write_reports(output, payload)
        return {"run_id": run_id, "payload": payload, **written}
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
            provenance=provenance,
        )
        if mode == "replay":
            try:
                store.record_provenance(run_id, provenance)
            except HarvestError:
                pass
        store.finish_run(run_id, status="failed", finished_at=finished_at, record_count=0, error_count=1, error_code=exc.code, summary=payload["summary"])
        write_reports(output, payload)
        raise
