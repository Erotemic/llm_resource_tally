# Ledger format spec (v3)

The ledger is the durable artifact; the tool is just one reader/writer of it. This documents the
on-disk format precisely enough for another tool to read or write it. The reference
implementation is [`schema.py`](../llm_resource_tally/schema.py); on any disagreement, the code
wins.

## Storage envelope

The row encoding below is independent of the selected storage mode:

- **`local`** (default) appends under `.llm_resource_tally/local/`; explicit `publish` creates
  the tracked append-only ledger under `.llm_resource_tally/ledger/`.

- **`committed`** stores append-only shards under `.llm_resource_tally/ledger/` and normally
  commits them with the repository.
- **`ignored`** uses the same file layout but manages a `.gitignore` block so the observations
  remain local.
- **`notes`** stores the same compact JSON objects, one per line, in
  `refs/notes/llm-resource-tally`. Mutable settings and generated reports live beneath the git
  common directory. Git notes require explicit fetch/push configuration to travel between clones.

Readers union published shards, local spool shards, and locally available notes, then de-duplicate
the combined rows. This
allows a repository to change storage modes without making earlier observations invisible.

## File layout

| Path | Role | Versioned by default |
|------|------|----------------------|
| `local/ledger.jsonl` | active append-only local spool | no |
| `local/ledger.<UTCstamp>.jsonl` | rotated local spool archives | no |
| `ledger/ledger.jsonl` | active tracked shard: `publish` appends here, `committed`/`ignored` modes record here directly | yes (not in `ignored`) |
| `ledger/ledger.<UTCstamp>.jsonl` | shards retired from the active one once it passes the size limit | yes (not in `ignored`) |
| `ledger/ledger.sha256-<digest>.jsonl` | legacy per-publication shard; still read, no longer written | yes |
| `resource-ledger.jsonl` | legacy pre-rolling flat log, read first if present | yes |
| `.gitattributes` | marks `ledger/*.jsonl` as `merge=union` | yes |
| `settings.json` | portable backends + installation policy (`storage`, `tool_format`, `tool_path`, `modeling`) | yes |
| `lifetime-totals.json` | published rollup, refreshed by `publish`; includes readable totals plus machine-readable `accounting_scope` limitations | yes |
| `local/lifetime-totals.json` | working rollup written by `rollup` | no |

File readers glob all published and local `*.jsonl` shards. Files contain append-only observations;
publication can overlap safely because row identity de-duplicates the union.

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

- measured, real commit: `("measured", agent, sid, c)`
- measured, pending (`c` starts `pending@`): `("measured", agent, sid, c, tr[1])` — the swept-window end
  disambiguates same-day sweeps
- compaction: `("compaction", agent, sid, bt)`

This makes duplicate copies of the **same row identity** harmless (for example local/published
overlap or a `merge=union` duplicate). It is deliberately **not** a global billed-turn identity:
real-commit row identity contains the commit SHA, and aggregate rows do not retain every source
message id. The same underlying turns allocated under another commit/session/repository therefore
form a different row and are not removed by this reader-level deduplication.

Sequential same-machine cross-repo allocation is guarded separately by the local claims file; that
mechanism is best-effort and not synchronized across machines. Organization-wide/fork-aware
deduplication requires globally stable observation identities that the v3 ledger does not yet
store.

## Writer rules (to stay compatible)

1. Append only; never rewrite or delete rows. Rotate by renaming the active shard.
2. Store **measurements only** — no energy/carbon/USD/inference-time. Those are modeled post-hoc
   from these fields (see [modeling](modeling.md)) and must never be baked in.
3. Unknown measured values are `null`, never a fabricated default.
4. Emit `v:3` rows in the compact form above. Legacy verbose rows (with a `tokens` object or a
   `schema` string) are still read for back-compat.
5. Do not treat row identity as proof of global observation uniqueness. Writers that allocate one
   transcript across repositories must coordinate allocation explicitly; the reference writer uses
   a local per-user claim floor for sequential same-machine work.
