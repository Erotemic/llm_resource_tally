# SPDX-License-Identifier: Apache-2.0
"""Tests for the Pi coding agent backend and durable observation allocation.

In-process unit tests cover the session-dir munging, the turn parser (zero usage, measured
vs estimated compaction, top-level `usage` entries, branch-aware model state resolution,
the `responseModel` billing-vs-state split, v1/malformed tolerance), fork/clone observation
allocation (migration-stable ids persisted in ledger rows with a recoverable local index), and
session-dir resolution and discovery under Pi's two layouts (default encoded-cwd
children vs explicit flat dir).
Subprocess e2e tests exercise the real CLI (`record` / `reconcile` / `doctor`) against
synthetic Pi session JSONL files.

Every subprocess run gets an isolated `LLM_RESOURCE_TALLY_HOME` (per the e2e convention:
`<tmp>/.rt_home`), a temp `PI_SESSIONS_DIR`, and neutralized inherited `PI_*` variables —
this development machine is itself a Pi session, so the real `PI_SESSION_FILE` and friends
must not leak into tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "llm_resource_tally"
sys.path.insert(0, str(REPO))
from llm_resource_tally import ledger as tally_ledger  # noqa: E402
from llm_resource_tally import record as tally_record  # noqa: E402
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


def indexed_claim_ids(home: Path | str, agent: str = "pi") -> list[str]:
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

def test_stable_backend_requires_claim_ids():
    class StableBackend:
        name = "stable-test"
        stable_observation_ids = True

    with pytest.raises(ValueError, match="stable observation ids"):
        tally_record._require_stable_observation_ids(
            StableBackend(), [{"id": "missing", "ts": "2026-07-01T09:00:00Z"}], "turn"
        )


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
        # Tool-execution usage has no persisted provider/model of its own. Extensions can
        # report nested-model usage here, so keep it measured but do not guess provenance.
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
    assert turns[3]["model"] == "?"
    assert PI.usage_diagnostics(str(p)) == {"calls": 4, "zero_calls": 1, "zero_failed_calls": 1}


def test_parse_fork_emits_copies_with_stable_claims(tmp_path):
    """The parser no longer floors on the fork header: a fork file emits EVERY entry it
    contains (copied prefix included), each with a stable claim_id. Copies of the same
    observation carry the SAME claim_id in both files (verbatim copies are
    copy-invariant), so the accounting layer - not the parser - is what allocates each
    physical observation exactly once: an unclaimed copy stays billable from any file,
    and an already-billed copy is suppressed wherever it reappears."""
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
    parent_turns = PI.parse_turns(str(a))
    fork_turns = PI.parse_turns(str(b))
    assert [t["id"] for t in parent_turns] == ["a1", "a2"]
    # The fork emits its copied prefix too (no header-timestamp floor)...
    assert [t["id"] for t in fork_turns] == ["a1", "a2", "a3"]
    # ...and every verbatim copy carries the identical claim key...
    pmap = {t["id"]: t["claim_id"] for t in parent_turns}
    fmap = {t["id"]: t["claim_id"] for t in fork_turns}
    assert fmap["a1"] == pmap["a1"] and fmap["a2"] == pmap["a2"]
    # ...while the fork's own new work has a distinct one.
    assert len({*pmap.values(), *fmap.values()}) == 3
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
    """Source-faithful Pi v1: session header present but has no ``version``; tree entry
    ids/parentIds do not exist yet. Malformed lines are skipped like Pi's own loader."""
    p = tmp_path / "s.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{not-json}\n')
        fh.write(json.dumps({"type": "session", "id": "v1-session", "timestamp": "2026-07-01T09:00:00.000Z", "cwd": str(tmp_path)}) + "\n")
        fh.write(json.dumps({"type": "message", "timestamp": "2026-07-01T09:01:00.000Z",
                             "message": {"role": "assistant", "model": "m1", "provider": "p1",
                                         "usage": pi_usage(1, 2), "stopReason": "stop"}}) + "\n")
        fh.write(json.dumps({"type": "message", "timestamp": "2026-07-01T09:02:00.000Z",
                             "message": {"role": "user", "content": []}}) + "\n")
        fh.write('{"type":"message","id":"broke\n')
    turns = PI.parse_turns(str(p))
    assert len(turns) == 1
    assert turns[0]["model"] == "p1/m1"
    assert turns[0]["usage"]["output_tokens"] == 2
    assert turns[0]["claim_id"].startswith("pi-v2:")
    assert PI.session_id(str(p)) == "v1-session"
    assert PI.usage_diagnostics(str(p)) == {"calls": 1, "zero_calls": 0, "zero_failed_calls": 0}


def test_parse_usage_entries(tmp_path):
    """Top-level `usage` entries (Pi v3 UsageEntry, e.g. cache warming) are measured turns
    billed under their OWN provider/model; the arbitrary `kind` is preserved in the turn
    type, and unknown kinds are counted rather than rejected."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    p = tmp_path / "s.jsonl"
    write_session(p, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 10)),
        # cache warming: its own provider/model, cache-write only.
        pi_usage_entry("u1", "2026-07-01T09:02:00.000Z", "cache_warming", "litellm", "qwen3.8-27b", pi_usage(0, 0, cw=5000), parent="a1"),
        # cache-read only, a different (local) model — still counted.
        pi_usage_entry("u2", "2026-07-01T09:03:00.000Z", "prompt_cache_read", "openai-compat", "local-8b", pi_usage(0, 0, cr=2500), parent="u1"),
        # an unknown, arbitrary kind is counted, never rejected.
        pi_usage_entry("u3", "2026-07-01T09:04:00.000Z", "brand_new_kind", "anthropic", "claude-opus-4-8", pi_usage(7, 3), parent="u2"),
        # no usage object: nothing measured to bill -> not a turn.
        pi_usage_entry("u4", "2026-07-01T09:05:00.000Z", "x", "p", "m", None, parent="u3"),
    ])
    turns = PI.parse_turns(str(p))
    by_id = {t["id"]: t for t in turns}
    assert [t["id"] for t in turns] == ["a1", "u1", "u2", "u3"]
    assert by_id["u1"]["type"] == "usage:cache_warming"
    assert by_id["u1"]["model"] == "litellm/qwen3.8-27b"
    assert by_id["u1"]["usage"] == {"input_tokens": 0, "cache_creation_input_tokens": 5000,
                                    "cache_read_input_tokens": 0, "output_tokens": 0}
    assert by_id["u2"]["type"] == "usage:prompt_cache_read"
    assert by_id["u2"]["model"] == "openai-compat/local-8b"
    assert by_id["u2"]["usage"]["cache_read_input_tokens"] == 2500
    assert by_id["u3"]["type"] == "usage:brand_new_kind"
    assert by_id["u3"]["model"] == "anthropic/claude-opus-4-8"
    assert by_id["u3"]["usage"]["output_tokens"] == 3
    assert PI.usage_diagnostics(str(p)) == {"calls": 4, "zero_calls": 0, "zero_failed_calls": 0}


def test_parse_branched_model_state(tmp_path):
    """Effective model state resolves by PARENT ANCESTRY, not append order. Append order
    here is mcA -> aA -> mcB -> aB -> cC -> bs, but cC and bs hang off aA (the model-A
    branch) and must see model A, not the model B that was appended earlier in the file.
    A usage entry with no model of its own and no source above it stays "?"."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    p = tmp_path / "s.jsonl"
    write_session(p, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
        pi_model_change("mcA", "2026-07-01T09:01:00.000Z", "provA", "modelA", parent=None),
        pi_assistant("aA", "2026-07-01T09:02:00.000Z", "modelA", "provA", pi_usage(10, 1), parent="mcA"),
        pi_model_change("mcB", "2026-07-01T09:03:00.000Z", "provB", "modelB", parent="aA"),
        pi_assistant("aB", "2026-07-01T09:04:00.000Z", "modelB", "provB", pi_usage(20, 2), parent="mcB"),
        # both hang off aA (the model-A side), appended AFTER the model-B branch:
        pi_compaction("cC", "2026-07-01T09:05:00.000Z", pi_usage(300, 3000), parent="aA", tokens_before=5000, summary="c" * 20),
        pi_branch_summary("bs", "2026-07-01T09:06:00.000Z", pi_usage(5, 6), parent="aA", summary="b" * 10),
        # no model of its own, at a root with no model source above: genuinely "?".
        pi_usage_entry("u0", "2026-07-01T09:07:00.000Z", "cache_warming", None, None, pi_usage(1, 1), parent=None),
    ])
    turns = PI.parse_turns(str(p))
    by_id = {t["id"]: t for t in turns}
    assert set(by_id) == {"aA", "aB", "cC", "bs", "u0"}
    assert by_id["aA"]["model"] == "provA/modelA"
    assert by_id["aB"]["model"] == "provB/modelB"
    # the alternate branch sees A, not the earlier-appended B:
    assert by_id["cC"]["type"] == "compaction"
    assert by_id["cC"]["model"] == "provA/modelA"
    assert by_id["bs"]["type"] == "branch_summary"
    assert by_id["bs"]["model"] == "provA/modelA"
    assert by_id["u0"]["model"] == "?"


