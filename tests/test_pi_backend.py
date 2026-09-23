# SPDX-License-Identifier: Apache-2.0
"""Tests for the Pi coding agent backend (`backends/pi.py`) and the per-entry-id claim log.

In-process unit tests cover the session-dir munging, the turn parser (fork floors, zero
usage, measured vs estimated compaction, v1/malformed tolerance), and session-dir resolution
and discovery under Pi's two layouts (default encoded-cwd children vs explicit flat dir).
Subprocess e2e tests exercise the real CLI (`record` / `reconcile` / `doctor`) against
synthetic Pi session JSONL files.

Every subprocess run gets an isolated `LLM_RESOURCE_TALLY_HOME` (per the e2e convention:
`<tmp>/.rt_home`), a temp `PI_SESSIONS_DIR`, and neutralized inherited `PI_*` variables —
this development machine is itself a Pi session, so the real `PI_SESSION_FILE` and friends
must not leak into tests.
"""

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
from llm_resource_tally.backends import get_backend  # noqa: E402
from llm_resource_tally.backends.pi import (  # noqa: E402
    LAYOUT_DEFAULT_ROOT,
    LAYOUT_EXPLICIT_DIR,
    default_sessions_dir,
    pi_munged_project_dir,
    resolve_sessions,
)

PI = get_backend("pi")

# ------------------------------------------------------------------- helpers

#: Variables Pi's shell tool sets for children. Neutralized everywhere in these tests.
_PI_ENV = {
    "PI_SESSION_FILE": "",
    "PI_SESSION_ID": "",
    "PI_PROVIDER": "",
    "PI_MODEL": "",
    "PI_REASONING_LEVEL": "",
    "PI_SESSIONS_DIR": "",
    "PI_CODING_AGENT_SESSION_DIR": "",
    "PI_CODING_AGENT_DIR": "",
}


@pytest.fixture(autouse=True)
def _neutralize_pi_env(monkeypatch):
    for key, val in _PI_ENV.items():
        monkeypatch.setenv(key, val)


def run(args, cwd, env=None):
    e = {
        **os.environ,
        **_PI_ENV,
        "LLM_RESOURCE_TALLY_HOME": os.path.join(os.path.dirname(os.path.abspath(cwd)), ".rt_home"),
        **(env or {}),
    }
    return subprocess.run(args, cwd=cwd, env=e, capture_output=True, text=True)


def git(args, cwd, env=None):
    return run(["git", *args], cwd, env)


def init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    assert git(["init", "-q"], path).returncode == 0
    git(["config", "user.email", "t@t"], path)
    git(["config", "user.name", "t"], path)
    git(["config", "commit.gpgsign", "false"], path)
    (path / "seed.txt").write_text("seed\n")
    git(["add", "-A"], path)
    assert git(["commit", "-qm", "seed"], path).returncode == 0


def commit(path: Path, msg: str, ts: str) -> str:
    """A commit with a fixed committer date (so turn/commit window math is deterministic)."""
    (path / f"{msg}.txt").write_text(msg + "\n")
    git(["add", "-A"], path)
    env = {"GIT_AUTHOR_DATE": ts, "GIT_COMMITTER_DATE": ts}
    assert git(["commit", "-qm", msg], path, env).returncode == 0
    return git(["rev-parse", "HEAD"], path).stdout.strip()


