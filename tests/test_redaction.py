from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from termharvest.capture import CapturedFrame
from termharvest.cli import main as cli_main
from termharvest.clock import FixedClock
from termharvest.config_types import Expectation, Locator, RetryConfig, ScreenContract, ScreenField, SessionContract
from termharvest.errors import HarvestError
from termharvest.pipeline import run_pipeline
from termharvest.redaction import (
    REDACTION_RULES_VERSION,
    contract_digest,
    export_redacted_package,
    load_redacted_package,
    verify_redacted_package,
)
from termharvest.screen import ScreenFrame
from termharvest.store import Store

TOKEN = "SYNTH-PLACEHOLDER-NOT-A-SECRET"

# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

def make_frame(rows: dict[int, str]) -> ScreenFrame:
    grid = [" ".ljust(80) for _ in range(24)]
    for index, text in rows.items():
        grid[index] = text.ljust(80)[:80]
    return ScreenFrame.from_rows(grid)

def label_locator(label: str, search_rows: list[int], *, direction: str = "right", length: int = 16, row_offset: int = 1, col: int | None = None) -> Locator:
    return Locator("label", {"label": label, "search_rows": search_rows, "direction": direction, "length": length, "row_offset": row_offset, "col": col})

def token_frame(marker: str | None = None) -> ScreenFrame:
    rows = {
        0: " SYNTHETIC OPERATIONS CONSOLE / CUSTOMER PROFILE",
        3: " CUSTOMER ID         : C00042",
        5: " CITY                : HARBORVIEW",
        22: " OPERATOR TOKEN      : " + TOKEN,
    }
    if marker is not None:
        rows[22] = rows[22].ljust(66) + marker
    return make_frame(rows)

def token_contract(*, fingerprint: str | None = None, token_search_rows: list[int] | None = None, with_flag_field: bool = False) -> ScreenContract:
    fields = [
        ScreenField("customer_id", label_locator("CUSTOMER ID", [3, 3], length=16)),
        ScreenField("city", label_locator("CITY", [5, 5], length=30)),
        ScreenField("operator_token", label_locator("OPERATOR TOKEN", token_search_rows or [22, 22], length=40), sensitive=True),
    ]
    if with_flag_field:
        fields.append(ScreenField("flag", Locator("absolute", {"row": 22, "col": 66, "length": 4})))
    return ScreenContract(
        screen_id="token_screen",
        match=("CUSTOMER PROFILE", "CUSTOMER ID"),
        fields=tuple(fields),
        summary_fields=(),
        list_region=None,
        primary_key=("customer_id",),
        fingerprint=fingerprint,
        source_file="<test>",
    )

def single_frame_session(screen_id: str) -> SessionContract:
    return SessionContract(
        session_id=f"session_{screen_id}",
        target_host="127.0.0.1",
        target_port=19771,
        start=Expectation(screen_id=screen_id, contains=()),
        steps=(),
        paging=None,
        retry=RetryConfig(),
        source_file="<test>",
    )

def export_package(tmp_path: Path, frame: ScreenFrame, contract: ScreenContract, *, scope: str = "span", name: str = "pkg.jsonl") -> Path:
    captured = [CapturedFrame(frame_seq=0, step_id="start", expected_screen=contract.screen_id, frame=frame)]
    out = tmp_path / name
    export_redacted_package(out, frames=captured, screens={contract.screen_id: contract}, session=single_frame_session(contract.screen_id), scope=scope)
    return out

def verify_package(path: Path, contract: ScreenContract, *, session: SessionContract | None = None) -> dict:
    return verify_redacted_package(path, screens={contract.screen_id: contract}, session=session or single_frame_session(contract.screen_id))

def read_pkg(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

def write_pkg(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for record in records) + "\n", encoding="utf-8")

def assert_context_is_sanitized(error: HarvestError) -> None:
    assert TOKEN not in json.dumps(error.context, ensure_ascii=False)
    assert TOKEN not in error.message

# ---------------------------------------------------------------------------
# Four locator kinds
# ---------------------------------------------------------------------------

