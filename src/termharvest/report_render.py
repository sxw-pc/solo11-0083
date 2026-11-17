"""Self-contained Markdown and HTML renderers."""

from __future__ import annotations

import html
import json
from typing import Any

def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")

TRUST_LABELS = {
    "trusted": "受信回放 / trusted",
    "unauthenticated": "未认证 / unauthenticated",
    "not_applicable": "不适用 / not applicable",
    "rejected": "已拒绝 / rejected",
}

def _trust(payload: dict[str, Any]) -> dict[str, Any]:
    trust = payload.get("trust") or payload["summary"].get("trust") or {}
    return {
        "trust_status": trust.get("trust_status", payload["summary"].get("trust_status", "not_applicable")),
        "trusted": bool(trust.get("trusted", payload["summary"].get("trusted", False))),
        "policy": trust.get("policy"),
        "cassette_version": trust.get("cassette_version"),
        "record_seq": trust.get("record_seq"),
        "accepted_record_seq": trust.get("accepted_record_seq"),
        "contract_digest": trust.get("contract_digest"),
        "signers": trust.get("signers") or [],
        "error_code": trust.get("error_code"),
        "reason": trust.get("reason"),
        "idempotent": bool(trust.get("idempotent", False)),
        "package_digest": trust.get("package_digest"),
    }

