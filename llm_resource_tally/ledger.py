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
from contextlib import contextmanager

from ._util import now_iso, now_stamp, span_seconds
from .gitutil import git, repo_root
from .schema import COMPACTION_KIND, SCHEMA, TOKEN_KEYS, decode_row, encode_row
from .storage import (
    data_dir as selected_data_dir,
    local_data_dir,
    local_state_dir,
    notes_ref,
    published_ledger_dir as configured_published_ledger_dir,
    published_totals_path as configured_published_totals_path,
    storage_mode,
    worktree_data_dir,
)

# Rotate a shard once it passes this size so no single JSONL file grows without bound.
MAX_LEDGER_BYTES = int(os.environ.get("LLM_RESOURCE_TALLY_MAX_LEDGER_BYTES", str(1_000_000)))


def data_dir(root: str | None = None) -> str:
    """Selected mutable state directory for ``root`` (worktree except in notes mode)."""
    return selected_data_dir(root)


def published_ledger_dir(root: str | None = None) -> str:
    return configured_published_ledger_dir(root)


def local_ledger_dir(root: str | None = None) -> str:
    return local_data_dir(root)


def ledger_dir(root: str | None = None) -> str:
    """Directory receiving new file rows for the selected storage mode."""
    return local_ledger_dir(root) if storage_mode(root) == "local" else published_ledger_dir(root)


def active_shard(root: str | None = None) -> str:
    return os.path.join(ledger_dir(root), "ledger.jsonl")


def totals_path(root: str | None = None) -> str:
    """Working rollup for the selected mode; durable file modes honor publication settings."""
    mode = storage_mode(root)
    if mode in ("local", "notes"):
        return os.path.join(data_dir(root), "lifetime-totals.json")
    return published_totals_path(root)


def published_totals_path(root: str | None = None) -> str:
    """Durable rollup refreshed by `publish`; destination is repository-configurable."""
    return configured_published_totals_path(root)


def shard_paths_in(dd: str) -> list[str]:
    """Published/legacy file shards beneath a ``.llm_resource_tally`` directory."""
    paths = sorted(glob.glob(os.path.join(dd, "ledger", "*.jsonl")))
    legacy = os.path.join(dd, "resource-ledger.jsonl")
    if os.path.exists(legacy):
        paths = [legacy] + paths
    return paths


def shard_paths_in_ledger_dir(directory: str) -> list[str]:
    return sorted(glob.glob(os.path.join(directory, "*.jsonl")))


def published_shard_paths(root: str | None = None) -> list[str]:
    """Durable file shards, including the historical default after redirection."""
    root = root or repo_root()
    paths = shard_paths_in(worktree_data_dir(root))
    configured = published_ledger_dir(root)
    default = os.path.join(worktree_data_dir(root), "ledger")
    if os.path.normcase(os.path.abspath(configured)) != os.path.normcase(os.path.abspath(default)):
        paths.extend(shard_paths_in_ledger_dir(configured))
    return list(dict.fromkeys(paths))


def shard_paths(root: str | None = None) -> list[str]:
    """All locally visible file shards, durable sources first and local spool second.

    The historical worktree ledger remains a read source even after publication is redirected,
    so changing the destination does not make already-committed measurements disappear.
    """
    root = root or repo_root()
    paths = published_shard_paths(root)
    paths.extend(sorted(glob.glob(os.path.join(local_ledger_dir(root), "ledger*.jsonl"))))
    return list(dict.fromkeys(paths))


def local_shard_paths(root: str | None = None) -> list[str]:
    return sorted(glob.glob(os.path.join(local_ledger_dir(root), "ledger*.jsonl")))


def _ensure_merge_attribute(path: str, pattern: str) -> None:
    line = f"{pattern} merge=union"
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    if any(raw.strip() == line for raw in text.splitlines()):
        return
    prefix = "" if not text else ("" if text.endswith("\n") else "\n")
    comment = "# append-only ledger shards: keep rows from both sides on merge/rebase.\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(prefix + comment + line + "\n")


def ensure_published_layout(root: str | None = None) -> str:
    """Create the configured durable ledger directory and its merge policy when needed."""
    root = root or repo_root()
    directory = published_ledger_dir(root)
    os.makedirs(directory, exist_ok=True)
    default = os.path.join(worktree_data_dir(root), "ledger")
    if os.path.normcase(os.path.abspath(directory)) == os.path.normcase(os.path.abspath(default)):
        _ensure_merge_attribute(os.path.join(worktree_data_dir(root), ".gitattributes"), "ledger/*.jsonl")
    else:
        # Keep external publication self-contained: placing the attribute file inside the ledger
        # directory scopes the union driver to these shards without modifying an arbitrary parent.
        _ensure_merge_attribute(os.path.join(directory, ".gitattributes"), "*.jsonl")
    return directory


def ensure_data_dir(root: str | None = None) -> str:
    root = root or repo_root()
    if storage_mode(root) == "notes":
        os.makedirs(local_state_dir(root), exist_ok=True)
        return data_dir(root)
    os.makedirs(ledger_dir(root), exist_ok=True)
    if storage_mode(root) != "local":
        ensure_published_layout(root)
    return data_dir(root)