def four_kind_frame() -> ScreenFrame:
    return make_frame(
        {
            0: " SYNTHETIC CONSOLE / PROFILE",
            2: " ABSOLUTE SECRET: AAAABBBB",
            4: " RIGHT SECRET      : CCCC-DDDD",
            6: " NEXTLINE SECRET:",
            7: "  EEEE-FFFF",
            9: " MULTI ONE         : GGGG",
            10: " MULTI TWO         : HHHH",
            12: " CUSTOMER ID         : C00042",
        }
    )

def four_kind_contract() -> ScreenContract:
    return ScreenContract(
        screen_id="four_kind",
        match=("SYNTHETIC CONSOLE", "CUSTOMER ID"),
        fields=(
            ScreenField("customer_id", label_locator("CUSTOMER ID", [12, 12], length=16)),
            ScreenField("abs_secret", Locator("absolute", {"row": 2, "col": 18, "length": 8}), sensitive=True),
            ScreenField("right_secret", label_locator("RIGHT SECRET", [4, 4], length=10), sensitive=True),
            ScreenField("next_secret", label_locator("NEXTLINE SECRET", [6, 6], direction="next_line", row_offset=1, col=2, length=9), sensitive=True),
        ),
        summary_fields=(ScreenField("multi_secret", Locator("multiline", {"rows": [9, 10], "col": 21, "length": 4, "join": " / "}), sensitive=True),),
        list_region=None,
        primary_key=("customer_id",),
        fingerprint=None,
        source_file="<test>",
    )

def test_all_four_locator_kinds_are_redacted(tmp_path):
    contract = four_kind_contract()
    package_path = export_package(tmp_path, four_kind_frame(), contract)
    package_text = package_path.read_text(encoding="utf-8")
    for secret in ("AAAABBBB", "CCCC-DDDD", "EEEE-FFFF", "GGGG", "HHHH"):
        assert secret not in package_text, secret
    for kept in ("C00042", "CUSTOMER ID", "ABSOLUTE SECRET:", "RIGHT SECRET", "MULTI ONE"):
        assert kept in package_text, kept
    package = load_redacted_package(package_path)
    frame = package.frames[0].frame
    assert len(frame.rows) == 24 and all(len(row) == 80 for row in frame.rows)
    assert frame.rows[2][18:26] == "█" * 8
    assert frame.rows[4][21:31] == "█" * 10
    assert frame.rows[7][2:11] == "█" * 9
    assert frame.rows[9][21:25] == "█" * 4
    assert frame.rows[10][21:25] == "█" * 4
    summary = verify_package(package_path, contract)
    assert summary["status"] == "verified"
    assert summary["records"] == 1
    assert sorted(summary["sensitive_skipped"]) == ["abs_secret", "multi_secret", "next_secret", "right_secret"]

# ---------------------------------------------------------------------------
# Label ambiguity and out-of-bounds locators
# ---------------------------------------------------------------------------

def test_label_on_multiple_rows_is_ambiguous(tmp_path):
    frame = make_frame(
        {
            0: " SYNTHETIC OPERATIONS CONSOLE / CUSTOMER PROFILE",
            3: " CUSTOMER ID         : C00042",
            20: " OPERATOR TOKEN      : FIRST-VALUE",
            22: " OPERATOR TOKEN      : " + TOKEN,
        }
    )
    contract = token_contract(token_search_rows=[20, 22])
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, frame, contract)
    assert caught.value.code == "E_REDACT_LABEL_AMBIGUOUS"
    assert caught.value.context["rows"] == [20, 22]
    assert caught.value.context["field"] == "operator_token"
    assert_context_is_sanitized(caught.value)

def test_label_repeated_on_same_row_is_ambiguous(tmp_path):
    frame = make_frame(
        {
            0: " SYNTHETIC OPERATIONS CONSOLE / CUSTOMER PROFILE",
            3: " CUSTOMER ID         : C00042",
            22: " OPERATOR TOKEN      : " + TOKEN + "  OPERATOR TOKEN      : OTHER",
        }
    )
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, frame, token_contract())
    assert caught.value.code == "E_REDACT_LABEL_AMBIGUOUS"
    assert caught.value.context["row"] == 22
    assert_context_is_sanitized(caught.value)

