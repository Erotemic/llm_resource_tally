# SPDX-License-Identifier: Apache-2.0
"""Claude Code PostToolUse(Bash) hook handler — the precise cross-repo recorder.

A git post-commit hook can't identify the Claude session (the CLI exposes no session id
to subprocesses), so when a session in repo A commits into repo B it can't attribute
correctly. Claude Code's *native* PostToolUse hook CAN: it delivers session_id +
transcript_path + cwd (the repo the commit landed in) on stdin. This handler consumes
that payload and records the exact session against the exact repo. Claude-specific.
"""
from __future__ import annotations

import json
import os
import re
import sys
import types

# `git` possibly preceded by leading env assignments, then any number of `-c k=v` / `-C path`
# global flags in ANY order (path may be quoted, so it can contain spaces), then `commit`.
_ARG = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
_GIT_COMMIT_RE = re.compile(rf"\bgit\b(?:\s+-[cC]\s+{_ARG})*\s+commit\b")
_C_FLAG_RE = re.compile(rf"\bgit\b(?:\s+-c\s+{_ARG})*\s+-C\s+({_ARG})")
_CD_RE = re.compile(rf"(?:^|&&|;|\|)\s*cd\s+({_ARG})")


def is_git_commit(command: str) -> bool:
    """Heuristic: does this shell command create a commit? (Excludes dry-run/help.)"""
    if not command or not _GIT_COMMIT_RE.search(command):
        return False
    return not re.search(r"--dry-run|--help|(?:^|\s)-h(?:\s|$)", command)


def _resolve(path: str, base: str) -> str:
    path = os.path.expanduser(path.strip("\"'"))
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def commit_repo_dir(command: str, cwd: str) -> str:
    """The repo the commit lands in. Precedence, most authoritative first:
    `git -C <path>` (resolved against a leading `cd` target if any), then a leading
    `cd <path> && …`, then the tool-call cwd. Handles quoted paths and `-c k=v` before `-C`."""
    base = cwd
    cd = _CD_RE.search(command)
    if cd:
        base = _resolve(cd.group(1), cwd)
    c = _C_FLAG_RE.search(command)
    if c:
        return _resolve(c.group(1), base)
    return base


def cmd_hook(args) -> None:
    """Read the PostToolUse JSON on stdin; if the call was a `git commit`, record that
    session's turns against the commit in whatever repo it landed in. MUST NEVER raise or
    print — a hook must not disrupt the session — so everything is wrapped and silenced."""
    from ..record import cmd_record
    from ..gitutil import repo_root
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    try:
        command = (payload.get("tool_input") or {}).get("command") or ""
        if not is_git_commit(command):
            return
        cwd = payload.get("cwd") or os.getcwd()
        repo_dir = commit_repo_dir(command, cwd)
        transcript = payload.get("transcript_path")
        if not transcript or not os.path.exists(transcript):
            return
        ns = types.SimpleNamespace(
            backend="claude", commit="HEAD", transcript=transcript, session=None,
            projects_dir=args.projects_dir, label=None, force=False,
            no_estimate_compaction=False)
        old_cwd, old_out = os.getcwd(), sys.stdout
        try:
            os.chdir(repo_dir)
            repo_root()                    # raises if repo_dir isn't a git repo -> skip
            sys.stdout = open(os.devnull, "w")
            cmd_record(ns)
        finally:
            try:
                sys.stdout.close()
            except Exception:
                pass
            sys.stdout = old_out
            os.chdir(old_cwd)
    except Exception:
        return
