from __future__ import annotations

from dataclasses import replace

from termharvest.cassette import write_cassette
from termharvest.clock import FixedClock
from termharvest.config_session import load_session
from termharvest.config_screen import load_screen
from termharvest.config_types import Expectation, SessionStep
from termharvest.extract import make_record_key
from termharvest.pipeline import run_pipeline
from termharvest.screen import ScreenFrame
from termharvest.store import Store
from termharvest.validate_config import load_validation_config

from ownership_helpers import synth_bill_frame

CONTRACTS = "/workspace/contracts"


def load_synth_config(name: str):
    return load_validation_config(f"{CONTRACTS}/validators/{name}")


def _menu_frame() -> ScreenFrame:
    rows = [" " * 80 for _ in range(24)]
    rows[0] = " SYNTHETIC OPERATIONS CONSOLE / SYNTH BILL OWNERSHIP".ljust(80)
    rows[5] = " MAIN MENU".ljust(80)
    return ScreenFrame.from_rows(rows)


def write_synth_cassette(tmp_path, frames):
    path = tmp_path / "synth.jsonl"
    write_cassette(path, session_id="synth_session", frames=frames, captured_by="replay-fixture")
    return path


def synth_session(extra_steps: int = 0):
    base = load_session(f"{CONTRACTS}/sessions/demo_session.yaml")
    steps = (SessionStep(index=0, step_id="open_bills", key="ENTER", expect=Expectation(screen_id="demo_synth_bills", contains=("SYNTH BILL OWNERSHIP",))),)
    if extra_steps:
        steps = steps + tuple(
            SessionStep(index=i + 1, step_id=f"extra_{i}", key="ENTER", expect=Expectation(screen_id="demo_customer", contains=("CUSTOMER PROFILE",)))
            for i in range(extra_steps)
        )
    return replace(
        base,
        session_id="synth_session",
        start=Expectation(screen_id="demo_synth_bills", contains=("SYNTH BILL OWNERSHIP",)),
        steps=steps,
        paging=None,
    )


def run_synth(tmp_path, *, footers, rows_spec, config_name, credit_limit="50.00"):
    frame = synth_bill_frame(rows_spec, footers)
    customer_frame = _customer_frame(credit_limit)
    cassette = write_synth_cassette(
        tmp_path,
        [
            (0, "start", "demo_synth_bills", frame),
            (1, "open_bills", "demo_synth_bills", frame),
            (2, "extra_0", "demo_customer", customer_frame),
        ],
    )
    screens = {
        "demo_synth_bills": load_screen(f"{CONTRACTS}/screens/demo_synth_bills.yaml"),
        "demo_customer": load_screen(f"{CONTRACTS}/screens/demo_customer.yaml"),
    }
    config = load_synth_config(config_name)
    store = Store(tmp_path / "ledger.sqlite")
    try:
        return run_pipeline(
            session=synth_session(extra_steps=1),
            screens=screens,
            validation_config=config,
            store=store,
            output_dir=tmp_path / "report",
            clock=FixedClock("2026-09-01T00:00:00.000Z"),
            mode="replay",
            cassette_path=cassette,
            evidence_dir=tmp_path / "evidence",
        )
    finally:
        store.close()


CANONICAL_ROWS = [
    ("B000001", "2026-01-05", "80.00", "OPEN", "A,B"),
    ("B000002", "2026-01-06", "20.00", "OPEN", "A"),
]
PINNED_FOOTERS = {"a_count": "1", "a_total": "20.00", "b_count": "1", "b_total": "80.00"}


def test_extraction_captures_mask_provenance_without_changing_record_key(tmp_path):
    frame = synth_bill_frame(CANONICAL_ROWS, PINNED_FOOTERS)
    contract = load_screen(f"{CONTRACTS}/screens/demo_synth_bills.yaml")
    from termharvest.extract import extract_screen

    extraction = extract_screen(frame, contract, 0)
    records = extraction.records
    assert [r["normalized_values"]["bill_id"] for r in records] == ["B000001", "B000002"]
    first = records[0]
    # mask source points at the exact frame cell
    assert first["field_sources"]["customer_ref"]["row"] == 5
    assert first["field_sources"]["customer_ref"]["col"] == 50
    # record_key is derived only from natural key/raw content, never from provenance
    expected_key = make_record_key(contract.screen_id, contract.primary_key, first["normalized_values"], first["raw_values"])
    assert first["record_key"] == expected_key
    # footer provenance retained
    assert extraction.fact_sources["a_total"]["row"] == 20


def test_pipeline_pinned_footers_prove_safe_in_compliance_contract(tmp_path):
    result = run_synth(tmp_path, footers=PINNED_FOOTERS, rows_spec=CANONICAL_ROWS, config_name="demo_ownership_compliance.yaml")
    assert result["summary"]["status"] == "success"
    exposure = result["payload"]["credit_exposure"]
    assert len(exposure) == 1
    item = exposure[0]
    assert item["status"] == "proven"
    assert item["open_minimum"] == "20.00"
    assert item["open_maximum"] == "20.00"
    assert item["credit_state"] == "safe"
    assert item["credit_action"] == "pass"
    # report summary carries the credit status too
    assert result["summary"]["credit_exposure"][0]["credit_state"] == "safe"
    # witness is a full one-to-one assignment
    edges = {edge["bill_id"]: edge["bucket"] for edge in item["minimum_witness"]["edges"]}
    assert edges == {"B000001": "B", "B000002": "A"}


