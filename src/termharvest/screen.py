"""Fixed-size screen frames, matching and value normalization."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .errors import E_FRAME_MALFORMED, HarvestError

FRAME_ROWS = 24
FRAME_COLS = 80
MAX_FRAME_BYTES = 64 * 1024
FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

@dataclass(frozen=True)
class ScreenFrame:
    """A validated 24 x 80 character screen frame."""

    text: str
    rows: tuple[str, ...]
    sha256: str

    @classmethod
    def parse(cls, text: str, *, expected_rows: int = FRAME_ROWS, expected_cols: int = FRAME_COLS) -> "ScreenFrame":
        if not isinstance(text, str):
            raise HarvestError(E_FRAME_MALFORMED, "frame must be text", context={"type": type(text).__name__})
        if "\r" in text:
            raise HarvestError(E_FRAME_MALFORMED, "frame must not contain carriage returns")
        if "\x00" in text:
            raise HarvestError(E_FRAME_MALFORMED, "frame must not contain NUL")
        rows = text.split("\n")
        if len(rows) != expected_rows:
            raise HarvestError(E_FRAME_MALFORMED, f"frame has {len(rows)} rows, expected {expected_rows}", context={"rows": len(rows), "expected_rows": expected_rows})
        for index, row in enumerate(rows):
            if len(row) != expected_cols:
                raise HarvestError(E_FRAME_MALFORMED, f"row {index} has {len(row)} columns, expected {expected_cols}", context={"row": index, "columns": len(row), "expected_cols": expected_cols})
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return cls(text=text, rows=tuple(rows), sha256=digest)

    @classmethod
    def from_rows(cls, rows: list[str] | tuple[str, ...]) -> "ScreenFrame":
        return cls.parse("\n".join(rows))

    def contains_all(self, fragments: tuple[str, ...] | list[str]) -> bool:
        return all(fragment in self.text for fragment in fragments)

    def line(self, row: int) -> str:
        if not 0 <= row < len(self.rows):
            raise HarvestError(E_FRAME_MALFORMED, f"row {row} is outside frame", context={"row": row, "rows": len(self.rows)})
        return self.rows[row]

    def fingerprint(self) -> str:
        return f"sha256:{self.sha256}"

def ensure_contract_match(frame: ScreenFrame, *, screen_id: str, fragments: tuple[str, ...]) -> None:
    missing = [fragment for fragment in fragments if fragment not in frame.text]
    if missing:
        from .errors import E_SCREEN_CONTRACT_MISMATCH

        raise HarvestError(E_SCREEN_CONTRACT_MISMATCH, "screen does not contain required match fragments", context={"screen_id": screen_id, "missing": missing, "frame_sha256": frame.sha256})

def ensure_fingerprint(frame: ScreenFrame, fingerprint: str | None) -> None:
    if fingerprint is None:
        return
    actual = frame.fingerprint()
    if actual != fingerprint:
        from .errors import E_SCREEN_FINGERPRINT_MISMATCH

        raise HarvestError(E_SCREEN_FINGERPRINT_MISMATCH, "screen fingerprint does not match contract", context={"expected": fingerprint, "actual": actual})

def to_halfwidth_digits(value: str) -> str:
    return value.translate(FULLWIDTH_DIGITS)

def normalize_value(raw: str | None, rules: tuple[str, ...] | list[str]) -> str | None:
    """Normalize while retaining an empty value as ``None``."""
    if raw is None:
        return None
    value = raw
    for rule in rules:
        if rule == "trim":
            value = value.strip()
        elif rule == "collapse_space":
            value = re.sub(r"[ \t]+", " ", value)
        elif rule == "fullwidth_digits":
            value = to_halfwidth_digits(value)
        elif rule == "upper":
            value = value.upper()
        else:
            raise ValueError(f"unknown normalize rule: {rule}")
    if value.strip() == "":
        return None
    return value

def find_label_row(frame: ScreenFrame, label: str, first: int, last: int) -> int | None:
    for row_index in range(max(0, first), min(last, len(frame.rows) - 1) + 1):
        if label in frame.rows[row_index]:
            return row_index
    return None
