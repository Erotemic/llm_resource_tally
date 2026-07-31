# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "llm_resource_tally"
sys.path.insert(0, str(REPO))
from llm_resource_tally import ledger as tally_ledger  # noqa: E402


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


def vendor(dest: Path):
    shutil.copytree(PKG, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(REPO / "VERSION", dest / "VERSION")


def write_transcript(path: Path, repo: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "type": "assistant",
        "timestamp": "2026-07-10T12:00:00.000Z",
        "message": {
            "id": "m1",
            "model": "claude-opus-4-8",
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 200,
                "output_tokens": 30,
            },
        },
    }
    path.write_text(json.dumps(rec) + "\n")


def sample_row():
    return {
        "commit": "abc",
        "commit_ts": "2026-07-10T12:00:00+00:00",
        "recorded_at": "2026-07-10T12:00:01+00:00",
        "agent": "claude-code",
        "models": ["unknown"],
        "by_model": {"unknown": {"input": 1000, "cache_write": 500, "cache_read": 5000, "output": 1000}},
        "tokens": {
            "input": 1000,
            "cache_write": 500,
            "cache_read": 5000,
            "output": 1000,
            "billable_input": 6500,
        },
        "turns": 3,
        "server_tools": {"web_search": 1, "web_fetch": 0},
        "time": {"wall_clock_s": 10},
        "turn_ts_range": ["2026-07-10T11:59:00Z", "2026-07-10T12:00:00Z"],
    }


def test_generic_wide_intervals_and_typed_mitigation():
    from llm_resource_tally.modeling.estimate import estimate, load_pack
    from llm_resource_tally.modeling.mitigation import load_mitigation

    pack = load_pack("generic-wide")
    result = estimate([sample_row()], pack, mitigation=load_mitigation("builtin"))
    energy = result["intervals"]["totals"]["energy_kwh"]
    carbon = result["intervals"]["totals"]["carbon_gco2e"]
    assert energy["low"] <= energy["central"] <= energy["high"]
    assert carbon["low"] <= carbon["central"] <= carbon["high"]
    assert result["totals"]["energy_kwh"] == pytest.approx(energy["central"], abs=1e-6)
    scenarios = result["mitigation"]["price_scenarios"]
    assert {
        "avoided_or_reduced_emissions",
        "nature_based_removal",
        "biochar_carbon_removal",
        "geological_or_mineral_removal",
    } <= set(scenarios)
    assert scenarios["biochar_carbon_removal"]["credit_category"] == "carbon_removal"
    assert scenarios["avoided_or_reduced_emissions"]["credit_category"] == "emission_avoidance_or_reduction"


def test_ignored_storage_manages_gitignore(tmp_path):
    repo = tmp_path / "ignored"
    init_repo(repo)
    dest = repo / ".llm_resource_tally" / "tool"
    vendor(dest)
    r = run(["python3", "-B", str(dest), "install", "--storage", "ignored", "--hook-mode", "none"], repo)
    assert r.returncode == 0, r.stderr
    text = (repo / ".gitignore").read_text()
    assert "llm_resource_tally local state" in text
    settings = json.loads((repo / ".llm_resource_tally" / "settings.json").read_text())
    assert settings["installation"]["storage"] == "ignored"
    assert git(["config", "--local", "--get", "llmResourceTally.storage"], repo).stdout.strip() == ""
    assert git(["check-ignore", "-q", ".llm_resource_tally/tool"], repo).returncode == 0
    assert git(["check-ignore", "-q", ".llm_resource_tally/settings.json"], repo).returncode != 0


def test_notes_storage_is_worktree_clean_and_fleet_visible(tmp_path):
    from llm_resource_tally.backends.claude import munged_project_dir

    root = tmp_path / "org"
    repo = root / "notes"
    init_repo(repo)
    dest = repo / ".llm_resource_tally" / "tool"
    vendor(dest)
    r = run(["python3", "-B", str(dest), "install", "--storage", "notes", "--hook-mode", "none"], repo)
    assert r.returncode == 0, r.stderr
    git(["add", "-A"], repo)
    git(["commit", "-qm", "install tally"], repo)
    projects = tmp_path / "projects"
    transcript = projects / munged_project_dir(str(repo)) / "s.jsonl"
    write_transcript(transcript, repo)
    r = run(
        ["python3", "-B", str(dest), "record", "--commit", "HEAD"],
        repo,
        {"CLAUDE_PROJECTS_DIR": str(projects)},
    )
    assert r.returncode == 0, r.stderr
    assert git(["status", "--porcelain"], repo).stdout == ""
    assert git(["notes", "--ref=refs/notes/llm-resource-tally", "list"], repo).stdout.strip()
    r = run(["python3", "-B", str(dest), "fleet", str(root), "--format", "json"], repo)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert len(data["repos"]) == 1 and data["total"]["output"] == 30


