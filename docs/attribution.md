# How cost is attributed

The atomic unit is a **turn** (one API call, identified by `message.id`); each turn belongs to
one **session** (one transcript). The rule:

> A turn is attributed to the repo of the **next commit** it feeds. If it never feeds a commit,
> `reconcile` attributes it to the repo where the **session runs**.

- **Normal work** (session and commit in the same repo): fully automatic via the post-commit
  hook.
- **Non-committing work** (planning, a review that changed nothing, "just asking"): real tokens,
  no commit. `reconcile` sweeps them into a `pending@<date>` row so nothing is lost — **run it
  at session end** (the hook only fires on commits).
- **Cross-repo** (a session in repo A commits a fix into repo B): the git hook alone can't tell
  — the Claude CLI exposes no session id to a git hook, so it can only guess by directory. The
  **`--claude` PostToolUse hook** *can*: Claude hands it the exact session **and** the repo the
  commit landed in, so the cost is attributed to B correctly. Without it, bridge manually:
  `cd B && <rt> record --session <id> --commit <sha>` (and don't also sweep those turns in A — a
  given turn should be claimed by only one repo).
- **Submodules** are just the cross-repo case: a submodule is its own git repo, so a commit into
  it is tracked in **its own** `.llm_resource_tally/`, entirely separate from the parent — the
  ledger always follows the repo the commit lands in. Install the tool in each submodule you
  want auto-tracked (each gets its own `post-commit` hook), **or** use `--claude` once at the
  parent — a single PostToolUse hook attributes every commit to whichever repo (parent or
  submodule) it landed in.

`--label` tags what the work was (e.g. `record --label implementation`, `reconcile --label
planning`). Every row carries an `activity`, and `rollup` breaks output tokens down
`by_activity`, so non-code work is counted *and* attributable.

## Correctness guarantees

- **No double-count under concurrency.** Usage is attributed per session (each agent = its own
  transcript = disjoint turns); rows are keyed `(session_id, commit)`, appended under an `flock`.
- **No undercount.** `record` sweeps a session's turns in `(watermark, commit_ts]`; the next
  commit continues from that watermark. `reconcile` catches turns that never produced a commit.
- **Dedup by message id.** A transcript logs each message several times with identical usage
  (~2.6× overcount on cache reads if summed raw). The tool dedups by `message.id` — do not
  hand-count. Readers also collapse duplicate rows (latest-wins), so the ledger is safe to
  `merge=union` and to carry through history rewrites.
- **The ledger tip trails the commit tip by one row, by design** — recording commit *N* modifies
  the ledger, which lands in commit *N+1*. A fixed point (a commit's tree can't contain its own
  hash), not a bug.

## History rewrites (rebase, `filter-repo`, squash)

Your **totals are always safe**: a turn's identity is its `message.id`, immutable under any git
rewrite. What can go stale is git bookkeeping: `commit` fields may point at SHAs that no longer
exist (`commits_accounted` becomes approximate). If a rewrite *drops* commits, it can drop the
ledger rows they carried — **run `reconcile` after any rewrite** and the timestamp watermark
re-captures the missing turns. Policy: never hand-delete rows; on a merge/rebase conflict keep
both sides (that's what `merge=union` does); readers dedup.

## Context compaction (measured signals, cost imputed later)

When `/compact` fires, the harness runs a real summarization call but writes **no `usage`
object** — only a `compact_boundary` marker and an `isCompactSummary` record. Rather than
fabricate a token count, `record`/`reconcile` add a `kind: compaction-estimate` row per boundary
holding only the **measured** signals — `peak_context_tokens` and `summary_chars` — keyed by
boundary timestamp so it's never double-counted. `rollup` reports these under
`compaction_signals`; conversions happen in the modeling pass. Disable with
`--no-estimate-compaction`. (The parser counts *any* record with a real `usage` object, so if a
future harness logs compaction usage it is measured automatically.)

**Backfill note:** past sessions are recoverable only as far back as the agent retains transcripts
(Claude Code defaults to **30 days**, `cleanupPeriodDays`). Set it high *now* if you want a deep
baseline later. See **[backfill](backfill.md)** for how to recover pre-install history and the
limits that bound it.
