"""Deterministic report payload construction and artifact writing."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .capture import CapturedFrame
from .extract import Extraction
from .report_render import render_html, render_markdown
from .validate import ValidationResult

def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

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
    summary = {
        "schema_version": 1,
        "run_id": run_id,
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
        "report_files": ["report.html", "report.json", "report.md", "report_summary.json", "records.csv"],
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
            "validation_status": record.get("validation_status", "valid"),
        }
        for record in records
    ]
    return {
        "summary": summary,
        "records": record_rows,
        "errors": errors,
        "captured_frames": [{"frame_seq": item.frame_seq, "step_id": item.step_id, "screen_id": item.expected_screen, "frame_sha256": item.frame.sha256} for item in frames],
    }

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

def write_reports(output_dir: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "report.json", payload)
    _write_json(directory / "report_summary.json", payload["summary"])
    _write_csv(directory / "records.csv", payload["records"])
    (directory / "report.md").write_text(render_markdown(payload), encoding="utf-8")
    (directory / "report.html").write_text(render_html(payload), encoding="utf-8")
    return {"output_dir": str(directory), "files": list(payload["summary"]["report_files"]), "summary": payload["summary"]}
