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
    assert data["accounting_scope"]["aggregation"] == "gross_repository_attributed_sum"
    assert data["accounting_scope"]["global_observation_deduplication"] is False


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
    ledger_dir = repo / ".llm_resource_tally" / "ledger"
    active = ledger_dir / "ledger.jsonl"
    assert active.is_file() and len(active.read_text().splitlines()) == 1
    assert local.read_text() == ""
    assert len(tally_ledger.read_ledger(root=str(repo))) == 1

    # Reintroducing the same rows models interruption after appending but before local cleanup.
    local.write_bytes(active.read_bytes())
    r = run(["python3", "-B", str(dest), "publish"], repo)
    assert r.returncode == 0, r.stderr
    assert "were already published" in r.stdout
    assert len(active.read_text().splitlines()) == 1, "a republished row must not be appended twice"
    assert len(tally_ledger.read_ledger(root=str(repo))) == 1

    # publishing repeatedly must not multiply files: one active shard, no per-call shards
    for _ in range(3):
        assert run(["python3", "-B", str(dest), "publish"], repo).returncode == 0
    assert [p.name for p in sorted(ledger_dir.glob("*.jsonl"))] == ["ledger.jsonl"]


def test_published_shard_rotates_by_size_not_per_publish(tmp_path, monkeypatch):
    """Growth is bounded by rotation, not by minting a file per publication."""
    import importlib

    repo = tmp_path / "repo"
    init_repo(repo)
    monkeypatch.setenv("LLM_RESOURCE_TALLY_MAX_LEDGER_BYTES", "400")
    monkeypatch.chdir(repo)
    from llm_resource_tally import ledger as led

    importlib.reload(led)
    from llm_resource_tally import publish as pub

    importlib.reload(pub)
    led.ensure_published_layout(str(repo))
    spool = repo / ".llm_resource_tally" / "local"
    spool.mkdir(parents=True, exist_ok=True)
    try:
        for i in range(6):
            row = {"v": 3, "rec": f"2026-01-0{i + 1}T00:00:00+00:00", "c": f"c{i}", "a": "x", "sid": f"s{i}"}
            (spool / "ledger.jsonl").write_text(json.dumps(row) + "\n")
            pub.publish_local(str(repo))
        names = sorted(p.name for p in (repo / ".llm_resource_tally" / "ledger").glob("*.jsonl"))
        assert "ledger.jsonl" in names
        assert len(names) < 6, f"rotation should bound shard count, got {names}"
        assert all(n == "ledger.jsonl" or n.startswith("ledger.20") for n in names), names
        assert len(led.read_ledger(root=str(repo))) == 6
    finally:
        monkeypatch.undo()
        importlib.reload(led)
        importlib.reload(pub)


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
    assert "Publish before you hand off substantial work" in text
    assert "This is routine" in text
    assert "Stage and commit what it writes" in text
    assert "never let accounting block the repository work you were asked to do" in text
    assert "rather than proof of complete history" in text
    assert "local to this user and machine" in text


def test_agents_block_never_breaks_a_command_across_lines():
    """A wrapped command cannot be copy-pasted, so code spans must survive wrapping intact."""
    from llm_resource_tally.wiring_agents import managed_agents_block

    run = "python3 -B .llm_resource_tally/tool"
    for mode in ("local", "committed", "ignored", "notes"):
        text = managed_agents_block(run, "1.0", mode)
        for cmd in ("publish", "doctor", "install"):
            if f"`{run} {cmd}`" in " ".join(text.split()):
                assert f"{run} {cmd}" in text, f"{mode}: `{run} {cmd}` was split across lines"


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


def test_passive_record_continues_after_one_backend_fails(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import llm_resource_tally.record as record

    class Broken:
        name = "broken"

        def default_projects_dir(self):
            return str(tmp_path)

        def find_transcript(self, projects, session, strict=False):
            raise ValueError("bad transcript index")

    class Good:
        name = "good"

        def default_projects_dir(self):
            return str(tmp_path)

        def find_transcript(self, projects, session, strict=False):
            return str(tmp_path / "good.jsonl")

    backends = {"broken": Broken(), "good": Good()}
    calls = []
    monkeypatch.setattr(record, "registered_backends", lambda: ["broken", "good"])
    monkeypatch.setattr(record, "get_backend", lambda name: backends[name])
    monkeypatch.setattr(record, "repo_root", lambda: str(tmp_path / "repo"))
    monkeypatch.setattr(record, "_record_transcript", lambda backend, path, args, repo: calls.append((backend.name, path)))
    args = SimpleNamespace(
        backend=None,
        transcript=None,
        session=None,
        projects_dir=None,
    )

    with pytest.raises(SystemExit, match="passive recording incomplete.*broken.*bad transcript index"):
        record.cmd_record(args)
    assert calls == [("good", str(tmp_path / "good.jsonl"))]
