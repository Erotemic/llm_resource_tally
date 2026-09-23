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
    """Serialize read/compact/replace so concurrent repositories cannot lose a local claim."""
    os.makedirs(_home(), exist_ok=True)
    with open(claims_path() + ".lock", "a", encoding="utf-8") as fh:
        with exclusive_file_lock(fh):
            yield


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
# Observation claim log — exact-once allocation for backends whose usage records are
# copied VERBATIM into other session files (Pi fork/clone/branch). A *physical* model-usage
# observation (one LLM call) must be billed exactly once, in any repo, no matter how many
# files contain a copy of it. Backends expose this by attaching an optional ``claim_id`` to
# normalized turns and compaction events: a stable OPAQUE identity for the observation,
# distinct from the display ``id``. For Pi it is a digest of the entry's stable non-content
# metadata (entry id, parentId, timestamp, kind, intrinsically recorded provider/model,
# normalized usage counters) — identical for a verbatim copy in a fork, different for two
# unrelated sessions even when they happen to reuse the same short entry id. Only the digest
# is persisted here, never message/prompt/summary text. Advisory, local, never committed;
# I/O failures are swallowed like the rest of the claims log.

def event_claims_path() -> str:
    return os.path.join(_home(), "event-claims.jsonl")


#: Try a shrink-only dedup compaction once the append-only log passes this size.
_EVENT_CLAIMS_MAX_BYTES = 256 * 1024


def _load_event_claims() -> set[tuple[str, str]]:
    """``{(agent, claim-key)}`` already allocated machine-wide.

    New rows carry the opaque ``claim_id`` (an observation fingerprint); legacy rows carry
    ``event_id`` (the pre-fingerprint scheme) and remain readable, though they match no new
    lookup and so only occupy space."""
    out: set = set()
    try:
        fh = open(event_claims_path(), encoding="utf-8")
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
            agent = d.get("agent")
            key = d.get("claim_id") or d.get("event_id")
            if isinstance(agent, str) and isinstance(key, str) and agent and key:
                out.add((agent, key))
    return out


def record_event_claims(agent: str, claim_ids, repo: str) -> None:
    """Mark a set of observation claim keys as allocated by ``repo`` (best effort, no-op
    on failure).

    The check and the append happen under the claims lock, so concurrent recorders on POSIX
    serialize on it (``flock`` is released on process exit, so a crashed writer cannot wedge
    it). On platforms without ``fcntl`` the lock is a no-op and the guard degrades to
    best-effort: two truly simultaneous writers could each allocate the same observation once
    (the shrink-only compaction reconciles the log, but not any ledger rows both emitted).

    Keys already claimed (by this or any other repo) are not re-appended, so the log holds
    one row per distinct ``(agent, claim_id)``."""
    if not agent or not claim_ids:
        return
    keys = [k for k in dict.fromkeys(claim_ids) if isinstance(k, str) and k]
    if not keys:
        return
    try:
        with _claim_lock():
            existing = _load_event_claims()
            fresh = [k for k in keys if (agent, k) not in existing]
            if fresh:
                with open(event_claims_path(), "a", encoding="utf-8") as fh:
                    for k in fresh:
                        fh.write(
                            json.dumps({"agent": agent, "claim_id": k, "repo": _realpath(repo)}) + "\n"
                        )
            if os.path.getsize(event_claims_path()) > _EVENT_CLAIMS_MAX_BYTES:
                _compact_event_claims()
    except OSError:
        return


def _compact_event_claims() -> None:
    """Dedup the log to one row per ``(agent, claim key)`` — but ONLY when that removes rows.

    Most claim keys are unique, so a naive "rewrite while over the size threshold" policy
    would re-rewrite the whole (unshrinkable) file on every append. Here compaction is
    shrink-only: an all-unique log is left byte-for-byte untouched (the read is cheap; the
    rewrite is not), and a full rewrite happens only when a duplicate row is actually present
    (racing or pre-lock writers, a torn compaction), keeping the last row per key with its
    repo."""
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