def test_parse_fork_copied_ancestry_model_state(tmp_path):
    """A fork's copied prefix is not suppressed by the parser (no header-timestamp floor),
    but it must stay available as ANCESTRY: a new compaction whose only model state lives in
    the copied prefix resolves to that model, not to "?" - and the copied turn carries the
    parent's claim id, so the accounting layer (not the parser) keeps it billed once."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a_rec = [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
        pi_model_change("mcX", "2026-07-01T09:01:00.000Z", "provX", "modelX", parent=None),
        pi_assistant("aX", "2026-07-01T09:02:00.000Z", "modelX", "provX", pi_usage(100, 10), parent="mcX"),
    ]
    write_session(a, a_rec)
    # Fork: fresh header at 10:00 + the copied prefix VERBATIM, then a new measured compaction.
    write_session(b, [
        pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", repo, parent=str(a)),
        a_rec[1],  # mcX: copied - not a turn, but ancestry for the compaction
        a_rec[2],  # aX:  copied - a turn again (same claim id as in the parent file)
        pi_compaction("cN", "2026-07-01T10:01:00.000Z", pi_usage(500, 4000), parent="aX", tokens_before=8000, summary="n" * 20),
    ])
    fork_turns = PI.parse_turns(str(b))
    by_id = {t["id"]: t for t in fork_turns}
    # The fork EMITS the copied aX (the accounting layer dedups it via its claim id) and
    # its own new measured compaction.
    assert set(by_id) == {"aX", "cN"}
    assert by_id["cN"]["type"] == "compaction"
    assert by_id["cN"]["model"] == "provX/modelX"  # resolved through the copied ancestry, not "?"
    # the parent session still bills its own prefix unchanged, with the same claim id.
    parent_turns = PI.parse_turns(str(a))
    assert [t["id"] for t in parent_turns] == ["aX"]
    assert parent_turns[0]["model"] == "provX/modelX"
    assert parent_turns[0]["claim_id"] == by_id["aX"]["claim_id"]
    assert by_id["cN"]["claim_id"] not in {t["claim_id"] for t in parent_turns} | {by_id["aX"]["claim_id"]}


def test_parse_linear_model_state_unchanged(tmp_path):
    """An ordinary linear session (append order == ancestry order) produces the same
    results as before, including usage entries woven into the chain (one with no model of
    its own inheriting the session state)."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    p = tmp_path / "s.jsonl"
    write_session(p, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m1", "p1", pi_usage(1, 1)),
        pi_model_change("mc2", "2026-07-01T09:02:00.000Z", "p2", "m2", parent="a1"),
        pi_assistant("a2", "2026-07-01T09:03:00.000Z", "m2", "p2", pi_usage(2, 2), parent="mc2"),
        pi_tool_result("t2", "2026-07-01T09:04:00.000Z", pi_usage(3, 3), parent="a2"),
        # no model of its own: inherits the session state (p2/m2) at its parent.
        pi_usage_entry("u2", "2026-07-01T09:05:00.000Z", "cache_warming", None, None, pi_usage(0, 0, cw=9), parent="t2"),
    ])
    turns = PI.parse_turns(str(p))
    by_id = {t["id"]: t for t in turns}
    assert [t["id"] for t in turns] == ["a1", "a2", "t2", "u2"]
    assert by_id["a1"]["model"] == "p1/m1"
    assert by_id["a2"]["model"] == "p2/m2"
    assert by_id["t2"]["model"] == "?"
    assert by_id["u2"]["type"] == "usage:cache_warming"
    assert by_id["u2"]["model"] == "p2/m2"
    assert by_id["u2"]["usage"]["cache_creation_input_tokens"] == 9
    assert PI.usage_diagnostics(str(p)) == {"calls": 4, "zero_calls": 0, "zero_failed_calls": 0}