def test_absolute_row_out_of_bounds(tmp_path):
    contract = replace(
        token_contract(),
        fields=(ScreenField("secret", Locator("absolute", {"row": 30, "col": 0, "length": 8}), sensitive=True),),
    )
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, token_frame(), contract)
    assert caught.value.code == "E_REDACT_LOCATOR_OUT_OF_BOUNDS"
    assert caught.value.context["row"] == 30

def test_label_right_length_overflow_is_out_of_bounds(tmp_path):
    frame = make_frame({0: " SYNTHETIC OPERATIONS CONSOLE / CUSTOMER PROFILE", 8: " LATE LABEL".ljust(70) + ": VALUE"})
    contract = replace(
        token_contract(),
        match=("CUSTOMER PROFILE",),
        fields=(ScreenField("late_secret", label_locator("LATE LABEL", [8, 8], length=40), sensitive=True),),
    )
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, frame, contract)
    assert caught.value.code == "E_REDACT_LOCATOR_OUT_OF_BOUNDS"
    assert caught.value.context["field"] == "late_secret"

def test_next_line_row_out_of_bounds(tmp_path):
    frame = make_frame({0: " SYNTHETIC OPERATIONS CONSOLE / CUSTOMER PROFILE", 23: " FOOTER LABEL      : X"})
    contract = replace(
        token_contract(),
        match=("CUSTOMER PROFILE",),
        fields=(ScreenField("footer_secret", label_locator("FOOTER LABEL", [23, 23], direction="next_line", row_offset=1, col=2, length=10), sensitive=True),),
    )
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, frame, contract)
    assert caught.value.code == "E_REDACT_LOCATOR_OUT_OF_BOUNDS"

def test_missing_label_is_out_of_bounds(tmp_path):
    contract = token_contract(token_search_rows=[10, 10])
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, token_frame(), contract)
    assert caught.value.code == "E_REDACT_LOCATOR_OUT_OF_BOUNDS"
    assert caught.value.context["label"] == "OPERATOR TOKEN"
    assert_context_is_sanitized(caught.value)

# ---------------------------------------------------------------------------
# Overlap with required non-sensitive content
# ---------------------------------------------------------------------------

def test_sensitive_span_overlapping_non_sensitive_field_is_refused(tmp_path):
    contract = replace(
        token_contract(),
        fields=(
            ScreenField("customer_id", label_locator("CUSTOMER ID", [3, 3], length=16)),
            ScreenField("secret", Locator("absolute", {"row": 3, "col": 23, "length": 6}), sensitive=True),
        ),
    )
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, token_frame(), contract)
    assert caught.value.code == "E_REDACT_OVERLAP"
    assert caught.value.context["conflicts_with"] == "field:customer_id"
    assert caught.value.context["row"] == 3

def test_sensitive_span_overlapping_match_fragment_is_refused(tmp_path):
    contract = replace(
        token_contract(),
        fields=(ScreenField("secret", Locator("absolute", {"row": 3, "col": 0, "length": 5}), sensitive=True),),
    )
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, token_frame(), contract)
    assert caught.value.code == "E_REDACT_OVERLAP"
    assert caught.value.context["conflicts_with"].startswith("match_fragment:")

def test_residual_sensitive_value_elsewhere_is_refused(tmp_path):
    frame = make_frame(
        {
            0: " SYNTHETIC OPERATIONS CONSOLE / CUSTOMER PROFILE",
            3: " CUSTOMER ID         : C00042",
            5: " CITY                : HARBORVIEW",
            18: " " + TOKEN,
            22: " OPERATOR TOKEN      : " + TOKEN,
        }
    )
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, frame, token_contract())
    assert caught.value.code == "E_REDACT_RESIDUAL_VALUE"
    assert caught.value.context["row"] == 18
    assert_context_is_sanitized(caught.value)

# ---------------------------------------------------------------------------
# The two share scopes
# ---------------------------------------------------------------------------

def test_span_scope_preserves_same_row_marker(tmp_path):
    package_path = export_package(tmp_path, token_frame(marker="FLAG: GREEN"), token_contract())
    package_text = package_path.read_text(encoding="utf-8")
    assert TOKEN not in package_text
    assert "FLAG: GREEN" in package_text
    package = load_redacted_package(package_path)
    row22 = package.frames[0].frame.rows[22]
    assert "OPERATOR TOKEN" in row22
    assert "FLAG: GREEN" in row22
    assert "█" in row22

