# SPDX-License-Identifier: Apache-2.0
"""Publish the ignored local JSONL spool as an immutable tracked shard.

Publication is what makes accounting a property of the repository rather than of one
workstation, so it refreshes the tracked rollup and badge alongside the new shard. Both are
derived deterministically from the ledger by `compute_totals`, so they only change when the
underlying measurements do.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile

from .gitutil import repo_root
from .ledger import (
    active_shard,
    ensure_published_layout,
    local_ledger_lock,
    local_shard_paths,
    published_badge_path,
    published_ledger_dir,
    published_totals_path,
    read_ledger,
)
from .rollup import badge_endpoint, compute_totals


def _snapshot(paths: list[str]) -> bytes:
    chunks = []
    for path in paths:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if not data:
            continue
        chunks.append(data if data.endswith(b"\n") else data + b"\n")
    return b"".join(chunks)


def _write_immutable(path: str, payload: bytes) -> bool:
    """Atomically create ``path``; return whether this invocation created it."""
    if os.path.exists(path):
        return False
    parent = os.path.dirname(path)
    fd, temp = tempfile.mkstemp(prefix=".publish.", suffix=".jsonl.tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(path):
            return False
        os.replace(temp, path)
        return True
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass


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


def refresh_published_reports(root: str | None = None) -> list[str]:
    """Rewrite the tracked rollup and badge from the full ledger; return what changed."""
    root = root or repo_root()
    totals = compute_totals(read_ledger(root=root))
    changed = []
    if _write_json(published_totals_path(root), totals, indent=2):
        changed.append(os.path.relpath(published_totals_path(root), root))
    if _write_json(published_badge_path(root), badge_endpoint(totals), indent=None):
        changed.append(os.path.relpath(published_badge_path(root), root))
    return changed


def publish_local(root: str | None = None) -> tuple[str | None, int, bool, list[str]]:
    """Snapshot local rows into a content-addressed shard and clear the local spool.

    The ledger reader de-duplicates overlapping rows by stable row identity, so interruption after
    publication but before cleanup is harmless. The content-addressed destination also makes a
    retry idempotent.
    """
    root = root or repo_root()
    with local_ledger_lock(root):
        paths = local_shard_paths(root)
        payload = _snapshot(paths)
        if not payload:
            return None, 0, False, refresh_published_reports(root)
        ensure_published_layout(root)
        digest = hashlib.sha256(payload).hexdigest()
        path = os.path.join(published_ledger_dir(root), f"ledger.sha256-{digest}.jsonl")
        created = _write_immutable(path, payload)
        _clear_local(paths, root)
        return path, payload.count(b"\n"), created, refresh_published_reports(root)


def cmd_publish(args) -> None:
    root = repo_root()
    path, rows, created, reports = publish_local(root)
    if path is None:
        print("no new local ledger rows to publish.")
    else:
        action = "published" if created else "already published"
        print(f"{action} {rows} local row(s) -> {os.path.relpath(path, root)}")
    for rel in reports:
        print(f"refreshed {rel}")
    if path is None and not reports:
        print("nothing to commit; the tracked ledger and reports are already current.")
        return
    print("stage and commit the tracked shard and reports so the accounting lands in the repo.")
