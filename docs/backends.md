# Backends

Everything agent-specific — where transcripts live, how tokens are parsed, whether the agent
has a compaction concept — is isolated behind a `Backend`
([`backends/`](../llm_resource_tally/backends/)). Built-in backends:

- **`claude`** (Claude Code) — the default; reads `~/.claude/projects/**` JSONL, including
  Task/sidechain **subagent** sessions (`<project>/<session-id>/subagents/`).
- **`codex`** — Codex CLI JSONL under `~/.codex/sessions/` (or `$CODEX_SESSIONS_DIR`).
- **`opencode`** — reads the opencode **SQLite** store (`~/.local/share/opencode/opencode.db`,
  or `$OPENCODE_DATA_DIR`) via stdlib `sqlite3`, read-only. Not on by default (it would query
  the DB on every commit for non-users); opt in with `install --backend opencode`.
- **`pi`** (Pi coding agent) — reads Pi's session JSONL files, under Pi's session root
  `~/.pi/agent/sessions/` (per-cwd encoded dirs) or directly in an explicit session dir
  (`$PI_SESSIONS_DIR` / Pi's `$PI_CODING_AGENT_SESSION_DIR` / the `sessionDir` setting). Not on
  by default; opt in with `install --backend pi`. Details below.

The core (record/reconcile/rollup, the ledger, git wiring) is backend-agnostic. Each row
records its `agent`, so a repo can mix backends.

Adding another agent is a new `Backend` implementing
[`backends/base.py`](../llm_resource_tally/backends/base.py) — nothing else changes.

## Registered backends (what the passive hook records)

The git `post-commit` hook runs a bare `record` (no `--backend`). Rather than hard-code an agent,
that bare form walks the repo's **registered backends** — the `backends` list in
`.llm_resource_tally/settings.json` — and records whichever one has a session **matching this
repo**. Matching is *strict*: a backend that finds no session for this repo (or only an unrelated
session in some other directory) records nothing, so a stray Codex session elsewhere is never
mis-attributed to your commit.

- A fresh install registers **both** `claude` and `codex` by default (strict matching means a
  backend with no session for this repo simply records nothing, so enabling one you don't use is
  harmless). `settings.json` is a committed, hand-editable JSON file, so a mixed team shares one
  list — trim it to just `["claude"]` if you never use Codex.
- Register additional backends with install: `<rt> install --backend <name>` unions it in
  (existing entries are always kept). After that, that backend's commits auto-record through the
  same hook — no flag per commit.
- **Explicit invocation still wins.** Passing `--backend <name>` (or `--session` / `--transcript`)
  bypasses the registered list and uses exactly that backend with its normal discovery — handy
  for one-offs or backends without auto-discovery:
  `<rt> record --backend <name> --transcript <path/to/session.jsonl>`.

## Pi

Pi writes one JSONL file per session, in one of two layouts:

- **Default storage** (no explicit dir is set): `<agent-dir>/sessions/--<munged-cwd>--/<ts>_<uuid>.jsonl`,
  where `<agent-dir>` is `~/.pi/agent` (or `$PI_CODING_AGENT_DIR`) and the munged dir is the
  session's full cwd with a single leading `/` or `\` stripped and every remaining `/`, `\`, `:`
  turned into `-` (dots, underscores, and spaces preserved). A session started in a
  subdirectory of a repo therefore lives in `--<repo>-<sub>--/`.
- **Explicit session dir** — `--session-dir`, `PI_CODING_AGENT_SESSION_DIR`, or the `sessionDir`
  setting (and tally's own `$PI_SESSIONS_DIR` override) name a directory that Pi passes to its
  `SessionManager` verbatim: the `<ts>_<uuid>.jsonl` files live **directly in that dir**, one
  level deep. The encoded-cwd dir is part of Pi's *default* path computation, not something
  appended to an explicit dir, so discovery of an explicit dir never looks for (or requires)
  an encoded-cwd child beneath it. The `sessionDir` setting is read from Pi's project settings
  `<cwd>/.pi/settings.json` (which wins over the global `<agent-dir>/settings.json` when both
  set one); a *relative* value — from either file, and from `--session-dir` /
  `PI_CODING_AGENT_SESSION_DIR` — resolves against Pi's working directory, never against the
  directory containing the settings file.
- **Discovery boundary for one-off session dirs.** Tally's resolver takes the working directory
  of the invocation it is reconstructing (default: its own cwd, which is what a git hook sees)
  and reads exactly those two settings files — it does not walk the repo for `.pi` dirs. That
  reconstructs the settings-based choice whenever the tally shares Pi's cwd, but it cannot
  reconstruct a session parked in a one-off `--session-dir` or an extension-chosen directory:
  no trace of such a choice remains after Pi exits, and no amount of scanning would recover it
  reliably. For those sessions, reconcile with an explicit `--projects-dir <actual-session-dir>`
  (or point tally's `$PI_SESSIONS_DIR` at the dir); exact post-commit attribution through
  `$PI_SESSION_FILE` works regardless of how Pi chose its dir. (Repository containment is a
  separate anchor: whether a discovered session's header `cwd` belongs to this repo is decided
  against the git superproject root, independent of the cwd the resolver used.)

Either way, v1 (linear), v2, and v3 (current, tree with entry ids) files all parse. Pi v1 already
has a `type: "session"` header; it lacks the `version` field and its entries do not yet have the
v2 tree ids/parent links. Malformed/torn JSONL records are skipped while locating the header, and
a torn tail does not kill a read. Directory naming is never trusted on its own: every candidate
file is validated against its session header `cwd` (which must lie in this repo) before it is
attributed here.

- **Model identity is provider-qualified, and billing vs state are distinct.** Each assistant
  message records the requested `provider` and `model`; newer Pi versions also record the
  concrete model that answered in `responseModel` when it differs (e.g. a fallback). The call
  is billed as `<provider>/<responseModel ?? model>` — Pi's own usage keying — so one repo can
  mix cloud and local endpoints in a single ledger and `report --by model` splits them. The
  model state a call establishes for its descendants is the requested `<provider>/<model>` —
  exactly Pi's own session-state reconstruction (`getSessionContextSettings`) — so built-in
  compactions and branch summaries inherit the logical model, and a later `model_change` still
  overrides it. Tool-result usage is usage from the tool execution itself; Pi persists no
  provider/model for that work, so tally keeps those measured tokens under model `?` rather than
  guessing that the surrounding conversation model performed them.
- **Compaction usage is measured when Pi persists usage, but model provenance can be unknown.**
  A Pi `compaction` or `branch_summary` entry with a real `usage` object is billed as measured
  usage. For Pi's built-in summaries, the persisted entry does not name a model, so tally uses
  the logical model inherited from the entry's parent path. For extension-generated entries
  (`fromHook: true`), Pi likewise persists usage but no standardized provider/model and the
  extension may have called a different LLM; tally therefore keeps the measured tokens but bills
  them under model `?` rather than inventing provenance. An entry with *no* usage object becomes
  a reconstructed compaction-estimate row; `fromHook` estimates likewise use model `?`.
- **Fork/clone copies expose the same source observation.** Pi emits a versioned `pi-v2:`
  identity for each billable entry. It excludes generated tree ids, so v1→v3 migration preserves
  identity; older fingerprint aliases preserve previously recorded usage. The parser retains copied
  entries for the allocator to decide ownership. See [stable observation allocation](observation-allocation.md)
  for ledger authority, SQLite recovery, and cross-repository limits.
- **Zero usage.** pi-ai pre-allocates a zero-filled usage struct and keeps it when an endpoint
  reports nothing. A zero-usage call that *failed* (`stopReason: "error"`) consumed nothing and
  is excluded entirely; a zero-usage call that *succeeded* means the endpoint is not reporting
  token counts — it stays as a zero-token turn (turn counts stay honest) and `doctor` warns
  whenever any successful call reports zero usage, so partial endpoint under-reporting is visible.
- **Reasoning tokens are a subset of `output`** in both of pi-ai's provider normalizers, so
  they are not added on top of output (adding them would inflate output by ~60% for
  reasoning-heavy models).
- **Exact attribution via `$PI_SESSION_FILE`.** Commands run by Pi's shell tool carry the
  session's file path in their environment, so a commit made from a Pi session is attributed to
  exactly that session (the hook prefers it over most-recent-modified discovery) — including
  when the session's recorded cwd is a different tree than the repo that received the commit
  (the hint is trusted like the Claude `--claude` hook's).
- **Session identity** is the header's uuid (the filename is `<timestamp>_<uuid>.jsonl`, and
  forks get fresh uuids), which is what the ledger's `session` field uses. Observation allocation
  is deliberately independent of that session id.
- **Coverage limits.** Ephemeral sessions (`--no-session`, SDK `inMemory`) write no file and
  are invisible to this backend. A session started in a directory that is neither this repo nor
  one of its subdirectories is only attributed to a commit here through `$PI_SESSION_FILE`
  (its header `cwd` fails the strict containment check, on purpose).