def make_vendored(dest: Path):
    shutil.copytree(PKG, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(REPO / "VERSION", dest / "VERSION")


def tool(dest: Path):
    return ["python3", str(dest)]


def read_rows(repo):
    return tally_ledger.read_ledger(root=repo)


def measured(rows):
    return [r for r in rows if r.get("kind") != "compaction-estimate"]


def set_mtime(path: Path, ts: float):
    os.utime(path, (ts, ts))


# ---- synthetic Pi session writers (shape-matched to real pi-ai session files)

def pi_cost():
    return {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}


def pi_usage(i, o, cw=0, cr=0, reasoning=0):
    return {
        "input": i,
        "output": o,
        "cacheRead": cr,
        "cacheWrite": cw,
        "reasoning": reasoning,
        "totalTokens": i + o + cr + cw,
        "cost": pi_cost(),
    }


def pi_header(sid, ts, cwd, parent=None):
    h = {"type": "session", "version": 3, "id": sid, "timestamp": ts, "cwd": cwd}
    if parent:
        h["parentSession"] = parent
    return h


def pi_assistant(mid, ts, model, provider, usage, stop="stop", parent=None):
    msg = {"role": "assistant", "content": [{"type": "text", "text": "ok"}], "usage": usage, "stopReason": stop}
    if model is not None:
        msg["model"] = model
        msg["provider"] = provider
    return {"type": "message", "id": mid, "parentId": parent, "timestamp": ts, "message": msg}


def pi_tool_result(mid, ts, usage, parent=None, is_error=False):
    return {
        "type": "message",
        "id": mid,
        "parentId": parent,
        "timestamp": ts,
        "message": {
            "role": "toolResult",
            "toolName": "execute_command",
            "content": [{"type": "text", "text": "output"}],
            "isError": is_error,
            "usage": usage,
        },
    }


def pi_compaction(mid, ts, usage, parent=None, tokens_before=1000, summary="s" * 50):
    rec = {
        "type": "compaction",
        "id": mid,
        "parentId": parent,
        "timestamp": ts,
        "summary": summary,
        "tokensBefore": tokens_before,
        "firstKeptEntryId": "a1",
        "details": {"readFiles": [], "modifiedFiles": []},
        "fromHook": False,
    }
    if usage is not None:
        rec["usage"] = usage
    return rec


def pi_branch_summary(mid, ts, usage, parent=None, summary="b" * 30):
    return {
        "type": "branch_summary",
        "id": mid,
        "parentId": parent,
        "timestamp": ts,
        "summary": summary,
        "usage": usage,
    }


def pi_model_change(mid, ts, provider, model_id, parent=None):
    return {
        "type": "model_change",
        "id": mid,
        "parentId": parent,
        "timestamp": ts,
        "provider": provider,
        "modelId": model_id,
    }


def write_session(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


# ------------------------------------------------------------------- unit: munging

def test_munged_encoding():
    # Mirrors Pi's `--${cwd.replace(/^[/\\]/, "").replace(/[/\\:]/g, "-")}--`.
    assert pi_munged_project_dir("/home/u/llm_resource_tally") == "--home-u-llm_resource_tally--"
    assert pi_munged_project_dir("/home/u/a/b_c.d x") == "--home-u-a-b_c.d x--"
    assert pi_munged_project_dir("/home/u/repo/sub/deep") == "--home-u-repo-sub-deep--"
    assert pi_munged_project_dir("C:\\Users\\me\\repo") == "--C--Users-me-repo--"
    assert pi_munged_project_dir("/tmp/x:y/z") == "--tmp-x-y-z--"
    assert pi_munged_project_dir("/") == "----"
    # A subdirectory dir is the root dir with its final dash extended by the subpath.
    assert pi_munged_project_dir("/r/sub") == pi_munged_project_dir("/r")[:-1] + "sub--"


# ------------------------------------------------------------------- unit: parse_turns

def test_parse_basic_reasoning_subset_and_zero_usage(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    p = tmp_path / "s.jsonl"
    write_session(p, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
        # reasoning=15 is a SUBSET of output=30: it must not be added on top.
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 30, cw=50, cr=200, reasoning=15)),
        # failed + zero usage: excluded entirely (no consumption recorded).
        pi_assistant("a2", "2026-07-01T09:02:00.000Z", "qwen3.8-27b", "litellm", pi_usage(0, 0), stop="error", parent="a1"),
        # succeeded + zero usage: endpoint silent -> zero-token turn, counted.
        pi_assistant("a3", "2026-07-01T09:03:00.000Z", "qwen3.8-27b", "litellm", pi_usage(0, 0), parent="a2"),
        # model switch; the following assistant records no model of its own -> inherits it.
        pi_model_change("m1", "2026-07-01T09:04:00.000Z", "anthropic", "claude-opus-4-8", parent="a3"),
        pi_assistant("a4", "2026-07-01T09:05:00.000Z", None, None, pi_usage(10, 20), parent="m1"),
        # nested LLM usage from a tool: attributed to the model that invoked it (a4's).
        pi_tool_result("t1", "2026-07-01T09:06:00.000Z", pi_usage(5, 7), parent="a4"),
    ])
    turns = PI.parse_turns(str(p))
    assert [t["id"] for t in turns] == ["a1", "a3", "a4", "t1"]
    assert turns[0]["model"] == "litellm/qwen3.8-27b"
    assert turns[0]["usage"] == {
        "input_tokens": 100,
        "cache_creation_input_tokens": 50,
        "cache_read_input_tokens": 200,
        "output_tokens": 30,
    }
    assert turns[1]["usage"] == {k: 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
    assert turns[2]["model"] == "anthropic/claude-opus-4-8"
    assert turns[3]["type"] == "tool_result"
    assert turns[3]["model"] == "anthropic/claude-opus-4-8"
    assert PI.usage_diagnostics(str(p)) == {"calls": 4, "zero_calls": 1, "zero_failed_calls": 1}


def test_parse_fork_timestamp_floor(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a_rec = [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 10)),
        pi_assistant("a2", "2026-07-01T09:02:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 20), parent="a1"),
    ]
    write_session(a, a_rec)
    # Fork: fresh header + fresh uuid, copied prefix VERBATIM (same ids/timestamps/usage).
    write_session(b, [
        pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", repo, parent=str(a)),
        a_rec[1],
        a_rec[2],
        pi_assistant("a3", "2026-07-01T10:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(5, 40), parent="a2"),
    ])
    assert [t["id"] for t in PI.parse_turns(str(a))] == ["a1", "a2"]
    # The copied prefix is NOT re-billed by the fork; only the new work is.
    fork_turns = PI.parse_turns(str(b))
    assert [t["id"] for t in fork_turns] == ["a3"]
    # Session identity is the header uuid, not the timestamped filename stem.
    assert PI.session_id(str(b)) == "22222222-0000-0000-0000-000000000002"
    assert PI.session_id(str(a)) == "11111111-0000-0000-0000-000000000001"


def test_parse_compaction_measured_and_estimate(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    p = tmp_path / "s.jsonl"
    write_session(p, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 10)),
        # measured compaction: the summarization call's real usage -> an ordinary turn.
        pi_compaction("c1", "2026-07-01T09:30:00.000Z", pi_usage(500, 4000), parent="a1", tokens_before=12000, summary="summary" * 20),
        # usage-less compaction: nothing measured -> only an estimate event.
        pi_compaction("c2", "2026-07-01T09:40:00.000Z", None, parent="c1", tokens_before=9000, summary="x" * 100),
        # branch summary with usage: measured too.
        pi_branch_summary("b1", "2026-07-01T09:50:00.000Z", pi_usage(5, 6), parent="c2", summary="y" * 10),
    ])
    turns = PI.parse_turns(str(p))
    by_id = {t["id"]: t for t in turns}
    assert set(by_id) == {"a1", "c1", "b1"}
    assert by_id["c1"]["type"] == "compaction"
    assert by_id["c1"]["model"] == "litellm/qwen3.8-27b"  # active model; the entry records none
    assert by_id["c1"]["usage"]["output_tokens"] == 4000
    assert by_id["b1"]["type"] == "branch_summary"
    events = PI.parse_compaction_events(str(p))
    assert len(events) == 1
    assert events[0]["boundary_ts"] == "2026-07-01T09:40:00.000Z"
    assert events[0]["peak_context_tokens"] == 9000
    assert events[0]["summary_chars"] == 100


