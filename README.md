# llm_resource_tally — measured LLM resource accounting (per commit)

A small, **self-contained** tool (Python **stdlib only** — zero dependencies) that records the
measured LLM usage associated with building and maintaining a repository. Its practical goal is to
make the order of magnitude of LLM-assisted development visible: measured tokens and model names
feed explicit, revisable estimates of operational electricity, greenhouse-gas emissions, and
expenditure.

The default layout keeps the append-only ledger and vendored tool under
**`.llm_resource_tally/`**, committed alongside the code. Optional ignored and git-notes storage
modes are available when accounting should not alter the worktree. Measurements remain separate
from every energy, carbon, price, or mitigation assumption.

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
<rt> show                       # print the raw ledger
<rt> report --by commit         # readable grouped views (--by commit|day|activity|agent|model)
<rt> report --commits main..HEAD  # the measured cost of a branch / PR
<rt> estimate                   # cited central energy/carbon/API-cost estimate
<rt> estimate --pack generic-wide # broad dependency-free scenario bounds
<rt> estimate --mitigation        # separately price typed mitigation/removal scenarios
<rt> doctor                     # is the hook armed? backends found? retention safe?
<rt> fleet ~/code               # one report across every repo's ledger under a dir
```

`estimate` turns the ledger's **measured tokens** into energy (kWh), carbon (gCO₂e), and USD
using a versioned, editable **assumption pack** — the modeling layer is kept *outside* the
ledger so it can change without re-recording. It lives in a separate **modeling package** that
the minimal `curl` install leaves out (so bootstrapping stays tiny); add it with `<rt> install
--modeling` (or `RT_MODELING=1` at curl time, or `pip install llm_resource_tally`). The built-in
pack is a cited central baseline; pass `--pack your-pack.json`, use the broad offline
`generic-wide` pack, or select the shipped per-region grid (`--pack grid-codecarbon --region FRA`)
built from CodeCarbon data. Optional `--mitigation` pricing is a separate account and never
subtracts from gross emissions. See **[docs/modeling.md](docs/modeling.md)**.

The one habit to keep: **at session end, run `<rt> reconcile && <rt> rollup`** — the hook only
fires on commits, so `reconcile` is what captures planning/chat/review that produced none. Tag
work with `--label` (e.g. `record --label implementation`) so `rollup` can break usage down
`by_activity`. Codex agents can record with `<rt> record --backend codex`; other non-Claude
agents use `<rt> record --backend <name> --transcript <session.jsonl>`.

## How tracking works

The tool reads the **session transcript** your agent already writes (Claude Code and Codex both
do) and, per **turn** (one API call), keeps only the measurements the agent itself logged — token
counts, model, timestamps — **never message content, code, or prompts**. Each turn is attributed
to the commit it feeds; turns that produce no commit are swept by `reconcile`. Rows are deduped by
message id and appended to the selected ledger storage.

- **Measured & stored** (verbatim from the transcript): model; tokens by kind (input, cache-write,
  cache-read, output); server-tool calls where the agent reports them; turn timestamps +
  wall-clock; and context-compaction signals (peak context, summary size) when the agent compacts.
- **Derived later, never stored:** inference-seconds, energy (kWh), carbon (gCO₂e), USD. Each is
  an assumption *over* the measurements, so the modeling pass can change without re-recording.
- **Not captured** (nothing to read): a commit made with no agent session; usage older than the
  agent's transcript retention (Claude Code defaults to 30 days); and a session in one repo that
  commits into another, which needs a hint to attribute (the Claude `--claude` hook, or a one-line
  manual bridge).

**Does the agent have to think about this?** For Claude Code, no — the git `post-commit` hook
records every commit automatically. Codex (or a mix) is the same after a one-time `install
--backend codex`, which registers it in `.llm_resource_tally/settings.json` so the hook records it
too; the hook walks the registered backends and records whichever agent actually produced the
commit (matched strictly to this repo, so an unrelated session is never mis-attributed). Work that
makes no commit is captured by a **session-end sweep** (`reconcile && rollup`) — which
`install --claude` automates via a Claude **SessionEnd** hook, so the agent needn't remember it.
The one unavoidable manual step is running `install` once in a fresh clone, since git never
transfers hook wiring (`core.hooksPath`) on clone.

Case-by-case details — cross-repo, submodules, non-committing work, history rewrites, compaction,
per-backend field mapping, storage, modeling boundaries, and the exact on-disk fields — are in
the docs below. The managed `AGENTS.md` block explicitly tells agents that generated accounting
changes are expected bookkeeping so they do not waste cycles investigating normal ledger updates.

## Documentation

- **[Mission and method](docs/mission-and-method.md)** — the repository-footprint question, why
  ballpark estimates are useful, and the exact claim the tool is designed to support.
- **[Challenges and roadmap](docs/challenges-and-roadmap.md)** — coverage, attribution,
  uncertainty, evidence quality, accounting boundaries, and how estimates can improve.
- **[Install & wiring](docs/install.md)** — every install route (curl / pip / submodule), the
  `<rt>` alias, hook-mode options, update/uninstall, and self-replicating installs.
- **[Attribution](docs/attribution.md)** — how cost is attributed to commits, cross-repo and
  submodule cases, the Claude `--claude` hook, correctness guarantees, history rewrites, and
  context compaction.
- **[Data model](docs/data-model.md)** — where data lives, the measurements-only principle, the
  compact rolling ledger, and generated reports.
- **[Storage modes](docs/storage.md)** — committed, ignored, and git-notes ledgers, including notes
  synchronization and mode switching.
- **[Ledger format spec](docs/schema-spec.md)** — the on-disk row format (v3), file layout, and
  de-dup rules, precise enough for another tool to read or write the ledger.
- **[Reporting & modeling](docs/modeling.md)** — `report`, `fleet`, central and interval
  `estimate` packs, CodeCarbon regional grids, provenance, and `doctor`.
- **[Carbon credits and removal](docs/carbon-credits-and-removal.md)** — avoidance versus actual
  removal, biochar and durable pathways, uncertainty, provider due diligence, and separate
  mitigation-cost scenarios.
- **[Backfill](docs/backfill.md)** — recovering usage from before the hook was installed, and the
  retention horizon that bounds how far back you can go.
- **[Backends](docs/backends.md)** — the agent-agnostic core and how to add one (Codex, etc.).
- **[Development](docs/development.md)** — package layout, the three invocation styles, tests & CI.
- **[Related work](docs/related-work.md)** — how this differs from ccusage, claude-budget,
  llm-usage-metrics, Claude Code Analytics, and live monitors.

## License

Apache-2.0. See [LICENSE](LICENSE).
