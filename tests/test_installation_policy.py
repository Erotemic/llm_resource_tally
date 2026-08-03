# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

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

def test_bootstrap_help_is_non_mutating_and_dependency_free(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    env = {"PATH": ""}
    result = subprocess.run(
        ["/bin/sh", str(REPO / "install.sh"), "--help"], cwd=repo, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "Usage: install.sh [--help]" in result.stdout
    assert "RT_TOOL_FORMAT" in result.stdout
    assert not (repo / ".llm_resource_tally").exists()

def test_bootstrap_short_help_is_non_mutating(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    result = run(["/bin/sh", str(REPO / "install.sh"), "-h"], repo)
    assert result.returncode == 0, result.stderr
    assert "show this help and exit without changing anything" in result.stdout
    assert not (repo / ".llm_resource_tally").exists()

def test_bootstrap_rejects_unknown_arguments_before_installation(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    env = {"PATH": ""}
    result = subprocess.run(
        ["/bin/sh", str(REPO / "install.sh"), "--unexpected"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unknown argument: --unexpected" in result.stderr
    assert "try --help" in result.stderr
    assert not (repo / ".llm_resource_tally").exists()

def test_explicit_install_policy_is_persisted_and_reused(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    first = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "install",
            "--tool-format",
            "source",
            "--storage",
            "ignored",
            "--modeling",
            "--hook-mode",
            "none",
        ],
        repo,
    )
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
    first = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "install",
            "--tool-format",
            "source",
            "--storage",
            "committed",
            "--no-modeling",
            "--hook-mode",
            "none",
        ],
        repo,
    )
    assert first.returncode == 0, first.stderr
    assert (repo / ".llm_resource_tally" / "tool").is_dir()
    git(["add", "-A"], repo)
    git(["commit", "-qm", "commit tally install"], repo)

    converted = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "install",
            "--tool-format",
            "zipapp",
            "--storage",
            "ignored",
            "--modeling",
            "--hook-mode",
            "none",
        ],
        repo,
    )
    assert converted.returncode == 0, converted.stderr
    assert (repo / ".llm_resource_tally" / "tool").is_file()
    assert not (repo / ".llm_resource_tally" / "tool.pyz").exists()
    policy = json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())["installation"]
    assert policy["tool_format"] == "zipapp"
    assert policy["tool_path"] == ".llm_resource_tally/tool"
    assert policy["storage"] == "ignored"
    assert policy["modeling"] is True
    tracked = git(["ls-files", ".llm_resource_tally"], repo).stdout.splitlines()
    assert tracked == [".llm_resource_tally/settings.json"]

def test_update_forwards_explicit_policy_to_bootstrap(tmp_path, monkeypatch):
    from llm_resource_tally import install
    from llm_resource_tally.config import set_installation_policy

    repo = tmp_path / "repo"
    init_repo(repo)
    set_installation_policy(
        root=str(repo),
        storage="committed",
        tool_format="source",
        tool_path=".llm_resource_tally/tool",
        modeling=False,
    )
    monkeypatch.chdir(repo)
    monkeypatch.setattr(install, "repo_root", lambda: str(repo))
    monkeypatch.setattr(install, "rel_dir", lambda root: ".llm_resource_tally/tool")
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/curl" if name == "curl" else None)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    args = SimpleNamespace(
        repo="Erotemic/llm_resource_tally",
        ref="main",
        tool_format="zipapp-deflate",
        storage="ignored",
        modeling=True,
    )
    install.cmd_update(args)
    assert len(calls) == 1
    env = calls[0][1]["env"]
    assert env["RT_TOOL_FORMAT"] == "zipapp-deflate"
    assert "RT_DIR" not in env
    assert env["RT_STORAGE"] == "ignored"
    assert env["RT_MODELING"] == "1"

def test_bootstrap_uses_committed_policy_on_fresh_workstation(tmp_path):
    repo = tmp_path / "host"
    init_repo(repo)
    policy_dir = repo / ".llm_resource_tally"
    policy_dir.mkdir()
    (policy_dir / "settings.json").write_text(
        json.dumps(
            {
                "backends": ["claude", "codex"],
                "installation": {
                    "storage": "ignored",
                    "tool_format": "source",
                    "tool_path": ".llm_resource_tally/tool",
                    "modeling": True,
                },
            },
            indent=2,
        )
        + "\n"
    )
    git(["add", ".llm_resource_tally/settings.json"], repo)
    git(["commit", "-qm", "add tally policy"], repo)

    archive_root = tmp_path / "archive-root" / "llm_resource_tally-main"
    shutil.copytree(
        REPO, archive_root, ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "*.pyc")
    )
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(archive_root, arcname=archive_root.name)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text('#!/bin/sh\ncat "$FAKE_ARCHIVE"\n')
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