def test_parse_response_model_billing_vs_state(tmp_path):
    """``responseModel`` splits an assistant call's billing identity from the session state
    it establishes. The call is billed under the concrete model that answered
    (``provider/<responseModel ?? model>`` — Pi's own usage keying), but its descendants
    inherit the REQUESTED ``provider/model`` — exactly Pi's ``getSessionContextSettings``:
    a measured built-in compaction and branch summary take the logical model; tool-execution
    usage remains model-unknown because Pi persists no provider/model for it; a later explicit
    ``model_change`` still overrides the state normally;
    an ordinary assistant without ``responseModel`` is billed and establishes the same
    model as before. The billed assistant's ``claim_id`` must stay byte-identical to the
    pre-fix fingerprint (which keys the concrete response identity), so already-claimed
    observations are not reopened by this change."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    p = tmp_path / "s.jsonl"
    A, B = "modelA", "modelB"  # requested (logical) vs the concrete model that answered
    records = [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
        pi_model_change("mcA", "2026-07-01T09:01:00.000Z", "provA", A, parent=None),
        pi_assistant("aA", "2026-07-01T09:02:00.000Z", A, "provA", pi_usage(10, 2), parent="mcA", response_model=B),
        pi_tool_result("tA", "2026-07-01T09:03:00.000Z", pi_usage(3, 4), parent="aA"),
        pi_compaction("cA", "2026-07-01T09:04:00.000Z", pi_usage(300, 3000), parent="aA", tokens_before=5000, summary="c" * 20),
        pi_branch_summary("bsA", "2026-07-01T09:05:00.000Z", pi_usage(5, 6), parent="aA", summary="b" * 10),
        pi_model_change("mcB", "2026-07-01T09:06:00.000Z", "provB", "modelB2", parent="aA"),
        pi_assistant("aB", "2026-07-01T09:07:00.000Z", "modelB2", "provB", pi_usage(20, 5), parent="mcB"),
        pi_tool_result("tB", "2026-07-01T09:08:00.000Z", pi_usage(7, 8), parent="mcB"),
    ]
    write_session(p, records)
    turns = PI.parse_turns(str(p))
    by_id = {t["id"]: t for t in turns}
    # the assistant call itself bills the CONCRETE model that answered:
    assert by_id["aA"]["model"] == f"provA/{B}"
    # ... but built-in summarization descendants inherit the REQUESTED logical model.
    # Tool execution usage is measured without invented provider/model provenance:
    assert by_id["tA"]["type"] == "tool_result"
    assert by_id["tA"]["model"] == "?"
    assert by_id["cA"]["type"] == "compaction"
    assert by_id["cA"]["model"] == f"provA/{A}"
    assert by_id["bsA"]["type"] == "branch_summary"
    assert by_id["bsA"]["model"] == f"provA/{A}"
    # a later explicit model change still overrides the state normally:
    assert by_id["tB"]["model"] == "?"
    # an ordinary assistant without responseModel: billing == state, unchanged:
    assert by_id["aB"]["model"] == "provB/modelB2"

    # claim_id stability: recompute the billed assistants' fingerprints with the PRE-FIX
    # formula (the concrete response identity: id-or-timestamp, parentId, ts, kind,
    # normalized usage, provider, responseModel ?? model, stopReason) and require an
    # exact match — this change must not reopen already-claimed observations.
    def pre_fix_claim_id(rec: dict, usage: dict) -> str:
        msg = rec["message"]
        payload = {
            "id": rec.get("id") or rec.get("timestamp"),
            "parentId": rec.get("parentId"),
            "ts": rec.get("timestamp"),
            "kind": "assistant",
            "usage": usage,
            "provider": msg.get("provider"),
            "model": msg.get("responseModel") or msg.get("model"),
            "stopReason": msg.get("stopReason"),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def canon(i, o):
        return {"input_tokens": i, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": o}

    recs = {r.get("id"): r for r in records}
    assert by_id["aA"]["claim_id"].startswith("pi-v2:")
    assert pre_fix_claim_id(recs["aA"], canon(10, 2)) in by_id["aA"]["claim_aliases"]
    assert pre_fix_claim_id(recs["aB"], canon(20, 5)) in by_id["aB"]["claim_aliases"]


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
    # 2. Pi's env var next — also explicit. And it beats any sessionDir setting (Pi's own
    #    precedence: --session-dir / PI_CODING_AGENT_SESSION_DIR, then settings).
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", agent_env)
    (repo / ".pi").mkdir()
    (repo / ".pi" / "settings.json").write_text(json.dumps({"sessionDir": "my-sessions"}))
    monkeypatch.chdir(repo)
    assert resolve_sessions() == (agent_env, LAYOUT_EXPLICIT_DIR)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR")
    # 3. project .pi/settings.json sessionDir (relative -> resolved against the cwd, which
    #    here is the repo root) — explicit. Pi loads it from <cwd>/.pi/settings.json.
    assert resolve_sessions() == (os.path.normpath(str(repo / "my-sessions")), LAYOUT_EXPLICIT_DIR)
    (repo / ".pi" / "settings.json").unlink()
    # 4. global <agent-dir>/settings.json sessionDir (absolute kept as-is) — explicit.
    monkeypatch.setenv("PI_CODING_AGENT_DIR", global_dir)
    Path(global_dir).mkdir(parents=True, exist_ok=True)
    (Path(global_dir) / "settings.json").write_text(json.dumps({"sessionDir": str(tmp_path / "global-sessions")}))
    assert resolve_sessions() == (str(tmp_path / "global-sessions"), LAYOUT_EXPLICIT_DIR)
    # 4b. a RELATIVE global sessionDir resolves against the cwd, NOT the agent dir: Pi
    #     normalizes the value (tilde/expand) but leaves it relative, and the session fs
    #     layer then resolves it against the process working directory.
    (Path(global_dir) / "settings.json").write_text(json.dumps({"sessionDir": "relative-sessions"}))
    assert resolve_sessions() == (os.path.normpath(str(repo / "relative-sessions")), LAYOUT_EXPLICIT_DIR)
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
    # When Pi supplies both exact-session hints, they must agree. A stale session-file env
    # value falls back to ordinary repo discovery instead of silently misattributing it.
    monkeypatch.setenv("PI_SESSION_ID", "22222222-0000-0000-0000-000000000002")
    assert PI.find_transcript(sessions, None, strict=True) == str(s)
    monkeypatch.setenv("PI_SESSION_ID", "")
    # a caller-requested session id that disagrees with the env file also falls through.
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

    # (b) the sessionDir setting (project scope): a relative value resolves against the
    #     invocation's cwd (here the repo root, where Pi's <cwd>/.pi/settings.json lives);
    #     files are written directly in it.
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


def test_resolve_sessions_project_settings_from_subdirectory_cwd(tmp_path, monkeypatch):
    """The project settings lookup is ``<cwd>/.pi/settings.json`` — the invocation's working
    directory, NOT the git superproject root: a session started from a repo subdirectory
    whose subdirectory carries its own ``.pi/settings.json`` gets that file (winning over
    one at the repo root), with a relative ``sessionDir`` resolved against the same cwd. The
    explicit ``cwd`` argument makes the same behavior testable without chdir."""
    repo = tmp_path / "repo"
    init_repo(repo)
    sub = repo / "sub"
    sub.mkdir()
    (repo / ".pi").mkdir()
    (repo / ".pi" / "settings.json").write_text(json.dumps({"sessionDir": "root-level"}))
    (sub / ".pi").mkdir()
    (sub / ".pi" / "settings.json").write_text(json.dumps({"sessionDir": "sub-sessions"}))
    agent_dir = str(tmp_path / "agent")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", agent_dir)

    # from the subdirectory: the subdirectory's own .pi file wins, relative to the cwd:
    monkeypatch.chdir(sub)
    assert resolve_sessions() == (os.path.normpath(str(sub / "sub-sessions")), LAYOUT_EXPLICIT_DIR)
    # ...same result through the explicit cwd argument (no chdir involved):
    monkeypatch.chdir(tmp_path)
    assert resolve_sessions(cwd=str(sub)) == (os.path.normpath(str(sub / "sub-sessions")), LAYOUT_EXPLICIT_DIR)
    # from the repo root: the root-level file wins there instead:
    monkeypatch.chdir(repo)
    assert resolve_sessions() == (os.path.normpath(str(repo / "root-level")), LAYOUT_EXPLICIT_DIR)

    # and header containment in discovery is a separate anchor: invoked from the
    # subdirectory, a session whose header cwd IS the subdirectory is found under the
    # subdirectory-resolved explicit dir (the containment check still uses the repo root).
    sd = str(sub / "sub-sessions")
    os.makedirs(sd)
    a = Path(sd) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-000000000001.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", str(sub)),
                      pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 1))])
    monkeypatch.chdir(sub)
    found = PI.session_transcripts(resolve_sessions()[0])
    assert [os.path.basename(p) for p in found] == [a.name]


def test_oneoff_session_dir_not_discoverable_but_reachable(tmp_path, monkeypatch):
    """Discovery boundary: Pi can park a session in a one-off ``--session-dir`` (or an
    extension-chosen dir) that no env var, no settings file, and no default root points at —
    not reconstructible after Pi exits, so default discovery finds nothing. The supported
    escape hatches: an explicit ``--projects-dir`` (or tally's ``PI_SESSIONS_DIR``) pointed
    at the actual dir, and ``$PI_SESSION_FILE``, which bypasses discovery entirely no matter
    how Pi chose its dir."""
    repo = tmp_path / "repo"
    init_repo(repo)
    repo = str(repo)
    monkeypatch.chdir(repo)
    agent_dir = str(tmp_path / "agent")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", agent_dir)
    # no PI_SESSIONS_DIR / PI_CODING_AGENT_SESSION_DIR / sessionDir anywhere: the resolver
    # lands on the default root, which holds nothing.
    default_root, layout = resolve_sessions()
    assert layout == LAYOUT_DEFAULT_ROOT
    assert default_root == os.path.join(agent_dir, "sessions")
    oneoff = str(tmp_path / "one-off")
    os.makedirs(oneoff)
    a = Path(oneoff) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-000000000001.jsonl"
    write_session(a, [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
                      pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 1))])
    # 1. default discovery cannot reach the one-off dir:
    assert PI.session_transcripts(default_root) == []
    # 2. an explicit --projects-dir at the actual dir can:
    assert PI.find_transcript(oneoff, None, strict=True) == str(a)
    # 3. ...and so can tally's own PI_SESSIONS_DIR override pointed at the same dir:
    monkeypatch.setenv("PI_SESSIONS_DIR", oneoff)
    assert resolve_sessions() == (oneoff, LAYOUT_EXPLICIT_DIR)
    assert [os.path.basename(p) for p in PI.session_transcripts(default_sessions_dir())] == [a.name]
    monkeypatch.delenv("PI_SESSIONS_DIR")
    # 4. $PI_SESSION_FILE bypasses discovery entirely:
    monkeypatch.setenv("PI_SESSION_FILE", str(a))
    assert PI.find_transcript(default_root, None, strict=True) == str(a)


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
    # Durable rows own the observation ids; the workstation index has one canonical
    # allocation per physical call.
    assert sum(len(row.get("observation_ids") or []) for row in rows) == 3
    assert len(indexed_claim_ids(tmp_path / ".rt_home")) == 3


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
    rt_home = tmp_path / ".rt_home"
    base_env = {"PI_SESSIONS_DIR": "", "LLM_RESOURCE_TALLY_HOME": str(rt_home)}
    r = run(tool(tool_dir) + ["install"], repo, base_env)
    assert r.returncode == 0, r.stderr
    sessions = str(tmp_path / "sessions")  # explicit dir: files live directly in it
    env = {"PI_SESSIONS_DIR": sessions}
    rd = str(repo)

    # Even partial silence matters: 5 normal calls, 1 successful zero-usage call -> WARN.
    p = Path(sessions) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    recs = [pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", rd)]
    for i in range(6):
        u = pi_usage(0, 0) if i == 5 else pi_usage(10, 10)
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

    # Once a durable Pi row exists, deleting only the workstation index is observable: the
    # current repo stays idempotent from its ledger, but cross-repo fork/clone coordination is
    # degraded until a later allocation rebuilds the index.
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", "HEAD"], repo, env)
    assert r.returncode == 0, r.stderr
    index = rt_home / "observation-claims.sqlite3"
    assert index.exists()
    index.unlink()
    r = run(tool(tool_dir) + ["doctor"], repo, env)
    assert r.returncode == 0, r.stderr
    assert "observation index is missing" in r.stdout
    assert "cross-repo" in r.stdout

    # Corrupt workstation coordination is recoverable/best-effort, but doctor must surface that
    # cross-repository stable-observation dedup is currently degraded.
    rt_home.mkdir(exist_ok=True)
    index.write_text("not a sqlite database")
    r = run(tool(tool_dir) + ["doctor"], repo, env)
    assert r.returncode == 0, r.stderr
    assert "observation index is unreadable/corrupt" in r.stdout
    assert "cross-repo" in r.stdout
# ------------------------------------------------------------------- fork/clone: exact-once observation allocation


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


def test_parse_clock_skew_copied_entry_still_allocated_once(tmp_path):
    """The fork-header timestamp has NO bearing on which entries are billed (in either
    direction of skew): a copied entry whose timestamp postdates the fork header (parent
    clock ahead) and one that predates it (parent clock behind) are both emitted, and both
    carry the parent's claim ids, so the accounting layer allocates each exactly once
    regardless of which clock the copies came from."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    a = tmp_path / "a.jsonl"
    write_session(a, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", repo),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(10, 1)),
        pi_assistant("a2", "2026-07-01T11:00:00.000Z", "m", "p", pi_usage(10, 2), parent="a1"),
    ])
    # Fork header 10:00: a1 (09:01) predates it, a2 (11:00) postdates it — skew both ways.
    b = tmp_path / "b.jsonl"
    a_rec = [r for r in (json.loads(l) for l in open(a)) if r["type"] == "message"]
    write_session(b, [
        pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", repo, parent=str(a)),
        a_rec[0],
        a_rec[1],
    ])
    pmap = {t["id"]: t["claim_id"] for t in PI.parse_turns(str(a))}
    fmap = {t["id"]: t["claim_id"] for t in PI.parse_turns(str(b))}
    # Both skew directions are emitted (no floor) and both match their source observation.
    assert set(fmap) == {"a1", "a2"}
    assert fmap == pmap  # verbatim copies -> identical claim ids, whatever the clocks say


def test_e2e_record_parent_unbilled_fork_first(tmp_path):
    """The parent session is never billed before the fork commits: the fork (child repo)
    bills the WHOLE copied prefix from its own copy, and the parent's later commit must
    then add nothing (its copies are already claimed) — nothing is lost, nothing doubles."""
    r1 = tmp_path / "r1"; r2 = tmp_path / "r2"
    init_repo(r1); init_repo(r2)
    tool_dir, env = _e2e_env(tmp_path)
    _fork_pair_repos(env["PI_SESSIONS_DIR"], r1, r2)

    # Child commits FIRST: its fresh session bills a1+a2 (unclaimed copies) and a3.
    c2 = commit(r2, "child", "2026-07-01T10:05:00Z")
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c2], r2, env)
    assert r.returncode == 0, r.stderr

    # Parent commits LATE (never billed before): its a1+a2 copies are already claimed.
    c1 = commit(r1, "parent", "2026-07-01T10:10:00Z")
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c1], r1, env)
    assert r.returncode == 0, r.stderr
    assert "no new turns" in r.stdout

    rows = measured(read_rows(r2) + read_rows(r1))
    assert [row["session_id"] for row in rows] == ["22222222-0000-0000-0000-000000000002"]
    assert rows[0]["turns"] == 3
    assert sum(row["tokens"]["output"] for row in rows) == 70  # a1+a2+a3 exactly once
    # The parent's late commit produced no row of its own.
    assert not [row for row in read_rows(r1) if row.get("kind") != "compaction-estimate"]


