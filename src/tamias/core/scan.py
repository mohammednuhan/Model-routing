"""Scan pipeline: build the canonical ledger from local session logs.

Local file reads only. No network access (spec: scan/report/receipt/doctor
never make network calls).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..core import ledger as ledger_mod
from ..core.ledger import Ledger
from ..sources import claude_code

DEFAULT_LOG_ROOT = Path.home() / ".claude" / "projects"
DEFAULT_DB_PATH = Path.home() / ".tamias" / "ledger.sqlite3"


@dataclass
class ScanDiagnostics:
    """Everything ``tamias scan`` needs to report about a parse."""

    scan_id: int = 0
    log_root: str = ""
    db_path: str = ""
    parser_version: str = claude_code.PARSER_VERSION
    schema_id: str = claude_code.SCHEMA_ID
    schema_status: str = claude_code.SCHEMA_STATUS
    files_found: int = 0
    files_read: int = 0
    lines_read: int = 0
    blank_lines: int = 0
    records_seen: int = 0
    records_inserted: int = 0
    duplicates_skipped: int = 0
    dedup_conflicts: int = 0
    usage_records: int = 0
    zero_usage_records: int = 0
    synthetic_records: int = 0
    unsupported_records: int = 0
    dropped_records: int = 0
    effort_recorded: int = 0
    effort_unknown: int = 0
    record_type_counts: dict[str, int] = field(default_factory=dict)
    drop_reason_counts: dict[str, int] = field(default_factory=dict)
    dedup_key_kind_counts: dict[str, int] = field(default_factory=dict)
    bucket_counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "log_root": self.log_root,
            "db_path": self.db_path,
            "parser_version": self.parser_version,
            "schema_id": self.schema_id,
            "schema_status": self.schema_status,
            "files_found": self.files_found,
            "files_read": self.files_read,
            "lines_read": self.lines_read,
            "blank_lines": self.blank_lines,
            "records_seen": self.records_seen,
            "records_inserted": self.records_inserted,
            "duplicates_skipped": self.duplicates_skipped,
            "dedup_conflicts": self.dedup_conflicts,
            "usage_records": self.usage_records,
            "zero_usage_records": self.zero_usage_records,
            "synthetic_records": self.synthetic_records,
            "unsupported_records": self.unsupported_records,
            "dropped_records": self.dropped_records,
            "effort_recorded": self.effort_recorded,
            "effort_unknown": self.effort_unknown,
            "record_type_counts": dict(sorted(self.record_type_counts.items())),
            "drop_reason_counts": dict(sorted(self.drop_reason_counts.items())),
            "dedup_key_kind_counts": dict(sorted(self.dedup_key_kind_counts.items())),
            "bucket_counts": dict(sorted(self.bucket_counts.items())),
            "notes": list(self.notes),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def scan(
    *,
    log_root: str | Path | None = None,
    db_path: str | Path | None = None,
    files: Sequence[str | Path] | None = None,
    run_id: str | None = None,
) -> ScanDiagnostics:
    """Parse local logs into the ledger and return parser diagnostics."""
    root = Path(log_root).expanduser() if log_root is not None else DEFAULT_LOG_ROOT
    db = Path(db_path).expanduser() if db_path is not None else DEFAULT_DB_PATH
    db.parent.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = (
        [Path(p).expanduser() for p in files] if files is not None else claude_code.iter_log_files(root)
    )

    diagnostics = ScanDiagnostics(log_root=str(root), db_path=str(db))
    diagnostics.files_found = len(paths)

    if not paths:
        diagnostics.notes.append(
            "no .jsonl log files found; nothing to scan (this is not an error)"
        )

    store = Ledger(db).connect()
    try:
        store.initialize()
        diagnostics.scan_id = store.begin_scan(
            parser_version=claude_code.PARSER_VERSION,
            schema_status=claude_code.SCHEMA_STATUS,
            started_utc=_now(),
            log_root=str(root),
            schema_id=claude_code.SCHEMA_ID,
        )
        scan_id = diagnostics.scan_id

        for path in paths:
            size, digest = _file_digest(path)
            store.record_source_file(scan_id, str(path), size_bytes=size, sha256=digest)
            result = claude_code.parse_file(path, run_id=run_id)
            diagnostics.files_read += result.files_read
            diagnostics.lines_read += result.lines_read
            diagnostics.blank_lines += result.blank_lines
            store.note_source_lines(scan_id, str(path), result.lines_read)

            for record_type, count in result.record_type_counts.items():
                diagnostics.record_type_counts[record_type] = (
                    diagnostics.record_type_counts.get(record_type, 0) + count
                )
            for record_type, keys in result.observed_keys.items():
                for key in keys:
                    store.observe_key(scan_id, record_type, key)

            diagnostics.records_seen += len(result.records) + len(result.issues)

            for issue in result.issues:
                diagnostics.dropped_records += 1
                diagnostics.drop_reason_counts[issue.reason] = (
                    diagnostics.drop_reason_counts.get(issue.reason, 0) + 1
                )
                if issue.record_type and issue.record_type not in claude_code.KNOWN_RECORD_TYPES:
                    diagnostics.unsupported_records += 1
                store.record_drop(
                    scan_id,
                    source_file=issue.source_file,
                    source_line=issue.source_line,
                    reason=issue.reason,
                    record_type=issue.record_type,
                    raw_record_hash=issue.raw_record_hash,
                )

            for parsed in result.records:
                outcome = store.insert_record(
                    parsed.record,
                    scan_id=scan_id,
                    source_file=parsed.source_file,
                    source_line=parsed.source_line,
                )
                if outcome.status == "duplicate":
                    diagnostics.duplicates_skipped += 1
                else:
                    # Both 'inserted' and 'conflict' write a ledger row. A
                    # conflict is an additional flag, not a rejection.
                    diagnostics.records_inserted += 1
                if outcome.status == "conflict":
                    diagnostics.dedup_conflicts += 1
                    diagnostics.notes.append(
                        "dedup conflict: one identity key resolved to two different raw "
                        f"records (dedup_key_kind={outcome.dedup_key_kind}); both retained"
                    )
                diagnostics.dedup_key_kind_counts[outcome.dedup_key_kind] = (
                    diagnostics.dedup_key_kind_counts.get(outcome.dedup_key_kind, 0) + 1
                )

                if outcome.status == "duplicate":
                    continue

                diagnostics.usage_records += 1
                if parsed.is_zero_usage:
                    diagnostics.zero_usage_records += 1
                if parsed.is_synthetic:
                    diagnostics.synthetic_records += 1
                if parsed.record["effort"] is None:
                    diagnostics.effort_unknown += 1
                else:
                    diagnostics.effort_recorded += 1
                bucket = parsed.record.get("request_bucket") or "unknown"
                diagnostics.bucket_counts[bucket] = diagnostics.bucket_counts.get(bucket, 0) + 1

        store.add_counters(
            scan_id,
            {
                "records_seen": diagnostics.records_seen,
                "records_inserted": diagnostics.records_inserted,
                "duplicates_skipped": diagnostics.duplicates_skipped,
                "dedup_conflicts": diagnostics.dedup_conflicts,
                "usage_records": diagnostics.usage_records,
                "zero_usage_records": diagnostics.zero_usage_records,
                "synthetic_records": diagnostics.synthetic_records,
                "unsupported_records": diagnostics.unsupported_records,
                "dropped_records": diagnostics.dropped_records,
                "effort_recorded": diagnostics.effort_recorded,
                "effort_unknown": diagnostics.effort_unknown,
            },
        )
        for name, value in diagnostics.drop_reason_counts.items():
            store.add_counter(scan_id, f"drop_reason:{name}", value)
        for name, value in diagnostics.record_type_counts.items():
            store.add_counter(scan_id, f"record_type:{name}", value)
        for name, value in diagnostics.dedup_key_kind_counts.items():
            store.add_counter(scan_id, f"dedup_key_kind:{name}", value)

        if diagnostics.schema_status == "unverified":
            diagnostics.notes.append(
                "log schema is UNVERIFIED for this client version: field presence must be "
                "confirmed against real logs (see docs/LOG-SCHEMA.md)"
            )
        if diagnostics.effort_unknown and not diagnostics.effort_recorded:
            diagnostics.notes.append(
                "Gate B: effort was not observed in any record; effort_source='unknown'. "
                "Real-session effort attribution is unavailable."
            )
        if diagnostics.dropped_records:
            diagnostics.notes.append(
                f"{diagnostics.dropped_records} record(s) were not written to the ledger; "
                "reasons are counted in dropped_record and drop_reason:* counters"
            )

        payload = diagnostics.as_dict()
        payload["ledger_rows_total"] = store.count("request_record")
        payload["ledger_schema_version"] = ledger_mod.LEDGER_SCHEMA_VERSION
        store.finish_scan(scan_id, finished_utc=_now(), diagnostics=payload)
        store.commit()
    finally:
        store.close()

    return diagnostics
