# How cost is attributed

The atomic unit is a **turn** (one API call, identified by `message.id`); each turn belongs to
one **session** (one transcript). The rule:

> A turn is attributed to the repo of the **next commit** it feeds. If it never feeds a commit,
> `reconcile` attributes it to the repo where the **session runs**.

- **Normal work** (session and commit in the same repo): automatic via the post-commit hook when a
  registered backend can resolve the matching retained transcript.
- **Non-committing work** (planning, a review that changed nothing, "just asking"): real tokens,
  no commit. `reconcile` sweeps retained/discoverable trailing turns into a `pending@<date>` row.
  The
  post-commit hook can't catch these (it only fires on commits), so run `reconcile` at session
  end — or wire it once with `install --claude`, whose **SessionEnd** hook runs `reconcile &&
  rollup` automatically when the session ends.
- **Cross-repo** (a session in repo A commits a fix into repo B): the git hook alone can't tell
  — the Claude CLI exposes no session id to a git hook, so it can only guess by directory. The
  **`--claude` PostToolUse hook** *can*: Claude hands it the exact session **and** the repo the
  commit landed in, so the cost is attributed to B correctly. Without it, bridge manually:
  `cd B && <rt> record --session <id> --commit <sha>` (and don't also sweep those turns in A — a
  given turn should be claimed by only one repo).
- **Submodules** are just the cross-repo case: a submodule is its own git repo, so when a commit
  into it is recorded, that row belongs to **its own** repository accounting, entirely separate
  from the parent. Install the tool in each submodule you
  want auto-tracked (each gets its own `post-commit` hook), **or** use `--claude` once at the
  parent — a single PostToolUse hook attributes every commit to whichever repo (parent or
  submodule) it landed in.

`--label` tags what the work was (e.g. `record --label implementation`, `reconcile --label
planning`). Every row carries an `activity`, and `rollup` breaks output tokens down
`by_activity`, so non-code work is counted *and* attributable.

## Reliability properties and their scope

These properties hold **for retained, discoverable observations and with the relevant wiring in
place**. They are deliberately narrower than a claim that the ledger is globally complete.

- **Streaming duplicates are removed within a transcript.** Agent transcript readers collapse
  repeated records for one billed message id before aggregation, so streaming copies are not
  summed repeatedly. The durable ledger does **not** store a global set of message ids.
- **Sequential recording in one repo advances a backend-scoped watermark.** `record` attributes
  only observations in `(allocation_floor, commit_ts]`; the next commit for the same
  `(backend, session_id)` continues after that floor. `reconcile` uses the same rule for retained
  trailing turns that did not produce a commit. Coincident textual session ids from different
  backends therefore do not suppress one another.
- **Repeated writes/publication of the same row identity are harmless.** Readers use stable row
  identities and latest-wins semantics, so local/published overlap and `merge=union` copies of the
  same row do not inflate totals. This is row deduplication, not global observation deduplication.
- **Sequential cross-repo allocation on one user/machine has a local double-count guard.** When the
  Claude PostToolUse hook records a session into repo B, the tool records a local claim through the
  last allocated observation. Claims are scoped by session plus a digest of the backend/transcript
  source, so unrelated transcripts that happen to reuse a textual session id do not share a floor.
  A later `record` or `reconcile` in repo A uses the matching claim as its floor. This specifically
  prevents a submodule commit followed by a parent gitlink-bump commit from charging the same turns
  twice when both hook events are seen on the same machine.
- **New work after a cross-repo claim is still allocatable.** The claim is only a timestamp floor;
  turns after it remain eligible for the next commit in whichever repository receives them.
- **The ledger tip trails the commit tip by one row, by design.** Recording commit *N* changes tally
  state after the commit exists, so tracked accounting for *N* can only land in a later commit. A
  commit tree cannot contain its own hash.

## Known accounting limitations

- **Coverage cannot be inferred from the ledger alone.** Hook downtime, unsupported runtimes,
  malformed/unsupported transcript records, pruned/deleted transcripts, pre-install history, and
  missed reconciliation can all produce undercount. Passive multi-backend recording attempts the
  remaining registered backends if one discovery/parser path fails, then reports the recording as
  incomplete. `doctor` reports backend discovery failures instead of treating them as healthy, but
  no current field proves historical completeness.
