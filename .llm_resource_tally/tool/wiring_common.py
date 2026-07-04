# SPDX-License-Identifier: Apache-2.0
"""Low-level filesystem/git/text helpers shared by the wiring modules (`wiring_git`,
`wiring_agents`, `wiring_claude`) and the installer. No policy here — just the primitives for
reading files, chmod +x, git config, and splicing sentinel-delimited managed regions."""
from __future__ import annotations

import os
import subprocess

from .gitutil import git


def chmod_x(path: str) -> None:
    try:
        os.chmod(path, os.stat(path).st_mode | 0o111)
    except OSError:
        pass


def git_config(root: str, *args: str) -> str:
    try:
        return git("config", *args, cwd=root)
    except subprocess.CalledProcessError:
        return ""


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def replace_region(text: str, begin: str, end: str, repl: str) -> str:
    """Replace the `begin`..`end` inclusive span with `repl` (no-op if absent)."""
    if begin in text and end in text:
        s = text.index(begin)
        e = text.index(end, s) + len(end)
        return text[:s] + repl + text[e:]
    return text


def strip_region(text: str, begin_idx: int, end_str: str) -> str:
    """Excise the managed region starting at `begin_idx` through `end_str`, healing the
    surrounding newlines so no blank gap is left behind."""
    e = text.index(end_str, begin_idx) + len(end_str)
    before, after = text[:begin_idx], text[e:]
    if before.endswith("\n"):
        before = before[:-1]
    if after.startswith("\n"):
        after = after[1:]
    return before + after
