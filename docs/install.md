# Installing, updating, and wiring

The executable and the measured ledger are separate. Every repository installation uses exactly
one executable path:

```text
.llm_resource_tally/tool
```

That path is either:

- a regular file containing a Python zipapp, with stored or deflated ZIP members; or
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
    "storage": "local",
    "tool_format": "zipapp",
    "tool_path": ".llm_resource_tally/tool",
    "modeling": true
  }
}
```

The `installation` object records:

- `storage`: `local`, `committed`, `ignored`, or `notes`;
- `tool_format`: `zipapp`, `zipapp-deflate`, or `source`;
- `tool_path`: always `.llm_resource_tally/tool`;
- `modeling`: whether the optional estimate/modeling package is included.

Precedence is:

1. explicit `install` or `update` flags, or bootstrap environment variables;
2. `.llm_resource_tally/settings.json`;
3. built-in defaults (`local`, `zipapp`, no modeling).

Explicit choices are persisted. In default local mode, a fresh clone keeps the tool and policy;
only unpublished local rows and mutable reports are machine-local.

## Artifact formats

Choose or change the representation without changing the invocation:

```bash
<rt> install --tool-format zipapp
<rt> install --tool-format zipapp-deflate
<rt> install --tool-format source
```

- `zipapp` makes `.llm_resource_tally/tool` a deterministic executable ZIP file whose members
  are stored uncompressed. This is the default because Git can delta-compress revisions well.
- `zipapp-deflate` makes the same single-file artifact with deflated members, reducing the
  checked-out size at the cost of noisier binary diffs between updates.
- `source` makes `.llm_resource_tally/tool` a Python package directory.

A format switch builds and validates the replacement beside the active artifact, moves the old
file or directory aside, puts the replacement at the canonical path, and deletes the old
representation. Hooks and `AGENTS.md` do not need a format-specific command because they always
invoke the same path.

Build a standalone zipapp directly from this repository when needed:

```bash
python3 . build-zipapp --output dist/llm_resource_tally.pyz
python3 . build-zipapp --output dist/llm_resource_tally-deflate.pyz --tool-format zipapp-deflate
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
RT_TOOL_FORMAT=zipapp|zipapp-deflate|source
RT_STORAGE=local|committed|ignored|notes
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
<rt> update --tool-format zipapp-deflate
<rt> update --tool-format source
<rt> update --storage local
<rt> update --storage ignored
<rt> update --storage committed
<rt> update --storage notes
<rt> update --modeling
<rt> update --no-modeling
```

Flags can be combined:

```bash
<rt> update --tool-format zipapp --storage local --modeling
```

The updater downloads a temporary source copy, builds the requested representation, validates it,
installs it at `.llm_resource_tally/tool`, rewrites policy and generated guidance, and removes the
old representation. Ledger data is outside the artifact and is never deleted by a format change.

## Local spool and publication

The default mode writes automatic observations and generated summaries beneath the managed ignore
path `.llm_resource_tally/local/`. Publish a reviewable repository snapshot explicitly:

```bash
<rt> publish
git add .llm_resource_tally/ledger/
git commit -m "Publish LLM resource tally"
```

`publish` appends to the tracked JSONL ledger. It does not stage, commit, or push.
Readers de-duplicate local and published overlap by row identity, so interrupted publication is
safe to retry.

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
attribution and a SessionEnd reconcile/rollup/publish sweep.

SessionEnd publication is a backstop, not the primary path — a session can end abruptly enough
that the hook never runs. Agents are told in the managed `AGENTS.md` block to publish after
substantial work for that reason. Both routes are idempotent: already-published rows are skipped and
the reports are deterministic, so a doubled publish is a no-op rather than a duplicate.

## Git hook wiring

Choose hook behavior with:

```text
--hook-mode auto|none
```

`none` skips hook wiring entirely. Earlier drafts also offered `hookspath` and `append`; once
tally stopped owning a generated worktree hook directory those stopped differing from `auto`, so
they were removed rather than kept as no-ops.

By default, the managed post-commit block lives in Git's repository-local hook directory
(`git rev-parse --git-path hooks`, normally `.git/hooks`). If the repository already has a
user-configured `core.hooksPath`, the installer respects it and appends the managed block there.
Older tally-owned `.llm_resource_tally/hooks` installations are migrated back to Git-local hooks.

The hook always invokes:

```bash
python3 -B "$root/.llm_resource_tally/tool"
```

Because the artifact path is invariant, format conversion does not change its command. The only
ignored tally state kept in the worktree is `.llm_resource_tally/local/`.

## Uninstall

```bash
<rt> uninstall
```

This removes managed hook and `AGENTS.md` wiring. It deliberately leaves the ledger, portable
settings, and installed artifact in place.
