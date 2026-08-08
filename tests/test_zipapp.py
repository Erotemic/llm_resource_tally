# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

def run(args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True)

def git(args, cwd):
    return run(["git", *args], cwd)

def init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    assert git(["init", "-q"], path).returncode == 0
    git(["config", "user.email", "t@t"], path)
    git(["config", "user.name", "t"], path)
    git(["config", "commit.gpgsign", "false"], path)
    (path / "seed.txt").write_text("seed\n")
    git(["add", "-A"], path)
    assert git(["commit", "-qm", "seed"], path).returncode == 0

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def compression_types(path: Path) -> set[int]:
    with zipfile.ZipFile(path) as zf:
        return {info.compress_type for info in zf.infolist() if not info.is_dir()}

def test_zipapp_build_is_reproducible_and_executable(tmp_path):
    from llm_resource_tally.zipapp_artifact import (
        build_zipapp,
        zipapp_metadata,
        zipapp_tool_format,
    )

    a, b = tmp_path / "a.pyz", tmp_path / "b.pyz"
    build_zipapp(str(a), include_modeling=True)
    build_zipapp(str(b), include_modeling=True)
    assert digest(a) == digest(b)
    assert os.access(a, os.X_OK)
    assert run([str(a), "--help"], tmp_path).returncode == 0
    meta = zipapp_metadata(str(a))
    assert meta["format"] == "llm-resource-tally-zipapp/v1"
    assert meta["tool_format"] == "zipapp"
    assert meta["modeling_included"] is True
    assert zipapp_tool_format(str(a)) == "zipapp"
    assert compression_types(a) == {zipfile.ZIP_STORED}
    with zipfile.ZipFile(a) as zf:
        assert "llm_resource_tally/modeling/assumptions/generic-wide-pack.json" in zf.namelist()
        assert "llm_resource_tally/VERSION" in zf.namelist()

def test_deflated_zipapp_is_reproducible_and_executable(tmp_path):
    from llm_resource_tally.zipapp_artifact import (
        build_zipapp,
        zipapp_metadata,
        zipapp_tool_format,
    )

    a, b = tmp_path / "a-deflate.pyz", tmp_path / "b-deflate.pyz"
    build_zipapp(str(a), include_modeling=True, tool_format="zipapp-deflate")
    build_zipapp(str(b), include_modeling=True, tool_format="zipapp-deflate")
    assert digest(a) == digest(b)
    assert os.access(a, os.X_OK)
    assert run([str(a), "--help"], tmp_path).returncode == 0
    assert zipapp_tool_format(str(a)) == "zipapp-deflate"
    assert zipapp_metadata(str(a))["tool_format"] == "zipapp-deflate"
    assert compression_types(a) == {zipfile.ZIP_DEFLATED}

def test_full_zipapp_loads_bundled_modeling_resources(tmp_path):
    from llm_resource_tally.zipapp_artifact import build_zipapp

    repo = tmp_path / "repo"
    init_repo(repo)
    app = tmp_path / "full.pyz"
    build_zipapp(str(app), include_modeling=True)
    result = run([str(app), "estimate", "--pack", "generic-wide", "--mitigation", "--format", "json"], repo)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["pack_version"] == "generic-wide-v1"
    assert "biochar_carbon_removal" in data["mitigation"]["price_scenarios"]
    grid = run(
        [str(app), "estimate", "--pack", "grid-codecarbon", "--region", "USA", "--format", "json"], repo
    )
    assert grid.returncode == 0, grid.stderr
    assert json.loads(grid.stdout)["grid_model"] == "region USA"

def test_fresh_install_defaults_to_minimal_zipapp(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    result = run([sys.executable, "-B", str(REPO), "install", "--hook-mode", "none"], repo)
    assert result.returncode == 0, result.stderr
    app = repo / ".llm_resource_tally" / "tool"
    assert app.is_file()
    assert "tool format: zipapp" in result.stdout
    assert compression_types(app) == {zipfile.ZIP_STORED}
    with zipfile.ZipFile(app) as zf:
        assert "llm_resource_tally/modeling/estimate.py" not in zf.namelist()
    help_result = run([sys.executable, "-B", str(app), "--help"], repo)
    assert help_result.returncode == 0
    estimate = run([sys.executable, "-B", str(app), "estimate"], repo)
    assert estimate.returncode != 0
    assert "install --modeling" in estimate.stderr + estimate.stdout
    agents = (repo / "AGENTS.md").read_text()
    assert "python3 .llm_resource_tally/tool" in agents

def test_zipapp_compression_format_can_switch_in_place(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    first = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "install",
            "--tool-format",
            "zipapp-deflate",
            "--hook-mode",
            "none",
        ],
        repo,
    )
    assert first.returncode == 0, first.stderr
    tool = repo / ".llm_resource_tally" / "tool"
    assert compression_types(tool) == {zipfile.ZIP_DEFLATED}
    settings = json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())
    assert settings["installation"]["tool_format"] == "zipapp-deflate"

    stored = run(
        [sys.executable, "-B", str(tool), "install", "--tool-format", "zipapp", "--hook-mode", "none"],
        repo,
    )
    assert stored.returncode == 0, stored.stderr
    assert compression_types(tool) == {zipfile.ZIP_STORED}
    settings = json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())
    assert settings["installation"]["tool_format"] == "zipapp"

    deflated = run(
        [
            sys.executable,
            "-B",
            str(tool),
            "install",
            "--tool-format",
            "zipapp-deflate",
            "--hook-mode",
            "none",
        ],
        repo,
    )
    assert deflated.returncode == 0, deflated.stderr
    assert compression_types(tool) == {zipfile.ZIP_DEFLATED}

