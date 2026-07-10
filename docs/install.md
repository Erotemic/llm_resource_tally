# Installing, updating, and wiring

The [README](../README.md) covers the common case. This document defines the complete install
contract, including portable repository policy, artifact conversion, storage migration, and hook
wiring.

The executable and the measured ledger are separate. The default executable is a deterministic
single-file zipapp at `.llm_resource_tally/tool.pyz`; a source-tree artifact at
`.llm_resource_tally/tool/` remains available for debugging and development. Neither artifact is
the measurement source of truth.

Throughout these docs, **`<rt>`** means the installed invocation, usually:

```bash
python3 .llm_resource_tally/tool.pyz
```

## Portable installation policy

Every configured repository has a committed policy at:

```text
.llm_resource_tally/settings.json
```

For example:

```json
{
  "backends": ["claude", "codex"],
  "installation": {
    "storage": "ignored",
    "tool_format": "zipapp",
    "tool_path": ".llm_resource_tally/tool.pyz",
    "modeling": true
  }
}
```

The `installation` object is the canonical source of truth for:

- `storage`: `committed`, `ignored`, or `notes`;
- `tool_format`: `zipapp` or `source`;
- `tool_path`: repository-relative artifact path;
- `modeling`: whether the optional estimate/modeling package is included.

Precedence is:

1. explicit `install` or `update` flags, or bootstrap environment variables;
2. `.llm_resource_tally/settings.json`;
3. built-in defaults (`committed`, `zipapp`, `.llm_resource_tally/tool.pyz`, no modeling).

Explicit flags replace and persist the policy. Omitted flags reuse it. No machine-local git config
is needed to remember the intended installation.

This matters most for ignored mode: generated state and the tool may be absent from a fresh clone,
but `settings.json` remains committed, so a plain bootstrap reconstructs the same representation.

## Artifact formats

Choose explicitly when changing policy:

```bash
<rt> install --tool-format zipapp
<rt> install --tool-format source
```

- `zipapp` installs one executable `.pyz`. Built-in data is loaded through
  `importlib.resources`, and updates replace the file atomically.
- `source` installs ordinary Python files. This is useful for inspection, local modification, and
  development.

If `--dir` is supplied without `--tool-format`, a `.pyz` suffix selects zipapp; other paths select
source format. The resolved format and path are written back to `settings.json`.

Build a zipapp directly from this repository:

```bash
python3 . build-zipapp --output dist/llm_resource_tally.pyz
python3 . build-zipapp --output dist/llm_resource_tally-full.pyz --modeling
```

## Installation routes

### A. Bootstrap with curl

From inside the repository to configure:

```bash
curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
```

The bootstrap reads the committed policy before choosing the artifact, storage mode, path, and
modeling content. This is the normal fresh-workstation command, including for ignored mode.

Environment variables are explicit overrides and are persisted by the installed tool:

```text
RT_TOOL_FORMAT=zipapp|source
RT_DIR=.llm_resource_tally/tool.pyz
RT_STORAGE=committed|ignored|notes
RT_MODELING=0|1
RT_REF=v1.2.3
RT_REPO=owner/name
```

For example:

```bash
RT_TOOL_FORMAT=zipapp RT_STORAGE=ignored RT_MODELING=1 \
  curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
```

### B. Pip

```bash
pip install llm_resource_tally
cd /your/repo
llm_resource_tally install
```

Pip is only the delivery mechanism. `install` materializes the repository-owned artifact described
by `settings.json`. The pip package is not needed afterward.

### C. Git submodule or source checkout

```bash
git submodule add https://github.com/Erotemic/llm_resource_tally .llm_resource_tally/tool
python3 .llm_resource_tally/tool install --tool-format source --modeling
```

Generated hooks are written outside the source checkout. To replace the submodule representation
with a zipapp, use `update --tool-format zipapp` or invoke `install --tool-format zipapp` from a
separate checkout/pip installation.

## Updating and changing policy

`update` is both the network updater and the clean representation/storage migration command:

```bash
<rt> update
<rt> update --tool-format zipapp
<rt> update --tool-format source
<rt> update --storage ignored
<rt> update --storage committed
<rt> update --storage notes
<rt> update --modeling
<rt> update --no-modeling
```

Flags can be combined:

```bash
<rt> update --tool-format zipapp --storage ignored --modeling
```

The command fetches the requested ref, builds the replacement artifact first, runs its installer,
rewrites hooks and `AGENTS.md`, persists the new policy, and removes the obsolete managed artifact
when the path changes. The ledger is never embedded in the artifact and is never deleted by an
artifact conversion.

An offline `install` can also change storage and format when the current invocation contains the
required source. Changing modeling content inside the exact artifact currently executing is
intentionally delegated to `update`, which can build the replacement before running it.

## Ignored-mode index migration

When changing from committed to ignored storage, `.gitignore` alone is insufficient because Git
continues tracking files already in the index. The installer therefore:

1. writes the managed ignore block;
2. stages removal of previously tracked generated tally/tool paths;
3. force-retains `.llm_resource_tally/settings.json` in the index.

Review and commit the resulting staged deletions. On a fresh ignored-mode install, only the policy
file, `.gitignore`, and normal documentation changes are candidates for commit; the executable and
generated accounting state remain local.

## Claude native hooks

```bash
<rt> install --claude
```

This adds best-effort, idempotent entries to `.claude/settings.json` for:

- `PostToolUse(Bash)`, providing exact cross-repository commit attribution;
- `SessionEnd`, running a final reconcile/rollup sweep.

## Git hook wiring

Choose hook behavior with:

```text
--hook-mode auto|hookspath|append|none
```

`auto` uses `core.hooksPath` when doing so will not shadow an existing hook, otherwise it appends a
sentinel-delimited block to the active `post-commit`. Artifact conversion rewrites the managed hook
to invoke the new path.

## Uninstall

```bash
<rt> uninstall
```

This removes managed hook and `AGENTS.md` wiring. It deliberately leaves the ledger, portable
settings, and installed artifact in place.
