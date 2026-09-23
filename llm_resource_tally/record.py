# SPDX-License-Identifier: Apache-2.0
"""record / reconcile — attribute a session's measured turns to commits. Backend-agnostic:
turns and compaction events come from whichever `Backend` the CLI selected."""

from __future__ import annotations

import os
import sys

from . import claims
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
    rows: list[dict], session_id: str, agent: str, repo_abs: str, claim_source: str
):
    """Latest observation already allocated for this session, locally or in another repo.

    The repository ledger supplies the durable local watermark. The per-user claims log supplies
    a best-effort cross-repo watermark so a submodule commit followed by a parent gitlink bump (or
    any other sequential cross-repo commit) cannot charge the same transcript prefix twice.
    """
    local = session_watermark(rows, session_id, agent)
    external = claims.external_claimed_ceiling(session_id, repo_abs, claim_source)
    dated = [(to_dt(ts), ts) for ts in (local, external) if ts]
    if not dated:
        return None, ""
    floor_dt, floor_ts = max(dated, key=lambda item: item[0])
    return floor_dt, floor_ts


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

    Usage-less estimate events are copied verbatim into forks too, so — like measured
    turns — they are allocated through the observation claim log in ONE locked section
    per batch (see claims.allocate_event_claims): the unclaimed check, every ledger
    append, and the claim append all happen under the per-user claims lock, so a
    concurrent same-machine recorder cannot allocate an estimate this pass allocates.
    """
    seen = recorded_boundary_ts(rows, session_id, backend.name)
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
        claim_ids = [ev["claim_id"] for ev in events if ev.get("claim_id")]
        billed = []
        with claims.allocate_event_claims(backend.name, claim_ids, repo_abs) as fresh:
            for ev in events:
                claim = ev.get("claim_id")
                if claim and claim not in fresh:
                    # This observation was already allocated from another copy (any repo
                    # on this machine): not billed again.
                    continue
                billed.append(ev)
                append_row(
                    compaction_row(ev, sha, commit_ts, session_id, activity, repo, backend.name)
                )
                seen.add(ev["boundary_ts"])
                n += 1
        for ev in billed:
            # Printed only AFTER the claims lock is released (and after every ledger
            # append in the batch is done): a failed print must never abort the claim
            # append for rows that were already written.
            print(
                f"  + compaction @ {ev['boundary_ts']}: peak_context~{ev['peak_context_tokens']:,} tok, "
                f"summary={ev['summary_chars']:,} chars [{ev['model']}] "
                f"(measured signals; token cost imputed post-hoc)"
            )
        if n:
            # Recorded after the context released the claims lock (re-acquiring flock
            # from this process would block forever); one call, at the latest boundary.
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
    wm_dt, wm = _session_attribution_floor(rows, session_id, backend.name, repo_abs, claim_source)
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
            claim_ids = [t["claim_id"] for t in new if t.get("claim_id")]
            appended = False
            with claims.allocate_event_claims(backend.name, claim_ids, repo_abs) as fresh:
                # ONE locked section: the unclaimed check, the ledger append, and the
                # claim append (context exit) — a concurrent recorder on this machine
                # cannot allocate the same observation in the meantime.
                if not args.force:
                    # A turn carrying a stable claim_id (Pi: an observation copied verbatim
                    # into fork/clone files) is not billed again from another copy;
                    # --force is an explicit manual re-bill and opts out of the guard (the
                    # claim is still recorded on exit).
                    billed = [t for t in new if not t.get("claim_id") or t["claim_id"] in fresh]
                else:
                    billed = new
                if not billed:
                    print(
                        f"no new turns for session {session_id[:8]} in ({wm or 'epoch'}, {commit_ts}]; "
                        f"{len(new)} turn(s) already allocated by this machine's observation claim log."
                    )
                else:
                    agg = aggregate(billed)
                    row = {**base_row(sha, commit_ts, session_id, args.label, repo, backend.name), **agg}
                    append_row(row)
                    appended = True
            if appended:
                # Recorded after the context released the claims lock (re-acquiring flock
                # from this process would block forever); make this allocation visible to
                # later record/reconcile calls in another repo.
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
            wm_dt, _ = _session_attribution_floor(rows, sid, backend.name, repo_abs, claim_source)
            turns = backend.parse_turns(f)
            new = [t for t in turns if wm_dt is None or to_dt(t["ts"]) > wm_dt]
            if new:
                claim_ids = [t["claim_id"] for t in new if t.get("claim_id")]
                # Same observation-claim guard as normal recording, in ONE locked section
                # (unclaimed check → ledger append → claim append): a turn whose stable
                # claim_id was already allocated from another copy (any repo) is not swept.
                appended = False
                with claims.allocate_event_claims(backend.name, claim_ids, repo_abs) as fresh:
                    billed = [t for t in new if not t.get("claim_id") or t["claim_id"] in fresh]
                    if billed:
                        agg = aggregate(billed)
                        row = {
                            **base_row(pending, None, sid, args.label, repo, backend.name),
                            **agg,
                            "note": "reconcile: un-committed turns swept so they are not undercounted",
                        }
                        append_row(row)
                        appended = True
                if appended:
                    # After the lock is released (re-acquiring flock would block forever).
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
