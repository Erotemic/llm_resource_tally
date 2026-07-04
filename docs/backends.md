# Backends

Everything agent-specific — where transcripts live, how tokens are parsed, whether the agent
has a compaction concept — is isolated behind a `Backend`
([`backends/`](../llm_resource_tally/backends/)). `claude` (Claude Code) is the default backend,
and `codex` reads Codex CLI transcripts from `~/.codex/sessions/` (or
`$CODEX_SESSIONS_DIR`). The core (record/reconcile/rollup, the ledger, git wiring) is
backend-agnostic. Each row records its `agent`, so a repo can mix backends.

Adding another agent is a new `Backend` implementing
[`backends/base.py`](../llm_resource_tally/backends/base.py) — nothing else changes.

## Registered backends (what the passive hook records)

The git `post-commit` hook runs a bare `record` (no `--backend`). Rather than hard-code an agent,
that bare form walks the repo's **registered backends** — the `backends` list in
`.llm_resource_tally/settings.json` — and records whichever one has a session **matching this
repo**. Matching is *strict*: a backend that finds no session for this repo (or only an unrelated
session in some other directory) records nothing, so a stray Codex session elsewhere is never
mis-attributed to your commit.

- A fresh install registers `claude` (the default). `settings.json` is a committed, hand-editable
  JSON file, so a mixed team shares one list.
- Register more with install: `<rt> install --backend codex` unions `codex` in (claude is always
  kept). After that, Codex commits auto-record through the same hook — no flag per commit.
- **Explicit invocation still wins.** Passing `--backend <name>` (or `--session` / `--transcript`)
  bypasses the registered list and uses exactly that backend with its normal discovery — handy
  for one-offs or backends without auto-discovery:
  `<rt> record --backend <name> --transcript <path/to/session.jsonl>`.
