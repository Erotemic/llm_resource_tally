# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import re
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
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if isinstance(command, list) and command and command[0] == "git":
            return real_run(command, **kwargs)
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

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
    assert not spool.exists()
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
    assert settings["publication"] == {
        "append_ledger_dir": ".llm_resource_tally/ledger",
        "lifetime_totals_path": ".llm_resource_tally/lifetime-totals.json",
    }

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
    assert not spool.exists()
    # rows went to notes, not to a tracked JSONL shard
    assert not list((repo / ".llm_resource_tally" / "ledger").glob("*.jsonl"))
    listing = git(["notes", "--ref=refs/notes/llm-resource-tally", "list"], repo).stdout
    assert listing.strip(), "rows should be reachable from the notes ref"

@pytest.mark.parametrize(
    ("old_mode", "new_mode"),
    [
        ("committed", "local"),
        ("local", "committed"),
        ("local", "notes"),
        ("notes", "local"),
        ("ignored", "local"),
        ("local", "ignored"),
    ],
)
def test_config_set_storage_transition_matrix(tmp_path, old_mode, new_mode):
    repo = tmp_path / f"{old_mode}-to-{new_mode}"
    init_repo(repo)
    installed = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "install",
            "--tool-format",
            "source",
            "--storage",
            old_mode,
            "--hook-mode",
            "none",
        ],
        repo,
    )
    assert installed.returncode == 0, installed.stderr

    switched = run(
        [sys.executable, "-B", str(REPO), "config", "set", "--storage", new_mode],
        repo,
    )
    assert switched.returncode == 0, switched.stderr
    assert f"storage {old_mode} -> {new_mode}" in switched.stdout
    settings = json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())
    assert settings["installation"]["storage"] == new_mode


def test_config_show_reports_effective_policy_and_defaults(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    tally = repo / ".llm_resource_tally"
    tally.mkdir()
    (tally / "settings.json").write_text(
        json.dumps(
            {
                "installation": {
                    "tool_format": "source",
                    "tool_path": ".llm_resource_tally/tool",
                    "modeling": False,
                }
            },
            indent=2,
        )
        + "\n"
    )

    shown = run([sys.executable, "-B", str(REPO), "config", "show"], repo)
    assert shown.returncode == 0, shown.stderr
    assert "settings_file: .llm_resource_tally/settings.json" in shown.stdout
    assert "storage: local (default)" in shown.stdout
    assert "tool_format: source (settings)" in shown.stdout
    assert "recorder_backends: claude, codex (default)" in shown.stdout
    assert "append_ledger_dir: .llm_resource_tally/ledger (default)" in shown.stdout
    assert (
        "lifetime_totals_path: .llm_resource_tally/lifetime-totals.json (default)"
        in shown.stdout
    )

    shown_json = run([sys.executable, "-B", str(REPO), "config", "show", "--json"], repo)
    assert shown_json.returncode == 0, shown_json.stderr
    payload = json.loads(shown_json.stdout)
    assert payload["storage"] == "local"
    assert payload["sources"]["storage"] == "default"
    assert payload["tool_format"] == "source"
    assert payload["append_ledger_dir"] == ".llm_resource_tally/ledger"
    assert payload["lifetime_totals_path"] == ".llm_resource_tally/lifetime-totals.json"
    assert payload["sources"]["append_ledger_dir"] == "default"


def test_config_show_infers_pre_policy_committed_storage(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    tally = repo / ".llm_resource_tally"
    (tally / "ledger").mkdir(parents=True)
    (tally / "settings.json").write_text('{"backends": ["claude"]}\n')
    (tally / "ledger" / "ledger.jsonl").write_text('{"schema": "legacy"}\n')
    git(["add", "-A"], repo)
    git(["commit", "-qm", "legacy tally"], repo)

    shown = run([sys.executable, "-B", str(REPO), "config", "show"], repo)
    assert shown.returncode == 0, shown.stderr
    assert "storage: committed (inferred from tracked accounting)" in shown.stdout


def test_config_set_rejects_invalid_storage(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    result = run(
        [sys.executable, "-B", str(REPO), "config", "set", "--storage", "somewhere"],
        repo,
    )
    assert result.returncode == 2
    assert "invalid choice" in result.stderr
    assert not (repo / ".llm_resource_tally").exists()


def test_config_refuses_to_overwrite_malformed_settings(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    settings = repo / ".llm_resource_tally" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"installation":')
    before = settings.read_bytes()

    shown = run([sys.executable, "-B", str(REPO), "config", "show"], repo)
    changed = run(
        [sys.executable, "-B", str(REPO), "config", "set", "--storage", "local"],
        repo,
    )
    assert shown.returncode != 0 and changed.returncode != 0
    assert "not valid JSON" in shown.stderr
    assert "not valid JSON" in changed.stderr
    assert settings.read_bytes() == before


def test_doctor_reports_malformed_settings_instead_of_using_defaults(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    settings = repo / ".llm_resource_tally" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"publication":')

    checked = run([sys.executable, "-B", str(REPO), "doctor"], repo)
    assert checked.returncode != 0
    assert "repository settings are unreadable/invalid" in checked.stdout
    assert "not valid JSON" in checked.stdout


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"installation": {"storage": "lcoal"}}, "unknown storage mode 'lcoal'"),
        ({"backends": ["claude", "mystery-agent"]}, "unknown recorder backend"),
    ],
)
def test_doctor_rejects_semantically_invalid_settings_instead_of_defaulting(
    tmp_path, payload, expected
):
    repo = tmp_path / "repo"
    init_repo(repo)
    settings = repo / ".llm_resource_tally" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps(payload) + "\n")

    checked = run([sys.executable, "-B", str(REPO), "doctor"], repo)
    assert checked.returncode != 0
    assert expected in checked.stdout


