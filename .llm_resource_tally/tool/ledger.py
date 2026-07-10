# SPDX-License-Identifier: Apache-2.0
"""The ledger: append-only, rolling JSONL shards of MEASURED usage, plus the readers
that de-duplicate them. Stores measurements only — inference-time, energy, and carbon
are derived post-hoc from these rows, so any modeling knob can change without re-recording.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess

from ._util import now_iso, now_stamp, span_seconds
from .gitutil import git, repo_root
from .schema import COMPACTION_KIND, SCHEMA, TOKEN_KEYS, decode_row, encode_row
from .storage import (data_dir as selected_data_dir, local_state_dir, notes_ref,
                      storage_mode, worktree_data_dir)

# Rotate a shard once it passes this size so no single JSONL file grows without bound.
MAX_LEDGER_BYTES = int(os.environ.get("LLM_RESOURCE_TALLY_MAX_LEDGER_BYTES", str(1_000_000)))


def data_dir(root: str | None = None) -> str:
    """Selected mutable state directory for ``root`` (worktree except in notes mode)."""
    return selected_data_dir(root)


def ledger_dir(root: str | None = None) -> str:
    return os.path.join(worktree_data_dir(root), "ledger")


def active_shard(root: str | None = None) -> str:
    return os.path.join(ledger_dir(root), "ledger.jsonl")


def totals_path(root: str | None = None) -> str:
    return os.path.join(data_dir(root), "lifetime-totals.json")


def badge_path(root: str | None = None) -> str:
    return os.path.join(data_dir(root), "badge.json")


def shard_paths_in(dd: str) -> list[str]:
    paths = sorted(glob.glob(os.path.join(dd, "ledger", "*.jsonl")))
    legacy = os.path.join(dd, "resource-ledger.jsonl")
    if os.path.exists(legacy):
        paths = [legacy] + paths
    return paths


def shard_paths(root: str | None = None) -> list[str]:
    return shard_paths_in(worktree_data_dir(root))


def ensure_data_dir(root: str | None = None) -> str:
    root = root or repo_root()
    if storage_mode(root) == "notes":
        os.makedirs(local_state_dir(root), exist_ok=True)
        return data_dir(root)
    os.makedirs(ledger_dir(root), exist_ok=True)
    ga = os.path.join(worktree_data_dir(root), ".gitattributes")
    if not os.path.exists(ga):
        with open(ga, "w", encoding="utf-8") as fh:
            fh.write("# append-only ledger shards: keep rows from both sides on "
                     "merge/rebase;\n# readers de-duplicate by row identity.\n"
                     "ledger/*.jsonl merge=union\n")
    return data_dir(root)


def _row_identity(r: dict):
    sid = r.get("session_id")
    agent = r.get("agent") or "unknown"
    if r.get("kind") == COMPACTION_KIND:
        return ("compaction", agent, sid, r.get("boundary_ts"))
    commit = r.get("commit")
    if isinstance(commit, str) and commit.startswith("pending@"):
        rng = r.get("turn_ts_range") or [None, None]
        return ("measured", agent, sid, commit, rng[1])
    return ("measured", agent, sid, commit)


def _parse_json_lines(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield decode_row(json.loads(line))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue


def _file_rows(paths: list[str]):
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                yield from _parse_json_lines(fh.read())
        except OSError:
            continue


def notes_rows(root: str | None = None) -> list[dict]:
    """Rows currently reachable from the configured notes ref for ``root``."""
    root = root or repo_root()
    try:
        listing = git("notes", f"--ref={notes_ref(root)}", "list", cwd=root)
    except subprocess.CalledProcessError:
        return []
    rows = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            text = git("notes", f"--ref={notes_ref(root)}", "show", parts[1], cwd=root)
        except subprocess.CalledProcessError:
            continue
        rows.extend(_parse_json_lines(text))
    return rows


def read_ledger(shards: list[str] | None = None, root: str | None = None) -> list[dict]:
    """Latest-wins rich-row view.

    Explicit ``shards`` preserves the historical file-only API. Otherwise readers union the
    worktree ledger and git notes so a storage-mode change does not hide earlier observations.
    """
    root = root or repo_root()
    source_rows = list(_file_rows(shard_paths(root) if shards is None else shards))
    if shards is None:
        source_rows.extend(notes_rows(root))
    order: list = []
    best: dict = {}
    for row in source_rows:
        key = _row_identity(row)
        if key not in best:
            order.append(key)
        cur = best.get(key)
        if cur is None or (row.get("recorded_at") or "") >= (cur.get("recorded_at") or ""):
            best[key] = row
    return [best[key] for key in order]


def _maybe_rotate(root: str | None = None) -> None:
    active = active_shard(root)
    try:
        if os.path.getsize(active) >= MAX_LEDGER_BYTES:
            arch = os.path.join(ledger_dir(root), f"ledger.{now_stamp()}.jsonl")
            if not os.path.exists(arch):
                os.replace(active, arch)
    except OSError:
        pass


def _lock(fh) -> None:
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX)
    except (ImportError, OSError):
        pass


def _unlock(fh) -> None:
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


def _note_target(row: dict, root: str) -> str:
    commit = str(row.get("commit") or "")
    if commit and not commit.startswith("pending@"):
        try:
            return git("rev-parse", "--verify", f"{commit}^{{commit}}", cwd=root)
        except subprocess.CalledProcessError:
            pass
    return git("rev-parse", "HEAD", cwd=root)


def _append_note(line: str, row: dict, root: str) -> None:
    ensure_data_dir(root)
    lock_path = os.path.join(local_state_dir(root), "notes.lock")
    with open(lock_path, "a", encoding="utf-8") as lock:
        _lock(lock)
        git("notes", f"--ref={notes_ref(root)}", "append", "-m", line,
            _note_target(row, root), cwd=root)
        _unlock(lock)


def append_row(row: dict) -> None:
    root = repo_root()
    line = json.dumps(encode_row(row), separators=(",", ":"), ensure_ascii=False)
    if storage_mode(root) == "notes":
        _append_note(line, row, root)
        return
    ensure_data_dir(root)
    _maybe_rotate(root)
    with open(active_shard(root), "a", encoding="utf-8") as fh:
        _lock(fh)
        fh.write(line + "\n")
        _unlock(fh)


def session_watermark(rows: list[dict], session_id: str) -> str:
    """Max turn timestamp already recorded for this session ('' if none)."""
    hi = ""
    for r in rows:
        if r.get("session_id") == session_id:
            rng = r.get("turn_ts_range") or [None, None]
            if rng[1] and rng[1] > hi:
                hi = rng[1]
    return hi


def recorded_boundary_ts(rows: list[dict], session_id: str) -> set:
    """Boundary timestamps of compaction estimates already recorded for a session
    (dedup key so re-running never double-counts a compaction event)."""
    return {r.get("boundary_ts") for r in rows
            if r.get("session_id") == session_id and r.get("kind") == COMPACTION_KIND}


def aggregate(turns: list[dict]) -> dict:
    """Sum the MEASURED usage of a set of turns. Wall-clock and the timestamp range are
    measured; inference-time/energy/carbon are derived post-hoc."""
    tok = {k: 0 for k in TOKEN_KEYS}
    by_model: dict[str, dict] = {}
    web_search = web_fetch = 0
    for t in turns:
        for k in TOKEN_KEYS:
            tok[k] += t["usage"][k]
        bm = by_model.setdefault(t["model"], {k: 0 for k in TOKEN_KEYS})
        for k in TOKEN_KEYS:
            bm[k] += t["usage"][k]
        web_search += t["web_search"]
        web_fetch += t["web_fetch"]
    ts_lo = turns[0]["ts"] if turns else None
    ts_hi = turns[-1]["ts"] if turns else None
    return {
        "turns": len(turns),
        "models": sorted(by_model),
        "tokens": {
            "input": tok["input_tokens"],
            "cache_write": tok["cache_creation_input_tokens"],
            "cache_read": tok["cache_read_input_tokens"],
            "output": tok["output_tokens"],
            "billable_input": (tok["input_tokens"]
                               + tok["cache_creation_input_tokens"]
                               + tok["cache_read_input_tokens"]),
        },
        "by_model": {m: {"input": v["input_tokens"],
                         "cache_write": v["cache_creation_input_tokens"],
                         "cache_read": v["cache_read_input_tokens"],
                         "output": v["output_tokens"]}
                     for m, v in by_model.items()},
        "server_tools": {"web_search": web_search, "web_fetch": web_fetch},
        "time": {"wall_clock_s": span_seconds(ts_lo, ts_hi)},
        "turn_ts_range": [ts_lo, ts_hi],
    }


def base_row(sha: str, commit_ts, session_id: str, activity, repo: str,
             agent: str) -> dict:
    """Common identity/provenance fields shared by measured and compaction rows. `agent`
    is the backend that produced the row (e.g. "claude-code", "codex") so a repo can mix
    backends in one ledger."""
    return {"schema": SCHEMA, "recorded_at": now_iso(), "repo": repo,
            "commit": sha, "commit_ts": commit_ts, "agent": agent,
            "activity": activity, "session_id": session_id}


def compaction_row(ev: dict, sha: str, commit_ts, session_id: str,
                   activity, repo: str, agent: str) -> dict:
    """A compaction row records only MEASURED signals — no fabricated token counts."""
    row = base_row(sha, commit_ts, session_id, activity, repo, agent)
    row.update(kind=COMPACTION_KIND, source="reconstructed",
               boundary_ts=ev["boundary_ts"], models=[ev["model"]],
               compaction={"peak_context_tokens": ev["peak_context_tokens"],
                           "summary_chars": ev["summary_chars"]})
    return row
