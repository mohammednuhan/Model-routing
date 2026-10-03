"""Deduplication tests (spec 6.4).

Primary key is (message_id, request_id) across ALL log files. Missing ids fall
back to a deterministic key whose kind is recorded. Uncertain records are never
silently merged.
"""

from __future__ import annotations

import json
from pathlib import Path

from tamias.core import scan as scan_mod
from tamias.core.ledger import Ledger, compute_dedup_key
from tamias.sources import claude_code

from conftest import assistant_record, write_jsonl


def test_same_record_in_two_files_is_inserted_once(tmp_path: Path, db_path: Path):
    record = assistant_record(message_id="m-1", request_id="r-1")
    write_jsonl(tmp_path / "copy-a" / "session.jsonl", [record])
    write_jsonl(tmp_path / "copy-b" / "session.jsonl", [record])
    write_jsonl(tmp_path / "copy-c" / "session.jsonl", [record, record])

    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path)

    assert diagnostics.records_inserted == 1
    assert diagnostics.duplicates_skipped == 3
    assert diagnostics.dedup_conflicts == 0

    store = Ledger(db_path).connect()
    assert store.count("request_record") == 1
    store.close()


def test_distinct_ids_produce_distinct_rows(tmp_path: Path, db_path: Path):
    write_jsonl(
        tmp_path / "s.jsonl",
        [
            assistant_record(message_id="m-1", request_id="r-1"),
            assistant_record(message_id="m-2", request_id="r-2"),
            assistant_record(message_id="m-1", request_id="r-2"),
        ],
    )
    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path)
    assert diagnostics.records_inserted == 3
    assert diagnostics.duplicates_skipped == 0


def test_missing_request_id_falls_back_to_message_id(tmp_path: Path, db_path: Path):
    write_jsonl(
        tmp_path / "s.jsonl",
        [
            assistant_record(message_id="m-1", request_id=None),
            assistant_record(message_id="m-1", request_id=None),
        ],
    )
    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path)
    assert diagnostics.records_inserted == 1
    assert diagnostics.duplicates_skipped == 1
    assert diagnostics.dedup_key_kind_counts == {"message_id": 2}

    store = Ledger(db_path).connect()
    row = store.rows()[0]
    assert row["dedup_key_kind"] == "message_id"
    assert row["request_id"] is None
    store.close()


def test_missing_both_ids_falls_back_to_raw_hash(tmp_path: Path, db_path: Path):
    record = assistant_record(message_id="m-1", request_id="r-1")
    record["message"].pop("id")
    record.pop("requestId")
    write_jsonl(tmp_path / "s.jsonl", [record, record])

    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path)
    assert diagnostics.records_inserted == 1
    assert diagnostics.duplicates_skipped == 1
    assert diagnostics.synthetic_records == 1
    assert diagnostics.dedup_key_kind_counts == {"raw_record_hash": 2}

    store = Ledger(db_path).connect()
    assert store.rows()[0]["dedup_key_kind"] == "raw_record_hash"
    store.close()


def test_dedup_key_kind_is_recorded_for_normal_records(tmp_path: Path, db_path: Path):
    write_jsonl(tmp_path / "s.jsonl", [assistant_record(message_id="m-1", request_id="r-1")])
    scan_mod.scan(log_root=tmp_path, db_path=db_path)
    store = Ledger(db_path).connect()
    assert store.rows()[0]["dedup_key_kind"] == "ids"
    store.close()


def test_uncertain_records_are_not_silently_merged(tmp_path: Path, db_path: Path):
    """Same identity, different usage: both rows survive and the clash is logged."""
    write_jsonl(
        tmp_path / "a" / "s.jsonl",
        [assistant_record(message_id="m-1", request_id="r-1", cache_read=1000)],
    )
    write_jsonl(
        tmp_path / "b" / "s.jsonl",
        [assistant_record(message_id="m-1", request_id="r-1", cache_read=9999)],
    )

    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path)
    assert diagnostics.records_inserted == 2
    assert diagnostics.dedup_conflicts == 1
    assert any("dedup conflict" in note for note in diagnostics.notes)

    store = Ledger(db_path).connect()
    rows = store.rows()
    assert len(rows) == 2, "uncertain records must not be merged into one row"
    assert sum(r["dedup_conflict"] for r in rows) == 2
    conflicts = store.conn.execute("SELECT COUNT(*) FROM dedup_conflict").fetchone()[0]
    assert conflicts == 2, "every participant in the clash must be recorded"
    store.close()


def test_rescanning_the_same_tree_adds_nothing(tmp_path: Path, db_path: Path):
    write_jsonl(
        tmp_path / "s.jsonl",
        [assistant_record(message_id="m-1", request_id="r-1"), assistant_record(message_id="m-2", request_id="r-2")],
    )
    first = scan_mod.scan(log_root=tmp_path, db_path=db_path)
    second = scan_mod.scan(log_root=tmp_path, db_path=db_path)

    assert first.records_inserted == 2
    assert second.records_inserted == 0
    assert second.duplicates_skipped == 2

    store = Ledger(db_path).connect()
    assert store.count("request_record") == 2
    assert store.count("scan") == 2
    store.close()


def test_dedup_key_is_stable_across_processes(tmp_path: Path):
    # Deterministic: same inputs -> same key string, no randomness or time.
    a = compute_dedup_key("m-1", "r-1", "f" * 64)
    b = compute_dedup_key("m-1", "r-1", "f" * 64)
    assert a.key == b.key
    # The key must not be confusable across id boundaries.
    assert compute_dedup_key("m-1r", None, "f" * 64).key != a.key


def test_subagent_and_main_records_are_both_retained(tmp_path: Path, db_path: Path):
    write_jsonl(
        tmp_path / "main.jsonl",
        [assistant_record(message_id="m-1", request_id="r-1", is_sidechain=False)],
    )
    write_jsonl(
        tmp_path / "subagent.jsonl",
        [assistant_record(message_id="m-2", request_id="r-1", is_sidechain=True, session_id="session-sub")],
    )
    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path)
    assert diagnostics.records_inserted == 2, "subagent logs are handled independently"
    assert diagnostics.bucket_counts == {"main": 1, "subagent": 1}

    store = Ledger(db_path).connect()
    buckets = sorted(r["request_bucket"] for r in store.rows())
    assert buckets == ["main", "subagent"]
    store.close()
