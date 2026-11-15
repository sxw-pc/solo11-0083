"""Injectable clocks so runs and tests do not depend on wall time."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

class Clock(Protocol):
    def now_iso(self) -> str: ...

    def sleep(self, seconds: float) -> None: ...

def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

class SystemClock:
    """UTC clock with millisecond precision."""

    def now_iso(self) -> str:
        return _utc_iso(datetime.now(timezone.utc))

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            import time

            time.sleep(seconds)

class FixedClock:
    """Deterministic clock that records requested sleeps instead of waiting."""

    def __init__(self, now: str = "2026-01-01T00:00:00.000Z") -> None:
        self._now = now
        self.sleeps: list[float] = []

    def now_iso(self) -> str:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleeps.append(round(float(seconds), 6))

    def advance(self, seconds: float) -> None:
        """Advance the fixed timestamp without relying on a real timer."""
        current = datetime.strptime(self._now, "%Y-%m-%dT%H:%M:%S.%fZ")
        updated = current.timestamp() + seconds
        self._now = _utc_iso(datetime.fromtimestamp(updated, timezone.utc))
