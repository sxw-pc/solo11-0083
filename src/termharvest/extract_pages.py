"""Pagination accumulation and duplicate suppression for list screens."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config_types import ScreenContract
from .extract import Extraction, extract_screen
from .screen import ScreenFrame

@dataclass
class PagedExtraction:
    extraction: Extraction
    page_count: int
    stop_reason: str
    pages_seen: list[int] = field(default_factory=list)

def extract_pages(
    frames: list[tuple[int, ScreenFrame]], contract: ScreenContract, *, max_pages: int | None = None, stop_marker: str | None = None, stop_on_repeated_page: bool = True
) -> PagedExtraction:
    """Accumulate pages, deduplicating records and stopping on repetition."""
    records: list[dict] = []
    seen_keys: set[str] = set()
    facts: dict[str, str | None] = {}
    facts_raw: dict[str, str] = {}
    fact_sources: dict[str, dict] = {}
    skipped: set[str] = set()
    pages_seen: list[int] = []
    previous_first: str | None = None
    stop_reason = "frames_exhausted"
    first_seq = frames[0][0] if frames else 0
    first_hash = frames[0][1].sha256 if frames else ""

    for seq, frame in frames:
        if max_pages is not None and len(pages_seen) >= max_pages:
            stop_reason = "max_pages"
            break
        page = extract_screen(frame, contract, seq, stop_marker=stop_marker)
        pages_seen.append(seq)
        skipped.update(page.sensitive_skipped)
        for name, value in page.facts.items():
            facts.setdefault(name, value)
        for name, value in page.facts_raw.items():
            facts_raw.setdefault(name, value)
        for name, source in page.fact_sources.items():
            fact_sources.setdefault(name, source)
        page_records = page.records
        if not page_records:
            stop_reason = "empty_page"
            break
        first_key = page_records[0]["record_key"]
        if stop_on_repeated_page and previous_first is not None and first_key == previous_first:
            stop_reason = "repeated_page"
            break
        for record in page_records:
            if record["record_key"] in seen_keys:
                continue
            seen_keys.add(record["record_key"])
            records.append(record)
        previous_first = first_key
        if stop_marker and stop_marker in frame.text:
            stop_reason = "stop_marker"
            break

    extraction = Extraction(screen_id=contract.screen_id, frame_seq=first_seq, frame_sha256=first_hash, records=records, facts=facts, facts_raw=facts_raw, fact_sources=fact_sources, sensitive_skipped=sorted(skipped))
    return PagedExtraction(extraction=extraction, page_count=len(pages_seen), stop_reason=stop_reason, pages_seen=pages_seen)