def test_e2e_record_parent_deleted_recovers(tmp_path):
    """The fork bills the copied prefix, then the parent FILE is deleted: the parent's
    commit finds no transcript (no crash, no row), and the observations survive — billed
    once, from the fork's copy. No data is lost to the parent's disappearance."""
    r1 = tmp_path / "r1"; r2 = tmp_path / "r2"
    init_repo(r1); init_repo(r2)
    tool_dir, env = _e2e_env(tmp_path)
    a, _ = _fork_pair_repos(env["PI_SESSIONS_DIR"], r1, r2)

    c2 = commit(r2, "child", "2026-07-01T10:05:00Z")
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c2], r2, env)
    assert r.returncode == 0, r.stderr

    a.unlink()  # the parent's only copy of the prefix is gone
    c1 = commit(r1, "parent", "2026-07-01T10:10:00Z")
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c1], r1, env)
    # No session candidates remain for this repo: record reports it and exits non-zero.
    # The post-commit hook runs it as `... || true`, so this stays non-blocking.
    assert r.returncode != 0
    assert "no Pi session" in r.stderr

    rows = measured(read_rows(r2) + read_rows(r1))
    assert [row["session_id"] for row in rows] == ["22222222-0000-0000-0000-000000000002"]
    assert sum(row["tokens"]["output"] for row in rows) == 70  # recovered from the fork, once


def test_e2e_reconcile_same_entry_id_two_sessions(tmp_path):
    """Two unrelated sessions legally reuse the same 8-hex entry id (Pi only checks new
    ids against the current session's entry map): a bare-id claim key would let the first
    session's claim suppress the second's. Observation fingerprints differ (different
    timestamps and usage), so BOTH sessions bill in full. (reconcile is the pass that
    sweeps every session of a repo; record only handles the newest one.)"""
    r1 = tmp_path / "r1"
    init_repo(r1)
    tool_dir, env = _e2e_env(tmp_path)
    sessions = env["PI_SESSIONS_DIR"]
    s1 = Path(sessions) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    s2 = Path(sessions) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    write_session(s1, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", str(r1)),
        pi_assistant("deadbeef", "2026-07-01T09:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 10)),
    ])
    write_session(s2, [
        pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", str(r1)),
        pi_assistant("deadbeef", "2026-07-01T10:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 99)),
    ])
    set_mtime(s1, 1_000_000)
    set_mtime(s2, 2_000_000)

    r = run(tool(tool_dir) + ["reconcile", "--backend", "pi", "--label", "t"], r1, env)
    assert r.returncode == 0, r.stderr

    rows = measured(read_rows(r1))
    # Both same-id sessions bill; neither was suppressed by the other's claim.
    assert {row["session_id"] for row in rows} == {'11111111-0000-0000-0000-000000000001', '22222222-0000-0000-0000-000000000002'}
    assert all(row["turns"] == 1 for row in rows)
    assert sum(row["tokens"]["output"] for row in rows) == 109
    keys = indexed_claim_ids(tmp_path / ".rt_home")
    assert len(keys) == 2 and len(set(keys)) == 2  # the bare id would have been one key


def test_e2e_reconcile_order_invariant(tmp_path):
    """Which fork-family member is reconciled first must not change the machine-wide
    allocation total: a1+a2 go to whichever session is swept first, a3 to the fork, and
    the sum over all repos is identical in both orders."""
    def run_order(root: Path, child_first: bool) -> dict:
        r1, r2 = root / "r1", root / "r2"
        init_repo(r1)
        init_repo(r2)
        tool_dir, env = _e2e_env(root)
        _fork_pair_repos(env["PI_SESSIONS_DIR"], r1, r2)
        commit(r1, "p", "2026-07-01T09:30:00Z")
        commit(r2, "c", "2026-07-01T10:05:00Z")
        for repo in ((r2, r1) if child_first else (r1, r2)):
            r = run(tool(tool_dir) + ["reconcile", "--backend", "pi", "--label", "t"], repo, env)
            assert r.returncode == 0, r.stderr
        rows = measured(read_rows(r1) + read_rows(r2))
        return {
            "total": sum(row["tokens"]["output"] for row in rows),
            "by_session": {row["session_id"]: row["turns"] for row in rows},
        }

    parent_first = run_order(tmp_path / "A", child_first=False)
    child_first = run_order(tmp_path / "B", child_first=True)
    # Same machine-wide total either way...
    assert parent_first["total"] == child_first["total"] == 70
    # ...with a1+a2 allocated to whichever session was swept first, a3 to the fork.
    assert parent_first["by_session"] == {
        "11111111-0000-0000-0000-000000000001": 2,
        "22222222-0000-0000-0000-000000000002": 1,
    }
    assert child_first["by_session"] == {
        "22222222-0000-0000-0000-000000000002": 3,  # got the unclaimed copies
    }
    # Both orders indexed exactly the three observations, once each.
    for root in ("A", "B"):
        keys = indexed_claim_ids(tmp_path / root / ".rt_home")
        assert len(keys) == 3 and len(set(keys)) == 3


def test_e2e_reconcile_idempotent(tmp_path):
    """Re-reconciling, and then recording the commit that was already reconciled, adds
    nothing: the sweep and the post-commit record share the same claim/watermark state, so
    a repeated pass is a no-op (idempotent) and cannot inflate the totals."""
    r1 = tmp_path / "r1"
    init_repo(r1)
    tool_dir, env = _e2e_env(tmp_path)
    sessions = env["PI_SESSIONS_DIR"]
    s = Path(sessions) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    write_session(s, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", str(r1)),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 10)),
        pi_assistant("a2", "2026-07-01T09:02:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 20), parent="a1"),
    ])
    set_mtime(s, 1_000_000)
    c1 = commit(r1, "one", "2026-07-01T09:30:00Z")

    r = run(tool(tool_dir) + ["reconcile", "--backend", "pi", "--label", "t"], r1, env)
    assert r.returncode == 0, r.stderr
    rows = measured(read_rows(r1))
    assert sum(row["tokens"]["output"] for row in rows) == 30 and rows[0]["turns"] == 2

    r = run(tool(tool_dir) + ["reconcile", "--backend", "pi", "--label", "t"], r1, env)
    assert r.returncode == 0, r.stderr
    assert "nothing to reconcile" in r.stdout

    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c1], r1, env)
    assert r.returncode == 0, r.stderr
    assert "no new turns" in r.stdout

    rows = measured(read_rows(r1))
    assert len(rows) == 1  # still one row; neither repeat pass added anything
    assert sum(row["tokens"]["output"] for row in rows) == 30
    keys = indexed_claim_ids(tmp_path / ".rt_home")
    assert len(keys) == 2 and len(set(keys)) == 2


def test_e2e_compaction_estimate_claims_once(tmp_path):
    """A usage-LESS compaction (a reconstructed estimate event, not a measured turn) is
    copied verbatim into forks too: it carries the same stable claim id as its underlying
    entry, so the first repo to record it emits the estimate row and the fork's copy is
    suppressed — the exact-once principle covers estimate events, not just measured turns."""
    r1 = tmp_path / "r1"; r2 = tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
    tool_dir, env = _e2e_env(tmp_path)
    sessions = env["PI_SESSIONS_DIR"]
    c1 = pi_compaction("c1", "2026-07-01T09:02:00.000Z", None, parent="a1")  # no usage -> estimate
    a = Path(sessions) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    write_session(a, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", str(r1)),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 10)),
        c1,
    ])
    set_mtime(a, 1_000_000)
    cA = commit(r1, "p", "2026-07-01T09:30:00Z")
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", cA], r1, env)
    assert r.returncode == 0, r.stderr
    # Parent: one measured row (a1) and one compaction-estimate row (c1).
    rows1 = read_rows(r1)
    assert [row.get("kind") for row in rows1] == [None, "compaction-estimate"]

    b = Path(sessions) / "2026-07-01T10-00-00-000Z_22222222-0000-0000-0000-000000000002.jsonl"
    write_session(b, [
        pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", str(r2), parent=str(a)),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(100, 10)),
        c1,  # verbatim copy of the usage-less compaction
        pi_assistant("a2", "2026-07-01T10:01:00.000Z", "qwen3.8-27b", "litellm", pi_usage(10, 40), parent="c1"),
    ])
    set_mtime(b, 2_000_000)
    cB = commit(r2, "c", "2026-07-01T10:05:00Z")
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", cB], r2, env)
    assert r.returncode == 0, r.stderr
    # Fork: only its own new turn (a2); the copied a1 and the copied estimate event c1
    # are already claimed, so no second estimate row appears.
    rows2 = read_rows(r2)
    assert [row.get("kind") for row in rows2] == [None]  # only the measured a2 row
    assert rows2[0]["turns"] == 1
    # (compaction rows record no token counts by design)
    measured_rows = [row for row in rows1 + rows2 if "tokens" in row]
    assert sum(row["tokens"]["output"] for row in measured_rows) == 50
    est = [row for row in rows1 + rows2 if row.get("kind") == "compaction-estimate"]
    assert len(est) == 1
    keys = indexed_claim_ids(tmp_path / ".rt_home")
    assert len(keys) == 3 and len(set(keys)) == 3  # a1, c1 (estimate), a2


def _strip_observation_ids_from_local_ledger(repo: Path) -> None:
    """Make a current row look like the immediately-pre-redesign compact ledger format."""
    ledger_path = repo / ".llm_resource_tally" / "local" / "ledger.jsonl"
    lines = []
    for line in ledger_path.read_text().splitlines():
        row = json.loads(line)
        row.pop("oi", None)
        lines.append(json.dumps(row, separators=(",", ":")))
    ledger_path.write_text("\n".join(lines) + "\n")


