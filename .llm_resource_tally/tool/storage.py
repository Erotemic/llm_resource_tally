# SPDX-License-Identifier: Apache-2.0
"""Ledger/state storage selection.

``committed`` keeps the historical worktree layout. ``ignored`` uses the same layout but
maintains a root ``.gitignore`` block. ``notes`` stores measured rows in
``refs/notes/llm-resource-tally`` and keeps mutable settings/reports below the repository's git
common directory. Readers union worktree shards and notes so changing modes never hides older
measurements.
"""
from __future__ import annotations

import os
import subprocess

from .gitutil import git, git_common_dir, repo_root

STORAGE_MODES = ("committed", "ignored", "notes")
DEFAULT_STORAGE = "committed"
DEFAULT_NOTES_REF = "refs/notes/llm-resource-tally"


def _config_get(key: str, root: str) -> str:
    try:
        return git("config", "--local", "--get", key, cwd=root)
    except subprocess.CalledProcessError:
        return ""


def storage_mode(root: str | None = None) -> str:
    root = root or repo_root()
    value = os.environ.get("LLM_RESOURCE_TALLY_STORAGE") or _config_get(
        "llmResourceTally.storage", root)
    return value if value in STORAGE_MODES else DEFAULT_STORAGE


def set_storage_mode(mode: str, root: str | None = None) -> str:
    if mode not in STORAGE_MODES:
        raise ValueError(f"unknown storage mode {mode!r}; choose from {', '.join(STORAGE_MODES)}")
    root = root or repo_root()
    git("config", "--local", "llmResourceTally.storage", mode, cwd=root)
    return mode


def notes_ref(root: str | None = None) -> str:
    root = root or repo_root()
    return (os.environ.get("LLM_RESOURCE_TALLY_NOTES_REF")
            or _config_get("llmResourceTally.notesRef", root)
            or DEFAULT_NOTES_REF)


def worktree_data_dir(root: str | None = None) -> str:
    return os.path.join(root or repo_root(), ".llm_resource_tally")


def local_state_dir(root: str | None = None) -> str:
    root = root or repo_root()
    return os.path.join(git_common_dir(root), "llm-resource-tally")


def data_dir(root: str | None = None) -> str:
    root = root or repo_root()
    return local_state_dir(root) if storage_mode(root) == "notes" else worktree_data_dir(root)


def storage_description(root: str | None = None) -> str:
    root = root or repo_root()
    mode = storage_mode(root)
    if mode == "notes":
        return (f"git notes ({notes_ref(root)}); mutable settings/reports under the git "
                "common directory")
    return f".llm_resource_tally/ ({'committed' if mode == 'committed' else 'gitignored/local'})"
