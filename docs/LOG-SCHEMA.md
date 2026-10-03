# LOG-SCHEMA.md

Purpose: record what the Claude Code session log **actually contains** for the
client version in use, so the parser in `src/tamias/sources/claude_code.py` is
built from evidence rather than from assumption.

Spec references: section 6.1 (request record), 6.2 (why each field exists),
6.3 (privacy), 6.4 (deduplication), Gate B (effort observability).
Governing rules: AGENTS.md rules 2, 3 and 6.

---

## 1. Observation status

```text
log root inspected : ~/.claude/projects/**/*.jsonl
result             : directory does not exist on this machine
files inspected    : 0
records inspected  : 0
client version     : UNKNOWN (no log carried a version field, because no log exists)
schema status      : UNVERIFIED
```

Reproduce with:

```bash
python tools/inspect_logs.py
python tools/inspect_logs.py --log-root <path-to-real-logs>
```

`tools/inspect_logs.py` prints record types, key paths and counts only. It never
prints message content, tool arguments or code.

### Consequence

Every field in section 6.1 is marked `unknown` below. Not `absent`: absence was
not observed. `unknown` means *not establishable from the evidence available*,
the state AGENTS.md rule 3 requires.

The parser therefore ships with:

- `SCHEMA_STATUS = "unverified"` in `src/tamias/sources/claude_code.py`;
- `schema_status = 'unverified'` on every record it writes;
- a declared, versioned field map of **candidate** key paths, treated as a
  hypothesis rather than as fact;
- `schema_observed_key` in the ledger, recording the key paths actually observed
  during each scan so this document can be corrected from evidence.

No claim below is derived from documentation, memory or convention.

---

## 2. Section 6.1 field presence

Status vocabulary:

| Status | Meaning |
|---|---|
| `present` | Observed in real logs of this client version |
| `absent` | Observed to be missing in real logs of this client version |
| `unknown` | Not establishable; no logs available |
| `derived` | Not a log field; computed by the ledger from other fields |

| Spec 6.1 field | Status | Source key path (hypothesis) | Notes |
|---|---|---|---|
| `run_id` | unknown | `runId`, `run_id` | Falls back to the scan invocation's run id |
| `session_id` | unknown | `sessionId`, `session_id` | Candidate name from convention only |
| `seq` | derived | — | Line ordinal within the log file |
| `ts_utc` | unknown | `timestamp`, `ts` | Normalisation to UTC not yet verified |
| `request_id` | unknown | `requestId`, `request_id` | Needed for the 6.4 primary key |
| `message_id` | unknown | `message.id`, `messageId`, `message_id` | Needed for the 6.4 primary key |
| `is_sidechain` | unknown | `isSidechain`, `is_sidechain` | Drives `request_bucket = subagent` |
| `request_bucket` | derived | — | `subagent` if sidechain, `compaction` if a compaction signal exists, `main` if ids exist, else `unknown` |
| `agent` | derived | — | `claude-code` when the record type is recognised |
| `agent_version` | unknown | `version`, `agentVersion` | Field meaning unconfirmed |
| `provider` | unknown | `provider` | |
| `surface` | unknown | `surface` | |
| `model_id` | unknown | `message.model`, `model`, `modelId` | Original id preserved; no alias normalisation yet |
| `model_source` | unknown | `modelSource`, `model_source` | `unknown` unless the log states provenance |
| `effort` | unknown | `effort`, `reasoningEffort` | **Gate B, see section 4** |
| `effort_source` | derived | — | `recorded` only if an effort key is present; otherwise `unknown` |
| `effort_mechanism` | unknown | `effortMechanism` | Spec values: `per_message`, `top_level`, `native_budget` |
| `thinking_mode` | unknown | `thinkingMode`, `thinking` | |
| `speed_or_service_tier` | unknown | `speed`, `serviceTier` | |
| `fallback_flag` | unknown | `isFallback`, `fallback` | |
| `fallback_target_model` | unknown | `fallbackTargetModel` | |
| `input_tokens` | unknown | `message.usage.input_tokens` + aliases | |
| `output_tokens` | unknown | `message.usage.output_tokens` + aliases | |
| `cache_read_tokens` | unknown | `message.usage.cache_read_input_tokens` + aliases | |
| `cache_write_tokens` | unknown | `message.usage.cache_creation_input_tokens` + aliases | |
| `cache_write_5m_tokens` | unknown | `message.usage.cache_creation_5m_input_tokens` + aliases | Split unconfirmed |
| `cache_write_1h_tokens` | unknown | `message.usage.cache_creation_1h_input_tokens` + aliases | Split unconfirmed |
| `cache_write_total_tokens` | derived | — | Prefers the logged total; sums the 5m/1h split only when the total is missing |
| `tool_names` | derived | tool-use block `name` only | Names only; tool arguments are never read or stored |
| `test_ran` | unknown | — | No candidate path declared; stays NULL |
| `test_passed` | unknown | — | No candidate path declared; stays NULL |
| `cache_miss_reason` | unknown | `cacheMissReason` | Provider/client specific |
| `cache_diagnostic_source` | unknown | `cacheDiagnosticSource` | `unknown` unless a diagnostic is present |
| `compaction_signal` | unknown | `isCompactSummary`, `compaction` | |
| `image_or_context_trim_signal` | unknown | `imageOrContextTrim`, `contextTrim` | |
| `raw_record_hash` | derived | — | SHA-256 of the raw line; the line itself is never stored |
| `parser_version` | derived | — | Bumped whenever field derivation changes |

