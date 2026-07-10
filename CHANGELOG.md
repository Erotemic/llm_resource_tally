# Changelog

All notable changes to `llm_resource_tally`. Versions follow the `VERSION` file; the ledger
schema version is tracked separately in `schema.py` (currently `v3`).

## [Unreleased]

Implements the v1.1 "Trust" and parts of the v1.2/v2.0 milestones from
`dev/planning/fable-plan-2026-07-04.md`.

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
- **Installation policy is portable and canonical.** `.llm_resource_tally/settings.json` now
  records storage mode, tool format/path, modeling inclusion, and backends. `install` and `update`
  use it as their default and explicit flags replace it. The bootstrap reads the same policy, so
  ignored-mode clones recreate the intended local tool without machine-local git config.
- **`update` can migrate representation and storage.** It accepts `--tool-format`, `--dir`,
  `--storage`, `--modeling`, and `--no-modeling`, rewires hooks, removes obsolete managed
  artifacts after successful replacement, and stages previously tracked generated files out of
  the index when entering ignored mode while retaining `settings.json`.
- **`rollup` output is deterministic.** `generated_at` (wall-clock) is replaced by `through`
  (the latest `recorded_at` in the ledger), so the same ledger always yields byte-identical
  totals — no spurious diffs or merge conflicts on `lifetime-totals.json`.
- **Richer rollup breakdowns.** All four token kinds are now broken down `by_model`,
  `by_activity`, and `by_agent` (previously only output tokens).
- File locking is isolated behind `_lock`/`_unlock` helpers and degrades gracefully where
  `fcntl` is unavailable (a step toward Windows support).
- `requires-python` raised to `>=3.10` to match the CI matrix (3.9 is near EOL).

### Added
- **Deterministic zipapp deployment.** Fresh pip and `curl | sh` installs now default to a
  single `.llm_resource_tally/tool.pyz`; `install --tool-format zipapp|source` retains the
  source-tree format for development and debugging. The
  archive embeds version/build metadata, is executable, copies itself atomically, and loads
  bundled assumption data through `importlib.resources`. `build-zipapp` creates minimal or
  modeling-inclusive artifacts with reproducible member ordering and timestamps.
- **`report`** — human-readable views over the committed ledger (`--by
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
- **badge** — `rollup` also writes `.llm_resource_tally/badge.json`, a shields.io endpoint
  object (deterministic) so a repo's cumulative footprint can be shown as a README badge.
- **opencode backend** — reads the opencode SQLite store (`~/.local/share/opencode/opencode.db`,
  or `$OPENCODE_DATA_DIR`) via stdlib `sqlite3`, read-only, mapping its `tokens
  {input, output, reasoning, cache}` into the ledger schema. Opt in with `install --backend
  opencode`. (Verified against real opencode data.)
- **`fleet`** — aggregate many repos' committed ledgers into one report (`fleet <dirs/repos>`,
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

### Removed
- The dead `Resource-Usage:` commit-trailer suggestion (it was printed to a stream the hook
  discarded). The ledger already captures everything it carried.
