# SPDX-License-Identifier: Apache-2.0
"""Vendoring & invocation-location logic: where the running package lives (pip vs vendored vs
submodule), how a human/hook invokes it, and copying the package into a repo for the pip/curl
routes so the repo becomes self-contained and offline."""
from __future__ import annotations

import os
import shutil

from .gitutil import repo_root
from .version import tool_version
from .wiring_git import ensure_hook_file

DEFAULT_VENDOR_DIR = ".llm_resource_tally/tool"   # code sits beside its data (one dotdir)


def module_dir() -> str:
    """This package's directory (…/llm_resource_tally)."""
    return os.path.dirname(os.path.abspath(__file__))


def _module_in_repo(root: str) -> bool:
    md, r = os.path.realpath(module_dir()), os.path.realpath(root)
    return md == r or md.startswith(r + os.sep)


def rel_dir(root: str) -> str | None:
    """The package dir relative to repo root if vendored/submodule, else None (pip)."""
    return os.path.relpath(module_dir(), root) if _module_in_repo(root) else None


def run_cmd(rel: str | None) -> str:
    """How a human/hook invokes the tool: run the vendored package dir by path (Python executes
    its __main__.py), or the console script when pip'd (bootstrap only)."""
    return f"python3 {rel}" if rel else "llm_resource_tally"


def is_pip_install() -> bool:
    md = module_dir()
    return "site-packages" in md or "dist-packages" in md or not _module_in_repo(repo_root())


def vendor_into(root: str, rel: str) -> str:
    """Copy the package from the running (pip-installed) install into `<root>/<rel>/`, then
    stamp VERSION and ensure the hook script — so the repo is self-contained and offline exactly
    like the curl route. PyPI is only the delivery mechanism."""
    dest = os.path.join(root, rel)
    shutil.copytree(module_dir(), dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    with open(os.path.join(dest, "VERSION"), "w", encoding="utf-8") as fh:
        fh.write(tool_version() + "\n")
    ensure_hook_file(root, rel)
    return f"vendored the package into {rel}/ (from the installed package)"
