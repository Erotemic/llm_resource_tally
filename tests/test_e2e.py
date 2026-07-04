# SPDX-License-Identifier: Apache-2.0
"""End-to-end + unit tests for llm_resource_tally.

Runs under pytest (`pytest -q`) or standalone (`python3 tests/test_e2e.py`). Stdlib only.
Exercises real git repos, real subprocess invocations, and (best-effort) a real venv pip
install — so the vendoring/hook/CLI wiring is tested the way users hit it.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_SRC = os.path.join(REPO, "llm_resource_tally")
sys.path.insert(0, REPO)
from llm_resource_tally import schema, ledger, config, munged_project_dir  # noqa: E402


# ------------------------------------------------------------------- helpers
def run(args, cwd, env=None, stdin=None):
    # Isolate the per-user cross-repo claims log to a temp home derived from the test's
    # tmp_path (the parent of cwd), so tests never touch the real ~/.llm_resource_tally and
    # repos sharing one tmp_path (e.g. the cross-repo case) share one claims view.
    e = {"LLM_RESOURCE_TALLY_HOME": os.path.join(os.path.dirname(os.path.abspath(cwd)),
                                                 ".rt_home"),
         **os.environ, **(env or {})}
    return subprocess.run(args, cwd=cwd, env=e, input=stdin,
                          capture_output=True, text=True)


def git(a, cwd, env=None):
    return run(["git", *a], cwd, env)


def init_repo(path):
    os.makedirs(path, exist_ok=True)
    git(["init", "-q"], path)
    git(["config", "user.email", "t@t"], path)
    git(["config", "user.name", "t"], path)
    git(["config", "commit.gpgsign", "false"], path)
    with open(os.path.join(path, "seed.txt"), "w") as fh:
        fh.write("seed\n")
    git(["add", "-A"], path)
    git(["commit", "-qm", "seed"], path)


def make_vendored(dest):
    """Vendor the live package into dest (dest IS the package dir), + stamp VERSION."""
    shutil.copytree(PKG_SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(os.path.join(REPO, "VERSION"), os.path.join(dest, "VERSION"))


def write_transcript(path, late=None, cwd=None):
    T = "2026-07-01T12:00:0"

    def turn(i, mid, o, inp, cw, cr, ws=0):
        u = {"input_tokens": inp, "cache_creation_input_tokens": cw,
             "cache_read_input_tokens": cr, "output_tokens": o}
        if ws:
            u["server_tool_use"] = {"web_search_requests": ws}
        return {"type": "assistant", "timestamp": T + f"{i}.000Z",
                "message": {"id": mid, "model": "claude-opus-4-8", "usage": u}}

    recs = [turn(0, "msg_1", 30, 100, 50, 200, ws=1),
            turn(1, "msg_1", 30, 100, 50, 200, ws=1),           # streaming dup -> once
            turn(2, "msg_2", 40, 10, 0, 300),
            {"type": "system", "subtype": "compact_boundary", "timestamp": T + "3.000Z"},
            {"type": "user", "isCompactSummary": True, "timestamp": T + "3.500Z",
             "message": {"role": "user", "content": "S" * 123}},
            turn(4, "msg_3", 25, 5, 0, 400)]
    if late:
        r = turn(9, "msg_4", 7, 1, 0, 0)
        r["timestamp"] = late
        recs.append(r)
    if cwd:                                                     # Claude records cwd per record
        for r in recs:
            r["cwd"] = cwd
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def write_codex_transcript(path, repo, sid="019f-test-session", model="gpt-5.5"):
    recs = [
        {"timestamp": "2026-07-01T12:00:00.000Z", "type": "session_meta",
         "payload": {"session_id": sid, "id": sid, "cwd": repo,
                     "model_provider": "openai", "cli_version": "0.test"}},
        {"timestamp": "2026-07-01T12:00:01.000Z", "type": "turn_context",
         "payload": {"type": "turn_context", "turn_id": "turn-a", "cwd": repo,
                     "workspace_roots": [repo], "model": model}},
        {"timestamp": "2026-07-01T12:00:02.000Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"last_token_usage": {
                         "input_tokens": 1000, "cached_input_tokens": 300,
                         "output_tokens": 40, "reasoning_output_tokens": 5,
                         "total_tokens": 1040}}}},
        {"timestamp": "2026-07-01T12:00:03.000Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"last_token_usage": {
                         "input_tokens": 900, "cached_input_tokens": 900,
                         "output_tokens": 20, "reasoning_output_tokens": 0,
                         "total_tokens": 920}}}},
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def read_rows(repo):
    """Decode all ledger shards (compact) into rich rows, the way read_ledger does."""
    rows = []
    d = os.path.join(repo, ".llm_resource_tally", "ledger")
    for p in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(schema.decode_row(json.loads(line)))
    return rows


def measured(rows):
    return [r for r in rows if r.get("kind") != "compaction-estimate"]


def tool(dest):
    """Invoke the vendored package by path (Python runs its __main__.py)."""
    return ["python3", dest]


# ------------------------------------------------------------------- unit: schema
def test_schema_roundtrip():
    rich = {"schema": "x", "recorded_at": "2026-01-01T00:00:00Z", "repo": "r",
            "commit": "abc", "commit_ts": "2026-01-01T00:00:00+00:00", "agent": "claude-code",
            "activity": "impl", "session_id": "s1", "turns": 3,
            "models": ["claude-opus-4-8"],
            "tokens": {"input": 100, "cache_write": 50, "cache_read": 200, "output": 30,
                       "billable_input": 350},
            "by_model": {"claude-opus-4-8": {"input": 100, "cache_write": 50,
                                             "cache_read": 200, "output": 30}},
            "server_tools": {"web_search": 1, "web_fetch": 0},
            "time": {"wall_clock_s": 12.5}, "turn_ts_range": ["a", "b"]}
    compact = schema.encode_row(rich)
    assert compact["t"] == [100, 50, 200, 30]              # positional token array
    assert "st" in compact and compact["st"] == [1, 0]
    back = schema.decode_row(compact)
    assert back["tokens"]["output"] == 30
    assert back["tokens"]["billable_input"] == 350        # derived on decode
    assert back["turns"] == 3 and back["agent"] == "claude-code"
    # compact serialization has no spaces
    line = json.dumps(compact, separators=(",", ":"))
    assert ", " not in line and '"tokens"' not in line


def test_schema_compaction_and_legacy():
    cx = schema.decode_row(schema.encode_row(
        {"kind": "compaction-estimate", "session_id": "s", "commit": "c",
         "boundary_ts": "t", "models": ["m"],
         "compaction": {"peak_context_tokens": 350, "summary_chars": 123}}))
    assert cx["kind"] == "compaction-estimate"
    assert cx["compaction"] == {"peak_context_tokens": 350, "summary_chars": 123}
    # a legacy verbose row (has "tokens") passes through unchanged
    legacy = {"schema": "resource-ledger/v2", "commit": "z", "tokens": {"output": 9}}
    assert schema.decode_row(legacy) is legacy


# ------------------------------------------------------------------- unit: rolling
def test_rolling_rotation(tmp_path, monkeypatch):
    repo = str(tmp_path / "roll")
    init_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ledger, "MAX_LEDGER_BYTES", 400)   # tiny cap to force rotation
    for i in range(40):
        ledger.append_row({"schema": "x", "recorded_at": f"2026-01-01T00:00:{i:02d}Z",
                           "repo": "roll", "commit": f"c{i}", "commit_ts": None,
                           "agent": "claude-code", "activity": None, "session_id": "s",
                           "turns": 1, "models": ["m"],
                           "tokens": {"input": 1, "cache_write": 0, "cache_read": 0,
                                      "output": i, "billable_input": 1},
                           "by_model": {}, "server_tools": {"web_search": 0, "web_fetch": 0},
                           "time": {"wall_clock_s": None}, "turn_ts_range": [None, None]})
    shards = glob.glob(os.path.join(repo, ".llm_resource_tally", "ledger", "*.jsonl"))
    assert len(shards) >= 2, f"expected rotation into multiple shards, got {shards}"
    rows = ledger.read_ledger()
    assert len(rows) == 40                                  # every distinct commit read back
    assert os.path.exists(os.path.join(repo, ".llm_resource_tally", ".gitattributes"))


# ------------------------------------------------------------------- A: vendored
def test_vendored_install(tmp_path):
    repo = str(tmp_path / "vendor")
    init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool")
    make_vendored(dest)
    projects = str(tmp_path / "proj")
    tpath = os.path.join(projects, munged_project_dir(repo), "sess-a.jsonl")
    write_transcript(tpath)
    env = {"CLAUDE_PROJECTS_DIR": projects}

    r = run(tool(dest) + ["install", "--dir", ".llm_resource_tally/tool"], repo, env)
    assert r.returncode == 0, r.stderr
    assert git(["config", "--get", "core.hooksPath"], repo).stdout.strip() == ".llm_resource_tally/tool/hooks"
    # the vendored tool ships its own .gitignore so running it never stages __pycache__/*.pyc
    tgi = os.path.join(repo, ".llm_resource_tally", "tool", ".gitignore")
    assert os.path.exists(tgi) and "__pycache__" in open(tgi).read()
    ga = os.path.join(repo, ".llm_resource_tally", ".gitattributes")

    r = run(tool(dest) + ["record", "--commit", "HEAD", "--label", "impl"], repo, env)
    assert r.returncode == 0, r.stderr
    m = measured(read_rows(repo))
    assert m and m[0]["tokens"]["output"] == 95            # dedup: msg_1 counted once
    assert m[0]["agent"] == "claude-code"                  # backend tag
    r = run(tool(dest) + ["rollup"], repo, env)
    assert r.returncode == 0 and "impl" in r.stdout
    assert os.path.exists(os.path.join(repo, ".llm_resource_tally", "lifetime-totals.json"))
    assert "merge=union" in open(ga).read()

    # the post-commit hook fires on a real commit
    late = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    write_transcript(tpath, late=late)
    with open(os.path.join(repo, "f2.txt"), "w") as fh:
        fh.write("x")
    git(["add", "-A"], repo)
    git(["commit", "-qm", "second"], repo, env=env)
    assert any(x.get("tokens", {}).get("output") == 7 for x in read_rows(repo))


# ------------------------------------------------------------------- B: pip bootstrap vendors
def test_pip_bootstrap_vendors(tmp_path):
    repo = str(tmp_path / "pipmode")
    init_repo(repo)
    site = str(tmp_path / "fakesite")
    shutil.copytree(PKG_SRC, os.path.join(site, "llm_resource_tally"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    env = {"PYTHONPATH": site, "CLAUDE_PROJECTS_DIR": str(tmp_path / "proj")}
    r = run(["python3", "-m", "llm_resource_tally", "install"], repo, env)
    assert r.returncode == 0, r.stderr
    vend = os.path.join(repo, ".llm_resource_tally", "tool")
    assert os.path.exists(os.path.join(vend, "__main__.py"))       # package vendored in
    assert os.path.exists(os.path.join(vend, "backends", "claude.py"))
    assert os.path.exists(os.path.join(vend, "hooks", "post-commit"))
    assert git(["config", "--get", "core.hooksPath"], repo).stdout.strip() == ".llm_resource_tally/tool/hooks"
    assert "python3 .llm_resource_tally/tool install" in open(os.path.join(repo, "AGENTS.md")).read()
    # the vendored package works offline (run by path, no PYTHONPATH to the package)
    tpath = os.path.join(str(tmp_path / "proj"), munged_project_dir(repo), "sess-b.jsonl")
    write_transcript(tpath)
    r = run(tool(vend) + ["record", "--commit", "HEAD"], repo,
            {"CLAUDE_PROJECTS_DIR": str(tmp_path / "proj")})
    assert r.returncode == 0, r.stderr
    m = measured(read_rows(repo))
    assert m and m[0]["tokens"]["output"] == 95


# ------------------------------------------------------------------- C: real pip (best-effort)
def test_real_pip(tmp_path):
    venv = str(tmp_path / "venv")
    if run(["python3", "-m", "venv", venv], str(tmp_path)).returncode != 0:
        pytest.skip("venv creation failed")
    pip = os.path.join(venv, "bin", "pip")
    if run([pip, "install", REPO], str(tmp_path)).returncode != 0:
        pytest.skip("pip install failed (likely no network for build deps)")
    exe = os.path.join(venv, "bin", "llm_resource_tally")
    assert os.path.exists(exe)
    assert run([exe, "--help"], str(tmp_path)).returncode == 0
    trepo = str(tmp_path / "target")
    init_repo(trepo)
    r = run([exe, "install"], trepo, {"CLAUDE_PROJECTS_DIR": str(tmp_path / "p")})
    assert r.returncode == 0, r.stderr
    assert os.path.exists(os.path.join(trepo, ".llm_resource_tally", "tool", "__main__.py"))


# ------------------------------------------------------------------- D: cross-repo claude hook
def test_cross_repo_claude_hook(tmp_path):
    a = str(tmp_path / "repoA"); init_repo(a)      # session runs here
    b = str(tmp_path / "repoB"); init_repo(b)      # commit lands here
    dest = os.path.join(b, ".llm_resource_tally", "tool")
    make_vendored(dest)
    tpath = os.path.join(str(tmp_path / "proj"), munged_project_dir(a), "sess-c.jsonl")
    write_transcript(tpath)
    with open(os.path.join(b, "fix.txt"), "w") as fh:
        fh.write("x")
    git(["add", "-A"], b)
    git(["commit", "-qm", "fix from A"], b)
    payload = {"session_id": "sess-c", "transcript_path": tpath, "cwd": b,
               "tool_input": {"command": f"git -C {b} commit -m x"}}
    r = run(tool(dest) + ["hook"], a, stdin=json.dumps(payload))   # run from A's cwd
    assert r.returncode == 0 and r.stdout.strip() == ""
    m = measured(read_rows(b))
    assert m and m[0]["tokens"]["output"] == 95                    # recorded into B
    assert not os.path.exists(os.path.join(a, ".llm_resource_tally", "ledger"))  # not A
    # a non-commit command is a no-op
    before = len(read_rows(b))
    run(tool(dest) + ["hook"], a,
        stdin=json.dumps({"transcript_path": tpath, "cwd": b, "tool_input": {"command": "ls"}}))
    assert len(read_rows(b)) == before


# ------------------------------------------------------------------- Claude native hooks
def test_claude_hooks_wire_idempotent_and_uninstall(tmp_path):
    repo = str(tmp_path / "clh"); init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool"); make_vendored(dest)
    sp = os.path.join(repo, ".claude", "settings.json")

    def settings():
        return json.load(open(sp))

    r = run(tool(dest) + ["install", "--claude"], repo)
    assert r.returncode == 0, r.stderr
    s = settings()
    ptu = s["hooks"]["PostToolUse"]
    se = s["hooks"]["SessionEnd"]
    assert len(ptu) == 1 and ptu[0]["matcher"] == "Bash" and ptu[0]["hooks"][0]["command"].endswith("hook  # llm_resource_tally")
    assert len(se) == 1 and "reconcile" in se[0]["hooks"][0]["command"] and "rollup" in se[0]["hooks"][0]["command"]
    assert "matcher" not in se[0]                             # fires on every session-end reason

    # idempotent: re-running does not duplicate either entry
    assert run(tool(dest) + ["install", "--claude"], repo).returncode == 0
    s = settings()
    assert len(s["hooks"]["PostToolUse"]) == 1 and len(s["hooks"]["SessionEnd"]) == 1

    # uninstall removes both ours, leaves the file valid
    assert run(tool(dest) + ["uninstall"], repo).returncode == 0
    s = settings()
    assert not any("tally" in h.get("command", "") for e in s["hooks"].get("PostToolUse", []) for h in e.get("hooks", []))
    assert not any("tally" in h.get("command", "") for e in s["hooks"].get("SessionEnd", []) for h in e.get("hooks", []))


# ------------------------------------------------------------------- E: reconcile underscore path
def test_reconcile_underscore_path(tmp_path):
    repo = str(tmp_path / "has_underscore_repo")
    init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool")
    make_vendored(dest)
    projects = str(tmp_path / "proj")
    tpath = os.path.join(projects, munged_project_dir(repo), "sess-f.jsonl")
    write_transcript(tpath)
    r = run(tool(dest) + ["reconcile", "--label", "planning"], repo,
            {"CLAUDE_PROJECTS_DIR": projects})
    assert "reconciled" in r.stdout, r.stdout + r.stderr
    pend = [x for x in measured(read_rows(repo)) if str(x.get("commit", "")).startswith("pending@")]
    assert len(pend) == 1 and pend[0]["activity"] == "planning"


# ------------------------------------------------------------------- Codex backend
def test_codex_backend_explicit_transcript(tmp_path):
    repo = str(tmp_path / "codex-explicit")
    init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool")
    make_vendored(dest)
    tpath = os.path.join(str(tmp_path / "codex"), "2026", "07", "01",
                         "rollout-2026-07-01T12-00-00-019f-codex.jsonl")
    write_codex_transcript(tpath, repo)

    r = run(tool(dest) + ["record", "--backend", "codex", "--transcript", tpath,
                          "--commit", "HEAD", "--label", "impl"], repo)
    assert r.returncode == 0, r.stderr
    m = measured(read_rows(repo))
    assert len(m) == 1
    assert m[0]["agent"] == "codex"
    assert m[0]["models"] == ["gpt-5.5"]
    assert m[0]["tokens"]["input"] == 700       # input_tokens minus cached_input_tokens
    assert m[0]["tokens"]["cache_read"] == 1200
    assert m[0]["tokens"]["cache_write"] == 0
    assert m[0]["tokens"]["output"] == 60


def test_codex_backend_discovers_repo_sessions(tmp_path):
    repo = str(tmp_path / "codex-discover")
    other = str(tmp_path / "other")
    init_repo(repo); init_repo(other)
    dest = os.path.join(repo, ".llm_resource_tally", "tool")
    make_vendored(dest)
    projects = str(tmp_path / "codex_sessions")
    write_codex_transcript(os.path.join(projects, "2026", "07", "01",
                                        "rollout-other.jsonl"), other, sid="other")
    write_codex_transcript(os.path.join(projects, "2026", "07", "02",
                                        "rollout-repo.jsonl"), repo, sid="repo")

    r = run(tool(dest) + ["reconcile", "--backend", "codex", "--projects-dir", projects,
                          "--label", "planning"], repo)
    assert r.returncode == 0, r.stderr
    m = measured(read_rows(repo))
    assert len(m) == 1
    assert m[0]["agent"] == "codex"
    assert m[0]["activity"] == "planning"
    assert m[0]["tokens"]["output"] == 60


# ------------------------------------------------------------------- registered backends
def test_registered_backends_default_and_register(tmp_path, monkeypatch):
    repo = str(tmp_path / "reg"); init_repo(repo)
    monkeypatch.chdir(repo)
    assert config.registered_backends() == ["claude", "codex"]  # both on by default
    assert not os.path.exists(config.settings_path())
    assert config.register_backend(None) == ["claude", "codex"]      # fresh repo seeded
    assert config.register_backend("codex") == ["claude", "codex"]   # idempotent/union
    data = json.load(open(config.settings_path()))
    assert data["backends"] == ["claude", "codex"]
    # an unknown name is dropped rather than trusted
    assert "bogus" not in config.register_backend("bogus")


def test_registered_backends_respects_curated_list(tmp_path, monkeypatch):
    repo = str(tmp_path / "curated"); init_repo(repo)
    monkeypatch.chdir(repo)
    config.ensure_data_dir()
    with open(config.settings_path(), "w") as fh:
        json.dump({"backends": ["claude"]}, fh)                 # user opted out of codex
    assert config.registered_backends() == ["claude"]
    assert config.register_backend(None) == ["claude"]         # re-install must not re-add codex
    assert config.register_backend("codex") == ["claude", "codex"]  # explicit add still works


def test_bare_record_auto_records_registered_codex_strictly(tmp_path):
    repo = str(tmp_path / "auto"); init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool"); make_vendored(dest)
    empty_claude = str(tmp_path / "no_claude"); os.makedirs(empty_claude)
    sessions = str(tmp_path / "codex_sessions")
    other = str(tmp_path / "elsewhere"); init_repo(other)
    # a session that belongs to THIS repo, and an unrelated (newer) one that must be ignored
    write_codex_transcript(os.path.join(sessions, "2026", "07", "01", "rollout-mine.jsonl"),
                           repo, sid="mine")
    write_codex_transcript(os.path.join(sessions, "2026", "07", "09", "rollout-other.jsonl"),
                           other, sid="other")
    env = {"CLAUDE_PROJECTS_DIR": empty_claude, "CODEX_SESSIONS_DIR": sessions}

    r = run(tool(dest) + ["install", "--backend", "codex"], repo, env)
    assert r.returncode == 0, r.stderr
    assert json.load(open(os.path.join(repo, ".llm_resource_tally", "settings.json")))[
        "backends"] == ["claude", "codex"]

    # bare record (no --backend): walks registered backends. claude finds nothing in the
    # empty projects dir; codex records ONLY the repo-matching session (strict, no fallback).
    r = run(tool(dest) + ["record", "--commit", "HEAD"], repo, env)
    assert r.returncode == 0, r.stderr
    m = measured(read_rows(repo))
    assert len(m) == 1                                   # not the unrelated 'other' session
    assert m[0]["agent"] == "codex" and m[0]["session_id"] == "rollout-mine"
    assert m[0]["tokens"]["output"] == 60


# ------------------------------------------------------------------- F: submodule separation
def test_submodule_separation(tmp_path):
    parent = str(tmp_path / "parent"); init_repo(parent)
    sub = os.path.join(parent, "sub"); os.makedirs(sub); init_repo(sub)  # nested repo
    dest = os.path.join(parent, ".llm_resource_tally", "tool")
    make_vendored(dest)
    tpath = os.path.join(str(tmp_path / "proj"), munged_project_dir(parent), "sess-g.jsonl")
    write_transcript(tpath)
    for d in (parent, sub):
        with open(os.path.join(d, "x.txt"), "w") as fh:
            fh.write("x")
        git(["add", "-A"], d)
        git(["commit", "-qm", "c"], d)
        run(tool(dest) + ["record", "--commit", "HEAD", "--transcript", tpath], d)
    assert os.path.exists(os.path.join(parent, ".llm_resource_tally", "ledger"))
    assert os.path.exists(os.path.join(sub, ".llm_resource_tally", "ledger"))   # separate!
    assert read_rows(sub) and read_rows(parent)


# ------------------------------------------------------------------- backend selection
def test_unknown_backend_errors(tmp_path):
    repo = str(tmp_path / "bk"); init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool"); make_vendored(dest)
    r = run(tool(dest) + ["record", "--backend", "nope", "--transcript", "/x", "--commit", "HEAD"], repo)
    assert r.returncode != 0 and "unknown backend" in (r.stdout + r.stderr)


# ------------------------------------------------------------------- issue 1: pending identity
def test_pending_rows_do_not_collide(tmp_path, monkeypatch):
    """Two same-day sweeps of one session cover disjoint windows; both must survive the read
    (latest-wins previously dropped the earlier turns)."""
    repo = str(tmp_path / "pend"); init_repo(repo)
    monkeypatch.chdir(repo)

    def prow(hi, out):
        return {"schema": "x", "recorded_at": f"2026-01-01T00:00:0{out}Z", "repo": "pend",
                "commit": "pending@2026-01-01", "commit_ts": None, "agent": "claude-code",
                "activity": None, "session_id": "s", "turns": 1, "models": ["m"],
                "tokens": {"input": 1, "cache_write": 0, "cache_read": 0, "output": out,
                           "billable_input": 1},
                "by_model": {}, "server_tools": {"web_search": 0, "web_fetch": 0},
                "time": {"wall_clock_s": None}, "turn_ts_range": [None, hi]}

    ledger.append_row(prow("2026-07-01T12:00:04Z", 5))
    ledger.append_row(prow("2026-07-01T12:10:00Z", 7))     # later disjoint window, same key
    outs = sorted(r["tokens"]["output"] for r in measured(ledger.read_ledger()))
    assert outs == [5, 7]                                   # both kept, not collapsed


# ------------------------------------------------------------------- issue 2: cross-repo claim
def test_cross_repo_reconcile_does_not_double_count(tmp_path):
    """A session in A that commits into B is recorded into B by the PostToolUse hook; A's
    SessionEnd reconcile must then skip those already-claimed turns."""
    a = str(tmp_path / "repoA"); init_repo(a)
    b = str(tmp_path / "repoB"); init_repo(b)
    dest = os.path.join(b, ".llm_resource_tally", "tool"); make_vendored(dest)
    projects = str(tmp_path / "proj")
    tpath = os.path.join(projects, munged_project_dir(a), "sess-x.jsonl")
    write_transcript(tpath)
    with open(os.path.join(b, "fix.txt"), "w") as fh:
        fh.write("x")
    git(["add", "-A"], b); git(["commit", "-qm", "fix from A"], b)
    payload = {"session_id": "sess-x", "transcript_path": tpath, "cwd": b,
               "tool_input": {"command": f"git -C {b} commit -m x"}}
    r = run(tool(dest) + ["hook"], a, stdin=json.dumps(payload))       # records into B + claims
    assert r.returncode == 0, r.stderr
    assert measured(read_rows(b))[0]["tokens"]["output"] == 95

    r = run(tool(dest) + ["reconcile"], a, {"CLAUDE_PROJECTS_DIR": projects})
    assert r.returncode == 0, r.stderr
    assert "nothing to reconcile" in r.stdout, r.stdout
    assert not measured(read_rows(a))                                  # A swept nothing


# ------------------------------------------------------------------- issue 5: git-commit parsing
def test_git_commit_command_parsing():
    from llm_resource_tally.backends.claude_hook import is_git_commit, commit_repo_dir
    assert is_git_commit("git commit -m x")
    assert is_git_commit('git -C "/tmp/dir with spaces" commit -m x')
    assert is_git_commit("git -c user.name=x -C /r commit")
    assert is_git_commit("cd /other && git commit -m y")
    assert not is_git_commit("git commit --dry-run")
    assert not is_git_commit("git log --oneline")
    assert commit_repo_dir("git commit", "/cwd") == "/cwd"
    assert commit_repo_dir("git -C /repo commit", "/cwd") == "/repo"
    assert commit_repo_dir('git -C "/a b" commit', "/cwd") == "/a b"
    assert commit_repo_dir("cd /other && git commit", "/cwd") == "/other"
    assert commit_repo_dir("git -c k=v -C /r commit", "/cwd") == "/r"
    assert commit_repo_dir("cd /base && git -C sub commit", "/cwd") == "/base/sub"


# ------------------------------------------------------------------- issue 7: subdir sessions
def test_subdir_session_is_discovered(tmp_path):
    repo = str(tmp_path / "subrepo"); init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool"); make_vendored(dest)
    subdir = os.path.join(repo, "pkg", "deep")
    projects = str(tmp_path / "proj")
    tpath = os.path.join(projects, munged_project_dir(subdir), "sess-sub.jsonl")
    write_transcript(tpath, cwd=subdir)                    # cwd verifies the munged-subdir dir
    r = run(tool(dest) + ["reconcile", "--label", "planning"], repo,
            {"CLAUDE_PROJECTS_DIR": projects})
    assert "reconciled" in r.stdout, r.stdout + r.stderr
    assert any(x["activity"] == "planning" for x in measured(read_rows(repo)))


# ------------------------------------------------------------------- subagent usage is counted
def test_subagent_usage_is_counted(tmp_path):
    """Claude stores Task/sidechain subagent sessions under <project>/<sid>/subagents/; their
    billed usage must fold into the parent session, not go uncounted."""
    repo = str(tmp_path / "subagent"); init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool"); make_vendored(dest)
    projects = str(tmp_path / "proj")
    munged = munged_project_dir(repo)
    write_transcript(os.path.join(projects, munged, "sess-main.jsonl"))     # main output = 95
    sub = os.path.join(projects, munged, "sess-main", "subagents", "agent-x.jsonl")
    os.makedirs(os.path.dirname(sub), exist_ok=True)
    with open(sub, "w") as fh:
        fh.write(json.dumps({
            "type": "assistant", "timestamp": "2026-07-01T12:00:05.000Z", "cwd": repo,
            "isSidechain": True,
            "message": {"id": "sub_1", "model": "claude-haiku-4-5-20251001",
                        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20,
                                  "cache_read_input_tokens": 0, "output_tokens": 50}}}) + "\n")
    r = run(tool(dest) + ["record", "--commit", "HEAD"], repo,
            {"CLAUDE_PROJECTS_DIR": projects})
    assert r.returncode == 0, r.stderr
    m = measured(read_rows(repo))
    assert m and m[0]["tokens"]["output"] == 145               # 95 main + 50 subagent
    assert "claude-haiku-4-5-20251001" in m[0]["models"]       # subagent model shows up


# ------------------------------------------------------------------- v1.2: report + richer rollup
def test_report_and_rollup_breakdown(tmp_path):
    repo = str(tmp_path / "rep"); init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool"); make_vendored(dest)
    projects = str(tmp_path / "proj")
    write_transcript(os.path.join(projects, munged_project_dir(repo), "sess-r.jsonl"))
    env = {"CLAUDE_PROJECTS_DIR": projects}
    assert run(tool(dest) + ["record", "--commit", "HEAD", "--label", "impl"], repo,
               env).returncode == 0
    r = run(tool(dest) + ["report", "--by", "activity", "--format", "json"], repo, env)
    assert r.returncode == 0, r.stderr
    assert any(row["group"] == "impl" and row["output"] == 95 for row in json.loads(r.stdout))

    totals_p = os.path.join(repo, ".llm_resource_tally", "lifetime-totals.json")
    assert run(tool(dest) + ["rollup"], repo, env).returncode == 0
    t1 = open(totals_p).read()
    assert run(tool(dest) + ["rollup"], repo, env).returncode == 0
    assert open(totals_p).read() == t1                     # deterministic (issue 13)
    d = json.loads(t1)
    assert "generated_at" not in d and d["through"]
    assert set(d["by_model"]["claude-opus-4-8"]) >= {"input", "cache_write",
                                                     "cache_read", "output"}


# ------------------------------------------------------------------- v2.0: estimate
def test_estimate_models_energy_carbon_cost(tmp_path):
    repo = str(tmp_path / "est"); init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool"); make_vendored(dest)
    projects = str(tmp_path / "proj")
    write_transcript(os.path.join(projects, munged_project_dir(repo), "sess-e.jsonl"))
    env = {"CLAUDE_PROJECTS_DIR": projects}
    run(tool(dest) + ["record", "--commit", "HEAD"], repo, env)
    r = run(tool(dest) + ["estimate", "--format", "json"], repo, env)
    assert r.returncode == 0, r.stderr
    est = json.loads(r.stdout)
    assert est["totals"]["cost_usd"] > 0
    assert est["by_model"]["claude-opus-4-8"]["energy_kwh"] > 0
    assert est["totals"]["carbon_gco2e"] > 0
    # the vendored pack carries first-class provenance (grid + energy sourced)
    assert est["provenance"] and {"grid", "energy"} <= {p.get("applies_to")
                                                        for p in est["provenance"]}


def test_estimation_source_adapter_and_provenance():
    from llm_resource_tally.estimate import (load_pack, normalize_provenance, register_adapter,
                                             resolve_source)
    # the vendored default loads through the json-file adapter and carries provenance
    assert normalize_provenance(load_pack())

    # "pointing at a new source is an adapter + a file pointer" — register + resolve
    register_adapter("inline-test", lambda ref: {
        "pack_version": ref, "grid": {"gco2e_per_kwh": 1},
        "defaults": {"wh_per_output_token": 0.0, "wh_per_input_token": 0.0,
                     "pricing_usd_per_mtok": {}}, "models": {},
        "provenance": [{"applies_to": "grid", "source": "unit"}]})
    pack = resolve_source({"adapter": "inline-test", "ref": "v9"})
    assert pack["pack_version"] == "v9"
    assert normalize_provenance(pack)[0]["source"] == "unit"

    # an unknown adapter is a clear error, not a silent wrong number
    try:
        resolve_source({"adapter": "nope", "ref": "x"})
        assert False, "expected ValueError for unknown adapter"
    except ValueError as e:
        assert "unknown estimation adapter" in str(e)

    # a source can declare provenance for an adapter whose pack carries none
    register_adapter("bare", lambda ref: {"pack_version": "b", "grid": {"gco2e_per_kwh": 0},
                                          "defaults": {"pricing_usd_per_mtok": {}}, "models": {}})
    pack = resolve_source({"adapter": "bare", "ref": "x",
                           "provenance": [{"applies_to": "all", "source": "from-spec"}]})
    assert normalize_provenance(pack)[0]["source"] == "from-spec"


# ------------------------------------------------------------------- v1.1: doctor
def test_doctor_reports_health(tmp_path):
    repo = str(tmp_path / "doc"); init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool"); make_vendored(dest)
    env = {"CLAUDE_PROJECTS_DIR": str(tmp_path / "p")}
    assert run(tool(dest) + ["install"], repo, env).returncode == 0
    r = run(tool(dest) + ["doctor"], repo, env)
    assert r.returncode == 0, r.stderr
    assert "post-commit armed" in r.stdout
    assert "retention" in r.stdout


# ------------------------------------------------------------------- v2.0: time-keyed grid
def test_estimate_time_keyed_grid():
    from llm_resource_tally.estimate import estimate, grid_at
    pack = {"pack_version": "t", "pue": 1.0,
            "grid": {"intensity_by_date": [{"from": "2024-01-01", "gco2e_per_kwh": 100},
                                           {"from": "2026-01-01", "gco2e_per_kwh": 500}]},
            "defaults": {"wh_per_output_token": 1.0, "wh_per_input_token": 0.0,
                         "pricing_usd_per_mtok": {"output": 0.0}},
            "models": {}}

    def row(commit_ts, out):
        return {"commit": "c" + str(out), "commit_ts": commit_ts, "recorded_at": commit_ts,
                "agent": "claude-code", "models": ["m"],
                "by_model": {"m": {"input": 0, "cache_write": 0, "cache_read": 0, "output": out}},
                "tokens": {"input": 0, "cache_write": 0, "cache_read": 0, "output": out,
                           "billable_input": 0},
                "turns": 1, "time": {"wall_clock_s": 0}, "turn_ts_range": [commit_ts, commit_ts]}

    rows = [row("2024-06-01T00:00:00+00:00", 1000), row("2026-06-01T00:00:00+00:00", 1000)]
    r = estimate(rows, pack)
    assert r["grid_model"].startswith("time-series")
    assert abs(r["totals"]["energy_kwh"] - 2.0) < 1e-9          # 1 kWh each
    assert abs(r["totals"]["carbon_gco2e"] - 600.0) < 1e-6      # 100 + 500 (per commit date)
    assert grid_at(pack, "2025-12-31T00:00:00Z") == 100
    assert grid_at(pack, "2026-01-01T00:00:00Z") == 500


# ------------------------------------------------------------------- v1.2: badge artifact
def test_rollup_writes_badge(tmp_path):
    repo = str(tmp_path / "badge"); init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool"); make_vendored(dest)
    projects = str(tmp_path / "proj")
    write_transcript(os.path.join(projects, munged_project_dir(repo), "s.jsonl"))
    env = {"CLAUDE_PROJECTS_DIR": projects}
    run(tool(dest) + ["record", "--commit", "HEAD"], repo, env)
    assert run(tool(dest) + ["rollup"], repo, env).returncode == 0
    bp = os.path.join(repo, ".llm_resource_tally", "badge.json")
    assert os.path.exists(bp)
    b = json.load(open(bp))
    assert b["schemaVersion"] == 1 and "tok" in b["message"] and "commits" in b["message"]


# ------------------------------------------------------------------- report --commits (PR cost)
def test_report_commits_filter(tmp_path):
    repo = str(tmp_path / "rcf"); init_repo(repo)
    dest = os.path.join(repo, ".llm_resource_tally", "tool"); make_vendored(dest)
    projects = str(tmp_path / "proj")
    write_transcript(os.path.join(projects, munged_project_dir(repo), "s.jsonl"))
    env = {"CLAUDE_PROJECTS_DIR": projects}
    with open(os.path.join(repo, "f.txt"), "w") as fh:
        fh.write("x")
    git(["add", "-A"], repo); git(["commit", "-qm", "work"], repo)
    run(tool(dest) + ["record", "--commit", "HEAD"], repo, env)
    head = git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert measured(read_rows(repo))[0]["commit"] == head
    root = git(["rev-list", "--max-parents=0", "HEAD"], repo).stdout.strip()
    # a range that includes HEAD -> the row is present
    r = run(tool(dest) + ["report", "--commits", f"{root}..HEAD", "--format", "json"], repo, env)
    assert r.returncode == 0, r.stderr
    assert any(d["output"] == 95 for d in json.loads(r.stdout))
    # an empty range -> valid empty json, no rows
    r = run(tool(dest) + ["report", "--commits", "HEAD..HEAD", "--format", "json"], repo, env)
    assert r.returncode == 0 and json.loads(r.stdout) == []


# ------------------------------------------------------------------- v3: fleet aggregator
def test_fleet_aggregates_repos(tmp_path):
    root = tmp_path / "org"
    projects = str(tmp_path / "proj")
    repos = []
    for name in ("r1", "r2"):
        repo = str(root / name); init_repo(repo)
        dest = os.path.join(repo, ".llm_resource_tally", "tool"); make_vendored(dest)
        write_transcript(os.path.join(projects, munged_project_dir(repo), f"{name}.jsonl"))
        run(tool(dest) + ["record", "--commit", "HEAD"], repo,
            {"CLAUDE_PROJECTS_DIR": projects})
        repos.append(repo)
    dest0 = os.path.join(repos[0], ".llm_resource_tally", "tool")
    r = run(tool(dest0) + ["fleet", str(root), "--format", "json"], repos[0])
    assert r.returncode == 0, r.stderr
    agg = json.loads(r.stdout)
    assert len(agg["repos"]) == 2
    assert agg["total"]["output"] == 190           # 95 + 95, summed across repos
    assert agg["total"]["commits"] == 2


# ------------------------------------------------------------------- issue 12: vendored sync
def test_vendored_copy_matches_source():
    """The dogfood copy under .llm_resource_tally/tool must not drift from the source package
    (every source file present and byte-identical). VERSION/.gitignore/hooks are tool-only."""
    vend = os.path.join(REPO, ".llm_resource_tally", "tool")
    if not os.path.isdir(vend):
        pytest.skip("no vendored dogfood copy in this checkout")
    mismatches = []
    for base, dirs, files in os.walk(PKG_SRC):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".pyc"):
                continue
            rel = os.path.relpath(os.path.join(base, f), PKG_SRC)
            vp = os.path.join(vend, rel)
            if not os.path.exists(vp):
                mismatches.append(f"missing in vendored: {rel}")
            elif open(os.path.join(base, f), "rb").read() != open(vp, "rb").read():
                mismatches.append(f"differs from source: {rel}")
    assert not mismatches, "vendored copy drifted:\n  " + "\n  ".join(mismatches)
