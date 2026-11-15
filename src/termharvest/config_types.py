"""Typed, immutable contract objects shared by loaders and the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class Locator:
    kind: str
    data: dict[str, Any]

@dataclass(frozen=True)
class Column:
    name: str
    start: int
    end: int

@dataclass(frozen=True)
class ScreenField:
    name: str
    locator: Locator
    normalize: tuple[str, ...] = ("trim", "collapse_space", "fullwidth_digits")
    sensitive: bool = False

@dataclass(frozen=True)
class ListRegion:
    start_row: int
    end_row: int
    step: int
    skip_blank: bool
    columns: tuple[Column, ...]

@dataclass(frozen=True)
class ScreenContract:
    screen_id: str
    match: tuple[str, ...]
    fields: tuple[ScreenField, ...]
    summary_fields: tuple[ScreenField, ...]
    list_region: ListRegion | None
    primary_key: tuple[str, ...]
    fingerprint: str | None
    source_file: str

    @property
    def field_names(self) -> set[str]:
        names = {item.name for item in self.fields}
        if self.list_region:
            names.update(column.name for column in self.list_region.columns)
        return names

@dataclass(frozen=True)
class Expectation:
    screen_id: str
    contains: tuple[str, ...]

@dataclass(frozen=True)
class SessionStep:
    index: int
    step_id: str
    key: str
    expect: Expectation

@dataclass(frozen=True)
class PagingConfig:
    screen_id: str
    page_key: str
    max_pages: int
    stop_marker: str | None
    stop_on_repeated_page: bool

@dataclass(frozen=True)
class RetryConfig:
    attempts: int = 2
    timeout_seconds: float = 3.0
    backoff_ms: int = 100

@dataclass(frozen=True)
class SessionContract:
    session_id: str
    target_host: str
    target_port: int
    start: Expectation
    steps: tuple[SessionStep, ...]
    paging: PagingConfig | None
    retry: RetryConfig
    source_file: str

@dataclass(frozen=True)
class ScreenBatch:
    screens: dict[str, ScreenContract] = field(default_factory=dict)

    def require(self, screen_id: str) -> ScreenContract:
        if screen_id not in self.screens:
            from .errors import E_CONFIG_SCHEMA, HarvestError

            raise HarvestError(E_CONFIG_SCHEMA, f"no screen contract for screen_id {screen_id!r}", context={"screen_id": screen_id})
        return self.screens[screen_id]
