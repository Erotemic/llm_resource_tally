# SPDX-License-Identifier: Apache-2.0
"""Legacy session/transcript timestamp-floor claims for cross-repository attribution.

Backends without durable per-observation identity use ``claims.jsonl`` to remember how far a
particular session/transcript source was allocated on this workstation. The state is advisory and
per-user/per-machine; repository ledgers remain durable accounting truth.

Backends with stable observation ids use :mod:`llm_resource_tally.observation_allocation` instead.
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
    """Serialize rewrites of the legacy session-floor claim log on this workstation."""
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
