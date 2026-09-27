# SPDX-License-Identifier: Apache-2.0
"""Pi parsing regression tests."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "llm_resource_tally"
sys.path.insert(0, str(REPO))
from llm_resource_tally import ledger as tally_ledger  # noqa: E402
from llm_resource_tally.backends.pi import (  # noqa: E402
    pi_munged_project_dir,
)

from pi_support import (  # noqa: E402
    PI,
    _neutralize_pi_env,  # noqa: F401
    pi_assistant,
    pi_branch_summary,
    pi_compaction,
    pi_header,
    pi_model_change,
    pi_tool_result,
    pi_usage,
    pi_usage_entry,
    write_session,
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

def test_parse_fork_emits_copies_with_stable_observations(tmp_path):
    """The parser no longer floors on the fork header: a fork file emits EVERY entry it
    contains (copied prefix included), each with a stable observation_id. Copies of the same
    observation carry the SAME observation_id in both files (verbatim copies are
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
    # ...and every verbatim copy carries the identical observation id...
    pmap = {t["id"]: t["observation_id"] for t in parent_turns}
    fmap = {t["id"]: t["observation_id"] for t in fork_turns}
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
    assert turns[0]["observation_id"].startswith("pi-v2:")
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
    parent's observation id, so the accounting layer (not the parser) keeps it billed once."""
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
        a_rec[2],  # aX:  copied - a turn again (same observation id as in the parent file)
        pi_compaction("cN", "2026-07-01T10:01:00.000Z", pi_usage(500, 4000), parent="aX", tokens_before=8000, summary="n" * 20),
    ])
    fork_turns = PI.parse_turns(str(b))
    by_id = {t["id"]: t for t in fork_turns}
    # The fork EMITS the copied aX (the accounting layer dedups it via its observation id) and
    # its own new measured compaction.
    assert set(by_id) == {"aX", "cN"}
    assert by_id["cN"]["type"] == "compaction"
    assert by_id["cN"]["model"] == "provX/modelX"  # resolved through the copied ancestry, not "?"
    # the parent session still bills its own prefix unchanged, with the same observation id.
    parent_turns = PI.parse_turns(str(a))
    assert [t["id"] for t in parent_turns] == ["aX"]
    assert parent_turns[0]["model"] == "provX/modelX"
    assert parent_turns[0]["observation_id"] == by_id["aX"]["observation_id"]
    assert by_id["cN"]["observation_id"] not in {t["observation_id"] for t in parent_turns} | {by_id["aX"]["observation_id"]}

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
    model as before. The billed assistant's ``observation_id`` must stay byte-identical to the
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

    # observation_id stability: recompute the billed assistants' fingerprints with the PRE-FIX
    # formula (the concrete response identity: id-or-timestamp, parentId, ts, kind,
    # normalized usage, provider, responseModel ?? model, stopReason) and require an
    # exact match — this change must not reopen already-claimed observations.
    def pre_fix_observation_id(rec: dict, usage: dict) -> str:
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
    assert by_id["aA"]["observation_id"].startswith("pi-v2:")
    assert pre_fix_observation_id(recs["aA"], canon(10, 2)) in by_id["aA"]["observation_aliases"]
    assert pre_fix_observation_id(recs["aB"], canon(20, 5)) in by_id["aB"]["observation_aliases"]

def test_parse_clock_skew_copied_entry_still_allocated_once(tmp_path):
    """The fork-header timestamp has NO bearing on which entries are billed (in either
    direction of skew): a copied entry whose timestamp postdates the fork header (parent
    clock ahead) and one that predates it (parent clock behind) are both emitted, and both
    carry the parent's observation ids, so the accounting layer allocates each exactly once
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
    a_rec = [r for r in (json.loads(line) for line in open(a)) if r["type"] == "message"]
    write_session(b, [
        pi_header("22222222-0000-0000-0000-000000000002", "2026-07-01T10:00:00.000Z", repo, parent=str(a)),
        a_rec[0],
        a_rec[1],
    ])
    pmap = {t["id"]: t["observation_id"] for t in PI.parse_turns(str(a))}
    fmap = {t["id"]: t["observation_id"] for t in PI.parse_turns(str(b))}
    # Both skew directions are emitted (no floor) and both match their source observation.
    assert set(fmap) == {"a1", "a2"}
    assert fmap == pmap  # verbatim copies -> identical observation ids, whatever the clocks say

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
    assert before["observation_id"] == after["observation_id"]
    # The old pre-redesign fingerprints for BOTH representations remain aliases.
    assert set(before["observation_aliases"]) & set(after["observation_aliases"])

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
    c1 = {t["id"]: t for t in PI.parse_turns(str(p1))}["t"]["observation_id"]
    c2 = {t["id"]: t for t in PI.parse_turns(str(p2))}["t"]["observation_id"]
    assert c1 != c2
