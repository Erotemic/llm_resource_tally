# SPDX-License-Identifier: Apache-2.0
"""record / reconcile — attribute a session's measured turns to commits. Backend-agnostic:
turns and compaction events come from whichever `Backend` the CLI selected."""

from __future__ import annotations

import os
import sys

from . import claims, observation_allocation
from ._util import now_iso, to_dt
from .backends import get_backend
from .config import registered_backends
from .gitutil import commit_meta, repo_root
from .ledger import (
    aggregate,
    append_row,
    base_row,
    compaction_row,
    read_ledger,
    recorded_boundary_ts,
    row_identity,
    session_watermark,
)


def _session_attribution_floor(
    rows: list[dict],
    session_id: str,
    agent: str,
    repo_abs: str,
    claim_source: str,
    *,
    use_external_claims: bool = True,
):
    """Latest observation already allocated for this session, locally or in another repo.

    The repository ledger supplies the durable local watermark. The per-user claims log supplies
    a best-effort cross-repo watermark so a submodule commit followed by a parent gitlink bump (or
    any other sequential cross-repo commit) cannot charge the same transcript prefix twice.
    """
    local = session_watermark(rows, session_id, agent)
    external = (
        claims.external_claimed_ceiling(session_id, repo_abs, claim_source)
        if use_external_claims
        else None
    )
    dated = [(to_dt(ts), ts) for ts in (local, external) if ts]
    if not dated:
        return None, ""
    floor_dt, floor_ts = max(dated, key=lambda item: item[0])
    return floor_dt, floor_ts


def _require_stable_observation_ids(backend, observations, label: str) -> None:
    """Fail closed if a backend promised durable observation identity but omitted it.

    Stable-id backends deliberately stop using session timestamp floors. Billing an anonymous
    observation in that mode would make repeated record/reconcile passes non-idempotent and would
    create a ledger row that cannot participate in ownership recovery.
    """
    if not backend.stable_observation_ids:
        return
    missing = [obs for obs in observations if not obs.get("observation_id")]
    if missing:
        raise ValueError(
            f"backend {backend.name} declared stable observation ids but {len(missing)} "
            f"{label} record(s) had no observation_id"
        )


def record_compactions(
    backend,
    transcript,
    session_id,
    sha,
    commit_ts,
    lo_dt,
    hi_dt,
    rows,
    activity,
    repo,
    repo_abs,
    claim_source,
) -> int:
    """Append a reconstructed row for each compaction boundary in (lo_dt, hi_dt] not
    already recorded. hi_dt=None means unbounded (trailing sweep, for reconcile).

    Usage-less estimate events can be copied into forks too, so — like measured turns —
    backends with stable observation ids allocate them through the ledger-backed observation
    index in one serialized section. The owning ledger row persists the id; the workstation
    index is finalized only after that durable ownership is visible.
    """
    # Stable-observation backends deduplicate compaction estimates by source observation id,
    # not by boundary timestamp. Two distinct calls can in principle share a timestamp, and a
    # lost owner row must become recoverable even if another boundary at that instant survives.
    seen = set() if backend.stable_observation_ids else recorded_boundary_ts(rows, session_id, backend.name)
    events = []
    for ev in backend.parse_compaction_events(transcript):
        bts = ev["boundary_ts"]
        if bts in seen:
            continue
        bdt = to_dt(bts)
        if lo_dt is not None and bdt <= lo_dt:
            continue
        if hi_dt is not None and bdt > hi_dt:
            continue
        events.append(ev)
    n = 0
    if events:
        _require_stable_observation_ids(backend, events, "compaction")
        billed = []
        with observation_allocation.allocate_observations(backend.name, events, repo_abs) as fresh:
            for ev in events:
                observation_id = ev.get("observation_id")
                if observation_id and observation_id not in fresh:
                    continue
                billed.append(ev)
                append_row(
                    compaction_row(ev, sha, commit_ts, session_id, activity, repo, backend.name)
                )
                seen.add(ev["boundary_ts"])
                n += 1
        for ev in billed:
            # Printed only AFTER the allocation lock is released and ledger ownership has
            # been finalized: presentation failures cannot affect accounting state.
            print(
                f"  + compaction @ {ev['boundary_ts']}: peak_context~{ev['peak_context_tokens']:,} tok, "
                f"summary={ev['summary_chars']:,} chars [{ev['model']}] "
                f"(measured signals; token cost imputed post-hoc)"
            )
        if n and not backend.stable_observation_ids:
            claims.record_claim(
                session_id, repo_abs, max(ev["boundary_ts"] for ev in billed), claim_source
            )
    return n


