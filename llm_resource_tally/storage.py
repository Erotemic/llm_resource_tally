# SPDX-License-Identifier: Apache-2.0
"""Ledger/state storage selected by the portable repository policy."""
from __future__ import annotations

import os

from .config import STORAGE_MODES, installation_policy, read_settings, write_settings
from .gitutil import git_common_dir, repo_root

DEFAULT_STORAGE = "committed"
DEFAULT_NOTES_REF = "refs/notes/llm-resource-tally"


def storage_mode(root: str | None = None) -> str:
    return installation_policy(root)["storage"]


def set_storage_mode(mode: str, root: str | None = None) -> str:
    """Update only storage in the canonical portable policy."""
    if mode not in STORAGE_MODES:
        raise ValueError(f"unknown storage mode {mode!r}; choose from {', '.join(STORAGE_MODES)}")
    root = root or repo_root()
    data = read_settings(root)
    install = data.get("installation")
    install = dict(install) if isinstance(install, dict) else {}
    install["storage"] = mode
    data["installation"] = install
    write_settings(data, root)
    return mode


def notes_ref(root: str | None = None) -> str:
    data = read_settings(root)
    install = data.get("installation")
    value = install.get("notes_ref") if isinstance(install, dict) else None
    return value if isinstance(value, str) and value.startswith("refs/notes/") else DEFAULT_NOTES_REF


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
        return (f"git notes ({notes_ref(root)}); mutable reports under the git common directory; "
                "settings.json remains portable in the worktree")
    if mode == "ignored":
        return ".llm_resource_tally/ generated state is gitignored; settings.json remains committed"
    return ".llm_resource_tally/ is committed"
