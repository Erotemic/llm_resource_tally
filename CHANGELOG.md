# Changelog

All notable changes to `llm_resource_tally`. Versions follow the `VERSION` file; the ledger
schema version is tracked separately in `schema.py` (currently `v3`).

## [0.3.0] - 2026-07-30

First tagged release.

Implements the v1.1 "Trust" and parts of the v1.2/v2.0 milestones from
`dev/planning/fable-plan-2026-07-04.md`.

### Fixed (safety)
- **The bootstrap now has a non-mutating help path.** `install.sh -h` and
  `install.sh --help` print usage and exit before checking dependencies, locating a repository,
  downloading code, or changing files. Unknown arguments fail before installation and point to
  `--help`.
- **Installing never silently moves a repository's accounting out of Git.** A `settings.json`
  written before the installation-policy block carries no explicit storage mode, so it would
  have fallen through to the new `local` default: the repository's tracked ledger would freeze,
  new rows would go to an ignored spool, and nothing would say so. `install` now infers
  `committed` when the worktree already carries tracked ledger or rollup files, and keeps
  `local` only for repositories with no such evidence. Tracked `lifetime-totals.json` is
  likewise left tracked rather than moved into ignored state. Ledger shards
  were never at risk — no code path rewrites or untracks them.
- **`doctor` no longer reports a healthy hook it never inspected.** Any `core.hooksPath` ending
  in `hooks` was reported as armed without checking for the managed block, so a repository with
  a custom hook path and no tally wiring passed. The managed block is now required in every case.

### Fixed (correctness)
- **Subagent usage is now counted.** Claude Code stores Task/sidechain subagent sessions under
  `<project>/<session-id>/subagents/agent-*.jsonl` — real billed API calls (often a different
  model, e.g. a haiku subagent) that the tool previously ignored entirely, undercounting every
  session that spawned subagents. Their turns now fold into the parent session (deduped by
  message id).
- **Pending rows no longer collide.** Two `reconcile` sweeps of one session on the same UTC
  day produced two `pending@<date>` rows with the same identity; latest-wins silently dropped
  the earlier turns. Pending-row identity now includes the swept window's end, so both survive.
- **Cross-repo work is no longer double-counted.** A session that commits into another repo is
  recorded there by the `--claude` PostToolUse hook *and* was swept again by the origin repo's
  SessionEnd `reconcile`. A local, per-user claims log (`~/.llm_resource_tally/claims.jsonl`,
  never committed) lets `reconcile` skip turns another repo already claimed.
- **`git commit` detection hardened.** The PostToolUse hook now recognizes `cd <dir> && git
  commit`, quoted `-C "path with spaces"`, and `-c k=v` before `-C` — previously missed.
- **Sessions started in a subdirectory are found.** Claude transcript discovery also scans
  munged sub-directory project dirs, verified by the transcript's recorded `cwd`.
- **Codex discovery is safer and cheaper.** The non-strict fallback to an unrelated session now
  warns loudly; session metadata is read from the opening records instead of scanning every
  token-count event in every session on every commit.

### Changed
- **Git hooks stay in Git-local storage.** Fresh installs append the managed post-commit block under `.git/hooks` (or an existing custom `core.hooksPath`), and migrate the former tally-owned `.llm_resource_tally/hooks` directory out of the worktree.
- **Local spool is now the default storage mode.** Automatic hooks write only beneath ignored
  `.llm_resource_tally/local/`, so normal Git operations stay free of tally-generated tracked
  changes without adding automatic notes synchronization. `publish` appends the spooled rows to
  the tracked append-only `.llm_resource_tally/ledger/ledger.jsonl`, which rotates to
  `ledger.<UTCstamp>.jsonl` past the size limit like the local spool — publication happens often
  enough that a file per call would bury the directory. Rows already present in the tracked
  shards are skipped, so republishing is a no-op and an interrupted publish cannot double-write;
  a row with a newer `recorded_at` is still appended, preserving latest-wins. Readers union and
  de-duplicate local, published, legacy file, and git-notes rows.
