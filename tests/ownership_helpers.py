"""Shared builders for masked ownership exposure tests."""

from __future__ import annotations

from decimal import Decimal

from termharvest.screen import ScreenFrame
from termharvest.validate_config import BucketFooters, OwnershipCheck

ROWS, COLS = 24, 80


def blank_frame_rows() -> list[str]:
    return [" " * COLS for _ in range(ROWS)]


def put(rows: list[str], row: int, col: int, value: str) -> None:
    rows[row] = rows[row][:col] + value + rows[row][col + len(value):]


def make_ownership_check(
    *,
    purpose: str = "compliance",
    limit: str | None = "50",
    limit_ref: dict | None = None,
    budget: int = 200_000,
    max_bills: int = 24,
    tolerance: str = "0.001",
    screen_id: str = "demo_synth_bills",
    open_statuses: tuple[str, ...] = ("OPEN",),
) -> OwnershipCheck:
    return OwnershipCheck(
        check_id="target_a_open_exposure",
        screen_id=screen_id,
        target_bucket="A",
        buckets=("A", "B"),
        mask_field="customer_ref",
        amount_field="amount",
        status_field="status",
        id_field="bill_id",
        open_statuses=open_statuses,
        footers=(BucketFooters("A", "a_count", "a_total"), BucketFooters("B", "b_count", "b_total")),
        tolerance=Decimal(tolerance),
        max_bills=max_bills,
        search_budget=budget,
        purpose=purpose,
        credit_limit_value=None if limit is None else Decimal(limit),
        credit_limit_screen=None if limit_ref is None else limit_ref["screen_id"],
        credit_limit_field=None if limit_ref is None else limit_ref["field"],
        message="no globally feasible bill-to-bucket assignment matches the trusted footers",
    )


def bill_record(bill_id: str, amount: str, status: str, ref: str | None, key: str, *, frame_seq: int = 0, frame_sha: str = "f" * 64) -> dict:
    values = {"bill_id": bill_id, "bill_date": "2026-01-05", "amount": amount, "status": status}
    if ref is not None:
        values["customer_ref"] = ref
    return {
        "screen_id": "demo_synth_bills",
        "frame_seq": frame_seq,
        "frame_sha256": frame_sha,
        "record_key": key,
        "primary_key": {"bill_id": bill_id},
        "raw_values": dict(values),
        "normalized_values": dict(values),
        "field_sources": {
            "customer_ref": {"row": 5, "col": 50, "end_col": 70, "frame_sha256": frame_sha},
            "amount": {"row": 5, "col": 25, "end_col": 36, "frame_sha256": frame_sha},
        },
    }


def customer_record(credit_limit: str = "50.00", *, customer_id: str = "C00001") -> dict:
    return {
        "screen_id": "demo_customer",
        "frame_seq": 1,
        "frame_sha256": "c" * 64,
        "record_key": "rc" + customer_id,
        "primary_key": {"customer_id": customer_id},
        "raw_values": {"customer_id": customer_id, "credit_limit": credit_limit},
        "normalized_values": {"customer_id": customer_id, "credit_limit": credit_limit},
        "field_sources": {"credit_limit": {"row": 8, "col": 23, "frame_sha256": "c" * 64}},
    }


def synth_bill_frame(rows_spec: list[tuple[str, str, str, str]], footers: dict[str, str]) -> ScreenFrame:
    """Build a 24x80 frame matching demo_synth_bills contract positions."""
    rows = blank_frame_rows()
    put(rows, 0, 1, "SYNTHETIC OPERATIONS CONSOLE / SYNTH BILL OWNERSHIP")
    put(rows, 4, 1, "BILL ID    BILL DATE    AMOUNT       STATUS")
    for offset, (bill_id, date, amount, status, *rest) in enumerate(rows_spec):
        row_index = 5 + offset
        ref = rest[0] if rest else ""
        put(rows, row_index, 1, bill_id)
        put(rows, row_index, 12, date)
        put(rows, row_index, 25, amount)
        put(rows, row_index, 38, status)
        if ref:
            put(rows, row_index, 50, ref)
    footer_rows = {"a_count": 19, "a_total": 20, "b_count": 21, "b_total": 22}
    labels = {"a_count": "BUCKET A COUNT", "a_total": "BUCKET A TOTAL", "b_count": "BUCKET B COUNT", "b_total": "BUCKET B TOTAL"}
    for name, label in labels.items():
        value = footers.get(name, "")
        text = f"{label} : {value}" if value else label
        put(rows, footer_rows[name], 1, text)
    return ScreenFrame.from_rows(rows)
