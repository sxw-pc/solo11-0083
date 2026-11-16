"""Generation publishing protocol: snapshots, identity, crash cut points, recovery."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from termharvest.clock import FixedClock
from termharvest.errors import HarvestError
from termharvest.pipeline import run_pipeline
from termharvest.report import (
    MANIFEST_NAME,
    POINTER_NAME,
    STAGING_DIRNAME,
    current_pointer,
    generation_dir,
    prepare_reports,
    recover_artifacts,
    staging_dir,
)
from termharvest.store import (
    GEN_ABANDONED,
    GEN_PUBLISHED,
    REL_INSERTED,
    REL_RETIRED,
    REL_UNCHANGED,
    REL_UPDATED,
    Store,
    generation_id_for,
)


def _write_cassette(path: Path, session_id: str, frames: list[tuple[int, str, str, str]]) -> None:
    lines = [json.dumps({"record_type": "header", "version": 1, "session_id": session_id, "captured_by": "replay-fixture"}, sort_keys=True)]
    for seq, step_id, screen_id, text in frames:
        lines.append(json.dumps({"record_type": "frame", "frame_seq": seq, "step_id": step_id, "expected_screen": screen_id, "frame_sha256": None, "text": text}, sort_keys=True))
    lines.append(json.dumps({"record_type": "trailer", "frame_count": len(frames)}, sort_keys=True))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fixture_frames(demo_paths):
    data = [json.loads(line) for line in demo_paths["replay_cassette"].read_text(encoding="utf-8").splitlines()]
    return [(r["frame_seq"], r["step_id"], r["expected_screen"], r["text"]) for r in data if r.get("record_type") == "frame"]


# --------------------------------------------------------------------- snapshots
def test_first_failure_shows_no_published_snapshot(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    store = Store(tmp_path / "db.sqlite")
    try:
        with pytest.raises(HarvestError) as caught:
            run_pipeline(
                session=demo_session,
                screens=demo_screens,
                validation_config=demo_validation,
                store=store,
                output_dir=tmp_path / "out",
                clock=FixedClock(),
                mode="replay",
                cassette_path=demo_paths["malformed"],
            )
        assert caught.value.code == "E_FRAME_MALFORMED"
        status = store.snapshot_status()
        assert status["published"] is False
        assert status["current_generation_id"] is None
        assert store.records() == []
        assert store.count_records() == 0
        assert len(status["abandoned_generations"]) == 1
        assert current_pointer(tmp_path / "out") is None
        # No staging directory is left behind; failure report is quarantined separately.
        assert not (tmp_path / "out" / STAGING_DIRNAME).exists()
        failed = list((tmp_path / "out" / "failed").iterdir())
        assert len(failed) == 1
        summary = json.loads((failed[0] / "report_summary.json").read_text(encoding="utf-8"))
        assert summary["status"] == "failed"
        assert summary["error_codes"] == ["E_FRAME_MALFORMED"]
    finally:
        store.close()


def test_failed_rerun_keeps_previous_published_generation_effective(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    store = Store(tmp_path / "db.sqlite")
    try:
        first = run_pipeline(
            session=demo_session, screens=demo_screens, validation_config=demo_validation,
            store=store, output_dir=tmp_path / "out", clock=FixedClock("2026-09-01T00:00:00.000Z"),
            mode="replay", cassette_path=demo_paths["replay_cassette"], evidence_dir=tmp_path / "ev1",
        )
        gen1 = first["generation_id"]
        assert store.current_generation_id() == gen1
        original_rows = store.records()
        assert len(original_rows) == 6

        with pytest.raises(HarvestError):
            run_pipeline(
                session=demo_session, screens=demo_screens, validation_config=demo_validation,
                store=store, output_dir=tmp_path / "out", clock=FixedClock("2026-09-02T00:00:00.000Z"),
                mode="replay", cassette_path=demo_paths["malformed"], evidence_dir=tmp_path / "ev2",
            )
        # Effective snapshot is still the complete previous generation.
        assert store.current_generation_id() == gen1
        assert store.records() == original_rows
        assert store.count_records() == 6
        pointer = current_pointer(tmp_path / "out")
        assert pointer["generation_id"] == gen1
        # The failed run is reviewable as an abandoned generation with zero visible rows.
        failed_run = [row for row in store.runs() if row["status"] == "failed"]
        assert len(failed_run) == 1
        failed_gen = store.generation_for_run(failed_run[0]["run_id"])
        assert failed_gen["status"] == GEN_ABANDONED
        assert failed_gen["parent_generation_id"] == gen1
        assert store.count_records(generation_id=failed_gen["generation_id"]) == 0
    finally:
        store.close()


# ------------------------------------------------------------- identity changes
def test_changed_disappearing_and_duplicate_keys_have_explicit_identity(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    frames = _fixture_frames(demo_paths)
    # Second cassette keeps all four frames (replay requires exact step
    # coverage) but rewrites page 2: one bill amount changes and one bill row
    # disappears, which must show up as updated vs retired identities.
    seq, step, screen, page2 = frames[3]
    # Rewrite fixed-width (80 column) rows so every replacement preserves the
    # frame geometry. B000105's row is fully blanked so its key retires;
    # B000104's amount changes; footer facts stay consistent with the new data
    # (4 bills / 505.00) so batch count and sum checks still pass.
    page_lines = page2.splitlines()

    def _rewrite_line(index: int, new_content: str) -> None:
        page_lines[index] = new_content.ljust(80)

    for index, line in enumerate(page_lines):
        stripped = line.strip()
        if stripped.startswith("B000104"):
            _rewrite_line(index, line.rstrip().replace("124.50", "129.50"))
        elif stripped.startswith("B000105"):
            _rewrite_line(index, "")
        elif stripped.startswith("BATCH COUNT"):
            _rewrite_line(index, " BATCH COUNT  : 4")
        elif stripped.startswith("BATCH TOTAL"):
            _rewrite_line(index, " BATCH TOTAL  : 505.00")
    rewritten_page2 = "\n".join(page_lines)
    changed_frames = frames[:3] + [(seq, step, screen, rewritten_page2)]
    cassette_v1 = tmp_path / "v1.jsonl"
    cassette_v2 = tmp_path / "v2.jsonl"
    _write_cassette(cassette_v1, demo_session.session_id, frames)
    _write_cassette(cassette_v2, demo_session.session_id, changed_frames)

    store = Store(tmp_path / "db.sqlite")
    try:
        first = run_pipeline(
            session=demo_session, screens=demo_screens, validation_config=demo_validation,
            store=store, output_dir=tmp_path / "out", clock=FixedClock("2026-10-01T00:00:00.000Z"),
            mode="replay", cassette_path=cassette_v1, evidence_dir=tmp_path / "ev1",
        )
        gen1 = first["generation_id"]
        first_keys = {row["record_key"] for row in store.records()}
        assert len(first_keys) == 6

        second = run_pipeline(
            session=demo_session, screens=demo_screens, validation_config=demo_validation,
            store=store, output_dir=tmp_path / "out", clock=FixedClock("2026-10-02T00:00:00.000Z"),
            mode="replay", cassette_path=cassette_v2, evidence_dir=tmp_path / "ev2",
        )
        gen2 = second["generation_id"]
        assert gen1 != gen2
        assert store.current_generation_id() == gen2

        changes = store.key_changes(gen2)
        by_relation = {relation: [c["record_key"] for c in changes if c["relation"] == relation] for relation in (REL_INSERTED, REL_UPDATED, REL_UNCHANGED, REL_RETIRED)}
        # B000104's amount changed -> exactly one updated key; B000105 vanished.
        assert len(by_relation[REL_UPDATED]) == 1
        updated = changes[[i for i, c in enumerate(changes) if c["relation"] == REL_UPDATED][0]]
        assert updated["parent_record_sha256"] != updated["record_sha256"]
        assert updated["parent_generation_id"] == gen1
        assert len(by_relation[REL_RETIRED]) == 1
        current_keys = {row["record_key"] for row in store.records()}
        assert set(by_relation[REL_RETIRED]).isdisjoint(current_keys)
        # Gen2 statistics count only gen2 identities.
        assert store.count_records() == len(by_relation[REL_INSERTED]) + len(by_relation[REL_UPDATED]) + len(by_relation[REL_UNCHANGED])
        assert len(current_keys) == store.count_records()
        # Every relation row is keyed deterministically and complete.
        assert {c["record_key"] for c in changes} == first_keys | current_keys
        # Report and ledger agree on the hash and identity set.
        summary = second["payload"]["summary"]
        assert summary["records_sha256"] == store.generation(gen2)["records_sha256"]
        report_relations = summary["key_changes"]
        assert sorted(report_relations["retired"], key=lambda x: x["record_key"]) == sorted(
            [{"record_key": c["record_key"], "screen_id": c["screen_id"], "parent_generation_id": c["parent_generation_id"], "parent_record_sha256": c["parent_record_sha256"], "record_sha256": c["record_sha256"], "occurrence_count": c["occurrence_count"]} for c in changes if c["relation"] == REL_RETIRED],
            key=lambda x: x["record_key"],
        )
        # Old generation remains fully reviewable.
        assert {row["record_key"] for row in store.records(generation_id=gen1)} == first_keys
    finally:
        store.close()


def test_same_screen_repeated_across_pages_keeps_one_identity(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    # The extractor already de-duplicates identical pages (see
    # test_screen_extract); here we verify the ledger identity contract when
    # the same record_key is staged repeatedly within one generation, which is
    # what any cross-page repeat resolves to once it reaches the store.
    clock = FixedClock("2026-11-01T00:00:00.000Z")
    store = Store(tmp_path / "db.sqlite")
    try:
        store.start_run("run-1", demo_session.session_id, "replay", clock.now_iso())
        gen_id = store.generation_for_run("run-1")["generation_id"]
        base = {
            "screen_id": "demo_bill_list",
            "frame_seq": 2,
            "frame_sha256": "a" * 64,
            "primary_key": {"bill_id": "B000101"},
            "raw_values": {"bill_id": "B000101", "amount": "100.00"},
            "normalized_values": {"bill_id": "B000101", "amount": "100.00"},
            "record_key": "k-1",
        }
        assert store.stage_record(dict(base), validation_status="valid", validation_errors=[], run_id="run-1", clock=clock) == REL_INSERTED
        repeat = dict(base, frame_seq=3, frame_sha256="c" * 64)
        assert store.stage_record(repeat, validation_status="valid", validation_errors=[], run_id="run-1", clock=clock) == REL_UNCHANGED
        digest, count = store.generation_digest("run-1")
        assert count == 1  # one canonical identity, never counted twice
        store.publish_generation(
            "run-1", status="success", finished_at=clock.now_iso(), record_count=1,
            frame_count=2, error_count=0, error_code=None, summary={"record_count": 1},
            report_manifest={"records_sha256": digest, "files": {}},
        )
        assert store.count_records() == 1
        row = store.records()[0]
        assert row["occurrence_count"] == 2
        change = store.key_changes(gen_id)[0]
        assert change["occurrence_count"] == 2
        assert change["relation"] == REL_INSERTED
    finally:
        store.close()


# ------------------------------------------------------------- crash cut points
def test_cut_point_1_half_written_report_quarantines_and_keeps_old_snapshot(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    output = tmp_path / "out"
    store = Store(tmp_path / "db.sqlite")
    try:
        first = run_pipeline(
            session=demo_session, screens=demo_screens, validation_config=demo_validation,
            store=store, output_dir=output, clock=FixedClock("2026-12-01T00:00:00.000Z"),
            mode="replay", cassette_path=demo_paths["replay_cassette"], evidence_dir=tmp_path / "ev1",
        )
        gen1 = first["generation_id"]
        first_rows = store.records()

        # Second run: crash after some report files land in staging but before
        # the manifest is finalized (simulates write dying mid-way).
        clock2 = FixedClock("2026-12-02T00:00:00.000Z")
        run_id2 = "run-" + __import__("hashlib").sha256(
            "|".join([demo_session.session_id, "replay", clock2.now_iso(), demo_session.target_host, str(demo_session.target_port)]).encode()
        ).hexdigest()[:16]
        gen2 = generation_id_for(run_id2)
        store.start_run(run_id2, demo_session.session_id, "replay", clock2.now_iso())
        half = staging_dir(output, gen2)
        half.mkdir(parents=True, exist_ok=True)
        (half / "report.json").write_text('{"broken": ', encoding="utf-8")  # no manifest -> incomplete
        store.close()  # process dies mid-write

        # New process opens the ledger and reconciles the file system.
        reopened = Store(tmp_path / "db.sqlite")
        try:
            actions = recover_artifacts(output, reopened)
            assert any(a["generation_id"] == gen2 and a["action"] == "quarantined" for a in actions)
            assert reopened.current_generation_id() == gen1
            assert reopened.records() == first_rows
            assert reopened.generation(gen2)["status"] == GEN_ABANDONED
            assert not half.exists()
            quarantined = list((output / "_recovered").iterdir())
            assert quarantined and any(gen2 in item.name for item in quarantined)
            # Pointer still resolves to the complete old report set.
            assert current_pointer(output)["generation_id"] == gen1
        finally:
            reopened.close()
    finally:
        try:
            store.close()
        except Exception:
            pass


def test_cut_point_2_files_complete_db_not_switched_republishes(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    output = tmp_path / "out"
    db_path = tmp_path / "db.sqlite"
    store = Store(db_path)
    first = run_pipeline(
        session=demo_session, screens=demo_screens, validation_config=demo_validation,
        store=store, output_dir=output, clock=FixedClock("2027-01-01T00:00:00.000Z"),
        mode="replay", cassette_path=demo_paths["replay_cassette"], evidence_dir=tmp_path / "ev1",
    )
    gen1 = first["generation_id"]

    # Build a complete second generation in staging WITHOUT committing the ledger
    # switch: stage rows, write a complete manifest, then "die" by closing.
    clock2 = FixedClock("2027-01-02T00:00:00.000Z")
    run_id2 = "run-" + __import__("hashlib").sha256(
        "|".join([demo_session.session_id, "replay", clock2.now_iso(), demo_session.target_host, str(demo_session.target_port)]).encode()
    ).hexdigest()[:16]
    gen2 = generation_id_for(run_id2)
    store.start_run(run_id2, demo_session.session_id, "replay", clock2.now_iso())
    payload = dict(first["payload"])
    payload["summary"] = dict(first["payload"]["summary"])
    payload["summary"]["run_id"] = run_id2
    payload["summary"]["generation_id"] = gen2
    payload["summary"]["parent_generation_id"] = gen1
    for record in payload["records"]:
        store.stage_record(record, validation_status=record["validation_status"], validation_errors=[], run_id=run_id2, clock=clock2)
    digest, _ = store.generation_digest(run_id2)
    payload["summary"]["records_sha256"] = digest
    prepared = prepare_reports(output, payload, generation_id=gen2)
    assert (staging_dir(output, gen2) / MANIFEST_NAME).is_file()
    store.close()  # process dies before publish_generation / publish_reports

    # A new process opens the ledger; the prepared generation is still
    # recoverable because no sweep ran yet.
    reopened = Store(db_path)
    try:
        assert reopened.generation(gen2)["status"] == "prepared"
        actions = recover_artifacts(output, reopened)
        assert any(a["generation_id"] == gen2 and a["action"] == "republished" for a in actions)
        assert reopened.current_generation_id() == gen2
        assert reopened.generation(gen2)["status"] == GEN_PUBLISHED
        assert reopened.generation(gen2)["records_sha256"] == digest
        pointer = current_pointer(output)
        assert pointer["generation_id"] == gen2
        final_dir = generation_dir(output, gen2)
        assert final_dir.is_dir() and (final_dir / MANIFEST_NAME).is_file()
        assert not staging_dir(output, gen2).exists()
        # Hash chain: report pointer, manifest and ledger all agree.
        assert pointer["records_sha256"] == digest
        # Re-running recovery is a no-op.
        assert recover_artifacts(output, reopened) == []
    finally:
        reopened.close()


def test_cut_point_3_db_switched_pointer_missing_rebuilds_pointer(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    output = tmp_path / "out"
    store = Store(tmp_path / "db.sqlite")
    try:
        first = run_pipeline(
            session=demo_session, screens=demo_screens, validation_config=demo_validation,
            store=store, output_dir=output, clock=FixedClock("2027-02-01T00:00:00.000Z"),
            mode="replay", cassette_path=demo_paths["replay_cassette"], evidence_dir=tmp_path / "ev1",
        )
        gen1 = first["generation_id"]
        # Simulate death after DB commit but before pointer replacement.
        pointer_path = output / POINTER_NAME
        pointer_path.unlink()
        final_dir = generation_dir(output, gen1)
        assert final_dir.is_dir()
        assert current_pointer(output) is None

        actions = recover_artifacts(output, store)
        assert any(a["generation_id"] == gen1 and a["action"] == "pointer_rebuilt" for a in actions)
        pointer = current_pointer(output)
        assert pointer["generation_id"] == gen1
        assert pointer["records_sha256"] == store.generation(gen1)["records_sha256"]
        # Re-running recovery is a no-op.
        assert recover_artifacts(output, store) == []
    finally:
        store.close()


def test_recovered_reports_and_ledger_are_consistent_after_rerun(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    output = tmp_path / "out"
    db_path = tmp_path / "db.sqlite"
    store = Store(db_path)
    first = run_pipeline(
        session=demo_session, screens=demo_screens, validation_config=demo_validation,
        store=store, output_dir=output, clock=FixedClock("2027-03-01T00:00:00.000Z"),
        mode="replay", cassette_path=demo_paths["replay_cassette"], evidence_dir=tmp_path / "ev1",
    )
    store.close()
    # Corrupt a staged half-run, then rerun the same successful generation.
    store = Store(db_path)
    clock = FixedClock("2027-03-02T00:00:00.000Z")
    ghost_run = "run-ghost"
    ghost_gen = generation_id_for(ghost_run)
    store.start_run(ghost_run, demo_session.session_id, "replay", clock.now_iso())
    ghost_staging = staging_dir(output, ghost_gen)
    ghost_staging.mkdir(parents=True, exist_ok=True)
    (ghost_staging / "report.md").write_text("half", encoding="utf-8")
    recover_artifacts(output, store)
    assert store.generation(ghost_gen)["status"] == GEN_ABANDONED

    second = run_pipeline(
        session=demo_session, screens=demo_screens, validation_config=demo_validation,
        store=store, output_dir=output, clock=FixedClock("2027-03-01T00:00:00.000Z"),
        mode="replay", cassette_path=demo_paths["replay_cassette"], evidence_dir=tmp_path / "ev2",
    )
    assert second["generation_id"] == first["generation_id"]
    pointer = current_pointer(output)
    final_dir = generation_dir(output, second["generation_id"])
    manifest = json.loads((final_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert pointer["records_sha256"] == manifest["records_sha256"] == store.generation(second["generation_id"])["records_sha256"]
    assert store.count_records() == 6
    store.close()


# ------------------------------------------------------------------- replay data
def test_replay_mode_never_touches_external_host_and_stays_readable(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    store = Store(tmp_path / "db.sqlite")
    try:
        result = run_pipeline(
            session=replace(demo_session, target_host="127.0.0.1", target_port=1),  # nothing listening
            screens=demo_screens, validation_config=demo_validation,
            store=store, output_dir=tmp_path / "out", clock=FixedClock("2027-04-01T00:00:00.000Z"),
            mode="replay", cassette_path=demo_paths["replay_cassette"], evidence_dir=tmp_path / "ev",
        )
        assert result["payload"]["summary"]["mode"] == "replay"
        assert store.current_generation_id() == result["generation_id"]
        # Reopening the ledger (read-only review console) shows exactly one complete gen.
        store.close()
        review = Store(tmp_path / "db.sqlite")
        try:
            status = review.snapshot_status()
            assert status["published"] is True
            assert status["prepared_generations"] == []
            assert review.count_records() == 6
            assert len(review.records()) == 6
        finally:
            review.close()
    finally:
        try:
            store.close()
        except Exception:
            pass


# ------------------------------------------------------------- legacy migration
def test_legacy_ledger_records_remain_readable_as_legacy_generation(tmp_path):
    import sqlite3

    from termharvest.store_schema import SCHEMA

    db = tmp_path / "legacy.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.execute(
        """INSERT INTO records(record_key, screen_id, frame_seq, frame_sha256, primary_key_json,
           raw_values_json, normalized_values_json, record_sha256, validation_status,
           validation_errors_json, first_seen_at, last_seen_at, first_run_id, last_run_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("key-1", "demo_customer", 1, "f" * 64, "{}", "{}", "{}", "r" * 64, "valid", "[]",
         "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z", "run-old", "run-old"),
    )
    con.commit()
    con.close()

    store = Store(db)
    try:
        imported = store.import_legacy_records()
        assert imported is not None
        assert imported["record_count"] == 1
        assert store.current_generation_id() == "gen-legacy"
        rows = store.records()
        assert len(rows) == 1 and rows[0]["record_key"] == "key-1"
        # Idempotent: a second import does nothing once a generation is current.
        assert store.import_legacy_records() is None
    finally:
        store.close()


