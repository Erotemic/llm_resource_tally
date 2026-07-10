# SPDX-License-Identifier: Apache-2.0
"""Thin git helpers. Everything resolves against the current process cwd unless a `cwd`
is passed, so `repo_root()` is the repo a commit lands in — the anchor for the ledger."""
from __future__ import annotations

import os
import subprocess


def git(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def repo_root(cwd: str | None = None) -> str:
    """Innermost git working tree of the cwd — i.e. the repo a commit here lands in.
    (Inside a submodule this is the submodule, which is what makes submodules track
    their own usage separately from the parent.)"""
    return git("rev-parse", "--show-toplevel", cwd=cwd)


def git_common_dir(root: str | None = None) -> str:
    """Absolute git common directory (shared by linked worktrees)."""
    root = root or repo_root()
    path = git("rev-parse", "--git-common-dir", cwd=root)
    return path if os.path.isabs(path) else os.path.abspath(os.path.join(root, path))


def superproject_root() -> str:
    """Parent repo working tree if we are a submodule, else our own toplevel. Used to
    locate the session transcript, which lives under the agent's (usually parent) cwd."""
    sp = git("rev-parse", "--show-superproject-working-tree")
    return sp or repo_root()


def commit_meta(ref: str) -> tuple[str, str]:
    """Return (full_sha, committer_date_iso) for `ref`."""
    sha = git("rev-parse", ref)
    ts = git("show", "-s", "--format=%cI", sha)
    return sha, ts
