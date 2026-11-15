"""Mock terminal scenario JSON loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import E_SCENARIO_INVALID, HarvestError
from .screen import FRAME_COLS, FRAME_ROWS, ScreenFrame

@dataclass(frozen=True)
class ScenarioScreen:
    screen_id: str
    frame: ScreenFrame
    transitions: dict[str, str]

@dataclass(frozen=True)
class MockScenario:
    scenario_id: str
    start_screen: str
    screens: dict[str, ScenarioScreen]
    delay_ms: dict[str, int]
    drop_keys: frozenset[str]
    unknown_key_screen: str | None
    source_file: str

    def frame(self, screen_id: str) -> ScreenFrame:
        return self.screens[screen_id].frame

    def next_screen(self, current: str, key: str) -> str:
        transitions = self.screens[current].transitions
        if key in transitions:
            return transitions[key]
        if self.unknown_key_screen is not None:
            return self.unknown_key_screen
        return current

def _invalid(message: str, source: Path, field_name: str) -> HarvestError:
    return HarvestError(E_SCENARIO_INVALID, message, context={"file": str(source), "field": field_name})

def _text(value: Any, source: Path, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid("expected non-empty string", source, field_name)
    return value

def load_scenario(path: str | Path) -> MockScenario:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HarvestError(E_SCENARIO_INVALID, f"cannot read scenario: {exc}", context={"file": str(source)}) from exc
    except json.JSONDecodeError as exc:
        raise HarvestError(E_SCENARIO_INVALID, "scenario is not valid JSON", context={"file": str(source), "line": exc.lineno, "column": exc.colno}, cause=exc) from exc
    if not isinstance(raw, dict):
        raise _invalid("scenario root must be an object", source, "$")
    rows = raw.get("rows", FRAME_ROWS)
    columns = raw.get("cols", FRAME_COLS)
    if rows != FRAME_ROWS or columns != FRAME_COLS:
        raise _invalid("scenario must use 24 rows x 80 columns", source, "rows")
    scenario_id = _text(raw.get("scenario_id"), source, "scenario_id")
    start_screen = _text(raw.get("start_screen"), source, "start_screen")
    screens_raw = raw.get("screens")
    if not isinstance(screens_raw, dict) or not screens_raw:
        raise _invalid("screens must be a non-empty object", source, "screens")
    screens: dict[str, ScenarioScreen] = {}
    for screen_id, spec in screens_raw.items():
        field_name = f"screens.{screen_id}"
        if not isinstance(spec, dict):
            raise _invalid("screen spec must be an object", source, field_name)
        screen_rows = spec.get("rows")
        if not isinstance(screen_rows, list) or not all(isinstance(row, str) for row in screen_rows):
            raise _invalid("screen rows must be a list of strings", source, f"{field_name}.rows")
        try:
            frame = ScreenFrame.from_rows(screen_rows)
        except HarvestError as exc:
            raise _invalid(f"screen has invalid dimensions: {exc.message}", source, f"{field_name}.rows") from exc
        transitions = spec.get("transitions", {})
        if not isinstance(transitions, dict) or not all(isinstance(key, str) and isinstance(target, str) for key, target in transitions.items()):
            raise _invalid("transitions must map strings to strings", source, f"{field_name}.transitions")
        screens[screen_id] = ScenarioScreen(screen_id=screen_id, frame=frame, transitions=dict(transitions))
    if start_screen not in screens:
        raise _invalid("start_screen does not exist", source, "start_screen")
    for screen in screens.values():
        for key, target in screen.transitions.items():
            if target not in screens:
                raise _invalid(f"transition target {target!r} does not exist", source, f"screens.{screen.screen_id}.transitions.{key}")
    unknown_key_screen = raw.get("unknown_key_screen")
    if unknown_key_screen is not None:
        unknown_key_screen = _text(unknown_key_screen, source, "unknown_key_screen")
        if unknown_key_screen not in screens:
            raise _invalid("unknown_key_screen does not exist", source, "unknown_key_screen")
    delay_raw = raw.get("delay_ms", {})
    if not isinstance(delay_raw, dict):
        raise _invalid("delay_ms must be an object", source, "delay_ms")
    delay_ms: dict[str, int] = {}
    for key, delay in delay_raw.items():
        if not isinstance(key, str) or isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
            raise _invalid("delay_ms values must be non-negative integers", source, "delay_ms")
        delay_ms[key] = delay
    drop_raw = raw.get("drop_keys", [])
    if not isinstance(drop_raw, list) or not all(isinstance(item, str) for item in drop_raw):
        raise _invalid("drop_keys must be a list of strings", source, "drop_keys")
    return MockScenario(
        scenario_id=scenario_id, start_screen=start_screen, screens=screens, delay_ms=delay_ms, drop_keys=frozenset(drop_raw), unknown_key_screen=unknown_key_screen, source_file=str(source)
    )
