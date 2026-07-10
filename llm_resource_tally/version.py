# SPDX-License-Identifier: Apache-2.0
"""Version and canonical source-location helpers."""
from __future__ import annotations

import os

CANONICAL_REPO = "Erotemic/llm_resource_tally"


def package_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def source_root() -> str:
    """Directory users invoke by path.

    A vendored install invokes the package directory itself. A source checkout or git
    submodule invokes the repository root containing ``pyproject.toml``, ``VERSION``, and the
    package directory. This prevents installation from generating files inside a submodule.
    """
    pkg = package_dir()
    parent = os.path.dirname(pkg)
    if (os.path.basename(pkg) == "llm_resource_tally"
            and os.path.isfile(os.path.join(parent, "pyproject.toml"))
            and os.path.isfile(os.path.join(parent, "VERSION"))
            and os.path.isfile(os.path.join(parent, "__main__.py"))):
        return parent
    return pkg


def tool_version() -> str:
    """Read the nearest VERSION file, then fall back to installed package metadata."""
    candidates = [os.path.join(package_dir(), "VERSION"),
                  os.path.join(source_root(), "VERSION")]
    for path in dict.fromkeys(candidates):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip() or "0.0.0"
        except OSError:
            pass
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("llm_resource_tally")
        except PackageNotFoundError:
            return "0.0.0"
    except Exception:
        return "0.0.0"
