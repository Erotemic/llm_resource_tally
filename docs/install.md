# Installing & wiring

The [README](../README.md) covers the common case. This is the complete reference for delivery
formats, hook wiring, updating, and removal.

The tool artifact and the measured ledger are deliberately separate. A normal new install places
a deterministic single-file zipapp at `.llm_resource_tally/tool.pyz`; the historical source-tree
artifact at `.llm_resource_tally/tool/` remains supported. Either representation works offline and
is disposable. The ledger is never embedded in or replaced with the tool.

Throughout the docs, **`<rt>`** means the installed invocation. For the default format:

```bash
python3 .llm_resource_tally/tool.pyz
```

For a source-tree install it is `python3 .llm_resource_tally/tool`.

## Artifact formats

`install --tool-format auto|zipapp|source` controls the representation:

- `auto` preserves an existing install. A new pip/bootstrap install chooses `zipapp`.
- `zipapp` installs one executable `.pyz`; built-in data is read with `importlib.resources`.
- `source` installs the import package as ordinary files for development or debugging.

The zipapp is reproducible for a fixed source tree and `SOURCE_DATE_EPOCH`, contains an embedded
version and build metadata, creates no package-local `__pycache__`, and is replaced atomically on
update. It is still an ordinary ZIP archive and can be inspected with standard tools.

Build one directly from this repository:

```bash
python3 . build-zipapp --output dist/llm_resource_tally.pyz
python3 . build-zipapp --output dist/llm_resource_tally-full.pyz --modeling
```

## Routes

### A. curl → sh

```bash
curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
```

The default result is `.llm_resource_tally/tool.pyz` containing the measurement core. Add the
optional energy/carbon modeling package later with `<rt> install --modeling`, or include it in the
initial artifact:

```bash
RT_MODELING=1 curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
```

Useful overrides:

```text
RT_TOOL_FORMAT=zipapp|source
RT_DIR=tools/tally.pyz
RT_REF=v1.2.3
RT_REPO=owner/name
RT_MODELING=1
RT_STORAGE=committed|ignored|notes
```

For the legacy source layout:

```bash
RT_TOOL_FORMAT=source curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
```

### B. pip

```bash
pip install llm_resource_tally
cd /your/repo
llm_resource_tally install
```

Pip is only the delivery mechanism. `install` builds a minimal local zipapp by default, after
which the host repository is self-contained and the pip package is not needed. Use
`llm_resource_tally install --modeling` for a full zipapp, or `--tool-format source` for ordinary
package files.

### C. git submodule

```bash
git submodule add https://github.com/Erotemic/llm_resource_tally .llm_resource_tally/tool
python3 .llm_resource_tally/tool install
```

A submodule remains a source-tree installation by default. Running `install` uses the repository
root as the invocation path, reads the correct `VERSION`, and writes generated hooks outside the
submodule. To produce a zipapp beside it instead:

```bash
python3 .llm_resource_tally/tool install --tool-format zipapp
# then validate and remove the submodule separately if it is no longer wanted
```

## Claude native hooks

```bash
<rt> install --claude
```

This adds two best-effort, idempotent entries to `.claude/settings.json`:

- **PostToolUse(Bash)** for exact cross-repository attribution;
- **SessionEnd** for automatic `reconcile && rollup` of non-committing work.

`uninstall` removes only the managed entries. In a submodule, enable these hooks only when the
superproject's sessions should count toward the submodule.

## Re-wire, update, and uninstall

```bash
<rt> install
<rt> update
<rt> uninstall
```

`install` is offline and idempotent. In `auto` mode it preserves the current source/zipapp format.
`update` fetches the configured ref and preserves both the artifact format and whether modeling is
included. `uninstall` removes wiring but deliberately leaves the ledger and tool artifact.

Hooks are shared through `core.hooksPath` only when that will not shadow an existing hook;
otherwise a sentinel-delimited block is appended to the active `post-commit`. Choose explicitly
with `--hook-mode auto|hookspath|append|none`.

## Self-replicating installs

A zipapp can seed another repository without network access:

```bash
mkdir -p /other/repo/.llm_resource_tally
cp .llm_resource_tally/tool.pyz /other/repo/.llm_resource_tally/tool.pyz
cd /other/repo
python3 .llm_resource_tally/tool.pyz install
```

It can also copy itself to a custom path:

```bash
python3 /path/to/tool.pyz install --tool-format zipapp --dir tools/tally.pyz
```

A source-tree install remains self-replicating with `cp -r` as before. The authoritative project
repository always remains normal source; only the host-repository deployment is zipped.

## Moving an existing install between formats

Conversion is explicit so the installer never deletes a working artifact unexpectedly:

```bash
# source tree -> sibling zipapp
python3 .llm_resource_tally/tool install --tool-format zipapp
python3 .llm_resource_tally/tool.pyz doctor

# pip/source checkout -> source-tree artifact
llm_resource_tally install --tool-format source
```

After validating the new artifact and hook path, remove the old artifact in an ordinary reviewed
commit. Ledger files and storage configuration are unaffected.

## Storage modes

`install --storage committed|ignored|notes` selects where new measurements and mutable state are
written. This is independent of whether the code is a zipapp or source tree.

- `committed` keeps the portable append-only ledger in the worktree.
- `ignored` keeps the same layout local and manages an explicit `.gitignore` block.
- `notes` writes measurements to `refs/notes/llm-resource-tally` and mutable reports below the git
  common directory.

See [Ledger storage modes](storage.md).
