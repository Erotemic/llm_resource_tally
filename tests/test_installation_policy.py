# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_explicit_install_policy_is_persisted_and_reused(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    first = run([sys.executable, "-B", str(REPO), "install",
                 "--tool-format", "source", "--storage", "ignored", "--modeling",
                 "--hook-mode", "none"], repo)
    assert first.returncode == 0, first.stderr
    settings_path = repo / ".llm_resource_tally" / "settings.json"
    settings = json.loads(settings_path.read_text())
    assert settings["installation"] == {
        "modeling": True,
        "storage": "ignored",
        "tool_format": "source",
        "tool_path": ".llm_resource_tally/tool",
    }
    assert (repo / ".llm_resource_tally" / "tool" / "modeling" / "estimate.py").is_file()
    assert git(["check-ignore", "-q", ".llm_resource_tally/tool"], repo).returncode == 0
    assert git(["check-ignore", "-q", ".llm_resource_tally/settings.json"], repo).returncode != 0

    # No policy flags: the committed settings file is the default.
    second = run([sys.executable, "-B", str(REPO), "install", "--hook-mode", "none"], repo)
    assert second.returncode == 0, second.stderr
    assert "tool format: source" in second.stdout
    assert "storage    : ignored" in second.stdout
    assert json.loads(settings_path.read_text())["installation"] == settings["installation"]


def test_install_can_change_format_and_storage_policy(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    first = run([sys.executable, "-B", str(REPO), "install",
                 "--tool-format", "source", "--storage", "committed",
                 "--no-modeling", "--hook-mode", "none"], repo)
    assert first.returncode == 0, first.stderr
    assert (repo / ".llm_resource_tally" / "tool").is_dir()
    git(["add", "-A"], repo)
    git(["commit", "-qm", "commit tally install"], repo)

    converted = run([sys.executable, "-B", str(REPO), "install",
                     "--tool-format", "zipapp", "--storage", "ignored",
                     "--modeling", "--hook-mode", "none"], repo)
    assert converted.returncode == 0, converted.stderr
    assert (repo / ".llm_resource_tally" / "tool.pyz").is_file()
    assert not (repo / ".llm_resource_tally" / "tool").exists()
    policy = json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())["installation"]
    assert policy["tool_format"] == "zipapp"
    assert policy["storage"] == "ignored"
    assert policy["modeling"] is True
    tracked = git(["ls-files", ".llm_resource_tally"], repo).stdout.splitlines()
    assert tracked == [".llm_resource_tally/settings.json"]


def test_update_forwards_explicit_policy_to_bootstrap(tmp_path, monkeypatch):
    from llm_resource_tally import install
    from llm_resource_tally.config import set_installation_policy

    repo = tmp_path / "repo"
    init_repo(repo)
    set_installation_policy(root=str(repo), storage="committed", tool_format="source",
                            tool_path=".llm_resource_tally/tool", modeling=False)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(install, "repo_root", lambda: str(repo))
    monkeypatch.setattr(install, "rel_dir", lambda root: ".llm_resource_tally/tool")
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/curl" if name == "curl" else None)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    args = SimpleNamespace(repo="Erotemic/llm_resource_tally", ref="main", dir=None,
                           tool_format="zipapp", storage="ignored", modeling=True)
    install.cmd_update(args)
    assert len(calls) == 1
    env = calls[0][1]["env"]
    assert env["RT_TOOL_FORMAT"] == "zipapp"
    assert env["RT_DIR"] == ".llm_resource_tally/tool.pyz"
    assert env["RT_STORAGE"] == "ignored"
    assert env["RT_MODELING"] == "1"


def test_bootstrap_uses_committed_policy_on_fresh_workstation(tmp_path):
    repo = tmp_path / "host"
    init_repo(repo)
    policy_dir = repo / ".llm_resource_tally"
    policy_dir.mkdir()
    (policy_dir / "settings.json").write_text(json.dumps({
        "backends": ["claude", "codex"],
        "installation": {
            "storage": "ignored",
            "tool_format": "source",
            "tool_path": ".llm_resource_tally/tool",
            "modeling": True,
        },
    }, indent=2) + "\n")
    git(["add", ".llm_resource_tally/settings.json"], repo)
    git(["commit", "-qm", "add tally policy"], repo)

    archive_root = tmp_path / "archive-root" / "llm_resource_tally-main"
    shutil.copytree(REPO, archive_root, ignore=shutil.ignore_patterns(
        ".git", ".pytest_cache", "__pycache__", "*.pyc"))
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(archive_root, arcname=archive_root.name)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\ncat \"$FAKE_ARCHIVE\"\n")
    fake_curl.chmod(0o755)
    env = {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "FAKE_ARCHIVE": str(archive),
    }
    result = run(["sh", str(REPO / "install.sh")], repo, env)
    assert result.returncode == 0, result.stderr
    assert (repo / ".llm_resource_tally" / "tool" / "modeling" / "estimate.py").is_file()
    settings = json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())
    assert settings["installation"]["storage"] == "ignored"
    assert settings["installation"]["tool_format"] == "source"
    assert settings["installation"]["modeling"] is True
    assert git(["check-ignore", "-q", ".llm_resource_tally/tool"], repo).returncode == 0
    assert git(["check-ignore", "-q", ".llm_resource_tally/settings.json"], repo).returncode != 0
