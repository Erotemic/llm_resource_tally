# SPDX-License-Identifier: Apache-2.0
"""Publish the ignored local JSONL spool as an immutable tracked shard."""
from __future__ import annotations

import hashlib
import os
import tempfile

from .gitutil import repo_root
from .ledger import (active_shard, ensure_published_layout, local_ledger_lock,
                     local_shard_paths, published_ledger_dir)


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


def publish_local(root: str | None = None) -> tuple[str | None, int, bool]:
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
            return None, 0, False
        ensure_published_layout(root)
        digest = hashlib.sha256(payload).hexdigest()
        path = os.path.join(published_ledger_dir(root), f"ledger.sha256-{digest}.jsonl")
        created = _write_immutable(path, payload)
        _clear_local(paths, root)
        return path, payload.count(b"\n"), created


def cmd_publish(args) -> None:
    root = repo_root()
    path, rows, created = publish_local(root)
    if path is None:
        print("no local ledger rows to publish.")
        return
    rel = os.path.relpath(path, root)
    action = "published" if created else "already published"
    print(f"{action} {rows} local row(s) -> {rel}")
    print("review and commit the immutable ledger shard when appropriate.")