def test_parse_v1_and_malformed(tmp_path):
    p = tmp_path / "s.jsonl"
    # v1: no header, no entry ids, linear. Plus a torn last line.
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "message", "timestamp": "2026-07-01T09:01:00.000Z",
                             "message": {"role": "assistant", "model": "m1", "provider": "p1",
                                         "usage": pi_usage(1, 2), "stopReason": "stop"}}) + "\n")
        fh.write(json.dumps({"type": "message", "timestamp": "2026-07-01T09:02:00.000Z",
                             "message": {"role": "user", "content": []}}) + "\n")
        fh.write('{"type":"message","id":"broke\n')  # torn append mid-write
    turns = PI.parse_turns(str(p))
    assert len(turns) == 1
    assert turns[0]["model"] == "p1/m1"
    assert turns[0]["usage"]["output_tokens"] == 2
    assert PI.usage_diagnostics(str(p)) == {"calls": 1, "zero_calls": 0, "zero_failed_calls": 0}
    # filename-stem fallback: `<timestamp>_<uuid>.jsonl` -> uuid part.
    p2 = tmp_path / "2026-07-01T09-00-00-000Z_deadbeef-dead-beef-dead-beefdeadbeef.jsonl"
    write_session(p2, [{"type": "message", "timestamp": "2026-07-01T09:01:00.000Z",
                        "message": {"role": "assistant", "usage": pi_usage(1, 1), "stopReason": "stop"}}])
    assert PI.session_id(str(p2)) == "deadbeef-dead-beef-dead-beefdeadbeef"


