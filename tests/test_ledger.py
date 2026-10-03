"""Ledger schema and normalisation tests (spec section 6.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tamias.core.ledger import (
    DEDUP_KEY_KINDS,
    REQUEST_RECORD_FIELDS,
    Ledger,
    compute_dedup_key,
    normalize_record,
)
from tamias.sources import claude_code


def _minimal_record(**overrides):
    record = {name: None for name in REQUEST_RECORD_FIELDS}
    record["raw_record_hash"] = "a" * 64
    record["parser_version"] = claude_code.PARSER_VERSION
    record["schema_status"] = claude_code.SCHEMA_STATUS
    record.update(overrides)
    return record


def _open_store(path: str | Path = ":memory:") -> Ledger:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return Ledger(path).connect().initialize()


def test_table_contains_every_spec_6_1_field():
    store = _open_store()
    columns = [d[0] for d in store.conn.execute("SELECT * FROM request_record LIMIT 0").description]
    for field in REQUEST_RECORD_FIELDS:
        assert field in columns, f"spec 6.1 field missing from request_record: {field}"
    store.close()


def test_spec_6_1_field_order_is_preserved():
    assert REQUEST_RECORD_FIELDS[:6] == (
        "run_id",
        "session_id",
        "seq",
        "ts_utc",
        "request_id",
        "message_id",
    )
    assert REQUEST_RECORD_FIELDS[-2:] == ("raw_record_hash", "parser_version")


def test_effort_source_is_unknown_when_effort_absent():
    normalized = normalize_record(_minimal_record())
    assert normalized["effort"] is None
    assert normalized["effort_source"] == "unknown"
    assert normalized["effort_mechanism"] == "unknown"


def test_effort_source_recorded_when_effort_present():
    normalized = normalize_record(_minimal_record(effort="high", effort_source="recorded"))
    assert normalized["effort"] == "high"
    assert normalized["effort_source"] == "recorded"


def test_normalize_does_not_invent_a_recorded_effort_source():
    """Only the parser may claim an effort was recorded."""
    normalized = normalize_record(_minimal_record(effort="high", effort_source=None))
    assert normalized["effort_source"] == "unknown"


def test_absent_fields_stay_null_not_guessed():
    normalized = normalize_record(_minimal_record(request_id="r-1", message_id="m-1"))
    for field in (
        "model_id",
        "cache_read_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "fallback_flag",
        "compaction_signal",
        "cache_miss_reason",
        "test_ran",
        "test_passed",
        "tool_names",
    ):
        assert normalized[field] is None, f"{field} should be NULL, not guessed"


def test_enumerated_fields_fall_back_to_unknown_member():
    normalized = normalize_record(
        _minimal_record(request_bucket="nonsense", model_source="guessed", thinking_mode="x")
    )
    assert normalized["request_bucket"] == "unknown"
    assert normalized["model_source"] == "unknown"
    assert normalized["thinking_mode"] == "unknown"
    assert normalized["cache_diagnostic_source"] == "unknown"


def test_parser_version_and_raw_hash_stored_on_every_record():
    store = _open_store()
    scan_id = store.begin_scan(
        parser_version=claude_code.PARSER_VERSION,
        schema_status=claude_code.SCHEMA_STATUS,
        started_utc="2026-09-30T00:00:00+00:00",
        log_root=None,
        schema_id=claude_code.SCHEMA_ID,
    )
    store.insert_record(
        _minimal_record(request_id="r-1", message_id="m-1"),
        scan_id=scan_id,
        source_file="a.jsonl",
        source_line=1,
    )
    store.insert_record(
        _minimal_record(request_id="r-2", message_id="m-2", raw_record_hash="b" * 64),
        scan_id=scan_id,
        source_file="a.jsonl",
        source_line=2,
    )
    rows = store.rows()
    assert len(rows) == 2
    for row in rows:
        assert row["parser_version"] == claude_code.PARSER_VERSION
        assert len(row["raw_record_hash"]) == 64
    store.close()


def test_ledger_round_trips_through_a_file(tmp_path: Path):
    db = tmp_path / "nested" / "ledger.sqlite3"
    store = _open_store(db)
    scan_id = store.begin_scan(
        parser_version=claude_code.PARSER_VERSION,
        schema_status=claude_code.SCHEMA_STATUS,
        started_utc="2026-09-30T00:00:00+00:00",
        log_root=None,
        schema_id=claude_code.SCHEMA_ID,
    )
    store.insert_record(
        _minimal_record(request_id="r-1", message_id="m-1", cache_read_tokens=42),
        scan_id=scan_id,
        source_file="a.jsonl",
        source_line=1,
    )
    store.commit()
    store.close()

    reopened = Ledger(db).connect()
    rows = reopened.rows()
    assert len(rows) == 1
    assert rows[0]["cache_read_tokens"] == 42
    reopened.close()


def test_dedup_key_kinds_are_declared():
    assert DEDUP_KEY_KINDS == ("ids", "message_id", "request_id", "raw_record_hash")


@pytest.mark.parametrize(
    ("message_id", "request_id", "expected_kind"),
    [
        ("m-1", "r-1", "ids"),
        ("m-1", None, "message_id"),
        ("m-1", "", "message_id"),
        (None, "r-1", "request_id"),
        (None, None, "raw_record_hash"),
    ],
)
def test_compute_dedup_key_is_deterministic(message_id, request_id, expected_kind):
    first = compute_dedup_key(message_id, request_id, "c" * 64)
    second = compute_dedup_key(message_id, request_id, "c" * 64)
    assert first == second
    assert first.kind == expected_kind


def test_normalize_rejects_booleans_in_token_fields():
    # A bool in a token column would silently become 1 token.
    normalized = normalize_record(_minimal_record(input_tokens=True))
    assert normalized["input_tokens"] is None
