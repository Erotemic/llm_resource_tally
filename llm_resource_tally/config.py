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

from .backends import backend_names
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


def read_settings(root: str | None = None) -> dict:
    try:
        with open(settings_path(root), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


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


def _canonical_tool_path(value: object = None) -> str:
    """Return the one supported repository-relative invocation path.

    The path is intentionally format-independent: it is a directory in source mode and a ZIP
    archive in zipapp mode. Python accepts either representation with the same invocation.
    """
    if value is None or value == CANONICAL_TOOL_PATH:
        return CANONICAL_TOOL_PATH
    return CANONICAL_TOOL_PATH


def installation_policy(root: str | None = None) -> dict:
    """Return the normalized portable installation policy."""
    raw = read_settings(root).get("installation")
    raw = raw if isinstance(raw, dict) else {}
    storage = raw.get("storage")
    if storage not in STORAGE_MODES:
        storage = DEFAULT_INSTALLATION["storage"]
    tool_format = raw.get("tool_format")
    if tool_format not in TOOL_FORMATS:
        tool_format = DEFAULT_INSTALLATION["tool_format"]
    modeling = raw.get("modeling")
    if not isinstance(modeling, bool):
        modeling = bool(DEFAULT_INSTALLATION["modeling"])
    return {
        "storage": storage,
        "tool_format": tool_format,
        "tool_path": _canonical_tool_path(raw.get("tool_path")),
        "modeling": modeling,
    }


def set_installation_policy(
    *, storage: str, tool_format: str, tool_path: str, modeling: bool, root: str | None = None
) -> dict:
    if storage not in STORAGE_MODES:
        raise ValueError(f"unknown storage mode {storage!r}")
    if tool_format not in TOOL_FORMATS:
        raise ValueError(f"unknown tool format {tool_format!r}")
    normalized_path = _canonical_tool_path(tool_path)
    if os.path.normpath(tool_path) != normalized_path:
        raise ValueError(f"tool path is fixed at {CANONICAL_TOOL_PATH!r}")
    data = read_settings(root)
    old_install = data.get("installation")
    policy = dict(old_install) if isinstance(old_install, dict) else {}
    policy.update({
        "storage": storage,
        "tool_format": tool_format,
        "tool_path": normalized_path,
        "modeling": bool(modeling),
    })
    data["installation"] = policy
    write_settings(data, root)
    return policy


def _path_setting(value: object, default: str) -> str:
    return value if isinstance(value, str) and value.strip() else default


def publication_policy(root: str | None = None) -> dict:
    """Return normalized durable publication destinations.

    Relative paths are repository-relative. Absolute paths and ``~`` are supported by
    :func:`resolve_repository_path`; the settings file keeps the user's original spelling so it
    remains reviewable and portable when a sibling path such as ``../accounting/ledger`` is used.
    """
    raw = read_settings(root).get("publication")
    raw = raw if isinstance(raw, dict) else {}
    return {
        "append_ledger_dir": _path_setting(
            raw.get("append_ledger_dir"), DEFAULT_PUBLICATION["append_ledger_dir"]
        ),
        "lifetime_totals_path": _path_setting(
            raw.get("lifetime_totals_path"), DEFAULT_PUBLICATION["lifetime_totals_path"]
        ),
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
    """Backends the passive hook should try, in order."""
    known = set(backend_names())
    names = read_settings(root).get("backends")
    if isinstance(names, list):
        valid = [n for n in names if isinstance(n, str) and n in known]
        if valid:
            return list(dict.fromkeys(valid))
    return list(DEFAULT_BACKENDS)


def register_backend(name: str | None, root: str | None = None) -> list[str]:
    """Union a backend into the portable settings file and return the active list."""
    known = set(backend_names())
    data = read_settings(root)
    existing = data.get("backends")
    names = (
        [n for n in existing if isinstance(n, str)] if isinstance(existing, list) else list(DEFAULT_BACKENDS)
    )
    if name and name not in names:
        names.append(name)
    names = [n for n in dict.fromkeys(names) if n in known] or list(DEFAULT_BACKENDS)
    data["backends"] = names
    write_settings(data, root)
    return names
