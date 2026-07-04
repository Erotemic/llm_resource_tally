# llm_resource_tally — measured LLM resource accounting (per commit)

A small, **self-contained** tool (Python **stdlib only** — zero dependencies) that records
the compute each commit cost an LLM agent, so a repo's lifetime *resource utilization* can
be estimated. Token counts and model are **measured** verbatim from the agent's own session
transcript; energy and carbon are derived later from the recorded tokens/time and the commit
timestamp (which fixes the grid's carbon intensity at that moment).

Everything the tool owns lives under **one** directory, **`.llm_resource_tally/`** in your repo
root, committed alongside your code: the ledger data in `ledger/`, and the vendored tool code in
`tool/`.

## Quick start

From inside the repo you want to track:

```bash
curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
```

That vendors a self-contained copy of the tool into `.llm_resource_tally/tool/` and wires a git
`post-commit` hook (plus a managed `AGENTS.md` block) — offline after the initial fetch. Review
and **commit `.llm_resource_tally/` + `AGENTS.md`** to share it. From then on every `git commit`
auto-records what it cost.

**Claude Code users** — add precise cross-repo attribution (recommended):
```bash
python3 .llm_resource_tally/tool install --claude   # also wires a Claude PostToolUse hook
```

Prefer pip or a git submodule, or want to vendor elsewhere / re-wire a fresh clone / update /
uninstall? See **[docs/install.md](docs/install.md)**.

## Usage

With the hook installed, recording is automatic. `<rt>` below is `python3 .llm_resource_tally/tool`:

```bash
<rt> reconcile --label review   # sweep turns that produced no commit (planning, chat, review)
<rt> rollup                     # refresh lifetime totals -> .llm_resource_tally/lifetime-totals.json
<rt> show                       # print the ledger
```

The one habit to keep: **at session end, run `<rt> reconcile && <rt> rollup`** — the hook only
fires on commits, so `reconcile` is what captures planning/chat/review that produced none. Tag
work with `--label` (e.g. `record --label implementation`) so `rollup` can break usage down
`by_activity`. Non-Claude agents: `<rt> record --backend <name> --transcript <session.jsonl>`.

## Documentation

- **[Install & wiring](docs/install.md)** — every install route (curl / pip / submodule), the
  `<rt>` alias, hook-mode options, update/uninstall, and self-replicating installs.
- **[Attribution](docs/attribution.md)** — how cost is attributed to commits, cross-repo and
  submodule cases, the Claude `--claude` hook, correctness guarantees, history rewrites, and
  context compaction.
- **[Data model](docs/data-model.md)** — where data lives, the measurements-only principle, the
  compact rolling ledger, and `lifetime-totals.json`.
- **[Backends](docs/backends.md)** — the agent-agnostic core and how to add one (Codex, etc.).
- **[Development](docs/development.md)** — package layout, the three invocation styles, tests & CI.
- **[Related work](docs/related-work.md)** — how this differs from ccusage, claude-budget,
  llm-usage-metrics, Claude Code Analytics, and live monitors.

## License

Apache-2.0. See [LICENSE](LICENSE).
