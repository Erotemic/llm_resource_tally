# llm_resource_tally — measured LLM resource accounting (per commit)

A small, **self-contained** tool (Python **stdlib only** — zero dependencies) that records the
measured LLM usage associated with building and maintaining a repository. Its practical goal is to
make the order of magnitude of LLM-assisted development visible: measured tokens and model names
feed explicit, revisable estimates of operational electricity, greenhouse-gas emissions, and
expenditure.

The repository-owned installation policy lives in committed
**`.llm_resource_tally/settings.json`**. It records the intended tool representation, invariant path,
modeling content, storage mode, publication destinations, and backends. By default, generated
accounting accumulates under gitignored `.llm_resource_tally/local/`; `publish` appends it to
`.llm_resource_tally/ledger/` and refreshes `.llm_resource_tally/lifetime-totals.json`. Either
durable destination can instead point elsewhere — including a sibling accounting repository —
without moving the tally tool itself. Legacy eager-committed, fully ignored, and git-notes modes
remain available. Measurements remain separate from every energy, carbon, price, or mitigation
assumption.

## Quick start

Inspect the bootstrap without making changes:

```bash
curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh -s -- --help
```

From inside the repo you want to track:

```bash
curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
```

That builds a deterministic, self-contained zipapp file with uncompressed ZIP members at
`.llm_resource_tally/tool` and wires a
git `post-commit` hook (plus a managed `AGENTS.md` block) — offline after the initial fetch.
Review and commit the intended policy/documentation changes. In the default local mode, hooks write
only beneath `.llm_resource_tally/local/`, so ordinary commits, merges, rebases, and stashes do not
encounter tally-generated tracked changes. `publish` is what turns those spooled rows into an
durable configured output; with default paths that is an ordinary main-repository change, while
redirected paths can land in a sibling repository. It runs at session end and whenever an agent
hands off substantial work. From then on every `git commit` auto-records what it cost.
Source-tree installs remain available with `RT_TOOL_FORMAT=source` or `install --tool-format source`.
Use `zipapp-deflate` only when minimizing the checked-out artifact matters more than
Git-friendly updates.

**Claude Code users** — add precise cross-repo attribution (recommended):
```bash
python3 .llm_resource_tally/tool install --claude   # also wires a Claude PostToolUse hook
```

Prefer pip or a git submodule, want to migrate between source and zipapp, change storage policy,
or reconstruct an installation on a fresh workstation? See
**[docs/install.md](docs/install.md)**.

## Usage

With the hook installed, recording is automatic. `<rt>` below is `python3 .llm_resource_tally/tool`:

```bash
<rt> config show                # effective repository policy and where it came from
<rt> config set --storage local # switch storage without reinstalling or downloading the tool
<rt> config set --append-ledger-dir ../accounting/ledger  # publish JSONL outside this repo
<rt> reconcile --label review   # sweep turns that produced no commit (planning, chat, review)
<rt> rollup                     # refresh local lifetime totals
<rt> publish                    # append local JSONL to configured durable destinations
<rt> show                       # print the raw ledger
<rt> report --by commit         # readable grouped views (--by commit|day|activity|agent|model)
<rt> report --commits main..HEAD  # the measured cost of a branch / PR
<rt> estimate                   # cited central energy/carbon/API-cost estimate
<rt> estimate --pack generic-wide # broad dependency-free scenario bounds
<rt> estimate --mitigation        # separately price typed mitigation/removal scenarios
<rt> doctor                     # is the hook armed? backends found? retention safe?
<rt> fleet ~/code               # one report across every repo's ledger under a dir
```

`config` changes repository policy in committed `.llm_resource_tally/settings.json`. `install`
installs or repairs the executable, hooks, and managed guidance; `update` downloads a newer tool.
Storage modes select how mutable accounting rows are written. The `publication` settings object
selects where the durable append ledger and lifetime totals live. Recorder backends such as Claude
and Codex instead select which agent transcripts the passive recorder can read.

`estimate` turns the ledger's **measured tokens** into energy (kWh), carbon (gCO₂e), and USD
using a versioned, editable **assumption pack** — the modeling layer is kept *outside* the
ledger so it can change without re-recording. It lives in a separate **modeling package** that
the minimal zipapp leaves out (so bootstrapping stays tiny); add it with `<rt> install
--modeling` (or `RT_MODELING=1` at curl time, or `pip install llm_resource_tally`). The built-in
pack is a cited central baseline; pass `--pack your-pack.json`, use the broad offline
`generic-wide` pack, or select the shipped per-region grid (`--pack grid-codecarbon --region FRA`)
built from CodeCarbon data. Optional `--mitigation` pricing is a separate account and never
subtracts from gross emissions. See **[docs/modeling.md](docs/modeling.md)**.

When a session-end hook fires, `<rt> reconcile && <rt> rollup` captures non-committing work and
refreshes local summaries. Session end is best effort rather than a durability boundary: commit
rows are already appended by `post-commit`. Tag work with `--label` (e.g. `record --label
implementation`) so `rollup` can break usage down `by_activity`. Codex agents can record with
`<rt> record --backend codex`; other non-Claude agents use `<rt> record --backend <name>
--transcript <session.jsonl>`.

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
the docs below. The managed `AGENTS.md` block tells agents that local accounting is ignored and
must never be hand-managed, and that publishing before a handoff is routine — so they neither
waste cycles tidying tally state nor leave measurements stranded on one machine.

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
- **[Storage modes](docs/storage.md)** — default local spool + explicit publication, plus committed,
  ignored, and git-notes compatibility modes.
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
- **[Development](docs/development.md)** — package layout, zipapp build, invocation styles, tests & CI.
- **[Related work](docs/related-work.md)** — how this differs from ccusage, claude-budget,
  llm-usage-metrics, Claude Code Analytics, and live monitors.

## License

Apache-2.0. See [LICENSE](LICENSE).
