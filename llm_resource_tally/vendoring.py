# SPDX-License-Identifier: Apache-2.0
"""Vendoring and invocation-location logic."""
from __future__ import annotations

import os
import shutil

from .gitutil import repo_root
from .version import package_dir, source_root, tool_version

DEFAULT_VENDOR_DIR = ".llm_resource_tally/tool"


def module_dir() -> str:
    """The import package directory copied by pip/bootstrap installs."""
    return package_dir()


def invocation_dir() -> str:
    """Directory users run by path: package dir when vendored, repo root for source/submodule."""
    return source_root()


def _module_in_repo(root: str) -> bool:
    md, r = os.path.realpath(invocation_dir()), os.path.realpath(root)
    return md == r or md.startswith(r + os.sep)


def rel_dir(root: str) -> str | None:
    return os.path.relpath(invocation_dir(), root) if _module_in_repo(root) else None


def run_cmd(rel: str | None) -> str:
    return f"python3 {rel}" if rel else "llm_resource_tally"


def is_pip_install() -> bool:
    md = module_dir()
    return "site-packages" in md or "dist-packages" in md or not _module_in_repo(repo_root())


def is_source_checkout_path(root: str, rel: str) -> bool:
    path = os.path.join(root, rel)
    return (os.path.isfile(os.path.join(path, "pyproject.toml"))
            and os.path.isfile(os.path.join(path, "VERSION"))
            and os.path.isdir(os.path.join(path, "llm_resource_tally")))


def shared_hooks_rel(root: str, rel: str) -> str:
    """Keep generated hooks outside a source checkout used as a git submodule."""
    if is_source_checkout_path(root, rel):
        parent = os.path.dirname(rel)
        return os.path.join(parent, "hooks") if parent else ".llm_resource_tally-hooks"
    return f"{rel}/hooks"


def vendor_into(root: str, rel: str) -> str:
    dest = os.path.join(root, rel)
    shutil.copytree(module_dir(), dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    with open(os.path.join(dest, "VERSION"), "w", encoding="utf-8") as fh:
        fh.write(tool_version() + "\n")
    return f"vendored the package into {rel}/ (from the installed package)"
