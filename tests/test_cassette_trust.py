"""Signed v2 cassette trust: policy, tampering, rollback and compatibility."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from termharvest.cassette import (
    CASSETTE_VERSION_SIGNED,
    compute_contract_digest,
    load_cassette,
    write_signed_cassette,
)
from termharvest.clock import FixedClock
from termharvest.errors import HarvestError
from termharvest.pipeline import run_pipeline
from termharvest.screen import ScreenFrame
from termharvest.signing import LocalSigner, keygen
from termharvest.store import Store
from termharvest.trust import (
    POLICY_ANY_TRUSTED,
    POLICY_TWO_DISTINCT,
    TrustStore,
    normalize_policy,
)

CLOCK = FixedClock("2026-09-01T00:00:00.000Z")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def keys():
    pairs = {}
    for key_id in ("recorder-a", "recorder-b", "recorder-c"):
        pem, public_b64 = keygen()
        pairs[key_id] = {"pem": pem, "public": public_b64, "signer": LocalSigner(pem, key_id=key_id)}
    return pairs

@pytest.fixture()
def trust_store(tmp_path, keys):
    store = TrustStore(tmp_path / "trust.sqlite")
    for key_id, material in keys.items():
        store.enroll_key(key_id, material["public"])
    try:
        yield store
    finally:
        store.close()

def demo_frames(demo_paths):
    return list(load_cassette(demo_paths["replay_cassette"]).frames)

def demo_digest(demo_session, demo_screens):
    return compute_contract_digest(demo_session, demo_screens)

def sign_package(tmp_path, name, frames, *, session_id, record_seq, contract_digest, signers, captured_by="offline-recorder"):
    path = tmp_path / name
    info = write_signed_cassette(
        path,
        frames=[(item.frame_seq, item.step_id, item.expected_screen, item.frame) for item in frames],
        session_id=session_id,
        record_seq=record_seq,
        contract_digest=contract_digest,
        captured_by=captured_by,
        signers=[signer.signer() for signer in signers],
        signed_at=CLOCK.now_iso(),
    )
    return path, info

def replay(tmp_path, name, cassette_path, session, screens, validation, *, trust_store=None, policy=None, require_trusted=False, clock=CLOCK):
    store = Store(tmp_path / f"{name}.sqlite")
    try:
        return run_pipeline(
            session=session,
            screens=screens,
            validation_config=validation,
            store=store,
            output_dir=tmp_path / name,
            clock=clock,
            mode="replay",
            cassette_path=cassette_path,
            evidence_dir=tmp_path / f"{name}_evidence",
            trust_store=trust_store,
            trust_policy=policy,
            require_trusted=require_trusted,
        )
    finally:
        store.close()

def rewrite_lines(path: Path, mutator):
    lines = path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    mutator(records)
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records) + "\n", encoding="utf-8")

# ---------------------------------------------------------------------------
# Policy: single trusted signer
# ---------------------------------------------------------------------------

def test_single_valid_signature_is_trusted_under_any_policy(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    frames = demo_frames(demo_paths)
    path, info = sign_package(
        tmp_path, "single.jsonl", frames,
        session_id=demo_session.session_id, record_seq=0,
        contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
    )
    result = replay(tmp_path, "trusted", path, demo_session, demo_screens, demo_validation, trust_store=trust_store, policy="any_trusted_recorder")
    summary = result["summary"]
    assert summary["trust_status"] == "trusted"
    assert summary["trusted"] is True
    assert summary["trust"]["policy"] == POLICY_ANY_TRUSTED
    assert summary["trust"]["record_seq"] == 0
    assert summary["trust"]["signers"] == [{"key_id": "recorder-a", "public_key_fingerprint": trust_store.get_key("recorder-a").fingerprint}]
    assert info["key_ids"] == ["recorder-a"]

def test_report_shows_policy_signers_fingerprints_and_sequence_without_secrets(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    path, _ = sign_package(
        tmp_path, "display.jsonl", demo_frames(demo_paths),
        session_id=demo_session.session_id, record_seq=4,
        contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
    )
    replay(tmp_path, "display", path, demo_session, demo_screens, demo_validation, trust_store=trust_store)
    payload = json.loads((tmp_path / "display" / "report.json").read_text(encoding="utf-8"))
    trust = payload["trust"]
    assert trust["trusted"] is True
    assert trust["policy"] == POLICY_ANY_TRUSTED
    assert trust["record_seq"] == 4
    assert trust["accepted_record_seq"] == 4
    assert trust["signers"][0]["key_id"] == "recorder-a"
    assert trust["signers"][0]["public_key_fingerprint"].startswith("sha256:")
    for artifact in ("report.json", "report.md", "report.html", "report_summary.json"):
        text = (tmp_path / "display" / artifact).read_text(encoding="utf-8")
        assert "BEGIN PRIVATE KEY" not in text
        assert keys["recorder-a"]["pem"].splitlines()[1] not in text
        assert "SYNTH-PLACEHOLDER" not in text
        assert "OPERATOR TOKEN" not in text
    markdown = (tmp_path / "display" / "report.md").read_text(encoding="utf-8")
    assert "recorder-a" in markdown
    assert trust["signers"][0]["public_key_fingerprint"] in markdown

# ---------------------------------------------------------------------------
# Policy: two distinct signers
# ---------------------------------------------------------------------------

def test_two_signer_policy_accepts_two_distinct_signatures(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    path, _ = sign_package(
        tmp_path, "dual.jsonl", demo_frames(demo_paths),
        session_id=demo_session.session_id, record_seq=1,
        contract_digest=demo_digest(demo_session, demo_screens),
        signers=[keys["recorder-a"]["signer"], keys["recorder-b"]["signer"]],
    )
    result = replay(tmp_path, "dual", path, demo_session, demo_screens, demo_validation, trust_store=trust_store, policy="two_distinct_recorders")
    trust = result["summary"]["trust"]
    assert trust["trust_status"] == "trusted"
    assert trust["policy"] == POLICY_TWO_DISTINCT
    assert [item["key_id"] for item in trust["signers"]] == ["recorder-a", "recorder-b"]
    assert trust_store.highest_record_seq(demo_session.session_id) == 1

def test_single_signature_is_insufficient_for_two_signer_policy_and_does_not_advance(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    frames = demo_frames(demo_paths)
    digest = demo_digest(demo_session, demo_screens)
    only_a, _ = sign_package(
        tmp_path, "only-a.jsonl", frames,
        session_id=demo_session.session_id, record_seq=2,
        contract_digest=digest, signers=[keys["recorder-a"]["signer"]],
    )
    with pytest.raises(HarvestError) as caught:
        replay(tmp_path, "insufficient", only_a, demo_session, demo_screens, demo_validation, trust_store=trust_store, policy="two_distinct_recorders")
    assert caught.value.code == "E_TRUST_INSUFFICIENT_SIGNATURES"
    assert trust_store.highest_record_seq(demo_session.session_id) is None

    # Same sequence, now independently signed by B as well: accepted.
    both, _ = sign_package(
        tmp_path, "both.jsonl", frames,
        session_id=demo_session.session_id, record_seq=2,
        contract_digest=digest,
        signers=[keys["recorder-a"]["signer"], keys["recorder-b"]["signer"]],
    )
    result = replay(tmp_path, "both", both, demo_session, demo_screens, demo_validation, trust_store=trust_store, policy="two_distinct_recorders")
    assert result["summary"]["trust_status"] == "trusted"
    assert trust_store.highest_record_seq(demo_session.session_id) == 2

def test_duplicated_signature_cannot_fake_two_signers(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    path, _ = sign_package(
        tmp_path, "dup.jsonl", demo_frames(demo_paths),
        session_id=demo_session.session_id, record_seq=3,
        contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
    )

    def duplicate(records):
        records[-1]["signatures"] = [records[-1]["signatures"][0], copy.deepcopy(records[-1]["signatures"][0])]

    rewrite_lines(path, duplicate)
    with pytest.raises(HarvestError) as caught:
        load_cassette(path)
    assert caught.value.code == "E_TRUST_DUPLICATE_SIGNER"
    with pytest.raises(HarvestError):
        replay(tmp_path, "dup", path, demo_session, demo_screens, demo_validation, trust_store=trust_store, policy="two_distinct_recorders")
    assert trust_store.highest_record_seq(demo_session.session_id) is None

def test_copied_signature_under_second_key_id_is_rejected(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    path, _ = sign_package(
        tmp_path, "copy.jsonl", demo_frames(demo_paths),
        session_id=demo_session.session_id, record_seq=3,
        contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
    )

    def copy_signature(records):
        forged = copy.deepcopy(records[-1]["signatures"][0])
        forged["key_id"] = "recorder-b"  # B's key_id but A's signature bytes
        records[-1]["signatures"].append(forged)

    rewrite_lines(path, copy_signature)
    with pytest.raises(HarvestError) as caught:
        replay(tmp_path, "copy", path, demo_session, demo_screens, demo_validation, trust_store=trust_store, policy="two_distinct_recorders")
    assert caught.value.code == "E_TRUST_BAD_SIGNATURE"
    assert trust_store.highest_record_seq(demo_session.session_id) is None

# ---------------------------------------------------------------------------
# Tampering
# ---------------------------------------------------------------------------

def test_tampered_content_with_recomputed_frame_hash_is_rejected(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    path, _ = sign_package(
        tmp_path, "tampered.jsonl", demo_frames(demo_paths),
        session_id=demo_session.session_id, record_seq=0,
        contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
    )

    def tamper(records):
        frame_record = next(record for record in records if record["record_type"] == "frame" and record["frame_seq"] == 1)
        changed = frame_record["text"].replace("C00042", "C00043")
        assert changed != frame_record["text"]
        frame_record["text"] = changed
        # Attacker recomputes the per-frame hash so structural checks pass.
        frame_record["frame_sha256"] = ScreenFrame.parse(changed).sha256

    rewrite_lines(path, tamper)
    # Structural + frame-hash validation alone accepts it ...
    structurally_loaded = load_cassette(path)
    assert structurally_loaded.frames[1].frame.text != demo_frames(demo_paths)[1].frame.text
    # ... but the package signature does not.
    with pytest.raises(HarvestError) as caught:
        replay(tmp_path, "tampered", path, demo_session, demo_screens, demo_validation, trust_store=trust_store)
    assert caught.value.code == "E_TRUST_BAD_SIGNATURE"
    assert trust_store.highest_record_seq(demo_session.session_id) is None
    serialized = json.dumps(caught.value.context, ensure_ascii=False)
    assert "C00043" not in serialized
    assert "NORTHWIND" not in serialized

def test_unknown_key_is_rejected(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, keys):
    store = TrustStore(tmp_path / "trust-unknown.sqlite")
    try:
        store.enroll_key("recorder-a", keys["recorder-a"]["public"])
        store.enroll_key("recorder-b", keys["recorder-b"]["public"])
        path, _ = sign_package(
            tmp_path, "unknown.jsonl", demo_frames(demo_paths),
            session_id=demo_session.session_id, record_seq=0,
            contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-c"]["signer"]],
        )
        with pytest.raises(HarvestError) as caught:
            replay(tmp_path, "unknown", path, demo_session, demo_screens, demo_validation, trust_store=store)
        assert caught.value.code == "E_TRUST_UNKNOWN_KEY"
        assert store.highest_record_seq(demo_session.session_id) is None
    finally:
        store.close()

def test_revoked_key_is_rejected_and_does_not_advance(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    path, _ = sign_package(
        tmp_path, "revoked.jsonl", demo_frames(demo_paths),
        session_id=demo_session.session_id, record_seq=5,
        contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
    )
    trust_store.revoke_key("recorder-a", reason="rotated")
    with pytest.raises(HarvestError) as caught:
        replay(tmp_path, "revoked", path, demo_session, demo_screens, demo_validation, trust_store=trust_store)
    assert caught.value.code == "E_TRUST_REVOKED_KEY"
    assert trust_store.highest_record_seq(demo_session.session_id) is None

def test_expired_key_is_rejected_and_does_not_advance(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, keys):
    store = TrustStore(tmp_path / "trust-expiry.sqlite")
    try:
        store.enroll_key("recorder-a", keys["recorder-a"]["public"], valid_until="2026-01-01T00:00:00Z")
        path, _ = sign_package(
            tmp_path, "expired.jsonl", demo_frames(demo_paths),
            session_id=demo_session.session_id, record_seq=0,
            contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
        )
        with pytest.raises(HarvestError) as caught:
            replay(tmp_path, "expired", path, demo_session, demo_screens, demo_validation, trust_store=store)
        assert caught.value.code == "E_TRUST_EXPIRED_KEY"
        assert store.highest_record_seq(demo_session.session_id) is None
    finally:
        store.close()

# ---------------------------------------------------------------------------
# Cross-session / contract misappropriation
# ---------------------------------------------------------------------------

def test_package_for_another_session_is_rejected(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    path, _ = sign_package(
        tmp_path, "other-session.jsonl", demo_frames(demo_paths),
        session_id="another_session", record_seq=0,
        contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
    )
    with pytest.raises(HarvestError) as caught:
        replay(tmp_path, "other-session", path, demo_session, demo_screens, demo_validation, trust_store=trust_store)
    assert caught.value.code == "E_TRUST_SESSION_MISMATCH"
    assert trust_store.highest_record_seq(demo_session.session_id) is None

def test_package_for_another_contract_is_rejected(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    altered_screens = dict(demo_screens)
    altered_screens["demo_customer"] = replace(demo_screens["demo_customer"], primary_key=("customer_name",))
    other_digest = compute_contract_digest(demo_session, altered_screens)
    assert other_digest != demo_digest(demo_session, demo_screens)
    path, _ = sign_package(
        tmp_path, "other-contract.jsonl", demo_frames(demo_paths),
        session_id=demo_session.session_id, record_seq=0,
        contract_digest=other_digest, signers=[keys["recorder-a"]["signer"]],
    )
    with pytest.raises(HarvestError) as caught:
        replay(tmp_path, "other-contract", path, demo_session, demo_screens, demo_validation, trust_store=trust_store)
    assert caught.value.code == "E_TRUST_CONTRACT_MISMATCH"
    assert trust_store.highest_record_seq(demo_session.session_id) is None

# ---------------------------------------------------------------------------
# Rollback / idempotency
# ---------------------------------------------------------------------------

def test_rollback_rejected_and_failure_does_not_advance(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    frames = demo_frames(demo_paths)
    digest = demo_digest(demo_session, demo_screens)
    newest, _ = sign_package(
        tmp_path, "newest.jsonl", frames,
        session_id=demo_session.session_id, record_seq=9,
        contract_digest=digest, signers=[keys["recorder-a"]["signer"]],
    )
    replay(tmp_path, "newest", newest, demo_session, demo_screens, demo_validation, trust_store=trust_store)
    assert trust_store.highest_record_seq(demo_session.session_id) == 9

    older, _ = sign_package(
        tmp_path, "older.jsonl", frames,
        session_id=demo_session.session_id, record_seq=8,
        contract_digest=digest, signers=[keys["recorder-a"]["signer"]],
    )
    with pytest.raises(HarvestError) as caught:
        replay(tmp_path, "older", older, demo_session, demo_screens, demo_validation, trust_store=trust_store)
    assert caught.value.code == "E_TRUST_ROLLBACK"
    assert trust_store.highest_record_seq(demo_session.session_id) == 9

    # A higher sequence with an invalid signature must not advance either.
    bad_high, _ = sign_package(
        tmp_path, "bad-high.jsonl", frames,
        session_id=demo_session.session_id, record_seq=99,
        contract_digest=digest, signers=[keys["recorder-a"]["signer"]],
    )
    def tamper_customer(records):
        frame_record = next(record for record in records if record["record_type"] == "frame" and record["frame_seq"] == 1)
        changed = frame_record["text"].replace("NORTHWIND", "NORTHW1ND")
        assert changed != frame_record["text"]
        frame_record["text"] = changed
        frame_record["frame_sha256"] = ScreenFrame.parse(changed).sha256

    rewrite_lines(bad_high, tamper_customer)
    with pytest.raises(HarvestError) as caught:
        replay(tmp_path, "bad-high", bad_high, demo_session, demo_screens, demo_validation, trust_store=trust_store)
    assert caught.value.code == "E_TRUST_BAD_SIGNATURE"
    assert trust_store.highest_record_seq(demo_session.session_id) == 9

def test_failure_after_verification_does_not_advance_sequence(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys, monkeypatch):
    """A trusted package whose replay fails after verification must not advance."""
    frames = demo_frames(demo_paths)
    path, _ = sign_package(
        tmp_path, "later-failure.jsonl", frames,
        session_id=demo_session.session_id, record_seq=4,
        contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
    )
    import termharvest.pipeline as pipeline_module

    def explode(*_args, **_kwargs):
        raise HarvestError("E_EXTRACT_LOCATOR_OUT_OF_BOUNDS", "simulated extraction failure after successful verification")

    monkeypatch.setattr(pipeline_module, "_extract_all", explode)
    with pytest.raises(HarvestError) as caught:
        replay(tmp_path, "later-failure", path, demo_session, demo_screens, demo_validation, trust_store=trust_store)
    assert caught.value.code == "E_EXTRACT_LOCATOR_OUT_OF_BOUNDS"
    assert trust_store.highest_record_seq(demo_session.session_id) is None


def test_contract_change_after_signing_is_rejected_as_contract_mismatch(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    frames = demo_frames(demo_paths)
    digest = demo_digest(demo_session, demo_screens)
    path, _ = sign_package(
        tmp_path, "reused.jsonl", frames,
        session_id=demo_session.session_id, record_seq=1,
        contract_digest=digest, signers=[keys["recorder-a"]["signer"]],
    )
    # Replay with the same session_id but a changed screen contract: the
    # signature still verifies, but the contract digest does not match.
    changed_screens = dict(demo_screens)
    changed_screens["demo_bill_list"] = replace(demo_screens["demo_bill_list"], fingerprint="sha256:" + "0" * 64)
    with pytest.raises(HarvestError) as caught:
        replay(tmp_path, "reused", path, demo_session, changed_screens, demo_validation, trust_store=trust_store)
    assert caught.value.code == "E_TRUST_CONTRACT_MISMATCH"
    assert trust_store.highest_record_seq(demo_session.session_id) is None


def test_repeat_verification_is_idempotent_and_not_a_new_recording_version(tmp_path, demo_paths, demo_session, demo_screens, trust_store, keys):
    path, _ = sign_package(
        tmp_path, "idem.jsonl", demo_frames(demo_paths),
        session_id=demo_session.session_id, record_seq=6,
        contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
    )
    first = trust_store.verify_and_accept(
        load_cassette(path), policy="any_trusted_recorder",
        session_id=demo_session.session_id, contract_digest=demo_digest(demo_session, demo_screens), clock=CLOCK,
    )
    second = trust_store.verify_and_accept(
        load_cassette(path), policy="any_trusted_recorder",
        session_id=demo_session.session_id, contract_digest=demo_digest(demo_session, demo_screens), clock=CLOCK,
    )
    assert first.trusted and not first.idempotent
    assert second.trusted and second.idempotent
    assert first.accepted_record_seq == second.accepted_record_seq == 6
    assert trust_store.highest_record_seq(demo_session.session_id) == 6

def test_tampered_package_with_already_accepted_sequence_is_a_conflict(tmp_path, demo_paths, demo_session, demo_screens, trust_store, keys):
    frames = demo_frames(demo_paths)
    digest = demo_digest(demo_session, demo_screens)
    path, _ = sign_package(tmp_path, "seq7.jsonl", frames, session_id=demo_session.session_id, record_seq=7, contract_digest=digest, signers=[keys["recorder-a"]["signer"]])
    trust_store.verify_and_accept(load_cassette(path), policy="any_trusted_recorder", session_id=demo_session.session_id, contract_digest=digest, clock=CLOCK)
    # A different, validly signed package at the same sequence must not replace it.
    other, _ = sign_package(
        tmp_path, "seq7-other.jsonl", frames, session_id=demo_session.session_id, record_seq=7,
        contract_digest=digest, signers=[keys["recorder-a"]["signer"], keys["recorder-b"]["signer"]],
    )
    outcome = trust_store.verify_and_accept(load_cassette(other), policy="any_trusted_recorder", session_id=demo_session.session_id, contract_digest=digest, clock=CLOCK)
    assert not outcome.trusted
    assert outcome.error_code == "E_TRUST_SEQUENCE_CONFLICT"
    assert trust_store.highest_record_seq(demo_session.session_id) == 7

# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_replay_of_same_package_keeps_sequence_atomic(tmp_path, demo_paths, demo_session, demo_screens, trust_store, keys):
    path, _ = sign_package(
        tmp_path, "concurrent.jsonl", demo_frames(demo_paths),
        session_id=demo_session.session_id, record_seq=11,
        contract_digest=demo_digest(demo_session, demo_screens),
        signers=[keys["recorder-a"]["signer"], keys["recorder-b"]["signer"]],
    )
    digest = demo_digest(demo_session, demo_screens)
    outcomes: list[object] = []
    barrier = threading.Barrier(2)

    def worker():
        store = TrustStore(tmp_path / "trust.sqlite")
        try:
            barrier.wait(timeout=5)
            outcomes.append(
                store.verify_and_accept(
                    load_cassette(path), policy="two_distinct_recorders",
                    session_id=demo_session.session_id, contract_digest=digest, clock=CLOCK,
                )
            )
        finally:
            store.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(outcomes) == 2
    assert all(outcome.trusted for outcome in outcomes)
    assert sorted(outcome.idempotent for outcome in outcomes) == [False, True]
    assert trust_store.highest_record_seq(demo_session.session_id) == 11

def test_concurrent_distinct_packages_same_sequence_accept_only_one(tmp_path, demo_paths, demo_session, demo_screens, trust_store, keys):
    frames = demo_frames(demo_paths)
    digest = demo_digest(demo_session, demo_screens)
    first, _ = sign_package(tmp_path, "race-a.jsonl", frames, session_id=demo_session.session_id, record_seq=12, contract_digest=digest, signers=[keys["recorder-a"]["signer"]])
    second, _ = sign_package(tmp_path, "race-b.jsonl", frames, session_id=demo_session.session_id, record_seq=12, contract_digest=digest, signers=[keys["recorder-b"]["signer"]])
    outcomes: list[object] = []
    paths = [first, second]
    barrier = threading.Barrier(2)

    def worker(cassette_path):
        store = TrustStore(tmp_path / "trust.sqlite")
        try:
            cassette = load_cassette(cassette_path)
            barrier.wait(timeout=5)
            outcomes.append(store.verify_and_accept(cassette, policy="any_trusted_recorder", session_id=demo_session.session_id, contract_digest=digest, clock=CLOCK))
        finally:
            store.close()

    threads = [threading.Thread(target=worker, args=(path,)) for path in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(outcomes) == 2
    accepted = [outcome for outcome in outcomes if outcome.trusted]
    rejected = [outcome for outcome in outcomes if not outcome.trusted]
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0].error_code == "E_TRUST_SEQUENCE_CONFLICT"
    assert trust_store.highest_record_seq(demo_session.session_id) == 12

# ---------------------------------------------------------------------------
# v1 compatibility and strict mode
# ---------------------------------------------------------------------------

def test_v1_cassette_remains_replayable_but_unauthenticated(tmp_path, demo_paths, demo_session, demo_screens, demo_validation):
    result = replay(tmp_path, "v1", demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation)
    summary = result["summary"]
    assert summary["status"] == "success"
    assert summary["trust_status"] == "unauthenticated"
    assert summary["trusted"] is False
    assert summary["trust"]["cassette_version"] == 1
    assert summary["trust"]["signers"] == []
    assert summary["record_count"] == 6
    markdown = (tmp_path / "v1" / "report.md").read_text(encoding="utf-8")
    assert "unsigned v1 cassette replay" in markdown
    assert "unauthenticated" in markdown

def test_strict_mode_refuses_v1_before_extraction(tmp_path, demo_paths, demo_session, demo_screens, demo_validation):
    store = Store(tmp_path / "strict.sqlite")
    try:
        with pytest.raises(HarvestError) as caught:
            run_pipeline(
                session=demo_session, screens=demo_screens, validation_config=demo_validation,
                store=store, output_dir=tmp_path / "strict", clock=CLOCK, mode="replay",
                cassette_path=demo_paths["replay_cassette"], require_trusted=True,
            )
        assert caught.value.code == "E_TRUST_UNSIGNED"
        assert store.count_frames() == 0
        assert store.count_records() == 0
        provenance = store.provenance(store.runs()[0]["run_id"])
        assert provenance["trust_status"] == "rejected"
        assert provenance["trusted"] is False
        report_text = (tmp_path / "strict" / "report.json").read_text(encoding="utf-8")
        summary = json.loads(report_text)["summary"]
        assert summary["trust_status"] == "rejected"
        assert summary["error_codes"] == ["E_TRUST_UNSIGNED"]
        # Failing stderr/report diagnostics must not echo raw screen text.
        assert "NORTHWIND" not in report_text
        assert "SYNTH-PLACEHOLDER" not in report_text
    finally:
        store.close()

def test_v2_replay_matches_v1_records_when_trusted(tmp_path, demo_paths, demo_session, demo_screens, demo_validation, trust_store, keys):
    v1 = replay(tmp_path, "baseline", demo_paths["replay_cassette"], demo_session, demo_screens, demo_validation)
    path, _ = sign_package(
        tmp_path, "signed.jsonl", demo_frames(demo_paths),
        session_id=demo_session.session_id, record_seq=0,
        contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
    )
    v2 = replay(tmp_path, "signed", path, demo_session, demo_screens, demo_validation, trust_store=trust_store)
    assert v1["payload"]["records"] == v2["payload"]["records"]
    assert v2["summary"]["trust_status"] == "trusted"

def test_trust_store_persists_only_public_material(tmp_path, demo_paths, demo_session, demo_screens, trust_store, keys):
    path, _ = sign_package(
        tmp_path, "public-only.jsonl", demo_frames(demo_paths),
        session_id=demo_session.session_id, record_seq=0,
        contract_digest=demo_digest(demo_session, demo_screens), signers=[keys["recorder-a"]["signer"]],
    )
    trust_store.verify_and_accept(load_cassette(path), policy="any_trusted_recorder", session_id=demo_session.session_id, contract_digest=demo_digest(demo_session, demo_screens), clock=CLOCK)
    dump = json.dumps(trust_store.list_keys(), ensure_ascii=False)
    assert "PRIVATE" not in dump
    assert keys["recorder-a"]["pem"].splitlines()[1] not in dump
    cassette_text = path.read_text(encoding="utf-8")
    assert "PRIVATE" not in cassette_text
    assert keys["recorder-a"]["pem"].splitlines()[1] not in cassette_text
    assert load_cassette(path).version == CASSETTE_VERSION_SIGNED

def test_policy_alias_and_normalization():
    assert normalize_policy("any_trusted_recorder") == POLICY_ANY_TRUSTED
    assert normalize_policy("two") == POLICY_TWO_DISTINCT
    with pytest.raises(HarvestError) as caught:
        normalize_policy("trust-me")
    assert caught.value.code == "E_TRUST_INVALID"
