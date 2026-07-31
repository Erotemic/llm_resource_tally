# SPDX-License-Identifier: Apache-2.0
"""Vendoring, artifact-format, and invocation-location logic."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid

from .config import CANONICAL_TOOL_PATH
from .gitutil import repo_root
from .version import package_dir, running_zipapp_path, source_root, tool_version

DEFAULT_TOOL_PATH = CANONICAL_TOOL_PATH
DEFAULT_SOURCE_DIR = DEFAULT_TOOL_PATH
DEFAULT_ZIPAPP_PATH = DEFAULT_TOOL_PATH
DEFAULT_VENDOR_DIR = DEFAULT_TOOL_PATH
LEGACY_TOOL_PATHS = (".llm_resource_tally/tool.pyz",)
TOOL_FORMATS = ("zipapp", "source")


def module_dir() -> str:
    """The real package directory, or the containing archive when running a zipapp."""
    return running_zipapp_path() or package_dir()


def invocation_dir() -> str:
    """Path users invoke: package dir, source root, or the running zipapp file."""
    return source_root()


def current_tool_format() -> str:
    return "zipapp" if running_zipapp_path() else "source"


def _module_in_repo(root: str) -> bool:
    path, r = os.path.realpath(invocation_dir()), os.path.realpath(root)
    return path == r or path.startswith(r + os.sep)


def rel_dir(root: str) -> str | None:
    return os.path.relpath(invocation_dir(), root) if _module_in_repo(root) else None


def run_cmd(rel: str | None = None) -> str:
    """Return the invariant repository invocation, independent of artifact format."""
    return f"python3 {rel or CANONICAL_TOOL_PATH}" if rel is not None else "llm_resource_tally"


def is_pip_install() -> bool:
    if running_zipapp_path():
        return False
    md = package_dir()
    return "site-packages" in md or "dist-packages" in md or not _module_in_repo(repo_root())


def is_source_checkout_path(root: str, rel: str) -> bool:
    path = os.path.join(root, rel)
    return (
        os.path.isdir(path)
        and os.path.isfile(os.path.join(path, "pyproject.toml"))
        and os.path.isfile(os.path.join(path, "VERSION"))
        and os.path.isdir(os.path.join(path, "llm_resource_tally"))
    )


def infer_tool_format(root: str, rel: str = CANONICAL_TOOL_PATH) -> str:
    path = os.path.join(root, rel)
    if os.path.isfile(path):
        return "zipapp"
    return "source"


def resolve_install_target(
    root: str, requested_dir: str | None, requested_format: str | None
) -> tuple[str, str]:
    """Resolve the fixed invocation path and the requested representation."""
    fmt = requested_format or "zipapp"
    if fmt not in TOOL_FORMATS:
        raise ValueError(f"unknown tool format {fmt!r}")
    if requested_dir is not None and os.path.normpath(requested_dir) != CANONICAL_TOOL_PATH:
        raise ValueError(f"tool path is fixed at {CANONICAL_TOOL_PATH!r}")
    return fmt, CANONICAL_TOOL_PATH


def _remove_path(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def staging_path(root: str) -> str:
    """Return a nonexistent sibling path suitable for either a file or directory artifact."""
    parent = os.path.join(root, os.path.dirname(CANONICAL_TOOL_PATH))
    os.makedirs(parent, exist_ok=True)
    return os.path.join(parent, f".tool-stage-{os.getpid()}-{uuid.uuid4().hex}")


def replace_managed_artifact(root: str, staged: str, rel: str = CANONICAL_TOOL_PATH) -> str:
    """Atomically-ish swap a validated staged file or directory into the canonical path.

    The new artifact is built on the same filesystem. The previous representation is first moved
    to a sibling backup, the staged representation is moved into place, and the backup is then
    deleted. If the second move fails, the previous artifact is restored.
    """
    final = os.path.join(root, rel)
    staged = os.path.abspath(staged)
    if not os.path.exists(staged):
        raise ValueError(f"staged tool artifact does not exist: {staged}")
    os.makedirs(os.path.dirname(final), exist_ok=True)
    backup = final + f".old-{os.getpid()}-{uuid.uuid4().hex}"
    had_old = os.path.lexists(final)
    try:
        if had_old:
            os.replace(final, backup)
        try:
            os.replace(staged, final)
        except BaseException:
            if had_old and os.path.lexists(backup) and not os.path.lexists(final):
                os.replace(backup, final)
            raise
        if had_old:
            _remove_path(backup)
    finally:
        if os.path.lexists(staged):
            _remove_path(staged)
    kind = "zipapp file" if os.path.isfile(final) else "source directory"
    return f"replaced {rel} with {kind}"


def cleanup_legacy_artifacts(root: str) -> list[str]:
    """Remove pre-invariant artifact names after the canonical path is installed."""
    messages = []
    running = os.path.realpath(invocation_dir())
    for rel in LEGACY_TOOL_PATHS:
        path = os.path.realpath(os.path.join(root, rel))
        if path == os.path.realpath(os.path.join(root, CANONICAL_TOOL_PATH)):
            continue
        if not os.path.lexists(path):
            continue
        if running == path or running.startswith(path + os.sep):
            messages.append(f"legacy artifact still running and could not be removed yet: {rel}")
            continue
        _remove_path(path)
        messages.append(f"removed legacy artifact {rel}")
    return messages


def _copy_source_tree(src: str, dest: str, include_modeling: bool) -> None:
    have_modeling = os.path.isfile(os.path.join(src, "modeling", "estimate.py"))
    if include_modeling and not have_modeling:
        raise ValueError("modeling was requested but is absent from the current artifact; use update")

    def ignore(path: str, names: list[str]) -> set[str]:
        ignored = set(shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")(path, names))
        if os.path.realpath(path) == os.path.realpath(src):
            ignored.update({"hooks", "ZIPAPP-METADATA.json"})
            if not include_modeling:
                ignored.add("modeling")
        return ignored

    if os.path.lexists(dest):
        _remove_path(dest)
    shutil.copytree(src, dest, ignore=ignore)
    with open(os.path.join(dest, "VERSION"), "w", encoding="utf-8") as fh:
        fh.write(tool_version() + "\n")


def vendor_source_into(root: str, rel_or_path: str, include_modeling: bool = False) -> str:
    dest = rel_or_path if os.path.isabs(rel_or_path) else os.path.join(root, rel_or_path)
    running = running_zipapp_path()
    if running:
        from .zipapp_artifact import extract_zipapp

        with tempfile.TemporaryDirectory() as td:
            src = extract_zipapp(running, td)
            _copy_source_tree(src, dest, include_modeling)
    else:
        src = package_dir()
        if not os.path.isdir(src):
            raise ValueError("source package is not available")
        _copy_source_tree(src, dest, include_modeling)
    flavor = "core + modeling" if include_modeling else "minimal core"
    return f"built source artifact ({flavor})"


def vendor_zipapp_into(root: str, rel_or_path: str, include_modeling: bool = False) -> str:
    from .zipapp_artifact import (
        build_zipapp,
        copy_zipapp,
        extract_zipapp,
        running_zipapp_path as archive_path,
        zipapp_has_modeling,
    )

    dest = rel_or_path if os.path.isabs(rel_or_path) else os.path.join(root, rel_or_path)
    running = archive_path()
    if running and zipapp_has_modeling(running) == include_modeling:
        copy_zipapp(running, dest)
    elif running:
        with tempfile.TemporaryDirectory() as td:
            src = extract_zipapp(running, td)
            have_modeling = os.path.isfile(os.path.join(src, "modeling", "estimate.py"))
            if include_modeling and not have_modeling:
                from .modeling_bridge import _fetch_modeling

                _fetch_modeling(None, "main", src)
            build_zipapp(dest, src, include_modeling=include_modeling)
    else:
        src = package_dir()
        have_modeling = os.path.isfile(os.path.join(src, "modeling", "estimate.py"))
        build_zipapp(dest, src, include_modeling=include_modeling and have_modeling)
        if include_modeling and not have_modeling:
            from .zipapp_artifact import rebuild_with_modeling

            rebuild_with_modeling(dest)
    flavor = "core + modeling" if include_modeling else "minimal core"
    return f"built deterministic zipapp ({flavor})"


def vendor_into(root: str, rel: str) -> str:
    """Compatibility wrapper for the historical source-tree vendoring API."""
    return vendor_source_into(root, rel)


def artifact_has_modeling(root: str, rel: str = CANONICAL_TOOL_PATH) -> bool:
    path = os.path.join(root, rel)
    if os.path.isfile(path):
        from .zipapp_artifact import zipapp_has_modeling

        return zipapp_has_modeling(path)
    return os.path.isfile(os.path.join(path, "modeling", "estimate.py")) or os.path.isfile(
        os.path.join(path, "llm_resource_tally", "modeling", "estimate.py")
    )
