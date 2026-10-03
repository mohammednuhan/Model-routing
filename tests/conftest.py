"""Shared pytest fixtures.

The fixtures are synthetic on purpose: no real prompt, code, tool argument or
assistant text from any user's logs is committed to this repository. The
sentinel strings below exist so the privacy test can prove that such content
never reaches the ledger.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# Content that must never appear in the database. Deliberately distinctive.
SENTINEL_PROMPT = "SENTINEL_PROMPT_a1b2c3_do_not_store"
SENTINEL_ASSISTANT = "SENTINEL_ASSISTANT_d4e5f6_do_not_store"
SENTINEL_TOOL_ARG = "SENTINEL_TOOL_ARG_g7h8i9_do_not_store"
SENTINEL_CODE = "SENTINEL_CODE_j0k1l2_do_not_store"
ALL_SENTINELS = (
    SENTINEL_PROMPT,
    SENTINEL_ASSISTANT,
    SENTINEL_TOOL_ARG,
    SENTINEL_CODE,
)


def assistant_record(
    *,
    message_id: str,
    request_id: str | None,
    model: str = "claude-sonnet-4-5",
    version: str = "1.0.99",
    timestamp: str = "2026-09-30T12:00:00.000Z",
    session_id: str = "session-0001",
    is_sidechain: bool = False,
    input_tokens: int = 12,
    output_tokens: int = 34,
    cache_read: int = 1000,
    cache_creation: int = 250,
    cache_creation_5m: int | None = None,
    cache_creation_1h: int | None = None,
    effort: str | None = None,
    usage_extra: dict[str, Any] | None = None,
    with_tool_use: bool = False,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }
    if cache_creation_5m is not None:
        usage["cache_creation_5m_input_tokens"] = cache_creation_5m
    if cache_creation_1h is not None:
        usage["cache_creation_1h_input_tokens"] = cache_creation_1h
    if usage_extra:
        usage.update(usage_extra)

    message: dict[str, Any] = {"id": message_id, "model": model, "usage": usage}
    if with_tool_use:
        message["content"] = [
            {"type": "text", "text": SENTINEL_ASSISTANT},
            {"type": "tool_use", "name": "Read", "input": {"file_path": SENTINEL_CODE}},
            {"type": "tool_use", "name": "Bash", "input": {"command": SENTINEL_TOOL_ARG}},
        ]

    record: dict[str, Any] = {
        "type": "assistant",
        "version": version,
        "sessionId": session_id,
        "timestamp": timestamp,
        "isSidechain": is_sidechain,
        "message": message,
        "uuid": f"uuid-{message_id}",
    }
    if request_id is not None:
        record["requestId"] = request_id
    if effort is not None:
        record["effort"] = effort
    return record


def user_record(*, message_id: str, session_id: str = "session-0001") -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": session_id,
        "timestamp": "2026-09-30T11:59:00.000Z",
        "message": {"role": "user", "content": SENTINEL_PROMPT},
        "uuid": f"uuid-{message_id}",
    }


def write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.sqlite3"


@pytest.fixture()
def simple_log(tmp_path: Path) -> Path:
    """One file, three usage records, one user record with no usage."""
    return write_jsonl(
        tmp_path / "logs" / "session-0001.jsonl",
        [
            user_record(message_id="u-1"),
            assistant_record(message_id="m-1", request_id="r-1"),
            assistant_record(
                message_id="m-2",
                request_id="r-2",
                cache_creation=0,
                cache_read=0,
                input_tokens=0,
                output_tokens=0,
            ),
            assistant_record(message_id="m-3", request_id="r-3", is_sidechain=True),
        ],
    )


@pytest.fixture()
def effort_log(tmp_path: Path) -> Path:
    """A log where one record carries an explicit effort value."""
    return write_jsonl(
        tmp_path / "logs-effort" / "session-0002.jsonl",
        [
            assistant_record(message_id="m-1", request_id="r-1", effort="high"),
            assistant_record(message_id="m-2", request_id="r-2"),
        ],
    )


@pytest.fixture()
def cache_split_log(tmp_path: Path) -> Path:
    return write_jsonl(
        tmp_path / "logs-split" / "session-0003.jsonl",
        [
            assistant_record(
                message_id="m-1",
                request_id="r-1",
                cache_creation=900,
                cache_creation_5m=700,
                cache_creation_1h=200,
            )
        ],
    )


@pytest.fixture()
def malformed_log(tmp_path: Path) -> Path:
    path = tmp_path / "logs-bad" / "session-bad.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(assistant_record(message_id="m-1", request_id="r-1")) + "\n")
        handle.write("{not json at all\n")
        handle.write("\n")
        handle.write(json.dumps({"type": "mystery-type", "payload": {"a": 1}}) + "\n")
    return path


@pytest.fixture()
def synthetic_log(tmp_path: Path) -> Path:
    """Usage record with no message id and no request id."""
    record = assistant_record(message_id="m-1", request_id="r-1")
    record["message"].pop("id")
    record.pop("requestId")
    return write_jsonl(tmp_path / "logs-synthetic" / "session-0004.jsonl", [record])


@pytest.fixture()
def tool_use_log(tmp_path: Path) -> Path:
    return write_jsonl(
        tmp_path / "logs-tools" / "session-0005.jsonl",
        [assistant_record(message_id="m-1", request_id="r-1", with_tool_use=True)],
    )
