# llm_resource_tally — measured LLM resource accounting (per commit)

A small, **self-contained** tool (Python **stdlib only** — zero dependencies) that records
the compute each commit cost an LLM agent, so a repo's lifetime *resource utilization* can
be estimated. Token counts and model are **measured** verbatim from the agent's own session
transcript; energy and carbon are derived later from the recorded tokens/time and the commit
timestamp (which fixes the grid's carbon intensity at that moment).

Everything the tool owns lives under **one** directory, **`.llm_resource_tally/`** in your repo
root, committed alongside your code: the ledger data in `ledger/`, and the vendored tool code in
`tool/`. This holds regardless of how the tool is installed.

## Install

Pick whichever route fits how you like to manage tooling. **Routes A and B end up identical** —
a self-contained copy vendored into `.llm_resource_tally/tool/` (committed, works offline, the
source of truth) plus wiring (a git `post-commit` hook + a managed `AGENTS.md` block). They
differ only in how the code is first *delivered*: PyPI or a curl'd tarball. Route C keeps the
tool as a live submodule instead. After any route, re-run `install` after cloning to wire a
fresh checkout.

**A. pip (PyPI is just the delivery — it vendors a copy in, same as curl):**
```bash
pip install llm_resource_tally
cd /your/repo && llm_resource_tally install     # copies the minimal tool into .llm_resource_tally/tool/ and wires it
```
After this the repo is self-contained; the pip package isn't needed again (a fresh clone re-wires
with `python3 .llm_resource_tally/tool install`, no pip). Pass `--dir tools/rt`
to vendor elsewhere.

**B. curl → sh (no PyPI needed):**
```bash
curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
```
Vendors into `.llm_resource_tally/tool/` (code only — never the ledger) and wires up offline.
Override with env vars: `RT_DIR=tools/rt` (where to vendor), `RT_REF=v1.2.3` (pin a
tag/branch/sha), `RT_REPO=owner/name` (source).

**C. Git submodule** (track upstream by ref instead of vendoring a copy):
```bash
git submodule add https://github.com/Erotemic/llm_resource_tally .llm_resource_tally/tool
python3 .llm_resource_tally/tool install
```

Throughout this README, `<rt>` is the vendored invocation every route leaves you with:
`python3 .llm_resource_tally/tool` (the pip console script `llm_resource_tally`
is only used once, to bootstrap route A).

**Claude Code users — turn on precise cross-repo attribution** (recommended, see *Attribution*):
```bash
<rt> install --claude          # also adds a PostToolUse hook to .claude/settings.json
```

**Re-wire / update / uninstall:**
```bash
<rt> install                                   # idempotent, offline; safe to re-run
<rt> update          # re-vendor latest from GitHub  (pip origin: pip install -U && llm_resource_tally install)
<rt> uninstall                                 # remove wiring; keeps .llm_resource_tally/
```
Hooks are wired safely: the tool shares via `core.hooksPath` only when that won't shadow an
existing hook, otherwise it appends a sentinel-guarded block to your active `post-commit`
(husky/lefthook/`.git/hooks` are never clobbered).

## Usage

```bash
<rt> record                    # attribute new turns -> HEAD  (usually automatic via the hook)
<rt> reconcile --label review  # sweep turns that produced no commit (planning, chat, review)
<rt> rollup                    # refresh lifetime totals
<rt> show                      # print the ledger
```
With the hook installed you normally only run, **at session end**, `<rt> reconcile && <rt> rollup`.
**Codex / non-Claude agents:** `<rt> record --transcript <path/to/session.jsonl>`.

**Tag what the work was** with `--label` (e.g. `record --label implementation`, or
`reconcile --label planning`). Every row carries an `activity`, and `rollup` breaks output
tokens down `by_activity`, so non-code work is counted *and* attributable.

## How cost is attributed

The atomic unit is a **turn** (one API call, identified by `message.id`); each turn belongs to
one **session** (one transcript). The rule:

> A turn is attributed to the repo of the **next commit** it feeds. If it never feeds a commit,
> `reconcile` attributes it to the repo where the **session runs**.

- **Normal work** (session and commit in the same repo): fully automatic via the post-commit hook.
- **Non-committing work** (planning, a review that changed nothing, "just asking"): real tokens,
  no commit. `reconcile` sweeps them into a `pending@<date>` row so nothing is lost — **run it at
  session end** (the hook only fires on commits).