- **`publish` also refreshes the tracked rollup in every storage mode.**
  `lifetime-totals.json` stays committed and is recomputed deterministically from the whole
  ledger, so a fresh clone can read totals without holding anyone's local spool. `notes` mode is
  an alternative store for *rows*, not an opt-out of publishing the aggregate.
- **Publication runs at session end as well.** The Claude `SessionEnd` hook now runs
  `reconcile`, `rollup`, then `publish`. It is a backstop rather than the primary path, since a
  session can end abruptly enough that the hook never fires; the managed `AGENTS.md` block tells
  agents to publish after substantial work for the same reason. Both routes are idempotent.
- **Switching storage modes is lossless in both directions.** `install` drains any pending local
  rows before leaving `local` mode, which would otherwise strand them where only that one machine
  could read them — into the tracked ledger, or into the notes ref when switching to `notes`, so
  rows always follow the destination mode's row store. The reverse direction needs no migration:
  tracked shards keep being read while new rows spool alongside them.
- **Installation policy is portable and canonical.** `.llm_resource_tally/settings.json` now
  records storage mode, tool format/path, modeling inclusion, and backends. `install` and `update`
  use it as their default and explicit flags replace it. The bootstrap reads the same policy, so
  ignored-mode clones recreate the intended local tool without machine-local git config.
- **The installed invocation is format-invariant.** Both source and zipapp installs use
  `.llm_resource_tally/tool`, so `python3 .llm_resource_tally/tool` works in either mode.
  `install` and `update` can switch formats by staging and validating the replacement, swapping
  the file or directory at that path, deleting the previous representation, and retaining the
  same hook and agent commands. They also migrate storage and modeling policy.
- **`rollup` output is deterministic.** `generated_at` (wall-clock) is replaced by `through`
  (the latest `recorded_at` in the ledger), so the same ledger always yields byte-identical
  totals — no spurious diffs or merge conflicts on `lifetime-totals.json`.
- **Richer rollup breakdowns.** All four token kinds are now broken down `by_model`,
  `by_activity`, and `by_agent` (previously only output tokens).
- File locking is isolated behind `_lock`/`_unlock` helpers and degrades gracefully where
  `fcntl` is unavailable (a step toward Windows support).
- `requires-python` raised to `>=3.10` to match the CI matrix (3.9 is near EOL).

### Added
- **`publish`** — appends the ignored local spool to the tracked ledger and refreshes the tracked
  rollup. It stages nothing and commits nothing; the result is an ordinary reviewable
  change.
- **Agent-facing guidance.** `--help` now states what the tool records, that recording is
  automatic, when to publish, and where to start when accounting looks broken. The managed
  `AGENTS.md` block was rewritten from prose into operating rules covering the same ground.
- **Deterministic zipapp deployment.** Fresh pip and `curl | sh` installs now default to a
  single zipapp file at `.llm_resource_tally/tool`; `install --tool-format
  zipapp|source` changes whether that same path is a file or source directory. The archive embeds version/build metadata, is executable, copies itself atomically, and loads
  bundled assumption data through `importlib.resources`. `build-zipapp` creates minimal or
  modeling-inclusive artifacts with reproducible member ordering and timestamps.
- **`report`** — human-readable views over the locally visible deduplicated ledger (`--by
  commit|day|activity|agent|model`, `--format table|md|tsv|json`).
- **Modeling is a separate, opt-in package.** The bare `curl | sh` install now vendors only the
  measurement **core**; the modeling layer (`estimate`) lives in `llm_resource_tally.modeling`
  and is deliberately left out so the offline footprint stays tiny. Add it with `install
  --modeling` (copies the subpackage from a pip/full install offline, else fetches just that
  subdir from the release tarball), `RT_MODELING=1` at curl time, or `pip install
  llm_resource_tally` (which includes it). When it's absent, `estimate` prints a one-line
  install hint instead of an ImportError; `record`/`report`/`fleet`/`doctor` never depend on it.
