# SPDX-License-Identifier: Apache-2.0
"""Portable per-repository settings.

``.llm_resource_tally/settings.json`` is the repository-owned policy file.  It is always
stored in the worktree, including when measured data uses ignored files or git notes, so a
fresh clone can reconstruct the intended installation without machine-local git config.
"""
from __future__ import annotations

import json
import os
import tempfile

from .backends import backend_names
from .gitutil import repo_root

DEFAULT_BACKENDS = ["claude", "codex"]
DEFAULT_INSTALLATION = {
    "storage": "committed",
    "tool_format": "zipapp",
    "tool_path": ".llm_resource_tally/tool.pyz",
    "modeling": False,
}
STORAGE_MODES = ("committed", "ignored", "notes")
TOOL_FORMATS = ("zipapp", "source")


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
    fd, temp = tempfile.mkstemp(prefix="settings.", suffix=".json.tmp",
                                dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(temp, path)
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass


def _safe_relative_tool_path(value: object, fmt: str) -> str:
    default = (".llm_resource_tally/tool.pyz" if fmt == "zipapp"
               else ".llm_resource_tally/tool")
    if not isinstance(value, str) or not value.strip():
        return default
    value = os.path.normpath(value.strip())
    if (os.path.isabs(value) or value in (".", "..", ".llm_resource_tally")
            or value.startswith(".." + os.sep)):
        return default
    if fmt == "zipapp" and not value.endswith(".pyz"):
        return default
    if fmt == "source" and value.endswith(".pyz"):
        return default
    return value


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
        "tool_path": _safe_relative_tool_path(raw.get("tool_path"), tool_format),
        "modeling": modeling,
    }


def set_installation_policy(*, storage: str, tool_format: str, tool_path: str,
                            modeling: bool, root: str | None = None) -> dict:
    if storage not in STORAGE_MODES:
        raise ValueError(f"unknown storage mode {storage!r}")
    if tool_format not in TOOL_FORMATS:
        raise ValueError(f"unknown tool format {tool_format!r}")
    normalized_path = _safe_relative_tool_path(tool_path, tool_format)
    if os.path.normpath(tool_path) != normalized_path:
        raise ValueError(f"invalid {tool_format} tool path {tool_path!r}")
    policy = {
        "storage": storage,
        "tool_format": tool_format,
        "tool_path": normalized_path,
        "modeling": bool(modeling),
    }
    data = read_settings(root)
    data["installation"] = policy
    write_settings(data, root)
    return policy


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
    names = ([n for n in existing if isinstance(n, str)] if isinstance(existing, list)
             else list(DEFAULT_BACKENDS))
    if name and name not in names:
        names.append(name)
    names = [n for n in dict.fromkeys(names) if n in known] or list(DEFAULT_BACKENDS)
    data["backends"] = names
    write_settings(data, root)
    return names
