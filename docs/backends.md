# Backends

Everything agent-specific — where transcripts live, how tokens are parsed, whether the agent
has a compaction concept — is isolated behind a `Backend`
([`backends/`](../llm_resource_tally/backends/)). `claude` (Claude Code) is the default and only
backend today; the core (record/reconcile/rollup, the ledger, git wiring) is backend-agnostic.
Each row records its `agent`, so a repo can mix backends.

Adding Codex or another agent is a new `Backend` implementing
[`backends/base.py`](../llm_resource_tally/backends/base.py) — nothing else changes. Select one
with `--backend <name>`; for non-Claude agents also point at the transcript:
`<rt> record --backend <name> --transcript <path/to/session.jsonl>`.
