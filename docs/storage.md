# Ledger storage modes

`install --storage …` selects where new measured rows are written. The choice is saved in local git config as `llmResourceTally.storage`; re-running `install`
without `--storage` preserves it in that checkout. Local git config is not cloned, so the managed
`AGENTS.md` block records the intended mode and tells a fresh clone which explicit install command
to run.

## `committed` (default)

```bash
<rt> install --storage committed
```

Ledger shards, settings, and generated reports live in `.llm_resource_tally/`. This is the
original behavior: the ledger can travel with ordinary commits and uses `merge=union`.

## `ignored`

```bash
<rt> install --storage ignored
```

The same local layout is used, but `install` maintains a sentinel-delimited root
`.gitignore` block for `.llm_resource_tally/` (and a custom tool directory, when needed).
This is useful when accounting should be local and must never alter commits. A fresh clone
will not contain an ignored vendored tool, so rerun the bootstrap with `RT_STORAGE=ignored`.
Switching back to `committed` or `notes` removes only the managed ignore block.

`.gitignore` does not untrack files already present in the index. When converting an existing
committed install, review the transition and remove the old paths from the index explicitly
(e.g. `git rm -r --cached .llm_resource_tally`) if you want them to become local-only; the
installer warns but does not perform this destructive step automatically.

## `notes`

```bash
<rt> install --storage notes
```

Compact measured rows are appended to:

```text
refs/notes/llm-resource-tally
```

Settings, locks, rollups, and model reports live under the repository's git common
directory (`.git/llm-resource-tally/` in a normal checkout). A post-commit record changes
the notes ref but not the worktree or commit tree.

Git notes are refs and are not fetched or pushed by default. Share them explicitly:

```bash
git push origin refs/notes/llm-resource-tally
git fetch origin refs/notes/llm-resource-tally:refs/notes/llm-resource-tally
```

If multiple writers update the notes ref independently, merge it with the normal git-notes
workflow before pushing. Local appends are serialized with an `flock`.

## Switching modes

Readers union worktree shards and the configured notes ref, then de-duplicate by observation
identity. Changing modes therefore affects new writes without hiding old measurements. No
automatic deletion or migration is performed.

The tool code and the ledger are separate concerns in notes mode: a vendored or submodule
copy may still be committed while measured rows live only in notes.
