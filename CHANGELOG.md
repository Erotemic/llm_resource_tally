# Changelog

All notable changes to `llm_resource_tally`. Versions follow the `VERSION` file; the ledger
schema version is tracked separately in `schema.py` (currently `v3`).

## [Unreleased]

Implements the v1.1 "Trust" and parts of the v1.2/v2.0 milestones from
`dev/planning/fable-plan-2026-07-04.md`.

### Fixed (correctness)
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
- **`rollup` output is deterministic.** `generated_at` (wall-clock) is replaced by `through`
  (the latest `recorded_at` in the ledger), so the same ledger always yields byte-identical
  totals — no spurious diffs or merge conflicts on `lifetime-totals.json`.
- **Richer rollup breakdowns.** All four token kinds are now broken down `by_model`,
  `by_activity`, and `by_agent` (previously only output tokens).
- File locking is isolated behind `_lock`/`_unlock` helpers and degrades gracefully where
  `fcntl` is unavailable (a step toward Windows support).
- `requires-python` raised to `>=3.10` to match the CI matrix (3.9 is near EOL).

### Added
- **`report`** — human-readable views over the committed ledger (`--by
  commit|day|activity|agent|model`, `--format table|md|tsv|json`).
- **`estimate`** — the modeling pass: derives energy (kWh), carbon (gCO₂e), and cost (USD)
  from the ledger's measured tokens times a versioned, editable **assumption pack** (ships an
  illustrative default). Computed **per row**, so a pack can pin grid carbon intensity over
  time (`grid.intensity_by_date`) and each commit's carbon reflects the grid at its own
  timestamp. Nothing is written back to the ledger.
- **`doctor`** — checks hook wiring, Claude native hooks, registered backends, ledger health,
  and warns when Claude's transcript retention (`cleanupPeriodDays`) is too low to backfill
  later. `install` now runs it at the end.
- **badge** — `rollup` also writes `.llm_resource_tally/badge.json`, a shields.io endpoint
  object (deterministic) so a repo's cumulative footprint can be shown as a README badge.

### Internal
- `install.py` split into focused modules — `vendoring`, `wiring_git`, `wiring_agents`,
  `wiring_claude`, `wiring_common` — leaving `install.py` as thin orchestration. No behavior
  change (guarded by the existing install/hook/claude tests).

### Removed
- The dead `Resource-Usage:` commit-trailer suggestion (it was printed to a stream the hook
  discarded). The ledger already captures everything it carried.