`test_signal { ran, passed }` is stored as the two nullable columns `test_ran`
and `test_passed`.

---

## 3. Record types

| Record type | Status | Handling |
|---|---|---|
| `assistant` | unknown | Expected to carry usage; parsed when a token field is found |
| `user` | unknown | Expected to carry no usage; counted as `no_usage_block` |
| `system`, `summary`, `file-history-snapshot` | unknown | Recognised names, no usage expected |
| anything else | unknown | Counted in `record_type:*`, flagged as `unsupported_records`, never silently dropped |

`KNOWN_RECORD_TYPES` lists names, not verified types. A record whose type falls
outside that set is still parsed if it carries usage, and is reported as
unsupported so the count stays visible.

---

## 4. Gate B — effort observability

```text
Gate B status               : UNRESOLVED
effort observed in real logs: UNKNOWN (no logs available)
real-session effort attribution: unavailable
lab effort study            : retained if controllable
```

Because effort presence is unknown, the ledger writes `effort = NULL` and
`effort_source = 'unknown'` for every record unless an effort key is actually
present. `tests/test_ledger.py` and `tests/test_scan.py` assert this, and
`tamias scan` emits a Gate B note when no record carries effort.

Resolution requires running, on a machine that has actually used Claude Code:

```bash
python tools/inspect_logs.py --log-root ~/.claude/projects
```

then updating sections 2, 3 and 4 with observed statuses and recording the
change in `docs/AMENDMENTS.md`.

---

## 5. Cache-write split

The 5m/1h split drives pricing and cache economics (spec 6.2). Whether this
client logs it is **unknown**. Until confirmed:

- both split columns stay NULL when absent;
- `cache_write_total_tokens` prefers the logged total and sums the split only
  when the total is missing;
- no cache-economics claim may be made for this client version.

---

## 6. Privacy

Per spec 6.3 and AGENTS.md rule 2 the ledger stores metadata only. Verified by
`tests/test_privacy.py`, which scans the raw bytes of the SQLite file for
sentinel prompt, assistant, tool-argument and code strings and fails if any
appears. Tool **names** are retained; tool **arguments** are never read by the
parser.

---

## 7. How to correct this document

1. Run `tools/inspect_logs.py` against real logs.
2. For each spec 6.1 field set `present` or `absent` and paste the observed key
   path. Do not record a path that was not observed.
3. Set `SCHEMA_STATUS` in `src/tamias/sources/claude_code.py` to `verified` only
   when every `unknown` above is resolved.
4. Bump `PARSER_VERSION`.
5. Record the amendment in `docs/AMENDMENTS.md` with the date and the evidence.
