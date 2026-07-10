# Ledger storage modes

Storage is repository policy, not workstation-local configuration. The canonical choice lives in
`.llm_resource_tally/settings.json` under `installation.storage`. Running `install` or `update`
without `--storage` reuses that value; an explicit flag replaces it.

```bash
<rt> install --storage committed
<rt> install --storage ignored
<rt> install --storage notes
```

The tool format is independent: any storage mode can use either a zipapp or source-tree artifact.

## `committed`

Measured JSONL shards, generated reports, the tool artifact, and settings normally live under
`.llm_resource_tally/` and may travel with ordinary commits. Ledger shards use `merge=union`.

This is the most portable mode because the executable, observations, and policy all clone normally.

## `ignored`

Generated accounting state and the installed tool remain local, while the portable policy remains
committed.

The managed root `.gitignore` block is equivalent to:

```gitignore
/.llm_resource_tally/*
!/.llm_resource_tally/settings.json
```

A custom tool path outside `.llm_resource_tally/` is also ignored. This layout allows a fresh clone
to retain the intended storage mode, artifact format, path, backend list, and modeling choice even
though the executable and ledger are absent.

On a new workstation, run the ordinary bootstrap:

```bash
curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
```

The bootstrap reads `settings.json` and recreates the intended local installation without requiring
`RT_STORAGE=ignored` or other repeated flags.

When converting an already committed installation to ignored mode, the installer stages removal of
tracked generated paths and then force-retains `settings.json`. Review and commit those index
changes. It does not delete the local generated files.

## `notes`

Measured rows are appended to:

```text
refs/notes/llm-resource-tally
```

Mutable reports and locks live under the Git common directory, while
`.llm_resource_tally/settings.json` remains committed in the worktree. The tool artifact can remain
committed or use another policy-selected path.

Git notes are not fetched or pushed by default:

```bash
git push origin refs/notes/llm-resource-tally
git fetch origin refs/notes/llm-resource-tally:refs/notes/llm-resource-tally
```

## Switching modes

Use either offline install or network update:

```bash
<rt> install --storage ignored
<rt> update --storage notes
```

The explicit choice is persisted to `settings.json`. New writes use the selected destination.
Readers union worktree shards and the configured notes ref, then de-duplicate observations, so a
mode change does not hide older locally available measurements.

Storage conversion does not rewrite historical ledger rows into a new backend. It changes where new
rows and mutable outputs go. The committed-to-ignored transition additionally updates the index so
tracked generated files stop producing normal worktree diffs.