def test_config_set_is_idempotent_without_file_churn(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    installed = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"],
        repo,
    )
    assert installed.returncode == 0, installed.stderr
    paths = [
        repo / ".llm_resource_tally" / "settings.json",
        repo / ".gitignore",
        repo / "AGENTS.md",
    ]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    first = run([sys.executable, "-B", str(REPO), "config", "set", "--storage", "local"], repo)
    second = run([sys.executable, "-B", str(REPO), "config", "set", "--storage", "local"], repo)
    assert first.returncode == 0 and second.returncode == 0
    assert "unchanged: storage is already local" in first.stdout
    assert "unchanged: storage is already local" in second.stdout
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    assert after == before


@pytest.mark.parametrize("destination", ["committed", "ignored", "notes"])
def test_config_set_preserves_pending_local_rows(tmp_path, destination):
    repo = tmp_path / destination
    init_repo(repo)
    installed = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"],
        repo,
    )
    assert installed.returncode == 0, installed.stderr
    spool = repo / ".llm_resource_tally" / "local" / "ledger.jsonl"
    row = '{"v":3,"rec":"2026-01-01T00:00:00+00:00","c":"abc","a":"claude-code","sid":"s1"}\n'
    spool.write_text(row)

    switched = run(
        [sys.executable, "-B", str(REPO), "config", "set", "--storage", destination],
        repo,
    )
    assert switched.returncode == 0, switched.stderr
    assert "drained" in switched.stdout
    assert not (repo / ".llm_resource_tally" / "local").exists()
    if destination == "notes":
        listing = git(["notes", "--ref=refs/notes/llm-resource-tally", "list"], repo)
        assert listing.returncode == 0 and listing.stdout.strip()
        assert not list((repo / ".llm_resource_tally" / "ledger").glob("*.jsonl"))
    else:
        assert (repo / ".llm_resource_tally" / "ledger" / "ledger.jsonl").read_text() == row