- **Cross-repo** (a session in repo A commits a fix into repo B): the git hook alone can't tell —
  the Claude CLI exposes no session id to a git hook, so it can only guess by directory. The
  **`--claude` PostToolUse hook** *can*: Claude hands it the exact session **and** the repo the
  commit landed in, so the cost is attributed to B correctly. Without it, bridge manually:
  `cd B && <rt> record --session <id> --commit <sha>` (and don't also sweep those turns in A —
  a given turn should be claimed by only one repo).
- **Submodules** are just the cross-repo case: a submodule is its own git repo, so a commit into
  it is tracked in **its own** `.llm_resource_tally/`, entirely separate from the parent — the
  ledger always follows the repo the commit lands in. Install the tool in each submodule you want
  auto-tracked (each gets its own `post-commit` hook), **or** use `--claude` once at the parent —
  a single PostToolUse hook attributes every commit to whichever repo (parent or submodule) it
  landed in.

## Where data lives, and the design principle

Everything the tool owns is under a single `.llm_resource_tally/` at your repo root, split into
**data** and **code** by subdirectory:

- **data** (precious): `ledger/` (append-only JSONL shards — the source of truth),
  `lifetime-totals.json` (readable, generated by `rollup`), and a `.gitattributes` marking the
  shards `merge=union`.
- **code** (disposable): `tool/` — the vendored package. `install`/`update` rewrite it; nothing
  here is irreplaceable. (`uninstall` intentionally leaves both in place.)

The ledger **rolls**: the active `ledger/ledger.jsonl` is rotated to a timestamped archive once it
passes ~1 MB (`LLM_RESOURCE_TALLY_MAX_LEDGER_BYTES`), so no single file grows without bound;
readers glob all shards. Rows are stored in a **compact** schema (terse keys + positional token
arrays, no whitespace) documented in [`schema.py`](llm_resource_tally/schema.py);
`lifetime-totals.json` keeps full readable keys.

**Why co-located under one dotdir?** So a host repo gains exactly **one** top-level entry, not two.
The data/code split is by subdir, and the sanctioned way to remove the tool is `uninstall` (which
never touches `ledger/`) — so `tool/` being disposable never endangers your data.

**Why the repo root, not next to where pip put the tool?** The ledger is a property of the *repo*,
not of the tool's install. Anchoring `.llm_resource_tally/` at the repo root is the one rule that
works for every route — pip (the package is in site-packages, nowhere near your repo), vendor, and
submodule — and it makes each submodule track *itself* (see *Attribution*). Your data location
stays put even if you switch how the tool is installed, and the tool's own copies never travel
with a foreign repo's ledger.

**The ledger stores MEASUREMENTS only.** Nothing that requires an assumption is baked in:

- **Measured & stored:** model, input/cache-write/cache-read/output tokens, server-tool calls,
  wall-clock span, turn timestamps. For context-compaction (which the harness doesn't bill), the
  two signals a later pass needs: peak preceding context and summary length in chars.
- **NOT stored (modeled post-hoc):** inference-seconds (throughput assumption), compaction token
  cost (chars→tokens), energy, carbon. Unobserved values are `null`, never an imputed default.

Because only raw observations are kept, any modeling decision can change later without
re-recording. `rollup` is itself such a regenerable post-hoc pass.

## Correctness guarantees

- **No double-count under concurrency.** Usage is attributed per session (each agent = its own
  transcript = disjoint turns); rows are keyed `(session_id, commit)`, appended under an `flock`.
- **No undercount.** `record` sweeps a session's turns in `(watermark, commit_ts]`; the next commit
  continues from that watermark. `reconcile` catches turns that never produced a commit.
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

**Backfill note:** past sessions are recoverable only as far back as Claude Code retains
transcripts — default **30 days** (`cleanupPeriodDays`). Set it high *now* if you want a deep
baseline later; older-than-that must be estimated (a labeled modeling layer), never fabricated.

## Self-replicating installs

**An install is self-sufficient.** Everything needed to (re)install is committed into your repo,
so `install`/`record` work with **zero network**. Hosting is only a convenience for the first
fetch and for `update`. A vendored copy (`tool/` only — never the ledger) can even seed another repo offline:
```bash
mkdir -p /other/repo/.llm_resource_tally && cp -r .llm_resource_tally/tool /other/repo/.llm_resource_tally/ \
  && (cd /other/repo && python3 .llm_resource_tally/tool install)
```

**This repo is the source, not an install.** The tool's code lives at the repo *root* (as the
`llm_resource_tally/` package); that root is what pip packages, what `curl | sh` vendors, and what
the submodule route clones. To dogfood, the tool is *installed into its own repo* under
`.llm_resource_tally/tool/` — a normal vendored install that tracks this repo's own development and
is itself self-replicating. The repo replicates *out*; the install *within* it replicates like any
other.