def test_row_scope_hides_same_row_marker(tmp_path):
    package_path = export_package(tmp_path, token_frame(marker="FLAG: GREEN"), token_contract(), scope="row")
    package_text = package_path.read_text(encoding="utf-8")
    assert TOKEN not in package_text
    assert "FLAG: GREEN" not in package_text
    package = load_redacted_package(package_path)
    assert package.frames[0].frame.rows[22] == "█" * 80

def test_both_scopes_replay_identical_non_sensitive_results(tmp_path):
    contract = token_contract()
    frame = token_frame(marker="FLAG: GREEN")
    span_path = export_package(tmp_path, frame, contract, scope="span", name="span.jsonl")
    row_path = export_package(tmp_path, frame, contract, scope="row", name="row.jsonl")
    span_summary = verify_package(span_path, contract)
    row_summary = verify_package(row_path, contract)
    assert span_summary["status"] == row_summary["status"] == "verified"
    assert span_summary["results_sha256"] == row_summary["results_sha256"]
    assert span_summary["records"] == row_summary["records"] == 1

def test_row_scope_refused_when_it_would_hide_required_field(tmp_path):
    contract = token_contract(with_flag_field=True)
    frame = token_frame(marker="FLAG: GREEN")
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, frame, contract, scope="row")
    assert caught.value.code == "E_REDACT_OVERLAP"
    assert caught.value.context["conflicts_with"] == "field:flag"
    span_path = export_package(tmp_path, frame, contract, scope="span")
    assert verify_package(span_path, contract)["status"] == "verified"

# ---------------------------------------------------------------------------
# Package tampering
# ---------------------------------------------------------------------------

def test_tampered_frame_text_is_detected(tmp_path):
    contract = token_contract()
    package_path = export_package(tmp_path, token_frame(), contract)
    records = read_pkg(package_path)
    records[1]["text"] = records[1]["text"].replace("C00042", "X00042")
    write_pkg(package_path, records)
    with pytest.raises(HarvestError) as caught:
        verify_package(package_path, contract)
    assert caught.value.code == "E_REDACT_PACKAGE_MISMATCH"

def test_tampered_recorded_hash_is_detected(tmp_path):
    contract = token_contract()
    package_path = export_package(tmp_path, token_frame(), contract)
    records = read_pkg(package_path)
    records[1]["redacted_sha256"] = "0" * 64
    write_pkg(package_path, records)
    with pytest.raises(HarvestError) as caught:
        verify_package(package_path, contract)
    assert caught.value.code == "E_REDACT_PACKAGE_MISMATCH"

def test_dropped_frame_is_detected(tmp_path):
    contract = token_contract()
    package_path = export_package(tmp_path, token_frame(), contract)
    records = read_pkg(package_path)
    del records[1]
    write_pkg(package_path, records)
    with pytest.raises(HarvestError) as caught:
        verify_package(package_path, contract)
    assert caught.value.code == "E_REDACT_PACKAGE_INCOMPLETE"

def test_trailer_count_mismatch_is_detected(tmp_path):
    contract = token_contract()
    package_path = export_package(tmp_path, token_frame(), contract)
    records = read_pkg(package_path)
    records[-1]["frame_count"] = 99
    write_pkg(package_path, records)
    with pytest.raises(HarvestError) as caught:
        verify_package(package_path, contract)
    assert caught.value.code == "E_REDACT_PACKAGE_INCOMPLETE"

def test_tampered_results_digest_is_detected(tmp_path):
    contract = token_contract()
    package_path = export_package(tmp_path, token_frame(), contract)
    records = read_pkg(package_path)
    records[-1]["results_sha256"] = "0" * 64
    write_pkg(package_path, records)
    with pytest.raises(HarvestError) as caught:
        verify_package(package_path, contract)
    assert caught.value.code == "E_REDACT_PACKAGE_MISMATCH"

