# SPDX-License-Identifier: Apache-2.0
"""Local, per-user cross-repo claim log — a best-effort double-count guard.

One agent session can commit into several repos (a fix in repo B from a session running in
repo A). The `--claude` PostToolUse hook records those turns into B's committed ledger, but
A's SessionEnd `reconcile` would then sweep the SAME turns into A's pending bucket, because a
per-repo watermark ([ledger.session_watermark]) can't see across repos. This module keeps a
tiny local log — "(session, repo) has recorded turns up to <ts>" — so `reconcile` can skip
turns another repo already claimed.

It is advisory and never committed: the committed per-repo ledgers remain the source of truth;
this only stops a *local* reconcile from re-counting cross-repo work. Any failure is swallowed
(a missing claim risks at worst a rare local double-count, never a crash or a bad row).
"""
from __future__ import annotations

import json
import os


def _home() -> str:
    """`LLM_RESOURCE_TALLY_HOME` overrides (tests point it at a tmp dir); else
    `~/.llm_resource_tally`. This is per-user local state, not repo data."""
    env = os.environ.get("LLM_RESOURCE_TALLY_HOME")
    return os.path.expanduser(env) if env else os.path.expanduser("~/.llm_resource_tally")


def claims_path() -> str:
    return os.path.join(_home(), "claims.jsonl")


def _load() -> dict:
    """{(session_id, repo): ts_hi} keeping the max ts_hi seen for each key."""
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
            sid, repo, hi = d.get("session_id"), d.get("repo"), d.get("ts_hi")
            if not sid or not repo or not hi:
                continue
            k = (sid, repo)
            if hi > out.get(k, ""):
                out[k] = hi
    return out


def record_claim(session_id: str, repo: str, ts_hi) -> None:
    """Note that `repo` (an absolute repo root) has recorded this session's turns up to
    `ts_hi`. Compacting: rewrite the log keeping only the max ts_hi per (session, repo), so it
    stays bounded by the number of distinct pairs. Best-effort; errors are swallowed."""
    if not session_id or not repo or not ts_hi:
        return
    try:
        claims = _load()
        k = (session_id, repo)
        if ts_hi <= claims.get(k, ""):
            return
        claims[k] = ts_hi
        os.makedirs(_home(), exist_ok=True)
        tmp = claims_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for (s, r), hi in sorted(claims.items()):
                fh.write(json.dumps({"session_id": s, "repo": r, "ts_hi": hi}) + "\n")
        os.replace(tmp, claims_path())
    except OSError:
        return


def claimed_ceiling(session_id: str, current_repo: str) -> str | None:
    """Latest turn timestamp another repo already claimed for this session (None if none).
    `reconcile` skips turns at or below this so cross-repo work isn't swept twice."""
    hi = ""
    for (sid, repo), ts in _load().items():
        if sid == session_id and repo != current_repo and ts > hi:
            hi = ts
    return hi or None
