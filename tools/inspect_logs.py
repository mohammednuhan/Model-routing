"""Inspect Claude Code session logs without reading their content.

Reports record types, field names and counts only. Message text, tool
arguments and code are never printed, hashed into the report or retained
beyond the JSON parse of a single line.

Usage:

    python tools/inspect_logs.py
    python tools/inspect_logs.py --log-root ~/.claude/projects
    python tools/inspect_logs.py --json

Field *presence* and counts are observations. They are not, on their own,
evidence of what a field means. Interpretation belongs in
``docs/LOG-SCHEMA.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator

# Field-name fragments we care about, mapped to the question they answer.
USAGE_TOKENS = ("input", "output", "cache_read", "cache_creation", "cache_write")
CACHE_WRITE_SPLIT = ("5m", "1h", "ephemeral", "short", "long")
IDENTITY = ("requestid", "request_id", "message", "uuid", "parentuuid", "sessionid", "version")
SIDECHAIN = ("sidechain", "is_sidechain")
TOPICS = {
    "effort": ("effort", "reasoning_effort", "reasoningeffort"),
    "thinking": ("thinking", "reasoning", "interleaved"),
    "fallback": ("fallback",),
    "compaction": ("compact", "summar", "microcompact"),
    "cache_diagnostic": ("cache_miss", "cachemiss", "cache_diag", "breakpoint", "ttl"),
    "service_tier": ("service_tier", "servicetier", "speed", "priority"),
}


def find_log_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("TAMIAS_LOG_ROOT")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude" / "projects"


def iter_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                yield line


def walk_keys(node: Any, prefix: str = "", depth: int = 0, out: set[str] | None = None) -> set[str]:
    """Collect key paths only. Values are discarded immediately."""
    if out is None:
        out = set()
    if depth > 8:
        return out
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.add(path)
            if isinstance(value, (dict, list)):
                walk_keys(value, path, depth + 1, out)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                walk_keys(item, f"{prefix}[]", depth + 1, out)
    return out


def classify(keys: set[str]) -> dict[str, list[str]]:
    """Map observed key paths onto the questions we need answered."""
    found: dict[str, list[str]] = {}

    usage = sorted(k for k in keys if any(t in k.lower() for t in USAGE_TOKENS))
    if usage:
        found["usage"] = usage

    split = sorted(k for k in keys if any(t in k.lower() for t in CACHE_WRITE_SPLIT))
    if split:
        found["cache_write_split"] = split

    identity = sorted(k for k in keys if any(t in k.lower().replace("_", "") for t in IDENTITY))
    if identity:
        found["identity"] = identity

    sidechain = sorted(k for k in keys if any(t in k.lower() for t in SIDECHAIN))
    if sidechain:
        found["sidechain"] = sidechain

    for topic, needles in TOPICS.items():
        hits = sorted(k for k in keys if any(n in k.lower() for n in needles))
        if hits:
            found[topic] = hits

    return found


def inspect_logs(log_root: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "log_root": str(log_root),
        "log_root_exists": log_root.exists(),
        "files": 0,
        "lines": 0,
        "malformed_lines": 0,
        "record_types": Counter(),
        "keys_by_record_type": defaultdict(Counter),
        "classified_by_record_type": defaultdict(dict),
    }

    if not log_root.exists():
        return report

    for path in sorted(log_root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        report["files"] += 1
        for line in iter_lines(path):
            report["lines"] += 1
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                report["malformed_lines"] += 1
                continue
            if not isinstance(payload, dict):
                report["malformed_lines"] += 1
                continue
            record_type = payload.get("type")
            if not isinstance(record_type, str) or not record_type:
                record_type = "unknown"
            report["record_types"][record_type] += 1
            keys = walk_keys(payload)
            for key in keys:
                report["keys_by_record_type"][record_type][key] += 1
            report["classified_by_record_type"][record_type] = classify(keys)

    return report


def render(report: dict[str, Any]) -> str:
    out: list[str] = []
    out.append(f"log root            : {report['log_root']}")
    out.append(f"log root exists     : {report['log_root_exists']}")
    if not report["log_root_exists"]:
        out.append("")
        out.append("No log directory at this path.")
        out.append("Field presence for this client version is therefore UNKNOWN.")
        out.append("No schema claim is inferred from the absence of logs.")
        return "\n".join(out)

    out.append(f"files               : {report['files']}")
    out.append(f"lines               : {report['lines']}")
    out.append(f"malformed lines     : {report['malformed_lines']}")
    out.append("")
    out.append("RECORD TYPES")
    for name, count in sorted(report["record_types"].items()):
        out.append(f"  {name:<28} {count}")
    out.append("")
    out.append("KEY PATHS BY RECORD TYPE (names and counts only)")
    for record_type in sorted(report["keys_by_record_type"]):
        out.append(f"  [{record_type}]")
        for key, count in sorted(report["keys_by_record_type"][record_type].items()):
            out.append(f"    {key:<52} {count}")
    out.append("")
    out.append("CLASSIFIED FIELD CANDIDATES")
    for record_type in sorted(report["classified_by_record_type"]):
        grouped = report["classified_by_record_type"][record_type]
        if not grouped:
            continue
        out.append(f"  [{record_type}]")
        for topic in sorted(grouped):
            out.append(f"    {topic}: {', '.join(grouped[topic])}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report record types and field names only.")
    parser.add_argument("--log-root", default=None, help="Directory of *.jsonl session logs.")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    args = parser.parse_args(argv)

    report = inspect_logs(find_log_root(args.log_root))
    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True, default=dict)
        sys.stdout.write("\n")
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
