"""Privacy tests (spec 6.3, AGENTS.md rule 2).

The product ledger must never persist prompt text, source-code text, tool
arguments, raw assistant text or transcript content. These tests assert that
against the raw bytes of the SQLite file, which catches anything stored in any
column, index or free page.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tamias.core import scan as scan_mod
from tamias.core.ledger import Ledger
from tamias.sources import claude_code

from conftest import (
    ALL_SENTINELS,
    SENTINEL_ASSISTANT,
    SENTINEL_CODE,
    SENTINEL_PROMPT,
    SENTINEL_TOOL_ARG,
    assistant_record,
    user_record,
    write_jsonl,
)


def _build_db(tmp_path: Path) -> Path:
    """Write every content-bearing record shape we support, then scan them."""
    write_jsonl(
        tmp_path / "logs" / "session-a.jsonl",
        [
            user_record(message_id="u-1"),
            assistant_record(message_id="m-1", request_id="r-1", with_tool_use=True),
            assistant_record(
                message_id="m-2",
                request_id="r-2",
                is_sidechain=True,
                effort="high",
                cache_creation=10,
                cache_creation_5m=6,
                cache_creation_1h=4,
            ),
        ],
    )
    db_path = tmp_path / "ledger.sqlite3"
    scan_mod.scan(log_root=tmp_path, db_path=db_path)
    return db_path


def test_no_prompt_or_assistant_text_anywhere_in_the_database(tmp_path: Path):
    db_path = _build_db(tmp_path)
    raw = db_path.read_bytes()
    for sentinel in ALL_SENTINELS:
        assert sentinel.encode("utf-8") not in raw, (
            f"content leaked into the ledger database: {sentinel.split('_')[1]}"
        )


def test_no_content_in_any_stored_column_value(tmp_path: Path):
    db_path = _build_db(tmp_path)
    store = Ledger(db_path).connect()
    stored = store.all_text_values()
    store.close()
    joined = "\n".join(stored)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in joined


def test_tool_names_are_stored_but_arguments_are_not(tmp_path: Path):
    db_path = _build_db(tmp_path)
    store = Ledger(db_path).connect()
    rows = store.rows()
    tool_names = [r["tool_names"] for r in rows if r["tool_names"]]
    store.close()

    assert tool_names, "tool names are metadata and should be kept"
    assert json.loads(tool_names[0]) == ["Read", "Bash"]
    assert SENTINEL_TOOL_ARG not in tool_names[0]
    assert SENTINEL_CODE not in tool_names[0]


def test_parser_output_itself_carries_no_content(tmp_path: Path):
    line = json.dumps(assistant_record(message_id="m-1", request_id="r-1", with_tool_use=True))
    parsed = claude_code.parse_record(line, source_file="f.jsonl", source_line=1)
    assert not isinstance(parsed, claude_code.ParseIssue)
    serialized = json.dumps(parsed.record, default=str)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in serialized
    assert parsed.raw_record_hash != line, "only a hash is retained, not the line"


def test_dropped_records_store_no_content(tmp_path: Path):
    write_jsonl(
        tmp_path / "logs" / "s.jsonl",
        [
            assistant_record(message_id="m-1", request_id="r-1", with_tool_use=True),
            {"type": "assistant", "message": {"content": SENTINEL_ASSISTANT}},
        ],
    )
    db_path = tmp_path / "ledger.sqlite3"
    scan_mod.scan(log_root=tmp_path, db_path=db_path)

    raw = db_path.read_bytes()
    for sentinel in ALL_SENTINELS:
        assert sentinel.encode("utf-8") not in raw

    conn = sqlite3.connect(db_path)
    reasons = [r[0] for r in conn.execute("SELECT reason FROM dropped_record")]
    conn.close()
    assert reasons, "unsupported records must be reported, not silently dropped"


def test_wal_and_temp_files_are_not_left_behind(tmp_path: Path):
    db_path = _build_db(tmp_path)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(("-wal", "-shm", "-journal"))]
    assert leftovers == [], f"unexpected SQLite sidecar files: {leftovers}"


def test_every_sentinel_actually_appears_in_the_source_log(tmp_path: Path):
    """Guard against a vacuous privacy test: the input must contain the strings."""
    path = write_jsonl(
        tmp_path / "logs" / "s.jsonl",
        [
            user_record(message_id="u-1"),
            assistant_record(message_id="m-1", request_id="r-1", with_tool_use=True),
        ],
    )
    raw = path.read_text(encoding="utf-8")
    assert SENTINEL_PROMPT in raw
    assert SENTINEL_ASSISTANT in raw
    assert SENTINEL_TOOL_ARG in raw
    assert SENTINEL_CODE in raw