# ------------------------------------------------------------------- unit: session dir resolution

def test_resolve_sessions_precedence_and_layout(tmp_path, monkeypatch):
    sessions = str(tmp_path / "tally-sessions")
    agent_env = str(tmp_path / "agent-env")
    global_dir = str(tmp_path / "agent-global")
    repo = tmp_path / "repo"
    init_repo(repo)

    # 1. tally's own override wins — an explicit dir (Pi would write files directly in it).
    monkeypatch.setenv("PI_SESSIONS_DIR", sessions)
    assert resolve_sessions() == (sessions, LAYOUT_EXPLICIT_DIR)
    monkeypatch.delenv("PI_SESSIONS_DIR")
    # 2. Pi's env var next — also explicit.
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", agent_env)
    assert resolve_sessions() == (agent_env, LAYOUT_EXPLICIT_DIR)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR")
    # 3. project .pi/settings.json sessionDir (relative -> resolved against the repo) — explicit.
    (repo / ".pi").mkdir()
    (repo / ".pi" / "settings.json").write_text(json.dumps({"sessionDir": "my-sessions"}))
    monkeypatch.chdir(repo)
    assert resolve_sessions() == (os.path.normpath(str(repo / "my-sessions")), LAYOUT_EXPLICIT_DIR)
    (repo / ".pi" / "settings.json").unlink()
    # 4. global <agent-dir>/settings.json sessionDir (absolute kept as-is) — explicit.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", global_dir)
    Path(global_dir).mkdir(parents=True, exist_ok=True)
    (Path(global_dir) / "settings.json").write_text(json.dumps({"sessionDir": str(tmp_path / "global-sessions")}))
    assert resolve_sessions() == (str(tmp_path / "global-sessions"), LAYOUT_EXPLICIT_DIR)
    (Path(global_dir) / "settings.json").unlink()
    # 5. no explicit source: the default root (encoded-cwd children underneath).
    assert resolve_sessions() == (os.path.join(global_dir, "sessions"), LAYOUT_DEFAULT_ROOT)
    # 6. agent dir default when the env var is absent: ~/.pi/agent/sessions.
    monkeypatch.delenv("PI_CODING_AGENT_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_sessions() == (os.path.join(str(tmp_path / "home"), ".pi", "agent", "sessions"), LAYOUT_DEFAULT_ROOT)
    assert default_sessions_dir() == os.path.join(str(tmp_path / "home"), ".pi", "agent", "sessions")


def test_find_transcript_explicit_dir(tmp_path, monkeypatch):
    """An explicit session dir holds the JSONLs DIRECTLY: no encoded-cwd child is created or
    required; a session whose cwd is a repo subdirectory is still found (via its header),
    and a session belonging to another repo in the same dir is rejected."""
    repo = tmp_path / "repo"
    init_repo(repo)  # discovery anchors on the process cwd's repo root
    monkeypatch.chdir(repo)
    repo = str(repo)
    other = str(tmp_path / "other")
    os.makedirs(other)
    sessions = str(tmp_path / "sessions")
    a = Path(sessions) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    b = Path(sessions) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    s = Path(sessions) / "2026-07-01T11-00-00-000Z_33333333-0000-0000-0000-000000000003.jsonl"
    o = Path(sessions) / "2026-07-01T12-00-00-000Z_44444444-0000-0000-0000-000000000004.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
                      pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(b, [pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", repo),
                      pi_assistant("b1", "2026-07-01T10:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(s, [pi_header("33333333-0000-0000-0000-000000000003", "2026-07-01T11:00:00.000Z", os.path.join(repo, "sub")),
                      pi_assistant("s1", "2026-07-01T11:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(o, [pi_header("44444444-0000-0000-0000-000000000004", "2026-07-01T12:00:00.000Z", other),
                      pi_assistant("o1", "2026-07-01T12:01:00.000Z", "m", "p", pi_usage(1, 1))])
    set_mtime(a, 1_000_000)
    set_mtime(b, 2_000_000)
    set_mtime(s, 3_000_000)
    set_mtime(o, 4_000_000)
    monkeypatch.setenv("PI_SESSIONS_DIR", sessions)
    assert resolve_sessions() == (sessions, LAYOUT_EXPLICIT_DIR)

    # most-recent-modified wins (b is newer than a; the subdir-cwd session s is newest of all)
    assert PI.find_transcript(sessions, None, strict=True) == str(s)
    # by session id
    assert PI.find_transcript(sessions, "22222222-0000-0000-0000-000000000002", strict=True) == str(b)
    assert PI.find_transcript(sessions, "nope", strict=True) is None
    # the other repo's session is never in the discovery set
    assert all(os.path.realpath(p) != os.path.realpath(str(o)) for p in PI.session_transcripts(sessions))
    # PI_SESSION_FILE: the exact session, even when its cwd is another tree (trusted hint).
    monkeypatch.setenv("PI_SESSION_FILE", str(o))
    assert PI.find_transcript(sessions, None, strict=True) == str(o)
    monkeypatch.setenv("PI_SESSION_FILE", str(a))
    assert PI.find_transcript(sessions, None, strict=True) == str(a)
    # a session id that disagrees with the env file falls through to discovery
    assert PI.find_transcript(sessions, "22222222-0000-0000-0000-000000000002", strict=True) == str(b)
    monkeypatch.delenv("PI_SESSION_FILE")
    # strict mode with no sessions at all: None, no exit
    empty = str(tmp_path / "empty")
    os.makedirs(empty)
    monkeypatch.setenv("PI_SESSIONS_DIR", empty)
    assert PI.find_transcript(empty, None, strict=True) is None


def test_find_transcript_default_layout(tmp_path, monkeypatch):
    """Default storage: files live in per-cwd ``--<encoded-cwd>--`` children under the agent
    dir's ``sessions`` root (a repo subdirectory gets its own ``--<repo>-<sub>--`` child);
    another repo's encoded dir is never scanned."""
    repo = tmp_path / "repo"
    init_repo(repo)
    monkeypatch.chdir(repo)
    repo = str(repo)
    other = str(tmp_path / "other")
    os.makedirs(other)
    agent_dir = str(tmp_path / "agent")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", agent_dir)
    sessions = os.path.join(agent_dir, "sessions")
    assert resolve_sessions() == (sessions, LAYOUT_DEFAULT_ROOT)
    assert default_sessions_dir() == sessions

    a = Path(sessions) / pi_munged_project_dir(repo) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    s = Path(sessions) / (pi_munged_project_dir(repo)[:-1] + "sub--") / "2026-07-01T11-00-00-000Z_33333333-0000-0000-0000-000000000003.jsonl"
    o = Path(sessions) / pi_munged_project_dir(other) / "2026-07-01T12-00-00-000Z_44444444-0000-0000-0000-000000000004.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
                      pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(s, [pi_header("33333333-0000-0000-0000-000000000003", "2026-07-01T11:00:00.000Z", os.path.join(repo, "sub")),
                      pi_assistant("s1", "2026-07-01T11:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(o, [pi_header("44444444-0000-0000-0000-000000000004", "2026-07-01T12:00:00.000Z", other),
                      pi_assistant("o1", "2026-07-01T12:01:00.000Z", "m", "p", pi_usage(1, 1))])
    set_mtime(a, 1_000_000)
    set_mtime(s, 2_000_000)
    set_mtime(o, 3_000_000)

    found = PI.session_transcripts(sessions)
    assert sorted(os.path.basename(p) for p in found) == [a.name, s.name]  # o never in the set
    # newest repo session wins (the subdir session s is newer than a)
    assert PI.find_transcript(sessions, None, strict=True) == str(s)


def test_explicit_session_dir_env_and_setting(tmp_path, monkeypatch):
    """Both remaining explicit sources put files DIRECTLY in the named dir (no encoded-cwd
    child beneath it), and a foreign-cwd file in that same dir is rejected."""
    repo = tmp_path / "repo"
    init_repo(repo)
    monkeypatch.chdir(str(repo))
    repo = str(repo)
    other = str(tmp_path / "other")
    os.makedirs(other)

    # (a) Pi's own env var: PI_CODING_AGENT_SESSION_DIR (mirrors the --session-dir flag).
    env_dir = str(tmp_path / "pi-sessions")
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", env_dir)
    a = Path(env_dir) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    o = Path(env_dir) / "2026-07-01T12-00-00-000Z_44444444-0000-0000-0000-000000000004.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
                      pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 1))])
    write_session(o, [pi_header("44444444-0000-0000-0000-000000000004", "2026-07-01T12:00:00.000Z", other),
                      pi_assistant("o1", "2026-07-01T12:01:00.000Z", "m", "p", pi_usage(1, 1))])
    found = PI.session_transcripts(default_sessions_dir())
    assert [os.path.basename(p) for p in found] == [a.name]  # foreign-cwd file rejected
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR")

    # (b) the sessionDir setting (project scope): relative to the repo, files directly in it.
    repo_p = tmp_path / "repo"
    (repo_p / ".pi").mkdir()
    (repo_p / ".pi" / "settings.json").write_text(json.dumps({"sessionDir": "my-sessions"}))
    sd = str(repo_p / "my-sessions")
    b = Path(sd) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    write_session(b, [pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", repo),
                      pi_assistant("b1", "2026-07-01T10:01:00.000Z", "m", "p", pi_usage(1, 1))])
    assert resolve_sessions() == (sd, LAYOUT_EXPLICIT_DIR)
    found = PI.session_transcripts(default_sessions_dir())
    assert [os.path.basename(p) for p in found] == [b.name]
    (repo_p / ".pi" / "settings.json").unlink()


