#!/usr/bin/env python3
"""Generate deterministic 24x80 mock scenarios and replay cassettes.
All screens are synthetic. Re-running this script produces byte-identical
scenario JSON but rewrites cassettes from the same screen definitions.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
ROOT = Path(__file__).resolve().parents[1]
ROWS, COLS = 24, 80
def blank() -> list[str]:
    return [" " * COLS for _ in range(ROWS)]
def put(rows: list[str], row: int, col: int, value: str) -> None:
    line = rows[row]
    if col + len(value) > COLS:
        raise ValueError(f"fixture text too wide: row={row} value={value!r}")
    rows[row] = line[:col] + value + line[col + len(value) :]
def labeled(rows: list[str], row: int, label: str, value: str) -> None:
    put(rows, row, 1, label)
    put(rows, row, 21, ":")
    put(rows, row, 23, value)
def base_header(rows: list[str], title: str) -> None:
    put(rows, 0, 1, "SYNTHETIC OPERATIONS CONSOLE" + title)
    put(rows, 1, 1, "READ-ONLY SCREEN - FIXTURE DATA ONLY")
def main_menu() -> list[str]:
    rows = blank()
    base_header(rows, "")
    put(rows, 3, 1, "No real company, host, account, or credential is represented.")
    put(rows, 5, 1, "MAIN MENU")
    put(rows, 7, 1, "C  CUSTOMER PROFILE")
    put(rows, 8, 1, "B  BILL LIST")
    put(rows, 9, 1, "X  EXIT")
    put(rows, 11, 1, "SELECT OPTION:")
    put(rows, 22, 1, "FIXTURE: synthetic-loopback-console")
    put(rows, 23, 1, "READ-ONLY DEMO / LOOPBACK ONLY")
    return rows
def customer() -> list[str]:
    rows = blank()
    base_header(rows, " / CUSTOMER PROFILE")
    labeled(rows, 3, "CUSTOMER ID", "C00042")
    labeled(rows, 4, "CUSTOMER NAME", "NORTHWIND SYNTHETIC CO")
    labeled(rows, 5, "CITY", "HARBORVIEW")
    labeled(rows, 6, "STATUS", "ACTIVE")
    labeled(rows, 7, "BALANCE", "1250.75")
    labeled(rows, 8, "CREDIT LIMIT", "5000.00")
    labeled(rows, 9, "PERIOD START", "2026-01-01")
    labeled(rows, 10, "PERIOD END", "2026-01-31")
    labeled(rows, 11, "ADDRESS LINE 1", "100 SAMPLE WAY")
    labeled(rows, 12, "ADDRESS LINE 2", "SUITE 200")
    put(rows, 13, 1, "NOTES:")
    put(rows, 14, 2, "SYNTHETIC CUSTOMER ONLY")
    labeled(rows, 15, "REGION CODE", "０７")
    put(rows, 17, 1, "CONTROL: ENTER = BILL LIST   PF3 = MAIN MENU")
    put(rows, 20, 1, "CUSTOMER RECORD IS A SYNTHETIC FIXTURE")
    labeled(rows, 22, "OPERATOR TOKEN", "SYNTH-PLACEHOLDER-NOT-A-SECRET")
    put(rows, 23, 1, "READ-ONLY DEMO / LOOPBACK ONLY")
    return rows
def bill_list(page: int, pages: int, bills: list[tuple[str, str, str, str]], batch_count: int, batch_total: str, end_marker: bool) -> list[str]:
    rows = blank()
    base_header(rows, " / BILL LIST")
    put(rows, 2, 1, f"PAGE {page} OF {pages}")
    put(rows, 3, 1, "CUSTOMER: C00042 NORTHWIND SYNTHETIC CO")
    put(rows, 4, 1, "BILL ID")
    put(rows, 4, 12, "BILL DATE")
    put(rows, 4, 25, "AMOUNT")
    put(rows, 4, 38, "STATUS")
    for offset, (_bill_id, _date, _amount, _status) in enumerate(bills):
        row = 5 + offset
        put(rows, row, 1, _bill_id)
        put(rows, row, 12, _date)
        put(rows, row, 25, _amount)
        put(rows, row, 38, _status)
    if end_marker:
        put(rows, 5 + len(bills), 1, "END OF LIST")
    put(rows, 20, 1, "BATCH COUNT")
    put(rows, 20, 14, ":")
    put(rows, 20, 16, str(batch_count))
    put(rows, 21, 1, "BATCH TOTAL")
    put(rows, 21, 14, ":")
    put(rows, 21, 16, batch_total)
    put(rows, 22, 1, "CONTROL: PF7 = NEXT PAGE   PF3 = MAIN MENU")
    put(rows, 23, 1, "READ-ONLY DEMO / LOOPBACK ONLY")
    return rows
def error_screen() -> list[str]:
    rows = blank()
    base_header(rows, " / ERROR")
    put(rows, 5, 1, "SYNTHETIC ERROR SCREEN")
    put(rows, 7, 1, "REQUESTED SCREEN IS NOT AVAILABLE")
    put(rows, 9, 1, "THIS IS A DELIBERATE FIXTURE FAILURE")
    put(rows, 22, 1, "CONTROL: PF3 = MAIN MENU")
    put(rows, 23, 1, "READ-ONLY DEMO / LOOPBACK ONLY")
    return rows
def scenario(scenario_id: str, screens: dict[str, list[str]], transitions: dict[str, dict[str, str]], **extra: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "scenario_id": scenario_id,
        "rows": ROWS,
        "cols": COLS,
        "start_screen": "main_menu",
        "screens": {name: {"rows": rows, "transitions": transitions.get(name, {})} for name, rows in screens.items()},
        **extra,
    }
def make_demo() -> dict[str, Any]:
    page1 = [("B000101", "2026-01-05", "100.00", "OPEN"), ("B000102", "2026-01-11", "200.00", "PAID"), ("B000103", "2026-01-18", "75.50", "OPEN")]
    page2 = [("B000104", "2026-01-22", "124.50", "PAID"), ("B000105", "2026-01-30", "50.00", "OPEN")]
    screens = {"main_menu": main_menu(), "customer": customer(), "bill_list_1": bill_list(1, 2, page1, 5, "550.00", False), "bill_list_2": bill_list(2, 2, page2, 5, "550.00", True)}
    return scenario(
        "demo",
        screens,
        {
            "main_menu": {"C": "customer", "B": "bill_list_1"},
            "customer": {"ENTER": "bill_list_1", "PF3": "main_menu"},
            "bill_list_1": {"PF7": "bill_list_2", "PF3": "main_menu"},
            "bill_list_2": {"PF7": "bill_list_2", "PF3": "main_menu"},
        },
    )
def make_paged() -> dict[str, Any]:
    pages = [
        [("B000201", "2026-02-01", "10.00", "OPEN"), ("B000202", "2026-02-02", "20.00", "OPEN")],
        [("B000203", "2026-02-03", "30.00", "PAID"), ("B000204", "2026-02-04", "40.00", "OPEN")],
        [("B000205", "2026-02-05", "50.00", "OPEN"), ("B000206", "2026-02-06", "60.00", "PAID")],
    ]
    screens = {"main_menu": main_menu(), "customer": customer()}
    transitions: dict[str, dict[str, str]] = {"main_menu": {"B": "bill_list_1", "C": "customer"}, "customer": {"PF3": "main_menu"}}
    for index, bills in enumerate(pages, 1):
        name = f"bill_list_{index}"
        screens[name] = bill_list(index, 3, bills, 6, "210.00", index == 3)
        transitions[name] = {"PF7": f"bill_list_{index + 1}" if index < 3 else name, "PF3": "main_menu"}
    return scenario("paged", screens, transitions)
def make_error() -> dict[str, Any]:
    screens = {"main_menu": main_menu(), "error_screen": error_screen()}
    return scenario(
        "error_screen",
        screens,
        {"main_menu": {"C": "error_screen", "E": "error_screen", "SLOW": "error_screen"}, "error_screen": {"PF3": "main_menu"}},
        drop_keys=["TIMEOUT"],
        delay_ms={"SLOW": 250},
        unknown_key_screen="error_screen",
    )
def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
def write_cassette(path: Path, scenario_doc: dict[str, Any], captured_by: str) -> None:
    sequence = [("start", "main_menu"), ("open_customer", "customer"), ("open_bills", "bill_list_1"), ("next_page", "bill_list_2")]
    expected_screen = {"start": "main_menu", "open_customer": "demo_customer", "open_bills": "demo_bill_list", "next_page": "demo_bill_list"}
    lines = [json.dumps({"captured_by": captured_by, "record_type": "header", "session_id": "demo_session", "version": 1}, ensure_ascii=False, sort_keys=True)]
    for seq, (step_id, screen_id) in enumerate(sequence):
        text = "\n".join(scenario_doc["screens"][screen_id]["rows"])
        lines.append(json.dumps({"expected_screen": expected_screen[step_id], "frame_seq": seq, "record_type": "frame", "step_id": step_id, "text": text}, ensure_ascii=False, sort_keys=True))
    lines.append(json.dumps({"frame_count": len(sequence), "record_type": "trailer"}, sort_keys=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
def write_malformed(path: Path, scenario_doc: dict[str, Any]) -> None:
    good = "\n".join(scenario_doc["screens"]["main_menu"]["rows"])
    lines = [
        json.dumps({"captured_by": "synthetic-malformed", "record_type": "header", "session_id": "demo_session", "version": 1}, sort_keys=True),
        json.dumps({"expected_screen": "main_menu", "frame_seq": 0, "record_type": "frame", "step_id": "start", "text": good}, sort_keys=True),
        json.dumps({"expected_screen": "demo_customer", "frame_seq": 1, "record_type": "frame", "step_id": "open_customer", "text": "BROKEN FRAME - WRONG DIMENSIONS"}, sort_keys=True),
        json.dumps({"frame_count": 2, "record_type": "trailer"}, sort_keys=True),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
def main() -> None:
    demo = make_demo()
    paged = make_paged()
    error = make_error()
    write_json(ROOT / "fixtures/mock_host/demo.json", demo)
    write_json(ROOT / "fixtures/mock_host/paged.json", paged)
    write_json(ROOT / "fixtures/mock_host/error_screen.json", error)
    write_cassette(ROOT / "fixtures/cassettes/demo_live.jsonl", demo, "live-mock")
    write_cassette(ROOT / "fixtures/cassettes/demo_replay.jsonl", demo, "replay-fixture")
    write_malformed(ROOT / "fixtures/cassettes/malformed.jsonl", demo)
    print("fixtures written:", ROOT / "fixtures")
if __name__ == "__main__":
    main()
