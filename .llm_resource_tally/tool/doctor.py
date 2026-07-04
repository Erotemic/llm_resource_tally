# SPDX-License-Identifier: Apache-2.0
"""`doctor` — is this repo wired correctly and will it keep recording?

A single read-only pass that answers the questions a user otherwise discovers too late: is the
post-commit hook actually armed, are the Claude native hooks wired, do the registered backends
find a session for this repo, is the ledger readable — and, most valuably, is the agent's
transcript retention set high enough that backfill will still be possible later. Retention is
the one silent, unrecoverable failure: nobody is warned until the history is already gone.
"""
from __future__ import annotations

import json
import os

from .backends import get_backend
from .config import registered_backends, settings_path
from .gitutil import repo_root
from .install import (HOOK_BEGIN, _effective_hooks_dir, _git_config, _hooks_dir_default,
                      _read_text)
from .ledger import read_ledger, shard_paths
from .version import tool_version

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}

#: Claude Code's default transcript retention; below this backfill is at risk.
_RETENTION_WARN_DAYS = 90


def _check_git_hook(root: str) -> tuple[str, str]:
    """Is a post-commit hook that calls our tool actually in place?"""
    hp = _git_config(root, "--get", "core.hooksPath")
    hd = _effective_hooks_dir(root, hp) if hp else _hooks_dir_default(root)
    hook = os.path.join(hd, "post-commit")
    if not os.path.exists(hook):
        return FAIL, f"no post-commit hook at {os.path.relpath(hook, root)} — run `install`"
    if hp and os.path.normpath(hp).endswith("hooks"):
        if not os.access(hook, os.X_OK):
            return WARN, f"post-commit at {os.path.relpath(hook, root)} is not executable"
        return OK, f"post-commit armed via core.hooksPath -> {hp}"
    if HOOK_BEGIN in _read_text(hook):
        return OK, f"post-commit armed (managed block in {os.path.relpath(hook, root)})"
    return WARN, (f"a post-commit hook exists at {os.path.relpath(hook, root)} but has no "
                  f"llm_resource_tally block — run `install`")


def _check_claude_hooks(root: str) -> tuple[str, str]:
    path = os.path.join(root, ".claude", "settings.json")
    text = _read_text(path)
    if not text.strip():
        return WARN, "Claude native hooks not wired (optional) — `install --claude` for cross-repo"
    try:
        data = json.loads(text) or {}
    except json.JSONDecodeError:
        return WARN, ".claude/settings.json is not valid JSON"
    hooks = data.get("hooks") or {}
    have = {ev for ev in ("PostToolUse", "SessionEnd")
            if any("llm_resource_tally" in h.get("command", "")
                   for e in hooks.get(ev, []) for h in e.get("hooks", []))}
    if have == {"PostToolUse", "SessionEnd"}:
        return OK, "Claude PostToolUse + SessionEnd hooks wired (cross-repo + auto-sweep)"
    if have:
        return WARN, f"only {', '.join(sorted(have))} wired — re-run `install --claude`"
    return WARN, "Claude native hooks not wired (optional) — `install --claude` for cross-repo"


def _claude_retention_days() -> int | None:
    cfg = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
    try:
        data = json.loads(_read_text(os.path.join(cfg, "settings.json"))) or {}
        v = data.get("cleanupPeriodDays")
        return int(v) if v is not None else None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _check_retention() -> tuple[str, str]:
    days = _claude_retention_days()
    effective = 30 if days is None else days       # Claude Code's default when unset
    src = "default" if days is None else "cleanupPeriodDays"
    if effective < _RETENTION_WARN_DAYS:
        return WARN, (f"Claude transcript retention is {effective}d ({src}); raise "
                      f"cleanupPeriodDays in ~/.claude/settings.json now — backfill can never "
                      f"recover pruned sessions")
    return OK, f"Claude transcript retention {effective}d ({src})"


def _check_backends(root: str) -> list[tuple[str, str]]:
    out = []
    for name in registered_backends():
        b = get_backend(name)
        try:
            t = b.find_transcript(b.default_projects_dir(), None, strict=True)
        except SystemExit:
            t = None
        if t:
            out.append((OK, f"backend {b.name}: found a session for this repo"))
        else:
            out.append((OK, f"backend {b.name}: registered, no session for this repo yet"))
    return out


def diagnose(root: str) -> list[tuple[str, str]]:
    """Return a list of (status, message) checks. Read-only; never raises."""
    checks: list[tuple[str, str]] = [(OK, f"tool version {tool_version()}")]
    checks.append(_check_git_hook(root))
    checks.append(_check_claude_hooks(root))
    checks.append(_check_retention())
    if os.path.exists(settings_path()):
        checks.append((OK, f"registered backends: {', '.join(registered_backends())}"))
    else:
        checks.append((WARN, "no settings.json — run `install` to register backends"))
    checks.extend(_check_backends(root))
    try:
        rows = read_ledger()
        checks.append((OK, f"ledger reads cleanly: {len(rows)} rows across "
                           f"{len(shard_paths())} shard(s)"))
    except Exception as e:                          # pragma: no cover - defensive
        checks.append((FAIL, f"ledger failed to read: {e}"))
    return checks


def print_report(root: str) -> str:
    """Print the diagnosis; return the worst status seen."""
    worst = OK
    for status, msg in diagnose(root):
        print(f"  {_MARK.get(status, '?')} {msg}")
        if status == FAIL or (status == WARN and worst == OK):
            worst = status
    return worst


def cmd_doctor(args) -> None:
    import sys
    root = repo_root()
    print(f"llm_resource_tally doctor — {os.path.basename(root)}")
    worst = print_report(root)
    if worst == FAIL:
        sys.exit(1)
