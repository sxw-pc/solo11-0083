"""Session script parsing and navigation expectations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config_types import Expectation, PagingConfig, PrecheckConfig, RetryConfig, SessionContract, SessionStep
from .config_util import integer, list_value, load_yaml_document, mapping, schema_error, string_list, text, validate_target_parts

KEY_RE = re.compile(r"^[A-Z0-9]{1,16}$")

def _parse_expectation(raw: Any, source: Path, field_name: str) -> Expectation:
    item = mapping(raw, source, field_name)
    contains = string_list(item.get("contains", []), source, f"{field_name}.contains", allow_empty=True)
    return Expectation(screen_id=text(item.get("screen_id"), source, f"{field_name}.screen_id"), contains=contains)

def load_session(path: str | Path) -> SessionContract:
    source = Path(path)
    doc = load_yaml_document(source)
    session_id = text(doc.get("session_id"), source, "session_id")
    target = mapping(doc.get("target"), source, "target")
    host, port = validate_target_parts(target.get("host"), target.get("port"), source=str(source))
    start = _parse_expectation(doc.get("start"), source, "start")
    steps_raw = list_value(doc.get("steps"), source, "steps")
    if not steps_raw:
        raise schema_error("steps must not be empty", source, "steps")

    steps: list[SessionStep] = []
    seen: set[str] = set()
    for index, raw_step in enumerate(steps_raw):
        field_name = f"steps[{index}]"
        item = mapping(raw_step, source, field_name)
        step_id = text(item.get("id"), source, f"{field_name}.id")
        if step_id in seen:
            raise schema_error(f"duplicate step id {step_id!r}", source, f"{field_name}.id")
        seen.add(step_id)
        key = text(item.get("key"), source, f"{field_name}.key").strip()
        if not KEY_RE.fullmatch(key):
            raise schema_error("key must match [A-Z0-9]{1,16}", source, f"{field_name}.key")
        steps.append(SessionStep(index=index, step_id=step_id, key=key, expect=_parse_expectation(item.get("expect"), source, f"{field_name}.expect")))

    retry_item = mapping(doc.get("retry", {}), source, "retry")
    timeout = retry_item.get("timeout_seconds", 3.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise schema_error("timeout_seconds must be > 0", source, "retry.timeout_seconds")
    retry = RetryConfig(
        attempts=integer(retry_item.get("attempts", 2), source, "retry.attempts", minimum=1),
        timeout_seconds=float(timeout),
        backoff_ms=integer(retry_item.get("backoff_ms", 100), source, "retry.backoff_ms", minimum=0),
    )

    paging = None
    if doc.get("paging") is not None:
        paging_item = mapping(doc.get("paging"), source, "paging")
        paging = PagingConfig(
            screen_id=text(paging_item.get("screen_id"), source, "paging.screen_id"),
            page_key=text(paging_item.get("page_key"), source, "paging.page_key"),
            max_pages=integer(paging_item.get("max_pages"), source, "paging.max_pages", minimum=1),
            stop_marker=(None if paging_item.get("stop_marker") is None else text(paging_item.get("stop_marker"), source, "paging.stop_marker")),
            stop_on_repeated_page=bool(paging_item.get("stop_on_repeated_page", True)),
        )

    precheck = _parse_precheck(doc.get("precheck"), source)

    return SessionContract(session_id=session_id, target_host=host, target_port=port, start=start, steps=tuple(steps), paging=paging, retry=retry, source_file=str(source), precheck=precheck)

def _parse_precheck(raw: Any, source: Path) -> PrecheckConfig | None:
    """Optional offline contract precheck; absent means the legacy flow."""
    if raw is None:
        return None
    item = mapping(raw, source, "precheck")
    deployment = item.get("deployment", False)
    if not isinstance(deployment, bool):
        raise schema_error("deployment must be boolean", source, "precheck.deployment")
    scenario = item.get("regression_scenario")
    if scenario is not None:
        scenario = text(scenario, source, "precheck.regression_scenario")
    budget_raw = mapping(item.get("budget", {}), source, "precheck.budget")
    budget: list[tuple[str, int]] = []
    for key, value in budget_raw.items():
        budget.append((text(key, source, "precheck.budget"), integer(value, source, f"precheck.budget.{key}", minimum=1)))
    if not deployment and scenario is None:
        raise schema_error("precheck must enable deployment or regression_scenario", source, "precheck")
    return PrecheckConfig(deployment=deployment, regression_scenario=scenario, budget=tuple(sorted(budget)))
