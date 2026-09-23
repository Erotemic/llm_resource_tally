# SPDX-License-Identifier: Apache-2.0
"""Local, per-user cross-repo claim log — a best-effort double-count guard.

One agent session can commit into several repositories. A common example is a commit inside a
submodule followed immediately by a parent commit that advances the gitlink. Repository-local
watermarks cannot see that the first repository already accounted for those observations, so the
second commit would otherwise count the same turns again.

This module keeps a tiny local allocation log —
``(session, transcript-source, repo) has accounted through <ts>``. The transcript source is stored
as a digest rather than a path, so coincidentally reused session ids do not cross-contaminate
unrelated sessions. Both normal ``record`` and trailing ``reconcile`` treat another repository's
matching claim as an attribution floor. New observations after that floor can still be charged to
the next repository; observations at or below it stay with the repository that claimed them first.

The claim log is deliberately advisory and never committed. Repository ledgers remain the durable
source of truth. Claims protect sequential cross-repo work on the same user/machine, but they are
not a globally stable observation identity: deleting the local log, working on another machine, or
manually recording the same observations into multiple repositories can still double-count them.
Claim I/O failures are swallowed so accounting integration never blocks a repository operation.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager

from ._util import compare_timestamps, exclusive_file_lock


def _home() -> str:
    """`LLM_RESOURCE_TALLY_HOME` overrides (tests point it at a tmp dir); else
    `~/.llm_resource_tally`. This is per-user local state, not repo data."""
    env = os.environ.get("LLM_RESOURCE_TALLY_HOME")
    return os.path.expanduser(env) if env else os.path.expanduser("~/.llm_resource_tally")


def claims_path() -> str:
    return os.path.join(_home(), "claims.jsonl")


def _realpath(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


@contextmanager
def _claim_lock():
    """Serialize the per-user claim state (both claim logs live under this one lock) so
    concurrent repositories cannot lose one another's claims.

    Lock order: this lock is always taken BEFORE any per-repo ledger lock and may be held
    across a ledger append (the observation-allocation section of record/reconcile). No
    path ever takes a claims lock while holding a ledger lock, so the order cannot cycle
    into a deadlock. ``flock`` is released on process death, so a crashed writer cannot
    wedge it; where ``fcntl`` is unavailable — or the lock call itself fails — the lock is
    a no-op (best effort).
    """
    os.makedirs(_home(), exist_ok=True)
    with open(claims_path() + ".lock", "a", encoding="utf-8") as fh:
        with exclusive_file_lock(fh):
            yield


def _open_claim_lock() -> object:
    """Open the per-user claims lock file (``flock`` is the serializing point, so the
    lock lives on a dedicated file rather than on either claim log). Raises ``OSError``
    if the home directory is unavailable — callers degrade to the unlocked best-effort
    path."""
    os.makedirs(_home(), exist_ok=True)
    return open(claims_path() + ".lock", "a", encoding="utf-8")


def claim_source_id(agent: str, transcript: str) -> str:
    """Stable local identity for the transcript source without storing its path in the claim log."""
    source = _realpath(transcript)
    material = f"{agent}\0{source}".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(material).hexdigest()[:32]


def _load() -> dict:
    """{(session_id, source_id, repo): ts_hi}, keeping the maximum timestamp per key.

    Legacy rows have no ``source_id`` and remain readable for the compatibility API, but new
    source-scoped allocation deliberately ignores them: a session id alone is not strong enough
    identity to suppress accounting in another repository.
    """
    out: dict = {}
    try:
        fh = open(claims_path(), encoding="utf-8")
    except OSError:
        return out
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid, source, repo, hi = (
                d.get("session_id"),
                d.get("source_id"),
                d.get("repo"),
                d.get("ts_hi"),
            )
            if not sid or not repo or not hi:
                continue
            repo = _realpath(repo)
            k = (sid, source, repo)
            if compare_timestamps(hi, out.get(k, "")) > 0:
                out[k] = hi
    return out


def record_claim(session_id: str, repo: str, ts_hi, source_id: str | None = None) -> None:
    """Record a best-effort local allocation ceiling for one session/source/repository.

    ``source_id`` should normally come from :func:`claim_source_id`. Compacting rewrites the log
    with only the maximum timestamp per identity, so growth is bounded by distinct allocations.
    Errors are swallowed because accounting integration must never block a repository operation.
    """
    if not session_id or not repo or not ts_hi:
        return
    try:
        repo = _realpath(repo)
        with _claim_lock():
            claim_map = _load()
            k = (session_id, source_id, repo)
            if compare_timestamps(ts_hi, claim_map.get(k, "")) <= 0:
                return
            claim_map[k] = ts_hi
            tmp = claims_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for (sid, source, root), hi in sorted(
                    claim_map.items(), key=lambda item: tuple(str(part or "") for part in item[0])
                ):
                    row = {"session_id": sid, "repo": root, "ts_hi": hi}
                    if source:
                        row["source_id"] = source
                    fh.write(json.dumps(row) + "\n")
            os.replace(tmp, claims_path())
    except OSError:
        return


def external_claimed_ceiling(
    session_id: str, current_repo: str, source_id: str | None = None
) -> str | None:
    """Latest matching observation timestamp another repo claimed for this session/source.

    New recording paths always supply ``source_id`` and therefore ignore legacy unscoped claim
    rows. Passing ``None`` retains the older session-id-only behavior for compatibility callers.
    """
    current_repo = _realpath(current_repo)
    hi = ""
    for (sid, source, repo), ts in _load().items():
        if sid != session_id or repo == current_repo:
            continue
        if source_id is not None and source != source_id:
            continue
        if compare_timestamps(ts, hi) > 0:
            hi = ts
    return hi or None


def claimed_ceiling(session_id: str, current_repo: str) -> str | None:
    """Compatibility alias for the pre-source-scoped helper."""
    return external_claimed_ceiling(session_id, current_repo)


# ---------------------------------------------------------------------------
# Observation claim log — a strong same-machine double-count guard for backends whose usage
# records are copied VERBATIM into other session files (Pi fork/clone/branch). A *physical*
# model-usage observation (one LLM call) is allocated at most once by the recorders that
# share this machine's claim log. Backends expose this by attaching an optional ``claim_id``
# to normalized turns and compaction events: a stable OPAQUE identity for the observation,
# distinct from the display ``id``. For Pi it is a digest of the entry's stable non-content
# metadata (entry id, parentId, timestamp, kind, intrinsically recorded provider/model,
# normalized usage counters) — identical for a verbatim copy in a fork, different for two
# unrelated sessions even when they happen to reuse the same short entry id.
#
# Allocation is serialized on POSIX: :func:`allocate_event_claims` holds the per-user claims
# lock across re-reading the log, deciding which candidate observations are still unclaimed,
# the caller's ledger append, and appending the resulting claim rows, so two same-machine
# recorders that race on the same observation cannot both bill it. That is a STRONG
# SAME-MACHINE duplicate guard, not ACID and not global exactly-once: where advisory locking
# is unavailable (no ``fcntl``, or a lock-file failure) the guard is best effort and the log
# stays append-only; the ledger append and the claim append are two separate files that are
# not journaled together (a process that dies between the two writes reopens the window);
# the lock and the log are per-user/per-machine (another workstation has its own log, so
# nothing is synchronized across machines); and deleting or losing the log reopens
# cross-repo duplicates. Only the digest is persisted here, never message/prompt/summary
# text. Advisory, local, never committed; I/O failures are swallowed like the rest of the
# claims log.

def event_claims_path() -> str:
    return os.path.join(_home(), "event-claims.jsonl")


#: Try a shrink-only dedup compaction once the append-only log passes this size.
_EVENT_CLAIMS_MAX_BYTES = 256 * 1024


def _scan_event_claims() -> tuple[set[tuple[str, str]], int]:
    """One pass over ``event-claims.jsonl``: the allocated keys plus a duplicate-row count.

    Returns ``(allocated, dupes)`` where ``allocated`` is the set of ``(agent, claim-key)``
    already allocated machine-wide (new rows carry the opaque ``claim_id``; legacy rows
    carry ``event_id`` (the pre-fingerprint scheme) and remain readable, though they match
    no new lookup and so only occupy space) and ``dupes`` counts physical rows whose key
    already appeared earlier in the file. The count is the cheap signal the compaction
    gate needs: an all-unique log reports zero duplicates and is never re-read for a
    rewrite."""
    out: set = set()
    dupes = 0
    try:
        fh = open(event_claims_path(), encoding="utf-8")
    except OSError:
        return out, dupes
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            agent = d.get("agent")
            key = d.get("claim_id") or d.get("event_id")
            if isinstance(agent, str) and isinstance(key, str) and agent and key:
                if (agent, key) in out:
                    dupes += 1
                out.add((agent, key))
    return out, dupes


def _load_event_claims() -> set[tuple[str, str]]:
    """``{(agent, claim-key)}`` already allocated machine-wide (see
    :func:`_scan_event_claims`)."""
    return _scan_event_claims()[0]


def _append_event_claims(agent: str, claim_keys, repo: str) -> None:
    """Append claim rows for ``claim_keys``. The caller must hold :func:`_claim_lock`; this
    deliberately does not take it — re-acquiring ``flock`` on a second file description
    within the same process would block forever."""
    with open(event_claims_path(), "a", encoding="utf-8") as fh:
        for k in claim_keys:
            fh.write(json.dumps({"agent": agent, "claim_id": k, "repo": _realpath(repo)}) + "\n")


@contextmanager
def allocate_event_claims(agent: str, claim_ids, repo: str):
    """Serialize the allocation of a batch of observations in one locked section.

    Holds the per-user claims lock across (1) re-reading the current claim log, (2)
    deciding which of ``claim_ids`` remain unclaimed, (3) the caller's durable ledger
    append (the with-block body), and (4) appending the resulting claim rows — then
    releases it. Yields the set of claim ids that were still unclaimed at the check:
    the caller bills exactly those (records without a claim key are outside the guard
    and the caller keeps them). On a clean exit from the with-block the fresh keys are
    appended to ``event-claims.jsonl`` and the shrink-only compaction gate runs; if the
    body raises, nothing is recorded — the observations stay allocatable again (they
    may be re-billed, never lost) — and the body's original exception propagates
    unchanged (it is re-raised, never masked or converted).

    Two same-machine recorders racing on one observation therefore serialize on POSIX
    (``flock``): the first bills it, the second re-reads under the lock, finds it
    claimed, and bills nothing for it — one ledger row, one claim row, in any order.
    This is a strong same-machine duplicate guard, not ACID and not global
    exactly-once:

    * Where advisory locking is unavailable — no ``fcntl``, or the lock file cannot
      be locked — the guard is best effort: truly simultaneous writers may each
      allocate the same observation once, and the log stays APPEND-ONLY (a rewrite
      is performed only while the lock is actually effective, because a rewrite
      without the lock could drop a concurrent writer's rows; any duplicate rows a
      best-effort writer leaves are removed by a later compaction on a machine
      where the lock works).
    * The ledger append and the claim append are two separate files that are not
      journaled together: a process that dies after the ledger append but before
      the claim append (or a body that fails part-way through a multi-observation
      batch) reopens the duplicate window for the already-appended rows.
    * The lock and the log are per-user/per-machine: another workstation (or user)
      has its own log, so nothing is synchronized across machines; deleting or
      losing the log reopens cross-repo duplicates.

    The claims lock is always taken before any per-repo ledger lock and is never
    taken while a ledger lock is held, so the two cannot deadlock; and this context
    manager takes it exactly once, so a process never re-acquires its own ``flock``
    (a second acquisition on another file description would block forever).
    I/O failures are swallowed (best effort).
    """
    keys: list[str] = []
    if agent:
        keys = [k for k in dict.fromkeys(claim_ids) if isinstance(k, str) and k]
    if not keys:
        # Fast path: a backend that emits no claim keys never touches the claim log.
        yield set()
        return
    # Take the claims lock in its own try so that ONLY lock acquisition can trigger the
    # unlocked fallback: a caller-body failure raised at the yield below must propagate
    # untouched, never be rerouted into the fallback (which would re-yield and turn the
    # context manager's throw into a RuntimeError).
    fh = None
    try:
        fh = _open_claim_lock()
    except OSError:
        fh = None  # unwritable home: best effort, unlocked
    lock = exclusive_file_lock(fh) if fh is not None else None
    if lock is not None:
        lock.__enter__()  # never raises; lock.acquired says whether it is real
    serialized = lock is not None and lock.acquired
    try:
        # ONE pass: the allocated keys plus a cheap duplicate-row count — everything the
        # compaction gate needs. The second full-file scan (rewrite) is invoked only when
        # that count proves a shrink is actually possible.
        existing, dupes = _scan_event_claims()
        fresh = [k for k in keys if (agent, k) not in existing]
        error = None
        try:
            yield set(fresh)
        except BaseException as exc:
            error = exc  # body failed: record nothing, re-raise below (see finally)
        if error is None and fresh:
            try:
                _append_event_claims(agent, fresh, repo)
            except OSError:
                pass  # unrecorded: the observations stay allocatable, nothing is lost
            if serialized:
                try:
                    over = os.path.getsize(event_claims_path()) > _EVENT_CLAIMS_MAX_BYTES
                except OSError:
                    over = False
                if over and dupes:
                    # A duplicate exists, so a rewrite actually shrinks: the only case
                    # where the second pass runs. An all-unique log (dupes == 0) is
                    # never re-read; with no effective lock there is no rewrite at all.
                    _compact_event_claims()
        if error is not None:
            raise error
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)
            fh.close()


def record_event_claims(agent: str, claim_ids, repo: str) -> None:
    """Mark a set of observation claim keys as allocated by ``repo`` (best effort, no-op
    on failure).

    Standalone entry point (one-off allocation and tests); the ``record``/``reconcile``
    flows use :func:`allocate_event_claims` so the ledger append sits inside the same
    locked section. The check and the append happen under the claims lock, so concurrent
    recorders on POSIX serialize on it (``flock`` is released on process exit, so a
    crashed writer cannot wedge it). On platforms without ``fcntl`` the lock is a no-op
    and the guard degrades to best effort. Keys already claimed (by this or any other
    repo) are not re-appended, so the log holds one row per distinct ``(agent,
    claim_id)``."""
    with allocate_event_claims(agent, claim_ids, repo) as _fresh:
        pass


def _compact_event_claims() -> None:
    """Dedup the log to one row per ``(agent, claim key)``.

    This is the second pass, and it is invoked only when the caller's single scan already
    proved a shrink is possible (:func:`_scan_event_claims` reported duplicate physical
    rows and the log is past the size threshold): a full rewrite keeps the last row per
    key with its repo, covering racing or pre-lock writers and torn compactions. The
    common all-unique case never reaches here and stays a pure append stream."""
    path = event_claims_path()
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("agent") and (d.get("claim_id") or d.get("event_id")):
                    rows.append(d)
    except OSError:
        return
    seen: dict = {}
    for d in rows:
        seen[(d["agent"], d.get("claim_id") or d.get("event_id"))] = d
    if len(seen) >= len(rows):
        return  # nothing to remove: no rewrite
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for d in sorted(
                seen.values(), key=lambda r: (r["agent"], r.get("claim_id") or r.get("event_id"))
            ):
                fh.write(json.dumps(d, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(path + ".tmp")
        except OSError:
            pass


def claimed_claim_ids(agent: str) -> set[str]:
    """Observation claim keys already allocated for this agent, in any repo on this machine.

    A normalized turn or compaction event carrying one of these ``claim_id`` values was
    billed already and must not be billed again from another copy of the same observation;
    records without a ``claim_id`` are allocated by the ordinary session watermarks."""
    return {key for (a, key) in _load_event_claims() if a == agent}

