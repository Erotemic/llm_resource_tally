# Backends

Everything agent-specific — where transcripts live, how tokens are parsed, whether the agent
has a compaction concept — is isolated behind a `Backend`
([`backends/`](../llm_resource_tally/backends/)). `claude` (Claude Code) is the default backend,
and `codex` reads Codex CLI transcripts from `~/.codex/sessions/` (or
`$CODEX_SESSIONS_DIR`). The core (record/reconcile/rollup, the ledger, git wiring) is
backend-agnostic. Each row records its `agent`, so a repo can mix backends.

Adding another agent is a new `Backend` implementing
[`backends/base.py`](../llm_resource_tally/backends/base.py) — nothing else changes. Select one
with `--backend <name>`. Codex can usually discover the right transcript from the repo cwd:
`<rt> record --backend codex`. For backends without discovery, point at the transcript:
`<rt> record --backend <name> --transcript <path/to/session.jsonl>`.
