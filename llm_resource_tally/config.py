# SPDX-License-Identifier: Apache-2.0
"""Portable per-repository settings.

``.llm_resource_tally/settings.json`` is the repository-owned policy file.  It is always
stored in the worktree, including when measured data uses local files or git notes, so a
fresh clone can reconstruct the intended installation without machine-local git config.
"""

from __future__ import annotations

import json
import os
import tempfile

from .backends import backend_names, canonical_backend_name
from .gitutil import repo_root

DEFAULT_BACKENDS = ["claude", "codex"]
CANONICAL_TOOL_PATH = ".llm_resource_tally/tool"
DEFAULT_INSTALLATION = {
    "storage": "local",
    "tool_format": "zipapp",
    "tool_path": CANONICAL_TOOL_PATH,
    "modeling": False,
}
DEFAULT_PUBLICATION = {
    "append_ledger_dir": ".llm_resource_tally/ledger",
    "lifetime_totals_path": ".llm_resource_tally/lifetime-totals.json",
}
STORAGE_MODES = ("local", "committed", "ignored", "notes")
ZIPAPP_TOOL_FORMATS = ("zipapp", "zipapp-deflate")
TOOL_FORMATS = (*ZIPAPP_TOOL_FORMATS, "source")


def settings_path(root: str | None = None) -> str:
    return os.path.join(root or repo_root(), ".llm_resource_tally", "settings.json")


def _validate_settings(data: dict, path: str) -> None:
    """Validate known policy fields while preserving unknown keys for forward compatibility."""
    installation = data.get("installation")
    if installation is not None and not isinstance(installation, dict):
        raise ValueError(f"{path}: installation policy must be a JSON object")
    if isinstance(installation, dict):
        storage = installation.get("storage")
        if storage is not None and storage not in STORAGE_MODES:
            raise ValueError(f"{path}: unknown storage mode {storage!r}")
        tool_format = installation.get("tool_format")
        if tool_format is not None and tool_format not in TOOL_FORMATS:
            raise ValueError(f"{path}: unknown tool format {tool_format!r}")
        tool_path = installation.get("tool_path")
        if tool_path is not None and tool_path != CANONICAL_TOOL_PATH:
            raise ValueError(f"{path}: tool path is fixed at {CANONICAL_TOOL_PATH!r}")
        modeling = installation.get("modeling")
        if modeling is not None and not isinstance(modeling, bool):
            raise ValueError(f"{path}: installation modeling must be true or false")
        notes = installation.get("notes_ref")
        if notes is not None and (not isinstance(notes, str) or not notes.startswith("refs/notes/")):
            raise ValueError(f"{path}: installation notes_ref must start with 'refs/notes/'")

    publication = data.get("publication")
    if publication is not None and not isinstance(publication, dict):
        raise ValueError(f"{path}: publication policy must be a JSON object")
    if isinstance(publication, dict):
        for key in ("append_ledger_dir", "lifetime_totals_path"):
            value = publication.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{path}: publication {key} must be a non-empty path string")

    backends = data.get("backends")
    if backends is not None:
        known = set(backend_names())
        if not isinstance(backends, list) or not backends:
            raise ValueError(f"{path}: backends must be a non-empty JSON list")
        invalid = [name for name in backends if not isinstance(name, str) or name not in known]
        if invalid:
            raise ValueError(f"{path}: unknown recorder backend(s): {invalid!r}")


def read_settings(root: str | None = None) -> dict:
    """Read repository policy, treating malformed/unreadable policy as an accounting error.

    A missing file means defaults.  Any existing-but-invalid file fails closed instead of silently
    redirecting recording/publication back to default locations.
    """
    path = settings_path(root)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path} is not valid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    _validate_settings(data, path)
    return data


def write_settings(data: dict, root: str | None = None) -> None:
    """Write settings atomically while preserving a stable, reviewable JSON format."""
    path = settings_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return
    except OSError:
        pass
    fd, temp = tempfile.mkstemp(prefix="settings.", suffix=".json.tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(temp, path)
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass


def installation_policy(root: str | None = None) -> dict:
    """Return portable installation policy with defaults filled in."""
    raw = read_settings(root).get("installation") or {}
    return {
        "storage": raw.get("storage", DEFAULT_INSTALLATION["storage"]),
        "tool_format": raw.get("tool_format", DEFAULT_INSTALLATION["tool_format"]),
        "tool_path": CANONICAL_TOOL_PATH,
        "modeling": raw.get("modeling", DEFAULT_INSTALLATION["modeling"]),
    }


def set_installation_policy(
    *, storage: str, tool_format: str, tool_path: str, modeling: bool, root: str | None = None
) -> dict:
    if storage not in STORAGE_MODES:
        raise ValueError(f"unknown storage mode {storage!r}")
    if tool_format not in TOOL_FORMATS:
        raise ValueError(f"unknown tool format {tool_format!r}")
    if os.path.normpath(tool_path) != CANONICAL_TOOL_PATH:
        raise ValueError(f"tool path is fixed at {CANONICAL_TOOL_PATH!r}")
    data = read_settings(root)
    old_install = data.get("installation")
    policy = dict(old_install) if isinstance(old_install, dict) else {}
    policy.update({
        "storage": storage,
        "tool_format": tool_format,
        "tool_path": CANONICAL_TOOL_PATH,
        "modeling": bool(modeling),
    })
    data["installation"] = policy
    write_settings(data, root)
    return policy


def publication_policy(root: str | None = None) -> dict:
    """Return normalized durable publication destinations.

    Relative paths are repository-relative. Absolute paths and ``~`` are supported by
    :func:`resolve_repository_path`; the settings file keeps the user's original spelling so it
    remains reviewable and portable when a sibling path such as ``../accounting/ledger`` is used.
    """
    raw = read_settings(root).get("publication") or {}
    return {
        key: raw.get(key, default)
        for key, default in DEFAULT_PUBLICATION.items()
    }


def set_publication_policy(
    *, append_ledger_dir: str, lifetime_totals_path: str, root: str | None = None
) -> dict:
    """Persist durable publication destinations without disturbing other settings."""
    values = {
        "append_ledger_dir": append_ledger_dir,
        "lifetime_totals_path": lifetime_totals_path,
    }
    for key, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"publication {key} must be a non-empty path string")
    data = read_settings(root)
    old = data.get("publication")
    policy = dict(old) if isinstance(old, dict) else {}
    policy.update(values)
    data["publication"] = policy
    write_settings(data, root)
    return policy


def resolve_repository_path(value: str, root: str | None = None) -> str:
    """Resolve a configured path, relative to the repository root when not absolute."""
    root = os.path.abspath(root or repo_root())
    value = os.path.expanduser(value)
    return os.path.normpath(value if os.path.isabs(value) else os.path.join(root, value))


def registered_backends(root: str | None = None) -> list[str]:
    """Backends the passive hook should try, in order, with aliases collapsed."""
    names = read_settings(root).get("backends") or DEFAULT_BACKENDS
    return list(dict.fromkeys(canonical_backend_name(name) for name in names))


def register_backend(name: str | None, root: str | None = None) -> list[str]:
    """Union one validated backend selector into portable settings."""
    data = read_settings(root)
    existing = data.get("backends") or DEFAULT_BACKENDS
    names = [canonical_backend_name(value) for value in existing]
    if name is not None:
        names.append(canonical_backend_name(name))
    names = list(dict.fromkeys(names))
    data["backends"] = names
    write_settings(data, root)
    return names
