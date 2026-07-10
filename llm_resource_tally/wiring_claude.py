# SPDX-License-Identifier: Apache-2.0
"""Claude Code native hook wiring in `.claude/settings.json` (opt-in via `install --claude`).

Two hooks, both best-effort and idempotent (each carries a sentinel so re-install/uninstall
targets exactly ours):
  PostToolUse(Bash) -> `hook`: attributes a commit made in ANOTHER repo to the exact session
                               (the git hook alone can't see the session id).
  SessionEnd        -> reconcile+rollup: sweeps non-committing work automatically. Output is
                       suppressed and exit forced 0 (SessionEnd must not disrupt the session).
"""
from __future__ import annotations

import json
import os

from .wiring_common import read_text

_HOOK_SENTINEL = "# llm_resource_tally"


def _claude_settings_path(root: str) -> str:
    return os.path.join(root, ".claude", "settings.json")


def _claude_ptu_cmd(rel: str) -> str:
    return f'python3 -B "$CLAUDE_PROJECT_DIR/{rel}" hook  {_HOOK_SENTINEL}'


def _claude_end_cmd(rel: str) -> str:
    q = f'python3 -B "$CLAUDE_PROJECT_DIR/{rel}"'
    return f'{q} reconcile >/dev/null 2>&1; {q} rollup >/dev/null 2>&1; true  {_HOOK_SENTINEL}'


def _entry_is_ours(entry: dict) -> bool:
    for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
        c = h.get("command", "") if isinstance(h, dict) else ""
        if _HOOK_SENTINEL in c:
            return True
        if "tally" in c and c.rstrip().endswith("hook"):     # legacy (pre-sentinel) entry
            return True
    return False


# (event, entry-factory) pairs we manage. SessionEnd omits a matcher so it fires on every end
# reason (clear/logout/exit/crash).
def _claude_managed(rel: str):
    return [
        ("PostToolUse", {"matcher": "Bash",
                         "hooks": [{"type": "command", "command": _claude_ptu_cmd(rel)}]}),
        ("SessionEnd", {"hooks": [{"type": "command", "command": _claude_end_cmd(rel)}]}),
    ]


def wire_claude_hook(root: str, rel: str) -> str:
    path = _claude_settings_path(root)
    data: dict = {}
    if os.path.exists(path):
        try:
            data = json.loads(read_text(path)) or {}
        except json.JSONDecodeError:
            return "skipped (.claude/settings.json is not valid JSON — wire it by hand)"
    hooks = data.setdefault("hooks", {})
    wired = []
    for event, entry in _claude_managed(rel):
        lst = hooks.setdefault(event, [])
        if not isinstance(lst, list):
            continue
        lst[:] = [e for e in lst if not _entry_is_ours(e)]   # replace ours (idempotent)
        lst.append(entry)
        wired.append(event)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    if "PostToolUse" in wired and "SessionEnd" in wired:
        return "PostToolUse(Bash) cross-repo + SessionEnd reconcile+rollup"
    return ", ".join(wired) if wired else "skipped (unexpected hooks shape)"


def unwire_claude_hook(root: str) -> str | None:
    path = _claude_settings_path(root)
    if not os.path.exists(path):
        return None
    try:
        data = json.loads(read_text(path)) or {}
    except json.JSONDecodeError:
        return None
    hooks = data.get("hooks") or {}
    removed = False
    for event in ("PostToolUse", "SessionEnd"):
        lst = hooks.get(event)
        if not isinstance(lst, list):
            continue
        kept = [e for e in lst if not _entry_is_ours(e)]
        if len(kept) != len(lst):
            hooks[event] = kept
            removed = True
    if not removed:
        return None
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    return "removed PostToolUse + SessionEnd hooks from .claude/settings.json"
