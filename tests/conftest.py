from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from termharvest.clock import FixedClock  # noqa: E402
from termharvest.config import load_session, load_screens_dir  # noqa: E402
from termharvest.mockhost import MockTerminalServer  # noqa: E402
from termharvest.validate_config import load_validation_config  # noqa: E402

@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT

@pytest.fixture(scope="session")
def fixed_clock() -> FixedClock:
    return FixedClock("2026-03-01T12:00:00.000Z")

@pytest.fixture()
def demo_session():
    return load_session(ROOT / "contracts/sessions/demo_session.yaml")

@pytest.fixture(scope="session")
def demo_screens():
    return load_screens_dir(ROOT / "contracts/screens")

@pytest.fixture(scope="session")
def demo_validation():
    return load_validation_config(ROOT / "contracts/validators/demo_rules.yaml")

@pytest.fixture()
def demo_paths() -> dict[str, Path]:
    return {
        "demo": ROOT / "fixtures/mock_host/demo.json",
        "paged": ROOT / "fixtures/mock_host/paged.json",
        "error": ROOT / "fixtures/mock_host/error_screen.json",
        "live_cassette": ROOT / "fixtures/cassettes/demo_live.jsonl",
        "replay_cassette": ROOT / "fixtures/cassettes/demo_replay.jsonl",
        "malformed": ROOT / "fixtures/cassettes/malformed.jsonl",
        "expected_records": ROOT / "fixtures/expected/demo_records.json",
        "expected_summary": ROOT / "fixtures/expected/demo_report_summary.json",
    }

@pytest.fixture()
def mock_server(demo_paths):
    server = MockTerminalServer(demo_paths["demo"], host="127.0.0.1", port=0, frame_timeout=2.0)
    server.start()
    try:
        yield server
    finally:
        server.stop()

@pytest.fixture()
def live_session(demo_session, mock_server):
    return replace(demo_session, target_port=mock_server.actual_port)
