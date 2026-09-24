# Stable observation allocation

This document describes the stronger allocation path used by backends that can identify one
physical LLM usage observation independently of the session file that contains it. Pi is the first
backend to use it.

The problem is different from ordinary ledger row de-duplication. Pi forks/clones can preserve the
same source usage in more than one session file, sometimes under a new session id. A session
watermark can therefore distinguish files but cannot distinguish a genuinely new model call from a
copied observation.

## Invariants

The implementation is built around five invariants:

1. **The repository ledger is the accounting authority.** A usage observation is durably allocated
   only when a visible ledger row owns its canonical observation id.
2. **Workstation-local state is an index, never a tombstone.** A stale local index entry cannot
   permanently suppress retained transcript work. If its referenced owner row is gone, the entry is
   reclaimed and the observation is billable again.
3. **Copied source observations share identity.** A fork/clone copy must resolve to the same
   canonical id; unrelated calls must not be conflated merely because an agent reused a short entry
   id.
4. **Identity survives source-format migration.** Fields that Pi regenerates while migrating a
   session (notably v1 -> v2 tree `id` / `parentId`) are not part of the canonical fingerprint.
5. **Unknown provenance stays unknown.** Stable identity and measured token counts do not justify
   inventing a model id that the source transcript did not preserve.

## Durable representation

Compact v4 ledger rows may contain:

```text
oi = ["pi-v2:<sha256>", ...]
```

The rich in-memory key is `observation_ids`. These opaque ids are source-observation ownership.
For rows that carry them, the exact owned observation set also participates in ledger-row identity
so a later disjoint recovery for the same commit/session is additive instead of replacing the
earlier row. See [schema-spec.md](schema-spec.md).

Only canonical identities are persisted in the ledger. Compatibility fingerprints are lookup
probes for older ledgers / the legacy claim bridge; weak historical aliases are deliberately not
indexed for every new allocation because doing so could conflate unrelated modern calls.

## Workstation-local index

`~/.llm_resource_tally/observation-claims.sqlite3` (or the `LLM_RESOURCE_TALLY_HOME` equivalent)
is a stdlib-SQLite index containing:

- canonical observation id -> repository path + allocation state;
- compatibility alias -> canonical id;
- imported pre-index claim aliases from the legacy `event-claims.jsonl` format.

The index is not trusted blindly. Before an indexed allocation suppresses a candidate, tally reads
the referenced repository ledger and verifies that the canonical id is still visible there. A
missing owner row makes the index entry stale and the candidate becomes allocatable again.

The current repository is also checked directly before the cross-repository index. This lets tally
relearn ownership after the SQLite index has been deleted while the durable ledger remains. An
existing owner repository whose ledger is temporarily unreadable is treated as *unverifiable*, not
absent: its claim is preserved and suppressed until a later healthy pass can determine ownership.
This fail-closed choice avoids turning a transient parse/Git/storage failure into a permanent
double allocation.

## Allocation protocol

On POSIX, one per-user advisory file lock serializes this sequence:

1. Normalize candidate observations and compatibility aliases.
2. Relearn any ownership already visible in the current repository.
3. Revalidate indexed owners in other known repositories.
4. Insert genuinely fresh candidates in SQLite as `pending` and commit those reservations.
5. Yield the fresh canonical ids to the recorder.
6. The recorder appends ledger rows containing the ids it actually bills.
7. Re-read the current repository ledger:
   - visible ids become `committed` in SQLite;
   - pending ids whose ledger rows did not land are deleted.

Committing the pending reservation before the ledger append is deliberate. If the process dies:

- **before the ledger append:** the next allocator sees a pending owner with no durable row and
  reclaims it;
- **after the ledger append but before finalization:** the next allocator sees the durable row and
  finalizes the pending owner rather than billing it again.

This is not an ACID transaction across Git/repository files and SQLite, but the ledger-verification
rule makes either side recoverable without turning the local index into a second source of truth.

Where `flock` is unavailable or refused by the filesystem, same-machine concurrency protection is
best effort. The SQLite index is also per user/machine; nothing here performs cross-machine global
deduplication.

## Pi canonical identity

Pi canonical observation ids are versioned as `pi-v2:<sha256>`.

The canonical fingerprint excludes Pi entry `id` and `parentId`, because Pi's v1 -> v2 migration
creates those values. It uses stable metadata appropriate to the entry kind, including normalized
usage, timestamps, provider/model evidence when intrinsically recorded, response/tool-call
identity, and opaque hashes of content/summary values where useful. Message or summary plaintext is
never stored in the ledger or observation index.

For compatibility, the backend also emits aliases for:

- the immediately preceding tally fingerprint format; and
- the equivalent pre-migration v1 representation with no generated tree ids.

Aliases allow a previously-accounted observation to remain recognized after upgrading tally or
after Pi migrates an old session file. New ledger rows always own the current canonical identity.

## Legacy `event-claims.jsonl`

The former append-only observation claim log did not identify the exact ledger row that justified a
claim. Treating it as authoritative could permanently undercount: deleting an unpublished local
ledger while retaining the transcript left a claim that prevented `reconcile` from restoring the
usage.

New code never appends that file. If it exists, changed contents are imported as compatibility
aliases. Such an alias suppresses a candidate only when its referenced repository still contains a
compatible visible old-format allocation. Otherwise the legacy claim is discarded as stale.

## Remaining limits

For stable-observation backends, session timestamps are not used as the lower allocation floor.
Every retained observation through the commit-time upper bound is reconsidered and the durable
observation identity decides whether it is already owned. This is what lets an older copied fork
prefix become recoverable if its previous repository owner later disappears, even when the child
session has already billed newer work.

Stable Pi observation identity improves same-machine fork/clone accounting, but it does not make
all totals globally unique:

- another machine has another allocation index;
- deleting local coordination state can reopen cross-repository duplicates until durable owners
  are encountered again;
- repository paths in the workstation index are location hints, not durable repository identities;
  if an owning repository is moved before tally encounters it at the new path, the old missing path
  is indistinguishable from a deleted owner and a retained copy in another repository can become
  allocatable again;
- fleet aggregation currently sums repository-attributed totals and does not yet deduplicate `oi`
  across repositories/machines;
- backends without stable observation ids still use session/transcript timestamp floors;
- `--force` deliberately bypasses automatic duplicate suppression.

These limits are reported as accounting scope, rather than hidden behind a stronger exactly-once
claim.
