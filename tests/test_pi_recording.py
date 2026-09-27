# SPDX-License-Identifier: Apache-2.0
"""Pi recording regression tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "llm_resource_tally"
sys.path.insert(0, str(REPO))
from llm_resource_tally.backends.pi import (  # noqa: E402
    pi_munged_project_dir,
)

from pi_support import (  # noqa: E402
    _e2e_env,
    _fork_pair_repos,
    _neutralize_pi_env,  # noqa: F401
    commit,
    indexed_observation_ids,
    init_repo,
    make_vendored,
    measured,
    pi_assistant,
    pi_compaction,
    pi_header,
    pi_usage,
    read_rows,
    run,
    set_mtime,
    tool,
    write_session,
)

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
    assert len(indexed_observation_ids(tmp_path / ".rt_home")) == 3

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

def test_e2e_record_parent_unbilled_fork_first(tmp_path):
    """The parent session is never billed before the fork commits: the fork (child repo)
    bills the WHOLE copied prefix from its own copy, and the parent's later commit must
    then add nothing (its copies are already claimed) — nothing is lost, nothing doubles."""
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
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
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
    init_repo(r1)
    init_repo(r2)
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
    ids against the current session's entry map): a bare-id observation id would let the first
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
    keys = indexed_observation_ids(tmp_path / ".rt_home")
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
        keys = indexed_observation_ids(tmp_path / root / ".rt_home")
        assert len(keys) == 3 and len(set(keys)) == 3

def test_e2e_reconcile_idempotent(tmp_path):
    """Re-reconciling, and then recording the commit that was already reconciled, adds
    nothing: the sweep and the post-commit record share the same allocation/watermark state, so
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
    keys = indexed_observation_ids(tmp_path / ".rt_home")
    assert len(keys) == 2 and len(set(keys)) == 2

def test_e2e_compaction_estimate_allocates_once(tmp_path):
    """A usage-LESS compaction (a reconstructed estimate event, not a measured turn) is
    copied verbatim into forks too: it carries the same stable observation id as its underlying
    entry, so the first repo to record it emits the estimate row and the fork's copy is
    suppressed — the exact-once principle covers estimate events, not just measured turns."""
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
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
    keys = indexed_observation_ids(tmp_path / ".rt_home")
    assert len(keys) == 3 and len(set(keys)) == 3  # a1, c1 (estimate), a2