- **Repository policy and durable ledger corruption are not treated as zero.** Existing malformed
  or semantically invalid settings, malformed JSONL rows, and unknown compact schema versions stop
  the read/publish path so a smaller total cannot masquerade as healthy accounting. `doctor`
  reports these failures. This is integrity checking of readable structure, not proof that every
  original observation existed or was honestly produced.
- **Cross-repo claims are local best-effort state, not a global observation identity.**
  `~/.llm_resource_tally/claims.jsonl` is intentionally uncommitted. Deleting it, switching
  machines/users, manually recording the same work into multiple repos, or aggregating copied
  histories can double-count the same underlying work. Repository ledgers remain the durable
  source of truth; claims only coordinate sequential local allocation.
- **Two agents in the *same* repo at once (plain passive Git hook).** A Git post-commit hook does
  not receive a session id. Backend discovery can therefore choose the wrong in-flight session for
  a commit. Aggregate observed usage may still be captured, but the per-commit split can be wrong.
  Claude `install --claude` fixes this for Claude because PostToolUse names the exact session.
- **Resumed / forked sessions and backend session-id reuse.** If a new session re-embeds billed
  turns from an earlier session under a new session id, those inherited turns can be counted
  again. Pi is the exception: its per-observation claim ids are stable across verbatim fork/
  clone copies, so a forked or resumed prefix allocates exactly once machine-wide (in any repo,
  in any order, even if the original file later disappears). For the other backends, aggregate
  ledger rows do not preserve message ids or the transcript-source digest, and the repository
  watermark treats a backend `session_id` as its session identity. Supported backends
  are expected to issue unique session ids; actual reuse for unrelated transcripts in one repo can
  collide. The local cross-repo claim log is source-digested, but the v3 durable ledger is not.
- **`commit --amend`, rebase, squash, and other rewrites.** Superseded SHAs can remain in rows. A
  rewrite that drops ledger rows can also undercount if the corresponding transcript has already
  expired; `reconcile` can only reconstruct observations that still exist.
- **Non-committing work needs reconciliation.** If SessionEnd never fires and nobody later runs
  `reconcile`, retained-but-unallocated work is absent from the repository total until that sweep
  happens.
- **Compaction usage can be unmetered by the agent runtime.** When the transcript exposes only a
  compaction boundary and summary, tally stores those measured signals and leaves token/energy cost
  to later modeling rather than inventing a measured value.
- **Portfolio/fleet sums are gross repository-attributed totals.** They do not globally deduplicate
  the same observations across forks, cherry-picks, parent/submodule ledgers recorded on different
  machines, or intentional manual duplication.

## History rewrites (rebase, `filter-repo`, squash)

History rewrites do not intrinsically duplicate an already-watermarked transcript prefix, but they
can leave stale commit SHAs or remove ledger rows from reachable history. After a rewrite, run
`reconcile`: any still-retained transcript observations missing from the visible ledger can be
allocated again. If the source transcript has already been pruned, dropped ledger rows are not
recoverable from tally alone. Policy: never hand-delete rows; on a merge/rebase conflict keep both
sides (that's what `merge=union` is for), then inspect/reconcile rather than assuming completeness.

## Context compaction (measured signals, cost imputed later)

When `/compact` fires, the harness runs a real summarization call but writes **no `usage`
object** — only a `compact_boundary` marker and an `isCompactSummary` record. Rather than
fabricate a token count, `record`/`reconcile` add a `kind: compaction-estimate` row per boundary
holding only the **measured** signals — `peak_context_tokens` and `summary_chars` — keyed by
(session, boundary timestamp) so re-recording that same session boundary does not duplicate it. `rollup` reports these under
`compaction_signals`; conversions happen in the modeling pass. Disable with
`--no-estimate-compaction`. (The parser counts *any* record with a real `usage` object, so if a
future harness logs compaction usage it is measured automatically.)

**Backfill note:** past sessions are recoverable only as far back as the agent retains transcripts
(Claude Code defaults to **30 days**, `cleanupPeriodDays`). Set it high *now* if you want a deep
baseline later. See **[backfill](backfill.md)** for how to recover pre-install history and the
limits that bound it.