def cmd_record(args) -> None:
    """Explicit path when the caller names a backend/session/transcript (single backend,
    normal discovery). Otherwise — the passive hook case, bare `record --commit HEAD` — walk
    the repo's registered backends and record whichever has a session matching this repo
    (strict; no global fallback), so Codex/mixed repos auto-record without a backend flag."""
    repo = os.path.basename(repo_root())
    if args.backend or args.transcript or args.session:
        backend = get_backend(args.backend)
        projects = args.projects_dir or backend.default_projects_dir()
        transcript = args.transcript or backend.find_transcript(projects, args.session)
        _record_transcript(backend, transcript, args, repo)
        return
    names = registered_backends()
    recorded = False
    errors = []
    for name in names:
        try:
            backend = get_backend(name)
            projects = args.projects_dir or backend.default_projects_dir()
            transcript = backend.find_transcript(projects, None, strict=True)
            if transcript:
                _record_transcript(backend, transcript, args, repo)
                recorded = True
        except Exception as exc:  # keep other registered backends observable in passive-hook mode
            errors.append(f"{name}: {exc}")
    if not recorded and not errors:
        print(f"no matching session for any registered backend ({', '.join(names)}).")
    if errors:
        sys.exit("error: passive recording incomplete; " + "; ".join(errors))


def _record_transcript(backend, transcript, args, repo) -> None:
    session_id = backend.session_id(transcript) or os.path.splitext(os.path.basename(transcript))[0]
    sha, commit_ts = commit_meta(args.commit)
    rows = read_ledger()
    repo_abs = repo_root()
    claim_source = claims.claim_source_id(backend.name, transcript)

    # Attribution window = (latest local OR cross-repo allocation floor, commit timestamp].
    # Bounding the top at commit_ts (not "now") keeps work done AFTER this commit rolling
    # forward. Including another repository's local claim prevents a submodule commit followed
    # by a parent gitlink bump from charging the same transcript prefix twice.
    if backend.stable_observation_ids:
        # Stable-observation backends must not use a session timestamp as a lower allocation
        # bound. A fork can bill only its new tail while older copied observations remain owned
        # by another repo; if that owner later disappears, those retained older observations
        # must become recoverable. Their observation ids (plus visible ledger ownership) decide whether
        # they are already billed. The commit timestamp below is still the upper bound, so work
        # performed after this commit rolls forward normally.
        wm_dt, wm = None, ""
    else:
        wm_dt, wm = _session_attribution_floor(
            rows,
            session_id,
            backend.name,
            repo_abs,
            claim_source,
            use_external_claims=True,
        )
    cut_dt = to_dt(commit_ts)

    measured_key = ("measured", backend.name, session_id, sha)
    measured_dup = not args.force and any(row_identity(r) == measured_key for r in rows)
    if measured_dup:
        print(
            f"already recorded session {session_id[:8]} @ commit {sha[:8]} "
            f"(use --force to override); measured turns skipped."
        )
    else:
        turns = backend.parse_turns(transcript)
        new = [
            t
            for t in turns
            if (wm_dt is None or to_dt(t["ts"]) > wm_dt) and to_dt(t["ts"]) <= cut_dt
        ]
        if not new:
            print(f"no new turns for session {session_id[:8]} in ({wm or 'epoch'}, {commit_ts}].")
        else:
            _require_stable_observation_ids(backend, new, "turn")
            appended = False
            if args.force:
                billed = new
                agg = aggregate(billed)
                row = {**base_row(sha, commit_ts, session_id, args.label, repo, backend.name), **agg}
                row["observation_ids"] = [t["observation_id"] for t in billed if t.get("observation_id")]
                append_row(row)
                appended = True
            else:
                with observation_allocation.allocate_observations(backend.name, new, repo_abs) as fresh:
                    billed = [t for t in new if not t.get("observation_id") or t["observation_id"] in fresh]
                    if not billed:
                        print(
                            f"no new turns for session {session_id[:8]} in ({wm or 'epoch'}, {commit_ts}]; "
                            f"{len(new)} turn(s) already allocated by visible ledger ownership."
                        )
                    else:
                        agg = aggregate(billed)
                        row = {**base_row(sha, commit_ts, session_id, args.label, repo, backend.name), **agg}
                        row["observation_ids"] = [t["observation_id"] for t in billed if t.get("observation_id")]
                        append_row(row)
                        appended = True
            if appended:
                # Non-observation-id backends still publish the older session-level local
                # claim after the allocation section; stable-id backends use ledger ownership.
                if not backend.stable_observation_ids:
                    claims.record_claim(session_id, repo_abs, agg["turn_ts_range"][1], claim_source)
                tk = agg["tokens"]
                print(
                    f"recorded {agg['turns']} turns for {sha[:8]} [{','.join(agg['models'])}]"
                    f"{(' <' + args.label + '>') if args.label else ''}: "
                    f"out={tk['output']} in={tk['input']} cache_w={tk['cache_write']} "
                    f"cache_r={tk['cache_read']}; wall={agg['time']['wall_clock_s']}s; "
                    f"inference-time/energy/carbon modeled post-hoc."
                )

    if not args.no_estimate_compaction:
        record_compactions(
            backend,
            transcript,
            session_id,
            sha,
            commit_ts,
            wm_dt,
            cut_dt,
            rows,
            args.label,
            repo,
            repo_abs,
            claim_source,
        )