def test_legacy_event_claim_import_detects_later_file_changes(tmp_path, monkeypatch):
    import llm_resource_tally.observation_allocation as cl

    home = tmp_path / "home"
    monkeypatch.setenv("LLM_RESOURCE_TALLY_HOME", str(home))
    assert cl.claimed_claim_ids("pi") == set()  # create DB while legacy file is absent
    legacy = home / "event-claims.jsonl"
    legacy.write_text(json.dumps({"agent": "pi", "claim_id": "old", "repo": str(tmp_path)}) + "\n")
    with cl._open_observation_db() as conn:
        rows = conn.execute("SELECT alias_id FROM legacy_claims WHERE agent='pi'").fetchall()
    assert rows == [("old",)]


def test_legacy_event_claim_import_respects_visible_old_owner(tmp_path):
    """An old JSONL claim remains effective only when an old-format owner row is visible."""
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
    tool_dir, env = _e2e_env(tmp_path)
    sessions = Path(env["PI_SESSIONS_DIR"])
    u1 = "11111111-0000-0000-0000-000000000001"
    u2 = "22222222-0000-0000-0000-000000000002"
    p1 = sessions / f"2026-07-01T09-00-00-000Z_{u1}.jsonl"
    p2 = sessions / f"2026-07-01T10-00-00-000Z_{u2}.jsonl"
    a1 = pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "litellm", pi_usage(10, 7))
    write_session(p1, [pi_header(u1, "2026-07-01T09:00:00.000Z", str(r1)), a1])
    old_alias = PI.parse_turns(str(p1))[0]["claim_aliases"][0]
    sha = commit(r1, "parent", "2026-07-01T09:30:00Z")
    assert run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", sha], r1, env).returncode == 0
    _strip_observation_ids_from_local_ledger(r1)

    home = tmp_path / ".rt_home"
    index = home / "observation-claims.sqlite3"
    if index.exists():
        index.unlink()
    (home / "event-claims.jsonl").write_text(
        json.dumps({"agent": "pi", "claim_id": old_alias, "repo": str(r1)}) + "\n"
    )

    a2 = pi_assistant("a2", "2026-07-01T10:01:00.000Z", "m", "litellm", pi_usage(20, 11), parent="a1")
    write_session(p2, [pi_header(u2, "2026-07-01T10:00:00.000Z", str(r2), parent=str(p1)), a1, a2])
    sha2 = commit(r2, "child", "2026-07-01T10:30:00Z")
    rr = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", sha2], r2, env)
    assert rr.returncode == 0, rr.stderr
    rows2 = measured(read_rows(r2))
    assert len(rows2) == 1
    assert rows2[0]["turns"] == 1 and rows2[0]["tokens"]["output"] == 11


def test_legacy_event_claim_import_drops_stale_tombstone(tmp_path):
    """An old JSONL claim cannot suppress retained usage after its owner row disappears."""
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
    tool_dir, env = _e2e_env(tmp_path)
    sessions = Path(env["PI_SESSIONS_DIR"])
    u1 = "11111111-0000-0000-0000-000000000001"
    u2 = "22222222-0000-0000-0000-000000000002"
    p1 = sessions / f"2026-07-01T09-00-00-000Z_{u1}.jsonl"
    p2 = sessions / f"2026-07-01T10-00-00-000Z_{u2}.jsonl"
    a1 = pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "litellm", pi_usage(10, 7))
    write_session(p1, [pi_header(u1, "2026-07-01T09:00:00.000Z", str(r1)), a1])
    old_alias = PI.parse_turns(str(p1))[0]["claim_aliases"][0]

    home = tmp_path / ".rt_home"
    home.mkdir(exist_ok=True)
    (home / "event-claims.jsonl").write_text(
        json.dumps({"agent": "pi", "claim_id": old_alias, "repo": str(r1)}) + "\n"
    )
    # No ledger row exists in r1: the compatibility claim is stale.
    a2 = pi_assistant("a2", "2026-07-01T10:01:00.000Z", "m", "litellm", pi_usage(20, 11), parent="a1")
    write_session(p2, [pi_header(u2, "2026-07-01T10:00:00.000Z", str(r2), parent=str(p1)), a1, a2])
    sha2 = commit(r2, "child", "2026-07-01T10:30:00Z")
    rr = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", sha2], r2, env)
    assert rr.returncode == 0, rr.stderr
    rows2 = measured(read_rows(r2))
    assert len(rows2) == 1
    assert rows2[0]["turns"] == 2 and rows2[0]["tokens"]["output"] == 18


def test_observation_index_recovers_when_local_ledger_is_lost(tmp_path):
    """The machine index is not a tombstone: deleting an unpublished local allocation
    while retaining the transcript makes the observation billable again on reconcile."""
    repo = tmp_path / "repo"
    init_repo(repo)
    tool_dir, env = _e2e_env(tmp_path)
    sessions = Path(env["PI_SESSIONS_DIR"])
    p = sessions / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    write_session(p, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", str(repo)),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "litellm", pi_usage(10, 7)),
    ])
    sha = commit(repo, "one", "2026-07-01T09:30:00Z")
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", sha], repo, env)
    assert r.returncode == 0, r.stderr
    rows = measured(read_rows(repo))
    assert len(rows) == 1 and rows[0]["tokens"]["output"] == 7
    assert len(rows[0]["observation_ids"]) == 1
    assert len(indexed_claim_ids(tmp_path / ".rt_home")) == 1

    shutil.rmtree(repo / ".llm_resource_tally" / "local")
    assert measured(read_rows(repo)) == []
    r = run(tool(tool_dir) + ["reconcile", "--backend", "pi"], repo, env)
    assert r.returncode == 0, r.stderr
    rows = measured(read_rows(repo))
    assert len(rows) == 1 and rows[0]["tokens"]["output"] == 7
    assert len(indexed_claim_ids(tmp_path / ".rt_home")) == 1


def test_cross_repo_owner_loss_recovers_older_fork_prefix(tmp_path):
    """Stable observation ids, not the child session watermark, are the allocation floor.

    The parent owns a1+a2; the child then owns only newer a3. If the parent's unpublished
    ledger disappears later, the child's retained copied prefix must become recoverable even
    though a1/a2 are older than the child's already-recorded turn timestamp.
    """
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
    tool_dir, env = _e2e_env(tmp_path)
    _fork_pair_repos(env["PI_SESSIONS_DIR"], r1, r2)

    c1 = commit(r1, "parent", "2026-07-01T09:30:00Z")
    rr = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c1], r1, env)
    assert rr.returncode == 0, rr.stderr
    assert sum(r["tokens"]["output"] for r in measured(read_rows(r1))) == 30

    c2 = commit(r2, "child", "2026-07-01T10:30:00Z")
    rr = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c2], r2, env)
    assert rr.returncode == 0, rr.stderr
    rows2 = measured(read_rows(r2))
    assert sum(r["tokens"]["output"] for r in rows2) == 40  # only child-only a3 so far

    shutil.rmtree(r1 / ".llm_resource_tally" / "local")
    assert measured(read_rows(r1)) == []

    rr = run(tool(tool_dir) + ["reconcile", "--backend", "pi"], r2, env)
    assert rr.returncode == 0, rr.stderr
    rows2 = measured(read_rows(r2))
    assert sum(r["tokens"]["output"] for r in rows2) == 70  # a1+a2 recovered + a3 once
    assert sum(r["turns"] for r in rows2) == 3


def test_recovered_prefix_on_same_commit_is_additive_not_replacement(tmp_path):
    """Stable-id rows for one commit are additive allocations, not latest-wins replacements.

    The child commit first owns only its new tail. After the parent's unpublished owner row is
    lost, recording that SAME child commit again recovers the older copied prefix. Both allocations
    must remain visible; a row identity keyed only by commit/session would hide the first row.
    """
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
    tool_dir, env = _e2e_env(tmp_path)
    _fork_pair_repos(env["PI_SESSIONS_DIR"], r1, r2)

    c1 = commit(r1, "parent", "2026-07-01T09:30:00Z")
    rr = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c1], r1, env)
    assert rr.returncode == 0, rr.stderr

    c2 = commit(r2, "child", "2026-07-01T10:30:00Z")
    rr = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c2], r2, env)
    assert rr.returncode == 0, rr.stderr
    rows2 = measured(read_rows(r2))
    assert sum(r["tokens"]["output"] for r in rows2) == 40

    shutil.rmtree(r1 / ".llm_resource_tally" / "local")
    rr = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c2], r2, env)
    assert rr.returncode == 0, rr.stderr
    rows2 = measured(read_rows(r2))
    assert len(rows2) == 2
    assert sum(r["tokens"]["output"] for r in rows2) == 70
    assert sum(r["turns"] for r in rows2) == 3


def test_corrupt_observation_index_keeps_same_repo_idempotence(tmp_path):
    """SQLite coordination failure may lose cross-repo dedup, never local durable ownership."""
    repo = tmp_path / "repo"
    init_repo(repo)
    tool_dir, env = _e2e_env(tmp_path)
    sessions = Path(env["PI_SESSIONS_DIR"])
    p = sessions / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    write_session(p, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", str(repo)),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "litellm", pi_usage(10, 7)),
    ])
    sha = commit(repo, "one", "2026-07-01T09:30:00Z")
    rr = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", sha], repo, env)
    assert rr.returncode == 0, rr.stderr
    assert sum(r["tokens"]["output"] for r in measured(read_rows(repo))) == 7

    index = tmp_path / ".rt_home" / "observation-claims.sqlite3"
    index.write_text("not a sqlite database")
    rr = run(tool(tool_dir) + ["reconcile", "--backend", "pi"], repo, env)
    assert rr.returncode == 0, rr.stderr
    assert sum(r["tokens"]["output"] for r in measured(read_rows(repo))) == 7


