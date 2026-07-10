# SPDX-License-Identifier: Apache-2.0
"""Build, inspect, copy, and enrich the vendored zipapp artifact.

The authoritative project remains an ordinary Python source tree.  A host repository may carry
that source tree, or a single deterministic ``.pyz`` containing the same package.  The archive is
stdlib-only, runs with ``python3 path/to/tool.pyz``, and includes its assumption-pack resources
when modeling is requested.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from .version import CANONICAL_REPO, package_dir, tool_version

ZIPAPP_FORMAT = "llm-resource-tally-zipapp/v1"
ZIPAPP_METADATA = "llm_resource_tally/ZIPAPP-METADATA.json"
ZIPAPP_VERSION = "llm_resource_tally/VERSION"
_SHEBANG = b"#!/usr/bin/env python3\n"
_ROOT_MAIN = """# SPDX-License-Identifier: Apache-2.0
from llm_resource_tally.cli import main

if __name__ == "__main__":
    main()
"""


def running_zipapp_path() -> str | None:
    """Return the archive containing this module, if imported through zipimport."""
    loader = globals().get("__loader__")
    archive = getattr(loader, "archive", None)
    if archive and os.path.isfile(archive) and zipfile.is_zipfile(archive):
        return os.path.abspath(archive)
    return None


def is_zipapp_path(path: str | os.PathLike[str]) -> bool:
    p = os.fspath(path)
    return os.path.isfile(p) and zipfile.is_zipfile(p)


def _fixed_zip_time() -> tuple[int, int, int, int, int, int]:
    """A reproducible ZIP timestamp, optionally controlled by SOURCE_DATE_EPOCH."""
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    try:
        epoch = int(raw) if raw is not None else 315532800  # 1980-01-01 UTC
    except ValueError:
        epoch = 315532800
    epoch = max(epoch, 315532800)
    return time.gmtime(epoch)[:6]


def _source_commit(pkg_dir: str) -> str | None:
    root = os.path.dirname(pkg_dir)
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    try:
        return subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None




def _source_tree_digest(pkg_dir: str, include_modeling: bool) -> str:
    """Digest exactly the package payload represented by the artifact metadata."""
    h = hashlib.sha256()
    h.update(b"__main__.py\0" + _ROOT_MAIN.encode("utf-8") + b"\0")
    for path, arcname in _iter_package_files(pkg_dir, include_modeling):
        if arcname in {ZIPAPP_METADATA, ZIPAPP_VERSION}:
            continue
        h.update(arcname.encode("utf-8") + b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    h.update(b"VERSION\0" + tool_version().encode("utf-8") + b"\0")
    return h.hexdigest()

def _metadata(pkg_dir: str, include_modeling: bool,
              source_commit: str | None = None) -> dict:
    return {
        "format": ZIPAPP_FORMAT,
        "version": tool_version(),
        "source_repository": CANONICAL_REPO,
        "source_commit": source_commit or _source_commit(pkg_dir),
        "source_tree_sha256": _source_tree_digest(pkg_dir, include_modeling),
        "python_requires": ">=3.10",
        "modeling_included": bool(include_modeling),
        "reproducible_build": True,
    }


def _iter_package_files(pkg_dir: str, include_modeling: bool):
    base = Path(pkg_dir)
    for path in sorted(base.rglob("*"), key=lambda p: p.as_posix()):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        parts = rel.split("/")
        if "__pycache__" in parts or rel.endswith((".pyc", ".pyo")):
            continue
        if parts[0] == "modeling" and not include_modeling:
            continue
        # The vendored source-tree bytecode ignore file is unnecessary inside an archive.
        if rel == ".gitignore":
            continue
        yield path, f"llm_resource_tally/{rel}"


def _write_member(zf: zipfile.ZipFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = zipfile.ZipInfo(name, _fixed_zip_time())
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    zf.writestr(info, data)


def build_zipapp(output: str, source_package: str | None = None,
                 include_modeling: bool = False,
                 source_commit: str | None = None) -> str:
    """Build a deterministic zipapp atomically and return its SHA-256 digest."""
    pkg = os.path.abspath(source_package or package_dir())
    if not os.path.isdir(pkg):
        raise ValueError(f"zipapp source package is not a directory: {pkg}")
    if not os.path.isfile(os.path.join(pkg, "__init__.py")):
        raise ValueError(f"zipapp source is not llm_resource_tally package data: {pkg}")
    if include_modeling and not os.path.isfile(os.path.join(pkg, "modeling", "estimate.py")):
        raise ValueError("modeling was requested but is absent from the source package")

    output = os.path.abspath(output)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=os.path.basename(output) + ".", suffix=".tmp",
                                dir=os.path.dirname(output) or ".")
    os.close(fd)
    try:
        archive_temp = temp + ".zip"
        with zipfile.ZipFile(archive_temp, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9, strict_timestamps=False) as zf:
            _write_member(zf, "__main__.py", _ROOT_MAIN.encode("utf-8"))
            for path, arcname in _iter_package_files(pkg, include_modeling):
                # Build metadata and the embedded version are generated below, never copied.
                if arcname in {ZIPAPP_METADATA, ZIPAPP_VERSION}:
                    continue
                _write_member(zf, arcname, path.read_bytes())
            _write_member(zf, ZIPAPP_VERSION, (tool_version() + "\n").encode("utf-8"))
            metadata = _metadata(pkg, include_modeling, source_commit=source_commit)
            raw = json.dumps(metadata, sort_keys=True, indent=2).encode("utf-8") + b"\n"
            _write_member(zf, ZIPAPP_METADATA, raw)
        with open(temp, "wb") as out, open(archive_temp, "rb") as src:
            out.write(_SHEBANG)
            shutil.copyfileobj(src, out)
        os.remove(archive_temp)
        os.chmod(temp, 0o755)
        os.replace(temp, output)
    finally:
        for leftover in (temp, temp + ".zip"):
            try:
                os.remove(leftover)
            except OSError:
                pass
    return sha256_file(output)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_zipapp(source: str, output: str) -> str:
    """Atomically copy a running or stored zipapp and return its SHA-256 digest."""
    source, output = os.path.abspath(source), os.path.abspath(output)
    if not is_zipapp_path(source):
        raise ValueError(f"not a zipapp: {source}")
    if source == output:
        return sha256_file(output)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=os.path.basename(output) + ".", suffix=".tmp",
                                dir=os.path.dirname(output) or ".")
    os.close(fd)
    try:
        shutil.copyfile(source, temp)
        os.chmod(temp, 0o755)
        os.replace(temp, output)
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass
    return sha256_file(output)


def zipapp_metadata(path: str) -> dict:
    if not is_zipapp_path(path):
        return {}
    try:
        with zipfile.ZipFile(path) as zf:
            return json.loads(zf.read(ZIPAPP_METADATA))
    except (KeyError, OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile):
        return {}


def zipapp_has_modeling(path: str) -> bool:
    if not is_zipapp_path(path):
        return False
    meta = zipapp_metadata(path)
    if "modeling_included" in meta:
        return bool(meta["modeling_included"])
    try:
        with zipfile.ZipFile(path) as zf:
            return "llm_resource_tally/modeling/estimate.py" in zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def extract_zipapp(path: str, destination: str) -> str:
    """Extract an archive built by this module; return its package directory."""
    if not is_zipapp_path(path):
        raise ValueError(f"not a zipapp: {path}")
    with zipfile.ZipFile(path) as zf:
        zf.extractall(destination)
    pkg = os.path.join(destination, "llm_resource_tally")
    if not os.path.isfile(os.path.join(pkg, "__init__.py")):
        raise ValueError("archive did not contain llm_resource_tally package")
    return pkg


def rebuild_with_modeling(path: str, repo: str | None = None, ref: str = "main") -> str:
    """Fetch the optional modeling subtree and atomically enrich an existing minimal zipapp."""
    if zipapp_has_modeling(path):
        return "modeling already bundled in zipapp"
    old_meta = zipapp_metadata(path)
    with tempfile.TemporaryDirectory() as td:
        pkg = extract_zipapp(path, td)
        # Imported lazily to avoid making the build-only path depend on urllib/tarfile setup.
        from .modeling_bridge import _fetch_modeling
        _fetch_modeling(repo or CANONICAL_REPO, ref, pkg)
        build_zipapp(path, pkg, include_modeling=True,
                     source_commit=old_meta.get("source_commit"))
    return f"added modeling to {os.path.basename(path)}"


def cmd_build_zipapp(args) -> None:
    digest = build_zipapp(args.output, include_modeling=args.modeling)
    flavor = "core + modeling" if args.modeling else "minimal core"
    print(f"built {args.output} ({flavor})")
    print(f"sha256 {digest}")
