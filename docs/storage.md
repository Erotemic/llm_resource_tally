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

`publish` appends the spooled rows to the tracked ledger and refreshes the tracked reports:

```text
.llm_resource_tally/ledger/ledger.jsonl
.llm_resource_tally/lifetime-totals.json
```

Rows are appended to one active shard, which rotates to `ledger/ledger.<UTCstamp>.jsonl` once it
passes `LLM_RESOURCE_TALLY_MAX_LEDGER_BYTES` (1 MB by default) — the same rolling policy the local
spool uses. Publishing happens at every session end and at every agent handoff, so writing a file
per publication would bury the directory in thousands of tiny shards. Rows already present in the
tracked shards are skipped, which makes republication a no-op and keeps an interrupted publish from
double-writing. Concurrent branches appending to the same shard are reconciled by the `merge=union`
gitattribute and de-duplicated on read by row identity.

The rollup is recomputed from the whole ledger by the same deterministic pass `rollup` uses, so
it changes only when the underlying measurements do — never a spurious diff. That keeps a clone
able to read totals without anyone holding the local spool.

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
the Git common directory, while `settings.json` remains committed.

This is an alternative store for *rows*, not an opt-out of publishing. `publish` still refreshes
the tracked `lifetime-totals.json`, so a clone can read the aggregate even without
the notes ref; it simply has no JSONL shard to write. Switching to this mode from `local` moves any
pending spooled rows into the notes ref rather than into a shard.

Git notes are not fetched or pushed by default:

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
