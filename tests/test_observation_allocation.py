# SPDX-License-Identifier: Apache-2.0
"""Observation allocation regression tests."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "llm_resource_tally"
sys.path.insert(0, str(REPO))
from llm_resource_tally import ledger as tally_ledger  # noqa: E402
from llm_resource_tally import record as tally_record  # noqa: E402

from pi_support import (  # noqa: E402
    PI,
    _PI_ENV,
    _barrier_release,
    _e2e_env,
    _fork_pair_repos,
    _neutralize_pi_env,  # noqa: F401
    _start_raced_recorder,
    _strip_observation_ids_from_local_ledger,
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
    tool,
    write_session,
)

def test_stable_backend_requires_observation_ids():
    class StableBackend:
        name = "stable-test"
        stable_observation_ids = True

    with pytest.raises(ValueError, match="stable observation ids"):
        tally_record._require_stable_observation_ids(
            StableBackend(), [{"id": "missing", "ts": "2026-07-01T09:00:00Z"}], "turn"
        )

def test_legacy_event_claim_import_detects_later_file_changes(tmp_path, monkeypatch):
    import llm_resource_tally.observation_allocation as cl

    home = tmp_path / "home"
    monkeypatch.setenv("LLM_RESOURCE_TALLY_HOME", str(home))
    assert cl.claimed_observation_ids("pi") == set()  # create DB while legacy file is absent
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
    old_alias = PI.parse_turns(str(p1))[0]["observation_aliases"][0]
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
    old_alias = PI.parse_turns(str(p1))[0]["observation_aliases"][0]

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
    assert len(indexed_observation_ids(tmp_path / ".rt_home")) == 1

    shutil.rmtree(repo / ".llm_resource_tally" / "local")
    assert measured(read_rows(repo)) == []
    r = run(tool(tool_dir) + ["reconcile", "--backend", "pi"], repo, env)
    assert r.returncode == 0, r.stderr
    rows = measured(read_rows(repo))
    assert len(rows) == 1 and rows[0]["tokens"]["output"] == 7
    assert len(indexed_observation_ids(tmp_path / ".rt_home")) == 1

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
    assert t1["observation_id"] != t2["observation_id"]
    assert set(t1["observation_aliases"]) & set(t2["observation_aliases"])

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
    assert len(indexed_observation_ids(tmp_path / ".rt_home")) == 2

def test_allocate_observation_body_failure_does_not_leave_tombstone(tmp_path, monkeypatch):
    import llm_resource_tally.observation_allocation as cl

    monkeypatch.setenv("LLM_RESOURCE_TALLY_HOME", str(tmp_path / "home"))
    obs = {"observation_id": "pi-v2:" + "a" * 64, "ts": "2026-07-01T09:00:00Z"}
    with pytest.raises(ValueError, match="boom"):
        with cl.allocate_observations("pi", [obs], str(tmp_path / "repo")) as fresh:
            assert obs["observation_id"] in fresh
            raise ValueError("boom")
    assert cl.claimed_observation_ids("pi") == set()

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
        # Different usage AND timestamps per round: usage changes the observation ids, and the
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
            claims_dbg = indexed_observation_ids(home)
            print(f"[DBG] round {rnd}: rc=({p1.returncode},{p2.returncode}) rows={len(round_rows)} total={total} want={o1 + o2 + o3}", file=sys.stderr)
            print("[DBG]   r1:", out1.decode().strip()[:300], file=sys.stderr)
            print("[DBG]   r2:", out2.decode().strip()[:300], file=sys.stderr)
            for r in rows:
                print(f"[DBG]   row: {r.get('repo')} commit={str(r.get('commit'))[:8]} sid={str(r.get('session_id'))[:8]} kind={r.get('kind')} turns={r.get('turns')}", file=sys.stderr)
            print(f"[DBG]   indexed observations: {len(claims_dbg)} -> {claims_dbg[-8:]}", file=sys.stderr)
        # One row if the fork won the race (it bills the whole prefix plus a3), two if the
        # parent won (parent bills a1+a2, fork bills only its own a3) — but the shared
        # observations are billed exactly once in total either way.
        assert 1 <= len(round_rows) <= 2, round_rows
        assert total == o1 + o2 + o3
        if len(round_rows) == 2:
            assert sorted((r.get("tokens") or {}).get("output", 0) for r in round_rows) == sorted([o3, o1 + o2])

    keys = indexed_observation_ids(home)
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

    keys = indexed_observation_ids(home)
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
            claims_dbg = indexed_observation_ids(home)
            print(f"[DBG-d] round {rnd}: rc=({p1.returncode},{p2.returncode}) rows1={len(rows1)} rows2={len(rows2)} total={total} want={o1 + o2 + o3 + o4}", file=sys.stderr)
            print("[DBG-d]   r1:", out1.decode().strip()[:300], file=sys.stderr)
            print("[DBG-d]   r2:", out2.decode().strip()[:300], file=sys.stderr)
            print(f"[DBG-d]   indexed observations: {len(claims_dbg)}", file=sys.stderr)
        # Both repos billed in full: no cross-repo false suppression of disjoint observations.
        assert len(rows1) == 1 and (rows1[0].get("tokens") or {}).get("output") == o1 + o2
        assert len(rows2) == 1 and (rows2[0].get("tokens") or {}).get("output") == o3 + o4

    keys = indexed_observation_ids(home)
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
        "observation_id": "pi-v2:" + "d" * 64,
        "ts": "2026-07-01T09:00:00Z",
        "model": "litellm/m",
        "type": "assistant",
        "usage": pi_usage(10, 7),
    }
    with allocation.allocate_observations("pi", [obs], str(repo)) as fresh:
        assert fresh == set()
    # Failing closed must not leave a tombstone either: no allocation was durably made.
    assert allocation.claimed_observation_ids("pi") == set()

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
        (PI.parse_compaction_events(str(path))[0]["observation_id"],),
        (PI.parse_compaction_events(str(path))[1]["observation_id"],),
    }
