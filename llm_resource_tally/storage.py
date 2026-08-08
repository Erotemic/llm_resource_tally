# SPDX-License-Identifier: Apache-2.0
"""Ledger/state storage selected by the portable repository policy."""

from __future__ import annotations

import os

from .config import (
    STORAGE_MODES,
    installation_policy,
    publication_policy,
    read_settings,
    resolve_repository_path,
    write_settings,
)
from .gitutil import git_common_dir, repo_root

DEFAULT_STORAGE = "local"
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


def local_data_dir(root: str | None = None) -> str:
    """Ignored mutable state for the default local-spool mode."""
    return os.path.join(worktree_data_dir(root), "local")


def local_state_dir(root: str | None = None) -> str:
    root = root or repo_root()
    return os.path.join(git_common_dir(root), "llm-resource-tally")


def published_ledger_dir(root: str | None = None) -> str:
    """Durable append-only JSONL destination configured for this repository."""
    root = root or repo_root()
    return resolve_repository_path(publication_policy(root)["append_ledger_dir"], root)


def published_totals_path(root: str | None = None) -> str:
    """Durable lifetime-total JSON destination configured for this repository."""
    root = root or repo_root()
    return resolve_repository_path(publication_policy(root)["lifetime_totals_path"], root)


def data_dir(root: str | None = None) -> str:
    root = root or repo_root()
    mode = storage_mode(root)
    if mode == "notes":
        return local_state_dir(root)
    if mode == "local":
        return local_data_dir(root)
    return worktree_data_dir(root)


def _display_path(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:  # pragma: no cover - different Windows drive
        return path


def storage_description(root: str | None = None) -> str:
    root = root or repo_root()
    mode = storage_mode(root)
    ledger = _display_path(published_ledger_dir(root), root)
    totals = _display_path(published_totals_path(root), root)
    if mode == "notes":
        return (
            f"git notes ({notes_ref(root)}); mutable reports under the git common directory; "
            f"published lifetime totals at {totals}"
        )
    if mode == "local":
        return (
            ".llm_resource_tally/local/ ignored mutable state; explicit `publish` appends rows "
            f"to {ledger} and refreshes {totals}"
        )
    if mode == "ignored":
        return (
            f"generated main-repository state is gitignored; durable row destination {ledger}; "
            f"lifetime totals {totals}; settings.json remains committed"
        )
    return f"durable row destination {ledger}; lifetime totals {totals}"
