# Installing, updating, and wiring

The executable and the measured ledger are separate. Every repository installation uses exactly
one executable path:

```text
.llm_resource_tally/tool
```

That path is either:

- a regular file containing a Python zipapp; or
- a directory containing the source package and `__main__.py`.

Python accepts both representations, so the invocation is invariant:

```bash
python3 .llm_resource_tally/tool
```

Throughout these docs, **`<rt>`** means that command. Neither executable representation is the
measurement source of truth.

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
    "tool_path": ".llm_resource_tally/tool",
    "modeling": true
  }
}
```

The `installation` object records:

- `storage`: `committed`, `ignored`, or `notes`;
- `tool_format`: `zipapp` or `source`;
- `tool_path`: always `.llm_resource_tally/tool`;
- `modeling`: whether the optional estimate/modeling package is included.

Precedence is:

1. explicit `install` or `update` flags, or bootstrap environment variables;
2. `.llm_resource_tally/settings.json`;
3. built-in defaults (`committed`, `zipapp`, no modeling).

Explicit choices are persisted. This is especially important in ignored mode: a fresh clone keeps
the policy even though its generated executable and ledger are absent.

## Artifact formats

Choose or change the representation without changing the invocation:

```bash
<rt> install --tool-format zipapp
<rt> install --tool-format source
```

- `zipapp` makes `.llm_resource_tally/tool` a deterministic ZIP file with an executable shebang.
- `source` makes `.llm_resource_tally/tool` a Python package directory.

A format switch builds and validates the replacement beside the active artifact, moves the old
file or directory aside, puts the replacement at the canonical path, and deletes the old
representation. Hooks and `AGENTS.md` do not need a format-specific command because they always
invoke the same path.

Build a standalone zipapp directly from this repository when needed:

```bash
python3 . build-zipapp --output dist/llm_resource_tally.pyz
python3 . build-zipapp --output dist/llm_resource_tally-full.pyz --modeling
```

The output name of a standalone build is arbitrary; only repository installations use the fixed
extensionless path.

## Installation routes

### A. Bootstrap with curl

Inspect the bootstrap interface without installing anything:

```bash
curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh -s -- --help
```

The help path exits before dependency checks, repository detection, downloads, or filesystem
changes. Unknown arguments also fail before installation begins.

From inside the repository to configure:

```bash
curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
```

The bootstrap reads the committed policy before choosing format, storage mode, and modeling
content. Environment variables are explicit overrides and are persisted:

```text
RT_TOOL_FORMAT=zipapp|source
RT_STORAGE=committed|ignored|notes
RT_MODELING=0|1
RT_REF=v1.2.3
RT_REPO=owner/name
```

There is no path override: repository installations always use `.llm_resource_tally/tool`.

### B. Pip

```bash
pip install llm_resource_tally
cd /your/repo
llm_resource_tally install
```

Pip is only the delivery mechanism. The repository-owned artifact is self-contained afterward.

### C. Source checkout

From a separate checkout of this project:

```bash
python3 /path/to/llm_resource_tally install --tool-format source --modeling
```

This copies the package into the canonical path. A checkout or submodule already occupying
`.llm_resource_tally/tool` is itself the source representation; switching it to zipapp replaces
that directory, so remove any obsolete submodule declaration as part of that intentional
migration.

## Updating and changing policy

`update` is both the network updater and the format/storage migration command:

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

The updater downloads a temporary source copy, builds the requested representation, validates it,
installs it at `.llm_resource_tally/tool`, rewrites policy and generated guidance, and removes the
old representation. Ledger data is outside the artifact and is never deleted by a format change.

The old pre-invariant path `.llm_resource_tally/tool.pyz` is removed after a successful install or
update.

## Ignored-mode index migration

When changing from committed to ignored storage, the installer:

1. writes the managed ignore block;
2. stages removal of tracked generated tally/tool paths;
3. force-retains `.llm_resource_tally/settings.json` in the index.

Review and commit the resulting staged changes. A fresh clone later runs the ordinary bootstrap,
which reconstructs the ignored executable and state from the committed policy.

## Claude native hooks

```bash
<rt> install --claude
```

This adds best-effort, idempotent entries to `.claude/settings.json` for cross-repository commit
attribution and a SessionEnd reconcile/rollup sweep.

## Git hook wiring

Choose hook behavior with:

```text
--hook-mode auto|hookspath|append|none
```

Generated hooks live at `.llm_resource_tally/hooks` and always invoke:

```bash
python3 -B "$root/.llm_resource_tally/tool"
```

Because the artifact path is invariant, format conversion does not move the hook directory or
change its command.

## Uninstall

```bash
<rt> uninstall
```

This removes managed hook and `AGENTS.md` wiring. It deliberately leaves the ledger, portable
settings, and installed artifact in place.
