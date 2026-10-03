"""End-to-end tests for ``tamias scan`` diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

from tamias.core import scan as scan_mod
from tamias.core.ledger import Ledger

from conftest import assistant_record, write_jsonl


def test_scan_reports_parser_diagnostics(tmp_path: Path, db_path: Path):
    write_jsonl(
        tmp_path / "s.jsonl",
        [
            assistant_record(message_id="m-1", request_id="r-1"),
            assistant_record(message_id="m-1", request_id="r-1"),
            assistant_record(message_id="m-2", request_id="r-2", is_sidechain=True),
        ],
    )
    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path).as_dict()

    assert diagnostics["parser_version"] == "claude-code-parser/0.1.0"
    assert diagnostics["schema_status"] == "unverified"
    assert diagnostics["files_found"] == 1
    assert diagnostics["lines_read"] == 3
    assert diagnostics["records_seen"] == 3
    assert diagnostics["records_inserted"] == 2
    assert diagnostics["duplicates_skipped"] == 1
    assert diagnostics["bucket_counts"] == {"main": 1, "subagent": 1}
    assert diagnostics["record_type_counts"]["assistant"] == 3


def test_scan_counts_and_reports_dropped_records(tmp_path: Path, malformed_log: Path, db_path: Path):
    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path).as_dict()

    assert diagnostics["records_inserted"] == 1
    assert diagnostics["dropped_records"] == 2
    assert diagnostics["drop_reason_counts"] == {
        "malformed_json": 1,
        "no_usage_block": 1,
    }
    assert diagnostics["unsupported_records"] == 1  # 'mystery-type'
    assert any("not written to the ledger" in note for note in diagnostics["notes"])

    store = Ledger(db_path).connect()
    rows = store.conn.execute(
        "SELECT source_line, reason, record_type FROM dropped_record ORDER BY source_line"
    ).fetchall()
    store.close()
    assert [(r["source_line"], r["reason"]) for r in rows] == [(2, "malformed_json"), (4, "no_usage_block")]
    assert rows[0]["record_type"] is None


def test_scan_flags_zero_usage_records(tmp_path: Path, db_path: Path):
    write_jsonl(
        tmp_path / "s.jsonl",
        [
            assistant_record(
                message_id="m-1",
                request_id="r-1",
                input_tokens=0,
                output_tokens=0,
                cache_read=0,
                cache_creation=0,
            ),
            assistant_record(message_id="m-2", request_id="r-2"),
        ],
    )
    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path).as_dict()
    assert diagnostics["zero_usage_records"] == 1
    assert diagnostics["records_inserted"] == 2, "zero-usage records are flagged, not dropped"

    store = Ledger(db_path).connect()
    flagged = [r["is_zero_usage"] for r in store.rows()]
    store.close()
    assert sorted(flagged) == [0, 1]


def test_scan_reports_gate_b_when_effort_is_never_observed(tmp_path: Path, simple_log: Path, db_path: Path):
    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path).as_dict()
    assert diagnostics["effort_recorded"] == 0
    assert diagnostics["effort_unknown"] == 3
    assert any("Gate B" in note for note in diagnostics["notes"])

    store = Ledger(db_path).connect()
    for row in store.rows():
        assert row["effort"] is None
        assert row["effort_source"] == "unknown"
    store.close()


def test_scan_counts_effort_when_the_log_carries_it(tmp_path: Path, effort_log: Path, db_path: Path):
    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path).as_dict()
    assert diagnostics["effort_recorded"] == 1
    assert diagnostics["effort_unknown"] == 1
    assert not any("Gate B" in note for note in diagnostics["notes"])


def test_scan_notes_unverified_schema(tmp_path: Path, simple_log: Path, db_path: Path):
    diagnostics = scan_mod.scan(log_root=tmp_path, db_path=db_path).as_dict()
    assert any("UNVERIFIED" in note for note in diagnostics["notes"])


def test_scan_on_empty_root_is_not_an_error(tmp_path: Path, db_path: Path):
    diagnostics = scan_mod.scan(log_root=tmp_path / "nothing-here", db_path=db_path).as_dict()
    assert diagnostics["files_found"] == 0
    assert diagnostics["records_inserted"] == 0
    assert any("no .jsonl log files found" in note for note in diagnostics["notes"])

    store = Ledger(db_path).connect()
    assert store.count("request_record") == 0
    store.close()


def test_scan_persists_source_file_digests(tmp_path: Path, simple_log: Path, db_path: Path):
    scan_mod.scan(log_root=tmp_path, db_path=db_path)
    store = Ledger(db_path).connect()
    rows = store.conn.execute(
        "SELECT source_file, size_bytes, sha256, lines_read FROM scan_source_file"
    ).fetchall()
    store.close()
    assert len(rows) == 1
    assert rows[0]["size_bytes"] > 0
    assert len(rows[0]["sha256"]) == 64
    assert rows[0]["lines_read"] == 4


def test_scan_persists_observed_key_paths(tmp_path: Path, simple_log: Path, db_path: Path):
    scan_mod.scan(log_root=tmp_path, db_path=db_path)
    store = Ledger(db_path).connect()
    keys = {r[0] for r in store.conn.execute("SELECT key_path FROM schema_observed_key")}
    store.close()
    assert "message.usage" in keys
    assert "requestId" in keys


def test_scan_diagnostics_are_persisted_as_json(tmp_path: Path, simple_log: Path, db_path: Path):
    scan_mod.scan(log_root=tmp_path, db_path=db_path)
    store = Ledger(db_path).connect()
    payload = store.conn.execute("SELECT diagnostics_json FROM scan").fetchone()[0]
    store.close()
    decoded = json.loads(payload)
    assert decoded["records_inserted"] == 3
    assert decoded["ledger_rows_total"] == 3


def test_cli_scan_runs_and_emits_json(tmp_path: Path, simple_log: Path, db_path: Path):
    from click.testing import CliRunner

    from tamias.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app, ["scan", "--log-root", str(tmp_path), "--db", str(db_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["records_inserted"] == 3
    assert payload["parser_version"] == "claude-code-parser/0.1.0"


def test_cli_help_lists_all_commands():
    from click.testing import CliRunner

    from tamias.cli import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("scan", "report", "receipt", "doctor", "probe"):
        assert command in result.output