def test_non_contiguous_frame_seq_is_detected(tmp_path):
    contract = token_contract()
    package_path = export_package(tmp_path, token_frame(), contract)
    records = read_pkg(package_path)
    records[1]["frame_seq"] = 7
    write_pkg(package_path, records)
    with pytest.raises(HarvestError) as caught:
        verify_package(package_path, contract)
    assert caught.value.code == "E_REDACT_PACKAGE_MISMATCH"

def test_rules_version_mismatch_is_detected(tmp_path):
    contract = token_contract()
    package_path = export_package(tmp_path, token_frame(), contract)
    records = read_pkg(package_path)
    records[0]["rules_version"] = "999"
    write_pkg(package_path, records)
    with pytest.raises(HarvestError) as caught:
        verify_package(package_path, contract)
    assert caught.value.code == "E_REDACT_CONTRACT_MISMATCH"

def test_unknown_scope_in_header_is_detected(tmp_path):
    contract = token_contract()
    package_path = export_package(tmp_path, token_frame(), contract)
    records = read_pkg(package_path)
    records[0]["scope"] = "everything"
    write_pkg(package_path, records)
    with pytest.raises(HarvestError) as caught:
        verify_package(package_path, contract)
    assert caught.value.code == "E_REDACT_PACKAGE_PARSE"

# ---------------------------------------------------------------------------
# Contract drift and session binding
# ---------------------------------------------------------------------------

def test_contract_drift_is_detected(tmp_path):
    contract = token_contract()
    package_path = export_package(tmp_path, token_frame(), contract)
    drifted = replace(contract, match=("CUSTOMER PROFILE", "CUSTOMER ID", "EXTRA FRAGMENT"))
    with pytest.raises(HarvestError) as caught:
        verify_package(package_path, drifted)
    assert caught.value.code == "E_REDACT_CONTRACT_MISMATCH"

def test_header_binds_contract_digest_and_rules_version(tmp_path):
    contract = token_contract()
    package_path = export_package(tmp_path, token_frame(), contract)
    header = read_pkg(package_path)[0]
    assert header["contract_sha256"] == contract_digest({"token_screen": contract})
    assert header["rules_version"] == REDACTION_RULES_VERSION
    assert header["scope"] == "span"

def test_session_mismatch_is_detected(tmp_path):
    contract = token_contract()
    package_path = export_package(tmp_path, token_frame(), contract)
    other_session = replace(single_frame_session(contract.screen_id), session_id="other_session")
    with pytest.raises(HarvestError) as caught:
        verify_package(package_path, contract, session=other_session)
    assert caught.value.code == "E_REDACT_PACKAGE_MISMATCH"

# ---------------------------------------------------------------------------
# Legacy whole-screen fingerprint contracts
# ---------------------------------------------------------------------------

def test_fingerprint_contract_exports_projection_and_verifies(tmp_path):
    raw = token_frame()
    contract = token_contract(fingerprint="sha256:" + raw.sha256)
    package_path = export_package(tmp_path, raw, contract)
    package = load_redacted_package(package_path)
    record = package.frames[0]
    assert record.projection_sha256 is not None
    assert record.source_frame_sha256 == raw.sha256
    assert record.redacted_sha256 != raw.sha256
    # the original whole-screen fingerprint is never compared to the redacted frame
    summary = verify_package(package_path, contract)
    assert summary["status"] == "verified"

def test_fingerprint_contract_projection_tamper_is_detected(tmp_path):
    raw = token_frame()
    contract = token_contract(fingerprint="sha256:" + raw.sha256)
    package_path = export_package(tmp_path, raw, contract)
    records = read_pkg(package_path)
    tampered = records[1]["text"].replace("C00042", "X00042")
    records[1]["text"] = tampered
    records[1]["redacted_sha256"] = hashlib.sha256(tampered.encode("utf-8")).hexdigest()
    write_pkg(package_path, records)
    with pytest.raises(HarvestError) as caught:
        verify_package(package_path, contract)
    assert caught.value.code == "E_REDACT_PACKAGE_MISMATCH"

