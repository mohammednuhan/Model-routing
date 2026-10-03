"""Claude Code session-log parser: metadata extraction only.

Scope
-----
This module turns raw Claude Code session-log lines into section 6.1 request
records. It extracts **metadata only**. It never returns, stores or logs
prompt text, source-code text, tool arguments, assistant text or any other
transcript content (spec 6.3).

Evidence status of the field map
--------------------------------
The candidate key paths below are declared, versioned and *unverified*: the
schema of this client version has not been confirmed against real logs on the
machine where this parser was written (see ``docs/LOG-SCHEMA.md``). Every
record produced here therefore carries ``schema_status='unverified'``, and the
parser also records the key paths it actually observed so the field map can be
corrected from evidence rather than from assumption.

Consequences of that status, per rule 3 (anything not establishable is UNKNOWN,
never a guess):

* a field whose key path is not present in a record is stored as NULL;
* ``effort`` is NULL and ``effort_source`` is ``'unknown'`` unless an effort
  key is actually present in the record;
* record types the parser does not recognise are counted and reported, never
  silently skipped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

PARSER_VERSION = "claude-code-parser/0.1.0"
SCHEMA_ID = "claude-code-session-log"
SCHEMA_STATUS = "unverified"
AGENT_NAME = "claude-code"

# Keys whose values must never leave the parser, whatever their nesting.
FORBIDDEN_CONTENT_KEYS = frozenset(
    {
        "text",
        "content",
        "input",
        "arguments",
        "args",
        "params",
        "prompt",
        "message_text",
        "result",
        "stdout",
        "stderr",
        "output",
        "command",
        "patch",
        "diff",
        "body",
    }
)

# Candidate scalar paths. Each entry lists candidate key paths; a dotted path
# addresses one level of nesting. These are hypotheses, not verified names.
CANDIDATE_PATHS: Mapping[str, tuple[str, ...]] = {
    "run_id": ("runId", "run_id"),
    "session_id": ("sessionId", "session_id"),
    "ts_utc": ("timestamp", "ts"),
    "request_id": ("requestId", "request_id"),
    "message_id": ("message.id", "messageId", "message_id"),
    "is_sidechain": ("isSidechain", "is_sidechain"),
    "agent_version": ("version", "agentVersion", "agent_version"),
    "model_id": ("message.model", "model", "modelId", "model_id"),
    "surface": ("surface",),
    "provider": ("provider",),
    "effort": ("effort", "reasoningEffort", "reasoning_effort"),
    "effort_mechanism": ("effortMechanism", "effort_mechanism"),
    "thinking_mode": ("thinkingMode", "thinking_mode", "thinking"),
    "speed_or_service_tier": ("speed", "serviceTier", "service_tier"),
    "fallback_flag": ("isFallback", "fallback", "fallback_flag"),
    "fallback_target_model": ("fallbackTargetModel", "fallback_target_model"),
    "cache_miss_reason": ("cacheMissReason", "cache_miss_reason"),
    "cache_diagnostic_source": ("cacheDiagnosticSource", "cache_diagnostic_source"),
    "compaction_signal": ("isCompactSummary", "compaction", "compaction_signal"),
    "image_or_context_trim_signal": (
        "imageOrContextTrim",
        "contextTrim",
        "image_or_context_trim_signal",
    ),
    "model_source": ("modelSource", "model_source"),
}

# Usage block location and the token fields inside it.
USAGE_PATHS: tuple[str, ...] = ("message.usage", "usage", "tokenUsage")
USAGE_TOKEN_PATHS: Mapping[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens"),
    "output_tokens": (
        "output_tokens",
        "outputTokens",
        "completion_tokens",
        "completionTokens",
    ),
    "cache_read_tokens": (
        "cache_read_input_tokens",
        "cacheReadInputTokens",
        "cache_read_tokens",
        "cacheReadTokens",
    ),
    "cache_write_tokens": (
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
        "cache_write_tokens",
        "cacheWriteTokens",
    ),
    "cache_write_5m_tokens": (
        "cache_creation_5m_input_tokens",
        "cacheCreation5mInputTokens",
        "cache_write_5m_tokens",
    ),
    "cache_write_1h_tokens": (
        "cache_creation_1h_input_tokens",
        "cacheCreation1hInputTokens",
        "cache_write_1h_tokens",
    ),
}

# Record types the parser knows how to name. Anything else is counted and
# reported as unsupported. These are names, not verified types.
KNOWN_RECORD_TYPES = frozenset(
    {"assistant", "user", "system", "summary", "file-history-snapshot"}
)

_MISSING = object()


@dataclass
class ParsedRecord:
    """One section 6.1 request record plus parser bookkeeping."""

    record: dict[str, Any]
    record_type: str
    raw_record_hash: str
    is_usage_record: bool
    is_zero_usage: bool
    is_synthetic: bool
    observed_keys: tuple[str, ...]
    source_line: int
    source_file: str


@dataclass
class ParseIssue:
    """A record the parser could not turn into a ledger row."""

    source_file: str
    source_line: int
    reason: str
    record_type: str | None = None
    raw_record_hash: str | None = None


@dataclass
class ParseResult:
    records: list[ParsedRecord] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    record_type_counts: dict[str, int] = field(default_factory=dict)
    observed_keys: dict[str, set[str]] = field(default_factory=dict)
    files_read: int = 0
    lines_read: int = 0
    blank_lines: int = 0


def dig(record: Any, path: str) -> Any:
    """Follow a dotted path. Returns ``_MISSING`` when any hop is absent."""
    current = record
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _first(record: Mapping[str, Any], paths: Sequence[str]) -> Any:
    for path in paths:
        value = dig(record, path)
        if value is not _MISSING and value is not None:
            return value
    return None


def walk_key_paths(node: Any, prefix: str = "", depth: int = 0, out: set[str] | None = None) -> set[str]:
    """Collect key *paths* only. Values are never captured."""
    if out is None:
        out = set()
    if depth > 6:
        return out
    if isinstance(node, Mapping):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.add(path)
            if isinstance(value, (Mapping, list)):
                walk_key_paths(value, path, depth + 1, out)
    elif isinstance(node, list):
        for item in node[:1]:
            if isinstance(item, (Mapping, list)):
                walk_key_paths(item, f"{prefix}[]", depth + 1, out)
    return out


def raw_hash(raw_line: str) -> str:
    """Stable hash of the raw log line. The line itself is not stored."""
    return hashlib.sha256(raw_line.encode("utf-8", errors="replace")).hexdigest()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
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


def _as_bool(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return int(bool(value))
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return 1
        if lowered in {"false", "0", "no"}:
            return 0
    return None


def extract_tool_names(record: Mapping[str, Any]) -> list[str]:
    """Collect tool *names* only.

    Content blocks are visited looking for tool-use blocks and only the
    ``name`` key is read. Tool arguments (``input``) are never read, so they
    cannot reach the ledger.
    """
    names: list[str] = []

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, Mapping):
            if node.get("type") == "tool_use" and isinstance(node.get("name"), str):
                name = node["name"]
                if name not in names:
                    names.append(name)
            for key, value in node.items():
                if key in FORBIDDEN_CONTENT_KEYS and not isinstance(value, (Mapping, list)):
                    continue
                if isinstance(value, (Mapping, list)):
                    visit(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                visit(item, depth + 1)

    visit(record)
    return names


def classify_record_type(record: Mapping[str, Any]) -> str:
    value = record.get("type")
    if isinstance(value, str) and value:
        return value
    message = record.get("message")
    if isinstance(message, Mapping):
        role = message.get("role")
        if isinstance(role, str) and role:
            return role
    return "unknown"


def parse_record(
    raw_line: str,
    *,
    source_file: str,
    source_line: int,
    run_id: str | None = None,
    seq: int | None = None,
) -> ParsedRecord | ParseIssue:
    """Parse one JSONL line into a section 6.1 record, or report why not."""
    line_hash = raw_hash(raw_line)
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError:
        return ParseIssue(source_file, source_line, "malformed_json", None, line_hash)

    if not isinstance(payload, Mapping):
        return ParseIssue(source_file, source_line, "not_an_object", None, line_hash)

    record_type = classify_record_type(payload)
    observed = tuple(sorted(walk_key_paths(payload)))

    model_id = _first(payload, CANDIDATE_PATHS["model_id"])
    request_id = _first(payload, CANDIDATE_PATHS["request_id"])
    message_id = _first(payload, CANDIDATE_PATHS["message_id"])

    usage: Mapping[str, Any] | None = None
    for path in USAGE_PATHS:
        candidate = dig(payload, path)
        if isinstance(candidate, Mapping):
            usage = candidate
            break

    token_values: dict[str, int | None] = {}
    for field_name, paths in USAGE_TOKEN_PATHS.items():
        raw_value = _first(usage, paths) if usage is not None else None
        token_values[field_name] = _as_int(raw_value)

    has_usage_block = usage is not None
    has_any_token = any(v is not None for v in token_values.values())

    if not has_any_token:
        # No measurable token usage: the record cannot become a priced request
        # record. Counted and reported, never silently dropped.
        return ParseIssue(
            source_file,
            source_line,
            "no_usage_block" if not has_usage_block else "usage_without_token_fields",
            record_type,
            line_hash,
        )

    known = [v for v in token_values.values() if v is not None]
    is_zero_usage = bool(known) and all(v == 0 for v in known)

    sidechain = _as_bool(_first(payload, CANDIDATE_PATHS["is_sidechain"]))
    compaction = _as_bool(_first(payload, CANDIDATE_PATHS["compaction_signal"]))
    is_synthetic = message_id is None and request_id is None

    if sidechain == 1:
        bucket = "subagent"
    elif compaction == 1:
        bucket = "compaction"
    elif is_synthetic:
        bucket = "unknown"
    else:
        bucket = "main"

    cache_write_5m = token_values["cache_write_5m_tokens"]
    cache_write_1h = token_values["cache_write_1h_tokens"]
    cache_write_total = token_values["cache_write_tokens"]
    if cache_write_total is None and (cache_write_5m is not None or cache_write_1h is not None):
        cache_write_total = (cache_write_5m or 0) + (cache_write_1h or 0)

    effort = _first(payload, CANDIDATE_PATHS["effort"])
    effort_mechanism = _first(payload, CANDIDATE_PATHS["effort_mechanism"])

    tool_names = extract_tool_names(payload)

    record: dict[str, Any] = {
        "run_id": run_id
        if run_id is not None
        else _first(payload, CANDIDATE_PATHS["run_id"]),
        "session_id": _first(payload, CANDIDATE_PATHS["session_id"]),
        "seq": seq,
        "ts_utc": _first(payload, CANDIDATE_PATHS["ts_utc"]),
        "request_id": request_id,
        "message_id": message_id,
        "is_sidechain": sidechain,
        "request_bucket": bucket,
        "agent": AGENT_NAME if record_type in KNOWN_RECORD_TYPES else None,
        "agent_version": _first(payload, CANDIDATE_PATHS["agent_version"]),
        "provider": _first(payload, CANDIDATE_PATHS["provider"]),
        "surface": _first(payload, CANDIDATE_PATHS["surface"]),
        "model_id": model_id,
        "model_source": _first(payload, CANDIDATE_PATHS["model_source"]) or "unknown",
        "effort": effort if isinstance(effort, (str, int, float)) else None,
        "effort_source": "recorded" if effort is not None else "unknown",
        "effort_mechanism": effort_mechanism
        if effort_mechanism in set(EFFORT_MECHANISM_VALUES)
        else "unknown",
        "thinking_mode": _coerce_thinking(
            _first(payload, CANDIDATE_PATHS["thinking_mode"])
        ),
        "speed_or_service_tier": _first(
            payload, CANDIDATE_PATHS["speed_or_service_tier"]
        ),
        "fallback_flag": _as_bool(_first(payload, CANDIDATE_PATHS["fallback_flag"])),
        "fallback_target_model": _first(
            payload, CANDIDATE_PATHS["fallback_target_model"]
        ),
        "input_tokens": token_values["input_tokens"],
        "output_tokens": token_values["output_tokens"],
        "cache_read_tokens": token_values["cache_read_tokens"],
        "cache_write_tokens": cache_write_total,
        "cache_write_5m_tokens": cache_write_5m,
        "cache_write_1h_tokens": cache_write_1h,
        "cache_write_total_tokens": cache_write_total,
        "tool_names": json.dumps(tool_names) if tool_names else None,
        "test_ran": None,
        "test_passed": None,
        "cache_miss_reason": _first(payload, CANDIDATE_PATHS["cache_miss_reason"]),
        "cache_diagnostic_source": _first(
            payload, CANDIDATE_PATHS["cache_diagnostic_source"]
        )
        or "unknown",
        "compaction_signal": compaction,
        "image_or_context_trim_signal": _as_bool(
            _first(payload, CANDIDATE_PATHS["image_or_context_trim_signal"])
        ),
        "raw_record_hash": line_hash,
        "parser_version": PARSER_VERSION,
        "is_zero_usage": is_zero_usage,
        "is_synthetic": is_synthetic,
        "schema_status": SCHEMA_STATUS,
    }

    return ParsedRecord(
        record=record,
        record_type=record_type,
        raw_record_hash=line_hash,
        is_usage_record=True,
        is_zero_usage=is_zero_usage,
        is_synthetic=is_synthetic,
        observed_keys=observed,
        source_line=source_line,
        source_file=source_file,
    )


# Spec 6.1 enum for effort_mechanism, duplicated here to keep the parser
# independent of the ledger module.
EFFORT_MECHANISM_VALUES = ("per_message", "top_level", "native_budget")
_THINKING_VALUES = ("adaptive", "enabled", "disabled", "between_tools")


def _coerce_thinking(value: Any) -> str:
    if isinstance(value, str) and value in _THINKING_VALUES:
        return value
    if isinstance(value, bool):
        return "enabled" if value else "disabled"
    return "unknown"


def iter_log_files(log_root: str | Path) -> list[Path]:
    """All ``*.jsonl`` files under ``log_root``, sorted for determinism."""
    root = Path(log_root).expanduser()
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*.jsonl") if p.is_file())


def parse_file(path: str | Path, *, run_id: str | None = None) -> ParseResult:
    """Parse one log file into records and issues."""
    file_path = Path(path)
    result = ParseResult(files_read=1)
    with file_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                result.blank_lines += 1
                continue
            result.lines_read += 1
            _absorb(
                result,
                parse_record(
                    line,
                    source_file=str(file_path),
                    source_line=line_no,
                    run_id=run_id,
                    seq=line_no,
                ),
            )
    return result


def parse_paths(
    paths: Sequence[str | Path], *, run_id: str | None = None
) -> ParseResult:
    """Parse many files, merging diagnostics. Dedup happens in the ledger."""
    merged = ParseResult()
    for path in paths:
        result = parse_file(path, run_id=run_id)
        merged.records.extend(result.records)
        merged.issues.extend(result.issues)
        merged.files_read += result.files_read
        merged.lines_read += result.lines_read
        merged.blank_lines += result.blank_lines
        for name, count in result.record_type_counts.items():
            merged.record_type_counts[name] = merged.record_type_counts.get(name, 0) + count
        for record_type, keys in result.observed_keys.items():
            merged.observed_keys.setdefault(record_type, set()).update(keys)
    return merged


def parse_stream(
    lines: Iterator[str], *, source_file: str, run_id: str | None = None
) -> ParseResult:
    """Parse an in-memory line iterator."""
    result = ParseResult(files_read=1)
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            result.blank_lines += 1
            continue
        result.lines_read += 1
        _absorb(
            result,
            parse_record(
                line, source_file=source_file, source_line=line_no, run_id=run_id, seq=line_no
            ),
        )
    return result


def _absorb(result: ParseResult, outcome: ParsedRecord | ParseIssue) -> None:
    if isinstance(outcome, ParseIssue):
        result.issues.append(outcome)
        key = outcome.record_type or "unknown"
        result.record_type_counts[key] = result.record_type_counts.get(key, 0) + 1
        return
    result.records.append(outcome)
    result.record_type_counts[outcome.record_type] = (
        result.record_type_counts.get(outcome.record_type, 0) + 1
    )
    result.observed_keys.setdefault(outcome.record_type, set()).update(outcome.observed_keys)
