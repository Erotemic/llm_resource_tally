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
from .config import installation_policy, registered_backends, settings_path
from .gitutil import repo_root
from .ledger import notes_rows, read_ledger, shard_paths
from .storage import notes_ref, storage_description, storage_mode
from .version import running_zipapp_path, tool_version
from .wiring_common import git_config, read_text
from .wiring_git import HOOK_BEGIN, effective_hooks_dir, hooks_dir_default

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}

#: Claude Code's default transcript retention; below this backfill is at risk.
_RETENTION_WARN_DAYS = 90


def _check_git_hook(root: str) -> tuple[str, str]:
    """Is a post-commit hook that calls our tool actually in place?"""
    hp = git_config(root, "--get", "core.hooksPath")
    hd = effective_hooks_dir(root, hp) if hp else hooks_dir_default(root)
    hook = os.path.join(hd, "post-commit")
    if not os.path.exists(hook):
        return FAIL, f"no post-commit hook at {os.path.relpath(hook, root)} — run `install`"
    if hp and os.path.normpath(hp).endswith("hooks"):
        if not os.access(hook, os.X_OK):
            return WARN, f"post-commit at {os.path.relpath(hook, root)} is not executable"
        return OK, f"post-commit armed via core.hooksPath -> {hp}"
    if HOOK_BEGIN in read_text(hook):
        return OK, f"post-commit armed (managed block in {os.path.relpath(hook, root)})"
    return WARN, (f"a post-commit hook exists at {os.path.relpath(hook, root)} but has no "
                  f"llm_resource_tally block — run `install`")


def _check_claude_hooks(root: str) -> tuple[str, str]:
    path = os.path.join(root, ".claude", "settings.json")
    text = read_text(path)
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
        data = json.loads(read_text(os.path.join(cfg, "settings.json"))) or {}
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
    for name in registered_backends(root):
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


def diagnose(root: str, tool_path: str | None = None) -> list[tuple[str, str]]:
    """Return a list of (status, message) checks. Read-only; never raises."""
    archive = tool_path if tool_path and os.path.isfile(tool_path) else running_zipapp_path()
    artifact = "zipapp" if archive else "source tree"
    version = tool_version()
    if archive:
        try:
            from .zipapp_artifact import sha256_file, zipapp_has_modeling, zipapp_metadata
            version = zipapp_metadata(archive).get("version") or version
            checks: list[tuple[str, str]] = [(OK, f"tool version {version} ({artifact})")]
            checks.append((OK, f"zipapp {os.path.relpath(archive, root)} · "
                               f"modeling {'included' if zipapp_has_modeling(archive) else 'omitted'} · "
                               f"sha256 {sha256_file(archive)[:12]}…"))
        except OSError as exc:
            checks = [(OK, f"tool version {version} ({artifact})"),
                      (WARN, f"could not inspect zipapp artifact: {exc}")]
    else:
        checks = [(OK, f"tool version {version} ({artifact})")]
    policy = installation_policy(root)
    mode = policy["storage"]
    checks.append((OK, f"installation policy: {policy['tool_format']} at "
                       f"{policy['tool_path']} · modeling "
                       f"{'included' if policy['modeling'] else 'omitted'}"))
    expected = os.path.join(root, policy["tool_path"])
    if os.path.exists(expected):
        checks.append((OK, f"policy artifact exists: {policy['tool_path']}"))
    else:
        checks.append((FAIL, f"policy artifact is missing: {policy['tool_path']} — run `install`"))
    checks.append((OK, f"storage {mode}: {storage_description(root)}"))
    if mode == "notes":
        checks.append((WARN, f"git notes are not fetched/pushed by default; sync {notes_ref(root)} explicitly"))
    checks.append(_check_git_hook(root))
    checks.append(_check_claude_hooks(root))
    checks.append(_check_retention())
    if os.path.exists(settings_path(root)):
        checks.append((OK, f"registered backends: {', '.join(registered_backends(root))}"))
    else:
        checks.append((WARN, "no settings.json — run `install` to register backends"))
    checks.extend(_check_backends(root))
    try:
        rows = read_ledger(root=root)
        note_count = len(notes_rows(root))
        checks.append((OK, f"ledger reads cleanly: {len(rows)} rows across "
                           f"{len(shard_paths(root))} file shard(s) + {note_count} note row(s)"))
    except Exception as e:                          # pragma: no cover - defensive
        checks.append((FAIL, f"ledger failed to read: {e}"))
    return checks


def print_report(root: str, tool_path: str | None = None) -> str:
    """Print the diagnosis; return the worst status seen."""
    worst = OK
    for status, msg in diagnose(root, tool_path=tool_path):
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
