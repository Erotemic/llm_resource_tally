# SPDX-License-Identifier: Apache-2.0
"""Bridge from the core measurement tool to the optional ``modeling`` package.

The minimal source-tree or zipapp install can omit modeling. ``estimate`` then exits with a
helpful install hint. ``install --modeling`` copies the package offline when available, enriches
a zipapp atomically, or fetches just the modeling subtree as a final fallback.
"""
from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request

from .gitutil import repo_root
from .version import CANONICAL_REPO
from .vendoring import infer_tool_format, module_dir, rel_dir, run_cmd

_MODELING_MARK = ("modeling", "estimate.py")


def _has_modeling(pkg_dir: str) -> bool:
    """Accept a package dir, whole source checkout, or zipapp artifact."""
    if os.path.isfile(pkg_dir):
        try:
            from .zipapp_artifact import zipapp_has_modeling
            return zipapp_has_modeling(pkg_dir)
        except (OSError, ValueError):
            return False
    return (os.path.exists(os.path.join(pkg_dir, *_MODELING_MARK))
            or os.path.exists(os.path.join(pkg_dir, "llm_resource_tally", *_MODELING_MARK)))


def install_hint() -> str:
    run = run_cmd(rel_dir(repo_root()))
    return ("estimate needs the optional modeling package, which the minimal install omits.\n"
            f"  add it:  {run} install --modeling"
            "   (offline if this is a pip/full install; otherwise fetches the subpackage)\n"
            "      or:  pip install llm_resource_tally")


def cmd_estimate(args) -> None:
    try:
        from .modeling.estimate import cmd_estimate as _run
    except ImportError:
        sys.exit(install_hint())
    _run(args)


def _copy_modeling(src_pkg: str, dest_dir: str) -> None:
    shutil.copytree(os.path.join(src_pkg, "modeling"), os.path.join(dest_dir, "modeling"),
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _fetch_modeling(repo: str, ref: str, dest_dir: str) -> None:
    """Fetch only ``llm_resource_tally/modeling/`` into a package directory."""
    url = f"https://github.com/{repo}/archive/{ref}.tar.gz"
    with tempfile.TemporaryDirectory() as td:
        tgz = os.path.join(td, "src.tgz")
        urllib.request.urlretrieve(url, tgz)          # noqa: S310 (github over https)
        with tarfile.open(tgz) as tf:
            members = [m for m in tf.getmembers()
                       if m.name.split("/")[1:3] == ["llm_resource_tally", "modeling"]]
            if not members:
                raise RuntimeError(f"{repo}@{ref} archive had no llm_resource_tally/modeling/")
            kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tf.extractall(td, members=members, **kw)
        source = next(p for p in os.listdir(td)
                      if os.path.isdir(os.path.join(td, p, "llm_resource_tally", "modeling")))
        _copy_modeling(os.path.join(td, source, "llm_resource_tally"), dest_dir)


def ensure_modeling(root: str, rel: str, repo: str | None = None, ref: str = "main") -> str:
    """Make modeling available in a source artifact or immutable zipapp."""
    dest = os.path.join(root, rel)
    if _has_modeling(dest):
        return "modeling already bundled in zipapp" if os.path.isfile(dest) else "modeling already vendored"
    if infer_tool_format(root, rel) == "zipapp":
        from .zipapp_artifact import rebuild_with_modeling
        return rebuild_with_modeling(dest, repo=repo or CANONICAL_REPO, ref=ref)

    running = module_dir()
    if os.path.isdir(running) and _has_modeling(running) and os.path.realpath(running) != os.path.realpath(dest):
        _copy_modeling(running, dest)
        return f"vendored modeling from the running package into {rel}/modeling/"
    repo = repo or CANONICAL_REPO
    _fetch_modeling(repo, ref, dest)
    return f"fetched modeling from {repo}@{ref} into {rel}/modeling/"