def test_footer_change_moves_feasible_region_and_credit_state(tmp_path):
    # Remove the trusted footers: same frames, but A could be 20 or 100 -> inconclusive.
    result = run_synth(tmp_path, footers={}, rows_spec=CANONICAL_ROWS, config_name="demo_ownership_compliance.yaml")
    item = result["payload"]["credit_exposure"][0]
    assert item["status"] == "proven"
    assert (item["open_minimum"], item["open_maximum"]) == ("20.00", "100.00")
    assert item["credit_state"] == "inconclusive"
    assert item["credit_action"] == "hold_for_manual_review"
    # run stays successful: inconclusive is not a validation error
    assert result["summary"]["status"] == "success"


def test_compliance_and_manual_review_diverge_on_same_ambiguous_page(tmp_path):
    compliance = run_synth(tmp_path, footers={}, rows_spec=CANONICAL_ROWS, config_name="demo_ownership_compliance.yaml")
    review = run_synth(tmp_path, footers={}, rows_spec=CANONICAL_ROWS, config_name="demo_ownership_review.yaml")
    c_item = compliance["payload"]["credit_exposure"][0]
    r_item = review["payload"]["credit_exposure"][0]
    # identical mathematical bounds ...
    assert (c_item["open_minimum"], c_item["open_maximum"]) == (r_item["open_minimum"], r_item["open_maximum"]) == ("20.00", "100.00")
    # ... but different operational conclusions
    assert c_item["credit_action"] == "hold_for_manual_review"
    assert r_item["credit_action"] == "review_with_safe_witness"
    assert r_item["safe_witness_available"] is True
    # the safe witness must not be mislabelled as a certain pass
    assert r_item["credit_state"] == "inconclusive"
    assert "safe_witness" not in c_item


def test_infeasible_footers_fail_the_run_with_conflict_core(tmp_path):
    bad_footers = {"a_count": "1", "a_total": "100.00", "b_count": "1", "b_total": "100.00"}
    result = run_synth(tmp_path, footers=bad_footers, rows_spec=CANONICAL_ROWS, config_name="demo_ownership_compliance.yaml")
    assert result["summary"]["status"] == "failed"
    assert "E_OWNERSHIP_INFEASIBLE" in result["summary"]["error_codes"]
    item = result["payload"]["credit_exposure"][0]
    assert item["status"] == "infeasible"
    assert item["conflict_core"]


def test_legacy_contract_without_customer_ref_runs_unchanged(tmp_path, demo_validation, demo_paths):
    """The old single-customer fixture and its config never enter the ownership path."""
    from termharvest.pipeline import run_pipeline as run

    store = Store(tmp_path / "legacy.sqlite")
    try:
        screens = {
            "demo_customer": load_screen(f"{CONTRACTS}/screens/demo_customer.yaml"),
            "demo_bill_list": load_screen(f"{CONTRACTS}/screens/demo_bill_list.yaml"),
        }
        result = run(
            session=load_session(f"{CONTRACTS}/sessions/demo_session.yaml"),
            screens=screens,
            validation_config=demo_validation,
            store=store,
            output_dir=tmp_path / "legacy_report",
            clock=FixedClock("2026-09-02T00:00:00.000Z"),
            mode="replay",
            cassette_path=demo_paths["replay_cassette"],
            evidence_dir=tmp_path / "legacy_evidence",
        )
        assert result["summary"]["status"] == "success"
        assert result["payload"]["credit_exposure"] == []
        assert result["summary"]["credit_exposure"] == []
        # legacy records still validate exactly as before
        assert result["summary"]["record_count"] == 6
    finally:
        store.close()


def test_customer_screen_credit_limit_feeds_the_decision(tmp_path):
    result = run_synth(tmp_path, footers=PINNED_FOOTERS, rows_spec=CANONICAL_ROWS, config_name="demo_ownership_compliance.yaml")
    item = result["payload"]["credit_exposure"][0]
    assert item["credit_limit"] == "50.00"
    assert item["credit_limit_source"]["screen_id"] == "demo_customer"
    assert item["credit_limit_source"]["source"]["row"] == 8
    assert item["credit_state"] == "safe"


def _customer_frame(credit_limit="50.00"):
    from termharvest.cassette import load_cassette

    cassette = load_cassette(fixture_cassette_path())
    frame = next(item.frame for item in cassette.frames if item.expected_screen == "demo_customer")
    rows = list(frame.rows)
    # overwrite the CREDIT LIMIT value cell (label row 8, value starts at col 23)
    line = rows[8]
    rows[8] = (line[:23] + credit_limit).ljust(80)[:80]
    return ScreenFrame.from_rows(rows)


def fixture_cassette_path():
    return "/workspace/fixtures/cassettes/demo_replay.jsonl"