The `AGENTS.md` block is managed automatically by `install` — do not paste it by hand.

## Backends

Everything agent-specific — where transcripts live, how tokens are parsed, whether the agent
has a compaction concept — is isolated behind a `Backend` ([`backends/`](llm_resource_tally/backends/)).
`claude` (Claude Code) is the default and only backend today; the core (record/reconcile/rollup,
the ledger, git wiring) is backend-agnostic. Each row records its `agent`, so a repo can mix
backends. Adding Codex or another agent is a new `Backend` implementing
[`backends/base.py`](llm_resource_tally/backends/base.py) — nothing else changes. Select one with
`--backend <name>`.

## Development

The tool is a stdlib-only package (no runtime deps):

```
llm_resource_tally/
  cli.py        argument parsing / dispatch        ledger.py    rolling shards, read/dedup/append
  record.py     record / reconcile                 schema.py    compact on-disk row codec
  rollup.py     rollup / show                       gitutil.py   git helpers (repo_root anchors data)
  install.py    install/vendor + hook & AGENTS      backends/    agent-specific transcript readers
```

Tests are `pytest tests/` (they spin up throwaway git repos and exercise the CLI end-to-end,
including a real venv `pip install`); CI runs them across Python 3.9–3.13
([.github/workflows/test.yml](.github/workflows/test.yml)).

## Related work

The Claude-Code usage-tracking space is real and worth knowing before you adopt this. Most
existing tools are **ephemeral viewers**; this one is a **committed, per-commit ledger**. The
distinction that motivated building it is the *combination* of three things no other tool does
together: a version-controlled ledger **committed inside each repo**, **token/energy/carbon**
accounting (not dollars), and a **lifetime-cumulative** framing over a repo's whole history.

- **[ccusage](https://github.com/ccusage/ccusage)** (~17k★) — a read-only CLI that parses the
  local JSONL logs Claude Code (and 11+ other agent CLIs) already write and prints token/USD
  reports grouped by session, day/week/month, or project. It **persists nothing** and never
  touches git — an ad-hoc analytics viewer, not durable accounting. Reach for it when you just
  want to *see* recent spend; reach for this when you want a versioned record that travels with
  the code.
- **[claude-budget](https://github.com/mooracle/claude-budget)** — a close analog: it also
  attributes Claude usage to individual **git commits**, via a hook. It writes a **USD cost
  trailer into the commit message** (e.g. `Claude-Cost: 0.42`), Claude-only. This tool instead
  writes a structured **token** ledger to a committed file (dollars/energy/carbon are derived
  post-hoc from the measured tokens + commit timestamp), keeps raw measurements only, and is
  built to grow more backends than Claude.
- **[llm-usage-metrics](https://github.com/ayagmar/llm-usage-metrics)** — the closest in spirit:
  a multi-tool CLI that *correlates* spend with git, reporting **$/commit** and **$/1k lines**
  and even replaying your token mix against alternative models. But it **computes those ratios
  on demand** from one machine's local session logs — it persists no per-commit record into the
  repo. This tool inverts that: it writes the durable per-commit record *first* (committed to
  version control, so it travels with the repo and survives the machine), and leaves ratios and
  pricing to a regenerable modeling pass over that record.
- **[Claude Code Analytics](https://code.claude.com/docs/en/analytics)** (official) — Anthropic's
  cloud dashboards for org admins: usage, accept-rate, spend, and a GitHub integration that labels
  *merged PRs* `claude-code-assisted`. Hosted in Anthropic's cloud, Claude-only, **PR granularity,
  nothing persisted into the repo**. Complementary at a different altitude: org-wide rollups you
  don't own vs. a per-commit record the repo carries itself.
- **Live monitors** — [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor),
  `claude-usage` dashboards, and Claude Code's own `/usage`, `/cost`, `/context` — real-time
  burn-rate and limit warnings. Complementary: those answer "am I about to hit a cap *now*?";
  this answers "what did this repo cost to build, cumulatively?"
- **Attribution trailers** (`Co-authored-by:` model lines, `llm-git`) record *which model*
  authored a commit, not what it *consumed* — orthogonal, and easily used alongside this.

## License

Apache-2.0. See [LICENSE](LICENSE).
