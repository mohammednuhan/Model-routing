"""Parser tests: metadata extraction, buckets, flags, drop accounting."""

from __future__ import annotations

import json
from pathlib import Path

from tamias.sources import claude_code

from conftest import assistant_record, user_record, write_jsonl


def _line(record) -> str:
    return json.dumps(record)


def test_usage_fields_are_extracted():
    parsed = claude_code.parse_record(
        _line(assistant_record(message_id="m-1", request_id="r-1", cache_read=1234, cache_creation=56)),
        source_file="f.jsonl",
        source_line=1,
    )
    assert not isinstance(parsed, claude_code.ParseIssue)
    record = parsed.record
    assert record["input_tokens"] == 12
    assert record["output_tokens"] == 34
    assert record["cache_read_tokens"] == 1234
    assert record["cache_write_tokens"] == 56
    assert record["model_id"] == "claude-sonnet-4-5"
    assert record["agent_version"] == "1.0.99"
    assert record["request_id"] == "r-1"
    assert record["message_id"] == "m-1"


def test_cache_write_split_is_recorded_when_present():
    parsed = claude_code.parse_record(
        _line(
            assistant_record(
                message_id="m-1",
                request_id="r-1",
                cache_creation=900,
                cache_creation_5m=700,
                cache_creation_1h=200,
            )
        ),
        source_file="f.jsonl",
        source_line=1,
    )
    record = parsed.record
    assert record["cache_write_5m_tokens"] == 700
    assert record["cache_write_1h_tokens"] == 200
    assert record["cache_write_total_tokens"] == 900


def test_cache_write_split_absent_stays_null():
    parsed = claude_code.parse_record(
        _line(assistant_record(message_id="m-1", request_id="r-1", cache_creation=900)),
        source_file="f.jsonl",
        source_line=1,
    )
    assert parsed.record["cache_write_5m_tokens"] is None
    assert parsed.record["cache_write_1h_tokens"] is None
    assert parsed.record["cache_write_total_tokens"] == 900


def test_effort_absent_yields_unknown_source():
    parsed = claude_code.parse_record(
        _line(assistant_record(message_id="m-1", request_id="r-1")),
        source_file="f.jsonl",
        source_line=1,
    )
    assert parsed.record["effort"] is None
    assert parsed.record["effort_source"] == "unknown"
    assert parsed.record["effort_mechanism"] == "unknown"


def test_effort_present_is_recorded_as_recorded():
    parsed = claude_code.parse_record(
        _line(assistant_record(message_id="m-1", request_id="r-1", effort="xhigh")),
        source_file="f.jsonl",
        source_line=1,
    )
    assert parsed.record["effort"] == "xhigh"
    assert parsed.record["effort_source"] == "recorded"


def test_sidechain_records_go_to_subagent_bucket():
    parsed = claude_code.parse_record(
        _line(assistant_record(message_id="m-1", request_id="r-1", is_sidechain=True)),
        source_file="f.jsonl",
        source_line=1,
    )
    assert parsed.record["is_sidechain"] == 1
    assert parsed.record["request_bucket"] == "subagent"


def test_main_records_go_to_main_bucket():
    parsed = claude_code.parse_record(
        _line(assistant_record(message_id="m-1", request_id="r-1")),
        source_file="f.jsonl",
        source_line=1,
    )
    assert parsed.record["request_bucket"] == "main"


def test_zero_usage_record_is_flagged_not_dropped():
    parsed = claude_code.parse_record(
        _line(
            assistant_record(
                message_id="m-1",
                request_id="r-1",
                input_tokens=0,
                output_tokens=0,
                cache_read=0,
                cache_creation=0,
            )
        ),
        source_file="f.jsonl",
        source_line=1,
    )
    assert not isinstance(parsed, claude_code.ParseIssue)
    assert parsed.is_zero_usage is True
    assert parsed.record["is_zero_usage"] is True


def test_record_without_ids_is_flagged_synthetic():
    record = assistant_record(message_id="m-1", request_id="r-1")
    record["message"].pop("id")
    record.pop("requestId")
    parsed = claude_code.parse_record(_line(record), source_file="f.jsonl", source_line=1)
    assert parsed.is_synthetic is True
    assert parsed.record["request_bucket"] == "unknown"


def test_record_without_usage_is_reported_as_issue():
    outcome = claude_code.parse_record(
        _line(user_record(message_id="u-1")), source_file="f.jsonl", source_line=7
    )
    assert isinstance(outcome, claude_code.ParseIssue)
    assert outcome.reason == "no_usage_block"
    assert outcome.source_line == 7
    assert outcome.record_type == "user"


def test_usage_block_without_token_fields_is_reported():
    outcome = claude_code.parse_record(
        _line({"type": "assistant", "usage": {"unrelated": 1}}),
        source_file="f.jsonl",
        source_line=1,
    )
    assert isinstance(outcome, claude_code.ParseIssue)
    assert outcome.reason == "usage_without_token_fields"


def test_malformed_json_is_reported_not_raised():
    outcome = claude_code.parse_record("{oops", source_file="f.jsonl", source_line=3)
    assert isinstance(outcome, claude_code.ParseIssue)
    assert outcome.reason == "malformed_json"
    assert outcome.raw_record_hash


def test_unknown_record_types_are_counted_as_unsupported(tmp_path: Path):
    path = write_jsonl(
        tmp_path / "s.jsonl",
        [
            assistant_record(message_id="m-1", request_id="r-1"),
            {"type": "brand-new-type", "message": {"usage": {"input_tokens": 5}}},
        ],
    )
    result = claude_code.parse_file(path)
    assert result.record_type_counts["brand-new-type"] == 1
    assert "brand-new-type" not in claude_code.KNOWN_RECORD_TYPES
    assert len(result.records) == 2  # still parsed, flagged by the caller


def test_tool_names_are_kept_but_arguments_are_not():
    parsed = claude_code.parse_record(
        _line(assistant_record(message_id="m-1", request_id="r-1", with_tool_use=True)),
        source_file="f.jsonl",
        source_line=1,
    )
    assert json.loads(parsed.record["tool_names"]) == ["Read", "Bash"]


def test_observed_key_paths_are_reported_without_values(tmp_path: Path):
    path = write_jsonl(tmp_path / "s.jsonl", [assistant_record(message_id="m-1", request_id="r-1")])
    result = claude_code.parse_file(path)
    keys = result.observed_keys["assistant"]
    assert "message.usage" in keys
    assert "requestId" in keys
    # Key paths only; no values leak into this structure.
    assert all(isinstance(k, str) for k in keys)


def test_parser_records_its_own_version_and_schema_status():
    parsed = claude_code.parse_record(
        _line(assistant_record(message_id="m-1", request_id="r-1")),
        source_file="f.jsonl",
        source_line=1,
    )
    assert parsed.record["parser_version"] == claude_code.PARSER_VERSION
    assert parsed.record["schema_status"] == claude_code.SCHEMA_STATUS == "unverified"


def test_iter_log_files_is_sorted_and_recursive(tmp_path: Path):
    write_jsonl(tmp_path / "b" / "second.jsonl", [])
    write_jsonl(tmp_path / "a" / "first.jsonl", [])
    (tmp_path / "a" / "ignored.txt").write_text("x", encoding="utf-8")
    found = claude_code.iter_log_files(tmp_path)
    assert [p.name for p in found] == ["first.jsonl", "second.jsonl"]


def test_iter_log_files_on_missing_root_returns_empty(tmp_path: Path):
    assert claude_code.iter_log_files(tmp_path / "nope") == []
