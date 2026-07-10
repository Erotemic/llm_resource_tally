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
    return subprocess.run(args, cwd=cwd, env={**os.environ, **(env or {})},
                          capture_output=True, text=True)


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


def test_zipapp_build_is_reproducible_and_executable(tmp_path):
    from llm_resource_tally.zipapp_artifact import build_zipapp, zipapp_metadata

    a, b = tmp_path / "a.pyz", tmp_path / "b.pyz"
    build_zipapp(str(a), include_modeling=True)
    build_zipapp(str(b), include_modeling=True)
    assert digest(a) == digest(b)
    assert os.access(a, os.X_OK)
    assert run([str(a), "--help"], tmp_path).returncode == 0
    meta = zipapp_metadata(str(a))
    assert meta["format"] == "llm-resource-tally-zipapp/v1"
    assert meta["modeling_included"] is True
    with zipfile.ZipFile(a) as zf:
        assert "llm_resource_tally/modeling/assumptions/generic-wide-pack.json" in zf.namelist()
        assert "llm_resource_tally/VERSION" in zf.namelist()


def test_full_zipapp_loads_bundled_modeling_resources(tmp_path):
    from llm_resource_tally.zipapp_artifact import build_zipapp

    repo = tmp_path / "repo"; init_repo(repo)
    app = tmp_path / "full.pyz"
    build_zipapp(str(app), include_modeling=True)
    result = run([str(app), "estimate", "--pack", "generic-wide", "--mitigation",
                  "--format", "json"], repo)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["pack_version"] == "generic-wide-v1"
    assert "biochar_carbon_removal" in data["mitigation"]["price_scenarios"]
    grid = run([str(app), "estimate", "--pack", "grid-codecarbon", "--region", "USA",
                "--format", "json"], repo)
    assert grid.returncode == 0, grid.stderr
    assert json.loads(grid.stdout)["grid_model"] == "region USA"


def test_fresh_install_defaults_to_minimal_zipapp(tmp_path):
    repo = tmp_path / "repo"; init_repo(repo)
    result = run([sys.executable, "-B", str(REPO), "install", "--hook-mode", "none"], repo)
    assert result.returncode == 0, result.stderr
    app = repo / ".llm_resource_tally" / "tool.pyz"
    assert app.is_file()
    assert not (repo / ".llm_resource_tally" / "tool").exists()
    assert "tool format: zipapp" in result.stdout
    with zipfile.ZipFile(app) as zf:
        assert "llm_resource_tally/modeling/estimate.py" not in zf.namelist()
    help_result = run([sys.executable, "-B", str(app), "--help"], repo)
    assert help_result.returncode == 0
    estimate = run([sys.executable, "-B", str(app), "estimate"], repo)
    assert estimate.returncode != 0
    assert "install --modeling" in estimate.stderr + estimate.stdout
    agents = (repo / "AGENTS.md").read_text()
    assert "python3 .llm_resource_tally/tool.pyz" in agents


def test_source_format_remains_available(tmp_path):
    repo = tmp_path / "repo"; init_repo(repo)
    result = run([sys.executable, "-B", str(REPO), "install", "--tool-format", "source",
                  "--hook-mode", "none"], repo)
    assert result.returncode == 0, result.stderr
    tool = repo / ".llm_resource_tally" / "tool"
    assert tool.is_dir() and (tool / "__main__.py").is_file()
    assert not (repo / ".llm_resource_tally" / "tool.pyz").exists()
    assert "tool format: source" in result.stdout


def test_zipapp_can_copy_itself_to_another_repo(tmp_path):
    from llm_resource_tally.zipapp_artifact import build_zipapp

    app = tmp_path / "source.pyz"
    build_zipapp(str(app), include_modeling=True)
    repo = tmp_path / "repo"; init_repo(repo)
    result = run([str(app), "install", "--dir", "tools/tally.pyz",
                  "--tool-format", "zipapp", "--hook-mode", "none"], repo)
    assert result.returncode == 0, result.stderr
    copied = repo / "tools" / "tally.pyz"
    assert copied.is_file() and digest(copied) == digest(app)
    assert run([str(copied), "estimate", "--format", "json"], repo).returncode == 0


def test_source_to_zipapp_conversion_migrates_shared_hook(tmp_path):
    repo = tmp_path / "repo"; init_repo(repo)
    first = run([sys.executable, "-B", str(REPO), "install", "--tool-format", "source"], repo)
    assert first.returncode == 0, first.stderr
    source = repo / ".llm_resource_tally" / "tool"
    assert git(["config", "--get", "core.hooksPath"], repo).stdout.strip() == ".llm_resource_tally/tool/hooks"

    converted = run([sys.executable, "-B", str(source), "install",
                     "--tool-format", "zipapp"], repo)
    assert converted.returncode == 0, converted.stderr
    assert "migrated core.hooksPath" in converted.stdout
    assert git(["config", "--get", "core.hooksPath"], repo).stdout.strip() == ".llm_resource_tally/hooks"
    app = repo / ".llm_resource_tally" / "tool.pyz"
    assert app.is_file()
    hook = (repo / ".llm_resource_tally" / "hooks" / "post-commit").read_text()
    assert "tool.pyz" in hook and '$root/.llm_resource_tally/tool" record' not in hook
    assert "python3 .llm_resource_tally/tool.pyz" in (repo / "AGENTS.md").read_text()
