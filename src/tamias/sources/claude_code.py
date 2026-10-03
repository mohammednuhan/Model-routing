"""Claude Code log source: metadata-only parser.

Scope and honesty rules (spec sections 6.1-6.4 and 31):

* Metadata only. This module never returns prompt text, assistant text, tool
  arguments or any transcript content. Tool *names* are kept because the spec
  lists them as a metadata field; tool inputs never are.
* Rule 3: anything not establishable from the line is ``None`` (NULL) or the
  spec's ``unknown`` enum member. Nothing is inferred to make a field look
  populated.
* The client log schema has NOT been verified against real logs for this
  client version, so ``SCHEMA_STATUS`` is ``unverified`` and every parsed
  record carries it. Field *paths* below are candidates to be confirmed, not
  verified facts.
* Every record carries ``raw_record_hash`` (spec 6.1) and ``parser_version``
  (spec 6.2 reproducibility requirement).
* Lines that cannot yield usage are reported as ``ParseIssue`` with a reason;
  they are never silently discarded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

# Bumped whenever extraction semantics change. Stored on every row so a later
# parser change cannot silently alter historical numbers.
PARSER_VERSION = "claude-code-parser/0.1.0"

# The candidate schema identity for the Claude Code JSONL transcript. This is
# a hypothesis to be verified (Gate B), not an established fact.
SCHEMA_ID = "claude-code-jsonl-candidate/0.1.0"

# No real logs have been inspected for this client version. Field presence is
# therefore UNKNOWN; see docs/LOG-SCHEMA.md.
SCHEMA_STATUS = "unverified"

# Record types this parser has candidate handling for. Anything else is
# counted as unsupported by the caller but still parsed if it carries usage.
KNOWN_RECORD_TYPES = (
    "assistant",
    "user",
    "system",
    "summary",
    "file-history-snapshot",
    "queued-command",
)

# Candidate field paths, relative to the record and to record["message"].
_USAGE_PATHS = ("message.usage", "usage")
_INPUT_KEYS = ("input_tokens", "prompt_tokens")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens")
_CACHE_READ_KEYS = ("cache_read_input_tokens", "cache_read_tokens")
_CACHE_WRITE_TOTAL_KEYS = ("cache_creation_input_tokens", "cache_write_tokens")
_CACHE_WRITE_5M_KEYS = ("cache_creation_5m_input_tokens", "cache_creation_5m_tokens")
_CACHE_WRITE_1H_KEYS = ("cache_creation_1h_input_tokens", "cache_creation_1h_tokens")

_TOKEN_KEYS = (
    *_INPUT_KEYS,
    *_OUTPUT_KEYS,
    *_CACHE_READ_KEYS,
    *_CACHE_WRITE_TOTAL_KEYS,
    *_CACHE_WRITE_5M_KEYS,
    *_CACHE_WRITE_1H_KEYS,
)

# Top-level keys that would indicate a compaction event if observed. Recorded
# as a signal when present; otherwise NULL (never guessed as 0/false).
_COMPACTION_KEYS = ("isCompactSummary", "compactMetadata", "subtype")


@dataclass(frozen=True)
class ParseIssue:
    """A line that produced no ledger record, with the reason why."""

    reason: str
    source_file: str
    source_line: int
    record_type: str | None = None
    raw_record_hash: str | None = None


@dataclass(frozen=True)
class ParsedRecord:
    """One metadata-only section 6.1 record ready for the ledger."""

    record: dict[str, Any]
    source_file: str
    source_line: int
    is_zero_usage: bool = False
    is_synthetic: bool = False

    @property
    def raw_record_hash(self) -> str:
        return str(self.record["raw_record_hash"])


@dataclass
class FileParseResult:
    path: Path
    records: list[ParsedRecord] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    record_type_counts: dict[str, int] = field(default_factory=dict)
    observed_keys: dict[str, list[str]] = field(default_factory=dict)
    files_read: int = 0
    lines_read: int = 0
    blank_lines: int = 0


def _hash(raw: str | bytes) -> str:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return hashlib.sha256(data).hexdigest()


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _first_int(usage: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        if key in usage:
            value = _as_int(usage[key])
            if value is not None:
                return value
    return None


def _dig(record: Mapping[str, Any], path: str) -> Any:
    node: Any = record
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _has_path(record: Mapping[str, Any], path: str) -> bool:
    node: Any = record
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False
        node = node[part]
    return True


def _tool_names(message: Mapping[str, Any]) -> str | None:
    """Tool names only. Tool inputs are never read, let alone stored."""
    content = message.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return None
    names: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    return json.dumps(names) if names else None


def _request_bucket(record: Mapping[str, Any]) -> str:
    sidechain = record.get("isSidechain")
    if sidechain is True or (
        not isinstance(sidechain, bool) and str(sidechain).lower() == "true"
    ):
        return "subagent"
    if record.get("isCompactSummary") or record.get("compactMetadata"):
        return "compaction"
    if "subtype" in record and str(record.get("subtype")).lower() == "compact":
        return "compaction"
    return "main"


def _effort_fields(record: Mapping[str, Any]) -> tuple[str | None, str, str]:
    """Return ``(effort, effort_source, effort_mechanism)``.

    Effort is reported only when the line states it. An absent effort yields
    ``(None, 'unknown', 'unknown')``; it is never back-filled.
    """
    for key in ("effort", "reasoningEffort", "thinking_effort"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), "recorded", "top_level"
    message = record.get("message")
    if isinstance(message, Mapping):
        for key in ("effort", "reasoningEffort", "thinking_effort"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), "recorded", "per_message"
        thinking = message.get("thinking")
        if isinstance(thinking, Mapping):
            budget = thinking.get("budget_tokens")
            if _as_int(budget) is not None:
                # A native budget is a different mechanism from a named
                # effort level, so the value is not translated into one.
                return None, "unknown", "native_budget"
    return None, "unknown", "unknown"


def parse_record(
    raw_line: str,
    *,
    source_file: str,
    source_line: int,
    run_id: str | None = None,
) -> ParsedRecord | ParseIssue:
    """Parse one JSONL line into a metadata-only record or a ``ParseIssue``."""
    raw_hash = _hash(raw_line)

    try:
        record = json.loads(raw_line)
    except (json.JSONDecodeError, ValueError):
        return ParseIssue(
            reason="malformed_json",
            source_file=source_file,
            source_line=source_line,
            raw_record_hash=raw_hash,
        )

    if not isinstance(record, Mapping):
        return ParseIssue(
            reason="not_an_object",
            source_file=source_file,
            source_line=source_line,
            raw_record_hash=raw_hash,
        )

    record_type = record.get("type") if isinstance(record.get("type"), str) else None

    usage = None
    for path in _USAGE_PATHS:
        candidate = _dig(record, path)
        if isinstance(candidate, Mapping):
            usage = candidate
            break
    if usage is None and isinstance(record.get("usage"), Mapping):
        usage = record["usage"]

    if usage is None:
        return ParseIssue(
            reason="no_usage_block",
            source_file=source_file,
            source_line=source_line,
            record_type=record_type,
            raw_record_hash=raw_hash,
        )

    if not any(_as_int(usage.get(key)) is not None for key in _TOKEN_KEYS):
        return ParseIssue(
            reason="usage_without_token_fields",
            source_file=source_file,
            source_line=source_line,
            record_type=record_type,
            raw_record_hash=raw_hash,
        )

    message = record.get("message") if isinstance(record.get("message"), Mapping) else {}

    input_tokens = _first_int(usage, _INPUT_KEYS)
    output_tokens = _first_int(usage, _OUTPUT_KEYS)
    cache_read = _first_int(usage, _CACHE_READ_KEYS)
    cache_write_total = _first_int(usage, _CACHE_WRITE_TOTAL_KEYS)
    cache_write_5m = _first_int(usage, _CACHE_WRITE_5M_KEYS)
    cache_write_1h = _first_int(usage, _CACHE_WRITE_1H_KEYS)

    token_values = (
        input_tokens,
        output_tokens,
        cache_read,
        cache_write_total,
        cache_write_5m,
        cache_write_1h,
    )
    # An absent split bucket is zero, not a positive signal: a record with no
    # non-zero token value anywhere is a zero-usage record.
    is_zero_usage = all((value or 0) == 0 for value in token_values)

    message_id = message.get("id")
    message_id = message_id.strip() if isinstance(message_id, str) else None
    request_id = record.get("requestId")
    request_id = request_id.strip() if isinstance(request_id, str) else None
    is_synthetic = not (message_id or request_id)

    model = message.get("model")
    effort, effort_source, effort_mechanism = _effort_fields(record)

    sidechain = record.get("isSidechain")
    compaction_signal = 1 if _request_bucket(record) == "compaction" else None

    out: dict[str, Any] = {
        "run_id": run_id,
        "session_id": _optional_str(record.get("sessionId")),
        "seq": None,
        "ts_utc": _optional_str(record.get("timestamp")),
        "request_id": request_id,
        "message_id": message_id,
        "is_sidechain": sidechain if isinstance(sidechain, bool) else None,
        "request_bucket": "unknown" if is_synthetic else _request_bucket(record),
        "agent": "claude-code",
        "agent_version": _optional_str(record.get("version")),
        "provider": "anthropic",
        "surface": "cli",
        "model_id": _optional_str(model),
        "model_source": "explicit" if isinstance(model, str) and model.strip() else "unknown",
        "effort": effort,
        "effort_source": effort_source,
        "effort_mechanism": effort_mechanism,
        "thinking_mode": "unknown",
        "speed_or_service_tier": _optional_str(record.get("serviceTier")),
        "fallback_flag": None,
        "fallback_target_model": None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write_total,
        "cache_write_5m_tokens": cache_write_5m,
        "cache_write_1h_tokens": cache_write_1h,
        "cache_write_total_tokens": cache_write_total,
        "tool_names": _tool_names(message),
        "test_ran": None,
        "test_passed": None,
        "cache_miss_reason": None,
        "cache_diagnostic_source": "unknown",
        "compaction_signal": compaction_signal,
        "image_or_context_trim_signal": None,
        "raw_record_hash": raw_hash,
        "parser_version": PARSER_VERSION,
        "is_zero_usage": is_zero_usage,
        "is_synthetic": is_synthetic,
        "schema_status": SCHEMA_STATUS,
    }
    # test_signal in the spec vocabulary maps to the two ledger booleans; the
    # log does not carry them, so both stay NULL.
    assert _COMPACTION_KEYS  # candidate keys retained for later verification

    return ParsedRecord(
        record=out,
        source_file=source_file,
        source_line=source_line,
        is_zero_usage=is_zero_usage,
        is_synthetic=is_synthetic,
    )


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _key_paths(node: Any, prefix: str = "", depth: int = 0) -> Iterator[str]:
    """Yield key *paths* only. Values are never collected."""
    if depth > 6 or not isinstance(node, Mapping):
        return
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        yield path
        if isinstance(value, Mapping):
            yield from _key_paths(value, path, depth + 1)


def parse_file(path: str | Path, *, run_id: str | None = None) -> FileParseResult:
    """Parse every line of one JSONL log file."""
    path = Path(path)
    result = FileParseResult(path=path)

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        result.issues.append(
            ParseIssue(
                reason="unreadable_file",
                source_file=str(path),
                source_line=0,
            )
        )
        return result

    with handle:
        result.files_read = 1
        seen_keys: set[str] = set()
        for index, line in enumerate(handle, start=1):
            result.lines_read += 1
            stripped = line.strip()
            if not stripped:
                result.blank_lines += 1
                continue

            parsed = parse_record(
                stripped, source_file=str(path), source_line=index, run_id=run_id
            )
            if isinstance(parsed, ParseIssue):
                result.issues.append(parsed)
                if parsed.record_type:
                    result.record_type_counts[parsed.record_type] = (
                        result.record_type_counts.get(parsed.record_type, 0) + 1
                    )
                continue

            result.records.append(parsed)
            try:
                raw = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                continue
            record_type = raw.get("type") if isinstance(raw, Mapping) else None
            if isinstance(record_type, str):
                result.record_type_counts[record_type] = (
                    result.record_type_counts.get(record_type, 0) + 1
                )
                if record_type not in result.observed_keys:
                    result.observed_keys[record_type] = []
                for key_path in _key_paths(raw):
                    if key_path not in seen_keys:
                        seen_keys.add(key_path)
                        result.observed_keys[record_type].append(key_path)

    for keys in result.observed_keys.values():
        keys.sort()
    return result


def iter_log_files(root: str | Path) -> list[Path]:
    """All ``*.jsonl`` files under ``root``, in a stable sorted order."""
    root = Path(root).expanduser()
    if not root.exists():
        return []
    if root.is_file():
        return [root] if root.suffix == ".jsonl" else []
    return sorted((p for p in root.rglob("*.jsonl") if p.is_file()), key=lambda p: str(p))