def test_new_index_does_not_persist_weak_legacy_aliases(tmp_path):
    """Compatibility aliases are lookup probes, not identities for new allocations.

    Two unrelated modern calls can share the old v1-compatible fingerprint while their canonical
    identities differ (here only response content differs). Indexing that weak alias for every new
    allocation would falsely suppress the second call.
    """
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
    tool_dir, env = _e2e_env(tmp_path)
    sessions = Path(env["PI_SESSIONS_DIR"])
    ts = "2026-07-01T09:01:00.000Z"

    m1 = pi_assistant("id-one", ts, "m", "litellm", pi_usage(10, 7))
    m2 = pi_assistant("id-two", ts, "m", "litellm", pi_usage(10, 7))
    m1["message"]["content"] = [{"type": "text", "text": "first"}]
    m2["message"]["content"] = [{"type": "text", "text": "second"}]
    p1 = sessions / "one.jsonl"
    p2 = sessions / "two.jsonl"
    write_session(p1, [pi_header("s1", "2026-07-01T09:00:00.000Z", str(r1)), m1])
    write_session(p2, [pi_header("s2", "2026-07-01T09:00:00.000Z", str(r2)), m2])
    t1, t2 = PI.parse_turns(str(p1))[0], PI.parse_turns(str(p2))[0]
    assert t1["claim_id"] != t2["claim_id"]
    assert set(t1["claim_aliases"]) & set(t2["claim_aliases"])

    c1 = commit(r1, "one", "2026-07-01T09:30:00Z")
    c2 = commit(r2, "two", "2026-07-01T09:30:00Z")
    rr1 = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c1], r1, env)
    rr2 = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c2], r2, env)
    assert rr1.returncode == 0, rr1.stderr
    assert rr2.returncode == 0, rr2.stderr
    assert sum(r["tokens"]["output"] for r in measured(read_rows(r1))) == 7
    assert sum(r["tokens"]["output"] for r in measured(read_rows(r2))) == 7


def test_unreadable_foreign_owner_is_not_mistaken_for_absent(tmp_path):
    """A transient owner-ledger read failure must not delete ownership and double bill.

    Missing owner data is recoverable; *unreadable* owner data is ambiguous. The allocator
    fails closed for that observation until the owner can be inspected again.
    """
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
    tool_dir, env = _e2e_env(tmp_path)
    _fork_pair_repos(env["PI_SESSIONS_DIR"], r1, r2)

    c1 = commit(r1, "parent", "2026-07-01T09:30:00Z")
    rr = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c1], r1, env)
    assert rr.returncode == 0, rr.stderr

    owner_ledger = r1 / ".llm_resource_tally" / "local" / "ledger.jsonl"
    clean = owner_ledger.read_text()
    owner_ledger.write_text(clean + "{definitely-not-json\n")

    c2 = commit(r2, "child", "2026-07-01T10:30:00Z")
    rr = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", c2], r2, env)
    assert rr.returncode == 0, rr.stderr
    rows2 = measured(read_rows(r2))
    assert len(rows2) == 1
    assert rows2[0]["turns"] == 1
    assert rows2[0]["tokens"]["output"] == 40  # copied a1/a2 stayed suppressed

    owner_ledger.write_text(clean)
    rr = run(tool(tool_dir) + ["reconcile", "--backend", "pi"], r2, env)
    assert rr.returncode == 0, rr.stderr
    assert sum(r["tokens"]["output"] for r in measured(read_rows(r2))) == 40


def test_observation_index_survives_publish_and_spool_clear(tmp_path):
    """Once the owning row is published, clearing the local spool does not reopen it."""
    repo = tmp_path / "repo"
    init_repo(repo)
    tool_dir, env = _e2e_env(tmp_path)
    p = Path(env["PI_SESSIONS_DIR"]) / "2026-07-01T09-00-00-000Z_11111111-0000-0000-0000-000000000001.jsonl"
    write_session(p, [
        pi_header("11111111-0000-0000-0000-000000000001", "2026-07-01T09:00:00.000Z", str(repo)),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "litellm", pi_usage(10, 7)),
    ])
    sha = commit(repo, "one", "2026-07-01T09:30:00Z")
    assert run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", sha], repo, env).returncode == 0
    r = run(tool(tool_dir) + ["publish"], repo, env)
    assert r.returncode == 0, r.stderr
    r = run(tool(tool_dir) + ["reconcile", "--backend", "pi"], repo, env)
    assert r.returncode == 0, r.stderr
    assert "nothing to reconcile" in r.stdout
    rows = measured(read_rows(repo))
    assert len(rows) == 1 and rows[0]["tokens"]["output"] == 7


def test_observation_index_rebuilds_from_current_repo_ledger(tmp_path):
    """Deleting the workstation index does not reopen observations already owned by this repo.

    The durable row carries ``observation_ids``; a later forked session in the same repository
    can therefore rebuild the local index and bill only genuinely new work.
    """
    repo = tmp_path / "repo"
    init_repo(repo)
    tool_dir, env = _e2e_env(tmp_path)
    sessions = Path(env["PI_SESSIONS_DIR"])
    u1 = "11111111-0000-0000-0000-000000000001"
    u2 = "22222222-0000-0000-0000-000000000002"
    p1 = sessions / f"2026-07-01T09-00-00-000Z_{u1}.jsonl"
    p2 = sessions / f"2026-07-01T10-00-00-000Z_{u2}.jsonl"
    a1 = pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "litellm", pi_usage(10, 7))
    write_session(p1, [pi_header(u1, "2026-07-01T09:00:00.000Z", str(repo)), a1])
    sha = commit(repo, "one", "2026-07-01T09:30:00Z")
    assert run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", sha], repo, env).returncode == 0
    assert run(tool(tool_dir) + ["publish"], repo, env).returncode == 0

    index = tmp_path / ".rt_home" / "observation-claims.sqlite3"
    assert index.exists()
    index.unlink()

    a2 = pi_assistant(
        "a2", "2026-07-01T10:01:00.000Z", "m", "litellm", pi_usage(20, 11), parent="a1"
    )
    write_session(
        p2,
        [pi_header(u2, "2026-07-01T10:00:00.000Z", str(repo), parent=str(p1)), a1, a2],
    )
    r = run(tool(tool_dir) + ["reconcile", "--backend", "pi"], repo, env)
    assert r.returncode == 0, r.stderr
    rows = measured(read_rows(repo))
    assert sum(row["tokens"]["output"] for row in rows) == 18  # 7 once + new 11
    assert sum(row["turns"] for row in rows) == 2
    assert len(indexed_claim_ids(tmp_path / ".rt_home")) == 2


def test_v1_to_v3_migration_keeps_canonical_observation_identity(tmp_path):
    """Pi's v1→v2 migration generates random tree ids; canonical identity must not use them."""
    v1 = tmp_path / "v1.jsonl"
    header = {"type": "session", "id": "s", "timestamp": "2026-07-01T09:00:00.000Z", "cwd": str(tmp_path)}
    msg = {"type": "message", "timestamp": "2026-07-01T09:01:00.000Z",
           "message": {"role": "assistant", "provider": "litellm", "model": "m",
                       "usage": pi_usage(10, 7), "stopReason": "stop",
                       "content": [{"type": "text", "text": "same"}]}}
    write_session(v1, [header, msg])
    before = PI.parse_turns(str(v1))[0]

    v3 = tmp_path / "v3.jsonl"
    migrated_header = dict(header, version=3)
    migrated_msg = dict(msg, id="deadbeef", parentId=None)
    write_session(v3, [migrated_header, migrated_msg])
    after = PI.parse_turns(str(v3))[0]
    assert before["claim_id"] == after["claim_id"]
    # The old pre-redesign fingerprints for BOTH representations remain aliases.
    assert set(before["claim_aliases"]) & set(after["claim_aliases"])


def test_from_hook_summaries_keep_usage_but_not_invent_model(tmp_path):
    """Extension compaction can use a different LLM; Pi persists usage but no provider/model."""
    p = tmp_path / "s.jsonl"
    comp = pi_compaction("c1", "2026-07-01T09:02:00.000Z", pi_usage(50, 6), parent="a1")
    comp["fromHook"] = True
    branch = pi_branch_summary("b1", "2026-07-01T09:03:00.000Z", pi_usage(20, 4), parent="a1")
    branch["fromHook"] = True
    write_session(p, [
        pi_header("s", "2026-07-01T09:00:00.000Z", str(tmp_path)),
        pi_assistant("a1", "2026-07-01T09:01:00.000Z", "local", "litellm", pi_usage(10, 1)),
        comp,
        branch,
    ])
    by_id = {t["id"]: t for t in PI.parse_turns(str(p))}
    assert by_id["a1"]["model"] == "litellm/local"
    assert by_id["c1"]["model"] == "?" and by_id["c1"]["usage"]["output_tokens"] == 6
    assert by_id["b1"]["model"] == "?" and by_id["b1"]["usage"]["output_tokens"] == 4
    agg = tally_ledger.aggregate(list(by_id.values()))
    assert agg["by_model"]["litellm/local"]["output"] == 1
    assert agg["by_model"]["?"]["output"] == 10


