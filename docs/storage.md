# Ledger storage modes

Storage and durable publication destinations are repository policy, not workstation-local
configuration. The canonical choices live in `.llm_resource_tally/settings.json`: mutable-row
behavior under `installation.storage`, and durable paths under `publication`. Inspect or change
them without reinstalling or downloading the tool:

```bash
<rt> config show
<rt> config set --storage local       # default
<rt> config set --storage committed
<rt> config set --storage ignored
<rt> config set --storage notes
<rt> config set --append-ledger-dir ../tally-data/ledger
<rt> config set --lifetime-totals-path ../tally-data/lifetime-totals.json
```

The publication object is explicit on fresh installs:

```json
{
  "publication": {
    "append_ledger_dir": ".llm_resource_tally/ledger",
    "lifetime_totals_path": ".llm_resource_tally/lifetime-totals.json"
  }
}
```

Relative paths are resolved from the repository root. Absolute paths and `~` are also accepted.
This makes a sibling repository such as `../tally-data/ledger` a natural destination when append-only
accounting history should not dirty the main repository. The two paths are independent, so the
append ledger can move out while lifetime totals remain in the main repository, or both can move.
Changing a publication path does not rewrite historical files. The historical default in-repo
ledger remains a read source after redirection; if you later redirect from one external ledger to
another, move or retain the older external store explicitly if you still want it locally visible.

`install --storage ...` and `update --storage ...` remain supported for compatibility and use the
same transition implementation. `config` changes repository policy; `install` installs or repairs
the executable and integrations; `update` downloads a newer tool. Storage modes are distinct from
recorder backends such as Claude and Codex, which choose which agent transcripts are inspected.

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

`publish` appends the spooled rows to the configured durable ledger and refreshes the configured
lifetime totals. By default those paths are:

```text
.llm_resource_tally/ledger/ledger.jsonl
.llm_resource_tally/lifetime-totals.json
```

Rows are appended to one active shard, which rotates to `ledger/ledger.<UTCstamp>.jsonl` once it
passes `LLM_RESOURCE_TALLY_MAX_LEDGER_BYTES` (1 MB by default) — the same rolling policy the local
spool uses. Publishing happens at every session end and at every agent handoff, so writing a file
per publication would bury the directory in thousands of tiny shards. Rows already present in the
durable shards are skipped, which makes republication a no-op and keeps an interrupted publish from
double-writing. Concurrent branches appending to the same shard are reconciled by the `merge=union`
gitattribute and de-duplicated on read by row identity. For a redirected append directory, the tool
places a scoped `.gitattributes` inside that directory so a separate accounting repository can
retain the same merge behavior without modifying an arbitrary parent directory.

The rollup is recomputed from the whole ledger by the same deterministic pass `rollup` uses, so
it changes only when the underlying measurements do — never a spurious diff. That keeps a clone
able to read totals without anyone holding the local spool.

It then clears the successfully snapshotted local files. Publication is crash tolerant: readers
union local and published rows and de-duplicate them by stable row identity, so overlap after an
interruption cannot double-count. A retry with identical bytes resolves to the same filename.
`publish` does not run `git add`, commit, or push. Commit each generated durable output in the
repository that owns its configured path.

Unpublished rows remain machine-local and can be lost if that checkout or VM is destroyed. This is
an intentional best-effort tradeoff that keeps automatic bookkeeping out of normal Git operations.

## `committed`

This compatibility mode appends directly to the configured durable ledger and writes its rollup
to the configured lifetime-totals path. With the default publication paths it dirties the main
worktree and can obstruct Git operations; redirected paths can instead place those writes elsewhere.
New installations should normally use `local` so automatic recording stays isolated from durable
publication.

## `ignored`

This legacy privacy/local-only mode ignores the installed tool and generated accounting inside the
main repository while retaining only `.llm_resource_tally/settings.json` as portable policy. It is useful when neither the
tool nor accounting should be committed, but it cannot publish through the normal local spool
workflow without first changing modes.

On a new workstation, run the ordinary bootstrap; it reads `settings.json` and reconstructs the
ignored installation. When converting a committed installation to ignored mode, the installer
stages removal of tracked generated paths and force-retains `settings.json`.

## `notes`

Measured rows are appended to `refs/notes/llm-resource-tally`. Mutable reports and locks live under
the Git common directory, while `settings.json` remains committed.

This is an alternative store for *rows*, not an opt-out of publishing. `publish` still refreshes
the configured durable lifetime-totals file, so a clone or sibling accounting repository can carry
the aggregate even without the notes ref; it simply has no JSONL shard to write. Switching to this
mode from `local` moves any
pending spooled rows into the notes ref rather than into a shard.

Git notes are not fetched or pushed by default:

```bash
git push origin refs/notes/llm-resource-tally
git fetch origin refs/notes/llm-resource-tally:refs/notes/llm-resource-tally
```

## Switching modes

Use `config set` for a policy-only transition:

```bash
<rt> config show
<rt> config set --storage local
<rt> config set --storage notes
```

The explicit choice is persisted to `settings.json`. The installed source tree or zipapp is not
replaced, and unrelated Git or Claude hooks are not rewired. Managed `.gitignore` and `AGENTS.md`
regions are refreshed only when their contents depend on the selected storage mode.

New writes use the selected destination.
Readers union published worktree shards, the local spool, and the configured notes ref, then
apply the same de-duplication key. A mode change therefore does not hide older locally available
measurements.

Storage conversion does not rewrite historical rows. In particular, an existing committed
`ledger/ledger.jsonl` becomes a stable published input when switching to `local`; new rows go to
`local/ledger.jsonl` until the next explicit publication. Leaving `local` first drains pending
rows: they move directly into the configured notes ref when selecting `notes`, or are published to
the worktree ledger when selecting `committed` or `ignored`. The local spool is removed only after
that transfer succeeds, so rerunning a failed transition can recover without losing rows.
