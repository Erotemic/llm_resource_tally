# SPDX-License-Identifier: Apache-2.0
"""Vendoring, artifact-format, and invocation-location logic."""
from __future__ import annotations

import os
import shutil

from .gitutil import repo_root
from .version import package_dir, running_zipapp_path, source_root, tool_version

DEFAULT_SOURCE_DIR = ".llm_resource_tally/tool"
DEFAULT_ZIPAPP_PATH = ".llm_resource_tally/tool.pyz"
DEFAULT_VENDOR_DIR = DEFAULT_SOURCE_DIR  # compatibility alias
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


def run_cmd(rel: str | None) -> str:
    return f"python3 {rel}" if rel else "llm_resource_tally"


def is_pip_install() -> bool:
    if running_zipapp_path():
        return False
    md = package_dir()
    return "site-packages" in md or "dist-packages" in md or not _module_in_repo(repo_root())


def is_source_checkout_path(root: str, rel: str) -> bool:
    path = os.path.join(root, rel)
    return (os.path.isdir(path)
            and os.path.isfile(os.path.join(path, "pyproject.toml"))
            and os.path.isfile(os.path.join(path, "VERSION"))
            and os.path.isdir(os.path.join(path, "llm_resource_tally")))


def shared_hooks_rel(root: str, rel: str) -> str:
    """Keep generated hooks outside source checkouts and beside a zipapp artifact."""
    path = os.path.join(root, rel)
    if is_source_checkout_path(root, rel):
        parent = os.path.dirname(rel)
        return os.path.join(parent, "hooks") if parent else ".llm_resource_tally-hooks"
    if rel.endswith(".pyz") or os.path.isfile(path):
        parent = os.path.dirname(rel)
        return os.path.join(parent, "hooks") if parent else ".llm_resource_tally-hooks"
    return f"{rel}/hooks"


def infer_tool_format(root: str, rel: str) -> str:
    path = os.path.join(root, rel)
    if rel.endswith(".pyz") or os.path.isfile(path):
        return "zipapp"
    return "source"


def resolve_install_target(root: str, requested_dir: str | None,
                           requested_format: str | None) -> tuple[str, str]:
    """Resolve a canonical ``(format, relative target)`` from repository policy."""
    fmt = requested_format or "zipapp"
    if fmt not in TOOL_FORMATS:
        raise ValueError(f"unknown tool format {fmt!r}")
    rel = requested_dir or (DEFAULT_ZIPAPP_PATH if fmt == "zipapp" else DEFAULT_SOURCE_DIR)
    rel = os.path.normpath(rel)
    if (os.path.isabs(rel) or rel in (".", "..", ".llm_resource_tally")
            or rel.startswith(".." + os.sep)):
        raise ValueError("tool path must be a dedicated path inside the repository")
    if fmt == "zipapp" and not rel.endswith(".pyz"):
        raise ValueError("zipapp tool paths must end in .pyz")
    if fmt == "source" and rel.endswith(".pyz"):
        raise ValueError("source tool paths must be directories, not .pyz files")
    return fmt, rel


def vendor_source_into(root: str, rel: str, include_modeling: bool = False) -> str:
    dest = os.path.join(root, rel)
    src = module_dir()
    if not os.path.isdir(src):
        raise ValueError("cannot create a source-tree install from a zipapp; use update so the "
                         "source artifact is fetched before execution")

    def ignore(path: str, names: list[str]) -> set[str]:
        ignored = set(shutil.ignore_patterns("__pycache__", "*.pyc")(path, names))
        if not include_modeling and os.path.realpath(path) == os.path.realpath(src):
            ignored.add("modeling")
        return ignored

    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=ignore)
    with open(os.path.join(dest, "VERSION"), "w", encoding="utf-8") as fh:
        fh.write(tool_version() + "\n")
    flavor = "core + modeling" if include_modeling else "minimal core"
    return f"vendored the package into {rel}/ ({flavor})"


def vendor_zipapp_into(root: str, rel: str, include_modeling: bool = False) -> str:
    from .zipapp_artifact import (build_zipapp, copy_zipapp, running_zipapp_path as archive_path,
                                  zipapp_has_modeling)
    dest = os.path.join(root, rel)
    running = archive_path()
    if running:
        copy_zipapp(running, dest)
        if include_modeling and not zipapp_has_modeling(dest):
            from .zipapp_artifact import rebuild_with_modeling
            rebuild_with_modeling(dest)
        flavor = "core + modeling" if zipapp_has_modeling(dest) else "minimal core"
        return f"copied the running zipapp to {rel} ({flavor})"
    src = package_dir()
    have_modeling = os.path.isfile(os.path.join(src, "modeling", "estimate.py"))
    build_zipapp(dest, src, include_modeling=include_modeling and have_modeling)
    if include_modeling and not have_modeling:
        from .zipapp_artifact import rebuild_with_modeling
        rebuild_with_modeling(dest)
    flavor = "core + modeling" if include_modeling else "minimal core"
    return f"built deterministic zipapp {rel} ({flavor})"


def vendor_into(root: str, rel: str) -> str:
    """Compatibility wrapper for the historical source-tree vendoring API."""
    return vendor_source_into(root, rel)


def artifact_has_modeling(root: str, rel: str) -> bool:
    path = os.path.join(root, rel)
    if infer_tool_format(root, rel) == "zipapp":
        from .zipapp_artifact import zipapp_has_modeling
        return zipapp_has_modeling(path)
    return (os.path.isfile(os.path.join(path, "modeling", "estimate.py"))
            or os.path.isfile(os.path.join(path, "llm_resource_tally", "modeling", "estimate.py")))
