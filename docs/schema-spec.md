# Ledger format spec (v3)

The ledger is the durable artifact; the tool is just one reader/writer of it. This documents the
on-disk format precisely enough for another tool to read or write it. The reference
implementation is [`schema.py`](../llm_resource_tally/schema.py); on any disagreement, the code
wins.

## Files (under `.llm_resource_tally/` at the repo root)

| Path | Role | Committed |
|------|------|-----------|
| `ledger/ledger.jsonl` | active append-only shard (source of truth) | yes |
| `ledger/ledger.<UTCstamp>.jsonl` | rotated archives (once a shard passes ~1 MB) | yes |
| `resource-ledger.jsonl` | legacy pre-rolling flat log, read first if present | yes |
| `.gitattributes` | marks `ledger/*.jsonl` as `merge=union` | yes |
| `settings.json` | `{"backends": [...]}` the passive hook records | yes |
| `lifetime-totals.json` | regenerable rollup (readable keys) | optional |
| `badge.json` | shields.io endpoint summary (regenerable) | optional |

Readers **glob all `*.jsonl` shards**, oldest first (archives sort before the active file), and
de-duplicate (below). Files are pure append-only logs, which is what makes `merge=union` safe.

## Row encoding

Each line is one JSON object, no whitespace (`separators=(",",":")`), UTF-8. Two row kinds share
a common header. Keys are terse; token counts are positional arrays. Absent optional fields are
omitted, not null (except where a measured value is genuinely unknown → `null`).

**Common header**

| Key | Meaning |
|-----|---------|
| `v` | schema version (`3`) |
| `rec` | `recorded_at`, ISO-8601 (dedup tiebreak: latest wins) |
| `r` | repo basename |
| `c` | commit SHA, or `pending@YYYY-MM-DD` for un-committed sweeps |
| `ct` | commit committer-date ISO, or `null` (pending) |
| `a` | agent/backend (`claude-code`, `codex`, `opencode`, …) |
| `sid` | session id |
| `act` | activity label (omitted if none) |
| `m` | list of model ids seen in this row |

**Measured row** (adds)

| Key | Meaning |
|-----|---------|
| `n` | turns (billed API calls) counted |
| `t` | `[input, cache_write, cache_read, output]` tokens |
| `bm` | `{model: [input, cache_write, cache_read, output]}` (omitted if empty) |
| `st` | `[web_search, web_fetch]` server-tool calls (omitted if both 0) |
| `w` | wall-clock seconds spanned, float or `null` |
| `tr` | `[ts_lo, ts_hi]` first/last turn timestamps |

`billable_input = input + cache_write + cache_read` is **derived on read, never stored**.

**Compaction row** (`k` = `"cx"`; replaces the measured fields)

| Key | Meaning |
|-----|---------|
| `k` | `"cx"` |
| `bt` | compaction boundary timestamp |
| `cp` | `[peak_context_tokens, summary_chars]` (measured signals only) |

## De-duplication (row identity)

Readers collapse rows to one per identity, keeping the largest `rec` (latest write wins):

- measured, real commit: `("measured", sid, c)`
- measured, pending (`c` starts `pending@`): `("measured", sid, c, tr[1])` — the swept-window end
  disambiguates same-day sweeps
- compaction: `("compaction", sid, bt)`

This is what makes the log safe to `merge=union` and to carry through a history rewrite: an
observation has a stable identity independent of git SHAs, so it is counted once.

## Writer rules (to stay compatible)

1. Append only; never rewrite or delete rows. Rotate by renaming the active shard.
2. Store **measurements only** — no energy/carbon/USD/inference-time. Those are modeled post-hoc
   from these fields (see [modeling](modeling.md)) and must never be baked in.
3. Unknown measured values are `null`, never a fabricated default.
4. Emit `v:3` rows in the compact form above. Legacy verbose rows (with a `tokens` object or a
   `schema` string) are still read for back-compat.