def test_bootstrap_default_zipapp_uses_invariant_path(tmp_path):
    repo = tmp_path / "host"
    init_repo(repo)

    archive_root = tmp_path / "archive-root" / "llm_resource_tally-main"
    shutil.copytree(
        REPO, archive_root, ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "*.pyc")
    )
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(archive_root, arcname=archive_root.name)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text('#!/bin/sh\ncat "$FAKE_ARCHIVE"\n')
    fake_curl.chmod(0o755)
    env = {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "FAKE_ARCHIVE": str(archive),
    }
    result = run(["sh", str(REPO / "install.sh")], repo, env)
    assert result.returncode == 0, result.stderr
    tool = repo / ".llm_resource_tally" / "tool"
    assert tool.is_file()
    assert not (repo / ".llm_resource_tally" / "tool.pyz").exists()
    assert run([sys.executable, "-B", str(tool), "--help"], repo).returncode == 0
    settings = json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())
    assert settings["installation"]["tool_format"] == "zipapp"
    assert settings["installation"]["tool_path"] == ".llm_resource_tally/tool"
    assert settings["installation"]["storage"] == "local"
    assert git(["check-ignore", "-q", ".llm_resource_tally/local/ledger.jsonl"], repo).returncode == 0

def test_bootstrap_zipapp_deflate_override(tmp_path):
    repo = tmp_path / "host"
    init_repo(repo)

    archive_root = tmp_path / "archive-root" / "llm_resource_tally-main"
    shutil.copytree(
        REPO, archive_root, ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "*.pyc")
    )
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(archive_root, arcname=archive_root.name)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text('#!/bin/sh\ncat "$FAKE_ARCHIVE"\n')
    fake_curl.chmod(0o755)
    env = {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "FAKE_ARCHIVE": str(archive),
        "RT_TOOL_FORMAT": "zipapp-deflate",
    }
    result = run(["sh", str(REPO / "install.sh")], repo, env)
    assert result.returncode == 0, result.stderr
    tool = repo / ".llm_resource_tally" / "tool"
    with zipfile.ZipFile(tool) as zf:
        assert {info.compress_type for info in zf.infolist() if not info.is_dir()} == {
            zipfile.ZIP_DEFLATED
        }
    settings = json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())
    assert settings["installation"]["tool_format"] == "zipapp-deflate"

def test_local_storage_keeps_rollup_tracked(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    first = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "install",
            "--tool-format",
            "source",
            "--storage",
            "committed",
            "--no-modeling",
            "--hook-mode",
            "none",
        ],
        repo,
    )
    assert first.returncode == 0, first.stderr
    tally = repo / ".llm_resource_tally"
    (tally / "lifetime-totals.json").write_text('{"turns": 1}\n')
    git(["add", "-A"], repo)
    git(["commit", "-qm", "commit tally rollup"], repo)

    converted = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"], repo
    )
    assert converted.returncode == 0, converted.stderr
    # switching to local must not untrack committed accounting: `publish` refreshes these in place
    assert (tally / "lifetime-totals.json").read_text() == '{"turns": 1}\n'
    tracked = git(["ls-files", "--", ".llm_resource_tally"], repo).stdout
    assert ".llm_resource_tally/lifetime-totals.json" in tracked
    assert git(["check-ignore", "-q", ".llm_resource_tally/local/ledger.jsonl"], repo).returncode == 0
    assert git(["status", "--short"], repo).stdout.count("D  .llm_resource_tally") == 0

def test_upgrade_does_not_flip_a_committed_repo_to_local(tmp_path):
    """A settings.json predating the installation block must not silently freeze a tracked ledger."""
    repo = tmp_path / "repo"
    init_repo(repo)
    tally = repo / ".llm_resource_tally"
    (tally / "ledger").mkdir(parents=True)
    (tally / "settings.json").write_text('{"backends": ["claude"]}\n')
    (tally / "ledger" / "ledger.jsonl").write_text('{"schema": "x", "commit": "abc"}\n')
    git(["add", "-A"], repo)
    git(["commit", "-qm", "pre-policy tally install"], repo)

    r = run([sys.executable, "-B", str(REPO), "install", "--hook-mode", "none"], repo)
    assert r.returncode == 0, r.stderr
    settings = json.loads((tally / "settings.json").read_text())
    assert settings["installation"]["storage"] == "committed"
    assert (tally / "ledger" / "ledger.jsonl").read_text() == '{"schema": "x", "commit": "abc"}\n'
    assert ".llm_resource_tally/ledger/ledger.jsonl" in git(["ls-files"], repo).stdout