def test_config_set_does_not_replace_tool_or_rewire_hooks(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    installed = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "local"],
        repo,
    )
    assert installed.returncode == 0, installed.stderr
    tool = repo / ".llm_resource_tally" / "tool"
    tool_before = (tool.read_bytes(), tool.stat().st_mtime_ns)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(hook.read_text() + "\n# custom hook content\n")
    hook_before = hook.read_bytes()
    claude = repo / ".claude" / "settings.json"
    claude.parent.mkdir()
    claude.write_text('{"custom": true}\n')
    claude_before = claude.read_bytes()
    settings = repo / ".llm_resource_tally" / "settings.json"
    settings_data = json.loads(settings.read_text())
    settings_data["custom_top_level"] = {"keep": True}
    settings_data["installation"]["future_policy_key"] = "keep"
    settings.write_text(json.dumps(settings_data, indent=2, sort_keys=True) + "\n")
    gitignore = repo / ".gitignore"
    gitignore.write_text("# user ignore\n" + gitignore.read_text())
    agents = repo / "AGENTS.md"
    agents.write_text("user guidance\n\n" + agents.read_text())

    switched = run(
        [sys.executable, "-B", str(tool), "config", "set", "--storage", "committed"],
        repo,
    )
    assert switched.returncode == 0, switched.stderr
    assert (tool.read_bytes(), tool.stat().st_mtime_ns) == tool_before
    assert hook.read_bytes() == hook_before
    assert claude.read_bytes() == claude_before
    assert gitignore.read_text().startswith("# user ignore\n")
    assert agents.read_text().startswith("user guidance\n\n")
    settings_after = json.loads(settings.read_text())
    assert settings_after["custom_top_level"] == {"keep": True}
    assert settings_after["installation"]["future_policy_key"] == "keep"


def test_policy_transition_failure_keeps_old_policy_and_spool(tmp_path, monkeypatch):
    from llm_resource_tally import repository_config
    from llm_resource_tally.config import set_installation_policy

    repo = tmp_path / "repo"
    init_repo(repo)
    set_installation_policy(
        root=str(repo),
        storage="local",
        tool_format="source",
        tool_path=".llm_resource_tally/tool",
        modeling=False,
    )
    spool = repo / ".llm_resource_tally" / "local" / "ledger.jsonl"
    spool.parent.mkdir(parents=True)
    spool.write_text('{"v":3,"c":"abc"}\n')
    before = (repo / ".llm_resource_tally" / "settings.json").read_bytes()

    def fail_publish(root):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(repository_config, "publish_local", fail_publish)
    requested = repository_config.effective_installation_policy(str(repo))
    requested["storage"] = "committed"
    with pytest.raises(OSError, match="simulated publication failure"):
        repository_config.apply_installation_policy(str(repo), requested)

    assert (repo / ".llm_resource_tally" / "settings.json").read_bytes() == before
    assert spool.read_text() == '{"v":3,"c":"abc"}\n'


def test_config_set_matches_install_storage_policy_effects(tmp_path):
    converted = tmp_path / "converted"
    direct = tmp_path / "direct"
    init_repo(converted)
    init_repo(direct)
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
            "--hook-mode",
            "none",
        ],
        converted,
    )
    assert first.returncode == 0, first.stderr
    switched = run(
        [sys.executable, "-B", str(REPO), "config", "set", "--storage", "local"],
        converted,
    )
    assert switched.returncode == 0, switched.stderr
    installed = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "install",
            "--tool-format",
            "source",
            "--storage",
            "local",
            "--hook-mode",
            "none",
        ],
        direct,
    )
    assert installed.returncode == 0, installed.stderr

    for rel in [".llm_resource_tally/settings.json", ".gitignore", "AGENTS.md"]:
        assert (converted / rel).read_bytes() == (direct / rel).read_bytes(), rel


