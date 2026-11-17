"""Self-contained Markdown and HTML renderers."""

from __future__ import annotations

import html
import json
from typing import Any

def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")

def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# 终端拾屏台采集报告 / termharvest report",
        "",
        "> 仅可用于本人所有或已获书面授权的本地模拟/测试环境。",
        "",
        f"- run_id: `{summary['run_id']}`",
        f"- session_id: `{summary['session_id']}`",
        f"- mode: `{summary['mode']}`",
        f"- status: `{summary['status']}`",
        f"- started_at: `{summary['started_at']}`",
        f"- finished_at: `{summary['finished_at']}`",
        f"- records: `{summary['record_count']}`",
        f"- frames: `{summary['frame_count']}`",
        f"- validation_errors: `{summary['error_count']}`",
        "",
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
    lines.extend(["", "## Credit Exposure", ""])
    exposure = payload.get("credit_exposure") or []
    if not exposure:
        lines.append("_No ownership exposure checks configured._")
    else:
        lines.extend(["| check_id | target | purpose | status | credit_state | min | max | limit | action |", "| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |"])
        for item in exposure:
            lines.append(
                "| {check_id} | {target} | {purpose} | {status} | {state} | {low} | {high} | {limit} | {action} |".format(
                    check_id=_md_cell(item["check_id"]),
                    target=_md_cell(item["target_bucket"]),
                    purpose=_md_cell(item["purpose"]),
                    status=_md_cell(item["status"]),
                    state=_md_cell(str(item.get("credit_state") or "-")),
                    low=_md_cell(str(item.get("open_minimum") if item.get("open_minimum") is not None else "-")),
                    high=_md_cell(str(item.get("open_maximum") if item.get("open_maximum") is not None else "-")),
                    limit=_md_cell(str(item.get("credit_limit") if item.get("credit_limit") is not None else "-")),
                    action=_md_cell(str(item.get("credit_action") or "-")),
                )
            )
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
    exposure = payload.get("credit_exposure") or []
    if not exposure:
        exposure_block = "<p>No ownership exposure checks configured.</p>"
    else:
        exposure_rows = []
        for item in exposure:
            exposure_rows.append(
                "<tr>"
                f"<td>{html.escape(item['check_id'])}</td>"
                f"<td>{html.escape(item['target_bucket'])}</td>"
                f"<td>{html.escape(item['purpose'])}</td>"
                f"<td>{html.escape(item['status'])}</td>"
                f"<td>{html.escape(str(item.get('credit_state') or '-'))}</td>"
                f"<td>{html.escape(str(item.get('open_minimum') if item.get('open_minimum') is not None else '-'))}</td>"
                f"<td>{html.escape(str(item.get('open_maximum') if item.get('open_maximum') is not None else '-'))}</td>"
                f"<td>{html.escape(str(item.get('credit_limit') if item.get('credit_limit') is not None else '-'))}</td>"
                f"<td>{html.escape(str(item.get('credit_action') or '-'))}</td>"
                "</tr>"
            )
        exposure_block = (
            "<table><thead><tr><th>check_id</th><th>target</th><th>purpose</th><th>status</th>"
            "<th>credit_state</th><th>min</th><th>max</th><th>limit</th><th>action</th></tr></thead>"
            f"<tbody>{''.join(exposure_rows)}</tbody></table>"
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
<h2>Credit Exposure</h2>
{exposure_block}
<h2>Provenance</h2>
<p>Started {html.escape(summary['started_at'])}, finished {html.escape(summary['finished_at'])}.
Frames captured {summary['frame_count']}, records extracted {summary['record_count']}.
Evidence hashes are SHA-256 and are retained in the SQLite ledger.</p>
</body>
</html>
"""