def test_leaving_local_mode_drains_the_spool(tmp_path):
    """Unpublished rows would otherwise be visible only on this machine, forever."""
    repo = tmp_path / "repo"
    init_repo(repo)
    r = run([sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"], repo)
    assert r.returncode == 0, r.stderr
    spool = repo / ".llm_resource_tally" / "local" / "ledger.jsonl"
    row = '{"v":3,"rec":"2026-01-01T00:00:00+00:00","c":"abc","a":"claude-code","sid":"s1"}\n'
    spool.write_text(row)

    switched = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "committed", "--hook-mode", "none"], repo
    )
    assert switched.returncode == 0, switched.stderr
    assert "drained" in switched.stdout
    assert spool.read_text() == ""
    assert (repo / ".llm_resource_tally" / "ledger" / "ledger.jsonl").read_text() == row

def test_session_end_hook_publishes_as_a_backstop(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    r = run([sys.executable, "-B", str(REPO), "install", "--claude", "--hook-mode", "none"], repo)
    assert r.returncode == 0, r.stderr
    hooks = json.loads((repo / ".claude" / "settings.json").read_text())["hooks"]
    end = hooks["SessionEnd"][0]["hooks"][0]["command"]
    assert " reconcile " in end and " rollup " in end and " publish " in end
    assert end.index("reconcile") < end.index("rollup") < end.index("publish")

def test_fresh_repo_still_defaults_to_local(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    r = run([sys.executable, "-B", str(REPO), "install", "--hook-mode", "none"], repo)
    assert r.returncode == 0, r.stderr
    settings = json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())
    assert settings["installation"]["storage"] == "local"

def test_install_respects_custom_core_hookspath(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    custom = repo / ".custom-hooks"
    custom.mkdir()
    git(["config", "core.hooksPath", ".custom-hooks"], repo)
    result = run([sys.executable, "-B", str(REPO), "install", "--tool-format", "source"], repo)
    assert result.returncode == 0, result.stderr
    assert git(["config", "--get", "core.hooksPath"], repo).stdout.strip() == ".custom-hooks"
    hook = custom / "post-commit"
    assert hook.is_file()
    assert "llm_resource_tally" in hook.read_text()
    assert not (repo / ".llm_resource_tally" / "hooks").exists()

def test_publish_refreshes_the_aggregate_in_every_storage_mode(tmp_path):
    """Where rows live is independent of whether the repo carries a readable aggregate."""
    from llm_resource_tally.config import STORAGE_MODES

    for mode in STORAGE_MODES:
        repo = tmp_path / f"repo-{mode}"
        init_repo(repo)
        r = run(
            [sys.executable, "-B", str(REPO), "install", "--storage", mode, "--hook-mode", "none"],
            repo,
        )
        assert r.returncode == 0, r.stderr
        p = run([sys.executable, "-B", str(REPO), "publish"], repo)
        assert p.returncode == 0, p.stderr
        tally = repo / ".llm_resource_tally"
        assert (tally / "lifetime-totals.json").is_file(), f"{mode}: no published rollup"
        if mode == "notes":
            assert "ledger and reports" not in p.stdout, "notes mode has no tracked ledger to name"

def test_switching_local_to_notes_moves_rows_into_notes(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    assert (
        run(
            [sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"], repo
        ).returncode
        == 0
    )
    spool = repo / ".llm_resource_tally" / "local" / "ledger.jsonl"
    spool.write_text('{"v":3,"rec":"2026-01-01T00:00:00+00:00","c":"abc","a":"claude-code","sid":"s1"}\n')

    switched = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "notes", "--hook-mode", "none"], repo
    )
    assert switched.returncode == 0, switched.stderr
    assert "into refs/notes/llm-resource-tally" in switched.stdout
    assert spool.read_text() == ""
    # rows went to notes, not to a tracked JSONL shard
    assert not list((repo / ".llm_resource_tally" / "ledger").glob("*.jsonl"))
    listing = git(["notes", "--ref=refs/notes/llm-resource-tally", "list"], repo).stdout
    assert listing.strip(), "rows should be reachable from the notes ref"