def test_config_command_runs_from_source_and_zipapp(tmp_path):
    source_repo = tmp_path / "source"
    zipapp_repo = tmp_path / "zipapp"
    init_repo(source_repo)
    init_repo(zipapp_repo)

    source_install = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "install",
            "--tool-format",
            "source",
            "--storage",
            "committed",
            "--hook-mode",
            "none",
        ],
        source_repo,
    )
    assert source_install.returncode == 0, source_install.stderr
    source_show = run(
        [sys.executable, "-B", str(source_repo / ".llm_resource_tally" / "tool"), "config", "show"],
        source_repo,
    )
    assert source_show.returncode == 0, source_show.stderr
    assert "storage: committed" in source_show.stdout

    zipapp_install = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "install",
            "--tool-format",
            "zipapp",
            "--storage",
            "committed",
            "--hook-mode",
            "none",
        ],
        zipapp_repo,
    )
    assert zipapp_install.returncode == 0, zipapp_install.stderr
    zipapp_tool = zipapp_repo / ".llm_resource_tally" / "tool"
    zipapp_set = run(
        [sys.executable, "-B", str(zipapp_tool), "config", "set", "--storage", "local"],
        zipapp_repo,
    )
    assert zipapp_set.returncode == 0, zipapp_set.stderr
    assert "storage committed -> local" in zipapp_set.stdout


def test_config_help_is_discoverable(tmp_path):
    top = run([sys.executable, "-B", str(REPO), "--help"], tmp_path)
    nested = run([sys.executable, "-B", str(REPO), "config", "--help"], tmp_path)
    setter = run([sys.executable, "-B", str(REPO), "config", "set", "--help"], tmp_path)
    assert top.returncode == nested.returncode == setter.returncode == 0
    assert re.search(
        r"^[ \t]+config[ \t]+inspect or modify repository configuration[ \t]*$",
        top.stdout,
        re.MULTILINE,
    )
    assert "Storage selects how mutable rows are recorded" in nested.stdout
    assert (
        "publication paths select where the durable append ledger and lifetime totals live"
        in nested.stdout
    )
    assert "recorder backends such as Claude and Codex" in nested.stdout
    assert "does not replace the installed tool or rewire Git/Claude hooks" in setter.stdout
    assert "--append-ledger-dir" in setter.stdout
    assert "--lifetime-totals-path" in setter.stdout


def _sample_compact_row(commit: str) -> str:
    return json.dumps(
        {
            "v": 3,
            "rec": "2026-08-08T12:00:00+00:00",
            "r": "repo",
            "c": commit,
            "ct": "2026-08-08T11:59:00+00:00",
            "a": "claude-code",
            "sid": f"session-{commit}",
            "m": ["test-model"],
            "n": 1,
            "t": [1, 0, 0, 2],
            "w": 1.0,
            "tr": ["2026-08-08T11:59:30+00:00", "2026-08-08T11:59:45+00:00"],
        },
        separators=(",", ":"),
    ) + "\n"


def test_config_set_can_redirect_only_the_append_ledger(tmp_path):
    repo = tmp_path / "repo"
    accounting = tmp_path / "accounting"
    init_repo(repo)
    init_repo(accounting)
    installed = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"],
        repo,
    )
    assert installed.returncode == 0, installed.stderr

    configured = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "config",
            "set",
            "--append-ledger-dir",
            "../accounting/ledger",
        ],
        repo,
    )
    assert configured.returncode == 0, configured.stderr
    settings = json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())
    assert settings["publication"] == {
        "append_ledger_dir": "../accounting/ledger",
        "lifetime_totals_path": ".llm_resource_tally/lifetime-totals.json",
    }
    assert (accounting / "ledger" / ".gitattributes").read_text().endswith("*.jsonl merge=union\n")

    git(["add", "-A"], repo)
    git(["commit", "-qm", "configure external append ledger"], repo)
    git(["add", "-A"], accounting)
    git(["commit", "-qm", "configure ledger merge policy"], accounting)

    row = _sample_compact_row("external-append")
    spool = repo / ".llm_resource_tally" / "local" / "ledger.jsonl"
    spool.write_text(row)
    published = run([sys.executable, "-B", str(REPO), "publish"], repo)
    assert published.returncode == 0, published.stderr
    assert (accounting / "ledger" / "ledger.jsonl").read_text() == row
    assert not (repo / ".llm_resource_tally" / "ledger" / "ledger.jsonl").exists()
    assert (repo / ".llm_resource_tally" / "lifetime-totals.json").is_file()
    assert "../accounting/ledger/ledger.jsonl" in published.stdout