# ------------------------------------------------------------------- e2e

def test_e2e_record_fork_no_double_count(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    tool_dir = tmp_path / "tally"
    make_vendored(tool_dir)
    sessions = str(tmp_path / "sessions")  # explicit dir: files live directly in it
    env = {"PI_SESSIONS_DIR": sessions}
    rd = str(repo)

    a = Path(sessions) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    a1 = pi_assistant("a1", "2026-07-01T09:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 10))
    a2 = pi_assistant("a2", "2026-07-01T09:02:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 20), parent="a1")
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", rd), a1, a2])
    set_mtime(a, 1_000_000)
    c1 = commit(repo, "first", "2026-07-01T09:30:00Z")
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c1], repo, env)
    assert r.returncode == 0, r.stderr

    b = Path(sessions) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    a3 = pi_assistant("a3", "2026-07-01T10:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(5, 40), parent="a2")
    write_session(b, [pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", rd, parent=str(a)), a1, a2, a3])
    set_mtime(b, 2_000_000)
    c2 = commit(repo, "second", "2026-07-01T10:05:00Z")
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c2], repo, env)
    assert r.returncode == 0, r.stderr

    rows = measured(read_rows(repo))
    assert [row["session_id"] for row in rows] == [
        "11111111-0000-0000-0000-000000000001",
        "22222222-0000-0000-0000-000000000002",
    ]
    # a1+a2 billed once (by the original session), a3 once (by the fork).
    assert sum(row["tokens"]["output"] for row in rows) == 70
    assert rows[0]["turns"] == 2 and rows[1]["turns"] == 1
    # the per-entry-id claim log saw each entry exactly once.
    claims_path = os.path.join(str(tmp_path), ".rt_home", "event-claims.jsonl")
    with open(claims_path, encoding="utf-8") as fh:
        seen = {}
        for line in fh:
            d = json.loads(line)
            seen.setdefault(d["event_id"], d["agent"])
    assert set(seen) == {"a1", "a2", "a3"}
    assert all(agent == "pi" for agent in seen.values())


def test_e2e_session_file_env_attribution(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    # passive discovery: pi registered in the repo's settings
    (repo / ".llm_resource_tally").mkdir(parents=True)
    (repo / ".llm_resource_tally" / "settings.json").write_text(json.dumps({"backends": ["pi"]}))
    tool_dir = tmp_path / "tally"
    make_vendored(tool_dir)
    sessions = str(tmp_path / "sessions")  # explicit dir: files live directly in it
    env = {"PI_SESSIONS_DIR": sessions}
    rd = str(repo)

    a = Path(sessions) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    b = Path(sessions) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", rd),
                      pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 11))])
    write_session(b, [pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", rd),
                      pi_assistant("b1", "2026-07-01T10:01:00.000Z", "m", "p", pi_usage(1, 22))])
    set_mtime(a, 1_000_000)
    set_mtime(b, 2_000_000)  # b is "the obvious" most-recent session...

    # ...but the commit came from session a's shell: PI_SESSION_FILE wins over mtime.
    c1 = commit(repo, "from-a", "2026-07-01T09:30:00Z")
    r = run(tool(tool_dir) + ["record", "--commit", c1], repo, {**env, "PI_SESSION_FILE": str(a)})
    assert r.returncode == 0, r.stderr
    rows = measured(read_rows(repo))
    assert rows[0]["session_id"] == "11111111-0000-0000-0000-000000000001"
    assert rows[0]["tokens"]["output"] == 11

    # without the env var, passive discovery picks the most recent (b).
    c2 = commit(repo, "from-b", "2026-07-01T10:05:00Z")
    r = run(tool(tool_dir) + ["record", "--commit", c2], repo, env)
    assert r.returncode == 0, r.stderr
    rows = measured(read_rows(repo))
    assert rows[1]["session_id"] == "22222222-0000-0000-0000-000000000002"
    assert rows[1]["tokens"]["output"] == 22


