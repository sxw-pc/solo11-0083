"""Pure pipeline steps shared by the orchestrator."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .capture import CapturedFrame
from .clock import Clock
from .config_types import ScreenContract, SessionContract
from .errors import E_SCENARIO_INVALID, E_SCREEN_NOT_CAPTURED, HarvestError
from .extract import Extraction, extract_screen
from .extract_pages import extract_pages
from .replay import collect_replay
from .screen import DEFAULT_INPUT_MODE
from .session import TerminalSession

def make_run_id(session: SessionContract, mode: str, started_at: str) -> str:
    seed = "|".join([session.session_id, mode, started_at, session.target_host, str(session.target_port)])
    return "run-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

def _collect(session: SessionContract, *, mode: str, cassette_path: str | Path | None, event_sink: Any, clock: Clock, evidence_dir: Path, logger: Any, input_mode: str = DEFAULT_INPUT_MODE) -> list[CapturedFrame]:
    if mode == "replay":
        if cassette_path is None:
            raise HarvestError(E_SCENARIO_INVALID, "replay mode requires a cassette path")
        return collect_replay(cassette_path, session, event_sink=event_sink, clock=clock, evidence_dir=evidence_dir, logger=logger)
    return TerminalSession(session, event_sink=event_sink, clock=clock, evidence_dir=evidence_dir, logger=logger, input_mode=input_mode).collect()

def _contract_screen_order(frames: list[CapturedFrame], screens: dict[str, ScreenContract]) -> list[str]:
    order: list[str] = []
    for item in frames:
        name = item.expected_screen
        if name in screens and name not in order:
            order.append(name)
    return order

def _extract_all(frames: list[CapturedFrame], screens: dict[str, ScreenContract], session: SessionContract) -> tuple[list[Extraction], list[dict[str, Any]]]:
    extractions: list[Extraction] = []
    page_details: list[dict[str, Any]] = []
    for screen_id in _contract_screen_order(frames, screens):
        contract = screens[screen_id]
        grouped = [item for item in frames if item.expected_screen == screen_id]
        if not grouped:
            raise HarvestError(E_SCREEN_NOT_CAPTURED, "no captured frame for contracted step", context={"screen_id": screen_id})
        if contract.list_region is not None:
            paging = session.paging if session.paging is not None and session.paging.screen_id == screen_id else None
            paged = extract_pages(
                [(item.frame_seq, item.frame) for item in grouped],
                contract,
                max_pages=paging.max_pages if paging else None,
                stop_marker=paging.stop_marker if paging else None,
                stop_on_repeated_page=(paging.stop_on_repeated_page if paging else True),
            )
            extractions.append(paged.extraction)
            page_details.append({"screen_id": screen_id, "page_count": paged.page_count, "stop_reason": paged.stop_reason, "pages_seen": paged.pages_seen})
        else:
            extractions.append(extract_screen(grouped[0].frame, contract, grouped[0].frame_seq))
    return extractions, page_details