def test_source_format_remains_available(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    result = run(
        [sys.executable, "-B", str(REPO), "install", "--tool-format", "source", "--hook-mode", "none"], repo
    )
    assert result.returncode == 0, result.stderr
    tool = repo / ".llm_resource_tally" / "tool"
    assert tool.is_dir() and (tool / "__main__.py").is_file()
    assert not (repo / ".llm_resource_tally" / "tool.pyz").exists()
    assert "tool format: source" in result.stdout

def test_zipapp_can_install_itself_at_invariant_path(tmp_path):
    from llm_resource_tally.zipapp_artifact import build_zipapp

    app = tmp_path / "source.pyz"
    build_zipapp(str(app), include_modeling=True)
    repo = tmp_path / "repo"
    init_repo(repo)
    result = run([str(app), "install", "--tool-format", "zipapp", "--modeling", "--hook-mode", "none"], repo)
    assert result.returncode == 0, result.stderr
    copied = repo / ".llm_resource_tally" / "tool"
    assert copied.is_file()
    assert run([sys.executable, "-B", str(copied), "estimate", "--format", "json"], repo).returncode == 0
    assert not (repo / ".llm_resource_tally" / "tool.pyz").exists()

def test_source_to_zipapp_conversion_keeps_invariant_invocation(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    first = run([sys.executable, "-B", str(REPO), "install", "--tool-format", "source"], repo)
    assert first.returncode == 0, first.stderr
    tool = repo / ".llm_resource_tally" / "tool"
    assert tool.is_dir()
    assert git(["config", "--get", "core.hooksPath"], repo).returncode != 0

    converted = run([sys.executable, "-B", str(tool), "install", "--tool-format", "zipapp"], repo)
    assert converted.returncode == 0, converted.stderr
    assert tool.is_file()
    assert run([sys.executable, "-B", str(tool), "doctor"], repo).returncode == 0
    assert git(["config", "--get", "core.hooksPath"], repo).returncode != 0
    hook = (repo / ".git" / "hooks" / "post-commit").read_text()
    assert '$root/.llm_resource_tally/tool" record' in hook
    assert "tool.pyz" not in hook
    assert "python3 .llm_resource_tally/tool" in (repo / "AGENTS.md").read_text()
    assert not (repo / ".llm_resource_tally" / "tool.pyz").exists()

def test_zipapp_to_source_conversion_keeps_invariant_invocation(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    first = run([sys.executable, "-B", str(REPO), "install", "--tool-format", "zipapp", "--modeling"], repo)
    assert first.returncode == 0, first.stderr
    tool = repo / ".llm_resource_tally" / "tool"
    assert tool.is_file()

    converted = run(
        [sys.executable, "-B", str(tool), "install", "--tool-format", "source", "--modeling"], repo
    )
    assert converted.returncode == 0, converted.stderr
    assert tool.is_dir() and (tool / "__main__.py").is_file()
    assert (tool / "modeling" / "estimate.py").is_file()
    assert run([sys.executable, "-B", str(tool), "doctor"], repo).returncode == 0
    assert not (repo / ".llm_resource_tally" / "tool.pyz").exists()

def test_legacy_worktree_hook_path_migrates_to_git_hooks(tmp_path):
    for style in ("relative", "absolute"):
        repo = tmp_path / f"repo-{style}"
        init_repo(repo)
        first = run([sys.executable, "-B", str(REPO), "install", "--tool-format", "source"], repo)
        assert first.returncode == 0, first.stderr
        tool = repo / ".llm_resource_tally" / "tool"
        sibling = repo / ".llm_resource_tally" / "hooks"
        legacy = tool / "hooks"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "post-commit").write_text(
            "#!/usr/bin/env bash\n# llm_resource_tally post-commit — best-effort measured usage recording.\n"
        )
        (legacy / "post-commit").chmod(0o755)
        configured = ".llm_resource_tally/tool/hooks" if style == "relative" else str(legacy)
        assert git(["config", "core.hooksPath", configured], repo).returncode == 0
        if sibling.exists():
            import shutil

            shutil.rmtree(sibling)

        converted = run([sys.executable, "-B", str(tool), "install", "--tool-format", "zipapp"], repo)
        assert converted.returncode == 0, converted.stderr
        assert git(["config", "--get", "core.hooksPath"], repo).returncode != 0
        assert not sibling.exists()
        assert (repo / ".git" / "hooks" / "post-commit").is_file()
        assert tool.is_file()

def test_dirty_source_does_not_claim_clean_commit(tmp_path):
    from llm_resource_tally.zipapp_artifact import _source_commit

    repo = tmp_path / "source-repo"
    package = repo / "llm_resource_tally"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n")
    (repo / "VERSION").write_text("0.0.0\n")
    assert git(["init", "-q"], repo).returncode == 0
    git(["config", "user.email", "t@t"], repo)
    git(["config", "user.name", "t"], repo)
    git(["add", "-A"], repo)
    assert git(["commit", "-qm", "source"], repo).returncode == 0
    head = git(["rev-parse", "HEAD"], repo).stdout.strip()

    assert _source_commit(str(package)) == head
    (package / "__init__.py").write_text("VALUE = 2\n")
    assert _source_commit(str(package)) is None


def test_repository_tracked_zipapp_matches_source_tree():
    from llm_resource_tally.zipapp_artifact import _source_tree_digest, zipapp_metadata

    tool = REPO / ".llm_resource_tally" / "tool"
    assert tool.is_file(), "the repository-owned self-recorder zipapp must be present"
    metadata = zipapp_metadata(str(tool))
    assert metadata["modeling_included"] is True
    assert metadata["source_tree_sha256"] == _source_tree_digest(
        str(REPO / "llm_resource_tally"), include_modeling=True
    )
