"""Canonical ledger: SQLite storage for the section 6.1 request record.

The ledger is the shared data contract between Observer and Lab.

Design constraints taken directly from the frozen spec:

* Section 6.1 defines the request record. Every field exists with the exact
  name and the exact enum vocabulary used by the spec.
* Nullable means NULL. A value the log does not carry is NULL, never a guess.
  Enumerated fields that must always hold a value use the spec's own
  ``unknown`` member.
* Section 6.3: the ledger never persists prompt text, source-code text, tool
  arguments, raw assistant text or transcript content. Only metadata.
* Section 6.4: primary key is ``(message_id, request_id)``. When an id is
  unavailable a deterministic fallback key is used and *which* fallback was
  used is recorded. Uncertain records are never silently merged.
* Section 6.2 requires ``parser_version`` for reproducibility after parser
  changes, and section 6.1 requires ``raw_record_hash`` on every record.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Rule 3: anything not establishable is this string, never a guess.
UNKNOWN = "UNKNOWN"

# Bumped whenever the ledger schema changes shape.
LEDGER_SCHEMA_VERSION = "1"

# Enumerations exactly as written in spec section 6.1.
MODEL_SOURCES = ("explicit", "session", "fallback", "skill", "command", "unknown")
EFFORT_SOURCES = ("recorded", "inferred", "unknown")
EFFORT_MECHANISMS = ("per_message", "top_level", "native_budget", "unknown")
THINKING_MODES = ("adaptive", "enabled", "disabled", "between_tools", "unknown")
REQUEST_BUCKETS = ("main", "subagent", "compaction", "unknown")
CACHE_DIAGNOSTIC_SOURCES = ("vendor", "log", "inferred", "unknown")

# How a dedup key was derived. Recorded on every row (spec 6.4).
DEDUP_KEY_KINDS = ("ids", "message_id", "request_id", "raw_record_hash")

# Section 6.1 field order. Single source of truth for the table definition,
# the insert statement and the tests.
REQUEST_RECORD_FIELDS: tuple[str, ...] = (
    "run_id",
    "session_id",
    "seq",
    "ts_utc",
    "request_id",
    "message_id",
    "is_sidechain",
    "request_bucket",
    "agent",
    "agent_version",
    "provider",
    "surface",
    "model_id",
    "model_source",
    "effort",
    "effort_source",
    "effort_mechanism",
    "thinking_mode",
    "speed_or_service_tier",
    "fallback_flag",
    "fallback_target_model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_write_total_tokens",
    "tool_names",
    "test_ran",
    "test_passed",
    "cache_miss_reason",
    "cache_diagnostic_source",
    "compaction_signal",
    "image_or_context_trim_signal",
    "raw_record_hash",
    "parser_version",
)

# Fields whose value must always be present because the spec gives them a
# closed vocabulary with an explicit ``unknown`` member.
_ALWAYS_PRESENT_DEFAULTS: Mapping[str, str] = {
    "request_bucket": "unknown",
    "model_source": "unknown",
    "effort_source": "unknown",
    "effort_mechanism": "unknown",
    "thinking_mode": "unknown",
    "cache_diagnostic_source": "unknown",
}

_COLUMN_TYPES: Mapping[str, str] = {
    "run_id": "TEXT",
    "session_id": "TEXT",
    "seq": "INTEGER",
    "ts_utc": "TEXT",
    "request_id": "TEXT",
    "message_id": "TEXT",
    "is_sidechain": "INTEGER",
    "request_bucket": "TEXT NOT NULL DEFAULT 'unknown'",
    "agent": "TEXT",
    "agent_version": "TEXT",
    "provider": "TEXT",
    "surface": "TEXT",
    "model_id": "TEXT",
    "model_source": "TEXT NOT NULL DEFAULT 'unknown'",
    "effort": "TEXT",
    "effort_source": "TEXT NOT NULL DEFAULT 'unknown'",
    "effort_mechanism": "TEXT NOT NULL DEFAULT 'unknown'",
    "thinking_mode": "TEXT NOT NULL DEFAULT 'unknown'",
    "speed_or_service_tier": "TEXT",
    "fallback_flag": "INTEGER",
    "fallback_target_model": "TEXT",
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "cache_read_tokens": "INTEGER",
    "cache_write_tokens": "INTEGER",
    "cache_write_5m_tokens": "INTEGER",
    "cache_write_1h_tokens": "INTEGER",
    "cache_write_total_tokens": "INTEGER",
    # JSON array of tool *names* only. Tool arguments are never stored.
    "tool_names": "TEXT",
    "test_ran": "INTEGER",
    "test_passed": "INTEGER",
    "cache_miss_reason": "TEXT",
    "cache_diagnostic_source": "TEXT NOT NULL DEFAULT 'unknown'",
    "compaction_signal": "INTEGER",
    "image_or_context_trim_signal": "INTEGER",
    "raw_record_hash": "TEXT NOT NULL",
    "parser_version": "TEXT NOT NULL",
}

# Bookkeeping columns required by spec 6.1/6.4 plus the observation flags the
# spec asks the parser to inspect (zero-usage, synthetic). They sit alongside
# the 6.1 fields rather than inside the 6.1 contract.
_LEDGER_ONLY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("record_uid", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("dedup_key", "TEXT NOT NULL"),
    ("dedup_key_kind", "TEXT NOT NULL"),
    ("dedup_conflict", "INTEGER NOT NULL DEFAULT 0"),
    ("is_zero_usage", "INTEGER NOT NULL DEFAULT 0"),
    ("is_synthetic", "INTEGER NOT NULL DEFAULT 0"),
    ("schema_status", "TEXT NOT NULL"),
)

_INSERT_COLUMNS: tuple[str, ...] = (
    "dedup_key",
    "dedup_key_kind",
    "dedup_conflict",
    "is_zero_usage",
    "is_synthetic",
    "schema_status",
    *REQUEST_RECORD_FIELDS,
)


def _ledger_column_ddl() -> str:
    """Render bookkeeping plus section 6.1 columns as DDL."""
    parts: list[str] = [f"    {name} {decl}," for name, decl in _LEDGER_ONLY_COLUMNS]
    parts.extend(f"    {name} {_COLUMN_TYPES[name]}," for name in REQUEST_RECORD_FIELDS)
    return "\n".join(parts)


SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS request_record (
    {_ledger_column_ddl()}
    UNIQUE (dedup_key, raw_record_hash)
);

CREATE INDEX IF NOT EXISTS idx_request_record_session
    ON request_record (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_request_record_dedup
    ON request_record (dedup_key);
CREATE INDEX IF NOT EXISTS idx_request_record_conflict
    ON request_record (dedup_conflict);

-- Which log file and line a ledger row came from. Kept out of the request
-- record so the section 6.1 contract stays clean.
CREATE TABLE IF NOT EXISTS record_provenance (
    record_uid INTEGER PRIMARY KEY
        REFERENCES request_record (record_uid) ON DELETE CASCADE,
    scan_id INTEGER NOT NULL,
    source_file TEXT NOT NULL,
    source_line INTEGER NOT NULL
);

-- One row per ``tamias scan`` invocation.
CREATE TABLE IF NOT EXISTS scan (
    scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parser_version TEXT NOT NULL,
    ledger_schema_version TEXT NOT NULL,
    schema_id TEXT,
    schema_status TEXT NOT NULL,
    log_root TEXT,
    started_utc TEXT NOT NULL,
    finished_utc TEXT,
    diagnostics_json TEXT
);

CREATE TABLE IF NOT EXISTS scan_source_file (
    scan_id INTEGER NOT NULL REFERENCES scan (scan_id) ON DELETE CASCADE,
    source_file TEXT NOT NULL,
    size_bytes INTEGER,
    sha256 TEXT,
    lines_read INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scan_id, source_file)
);

-- Named counters: record types, field-presence counts, drop reasons.
CREATE TABLE IF NOT EXISTS scan_counter (
    scan_id INTEGER NOT NULL REFERENCES scan (scan_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value INTEGER NOT NULL,
    PRIMARY KEY (scan_id, name)
);

-- Key paths actually observed, with occurrence counts. Key *names* only, no
-- values. This is the raw material for docs/LOG-SCHEMA.md.
CREATE TABLE IF NOT EXISTS schema_observed_key (
    scan_id INTEGER NOT NULL REFERENCES scan (scan_id) ON DELETE CASCADE,
    record_type TEXT NOT NULL,
    key_path TEXT NOT NULL,
    occurrences INTEGER NOT NULL,
    PRIMARY KEY (scan_id, record_type, key_path)
);

-- Records that were not inserted, and why. Never silently dropped.
CREATE TABLE IF NOT EXISTS dropped_record (
    dropped_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scan (scan_id) ON DELETE CASCADE,
    source_file TEXT NOT NULL,
    source_line INTEGER NOT NULL,
    reason TEXT NOT NULL,
    record_type TEXT,
    raw_record_hash TEXT
);

-- Spec 6.4: uncertain records must never be silently merged. When one dedup
-- key resolves to two different raw records, both are kept and every
-- participant is listed here.
CREATE TABLE IF NOT EXISTS dedup_conflict (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scan (scan_id) ON DELETE CASCADE,
    dedup_key TEXT NOT NULL,
    dedup_key_kind TEXT NOT NULL,
    record_uid INTEGER NOT NULL REFERENCES request_record (record_uid) ON DELETE CASCADE,
    raw_record_hash TEXT NOT NULL,
    source_file TEXT,
    source_line INTEGER
);

CREATE TABLE IF NOT EXISTS ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class InsertOutcome:
    """Result of offering one parsed record to the ledger."""

    status: str  # inserted | duplicate | conflict
    record_uid: int | None
    dedup_key: str
    dedup_key_kind: str
    raw_record_hash: str


@dataclass(frozen=True)
class DedupeKey:
    key: str
    kind: str


def compute_dedup_key(
    message_id: str | None,
    request_id: str | None,
    raw_record_hash: str,
) -> DedupeKey:
    """Deterministic identity for a request record (spec 6.4).

    Primary key is ``(message_id, request_id)``. When either id is missing we
    fall back, in order, to the surviving id and finally to the raw record
    hash. The kind is returned so the caller can persist which fallback was
    used. No two *different* raw records ever collapse onto a key here: the
    clash is detected at insert time and recorded, not merged away.
    """
    mid = (message_id or "").strip()
    rid = (request_id or "").strip()
    if mid and rid:
        return DedupeKey(f"{mid}\x1f{rid}", "ids")
    if mid:
        return DedupeKey(f"{mid}\x1f\x00no-request-id", "message_id")
    if rid:
        return DedupeKey(f"\x00no-message-id\x1f{rid}", "request_id")
    return DedupeKey(f"\x00no-ids\x1f{raw_record_hash}", "raw_record_hash")


class Ledger:
    """SQLite-backed canonical ledger."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Ledger":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Ledger.connect() has not been called")
        return self._conn

    def connect(self) -> "Ledger":
        if self._conn is None:
            self._conn = sqlite3.connect(self.path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def initialize(self) -> "Ledger":
        """Create the schema. Idempotent."""
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute(
            "INSERT OR REPLACE INTO ledger_meta (key, value) VALUES (?, ?)",
            ("ledger_schema_version", LEDGER_SCHEMA_VERSION),
        )
        self.conn.commit()
        return self

    def commit(self) -> None:
        self.conn.commit()

    # -- scans -------------------------------------------------------------

    def begin_scan(
        self,
        *,
        parser_version: str,
        schema_status: str,
        started_utc: str,
        log_root: str | None,
        schema_id: str | None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO scan (
                parser_version, ledger_schema_version, schema_id,
                schema_status, log_root, started_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                parser_version,
                LEDGER_SCHEMA_VERSION,
                schema_id,
                schema_status,
                log_root,
                started_utc,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_scan(
        self, scan_id: int, *, finished_utc: str, diagnostics: Mapping[str, Any]
    ) -> None:
        self.conn.execute(
            "UPDATE scan SET finished_utc = ?, diagnostics_json = ? WHERE scan_id = ?",
            (finished_utc, json.dumps(diagnostics, sort_keys=True), scan_id),
        )
        self.conn.commit()

    def add_counter(self, scan_id: int, name: str, value: int) -> None:
        self.conn.execute(
            """
            INSERT INTO scan_counter (scan_id, name, value) VALUES (?, ?, ?)
            ON CONFLICT (scan_id, name) DO UPDATE SET value = value + excluded.value
            """,
            (scan_id, name, int(value)),
        )

    def add_counters(self, scan_id: int, counts: Mapping[str, int]) -> None:
        for name, value in counts.items():
            self.add_counter(scan_id, name, value)

    def observe_key(self, scan_id: int, record_type: str, key_path: str) -> None:
        self.conn.execute(
            """
            INSERT INTO schema_observed_key (scan_id, record_type, key_path, occurrences)
            VALUES (?, ?, ?, 1)
            ON CONFLICT (scan_id, record_type, key_path)
            DO UPDATE SET occurrences = occurrences + 1
            """,
            (scan_id, record_type, key_path),
        )

    def record_source_file(
        self,
        scan_id: int,
        source_file: str,
        *,
        size_bytes: int | None,
        sha256: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO scan_source_file
                (scan_id, source_file, size_bytes, sha256)
            VALUES (?, ?, ?, ?)
            """,
            (scan_id, source_file, size_bytes, sha256),
        )

    def note_source_lines(self, scan_id: int, source_file: str, lines_read: int) -> None:
        self.conn.execute(
            """
            UPDATE scan_source_file SET lines_read = ?
            WHERE scan_id = ? AND source_file = ?
            """,
            (lines_read, scan_id, source_file),
        )

    def record_drop(
        self,
        scan_id: int,
        *,
        source_file: str,
        source_line: int,
        reason: str,
        record_type: str | None = None,
        raw_record_hash: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO dropped_record
                (scan_id, source_file, source_line, reason, record_type, raw_record_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (scan_id, source_file, source_line, reason, record_type, raw_record_hash),
        )

    # -- records -----------------------------------------------------------

    def insert_record(
        self,
        record: Mapping[str, Any],
        *,
        scan_id: int,
        source_file: str,
        source_line: int,
    ) -> InsertOutcome:
        """Insert one section 6.1 request record.

        Exact repeats (same dedup key, same raw hash) are skipped as
        duplicates. A dedup key that resolves to a *different* raw record is
        written and flagged; every participant is added to ``dedup_conflict``
        so the uncertainty is explicit rather than silently merged.
        """
        normalized = normalize_record(record)
        raw_hash = normalized["raw_record_hash"]
        if not isinstance(raw_hash, str) or not raw_hash:
            raise ValueError("raw_record_hash is required on every record")

        dedupe = compute_dedup_key(
            normalized["message_id"], normalized["request_id"], raw_hash
        )

        existing = self.conn.execute(
            "SELECT record_uid, raw_record_hash FROM request_record WHERE dedup_key = ?",
            (dedupe.key,),
        ).fetchall()

        same_hash = [r for r in existing if r["raw_record_hash"] == raw_hash]
        if same_hash:
            return InsertOutcome(
                status="duplicate",
                record_uid=int(same_hash[0]["record_uid"]),
                dedup_key=dedupe.key,
                dedup_key_kind=dedupe.kind,
                raw_record_hash=raw_hash,
            )

        conflict = len(existing) > 0
        values: list[Any] = [
            dedupe.key,
            dedupe.kind,
            int(conflict),
            int(normalized["is_zero_usage"]),
            int(normalized["is_synthetic"]),
            normalized["schema_status"],
        ]
        values.extend(_encode_value(name, normalized[name]) for name in REQUEST_RECORD_FIELDS)

        placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
        columns = ", ".join(_INSERT_COLUMNS)
        cur = self.conn.execute(
            f"INSERT INTO request_record ({columns}) VALUES ({placeholders})",
            values,
        )
        record_uid = int(cur.lastrowid)

        self.conn.execute(
            """
            INSERT INTO record_provenance (record_uid, scan_id, source_file, source_line)
            VALUES (?, ?, ?, ?)
            """,
            (record_uid, scan_id, source_file, source_line),
        )

        if conflict:
            # Flag every participant, including rows written before the clash
            # was known, so consumers can exclude uncertain records with one
            # predicate.
            self.conn.execute(
                "UPDATE request_record SET dedup_conflict = 1 WHERE dedup_key = ?",
                (dedupe.key,),
            )
            participants = [(record_uid, raw_hash, source_file, source_line)]
            participants.extend(
                (int(row["record_uid"]), str(row["raw_record_hash"]), None, None)
                for row in existing
            )
            for uid, hash_value, src_file, src_line in participants:
                self.conn.execute(
                    """
                    INSERT INTO dedup_conflict
                        (scan_id, dedup_key, dedup_key_kind, record_uid,
                         raw_record_hash, source_file, source_line)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (scan_id, dedupe.key, dedupe.kind, uid, hash_value, src_file, src_line),
                )

        return InsertOutcome(
            status="conflict" if conflict else "inserted",
            record_uid=record_uid,
            dedup_key=dedupe.key,
            dedup_key_kind=dedupe.kind,
            raw_record_hash=raw_hash,
        )

    # -- reads -------------------------------------------------------------

    def count(self, table: str = "request_record") -> int:
        allowed = {"request_record", "dropped_record", "dedup_conflict", "scan"}
        if table not in allowed:
            raise ValueError(f"unsupported table: {table}")
        row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])

    def counters(self, scan_id: int) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT name, value FROM scan_counter WHERE scan_id = ? ORDER BY name",
            (scan_id,),
        ).fetchall()
        return {str(r["name"]): int(r["value"]) for r in rows}

    def rows(self, where: str = "", params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        sql = "SELECT * FROM request_record"
        if where:
            sql += f" WHERE {where}"
        sql += " ORDER BY record_uid"
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def unconflicted_rows(self) -> list[sqlite3.Row]:
        """Rows with no unresolved identity clash. Downstream maths uses these."""
        return self.rows("dedup_conflict = 0")

    def all_text_values(self) -> list[str]:
        """Every TEXT value stored in the ledger, for privacy assertions."""
        values: list[str] = []
        for row in self.rows():
            for key in row.keys():
                value = row[key]
                if isinstance(value, str):
                    values.append(value)
        return values


def _encode_value(name: str, value: Any) -> Any:
    if name == "tool_names":
        if value is None:
            return None
        if isinstance(value, str):
            return value  # already serialised by the parser
        return json.dumps(list(value))
    if isinstance(value, bool):
        return int(value)
    return value


def normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Apply spec defaults and type coercion to a section 6.1 record.

    Any field the source log did not provide stays NULL. Enumerated fields
    fall back to the spec's ``unknown`` member rather than to a guess.
    """
    out: dict[str, Any] = {name: record.get(name) for name in REQUEST_RECORD_FIELDS}

    for name, default in _ALWAYS_PRESENT_DEFAULTS.items():
        if out.get(name) is None:
            out[name] = default

    for name in (
        "is_sidechain",
        "fallback_flag",
        "compaction_signal",
        "image_or_context_trim_signal",
        "test_ran",
        "test_passed",
    ):
        out[name] = _coerce_bool(out.get(name))

    for name in (
        "seq",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "cache_write_total_tokens",
    ):
        out[name] = _coerce_int(out.get(name))

    out["request_bucket"] = _coerce_enum(out["request_bucket"], REQUEST_BUCKETS)
    out["model_source"] = _coerce_enum(out["model_source"], MODEL_SOURCES)
    out["effort_source"] = _coerce_enum(out["effort_source"], EFFORT_SOURCES)
    out["effort_mechanism"] = _coerce_enum(out["effort_mechanism"], EFFORT_MECHANISMS)
    out["thinking_mode"] = _coerce_enum(out["thinking_mode"], THINKING_MODES)
    out["cache_diagnostic_source"] = _coerce_enum(
        out["cache_diagnostic_source"], CACHE_DIAGNOSTIC_SOURCES
    )

    # Spec 6.1/6.2: effort_source must never present an absent effort as
    # observed. With no established effort value the source is 'unknown'.
    if out["effort"] is None:
        out["effort_source"] = "unknown"
        out["effort_mechanism"] = "unknown"

    out["is_zero_usage"] = bool(record.get("is_zero_usage", False))
    out["is_synthetic"] = bool(record.get("is_synthetic", False))
    out["schema_status"] = str(record.get("schema_status") or UNKNOWN)
    return out


def _coerce_bool(value: Any) -> int | None:
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


def _coerce_int(value: Any) -> int | None:
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


def _coerce_enum(value: Any, allowed: Iterable[str]) -> str:
    if isinstance(value, str) and value in tuple(allowed):
        return value
    return "unknown"
