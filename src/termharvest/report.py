"""Deterministic report payload construction and atomic artifact publishing.

Artifact release protocol
-------------------------
SQLite commits and report files cannot share a transaction, so the report
artifacts follow an explicit prepare/publish/recover protocol:

1. **prepare** - every report file is written once into a private staging
   directory ``<out>/_staging/<generation_id>/`` and a ``manifest.json`` records
   the SHA-256 of every file plus the hash of the record set. Nothing at the
   public location is touched yet.
2. **publish** - the staging directory is atomically renamed to
   ``<out>/generations/<generation_id>/`` and ``current.json`` (the file-system
   pointer, mirrored by a best-effort ``current`` symlink) is replaced
   atomically. The caller publishes the SQLite generation first and only then
   publishes the pointer, so a reader always sees a complete old or complete new
   generation.
3. **recover** - :func:`recover_artifacts` reconciles staging directories and
   generations on the next open. Half-written staging directories are quarantined
   under ``_recovered/`` (no manual cleanup needed); complete-but-unpublished
   directories can be republished; a missing pointer after a database switch is
   rebuilt from the published generation directory.

Failed runs never overwrite the public pointer: their diagnostic report lands
under ``<out>/failed/<run_id>/``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any

from .capture import CapturedFrame
from .extract import Extraction
from .report_render import render_html, render_markdown
from .validate import ValidationResult

REPORT_FILES = ["report.html", "report.json", "report.md", "report_summary.json", "records.csv"]
MANIFEST_NAME = "manifest.json"
POINTER_NAME = "current.json"
STAGING_DIRNAME = "_staging"
RECOVERED_DIRNAME = "_recovered"
GENERATIONS_DIRNAME = "generations"
FAILED_DIRNAME = "failed"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def records_digest(records: list[dict[str, Any]]) -> str:
    """Stable SHA-256 over the exact record set embedded in the report."""
    hasher = hashlib.sha256()
    for record in records:
        line = "|".join(
            [
                str(record["record_key"]),
                str(record.get("record_sha256", "")),
                str(record.get("validation_status", "valid")),
                str(record.get("occurrence_count", 1)),
            ]
        )
        hasher.update(line.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def build_report_payload(
    *,
    run_id: str,
    session_id: str,
    mode: str,
    status: str,
    started_at: str,
    finished_at: str,
    frames: list[CapturedFrame],
    extractions: list[Extraction],
    page_details: list[dict[str, Any]],
    validation: ValidationResult,
    records: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    generation_id: str,
    parent_generation_id: str | None = None,
    records_sha256: str | None = None,
    key_changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    frames_by_screen: dict[str, int] = {}
    for item in frames:
        frames_by_screen[item.expected_screen] = frames_by_screen.get(item.expected_screen, 0) + 1
    pages_by_screen = {item["screen_id"]: item for item in page_details}
    screen_rows = []
    for extraction in extractions:
        page = pages_by_screen.get(extraction.screen_id)
        screen_rows.append(
            {
                "screen_id": extraction.screen_id,
                "frame_count": frames_by_screen.get(extraction.screen_id, 0),
                "record_count": len(extraction.records),
                "page_count": int(page["page_count"]) if page else 1,
                "stop_reason": page["stop_reason"] if page else "single_frame",
            }
        )
    records_by_screen: dict[str, int] = {}
    for record in records:
        name = str(record["screen_id"])
        records_by_screen[name] = records_by_screen.get(name, 0) + 1
    sensitive = sorted({name for extraction in extractions for name in extraction.sensitive_skipped})
    digest = records_sha256 or records_digest(
        [
            {
                "record_key": record["record_key"],
                "record_sha256": record.get("record_sha256"),
                "validation_status": record.get("validation_status", "valid"),
                "occurrence_count": record.get("occurrence_count", 1),
            }
            for record in records
        ]
    )
    summary = {
        "schema_version": 1,
        "run_id": run_id,
        "generation_id": generation_id,
        "parent_generation_id": parent_generation_id,
        "session_id": session_id,
        "mode": mode,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "record_count": len(records),
        "frame_count": len(frames),
        "error_count": len(errors),
        "error_codes": sorted({str(item["code"]) for item in errors}),
        "records_by_screen": records_by_screen,
        "screens": screen_rows,
        "pagination": page_details,
        "validation": validation.stats,
        "sensitive_fields_skipped": sensitive,
        "records_sha256": digest,
        "key_changes": _summarize_key_changes(key_changes or []),
        "report_files": REPORT_FILES,
    }
    record_rows = [
        {
            "screen_id": record["screen_id"],
            "frame_seq": record["frame_seq"],
            "record_key": record["record_key"],
            "primary_key": record.get("primary_key", {}),
            "raw_values": record.get("raw_values", {}),
            "normalized_values": record.get("normalized_values", {}),
            "frame_sha256": record.get("frame_sha256"),
            "record_sha256": record.get("record_sha256"),
            "validation_status": record.get("validation_status", "valid"),
            "occurrence_count": record.get("occurrence_count", 1),
        }
        for record in records
    ]
    return {
        "summary": summary,
        "records": record_rows,
        "errors": errors,
        "captured_frames": [{"frame_seq": item.frame_seq, "step_id": item.step_id, "screen_id": item.expected_screen, "frame_sha256": item.frame.sha256} for item in frames],
    }


def _summarize_key_changes(key_changes: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"inserted": [], "updated": [], "unchanged": [], "retired": []}
    for change in key_changes:
        relation = change["relation"]
        summary.setdefault(relation, []).append(
            {
                "record_key": change["record_key"],
                "screen_id": change["screen_id"],
                "parent_generation_id": change.get("parent_generation_id"),
                "parent_record_sha256": change.get("parent_record_sha256"),
                "record_sha256": change["record_sha256"],
                "occurrence_count": change.get("occurrence_count", 1),
            }
        )
    for relation in summary:
        summary[relation].sort(key=lambda item: item["record_key"])
    return summary


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["screen_id", "frame_seq", "record_key", "primary_key", "normalized_values", "validation_status"], lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow(
            {
                "screen_id": record["screen_id"],
                "frame_seq": record["frame_seq"],
                "record_key": record["record_key"],
                "primary_key": _json(record["primary_key"]),
                "normalized_values": _json(record["normalized_values"]),
                "validation_status": record["validation_status"],
            }
        )
    path.write_text(output.getvalue(), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def staging_dir(output_dir: str | Path, generation_id: str) -> Path:
    return Path(output_dir) / STAGING_DIRNAME / generation_id


def generation_dir(output_dir: str | Path, generation_id: str) -> Path:
    return Path(output_dir) / GENERATIONS_DIRNAME / generation_id


def failed_dir(output_dir: str | Path, run_id: str) -> Path:
    return Path(output_dir) / FAILED_DIRNAME / run_id


def prepare_reports(output_dir: str | Path, payload: dict[str, Any], *, generation_id: str) -> dict[str, Any]:
    """Write the full report set into a private staging directory.

    A manifest with per-file hashes is written last via a temporary file and an
    atomic replace, so a manifest present on disk means every report file is
    complete. Nothing visible to readers is modified.
    """
    directory = staging_dir(output_dir, generation_id)
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "report.json", payload)
    _write_json(directory / "report_summary.json", payload["summary"])
    _write_csv(directory / "records.csv", payload["records"])
    (directory / "report.md").write_text(render_markdown(payload), encoding="utf-8")
    (directory / "report.html").write_text(render_html(payload), encoding="utf-8")
    files = {name: _sha256_file(directory / name) for name in REPORT_FILES}
    manifest = {
        "schema_version": 1,
        "generation_id": generation_id,
        "run_id": payload["summary"]["run_id"],
        "status": payload["summary"]["status"],
        "records_sha256": payload["summary"]["records_sha256"],
        "record_count": payload["summary"]["record_count"],
        "files": files,
    }
    manifest_tmp = directory / f".{MANIFEST_NAME}.tmp"
    _write_json(manifest_tmp, manifest)
    os.replace(manifest_tmp, directory / MANIFEST_NAME)
    return {"staging_dir": str(directory), "manifest": manifest}


def _read_manifest(directory: Path) -> dict[str, Any] | None:
    path = directory / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def manifest_is_complete(directory: Path, manifest: dict[str, Any]) -> bool:
    """Verify every declared file exists and still hashes to the manifest."""
    for name, expected_hash in manifest.get("files", {}).items():
        path = directory / name
        if not path.is_file() or _sha256_file(path) != expected_hash:
            return False
    return True


def _atomic_write_pointer(output_dir: Path, manifest: dict[str, Any], *, final_dir: Path) -> None:
    pointer = {
        "schema_version": 1,
        "generation_id": manifest["generation_id"],
        "run_id": manifest["run_id"],
        "status": manifest["status"],
        "records_sha256": manifest["records_sha256"],
        "report_dir": str(final_dir),
        "files": list(manifest["files"].keys()),
    }
    tmp = output_dir / f".{POINTER_NAME}.tmp"
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(tmp, pointer)
    os.replace(tmp, output_dir / POINTER_NAME)
    # Symlink is a convenience mirror; the JSON pointer remains authoritative.
    link = output_dir / "current"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(final_dir.name if final_dir.parent == output_dir else str(final_dir), link)
    except OSError:
        pass


def publish_reports(output_dir: str | Path, *, generation_id: str) -> dict[str, Any]:
    """Promote staged artifacts to a generation directory and move the pointer.

    Both renames are atomic on the same file system. The SQLite generation must
    already be published, so a crash after the rename but before the pointer
    write leaves the previous pointer intact and is repaired on recovery.
    """
    output = Path(output_dir)
    staged = staging_dir(output, generation_id)
    manifest = _read_manifest(staged)
    if manifest is None or not manifest_is_complete(staged, manifest):
        raise FileNotFoundError(f"complete staged report not found for generation {generation_id}")
    final = generation_dir(output, generation_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        # Rerun after a crash between rename and pointer switch.
        if not manifest_is_complete(final, manifest):
            raise FileNotFoundError(f"published generation directory is corrupt: {final}")
    else:
        os.replace(staged, final)
    _atomic_write_pointer(output, manifest, final_dir=final)
    return {"output_dir": str(final), "pointer": str(output / POINTER_NAME), "files": list(manifest["files"].keys()), "summary": manifest}


def write_failure_report(output_dir: str | Path, payload: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    """Persist a diagnostic report for a failed run without touching the
    current published pointer.
    """
    directory = failed_dir(output_dir, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "report.json", payload)
    _write_json(directory / "report_summary.json", payload["summary"])
    _write_csv(directory / "records.csv", payload["records"])
    (directory / "report.md").write_text(render_markdown(payload), encoding="utf-8")
    (directory / "report.html").write_text(render_html(payload), encoding="utf-8")
    return {"output_dir": str(directory), "files": REPORT_FILES, "summary": payload["summary"]}


def write_reports(output_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Compatibility helper used outside the generation pipeline."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "report.json", payload)
    _write_json(directory / "report_summary.json", payload["summary"])
    _write_csv(directory / "records.csv", payload["records"])
    (directory / "report.md").write_text(render_markdown(payload), encoding="utf-8")
    (directory / "report.html").write_text(render_html(payload), encoding="utf-8")
    return {"output_dir": str(directory), "files": list(payload["summary"]["report_files"]), "summary": payload["summary"]}


def current_pointer(output_dir: str | Path) -> dict[str, Any] | None:
    path = Path(output_dir) / POINTER_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _quarantine(output: Path, path: Path, *, label: str) -> Path:
    recovered_root = output / RECOVERED_DIRNAME
    recovered_root.mkdir(parents=True, exist_ok=True)
    target = recovered_root / f"{path.name}.{label}"
    counter = 1
    while target.exists():
        target = recovered_root / f"{path.name}.{label}.{counter}"
        counter += 1
    os.replace(path, target)
    return target


def recover_artifacts(output_dir: str | Path, store: Any) -> list[dict[str, Any]]:
    """Reconcile report artifacts with ledger generations after any crash.

    Handles the three release cut points:

    * report half-written (no complete manifest) -> quarantine staging dir and
      mark the ledger generation abandoned;
    * files complete but SQLite never switched -> republish the generation
      through the store and move the pointer;
    * SQLite switched but the process died before returning -> rebuild the
      pointer from the published generation directory.

    Returns a human/audit-readable list of recovery actions; no manual cleanup
    of temporary directories is ever required.
    """
    output = Path(output_dir)
    actions: list[dict[str, Any]] = []
    staging_root = output / STAGING_DIRNAME
    if staging_root.is_dir():
        for staged in sorted(staging_root.iterdir()):
            if not staged.is_dir():
                continue
            gen_id = staged.name
            manifest = _read_manifest(staged)
            generation = store.generation(gen_id) if hasattr(store, "generation") else None
            if manifest is not None and manifest_is_complete(staged, manifest) and generation is not None and generation["status"] == "prepared":
                # Cut point 2: files complete, database not yet switched.
                republished = store.republish_recovered_generation(gen_id, finished_at=generation["created_at"], report_manifest=manifest)
                final = generation_dir(output, gen_id)
                final.parent.mkdir(parents=True, exist_ok=True)
                if not final.exists():
                    os.replace(staged, final)
                _atomic_write_pointer(output, manifest, final_dir=final)
                actions.append({"generation_id": gen_id, "action": "republished", "records_sha256": republished["records_sha256"]})
            else:
                # Cut point 1 (or run failed): incomplete evidence is kept for
                # review and the generation is explained as abandoned.
                target = _quarantine(output, staged, label="incomplete")
                if generation is not None and generation["status"] == "prepared" and hasattr(store, "abandon_generation"):
                    store.abandon_generation(
                        generation["run_id"],
                        reason="report artifacts incomplete at recovery",
                        abandoned_at=generation["created_at"],
                    )
                actions.append({"generation_id": gen_id, "action": "quarantined", "location": str(target)})
        if staging_root.exists() and not any(staging_root.iterdir()):
            staging_root.rmdir()

    current_gen = store.current_generation_id() if hasattr(store, "current_generation_id") else None
    if current_gen is not None:
        final = generation_dir(output, current_gen)
        manifest = _read_manifest(final)
        pointer = current_pointer(output)
        if manifest is not None and manifest_is_complete(final, manifest) and (pointer is None or pointer.get("generation_id") != current_gen):
            # Cut point 3: database already points at the new generation.
            _atomic_write_pointer(output, manifest, final_dir=final)
            actions.append({"generation_id": current_gen, "action": "pointer_rebuilt", "location": str(output / POINTER_NAME)})
    return actions