def test_config_set_can_redirect_all_durable_publication_outside_main_repo(tmp_path):
    repo = tmp_path / "repo"
    accounting = tmp_path / "accounting"
    init_repo(repo)
    init_repo(accounting)
    installed = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"],
        repo,
    )
    assert installed.returncode == 0, installed.stderr
    configured = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "config",
            "set",
            "--append-ledger-dir",
            "../accounting/ledger",
            "--lifetime-totals-path",
            "../accounting/lifetime-totals.json",
        ],
        repo,
    )
    assert configured.returncode == 0, configured.stderr
    git(["add", "-A"], repo)
    git(["commit", "-qm", "redirect durable tally data"], repo)

    row = _sample_compact_row("all-external")
    (repo / ".llm_resource_tally" / "local" / "ledger.jsonl").write_text(row)
    published = run([sys.executable, "-B", str(REPO), "publish"], repo)
    assert published.returncode == 0, published.stderr
    assert (accounting / "ledger" / "ledger.jsonl").read_text() == row
    totals = json.loads((accounting / "lifetime-totals.json").read_text())
    assert totals["ledger_rows"] == 1
    assert totals["tokens"]["output"] == 2
    assert not (repo / ".llm_resource_tally" / "lifetime-totals.json").exists()
    assert git(["status", "--short"], repo).stdout == ""


def test_redirected_ledger_keeps_default_historical_shards_readable(tmp_path):
    repo = tmp_path / "repo"
    accounting = tmp_path / "accounting"
    init_repo(repo)
    init_repo(accounting)
    installed = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"],
        repo,
    )
    assert installed.returncode == 0, installed.stderr
    historical = repo / ".llm_resource_tally" / "ledger" / "ledger.jsonl"
    historical.write_text(_sample_compact_row("historical"))

    configured = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "config",
            "set",
            "--append-ledger-dir",
            "../accounting/ledger",
        ],
        repo,
    )
    assert configured.returncode == 0, configured.stderr
    (repo / ".llm_resource_tally" / "local" / "ledger.jsonl").write_text(
        _sample_compact_row("redirected")
    )
    published = run([sys.executable, "-B", str(REPO), "publish"], repo)
    assert published.returncode == 0, published.stderr

    shown = run([sys.executable, "-B", str(REPO), "show"], repo)
    assert shown.returncode == 0, shown.stderr
    assert "historical" in shown.stdout
    assert "redirected" in shown.stdout


def test_config_rejects_invalid_publication_path_settings(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    tally = repo / ".llm_resource_tally"
    tally.mkdir()
    (tally / "settings.json").write_text('{"publication":{"append_ledger_dir":""}}\n')

    shown = run([sys.executable, "-B", str(REPO), "config", "show"], repo)
    assert shown.returncode != 0
    assert "append_ledger_dir must be a non-empty path string" in shown.stderr


@pytest.mark.parametrize(
    "target",
    [
        ".llm_resource_tally/ledger/ledger.jsonl",
        ".llm_resource_tally/ledger/summary.json",
    ],
)
def test_config_rejects_lifetime_totals_inside_ledger_directory(tmp_path, target):
    repo = tmp_path / "repo"
    init_repo(repo)
    installed = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"],
        repo,
    )
    assert installed.returncode == 0, installed.stderr
    settings = repo / ".llm_resource_tally" / "settings.json"
    before = settings.read_bytes()

    changed = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "config",
            "set",
            "--lifetime-totals-path",
            target,
        ],
        repo,
    )
    assert changed.returncode != 0
    assert "must be outside the ledger directory" in changed.stderr
    assert settings.read_bytes() == before


