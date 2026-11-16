from __future__ import annotations

import copy
import json

from termharvest.clock import FixedClock
from termharvest.pipeline import run_pipeline
from termharvest.report import generation_dir
from termharvest.report_render import render_html
from termharvest.store import Store
from termharvest.store import generation_id_for

def run_replay(tmp_path, name, session, screens, validation, demo_paths):
    store = Store(tmp_path / f"{name}.sqlite")
    try:
        result = run_pipeline(
            session=session,
            screens=screens,
            validation_config=validation,
            store=store,
            output_dir=tmp_path / name,
            clock=FixedClock("2026-08-01T00:00:00.000Z"),
            mode="replay",
            cassette_path=demo_paths["replay_cassette"],
            evidence_dir=tmp_path / f"{name}_evidence",
        )
        result["report_dir"] = generation_dir(tmp_path / name, result["generation_id"])
        return result
    finally:
        store.close()

def test_reports_are_byte_for_byte_deterministic(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    first = run_replay(tmp_path, "first", demo_session, demo_screens, demo_validation, demo_paths)
    second = run_replay(tmp_path, "second", demo_session, demo_screens, demo_validation, demo_paths)
    assert first["run_id"] == second["run_id"]
    assert generation_id_for(first["run_id"]) == first["generation_id"]
    for filename in ("report.json", "report_summary.json", "records.csv", "report.md", "report.html", "manifest.json"):
        left = (first["report_dir"] / filename).read_bytes()
        right = (second["report_dir"] / filename).read_bytes()
        assert left == right, filename

def test_summary_matches_expected_fixture(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    result = run_replay(tmp_path, "report", demo_session, demo_screens, demo_validation, demo_paths)
    expected = json.loads(demo_paths["expected_summary"].read_text(encoding="utf-8"))
    for key, value in expected.items():
        assert result["summary"][key] == value, key

def test_html_is_self_contained_and_escaped(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    result = run_replay(tmp_path, "report", demo_session, demo_screens, demo_validation, demo_paths)
    payload = copy.deepcopy(result["payload"])
    payload["records"][0]["normalized_values"]["customer_name"] = "<script>x</script>"
    rendered = render_html(payload)
    lowered = rendered.lower()
    assert "<style>" in lowered
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "<script>x</script>" not in rendered
    assert "&lt;script&gt;x&lt;/script&gt;" in rendered
    assert "仅可用于本人所有或已获书面授权的本地模拟/测试环境" in rendered

def test_csv_has_fixed_header_and_all_records(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    result = run_replay(tmp_path, "report", demo_session, demo_screens, demo_validation, demo_paths)
    lines = (result["report_dir"] / "records.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == ("screen_id,frame_seq,record_key,primary_key,normalized_values,validation_status")
    assert len(lines) == 7
    assert all("SYNTH-PLACEHOLDER" not in line for line in lines)