def test_e2e_reconcile_sweeps_repo_only(tmp_path):
    """Default layout (no explicit session dir): sessions live in ``--<encoded-cwd>--``
    children under the agent dir's ``sessions`` root; the other repo's encoded dir is never
    attributed here."""
    repo = tmp_path / "repo"
    init_repo(repo)
    other = tmp_path / "other"
    init_repo(other)
    tool_dir = tmp_path / "tally"
    make_vendored(tool_dir)
    agent_dir = str(tmp_path / "agent")
    sessions = os.path.join(agent_dir, "sessions")
    env = {"PI_CODING_AGENT_DIR": agent_dir}
    rd, od = str(repo), str(other)

    sub = Path(sessions) / (pi_munged_project_dir(rd)[:-1] + "sub--") / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    root = Path(sessions) / pi_munged_project_dir(rd) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    foreign = Path(sessions) / pi_munged_project_dir(od) / "2026-07-01T11-00-00-000Z_33333333-0000-0000-0000-000000000003.jsonl"
    write_session(sub, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", os.path.join(rd, "sub")),
                        pi_assistant("s1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 33))])
    write_session(root, [pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", rd),
                         pi_assistant("r1", "2026-07-01T10:01:00.000Z", "m", "p", pi_usage(1, 44))])
    write_session(foreign, [pi_header("33333333-0000-0000-0000-000000000003", "2026-07-01T11:00:00.000Z", od),
                            pi_assistant("f1", "2026-07-01T11:01:00.000Z", "m", "p", pi_usage(1, 55))])
    set_mtime(sub, 1_000_000)
    set_mtime(root, 2_000_000)
    set_mtime(foreign, 3_000_000)

    r = run(tool(tool_dir) + ["reconcile", "--backend", "pi", "--label", "sweep"], repo, env)
    assert r.returncode == 0, r.stderr
    rows = measured(read_rows(repo))
    # both this repo's sessions (root + subdir) swept; the other repo's session never is.
    assert sorted(row["session_id"] for row in rows) == [
        "11111111-0000-0000-0000-000000000001",
        "22222222-0000-0000-0000-000000000002",
    ]
    total = {row["session_id"]: row["tokens"]["output"] for row in rows}
    assert total["11111111-0000-0000-0000-000000000001"] == 33
    assert total["22222222-0000-0000-0000-000000000002"] == 44


def test_e2e_reconcile_fork_not_rebilled(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    tool_dir = tmp_path / "tally"
    make_vendored(tool_dir)
    sessions = str(tmp_path / "sessions")  # explicit dir: files live directly in it
    env = {"PI_SESSIONS_DIR": sessions}
    rd = str(repo)

    a = Path(sessions) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    a1 = pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 10))
    a2 = pi_assistant("a2", "2026-07-01T09:02:00.000Z", "m", "p", pi_usage(1, 20), parent="a1")
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", rd), a1, a2])
    set_mtime(a, 1_000_000)
    c1 = commit(repo, "first", "2026-07-01T09:30:00Z")
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c1], repo, env)
    assert r.returncode == 0, r.stderr

    b = Path(sessions) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    a3 = pi_assistant("a3", "2026-07-01T10:01:00.000Z", "m", "p", pi_usage(1, 30), parent="a2")
    write_session(b, [pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", rd, parent=str(a)), a1, a2, a3])
    set_mtime(b, 2_000_000)

    r = run(tool(tool_dir) + ["reconcile", "--backend", "pi"], repo, env)
    assert r.returncode == 0, r.stderr
    rows = measured(read_rows(repo))
    by_session = {row["session_id"]: row for row in rows}
    # the fork's swept row covers only the new turn; a1/a2 stay billed once, in total.
    assert by_session["22222222-0000-0000-0000-000000000002"]["tokens"]["output"] == 30
    assert sum(row["tokens"]["output"] for row in rows) == 60


def test_e2e_doctor_zero_usage(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_repo(repo)
    (repo / ".llm_resource_tally").mkdir(parents=True)
    (repo / ".llm_resource_tally" / "settings.json").write_text(json.dumps({"backends": ["pi"]}))
    tool_dir = tmp_path / "tally"
    make_vendored(tool_dir)
    r = run(tool(tool_dir) + ["install"], repo, {"PI_SESSIONS_DIR": ""})
    assert r.returncode == 0, r.stderr
    sessions = str(tmp_path / "sessions")  # explicit dir: files live directly in it
    env = {"PI_SESSIONS_DIR": sessions}
    rd = str(repo)

    # mostly-silent endpoint: 2 normal calls, 4 successful zero-usage calls -> WARN.
    p = Path(sessions) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    recs = [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", rd)]
    for i in range(6):
        u = pi_usage(10, 10) if i < 2 else pi_usage(0, 0)
        recs.append(pi_assistant(f"t{i}", f"2026-07-01T09:0{i}:00.000Z", "silent", "ep", u,
                                 parent=None if i == 0 else f"t{i-1}"))
    write_session(p, recs)
    set_mtime(p, 1_000_000)
    r = run(tool(tool_dir) + ["doctor"], repo, env)
    assert r.returncode == 0, r.stderr  # WARNs do not fail doctor; the setup is clean
    assert "reported zero usage" in r.stdout
    assert "may not be reporting" in r.stdout

    # healthy endpoint: no warning; a failed zero-usage call is noted as excluded.
    p2 = Path(sessions) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    recs = [
        pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", rd),
        pi_assistant("t1", "2026-07-01T10:01:00.000Z", "loud", "ep", pi_usage(10, 10)),
        pi_assistant("t2", "2026-07-01T10:02:00.000Z", "loud", "ep", pi_usage(0, 0), stop="error", parent="t1"),
    ]
    write_session(p2, recs)
    set_mtime(p2, 2_000_000)
    r = run(tool(tool_dir) + ["doctor"], repo, env)
    assert r.returncode == 0, r.stderr
    assert "failed zero-usage" in r.stdout
    assert "may not be reporting" not in r.stdout