def test_local_storage_is_clean_and_publish_is_idempotent(tmp_path):
    from llm_resource_tally.backends.claude import munged_project_dir

    root = tmp_path / "org"
    repo = root / "local"
    init_repo(repo)
    dest = repo / ".llm_resource_tally" / "tool"
    vendor(dest)
    r = run(["python3", "-B", str(dest), "install", "--storage", "local", "--hook-mode", "none"], repo)
    assert r.returncode == 0, r.stderr
    git(["add", "-A"], repo)
    git(["commit", "-qm", "install tally"], repo)
    projects = tmp_path / "projects"
    transcript = projects / munged_project_dir(str(repo)) / "s.jsonl"
    write_transcript(transcript, repo)
    r = run(
        ["python3", "-B", str(dest), "record", "--commit", "HEAD"],
        repo,
        {"CLAUDE_PROJECTS_DIR": str(projects)},
    )
    assert r.returncode == 0, r.stderr
    assert git(["status", "--porcelain"], repo).stdout == ""
    local = repo / ".llm_resource_tally" / "local" / "ledger.jsonl"
    assert local.is_file() and git(["check-ignore", "-q", str(local)], repo).returncode == 0

    r = run(["python3", "-B", str(dest), "publish"], repo)
    assert r.returncode == 0, r.stderr
    published = list((repo / ".llm_resource_tally" / "ledger").glob("ledger.sha256-*.jsonl"))
    assert len(published) == 1
    assert local.read_text() == ""
    assert len(tally_ledger.read_ledger(root=str(repo))) == 1

    # Reintroducing the same rows models interruption after publish but before local cleanup.
    local.write_bytes(published[0].read_bytes())
    r = run(["python3", "-B", str(dest), "publish"], repo)
    assert r.returncode == 0 and "already published" in r.stdout
    assert len(list((repo / ".llm_resource_tally" / "ledger").glob("ledger.sha256-*.jsonl"))) == 1
    assert len(tally_ledger.read_ledger(root=str(repo))) == 1


def test_submodule_style_source_install_stays_clean(tmp_path):
    parent = tmp_path / "parent"
    init_repo(parent)
    sub = parent / "vendor" / "llm_resource_tally"
    shutil.copytree(REPO, sub, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"))
    before = {p.relative_to(sub).as_posix() for p in sub.rglob("*")}
    r = run(["python3", "-B", str(sub), "install", "--hook-mode", "auto", "--modeling"], parent)
    assert r.returncode == 0, r.stderr
    after = {p.relative_to(sub).as_posix() for p in sub.rglob("*")}
    assert before == after
    assert "[.llm_resource_tally/tool]" in r.stdout
    assert (parent / ".llm_resource_tally" / "tool").is_file()
    assert "v0.0.0" not in r.stdout
    assert "modeling   : included" in r.stdout
    assert git(["config", "--get", "core.hooksPath"], parent).returncode != 0
    assert (parent / ".git" / "hooks" / "post-commit").exists()
    assert not (parent / ".llm_resource_tally" / "hooks").exists()
    assert not (sub / "llm_resource_tally" / "hooks").exists()


def agents_block(mode):
    """The rendered block with wrapping flattened, so assertions test wording not line breaks."""
    from llm_resource_tally.wiring_agents import managed_agents_block

    return " ".join(managed_agents_block("python3 -B .llm_resource_tally/tool", "1.0", mode).split())


def test_agents_guidance_normalizes_generated_changes():
    text = agents_block("committed")
    assert "expected bookkeeping" in text
    assert "rather than investigating or reverting them" in text
    assert "doctor" in text
    # committed mode has no separate publication step to advertise
    assert "publish" not in text


def test_agents_guidance_for_local_storage():
    text = agents_block("local")
    assert ".llm_resource_tally/local/" in text
    assert ".llm_resource_tally/ledger/" in text
    assert "repository owner's call" in text
    assert "Do not publish as routine tidy-up" in text
    assert "never let accounting block the repository work you were asked to do" in text


def test_agents_block_wraps_for_every_storage_mode():
    """The block is read by agents in-file, so no interpolated line may run off unwrapped."""
    from llm_resource_tally.config import STORAGE_MODES
    from llm_resource_tally.wiring_agents import WRAP_WIDTH, managed_agents_block

    for mode in STORAGE_MODES:
        text = managed_agents_block("python3 -B .llm_resource_tally/tool", "1.0", mode)
        body = [ln for ln in text.splitlines() if not ln.startswith("<!--")]
        assert body, mode
        for line in body:
            assert len(line) <= WRAP_WIDTH, f"{mode}: unwrapped line ({len(line)}): {line}"


def test_top_level_help_orients_an_agent(tmp_path):
    """`--help` must say what the tool does, that recording is automatic, and where to start."""
    r = run(["python3", "-B", str(REPO), "--help"], tmp_path)
    assert r.returncode == 0, r.stderr
    text = r.stdout
    assert "Recording is automatic" in text
    assert "run nothing at all" in text
    assert "doctor" in text and "publish" in text
    assert "python3 .llm_resource_tally/tool <command>" in text
    # every subcommand stays discoverable from the top-level listing
    for cmd in ("record", "reconcile", "rollup", "publish", "report", "estimate", "doctor", "fleet"):
        assert f"    {cmd}" in text
