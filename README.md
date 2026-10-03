# Tamias

Transition-aware inference economics system for coding agents.

> **Status:** observation instrument. `scan` is implemented; `report`,
> `receipt`, `doctor` and `probe` are stubs.

This codebase follows `TAMIAS-FINAL-ARCHITECTURE.md` (frozen spec). Only bug
fixes, parser corrections, provider-semantics corrections and reproducibility
amendments are allowed.

## Rules that constrain the code

See `AGENTS.md`. In short:

- The product never persists prompt text, code, tool arguments or assistant text.
- Anything not establishable is `UNKNOWN`, never a guess.
- Observed cost, estimated exposure and causal effect are separate quantities.
- Every dollar value shows source tokens, price sheet id, formula, evidence status.
- Commands that can spend money default to a mock provider; real runs need
  `--real`, `--max-spend` and interactive confirmation, and abort at the cap.

## Installation

```bash
pip install -e .
pip install -e ".[test]"   # to run the tests
```

Python 3.11+. SQLite comes from the standard library. The CLI uses `click`.

## Commands

```bash
tamias --help
tamias scan      # build the canonical ledger from local session logs
tamias report    # stub
tamias receipt   # stub
tamias doctor    # stub
tamias probe     # stub
```

`scan`, `report`, `receipt` and `doctor` never make network calls.

## Scan

```bash
tamias scan                                   # ~/.claude/projects -> ~/.tamias/ledger.sqlite3
tamias scan --log-root <dir> --db <file>
tamias scan --file a.jsonl --file b.jsonl
tamias scan --json                            # machine-readable diagnostics
```

`scan` parses JSONL session logs into the section 6.1 request record and prints
parser diagnostics: records seen, inserted, duplicates skipped, dedup conflicts,
dropped and unsupported records, zero-usage and synthetic records, effort
observability, dedup key basis, request buckets and record types.

Defaults: log root `~/.claude/projects`, ledger `~/.tamias/ledger.sqlite3`.

## Establish the log schema first

The parser's field map is **unverified**: no real Claude Code logs were
available on the machine where this was written, so every field in
`docs/LOG-SCHEMA.md` is marked `unknown` and every parsed record carries
`schema_status = 'unverified'`.

```bash
python tools/inspect_logs.py                    # field names and counts only
python tools/inspect_logs.py --log-root <dir>
```

The inspector prints record types, key paths and counts. It never prints message
content, tool arguments or code.

## Tests

```bash
python -m pytest
```

Fixtures are synthetic. `tests/test_privacy.py` asserts that no prompt, code,
tool-argument or assistant text reaches the database by scanning the raw bytes
of the SQLite file.

## Layout

```text
src/tamias/core/       ledger, parser-facing scan pipeline
src/tamias/sources/    session-log adapters (metadata only)
src/tamias/probe/      opt-in provider probes (not implemented)
registry/prices/       dated price sheets
registry/semantics/    versioned provider semantics with evidence
lab/                   research harness (not shipped as the public package)
tests/                 pytest suite
docs/                  architecture, log schema, semantics, prereg, amendments
tools/                 log inspection utilities
```

## License

MIT. See `LICENSE`.