def test_tool_result_canonical_identity_uses_tool_call_id(tmp_path):
    p1, p2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    base = pi_tool_result("t", "2026-07-01T09:02:00.000Z", pi_usage(3, 4), parent="a")
    base["message"]["toolCallId"] = "call-A"
    other = json.loads(json.dumps(base))
    other["message"]["toolCallId"] = "call-B"
    prefix = [pi_header("s", "2026-07-01T09:00:00.000Z", str(tmp_path)),
              pi_assistant("a", "2026-07-01T09:01:00.000Z", "m", "p", pi_usage(1, 1))]
    write_session(p1, [*prefix, base])
    write_session(p2, [*prefix, other])
    c1 = {t["id"]: t for t in PI.parse_turns(str(p1))}["t"]["claim_id"]
    c2 = {t["id"]: t for t in PI.parse_turns(str(p2))}["t"]["claim_id"]
    assert c1 != c2


def test_allocate_observation_body_failure_does_not_leave_tombstone(tmp_path, monkeypatch):
    import llm_resource_tally.observation_allocation as cl

    monkeypatch.setenv("LLM_RESOURCE_TALLY_HOME", str(tmp_path / "home"))
    obs = {"claim_id": "pi-v2:" + "a" * 64, "ts": "2026-07-01T09:00:00Z"}
    with pytest.raises(ValueError, match="boom"):
        with cl.allocate_observations("pi", [obs], str(tmp_path / "repo")) as fresh:
            assert obs["claim_id"] in fresh
            raise ValueError("boom")
    assert cl.claimed_claim_ids("pi") == set()


# A driver that (1) signals readiness, (2) waits at a barrier, then (3) REPLACES itself
# with the real tally CLI, so the race is between two actual OS processes.
_RACE_DRIVER = """
import os, sys, time
ready, barrier = sys.argv[1], sys.argv[2]
open(ready, "w").close()
for _ in range(30000):  # up to ~60s; a timeout fails the test loudly instead of racing unsynchronized
    if os.path.exists(barrier):
        break
    time.sleep(0.002)
else:
    sys.exit(2)
os.execvpe(sys.executable, [sys.executable] + sys.argv[3:], os.environ)
"""