def test_abandon_after_ledger_publish_retreats_pointer_to_parent(tmp_path):
    clock = FixedClock()
    store = Store(tmp_path / "db.sqlite")
    try:
        store.start_run("run-1", "demo_session", "live", clock.now_iso())
        record = {
            "screen_id": "demo_customer", "frame_seq": 1, "frame_sha256": "a" * 64,
            "primary_key": {"customer_id": "C1"}, "raw_values": {}, "normalized_values": {"x": "1"},
            "record_key": "k1",
        }
        store.stage_record(record, validation_status="valid", validation_errors=[], run_id="run-1", clock=clock)
        gen1 = store.generation_for_run("run-1")["generation_id"]
        digest1, _ = store.generation_digest("run-1")
        store.publish_generation("run-1", status="success", finished_at=clock.now_iso(), record_count=1, frame_count=0, error_count=0, error_code=None, summary={}, report_manifest={})

        store.start_run("run-2", "demo_session", "live", clock.now_iso())
        record2 = dict(record, record_key="k2", normalized_values={"x": "2"})
        store.stage_record(record2, validation_status="valid", validation_errors=[], run_id="run-2", clock=clock)
        digest2, _ = store.generation_digest("run-2")
        gen2 = store.generation_for_run("run-2")["generation_id"]
        store.publish_generation("run-2", status="success", finished_at=clock.now_iso(), record_count=1, frame_count=0, error_count=0, error_code=None, summary={}, report_manifest={})
        assert store.current_generation_id() == gen2

        # Post-publish failure: abandoning gen2 must retreat the pointer to gen1.
        result = store.abandon_generation("run-2", reason="report staging failed", abandoned_at=clock.now_iso(), retreat_pointer=True)
        assert result["current_generation_id"] == gen1
        assert store.current_generation_id() == gen1
        assert store.generation(gen2)["status"] == GEN_ABANDONED
        assert store.count_records() == 1
        assert store.records()[0]["record_key"] == "k1"
        assert digest1 != digest2
    finally:
        store.close()


