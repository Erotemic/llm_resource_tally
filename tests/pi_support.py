# SPDX-License-Identifier: Apache-2.0
"""Plain builders for Pi source files and temporary Git repositories.

Subprocesses receive an isolated tally home and neutralized Pi environment variables so
ambient agent sessions cannot affect attribution tests.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "llm_resource_tally"
sys.path.insert(0, str(REPO))
from llm_resource_tally import ledger as tally_ledger  # noqa: E402
from llm_resource_tally.backends import get_backend  # noqa: E402

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


def indexed_observation_ids(home: Path | str, agent: str = "pi") -> list[str]:
    path = Path(home) / "observation-claims.sqlite3"
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT claim_id FROM observation_claims WHERE agent=? ORDER BY claim_id", (agent,)
        )]


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


def pi_assistant(mid, ts, model, provider, usage, stop="stop", parent=None, response_model=None):
    msg = {"role": "assistant", "content": [{"type": "text", "text": "ok"}], "usage": usage, "stopReason": stop}
    if model is not None:
        msg["model"] = model
        msg["provider"] = provider
    if response_model is not None:
        msg["responseModel"] = response_model
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


def pi_usage_entry(mid, ts, kind, provider, model, usage, parent=None):
    """A Pi v3 top-level ``UsageEntry``: model-attributed usage outside assistant messages
    (Pi documents cache warming; it is included in Pi's session usage totals)."""
    rec = {"type": "usage", "id": mid, "parentId": parent, "timestamp": ts, "kind": kind}
    if provider is not None:
        rec["provider"] = provider
    if model is not None:
        rec["model"] = model
    if usage is not None:
        rec["usage"] = usage
    return rec


def write_session(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


# ------------------------------------------------------------------- unit: munging


def _fork_pair_repos(sessions_dir: str, r1, r2):
    """Two repos sharing one explicit sessions dir: the parent session (r1) has two
    assistant turns; the fork (r2) is a verbatim copy of them plus one new turn."""
    a1 = pi_assistant("a1", "2026-07-01T09:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 10))
    a2 = pi_assistant("a2", "2026-07-01T09:02:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 20), parent="a1")
    a3 = pi_assistant("a3", "2026-07-01T10:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(5, 40), parent="a2")
    a = Path(sessions_dir) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    b = Path(sessions_dir) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", str(r1)), a1, a2])
    write_session(b, [pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", str(r2), parent=str(a)), a1, a2, a3])
    set_mtime(a, 1_000_000)
    set_mtime(b, 2_000_000)
    return a, b

def _e2e_env(tmp_path):
    tool_dir = tmp_path / "tally"
    make_vendored(tool_dir)
    env = {"PI_SESSIONS_DIR": str(tmp_path / "sessions")}
    return tool_dir, env

def _strip_observation_ids_from_local_ledger(repo: Path) -> None:
    """Make a current row look like the immediately-pre-redesign compact ledger format."""
    ledger_path = repo / ".llm_resource_tally" / "local" / "ledger.jsonl"
    lines = []
    for line in ledger_path.read_text().splitlines():
        row = json.loads(line)
        row.pop("oi", None)
        lines.append(json.dumps(row, separators=(",", ":")))
    ledger_path.write_text("\n".join(lines) + "\n")

def _start_raced_recorder(driver, ready, barrier, tool_dir, repo, sha, env):
    return subprocess.Popen(
        [sys.executable, str(driver), str(ready), str(barrier),
         str(tool_dir), "record", "--backend", "pi", "--commit", sha],
        cwd=str(repo), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

def _barrier_release(barrier: Path):
    return barrier.write_text("")
