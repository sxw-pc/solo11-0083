"""CLI-level trusted replay: offline keygen, enrollment, signing and strict replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from termharvest.cli import main

def test_offline_signing_and_strict_trusted_replay(tmp_path, demo_paths, repo_root):
    private_path = tmp_path / "recorder.pem"
    public_path = tmp_path / "recorder.pub"
    trust_store = tmp_path / "trust.sqlite"
    signed = tmp_path / "signed.jsonl"
    out = tmp_path / "report"
    db = tmp_path / "ledger.sqlite"

    assert main(["keygen", "--key-id", "offline-a", "--out-private", str(private_path), "--out-public", str(public_path)]) == 0
    assert private_path.stat().st_mode & 0o077 == 0
    public_key = public_path.read_text(encoding="ascii").strip()
    assert "PRIVATE" not in public_key

    assert main(["trust", "enroll-key", "--trust-store", str(trust_store), "--key-id", "offline-a", "--public-key-file", str(public_path)]) == 0

    assert main([
        "sign-cassette",
        "--cassette", str(demo_paths["replay_cassette"]),
        "--out", str(signed),
        "--session", str(repo_root / "contracts/sessions/demo_session.yaml"),
        "--screens", str(repo_root / "contracts/screens"),
        "--key-id", "offline-a",
        "--private-key-file", str(private_path),
        "--record-seq", "3",
    ]) == 0

    cassette_text = signed.read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in cassette_text
    private_material = private_path.read_text(encoding="ascii").splitlines()[1]
    assert private_material not in cassette_text

    exit_code = main([
        "run",
        "--session", str(repo_root / "contracts/sessions/demo_session.yaml"),
        "--screens", str(repo_root / "contracts/screens"),
        "--validators", str(repo_root / "contracts/validators/demo_rules.yaml"),
        "--db", str(db),
        "--out", str(out),
        "--mode", "replay",
        "--cassette", str(signed),
        "--trust-store", str(trust_store),
        "--trust-policy", "any_trusted_recorder",
        "--require-trusted",
        "--log-level", "WARNING",
    ])
    assert exit_code == 0
    summary = json.loads((out / "report_summary.json").read_text(encoding="utf-8"))
    assert summary["trust_status"] == "trusted"
    assert summary["trusted"] is True
    assert summary["trust"]["record_seq"] == 3
    assert summary["trust"]["signers"][0]["key_id"] == "offline-a"
    assert (out / "report.md").read_text(encoding="utf-8").find("offline-a") >= 0

def test_cli_strict_replay_of_v1_exits_nonzero(tmp_path, demo_paths, repo_root):
    exit_code = main([
        "run",
        "--session", str(repo_root / "contracts/sessions/demo_session.yaml"),
        "--screens", str(repo_root / "contracts/screens"),
        "--validators", str(repo_root / "contracts/validators/demo_rules.yaml"),
        "--db", str(tmp_path / "ledger.sqlite"),
        "--out", str(tmp_path / "report"),
        "--mode", "replay",
        "--cassette", str(demo_paths["replay_cassette"]),
        "--require-trusted",
        "--log-level", "WARNING",
    ])
    assert exit_code == 1
    summary = json.loads((tmp_path / "report" / "report_summary.json").read_text(encoding="utf-8"))
    assert summary["trust_status"] == "rejected"
    assert summary["error_codes"] == ["E_TRUST_UNSIGNED"]
