# SPDX-License-Identifier: Apache-2.0
"""Bridge from the core (measurement) tool to the OPTIONAL `modeling` subpackage.

The minimal `curl | sh` install vendors the core WITHOUT `modeling/` so the offline footprint
stays tiny (see `llm_resource_tally.modeling`). This module is the seam:

  * `cmd_estimate` — the CLI's `estimate` handler; dispatches into `modeling` if present, else
    exits with a one-line install hint (never an obscure ImportError traceback).
  * `ensure_modeling` — the opt-in installer (`install --modeling`): copy the subpackage from
    the running package if it has it (pip / full vendor, offline), else fetch just that subdir
    from the canonical repo tarball (stdlib urllib + tarfile — no curl needed at this point).

Keeping this in core (not in `modeling`) is the point: core must run and give a helpful message
when `modeling` is absent, so it cannot import it at module load.
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
from .vendoring import module_dir, rel_dir, run_cmd

_MODELING_MARK = ("modeling", "estimate.py")   # a dir counts as "has modeling" if this exists


def _has_modeling(pkg_dir: str) -> bool:
    return os.path.exists(os.path.join(pkg_dir, *_MODELING_MARK))


def install_hint() -> str:
    run = run_cmd(rel_dir(repo_root()))
    return ("estimate needs the optional modeling package, which the minimal install omits.\n"
            f"  add it:  {run} install --modeling"
            "   (offline if this is a pip/full install; else fetches just that subpackage)\n"
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
    """Fetch just `llm_resource_tally/modeling/` from the repo's tarball into `<dest_dir>/`."""
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
        # the strip prefix is <repo>-<ref>/; find it robustly
        root = next(p for p in os.listdir(td)
                    if os.path.isdir(os.path.join(td, p, "llm_resource_tally", "modeling")))
        _copy_modeling(os.path.join(td, root, "llm_resource_tally"), dest_dir)


def ensure_modeling(root: str, rel: str, repo: str | None = None, ref: str = "main") -> str:
    """Make the modeling subpackage available in the vendored tool dir `<root>/<rel>/`.
    Idempotent. Prefers an offline copy from the running package; falls back to a network
    fetch of just that subdir."""
    dest = os.path.join(root, rel)
    if _has_modeling(dest):
        return "modeling already vendored"
    running = module_dir()
    if _has_modeling(running) and os.path.realpath(running) != os.path.realpath(dest):
        _copy_modeling(running, dest)
        return f"vendored modeling from the running package into {rel}/modeling/"
    repo = repo or CANONICAL_REPO
    _fetch_modeling(repo, ref, dest)
    return f"fetched modeling from {repo}@{ref} into {rel}/modeling/"
