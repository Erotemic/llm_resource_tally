# SPDX-License-Identifier: Apache-2.0
"""Publish the ignored local JSONL spool into the configured durable append-only ledger.

Rows are appended to one active shard, which rotates by size exactly like the local spool, rather
than minting a file per publication. Publishing happens at every session end and whenever an agent
hands off work, so a file per call would bury the ledger directory in thousands of tiny shards.
Concurrent branches appending to the same shard are reconciled by the `merge=union` gitattribute
and de-duplicated on read by row identity.

Publication is what makes accounting durable rather than a property of one workstation, so it
also refreshes the configured lifetime-total rollup. It is derived deterministically from the ledger
by `compute_totals`, so it only changes when the underlying measurements do.
"""

from __future__ import annotations

import json
import os
import tempfile

from ._util import compare_timestamps
from .gitutil import repo_root
from .ledger import (
    active_shard,
    ensure_published_layout,
    local_ledger_lock,
    local_shard_paths,
    maybe_rotate_published,
    published_active_shard,
    published_ledger_lock,
    published_shard_paths,
    published_totals_path,
    read_ledger,
    row_identity,
)
from .ledger import append_note, notes_rows
from .rollup import compute_totals
from .schema import decode_row
from .storage import storage_mode


def _spool_lines(paths: list[str]) -> list[tuple[str, dict]]:
    """Every spooled line with its decoded row; malformed accounting fails closed."""
    out = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append((line, decode_row(json.loads(line))))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{lineno}: invalid local ledger row: {exc}") from exc
    return out


def _already_published(root: str) -> dict:
    """Newest ``recorded_at`` already in durable shards, keyed by row identity."""
    seen: dict = {}
    for path in published_shard_paths(root):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = decode_row(json.loads(line))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{lineno}: invalid durable ledger row: {exc}") from exc
            key = row_identity(row)
            stamp = row.get("recorded_at") or ""
            if compare_timestamps(stamp, seen.get(key, "")) >= 0:
                seen[key] = stamp
    return seen


def _append_published(root: str, lines: list[str]) -> str:
    """Append to the durable active shard, rotating it first if it has grown past the limit."""
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
            if compare_timestamps(
                row.get("recorded_at"), seen.get(row_identity(row), "")
            ) <= 0:
                continue
            append_note(line, row, root)
            moved += 1
        _clear_local(paths, root)
        return moved


def _refresh_published_reports_unlocked(root: str) -> list[str]:
    totals = compute_totals(read_ledger(root=root))
    changed = []
    totals_path = published_totals_path(root)
    os.makedirs(os.path.dirname(totals_path), exist_ok=True)
    if _write_json(totals_path, totals, indent=2):
        changed.append(os.path.relpath(totals_path, root))
    return changed


def refresh_published_reports(root: str | None = None) -> list[str]:
    """Rewrite the durable rollup while holding the destination-scoped publication lock."""
    root = root or repo_root()
    with published_ledger_lock(root):
        return _refresh_published_reports_unlocked(root)


def publish_local(root: str | None = None) -> tuple[str | None, int, int, list[str]]:
    """Append local rows to the configured durable ledger and clear the spool.

    Rows already present in durable shards are skipped, so an interruption between appending
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
        with published_ledger_lock(root):
            published = _already_published(root)
            fresh = [
                line
                for line, row in spooled
                if compare_timestamps(
                    row.get("recorded_at"), published.get(row_identity(row), "")
                ) > 0
            ]
            path = _append_published(root, fresh) if fresh else None
            _clear_local(paths, root)
            reports = _refresh_published_reports_unlocked(root)
        return path, len(fresh), len(spooled) - len(fresh), reports


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
        print("nothing to publish; the durable accounting is already current.")
        return
    what = "lifetime totals" if storage_mode(root) == "notes" else "ledger and lifetime totals"
    print(f"commit the durable {what} in whichever repository owns each configured path.")