def test_fingerprint_contract_with_unresolvable_locator_is_rejected(tmp_path):
    raw = make_frame(
        {
            0: " SYNTHETIC OPERATIONS CONSOLE / CUSTOMER PROFILE",
            3: " CUSTOMER ID         : C00042",
            20: " OPERATOR TOKEN      : FIRST-VALUE",
            22: " OPERATOR TOKEN      : " + TOKEN,
        }
    )
    contract = token_contract(fingerprint="sha256:" + raw.sha256, token_search_rows=[20, 22])
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, raw, contract)
    assert caught.value.code == "E_REDACT_LABEL_AMBIGUOUS"

def test_fingerprint_mismatch_on_raw_frame_is_rejected(tmp_path):
    contract = token_contract(fingerprint="sha256:" + "0" * 64)
    with pytest.raises(HarvestError) as caught:
        export_package(tmp_path, token_frame(), contract)
    assert caught.value.code == "E_SCREEN_FINGERPRINT_MISMATCH"

# ---------------------------------------------------------------------------
# End to end: pipeline export plus offline verification
# ---------------------------------------------------------------------------

def test_pipeline_exports_redacted_package_in_both_scopes(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    digests: dict[str, str] = {}
    for scope in ("span", "row"):
        package_path = tmp_path / f"pkg_{scope}.jsonl"
        store = Store(tmp_path / f"ledger_{scope}.sqlite")
        try:
            run_pipeline(
                session=demo_session,
                screens=demo_screens,
                validation_config=demo_validation,
                store=store,
                output_dir=tmp_path / f"report_{scope}",
                mode="replay",
                cassette_path=demo_paths["replay_cassette"],
                redact_package=package_path,
                redact_scope=scope,
                clock=FixedClock("2026-03-02T12:00:00.000Z"),
                evidence_dir=tmp_path / f"evidence_{scope}",
            )
        finally:
            store.close()
        package_text = package_path.read_text(encoding="utf-8")
        assert TOKEN not in package_text
        assert "SYNTH-PLACEHOLDER" not in package_text
        report_text = (tmp_path / f"report_{scope}" / "report.json").read_text(encoding="utf-8")
        assert TOKEN not in report_text
        package = load_redacted_package(package_path)
        assert len(package.frames) == 4
        assert all(len(frame.frame.rows) == 24 for frame in package.frames)
        main_menu = package.frames[0]
        assert main_menu.contracted is False
        assert main_menu.source_frame_sha256 == main_menu.redacted_sha256
        customer = next(frame for frame in package.frames if frame.expected_screen == "demo_customer")
        row22 = customer.frame.rows[22]
        if scope == "span":
            assert "OPERATOR TOKEN" in row22
        else:
            assert row22 == "█" * 80
        summary = verify_redacted_package(package_path, screens=demo_screens, session=demo_session, validation_config=demo_validation)
        assert summary["status"] == "verified"
        assert summary["frames"] == 4
        assert summary["records"] == 6
        assert summary["sensitive_skipped"] == ["operator_token"]
        assert summary["validation_ok"] is True
        digests[scope] = summary["results_sha256"]
    assert digests["span"] == digests["row"]

def test_cli_run_and_verify_redacted(tmp_path, repo_root, demo_paths):
    package_path = tmp_path / "pkg.jsonl"
    run_rc = cli_main(
        [
            "run",
            "--session",
            str(repo_root / "contracts/sessions/demo_session.yaml"),
            "--screens",
            str(repo_root / "contracts/screens"),
            "--validators",
            str(repo_root / "contracts/validators/demo_rules.yaml"),
            "--db",
            str(tmp_path / "ledger.sqlite"),
            "--out",
            str(tmp_path / "report"),
            "--mode",
            "replay",
            "--cassette",
            str(demo_paths["replay_cassette"]),
            "--redact-package",
            str(package_path),
            "--redact-scope",
            "span",
        ]
    )
    assert run_rc == 0
    assert TOKEN not in package_path.read_text(encoding="utf-8")
    verify_rc = cli_main(
        [
            "verify-redacted",
            "--package",
            str(package_path),
            "--screens",
            str(repo_root / "contracts/screens"),
            "--session",
            str(repo_root / "contracts/sessions/demo_session.yaml"),
            "--validators",
            str(repo_root / "contracts/validators/demo_rules.yaml"),
        ]
    )
    assert verify_rc == 0
