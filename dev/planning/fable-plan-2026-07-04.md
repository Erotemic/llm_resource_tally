# llm_resource_tally — audit, shape, and roadmap (2026-07-04)

An audit of the repo at commit `4a4d039`, in three parts: **issues to fix**, **a better
shape** (what to keep, what to reorganize), and **a roadmap**. Written by Claude (Fable 5)
after reading every module, test, doc, and the dogfooded install.

The one-paragraph verdict: the core design is genuinely good — one dotdir, measurements-only
ledger, backend abstraction, stdlib-only, self-replicating installs are all the right calls
and are internally consistent. The three things holding it back are (1) a small family of
**double-count / lost-row correctness bugs** clustered around `reconcile` and cross-repo
attribution — the exact area the docs make correctness guarantees about, (2) the flagship
promise — **the energy/carbon/USD modeling pass** — does not exist yet, so the README's
central pitch is currently an IOU, and (3) day-to-day **legibility**: there is no way to see
a pretty report, check the wiring is healthy, or trust that the vendored dogfood copy matches
the source.

---

## Part 1 — Issues

Ordered by severity. File references are to the source package (the vendored
`.llm_resource_tally/tool/` copy mirrors it).

### P0 — correctness (the ledger can lose or double-count rows)

**1. Same-day re-reconcile silently drops earlier swept turns.**
`cmd_reconcile` buckets swept turns under `pending@YYYY-MM-DD`
([record.py:112](../../llm_resource_tally/record.py#L112)), and a measured row's dedup
identity is `("measured", session_id, commit)`
([ledger.py:63-69](../../llm_resource_tally/ledger.py#L63-L69)). Reconcile the same
still-active session twice in one UTC day — which the SessionEnd hook makes routine
(end session, resume, end again) — and you get two rows with the *same identity* holding
*disjoint* turn sets. `read_ledger` keeps only the latest (`recorded_at` wins), so the first
batch of turns vanishes from every rollup. This undercuts the "no undercount" guarantee in
[attribution.md](../../docs/attribution.md).
*Fix:* make the pending identity unique per sweep — include `turn_ts_range[1]` (or the full
range) in `_row_identity` for pending rows, or bucket as `pending@<full ISO timestamp>`.
Cheap, backward-compatible (old rows keep their identity), and testable.

**2. SessionEnd auto-reconcile double-counts cross-repo work.**
The `--claude` PostToolUse hook correctly attributes a commit made in repo B to B's ledger.
But the session's transcript lives under repo A's project dir, and A's SessionEnd hook then
runs `reconcile`, which sweeps **all** turns of that session into A's `pending@` bucket —
including the turns already claimed by B. Watermarks live per-repo-ledger
([ledger.py:128-136](../../llm_resource_tally/ledger.py#L128-L136)), so A cannot know B
claimed them. The docs tell the *human* not to double-sweep
([attribution.md](../../docs/attribution.md)), but `install --claude` automates the
double-sweep. The two headline features (cross-repo attribution + automatic session-end
sweep) are mutually unsafe today.
*Fix:* a tiny per-user **claims file** (e.g. `~/.llm_resource_tally/claims.jsonl`): every
`record` appends `(session_id, ts_hi, repo)`; `reconcile` skips turns at-or-below another
repo's claim for that session. Best-effort and advisory — the committed ledgers stay the
source of truth; the claims file is just a local double-count guard.

**3. Resumed/forked sessions likely re-count inherited turns.**
Message-id dedup happens only *within* one transcript parse
([backends/claude.py:92-122](../../llm_resource_tally/backends/claude.py#L92-L122)); the
ledger dedups by `(session, commit)`, never by message id across sessions. If Claude Code's
resume/fork writes prior turns (with their original usage objects) into a new session file
under a new session id — which historic versions did — `reconcile` sweeps the whole inherited
history again under the new sid. *Needs verification against current Claude Code behavior*,
then a defensive guard either way (e.g. skip turns whose `ts` predates the new session's
first user message, or a per-project-dir message-id high-water set).

### P1 — robustness and edge cases

**4. Codex non-strict discovery can grab an unrelated repo's session.**
Explicit `record --backend codex` with no repo-matching session falls back to the globally
most recent transcript ([backends/codex.py:116-118](../../llm_resource_tally/backends/codex.py#L116-L118))
— attributing another project's tokens to this repo's commit. The strict path already
refuses; the explicit path should at least warn loudly, or require `--transcript` when no
match is found rather than guessing.

**5. `is_git_commit` / `commit_repo_dir` miss common shapes.**
([backends/claude_hook.py:18-33](../../llm_resource_tally/backends/claude_hook.py#L18-L33))
- `git -C "dir with spaces" commit` — the quoted arg breaks the `-C\s+\S+` match, so the
  commit is missed entirely.
- `git -c k=v -C path commit` — flag order not covered by the regex.
- `cd /other/repo && git commit` — the payload `cwd` is the *pre-cd* directory, so the hook
  records against the wrong repo (usually a harmless no-op, but it silently defeats the
  cross-repo feature for a very common shell pattern). Parsing a leading `cd` target is an
  easy 80% fix.
These deserve a direct unit test table — today the regex has no dedicated tests.

**6. Concurrent sessions in one repo can cross streams.**
The passive post-commit path picks the most-recently-modified transcript in the project dir
([backends/claude.py:68-74](../../llm_resource_tally/backends/claude.py#L68-L74)). With two
agents active in the same repo, agent 1's commit can sweep agent 2's turns-so-far into agent
1's commit row. Totals stay correct (turns are disjoint per session), but per-commit
attribution is wrong. The `--claude` hook already fixes this (exact transcript in the
payload); worth documenting the passive-mode caveat, and worth having the post-commit hook
prefer the PostToolUse-provided attribution when both are wired (today the dup-guard makes
this mostly benign, but only if both picked the same transcript).

**7. Claude project-dir munging misses sessions started in a subdirectory.**
`find_transcript`/`session_transcripts` glob only the munged *repo-root* path
([backends/claude.py:88-90](../../llm_resource_tally/backends/claude.py#L88-L90)). A session
launched from `repo/subdir` lives under the munged subdir and is invisible to both strict
record and reconcile. Fix: also glob munged prefixes (any project dir whose decoded path is
inside the repo — the encoding is lossy, but prefix-matching the munged string of the root is
a safe overapproximation).

**8. Codex discovery reads every rollout file end-to-end.**
`_matches_repo` → `_session_meta` parses the *entire* transcript of every candidate, newest
first, on every passive record and reconcile
([backends/codex.py:51-84](../../llm_resource_tally/backends/codex.py#L51-L84)). With months
of sessions this makes every `git commit` pay a full scan of `~/.codex/sessions/**`. Fix:
break out of `_session_meta` once `session_meta`/first `turn_context` is seen (they are the
first records), and consider an mtime-based skip cache. Same idea applies to `reconcile`'s
repeated full parses of unchanged Claude transcripts — a `(path, mtime, size) → ts_hi` cache
would make session-end sweeps O(changed files).

**9. `commits_accounted` counts cross-repo rows ambiguously / amend churn.**
Minor and mostly documented, but: `git commit --amend` fires post-commit again and produces a
row for a SHA that immediately dies; rollup's `commits_accounted` then over/under-reports.
Consider detecting amends (`%cI` unchanged author date + same tree hint) or just documenting
the approximation directly in the rollup output.

### P2 — drift, hygiene, and packaging

**10. Doc drift: [backends.md](../../docs/backends.md) says "A fresh install registers
`claude` (the default)"** — stale since commit `e225135` made the default
`["claude", "codex"]` ([config.py:20](../../llm_resource_tally/config.py#L20)). The
`install --backend codex` example below it is likewise now a no-op ritual. Docs that
overstate the need for a step erode the "zero-thought" pitch.

**11. `requires-python >= 3.8` but CI tests 3.9–3.13** ([pyproject.toml:10](../../pyproject.toml#L10),
[test.yml:14](../../.github/workflows/test.yml#L14)). Either test 3.8 or bump the floor to
3.9. (3.8 is EOL; bump the floor.)

**12. No guard that the vendored dogfood copy matches the source.**
`.llm_resource_tally/tool/` is a manually-refreshed copy of `llm_resource_tally/`. Today they
match; nothing enforces it. A 5-line CI step (`diff -r` modulo `VERSION`/`.gitignore`/`hooks`)
turns silent drift into a red X.

**13. `lifetime-totals.json` is committed but nondeterministic.**
`generated_at` changes on every rollup, so every SessionEnd dirties the repo and two branches
always conflict on this file (no `merge=union` for JSON). Options: drop `generated_at` (or
derive it from the max `recorded_at` in the ledger, which is deterministic), or stop
committing totals and treat them as a build artifact. The ledger's own churn is the point;
the totals' churn is noise.

**14. The `Resource-Usage:` trailer is generated but goes nowhere.**
`trailer_line` ([record.py:136-142](../../llm_resource_tally/record.py#L136-L142)) prints a
suggestion to stdout — which the hook redirects to `/dev/null`. Either wire it (a
`prepare-commit-msg`/`commit --amend` opt-in is tricky post-commit; a `git notes` channel is
cleaner and rewrite-tolerant) or delete it. Half-features cost credibility.

**15. Windows is silently unsupported** (`fcntl`, bash hook scripts, POSIX classifier). Fine
as a stance — but say it in the README, and structure `append_row`'s locking behind a
`_lock()` helper so a future `msvcrt` shim is one function.

**16. No release scaffolding.** VERSION says 1.0.0 but there are no tags, no CHANGELOG, and
the curl route (and `update`) default to `main` — so every "install" is a moving target and
`RT_REF` pinning is advertised but has nothing stable to pin to. Tag `v1.0.0`, add a
CHANGELOG, and make README's curl line reference the latest tag.

### Test gaps (each maps to an issue above)

- Same-day double-reconcile keeps *both* turn sets (issue 1).
- Cross-repo `--claude` + SessionEnd reconcile does not double-count (issue 2).
- `is_git_commit` / `commit_repo_dir` truth table: quoted `-C`, flag order, `cd &&`,
  `--dry-run`, `commit` as an argument to something else (issue 5).
- Two concurrent sessions, one commit each — per-commit attribution (issue 6).
- Session started in a repo subdirectory is found by reconcile (issue 7).
- Ledger identity survives shard rotation (rotation test exists; add a duplicate-identity
  across-shards latest-wins case).

---

## Part 2 — Shape: what to keep, what to change

### What is already right (do not churn this)

- **One dotdir, data/code split by subdir.** The strongest design decision in the repo.
- **Measurements-only ledger, modeling post-hoc.** This is the moat versus every tool in
  [related-work.md](../../docs/related-work.md); it is what makes the ledger permanently
  trustworthy. Guard it zealously.
- **Backend interface** ([backends/base.py](../../llm_resource_tally/backends/base.py)) —
  small, honest, already proven by the second backend landing cleanly.
- **Stdlib-only, self-replicating vendored installs.** Unfashionable and correct for a tool
  that must run inside arbitrary repos for years.
- **Compact-on-disk / rich-in-memory codec isolated in one module** (schema.py). Textbook.

### Friction worth reorganizing

**1. `install.py` is four modules wearing a trenchcoat.** At 465 lines it is 3× the next
largest module and mixes vendoring, git-hook wiring, AGENTS.md block management, and Claude
settings.json surgery. Split it — flat, no subpackage needed:

```
llm_resource_tally/
  cli.py  record.py  rollup.py  ledger.py  schema.py  config.py  gitutil.py  _util.py
  install.py      # cmd_install/uninstall/update orchestration only (~100 lines)
  vendoring.py    # _vendor_into, module-location logic, update fetch
  wiring_git.py   # post-commit hook file + hooksPath/append modes
  wiring_agents.py# AGENTS.md managed block
  wiring_claude.py# .claude/settings.json hooks (+ future wiring_codex.py beside it)
  backends/
```

Each wiring module is one target (a file format it owns), independently testable, and the
pattern extends naturally when Codex/Gemini grow native hooks.

**2. Name the three layers the code already has.** The modules cleanly fall into
**measure** (backends, record, ledger, schema), **wire** (install + wiring_*), and
**report** (rollup, show, and everything in Part 3). Keeping the package flat is right at
this size — but the docs and `__init__` docstring should present this triad, because it is
the map new contributors need, and the roadmap adds modules to exactly one layer at a time.

**3. Separate computation from printing in `record.py`/`rollup.py`.** `cmd_*` functions
compute and `print` interleaved, which forces the tests to be subprocess string-greps and
forced `cmd_hook` into stdout-swapping gymnastics
([claude_hook.py:59-71](../../llm_resource_tally/backends/claude_hook.py#L59-L71)). Have the
work functions return summary dicts; let `cli.py` own formatting. This also gives `--json`
output on every command for free — which CI consumers and the future org aggregator will
want.

**4. Give the tool a speakable name.** The docs already invented `<rt>` because
`python3 .llm_resource_tally/tool` is unsayable. Ship a tiny committed wrapper at install
time (`.llm_resource_tally/rt` shell shim, one line) and/or a short console-script alias, and
let all docs use a real command instead of a placeholder. Small change, large legibility win.

**5. The claims file (from issue 2) is a shape decision, not just a bugfix.** It introduces
the one missing concept in the data model: *a turn is claimed by exactly one repo*. Today
that invariant is enforced only by per-repo watermarks (which can't see across repos) and a
docs admonition. Making the claim explicit — tiny, local, advisory, never committed — closes
the cross-repo family of bugs (issues 2, and partially 3/6) with one concept instead of three
patches.

---

## Part 3 — Roadmap

North star: **any repo can answer "what did this cost to build — tokens, hours, kWh, gCO₂e,
dollars — per commit, cumulatively, forever" from data it carries itself.** The ledger half
exists; the answer half doesn't yet.

### v1.1 — Trust (fix before anything new)

The correctness issues above, in order: reconcile identity collision (1), claims file (2),
resume-fork verification + guard (3), hook regex table (5), subdir munging (7), codex scan
cost (8). Plus the hygiene batch: doc drift (10), python floor (11), vendored-copy CI check
(12), deterministic totals (13), trailer decision (14), tag v1.0.0/v1.1.0 + CHANGELOG (16).
Everything here is small; the value is compounding — this tool's entire premise is that its
numbers can be trusted.

Add one new command: **`doctor`** — is the hook armed, is `core.hooksPath` sane, are Claude
hooks wired, do registered backends find transcripts for this repo, is `cleanupPeriodDays`
dangerously low (the retention horizon is the single most user-hostile silent failure —
today nobody is warned until the history is already unrecoverable), does the vendored VERSION
match the ledger schema. `install` should end by running it.

### v1.2 — Legibility (make the data pleasant)

- **`report`**: human-first views over the ledger — per-commit table, per-day/author/label
  rollups, `--format md|json|tsv`. `show` stays as the raw dump. This is where the committed
  ledger starts visibly beating ccusage-style ephemeral viewers: the report works on any
  clone, years later, no session logs required.
- **Richer rollup dimensions**: today only output tokens are broken down by
  model/activity/agent ([rollup.py:38-43](../../llm_resource_tally/rollup.py#L38-L43));
  break down all four token kinds, and add per-author (join `commit` → `git log` metadata at
  report time — derived, so it belongs in report, not the ledger).
- **Badge/summary artifact**: a small SVG or shields.io-compatible JSON endpoint file
  generated by rollup ("⚡ 66.5M tokens · 300 turns · 11 commits") for READMEs.

### v2.0 — The modeling pass (the promised half of the product)

A new `estimate` command and an `assumptions/` concept — the layer the README has promised
since day one:

- **Assumption packs, versioned like code**: a committed JSON file mapping model →
  (params/active-params estimate, J/token or tokens/s/GPU, PUE) plus a grid-intensity source
  keyed by the commit timestamps the ledger already stores. Packs are data, not code;
  publishing updated packs never touches ledgers.
- **`estimate` reads ledger + pack → derived report**: inference-seconds, kWh, gCO₂e, USD
  (pricing tables are just another pack). Every number is traceable to (measurement,
  assumption-pack-version) — printing that provenance is the honesty rule made visible.
  Compaction rows finally get costed here (chars→tokens imputation lives in the pack).
- This is also the differentiation moment: nothing in related-work does energy/carbon at
  all, and the "commit timestamp fixes the grid intensity" idea only works because the ledger
  is per-commit and committed. Lean into it.

### v2.x — Breadth

- **Backends**: Gemini CLI, opencode, aider, Copilot CLI — each is one file against
  `Backend`. Prioritize by what session logs actually exist on disk. Consider a generic
  OTel-ingest backend once agent CLIs converge on emitting usage telemetry.
- **Windows** (locking shim + PowerShell-compatible hook path).
- **GitHub Action**: on PR, verify ledger consistency (dedup invariants, schema versions),
  regenerate totals, and comment the PR's measured cost. This is the team-facing wedge — the
  ledger becomes visible in review, which is what makes teams keep it accurate.

### v3 — Ecosystem

- **`fleet`/org aggregator**: point at N repos (or a GitHub org), merge ledgers, one report.
  The committed-ledger design means this needs no server and no retention window — it reads
  clones.
- **Publish the ledger schema as a small spec** (it already has a version string and a
  documented compact codec) so other tools — including ccusage-style viewers — can read and
  write it. The long-game win is the format outliving the tool.

### Sequencing rationale

Trust before legibility before modeling before breadth: every later layer multiplies the
value of the ledger, so the ledger's correctness bugs are the highest-leverage work in the
repo; the modeling pass is the biggest promise outstanding, but shipping it on top of a
ledger that can double-count cross-repo work would poison exactly the numbers it exists to
make credible.

---

## Implementation status — updated 2026-07-04 (Opus)

A first pass through this plan landed in the same day. Summary of what shipped and what was
consciously deferred.

**Done (v1.1 Trust):**
- Issue 1 — pending-row identity now includes the swept-window end (`ledger.py`); two same-day
  sweeps no longer collide. Test: `test_pending_rows_do_not_collide`.
- Issue 2 — the claims file shipped as `claims.py`; `record` writes claims, `reconcile` honors
  another repo's ceiling. Test: `test_cross_repo_reconcile_does_not_double_count`.
- Issue 4 — Codex non-strict fallback now warns loudly.
- Issue 5 — hook parsing handles `cd &&`, quoted `-C`, and `-c k=v` order. Test:
  `test_git_commit_command_parsing`.
- Issue 7 — Claude discovery scans verified munged subdir project dirs. Test:
  `test_subdir_session_is_discovered`.
- Issue 8 — Codex `_session_meta` breaks out of the scan after the opening records.
- Issues 10, 11, 13, 14, 15 — doc drift fixed; python floor → 3.9; `rollup` deterministic
  (`through` replaces `generated_at`); dead trailer removed; locking behind `_lock`/`_unlock`.
- Issue 16 — `CHANGELOG.md` added. (Actually cutting/pushing a `v1.1.0` tag is left to the
  maintainer — an agent shouldn't mint release refs.)
- `doctor` command added and run at the end of `install`; warns on low Claude retention.

**Done (v1.2 Legibility):** `report` (`--by`, `--format`); all four token kinds broken down
`by_model`/`by_activity`/`by_agent` in `rollup`; `--json` on `report`/`estimate`.

**Done (v2.0 Modeling, MVP):** `estimate` + a versioned assumption pack
(`assumptions/default-pack.json`, shipped, illustrative). Energy/carbon/USD derived per model,
nothing written back to the ledger, provenance printed. Docs in `docs/modeling.md`.

**Deferred (documented, not yet coded):**
- Issue 3 (resume/fork re-count) — needs verification against current Claude Code behavior
  before a guard is safe to write; captured under "Known limitations" in `attribution.md`.
- Issue 6 (two agents, one repo, passive hook) — documented as a known limitation; `--claude`
  already resolves it.
- Issue 9 (`--amend` churn in `commits_accounted`) — documented as an approximation.

### Second pass (Opus) — later the same day

- **`install.py` split shipped** (Part 2 shape item 1): `vendoring`, `wiring_git`,
  `wiring_agents`, `wiring_claude`, `wiring_common`, leaving `install.py` as thin orchestration.
  Behavior-preserving, guarded by the existing install/hook/claude tests.
- **Per-commit-timestamped grid intensity shipped** (v2.0 refinement — the differentiator):
  `estimate` now computes per row and a pack can pin `grid.intensity_by_date`, so each commit's
  carbon uses the grid at its own timestamp. Test: `test_estimate_time_keyed_grid`.
- **Badge artifact shipped** (v1.2): `rollup` writes a deterministic shields.io endpoint
  `badge.json`. Test: `test_rollup_writes_badge`.
- Python floor raised to **3.10**.

**Still future milestones (as written above):** GitHub Action, org aggregator, publishing the
schema as a spec, more backends, Windows.