def test_config_rejects_lifetime_totals_overwriting_non_tally_file(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    installed = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"],
        repo,
    )
    assert installed.returncode == 0, installed.stderr
    settings = repo / ".llm_resource_tally" / "settings.json"
    before = settings.read_bytes()

    changed = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "config",
            "set",
            "--lifetime-totals-path",
            ".llm_resource_tally/settings.json",
        ],
        repo,
    )
    assert changed.returncode != 0
    assert "would overwrite an existing non-tally file" in changed.stderr
    assert settings.read_bytes() == before


def test_publish_refuses_malformed_local_row_without_clearing_spool(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    installed = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"],
        repo,
    )
    assert installed.returncode == 0, installed.stderr
    spool = repo / ".llm_resource_tally" / "local" / "ledger.jsonl"
    spool.write_text("not-json\n")

    published = run([sys.executable, "-B", str(REPO), "publish"], repo)
    assert published.returncode != 0
    assert "invalid local ledger row" in published.stderr
    assert spool.read_text() == "not-json\n"
    assert not (repo / ".llm_resource_tally" / "ledger" / "ledger.jsonl").exists()


def test_external_publication_lock_is_scoped_to_shared_destination(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    from llm_resource_tally.config import set_publication_policy
    from llm_resource_tally.ledger import published_ledger_lock

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    shared = tmp_path / "accounting" / "ledger"
    init_repo(repo_a)
    init_repo(repo_b)
    for repo in (repo_a, repo_b):
        set_publication_policy(
            root=str(repo),
            append_ledger_dir=str(shared),
            lifetime_totals_path=str(tmp_path / "accounting" / f"{repo.name}-totals.json"),
        )

    with published_ledger_lock(str(repo_a)):
        attributes = shared / ".gitattributes"
        assert attributes.is_file()
        with attributes.open("a+", encoding="utf-8") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    with attributes.open("a+", encoding="utf-8") as contender:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender, fcntl.LOCK_UN)


def test_config_set_rejects_mixed_storage_and_publication_transition(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    installed = run(
        [sys.executable, "-B", str(REPO), "install", "--storage", "local", "--hook-mode", "none"],
        repo,
    )
    assert installed.returncode == 0, installed.stderr
    settings = repo / ".llm_resource_tally" / "settings.json"
    before = settings.read_bytes()

    changed = run(
        [
            sys.executable,
            "-B",
            str(REPO),
            "config",
            "set",
            "--storage",
            "committed",
            "--append-ledger-dir",
            "../accounting/ledger",
        ],
        repo,
    )
    assert changed.returncode != 0
    assert "separate config set commands" in changed.stderr
    assert settings.read_bytes() == before


def test_install_rejects_unknown_backend_before_mutation(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    result = run(
        [sys.executable, "-B", str(REPO), "install", "--backend", "typod", "--hook-mode", "none"],
        repo,
    )
    assert result.returncode != 0
    assert "invalid choice" in result.stderr
    assert not (repo / ".llm_resource_tally").exists()


def test_backend_alias_registration_is_canonical_and_unique(tmp_path):
    from llm_resource_tally.config import register_backend, registered_backends

    repo = tmp_path / "repo"
    init_repo(repo)
    assert register_backend("claude-code", str(repo)) == ["claude", "codex"]
    assert registered_backends(str(repo)) == ["claude", "codex"]
    assert json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())["backends"] == [
        "claude",
        "codex",
    ]


def test_doctor_reports_backend_discovery_failure(monkeypatch, tmp_path):
    import llm_resource_tally.doctor as doctor

    class BrokenBackend:
        name = "broken"

        def default_projects_dir(self):
            return str(tmp_path)

        def find_transcript(self, projects, session, strict=False):
            raise ValueError("transcript index is malformed")

    monkeypatch.setattr(doctor, "registered_backends", lambda root: ["broken"])
    monkeypatch.setattr(doctor, "get_backend", lambda name: BrokenBackend())
    checks = doctor._check_backends(str(tmp_path))
    assert checks == [(doctor.FAIL, "backend broken: discovery failed: transcript index is malformed")]