def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    trust = _trust(payload)
    trust_lines = [
        "## Provenance / Trust",
        "",
        f"- trust_status: `{trust['trust_status']}` ({TRUST_LABELS.get(trust['trust_status'], '')})",
        f"- policy: `{trust['policy'] or '-'}`",
        f"- cassette_version: `{trust['cassette_version'] if trust['cassette_version'] is not None else '-'}`",
        f"- record_seq: `{trust['record_seq'] if trust['record_seq'] is not None else '-'}`",
        f"- accepted_record_seq: `{trust['accepted_record_seq'] if trust['accepted_record_seq'] is not None else '-'}`",
    ]
    if trust["trust_status"] == "unauthenticated":
        trust_lines.append("- note: unsigned v1 cassette replay; integrity is checked, recording identity is **not** authenticated.")
    if trust["trust_status"] == "not_applicable":
        trust_lines.append("- note: live collection; no cassette provenance applies.")
    if trust["reason"]:
        trust_lines.append(f"- reason: `{_md_cell(trust['reason'])}`")
    if trust["error_code"]:
        trust_lines.append(f"- error_code: `{trust['error_code']}`")
    trust_lines.extend(["", "| signer key_id | public key fingerprint |", "| --- | --- |"])
    if trust["signers"]:
        for signer in trust["signers"]:
            trust_lines.append(f"| `{_md_cell(signer.get('key_id') or '-')}` | `{_md_cell(signer.get('public_key_fingerprint') or '-')}` |")
    else:
        trust_lines.append("| _none_ | _none_ |")
    trust_lines.append("")
    lines = [
        "# 终端拾屏台采集报告 / termharvest report",
        "",
        "> 仅可用于本人所有或已获书面授权的本地模拟/测试环境。",
        "",
        f"- run_id: `{summary['run_id']}`",
        f"- session_id: `{summary['session_id']}`",
        f"- mode: `{summary['mode']}`",
        f"- status: `{summary['status']}`",
        f"- trust: `{trust['trust_status']}`",
        f"- started_at: `{summary['started_at']}`",
        f"- finished_at: `{summary['finished_at']}`",
        f"- records: `{summary['record_count']}`",
        f"- frames: `{summary['frame_count']}`",
        f"- validation_errors: `{summary['error_count']}`",
        "",
        *trust_lines,
        "## Screens",
        "",
        "| screen_id | frames | records | pages | stop_reason |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for screen in summary["screens"]:
        lines.append(
            "| {screen_id} | {frame_count} | {record_count} | {page_count} | {stop_reason} |".format(
                screen_id=_md_cell(screen["screen_id"]),
                frame_count=screen["frame_count"],
                record_count=screen["record_count"],
                page_count=screen["page_count"],
                stop_reason=_md_cell(screen["stop_reason"] or "-"),
            )
        )
    lines.extend(["", "## Records", ""])
    if not payload["records"]:
        lines.append("_No records extracted._")
    else:
        lines.extend(["| screen_id | frame | record_key | normalized_values | status |", "| --- | ---: | --- | --- | --- |"])
        for record in payload["records"]:
            lines.append(
                "| {screen} | {frame} | `{key}` | `{values}` | {status} |".format(
                    screen=_md_cell(record["screen_id"]),
                    frame=record["frame_seq"],
                    key=record["record_key"][:16],
                    values=_md_cell(_compact(record["normalized_values"])),
                    status=_md_cell(record["validation_status"]),
                )
            )
    lines.extend(["", "## Validation Errors", ""])
    if not payload["errors"]:
        lines.append("_No validation errors._")
    else:
        value = json.dumps(payload["errors"], ensure_ascii=False, sort_keys=True, indent=2)
        lines.extend(["```json", value, "```"])
    lines.extend(
        [
            "",
            "## Pagination",
            "",
            "```json",
            json.dumps(payload["summary"]["pagination"], ensure_ascii=False, sort_keys=True, indent=2),
            "```",
            "",
            "## Compliance",
            "",
            "Read-only collection against synthetic loopback fixtures. " "No external network access, no credential extraction.",
            "",
        ]
    )
    return "\n".join(lines)

def render_html(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    trust = _trust(payload)
    signer_rows = []
    for signer in trust["signers"]:
        signer_rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(signer.get('key_id') or '-'))}</code></td>"
            f"<td><code>{html.escape(str(signer.get('public_key_fingerprint') or '-'))}</code></td>"
            "</tr>"
        )
    if not signer_rows:
        signer_rows.append('<tr><td colspan="2">No verified signers.</td></tr>')
    trust_note = ""
    if trust["trust_status"] == "unauthenticated":
        trust_note = "<p><strong>Unauthenticated:</strong> unsigned v1 cassette replay; integrity is checked, recording identity is not authenticated.</p>"
    elif trust["trust_status"] == "not_applicable":
        trust_note = "<p>Live collection; no cassette provenance applies.</p>"
    reason_html = f"<p>Reason: <code>{html.escape(str(trust['reason']))}</code></p>" if trust["reason"] else ""
    error_html = f"<p>Error: <code>{html.escape(str(trust['error_code']))}</code></p>" if trust["error_code"] else ""
    trust_block = f"""<p>trust_status <code>{html.escape(str(trust['trust_status']))}</code> / policy
<code>{html.escape(str(trust['policy'] or '-'))}</code> / cassette version
<code>{html.escape(str(trust['cassette_version'] if trust['cassette_version'] is not None else '-'))}</code> / record_seq
<code>{html.escape(str(trust['record_seq'] if trust['record_seq'] is not None else '-'))}</code> / accepted_record_seq
<code>{html.escape(str(trust['accepted_record_seq'] if trust['accepted_record_seq'] is not None else '-'))}</code></p>
{trust_note}{reason_html}{error_html}
<table><thead><tr><th>signer key_id</th><th>public key fingerprint</th></tr></thead>
<tbody>{''.join(signer_rows)}</tbody></table>"""
    rows = []
    for record in payload["records"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(record['screen_id'])}</td>"
            f"<td>{record['frame_seq']}</td>"
            f"<td><code>{html.escape(record['record_key'][:16])}</code></td>"
            f"<td><code>{html.escape(_compact(record['normalized_values']))}</code></td>"
            f"<td>{html.escape(record['validation_status'])}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="5">No records extracted.</td></tr>')
    error_block = "<p>No validation errors.</p>" if not payload["errors"] else "<pre>" + html.escape(json.dumps(payload["errors"], ensure_ascii=False, indent=2)) + "</pre>"
    screen_rows = []
    for screen in summary["screens"]:
        screen_rows.append(
            "<tr>"
            f"<td>{html.escape(screen['screen_id'])}</td>"
            f"<td>{screen['frame_count']}</td>"
            f"<td>{screen['record_count']}</td>"
            f"<td>{screen['page_count']}</td>"
            f"<td>{html.escape(screen['stop_reason'] or '-')}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>termharvest report</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; margin: 2rem; line-height: 1.5; }}
h1, h2 {{ font-weight: 650; }}
.notice {{ border-left: 4px solid #8a6d3b; padding: .5rem 1rem; background: rgba(138,109,59,.12); }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #8888; padding: .4rem .55rem; text-align: left; vertical-align: top; }}
code, pre {{ overflow-wrap: anywhere; }}
pre {{ padding: 1rem; border: 1px solid #8888; overflow: auto; }}
</style>
</head>
<body>
<h1>终端拾屏台采集报告</h1>
<p class="notice">仅可用于本人所有或已获书面授权的本地模拟/测试环境。</p>
<p>run_id <code>{html.escape(summary['run_id'])}</code> / session
<code>{html.escape(summary['session_id'])}</code> / mode
<code>{html.escape(summary['mode'])}</code> / status
<code>{html.escape(summary['status'])}</code></p>
<h2>Screens</h2>
<table><thead><tr><th>screen_id</th><th>frames</th><th>records</th><th>pages</th><th>stop_reason</th></tr></thead>
<tbody>{''.join(screen_rows)}</tbody></table>
<h2>Records</h2>
<table><thead><tr><th>screen_id</th><th>frame</th><th>key</th><th>normalized_values</th><th>status</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<h2>Validation Errors</h2>
{error_block}
<h2>Provenance / Trust</h2>
{trust_block}
<h2>Provenance</h2>
<p>Started {html.escape(summary['started_at'])}, finished {html.escape(summary['finished_at'])}.
Frames captured {summary['frame_count']}, records extracted {summary['record_count']}.
Evidence hashes are SHA-256 and are retained in the SQLite ledger.</p>
</body>
</html>
"""
