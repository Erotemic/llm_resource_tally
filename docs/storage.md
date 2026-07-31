# Ledger storage modes

Storage is repository policy, not workstation-local configuration. The canonical choice lives in
`.llm_resource_tally/settings.json` under `installation.storage`. Running `install` or `update`
without `--storage` reuses that value; an explicit flag replaces it.

```bash
<rt> install --storage local       # default
<rt> install --storage committed
<rt> install --storage ignored
<rt> install --storage notes
```

The tool format is independent: any storage mode can use either a zipapp or source-tree artifact.

## `local` (default)

Hooks append compact JSONL rows and write mutable reports only beneath:

```text
.llm_resource_tally/local/
    ledger.jsonl
    ledger.<UTCstamp>.jsonl
    lifetime-totals.json
    badge.json
    ledger.lock
```

The installer manages this root ignore rule:

```gitignore
/.llm_resource_tally/local/
```

Therefore automatic recording does not modify tracked files and does not interfere with commits,
merges, rebases, or stash operations. The policy, tool artifact, managed guidance, and publication
merge policy remain normal repository files.

Publish the currently accumulated local JSONL on demand:

```bash
<rt> publish
```

`publish` writes one immutable content-addressed shard:

```text
.llm_resource_tally/ledger/ledger.sha256-<digest>.jsonl
```

It then clears the successfully snapshotted local files. Publication is crash tolerant: readers
union local and published rows and de-duplicate them by stable row identity, so overlap after an
interruption cannot double-count. A retry with identical bytes resolves to the same filename.
`publish` does not run `git add`, commit, or push; the generated shard is an ordinary reviewable
repository change.

Unpublished rows remain machine-local and can be lost if that checkout or VM is destroyed. This is
an intentional best-effort tradeoff that keeps automatic bookkeeping out of normal Git operations.

## `committed`

This compatibility mode appends directly to `.llm_resource_tally/ledger/ledger.jsonl` and writes
rollups in `.llm_resource_tally/`. It is maximally portable through ordinary clones, but each hook
write dirties the worktree and can obstruct Git operations. New installations should normally use
`local` instead.

## `ignored`

This legacy privacy/local-only mode ignores the installed tool and all generated accounting while
retaining only `.llm_resource_tally/settings.json` as portable policy. It is useful when neither the
tool nor accounting should be committed, but it cannot publish through the normal local spool
workflow without first changing modes.

On a new workstation, run the ordinary bootstrap; it reads `settings.json` and reconstructs the
ignored installation. When converting a committed installation to ignored mode, the installer
stages removal of tracked generated paths and force-retains `settings.json`.

## `notes`

Measured rows are appended to `refs/notes/llm-resource-tally`. Mutable reports and locks live under
the Git common directory, while `settings.json` remains committed. Git notes are not fetched or
pushed by default:

```bash
git push origin refs/notes/llm-resource-tally
git fetch origin refs/notes/llm-resource-tally:refs/notes/llm-resource-tally
```

## Switching modes

Use either offline install or network update:

```bash
<rt> install --storage local
<rt> update --storage notes
```

The explicit choice is persisted to `settings.json`. New writes use the selected destination.
Readers union published worktree shards, the local spool, and the configured notes ref, then
apply the same de-duplication key. A mode change therefore does not hide older locally available
measurements.

Storage conversion does not rewrite historical rows. In particular, an existing committed
`ledger/ledger.jsonl` becomes a stable published input when switching to `local`; new rows go to
`local/ledger.jsonl` until the next explicit publication.