def cmd_reconcile(args) -> None:
    """Allocate retained/discoverable trailing turns to a pending bucket.

    This closes gaps within transcript coverage; it cannot recover sessions that are absent,
    unsupported, or already pruned.
    """
    names = [args.backend] if args.backend else registered_backends()
    rows = read_ledger()
    repo_abs = repo_root()
    repo = os.path.basename(repo_abs)
    total = 0
    pending = f"pending@{now_iso()[:10]}"
    for name in names:
        backend = get_backend(name)
        projects = args.projects_dir or backend.default_projects_dir()
        for f in backend.session_transcripts(projects):
            sid = backend.session_id(f) or os.path.splitext(os.path.basename(f))[0]
            # Use the same local + cross-repo allocation floor as normal commit recording.
            claim_source = claims.claim_source_id(backend.name, f)
            if backend.stable_observation_ids:
                # Reconsider every retained observation. Durable observation ids, not a time floor,
                # are what make this idempotent and let an older copied prefix heal after its
                # previous owner ledger disappears.
                wm_dt = None
            else:
                wm_dt, _ = _session_attribution_floor(
                    rows,
                    sid,
                    backend.name,
                    repo_abs,
                    claim_source,
                    use_external_claims=True,
                )
            turns = backend.parse_turns(f)
            new = [t for t in turns if wm_dt is None or to_dt(t["ts"]) > wm_dt]
            if new:
                _require_stable_observation_ids(backend, new, "turn")
                appended = False
                with observation_allocation.allocate_observations(backend.name, new, repo_abs) as fresh:
                    billed = [t for t in new if not t.get("observation_id") or t["observation_id"] in fresh]
                    if billed:
                        agg = aggregate(billed)
                        row = {
                            **base_row(pending, None, sid, args.label, repo, backend.name),
                            **agg,
                            "note": "reconcile: un-committed turns swept so they are not undercounted",
                            "observation_ids": [t["observation_id"] for t in billed if t.get("observation_id")],
                        }
                        append_row(row)
                        appended = True
                if appended:
                    # Stable-id backends already finalized durable observation ownership;
                    # older backends still update the session-level local claim here.
                    if not backend.stable_observation_ids:
                        claims.record_claim(sid, repo_abs, agg["turn_ts_range"][1], claim_source)
                    total += agg["turns"]
                    print(
                        f"reconciled {agg['turns']} un-recorded turns for session {sid[:8]}"
                        f" [{backend.name}]{(' <' + args.label + '>') if args.label else ''}."
                    )
            if not args.no_estimate_compaction:
                total += record_compactions(
                    backend,
                    f,
                    sid,
                    pending,
                    None,
                    wm_dt,
                    None,
                    rows,
                    args.label,
                    repo,
                    repo_abs,
                    claim_source,
                )
    if total == 0:
        print("nothing to reconcile among retained, discovered session turns.")
