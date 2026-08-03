# SPDX-License-Identifier: Apache-2.0
"""Publish the ignored local JSONL spool into the tracked append-only ledger.

Rows are appended to one active shard, which rotates by size exactly like the local spool, rather
than minting a file per publication. Publishing happens at every session end and whenever an agent
hands off work, so a file per call would bury the ledger directory in thousands of tiny shards.
Concurrent branches appending to the same shard are reconciled by the `merge=union` gitattribute
and de-duplicated on read by row identity.

Publication is what makes accounting a property of the repository rather than of one workstation,
so it also refreshes the tracked rollup. It is derived deterministically from the ledger by
`compute_totals`, so it only changes when the underlying measurements do.
"""

from __future__ import annotations

import json
import os
import tempfile

from .gitutil import repo_root
from .ledger import (
    active_shard,
    ensure_published_layout,
    local_ledger_lock,
    local_shard_paths,
    maybe_rotate_published,
    published_active_shard,
    published_totals_path,
    read_ledger,
    row_identity,
    shard_paths_in,
)
from .ledger import append_note, notes_rows
from .rollup import compute_totals
from .schema import decode_row
from .storage import storage_mode, worktree_data_dir


def _spool_lines(paths: list[str]) -> list[tuple[str, dict | None]]:
    """Every spooled line with its decoded row; undecodable lines are carried through verbatim."""
    out = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append((line, decode_row(json.loads(line))))
            except (json.JSONDecodeError, TypeError, ValueError):
                out.append((line, None))
    return out


def _already_published(root: str) -> dict:
    """Newest ``recorded_at`` already in the tracked shards, keyed by row identity."""
    seen: dict = {}
    for path in shard_paths_in(worktree_data_dir(root)):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = decode_row(json.loads(line))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            key = row_identity(row)
            stamp = row.get("recorded_at") or ""
            if stamp >= seen.get(key, ""):
                seen[key] = stamp
    return seen


def _append_published(root: str, lines: list[str]) -> str:
    """Append to the tracked active shard, rotating it first if it has grown past the limit."""
    maybe_rotate_published(root)
    path = published_active_shard(root)
    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


def _clear_local(paths: list[str], root: str) -> None:
    active = active_shard(root)
    for path in paths:
        try:
            if os.path.normpath(path) == os.path.normpath(active):
                with open(path, "w", encoding="utf-8"):
                    pass
            else:
                os.remove(path)
        except OSError:
            pass


def _write_json(path: str, payload: dict, indent: int | None) -> bool:
    """Atomically rewrite ``path``; return whether the bytes actually changed."""
    text = json.dumps(payload, indent=indent, ensure_ascii=False) + "\n"
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return False
    except OSError:
        pass
    fd, temp = tempfile.mkstemp(prefix=".publish.", suffix=".json.tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(temp, path)
        return True
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass


def drain_spool_to_notes(root: str | None = None) -> int:
    """Move spooled rows into the notes ref instead of a JSONL shard, and clear the spool.

    Notes mode is an alternative row store, not an alternative to publishing: the aggregate is
    still published as tracked files. Only the rows change destination.
    """
    root = root or repo_root()
    with local_ledger_lock(root):
        paths = local_shard_paths(root)
        spooled = _spool_lines(paths)
        if not spooled:
            return 0
        seen = {row_identity(r): (r.get("recorded_at") or "") for r in notes_rows(root)}
        moved = 0
        for line, row in spooled:
            if row is not None and (row.get("recorded_at") or "") <= seen.get(row_identity(row), ""):
                continue
            append_note(line, row or {}, root)
            moved += 1
        _clear_local(paths, root)
        return moved


def refresh_published_reports(root: str | None = None) -> list[str]:
    """Rewrite the tracked rollup from the full ledger; return what changed.

    This runs in every storage mode, notes included — where rows live is a separate question from
    whether the repository carries a readable aggregate.
    """
    root = root or repo_root()
    totals = compute_totals(read_ledger(root=root))
    changed = []
    if _write_json(published_totals_path(root), totals, indent=2):
        changed.append(os.path.relpath(published_totals_path(root), root))
    return changed


def publish_local(root: str | None = None) -> tuple[str | None, int, int, list[str]]:
    """Append local rows to the tracked ledger and clear the spool.

    Rows already present in the tracked shards are skipped, so an interruption between appending
    and clearing the spool cannot double-write them on the next run. A row whose identity is
    already published but carries a newer ``recorded_at`` is appended, preserving the reader's
    latest-wins semantics for a re-recorded commit.
    """
    root = root or repo_root()
    with local_ledger_lock(root):
        paths = local_shard_paths(root)
        spooled = _spool_lines(paths)
        if not spooled:
            return None, 0, 0, refresh_published_reports(root)
        ensure_published_layout(root)
        published = _already_published(root)
        fresh = [
            line
            for line, row in spooled
            if row is None or (row.get("recorded_at") or "") > published.get(row_identity(row), "")
        ]
        path = _append_published(root, fresh) if fresh else None
        _clear_local(paths, root)
        return path, len(fresh), len(spooled) - len(fresh), refresh_published_reports(root)


def cmd_publish(args) -> None:
    root = repo_root()
    path, appended, skipped, reports = publish_local(root)
    if path is not None:
        print(f"appended {appended} row(s) -> {os.path.relpath(path, root)}")
    elif skipped:
        print(f"no new rows: all {skipped} local row(s) were already published.")
    for rel in reports:
        print(f"refreshed {rel}")
    if path is None and not skipped and not reports:
        print("nothing to publish; the tracked accounting is already current.")
        return
    what = "reports" if storage_mode(root) == "notes" else "ledger and reports"
    print(f"stage and commit the tracked {what} so the accounting lands in the repo.")