def _start_raced_recorder(driver, ready, barrier, tool_dir, repo, sha, env):
    return subprocess.Popen(
        [sys.executable, str(driver), str(ready), str(barrier),
         str(tool_dir), "record", "--backend", "pi", "--commit", sha],
        cwd=str(repo), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _barrier_release(barrier: Path):
    return barrier.write_text("")


def test_concurrent_same_observation_allocates_once(tmp_path):
    """Two REAL recorders, started simultaneously behind a barrier, race on a fork pair
    whose files share two verbatim-copied observations (plus one fork-only turn):
    exactly one allocation happens per observation — the observations' usage is billed
    exactly once in total, and each canonical observation has one indexed owner — no matter
    which recorder grabs the per-user allocation lock first.

    Regression: a non-serialized check followed by a per-repo ledger append lets two same-machine
    recorders both decide that the copied observation is fresh."""
    tool_dir = tmp_path / "tally"
    make_vendored(tool_dir)
    (tmp_path / "race_driver.py").write_text(_RACE_DRIVER)
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
    sessions = tmp_path / "sessions"
    home = tmp_path / ".rt_home"
    env = {**os.environ, **_PI_ENV, "LLM_RESOURCE_TALLY_HOME": str(home), "PI_SESSIONS_DIR": str(sessions)}

    u1 = "11111111-0000-0000-0000-000000000001"
    u2 = "22222222-0000-0000-0000-000000000002"
    a_file = sessions / f"2026-07-01T09-00-00-000Z_{u1}.jsonl"
    b_file = sessions / f"2026-07-01T10-00-00-000Z_{u2}.jsonl"
    barrier = tmp_path / "go"

    for rnd in range(5):
        # Different usage AND timestamps per round: usage changes the claim ids, and the
        # timestamps must postdate the previous round's per-session watermark or they are
        # legitimately out of window.
        o1, o2, o3 = 10 + rnd, 20 + rnd, 40 + rnd
        m1 = 10 + rnd * 10
        a1 = pi_assistant("a1", f"2026-07-01T09:{m1:02d}:00.000Z", "m", "litellm", pi_usage(100, o1))
        a2 = pi_assistant("a2", f"2026-07-01T09:{m1 + 1:02d}:00.000Z", "m", "litellm", pi_usage(100, o2), parent="a1")
        a3 = pi_assistant("a3", f"2026-07-01T11:{m1:02d}:00.000Z", "m", "litellm", pi_usage(100, o3), parent="a2")
        sessions.mkdir(exist_ok=True)
        write_session(a_file, [pi_header(u1, "2026-07-01T09:00:00.000Z", str(r1)), a1, a2])
        write_session(b_file, [pi_header(u2, "2026-07-01T10:00:00.000Z", str(r2), parent=str(a_file)), a1, a2, a3])
        sha1 = commit(r1, f"ra{rnd}", f"2026-07-01T09:{m1 + 5:02d}:00Z")
        sha2 = commit(r2, f"rb{rnd}", f"2026-07-01T11:{m1 + 5:02d}:00Z")

        ready1, ready2 = tmp_path / f"ready1-{rnd}", tmp_path / f"ready2-{rnd}"
        p1 = _start_raced_recorder(tmp_path / "race_driver.py", ready1, barrier, tool_dir, r1, sha1, env)
        p2 = _start_raced_recorder(tmp_path / "race_driver.py", ready2, barrier, tool_dir, r2, sha2, env)
        for _ in range(30000):
            if ready1.exists() and ready2.exists():
                break
            time.sleep(0.002)
        else:
            p1.kill()
            p2.kill()
            pytest.fail("racer did not reach the barrier in time")
        _barrier_release(barrier)
        out1, err1 = p1.communicate(timeout=60)
        out2, err2 = p2.communicate(timeout=60)
        assert p1.returncode == 0, err1
        assert p2.returncode == 0, err2
        if barrier.exists():
            barrier.unlink()

        rows = tally_ledger.read_ledger(root=r1) + tally_ledger.read_ledger(root=r2)
        round_rows = [r for r in rows if r.get("commit") in (sha1, sha2) and r.get("agent") == "pi"]
        total = sum((r.get("tokens") or {}).get("output", 0) for r in round_rows)
        if total != o1 + o2 + o3:  # debug: dump everything about the offending round
            claims_dbg = indexed_claim_ids(home)
            print(f"[DBG] round {rnd}: rc=({p1.returncode},{p2.returncode}) rows={len(round_rows)} total={total} want={o1 + o2 + o3}", file=sys.stderr)
            print("[DBG]   r1:", out1.decode().strip()[:300], file=sys.stderr)
            print("[DBG]   r2:", out2.decode().strip()[:300], file=sys.stderr)
            for r in rows:
                print(f"[DBG]   row: {r.get('repo')} commit={str(r.get('commit'))[:8]} sid={str(r.get('session_id'))[:8]} kind={r.get('kind')} turns={r.get('turns')}", file=sys.stderr)
            print(f"[DBG]   indexed claims: {len(claims_dbg)} -> {claims_dbg[-8:]}", file=sys.stderr)
        # One row if the fork won the race (it bills the whole prefix plus a3), two if the
        # parent won (parent bills a1+a2, fork bills only its own a3) — but the shared
        # observations are billed exactly once in total either way.
        assert 1 <= len(round_rows) <= 2, round_rows
        assert total == o1 + o2 + o3
        if len(round_rows) == 2:
            assert sorted((r.get("tokens") or {}).get("output", 0) for r in round_rows) == sorted([o3, o1 + o2])

    keys = indexed_claim_ids(home)
    assert len(keys) == 15 and len(set(keys)) == 15  # 3 observations x 5 rounds: one allocation each


def test_concurrent_compaction_estimate_allocates_once(tmp_path):
    """Usage-LESS compaction/branch-summary events (reconstructed estimates, not measured
    turns) get the SAME concurrent-allocation protection as measured turns: two recorders
    racing on a fork pair whose files share a verbatim-copied estimate event must emit
    exactly one estimate row in total and one claim record for it."""
    tool_dir = tmp_path / "tally"
    make_vendored(tool_dir)
    (tmp_path / "race_driver.py").write_text(_RACE_DRIVER)
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
    sessions = tmp_path / "sessions"
    home = tmp_path / ".rt_home"
    env = {**os.environ, **_PI_ENV, "LLM_RESOURCE_TALLY_HOME": str(home), "PI_SESSIONS_DIR": str(sessions)}

    u1 = "11111111-0000-0000-0000-000000000001"
    u2 = "22222222-0000-0000-0000-000000000002"
    a_file = sessions / f"2026-07-01T09-00-00-000Z_{u1}.jsonl"
    b_file = sessions / f"2026-07-01T10-00-00-000Z_{u2}.jsonl"
    barrier = tmp_path / "go"

    for rnd in range(3):
        o1, o2 = 10 + rnd, 20 + rnd
        m1 = 10 + rnd * 10  # postdates the previous round's watermark
        a1 = pi_assistant("a1", f"2026-07-01T09:{m1:02d}:00.000Z", "m", "litellm", pi_usage(100, o1))
        # Usage-LESS compaction: billed as a reconstructed estimate row; verbatim-copied
        # into the fork, it must be allocated (and billed) exactly once across both racers.
        c1 = pi_compaction("c1", f"2026-07-01T09:{m1 + 1:02d}:00.000Z", None, parent="a1")
        a2 = pi_assistant("a2", f"2026-07-01T11:{m1:02d}:00.000Z", "m", "litellm", pi_usage(100, o2), parent="c1")
        sessions.mkdir(exist_ok=True)
        write_session(a_file, [pi_header(u1, "2026-07-01T09:00:00.000Z", str(r1)), a1, c1])
        write_session(
            b_file,
            [pi_header(u2, "2026-07-01T10:00:00.000Z", str(r2), parent=str(a_file)), a1, c1, a2],
        )
        sha1 = commit(r1, f"ra{rnd}", f"2026-07-01T09:{m1 + 5:02d}:00Z")
        sha2 = commit(r2, f"rb{rnd}", f"2026-07-01T11:{m1 + 5:02d}:00Z")

        ready1, ready2 = tmp_path / f"ready1-{rnd}", tmp_path / f"ready2-{rnd}"
        p1 = _start_raced_recorder(tmp_path / "race_driver.py", ready1, barrier, tool_dir, r1, sha1, env)
        p2 = _start_raced_recorder(tmp_path / "race_driver.py", ready2, barrier, tool_dir, r2, sha2, env)
        for _ in range(30000):
            if ready1.exists() and ready2.exists():
                break
            time.sleep(0.002)
        else:
            p1.kill()
            p2.kill()
            pytest.fail("racer did not reach the barrier in time")
        _barrier_release(barrier)
        out1, err1 = p1.communicate(timeout=60)
        out2, err2 = p2.communicate(timeout=60)
        assert p1.returncode == 0, err1
        assert p2.returncode == 0, err2
        if barrier.exists():
            barrier.unlink()

        rows1 = [r for r in tally_ledger.read_ledger(root=r1) if r.get("commit") == sha1 and r.get("agent") == "pi"]
        rows2 = [r for r in tally_ledger.read_ledger(root=r2) if r.get("commit") == sha2 and r.get("agent") == "pi"]
        est = [r for r in rows1 + rows2 if r.get("kind") == "compaction-estimate"]
        # Exactly ONE estimate row machine-wide, whatever recorder won the allocation race;
        # the measured turns are billed exactly once in total as well.
        assert len(est) == 1, (est, out1, out2)
        total = sum((r.get("tokens") or {}).get("output", 0) for r in rows1 + rows2)
        assert total == o1 + o2

    keys = indexed_claim_ids(home)
    assert len(keys) == 9 and len(set(keys)) == 9  # 3 observations (a1, c1, a2) x 3 rounds


def test_concurrent_disjoint_sessions_both_bill(tmp_path):
    """Two recorders started simultaneously on DIFFERENT repos with unrelated sessions
    must not suppress each other: the shared per-user claims lock serializes their
    allocation sections, but disjoint observations are each billed in full (the lock
    serializes; it does not conflate)."""
    tool_dir = tmp_path / "tally"
    make_vendored(tool_dir)
    (tmp_path / "race_driver.py").write_text(_RACE_DRIVER)
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
    sessions = tmp_path / "sessions"
    home = tmp_path / ".rt_home"
    env = {**os.environ, **_PI_ENV, "LLM_RESOURCE_TALLY_HOME": str(home), "PI_SESSIONS_DIR": str(sessions)}

    u1 = "11111111-0000-0000-0000-000000000001"
    u2 = "22222222-0000-0000-0000-000000000002"
    fa = sessions / f"2026-07-01T09-00-00-000Z_{u1}.jsonl"
    fb = sessions / f"2026-07-01T10-00-00-000Z_{u2}.jsonl"
    barrier = tmp_path / "go"

    for rnd in range(3):
        o1, o2, o3, o4 = 30 + rnd, 31 + rnd, 50 + rnd, 51 + rnd
        m1 = 10 + rnd * 10  # postdates the previous round's watermark
        c1 = pi_assistant("c1", f"2026-07-01T09:{m1:02d}:00.000Z", "m", "litellm", pi_usage(100, o1))
        c2 = pi_assistant("c2", f"2026-07-01T09:{m1 + 1:02d}:00.000Z", "m", "litellm", pi_usage(100, o2), parent="c1")
        d1 = pi_assistant("d1", f"2026-07-01T11:{m1:02d}:00.000Z", "m", "litellm", pi_usage(100, o3))
        d2 = pi_assistant("d2", f"2026-07-01T11:{m1 + 1:02d}:00.000Z", "m", "litellm", pi_usage(100, o4), parent="d1")
        sessions.mkdir(exist_ok=True)
        write_session(fa, [pi_header(u1, "2026-07-01T09:00:00.000Z", str(r1)), c1, c2])
        write_session(fb, [pi_header(u2, "2026-07-01T10:00:00.000Z", str(r2)), d1, d2])
        sha1 = commit(r1, f"ra{rnd}", f"2026-07-01T09:{m1 + 5:02d}:00Z")
        sha2 = commit(r2, f"rb{rnd}", f"2026-07-01T11:{m1 + 5:02d}:00Z")

        ready1, ready2 = tmp_path / f"ready1-{rnd}", tmp_path / f"ready2-{rnd}"
        p1 = _start_raced_recorder(tmp_path / "race_driver.py", ready1, barrier, tool_dir, r1, sha1, env)
        p2 = _start_raced_recorder(tmp_path / "race_driver.py", ready2, barrier, tool_dir, r2, sha2, env)
        for _ in range(30000):
            if ready1.exists() and ready2.exists():
                break
            time.sleep(0.002)
        else:
            p1.kill()
            p2.kill()
            pytest.fail("racer did not reach the barrier in time")
        _barrier_release(barrier)
        out1, err1 = p1.communicate(timeout=60)
        out2, err2 = p2.communicate(timeout=60)
        assert p1.returncode == 0, err1
        assert p2.returncode == 0, err2
        if barrier.exists():
            barrier.unlink()

        rows1 = [r for r in tally_ledger.read_ledger(root=r1) if r.get("commit") == sha1 and r.get("agent") == "pi"]
        rows2 = [r for r in tally_ledger.read_ledger(root=r2) if r.get("commit") == sha2 and r.get("agent") == "pi"]
        total = sum((r.get("tokens") or {}).get("output", 0) for r in rows1 + rows2)
        if total != o1 + o2 + o3 + o4:
            claims_dbg = indexed_claim_ids(home)
            print(f"[DBG-d] round {rnd}: rc=({p1.returncode},{p2.returncode}) rows1={len(rows1)} rows2={len(rows2)} total={total} want={o1 + o2 + o3 + o4}", file=sys.stderr)
            print("[DBG-d]   r1:", out1.decode().strip()[:300], file=sys.stderr)
            print("[DBG-d]   r2:", out2.decode().strip()[:300], file=sys.stderr)
            print(f"[DBG-d]   indexed claims: {len(claims_dbg)}", file=sys.stderr)
        # Both repos billed in full: no cross-repo false suppression of disjoint observations.
        assert len(rows1) == 1 and (rows1[0].get("tokens") or {}).get("output") == o1 + o2
        assert len(rows2) == 1 and (rows2[0].get("tokens") or {}).get("output") == o3 + o4

    keys = indexed_claim_ids(home)
    assert len(keys) == 12 and len(set(keys)) == 12  # 4 observations x 3 rounds, all distinct


def test_allocator_fails_closed_when_current_ledger_is_unreadable(tmp_path, monkeypatch):
    """A missing workstation index must not turn an unreadable local ledger into freshness."""
    import llm_resource_tally.observation_allocation as allocation

    repo = tmp_path / "repo"
    init_repo(repo)
    home = tmp_path / "home"
    monkeypatch.setenv("LLM_RESOURCE_TALLY_HOME", str(home))
    local = repo / ".llm_resource_tally" / "local"
    local.mkdir(parents=True)
    (local / "ledger.jsonl").write_text("{not-json\n")

    obs = {
        "claim_id": "pi-v2:" + "d" * 64,
        "ts": "2026-07-01T09:00:00Z",
        "model": "litellm/m",
        "type": "assistant",
        "usage": pi_usage(10, 7),
    }
    with allocation.allocate_observations("pi", [obs], str(repo)) as fresh:
        assert fresh == set()
    # Failing closed must not leave a tombstone either: no allocation was durably made.
    assert allocation.claimed_claim_ids("pi") == set()


def test_distinct_compaction_estimates_same_timestamp_are_not_collapsed(tmp_path):
    """Stable Pi estimates dedup by observation identity, never boundary timestamp alone."""
    repo = tmp_path / "repo"
    init_repo(repo)
    tool_dir, env = _e2e_env(tmp_path)
    sessions = Path(env["PI_SESSIONS_DIR"])
    sid = "11111111-0000-0000-0000-000000000001"
    path = sessions / f"2026-07-01T09-00-00-000Z_{sid}.jsonl"
    ts = "2026-07-01T09:02:00.000Z"
    write_session(
        path,
        [
            pi_header(sid, "2026-07-01T09:00:00.000Z", str(repo)),
            pi_assistant("a1", "2026-07-01T09:01:00.000Z", "m", "litellm", pi_usage(10, 1)),
            pi_compaction("c1", ts, None, parent="a1", tokens_before=1000, summary="first"),
            pi_compaction("c2", ts, None, parent="c1", tokens_before=1100, summary="second"),
        ],
    )
    sha = commit(repo, "two-compactions", "2026-07-01T09:30:00Z")
    r = run(tool(tool_dir) + ["record", "--backend", "pi", "--commit", sha], repo, env)
    assert r.returncode == 0, r.stderr
    estimates = [r for r in read_rows(repo) if r.get("kind") == "compaction-estimate"]
    assert len(estimates) == 2
    assert {tuple(r.get("observation_ids") or []) for r in estimates} == {
        (PI.parse_compaction_events(str(path))[0]["claim_id"],),
        (PI.parse_compaction_events(str(path))[1]["claim_id"],),
    }
