# SPDX-License-Identifier: Apache-2.0
"""Per-repo settings, committed at `.llm_resource_tally/settings.json`.

Chiefly the list of **backends the passive git hook should record**. A bare `record` /
`reconcile` (no `--backend`) walks this list and records whichever agent actually has a
session matching the repo — so a Codex repo (or a mixed one) auto-records without baking a
backend name into the hook. The file is a committed, hand-editable JSON object; stdlib only.
"""
from __future__ import annotations

import json
import os

from .backends import DEFAULT_BACKEND, backend_names
from .ledger import data_dir, ensure_data_dir


def settings_path() -> str:
    return os.path.join(data_dir(), "settings.json")


def read_settings() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def registered_backends() -> list[str]:
    """Backends the passive hook should try, in order. Defaults to just the default backend
    if the repo has no settings yet. Unknown names are dropped (forward/back compatible)."""
    known = set(backend_names())
    names = read_settings().get("backends")
    if isinstance(names, list):
        valid = [n for n in names if isinstance(n, str) and n in known]
        if valid:
            return list(dict.fromkeys(valid))
    return [DEFAULT_BACKEND]


def register_backend(name: str | None) -> list[str]:
    """Ensure the default backend and `name` (if given) are registered for this repo; write
    settings.json and return the resulting list. Union/idempotent — safe to re-run. Only
    known backend names are kept."""
    known = set(backend_names())
    data = read_settings()
    names = [n for n in (data.get("backends") or []) if isinstance(n, str)]
    if DEFAULT_BACKEND not in names:
        names.insert(0, DEFAULT_BACKEND)
    if name and name not in names:
        names.append(name)
    names = [n for n in dict.fromkeys(names) if n in known]
    data["backends"] = names
    ensure_data_dir()
    with open(settings_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    return names
