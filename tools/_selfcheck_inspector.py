"""Inspect a synthetic log tree with the real inspector, to prove it works.

Run:  python tools/_selfcheck_inspector.py
Uses only synthetic fixtures. Prints key names and counts, never content.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import inspect_logs  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "projects"
        session = root / "synthetic-session"
        session.mkdir(parents=True)
        lines = [
            {
                "type": "assistant",
                "version": "1.0.0",
                "sessionId": "s1",
                "timestamp": "2026-09-30T00:00:00.000Z",
                "isSidechain": False,
                "requestId": "req_01",
                "message": {
                    "id": "msg_01",
                    "model": "claude-sonnet-4-5",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "REDACTED-CONTENT"}],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 50,
                        "cache_creation_5m_input_tokens": 30,
                        "cache_creation_1h_input_tokens": 20,
                    },
                },
            },
            {
                "type": "user",
                "sessionId": "s1",
                "message": {"role": "user", "content": "REDACTED-CONTENT"},
            },
        ]
        with (session / "session.jsonl").open("w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")

        report = inspect_logs.inspect_logs(root)
        rendered = inspect_logs.render(report)

    print(rendered)
    assert "REDACTED-CONTENT" not in rendered, "inspector leaked message content"
    assert report["record_types"]["assistant"] == 1
    assert report["record_types"]["user"] == 1
    print("\nOK: content sentinel absent from inspector output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