def row_identity(r: dict):
    """The stable key readers de-duplicate on; also lets `publish` skip already-published rows."""
    sid = r.get("session_id")
    agent = r.get("agent") or "unknown"
    if r.get("kind") == COMPACTION_KIND:
        return ("compaction", agent, sid, r.get("boundary_ts"))
    commit = r.get("commit")
    if isinstance(commit, str) and commit.startswith("pending@"):
        rng = r.get("turn_ts_range") or [None, None]
        return ("measured", agent, sid, commit, rng[1])
    return ("measured", agent, sid, commit)


_row_identity = row_identity  # retained for older in-tree callers


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


def _rotate_dir(directory: str) -> str | None:
    """Retire ``directory``'s active shard once it is large enough; return the archived name."""
    active = os.path.join(directory, "ledger.jsonl")
    try:
        if os.path.getsize(active) < MAX_LEDGER_BYTES:
            return None
        arch = os.path.join(directory, f"ledger.{now_stamp()}.jsonl")
        if os.path.exists(arch):
            return None
        os.replace(active, arch)
        return os.path.basename(arch)
    except OSError:
        return None


def _maybe_rotate(root: str | None = None) -> None:
    _rotate_dir(ledger_dir(root))


def published_active_shard(root: str | None = None) -> str:
    """The one tracked shard `publish` appends to; rotated by size like the local spool."""
    return os.path.join(published_ledger_dir(root), "ledger.jsonl")


def maybe_rotate_published(root: str | None = None) -> str | None:
    return _rotate_dir(published_ledger_dir(root))


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


@contextmanager
def local_ledger_lock(root: str | None = None):
    """Serialize local-spool appends, rotation, and publication."""
    root = root or repo_root()
    os.makedirs(local_ledger_dir(root), exist_ok=True)
    path = os.path.join(local_ledger_dir(root), "ledger.lock")
    with open(path, "a", encoding="utf-8") as fh:
        _lock(fh)
        try:
            yield
        finally:
            _unlock(fh)


def _note_target(row: dict, root: str) -> str:
    commit = str(row.get("commit") or "")
    if commit and not commit.startswith("pending@"):
        try:
            return git("rev-parse", "--verify", f"{commit}^{{commit}}", cwd=root)
        except subprocess.CalledProcessError:
            pass
    return git("rev-parse", "HEAD", cwd=root)


def append_note(line: str, row: dict, root: str) -> None:
    # The notes state dir is created explicitly rather than via ensure_data_dir: rows can be
    # drained into notes while the policy still reads `local`, which would prepare a different dir.
    ensure_data_dir(root)
    os.makedirs(local_state_dir(root), exist_ok=True)
    lock_path = os.path.join(local_state_dir(root), "notes.lock")
    with open(lock_path, "a", encoding="utf-8") as lock:
        _lock(lock)
        git("notes", f"--ref={notes_ref(root)}", "append", "-m", line, _note_target(row, root), cwd=root)
        _unlock(lock)


def append_row(row: dict) -> None:
    root = repo_root()
    line = json.dumps(encode_row(row), separators=(",", ":"), ensure_ascii=False)
    if storage_mode(root) == "notes":
        append_note(line, row, root)
        return
    ensure_data_dir(root)
    if storage_mode(root) == "local":
        with local_ledger_lock(root):
            _maybe_rotate(root)
            with open(active_shard(root), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return
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
    return {
        r.get("boundary_ts")
        for r in rows
        if r.get("session_id") == session_id and r.get("kind") == COMPACTION_KIND
    }


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
            "billable_input": (
                tok["input_tokens"] + tok["cache_creation_input_tokens"] + tok["cache_read_input_tokens"]
            ),
        },
        "by_model": {
            m: {
                "input": v["input_tokens"],
                "cache_write": v["cache_creation_input_tokens"],
                "cache_read": v["cache_read_input_tokens"],
                "output": v["output_tokens"],
            }
            for m, v in by_model.items()
        },
        "server_tools": {"web_search": web_search, "web_fetch": web_fetch},
        "time": {"wall_clock_s": span_seconds(ts_lo, ts_hi)},
        "turn_ts_range": [ts_lo, ts_hi],
    }


def base_row(sha: str, commit_ts, session_id: str, activity, repo: str, agent: str) -> dict:
    """Common identity/provenance fields shared by measured and compaction rows. `agent`
    is the backend that produced the row (e.g. "claude-code", "codex") so a repo can mix
    backends in one ledger."""
    return {
        "schema": SCHEMA,
        "recorded_at": now_iso(),
        "repo": repo,
        "commit": sha,
        "commit_ts": commit_ts,
        "agent": agent,
        "activity": activity,
        "session_id": session_id,
    }


def compaction_row(ev: dict, sha: str, commit_ts, session_id: str, activity, repo: str, agent: str) -> dict:
    """A compaction row records only MEASURED signals — no fabricated token counts."""
    row = base_row(sha, commit_ts, session_id, activity, repo, agent)
    row.update(
        kind=COMPACTION_KIND,
        source="reconstructed",
        boundary_ts=ev["boundary_ts"],
        models=[ev["model"]],
        compaction={"peak_context_tokens": ev["peak_context_tokens"], "summary_chars": ev["summary_chars"]},
    )
    return row