- **`estimate`** — the modeling pass: derives energy (kWh), carbon (gCO₂e), and cost (USD)
  from the ledger's measured tokens times a versioned, editable **assumption pack**. Computed
  **per row**, so a pack can pin grid carbon intensity over time (`grid.intensity_by_date`) and
  each commit's carbon reflects the grid at its own timestamp. Nothing is written back to the
  ledger.
  - **Sources & adapters.** Where a pack comes from is a source (`{"adapter", "ref"}`); an
    adapter turns a `ref` into a pack. The vendored default loads through the *same* mechanism
    (a `json-file` source), so adding a new source is just `register_adapter(...)` + a ref — no
    estimator change. Two adapters ship: `json-file` and `codecarbon-energy-mix`.
  - **Provenance protocol.** Every pack carries a `provenance` list (per `applies_to`: grid /
    energy / pricing / pue) with `source`/`citation`/`license`/`retrieved`/`note`; `estimate`
    prints it and includes it in JSON, so every figure is traceable to its origin.
  - The default pack is now a **cited baseline** (not placeholders): grid from CodeCarbon's
    world-average intensity (MIT), per-token energy from published inference studies — with the
    honest caveat that codecarbon backs the grid, *not* the per-token energy. Pricing stays a
    labeled list-price placeholder.
- **Per-region grid (CodeCarbon).** A shipped `grid-codecarbon.json` pack carries per-country
  carbon intensity (213 countries) from CodeCarbon's global energy mix (MIT); `estimate
  --region <ISO3>` (e.g. `FRA`, `USA`, `NOR`) fixes each row's grid to that country — same
  energy, region-accurate carbon. The pack is frozen from the `codecarbon-energy-mix` adapter by
  `dev/build_grid_pack.py` (pinned to a CodeCarbon release), so the shipped data can never drift
  from the adapter that produces it.
- **`doctor`** — checks hook wiring, Claude native hooks, registered backends, ledger health,
  and warns when Claude's transcript retention (`cleanupPeriodDays`) is too low to backfill
  later. `install` now runs it at the end.
- **opencode backend** — reads the opencode SQLite store (`~/.local/share/opencode/opencode.db`,
  or `$OPENCODE_DATA_DIR`) via stdlib `sqlite3`, read-only, mapping its `tokens
  {input, output, reasoning, cache}` into the ledger schema. Opt in with `install --backend
  opencode`. (Verified against real opencode data.)
- **`fleet`** — aggregate many repos' locally visible ledgers into one report (`fleet <dirs/repos>`,
  `--format table|md|tsv|json`); the org-wide view needs no server and no retention window.
- **`report --commits <range>`** — scope a report to a git range (e.g. `main..HEAD`), i.e. the
  measured cost of a branch or PR.
- **`PR LLM cost` GitHub Action** (`.github/workflows/pr-ledger.yml`) — comments each PR with
  the measured cost of the commits it adds, using `report --commits`.

### Internal
- `install.py` split into focused modules — `vendoring`, `wiring_git`, `wiring_agents`,
  `wiring_claude`, `wiring_common` — leaving `install.py` as thin orchestration. No behavior
  change (guarded by the existing install/hook/claude tests).
- `estimate.py` + `assumptions/` moved from `llm_resource_tally/` to `llm_resource_tally/modeling/`.
  Reach the API as `from llm_resource_tally.modeling import estimate, load_pack` (the top-level
  package no longer re-exports `load_pack`, since it must import without modeling present). A new
  `modeling_bridge` module is the core↔modeling seam. Pre-1.0, so no deprecation shim.
- `ruff format` adopted repo-wide, configured in `pyproject.toml` at the 110-column width the
  code was already written to, excluding the generated `.llm_resource_tally` artifact.
- Dead hook-wiring code removed (`vendoring.shared_hooks_rel`,
  `wiring_git._has_active_git_hooks`), unreferenced since hooks moved back to Git-local storage.

### Removed
- The dead `Resource-Usage:` commit-trailer suggestion (it was printed to a stream the hook
  discarded). The ledger already captures everything it carried.
- `--hook-mode hookspath` and `--hook-mode append`. Once hooks moved back to Git-local storage
  neither differed from `auto`, and a flag that silently does nothing is worse than one that is
  gone. `--hook-mode` now takes `auto` or `none`.
