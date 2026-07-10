# SPDX-License-Identifier: Apache-2.0
"""Version and canonical source-location helpers."""
from __future__ import annotations

import os

CANONICAL_REPO = "Erotemic/llm_resource_tally"


def package_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def running_zipapp_path() -> str | None:
    """Return the archive containing this module, if this process is running a zipapp."""
    loader = globals().get("__loader__")
    archive = getattr(loader, "archive", None)
    if archive and os.path.isfile(archive):
        return os.path.abspath(archive)
    return None


def source_root() -> str:
    """Filesystem path users invoke directly.

    A zipapp returns the archive path. A vendored install returns the package directory. A source
    checkout or git submodule returns the repository root containing ``pyproject.toml``,
    ``VERSION``, and the package directory. This prevents installation from generating files
    inside a submodule.
    """
    archive = running_zipapp_path()
    if archive:
        return archive
    pkg = package_dir()
    parent = os.path.dirname(pkg)
    if (os.path.basename(pkg) == "llm_resource_tally"
            and os.path.isfile(os.path.join(parent, "pyproject.toml"))
            and os.path.isfile(os.path.join(parent, "VERSION"))
            and os.path.isfile(os.path.join(parent, "__main__.py"))):
        return parent
    return pkg


def _resource_version() -> str | None:
    try:
        from importlib.resources import files
        data = files("llm_resource_tally").joinpath("VERSION").read_bytes()
        return data.decode("utf-8").strip() or None
    except (FileNotFoundError, ModuleNotFoundError, OSError, TypeError):
        return None


def tool_version() -> str:
    """Read an embedded/nearby VERSION, then fall back to installed package metadata."""
    embedded = _resource_version()
    if embedded:
        return embedded
    archive = running_zipapp_path()
    candidates = [] if archive else [os.path.join(package_dir(), "VERSION"),
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