def test_empty_legacy_ledger_reports_no_snapshot(tmp_path):
    store = Store(tmp_path / "empty.sqlite")
    try:
        assert store.import_legacy_records() is None
        status = store.snapshot_status()
        assert status["published"] is False
        assert store.records() == []
    finally:
        store.close()


def test_report_ledger_and_manifest_hashes_align(tmp_path, demo_session, demo_screens, demo_validation, demo_paths):
    output = tmp_path / "out"
    store = Store(tmp_path / "db.sqlite")
    try:
        result = run_pipeline(
            session=demo_session, screens=demo_screens, validation_config=demo_validation,
            store=store, output_dir=output, clock=FixedClock("2027-05-01T00:00:00.000Z"),
            mode="replay", cassette_path=demo_paths["replay_cassette"], evidence_dir=tmp_path / "ev",
        )
        gen_id = result["generation_id"]
        final_dir = generation_dir(output, gen_id)
        manifest = json.loads((final_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        stored_generation = store.generation(gen_id)

        # One hash binds report rows, ledger generation and file manifest.
        assert result["payload"]["summary"]["records_sha256"] == stored_generation["records_sha256"]
        assert manifest["records_sha256"] == stored_generation["records_sha256"]
        assert current_pointer(output)["records_sha256"] == stored_generation["records_sha256"]

        # The record sets behind the hashes are identical row-for-row.
        report_rows = result["payload"]["records"]
        ledger_rows = store.records()
        assert sorted(r["record_key"] for r in report_rows) == sorted(r["record_key"] for r in ledger_rows)
        by_key = {r["record_key"]: r for r in ledger_rows}
        for row in report_rows:
            assert row["record_sha256"] == by_key[row["record_key"]]["record_sha256"]
            assert row["validation_status"] == by_key[row["record_key"]]["validation_status"]

        # Every manifest file hash still matches the bytes on disk.
        from termharvest.report import manifest_is_complete

        assert manifest_is_complete(final_dir, manifest)
        # The ledger-stored manifest copy matches the file-system manifest.
        assert json.loads(stored_generation["report_manifest_json"]) == manifest
    finally:
        store.close()
