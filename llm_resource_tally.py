#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Kitware, Inc. and Jon Crall
"""llm_resource_tally.py — measured LLM-usage accounting for this repo.

WHY
    Every commit here is produced by an LLM agent. To make the *resource cost* of
    that work legible, we record — per commit — the **measured** token usage and
    model, plus a time estimate, into an append-only ledger. Energy and carbon are
    *deferred*: they are derived later from the recorded tokens/time and the commit
    timestamp (which fixes the grid's carbon intensity at that moment). See AGENTS.md.

WHAT IS RECORDED (measurements only)
    measured  : model, input/cache-write/cache-read/output tokens, server-tool
                calls, wall-clock span, turn timestamps — all read verbatim from the
                agent's own session transcript (Claude Code writes a `usage` object per
                turn). For context-compaction (which the harness does not bill in the
                transcript) we keep the two measured signals a later pass can model
                from: peak preceding context and summary length in chars.
    NOT stored : anything needing a modeling knob — inference_seconds (throughput),
                compaction token cost (chars→tokens), energy_kwh, carbon_gco2e. These
                are left out/null and derived by a regenerable post-hoc pass, so the
                assumption can change without re-recording.

DESIGN (the constraints this satisfies)
    * No double-count under concurrency: usage is attributed **per session**. Each
      agent has its own transcript file (filename = session id), so two agents in
      the same repo touch disjoint turns. The ledger is append-only JSONL keyed by
      (session_id, commit); re-running for the same pair is a no-op without --force.
    * No undercount: a run sweeps *all* of the session's turns since that session's
      last recorded watermark — committed or not. `reconcile` attributes any
      trailing un-recorded turns to a pending bucket so nothing is lost.
    * Minimal agent tokens: the agent runs ONE command (or nothing, via the
      post-commit hook in dev/llm_resource_tally/hooks/). Language-agnostic: a Codex agent
      can pass `--transcript <path>` to its own log.

    This whole folder is self-contained and copy-pasteable between repos: when vendored,
    the ledger and rollup live under `<this folder>/data/`, next to this file. When the
    folder is a git *submodule* its files belong to the submodule, so the ledger (which
    must be committed to the HOST repo) is written to `<host>/.llm_resource_tally/`
    instead. Either way no host-repo layout is assumed. See README.md to install.

USAGE
    python dev/llm_resource_tally/llm_resource_tally.py record        # attribute new turns -> HEAD
    python dev/llm_resource_tally/llm_resource_tally.py record --commit <sha>
    python dev/llm_resource_tally/llm_resource_tally.py rollup        # refresh lifetime totals
    python dev/llm_resource_tally/llm_resource_tally.py show          # print the ledger
    python dev/llm_resource_tally/llm_resource_tally.py reconcile     # catch un-recorded turns

    Optional: --transcript PATH | --session ID | --label TEXT | --force | --projects-dir DIR
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

# The ledger stores ONLY measurements (and the identifiers/timestamps needed to
# attribute them). It records NO modeling assumptions: anything that requires a knob
# — inference-seconds (needs a throughput assumption), compaction token cost (needs a
# chars→tokens assumption), energy, carbon — is left out or null and is computed by a
# separate, regenerable post-hoc pass over these measurements. That way any modeling
# choice can change later without re-recording, because the raw observations are kept.
SCHEMA = "resource-ledger/v2"

# ---- distribution / self-install ----------------------------------------------
# The VENDORED copy of this folder is the source of truth: everything needed to
# (re)install lives here and gets committed into the host repo, so `install` works
# with zero network. Hosting is only a convenience for the first `curl | sh` and for
# `update`. SET CANONICAL_REPO to your GitHub "owner/name" to enable `update`; until
# then the vendored copy still works fully offline. See install.sh and README.md.
CANONICAL_REPO = "Erotemic/llm_resource_tally"


def tool_version() -> str:
    """Version truth. Vendored/submodule: the VERSION file next to this module. Pip:
    that file isn't installed, so fall back to the installed package's metadata."""
    try:
        with open(os.path.join(module_dir(), "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "0.0.0"
    except OSError:
        try:
            from importlib.metadata import version, PackageNotFoundError
            try:
                return version("llm_resource_tally")
            except PackageNotFoundError:
                return "0.0.0"
        except Exception:
            return "0.0.0"

# Context-compaction is a real LLM call the harness performs but does NOT log a usage
# object for (only a `compact_boundary` marker + an `isCompactSummary` text record). So
# it is invisible to parse_turns(). We do NOT fabricate token counts for it here; we
# record only the two MEASURED signals a later pass can model from — the peak preceding
# context (what the summarizer had to read) and the summary length in chars. See
# parse_compaction_events().
COMPACTION_KIND = "compaction-estimate"

TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "output_tokens")


# --------------------------------------------------------------------------- git
def git(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def repo_root() -> str:
    return git("rev-parse", "--show-toplevel")


def superproject_root() -> str:
    """Parent repo working tree if we are a submodule, else our own toplevel."""
    sp = git("rev-parse", "--show-superproject-working-tree")
    return sp or repo_root()


def commit_meta(ref: str) -> tuple[str, str]:
    """Return (full_sha, committer_date_iso) for `ref`."""
    sha = git("rev-parse", ref)
    ts = git("show", "-s", "--format=%cI", sha)
    return sha, ts


# ------------------------------------------------------------------- transcripts
def munged_project_dir(path: str) -> str:
    """Reproduce Claude Code's project-dir encoding: it stores transcripts under
    `<projects>/<encoded-cwd>/` where the cwd is encoded by replacing EVERY
    non-alphanumeric character (`/`, `_`, `.`, space, …) with `-` — not just `/`.
    (So `/home/u/llm_resource_tally` -> `-home-u-llm-resource-tally`.) Getting this
    exact matters: `reconcile` globs this dir directly, with no fallback. Note this
    encoding is lossy — `_` and `-` both map to `-` (Claude's own limitation)."""
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


def default_projects_dir() -> str:
    """Base dir for transcripts. `CLAUDE_PROJECTS_DIR` overrides outright; otherwise
    `<CLAUDE_CONFIG_DIR or ~/.claude>/projects` (Claude Code honors CLAUDE_CONFIG_DIR)."""
    env = os.environ.get("CLAUDE_PROJECTS_DIR")
    if env:
        return os.path.expanduser(env)
    cfg = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
    return os.path.join(cfg, "projects")


def find_session_transcript(projects_dir: str, session: str | None) -> str:
    """Locate the transcript for the current (or named) session.

    Prefers the project dir munged from the *superproject* cwd (the agent's cwd is
    typically the top-level repo, not this submodule). Falls back to a repo-wide
    scan. Picks the most-recently-modified `.jsonl` unless `--session` is given.
    """
    proj = os.path.join(projects_dir, munged_project_dir(superproject_root()))
    candidates = sorted(glob.glob(os.path.join(proj, "*.jsonl")),
                        key=os.path.getmtime, reverse=True)
    if not candidates:  # fall back: any transcript under projects_dir
        candidates = sorted(glob.glob(os.path.join(projects_dir, "**", "*.jsonl"),
                                      recursive=True),
                            key=os.path.getmtime, reverse=True)
    if session:
        for c in candidates:
            if os.path.splitext(os.path.basename(c))[0] == session:
                return c
        sys.exit(f"error: no transcript for session {session} under {projects_dir}")
    if not candidates:
        sys.exit(f"error: no session transcripts found under {projects_dir}")
    return candidates[0]


def parse_turns(transcript: str) -> list[dict]:
    """Extract every billed turn bearing a usage object, deduped by message id.

    We intentionally do NOT filter on `type == "assistant"`: any record that carries
    a `usage` object is a real billed API call and must be counted. Today usage only
    appears in `assistant` records, but this also future-proofs against *auxiliary
    LLM operations* — context **compaction/summarization**, title generation, etc. —
    whenever the harness logs their usage under a different record type. Each turn is
    tagged with its record `type` so those ops can be classified/split out later.

    Returns list of {id, ts, type, model, usage{...}, web_search, web_fetch}.
    Streaming can emit a message id more than once; the last occurrence wins.
    (Caveat: an op whose usage is *never written to the transcript* — e.g. `ai-title`
    records carry no usage — is still invisible here; that needs billing data.)
    """
    by_id: dict[str, dict] = {}
    with open(transcript, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            usage = (msg.get("usage") or rec.get("usage")) or {}
            if not usage:
                continue
            mid = (msg.get("id") or rec.get("requestId")
                   or rec.get("uuid") or rec.get("timestamp"))
            st = usage.get("server_tool_use") or {}
            by_id[mid] = {
                "id": mid,
                "ts": rec.get("timestamp"),
                "type": rec.get("type", "?"),
                "model": msg.get("model", "?"),
                "usage": {k: int(usage.get(k, 0) or 0) for k in TOKEN_KEYS},
                "web_search": int(st.get("web_search_requests", 0) or 0),
                "web_fetch": int(st.get("web_fetch_requests", 0) or 0),
            }
    turns = [t for t in by_id.values() if t["ts"]]
    turns.sort(key=lambda t: t["ts"])
    return turns


def _context_size(usage: dict) -> int:
    """Total prompt the model had to read on a turn (fresh + cache-write + cache-read)."""
    return sum(int(usage.get(k, 0) or 0)
               for k in ("input_tokens", "cache_creation_input_tokens",
                         "cache_read_input_tokens"))


def _summary_text(rec: dict) -> str:
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


def parse_compaction_events(transcript: str) -> list[dict]:
    """Detect context-compaction events and capture their MEASURED signals.

    The summarization call `/compact` performs is a genuine LLM API call, but the
    harness writes NO `usage` object for it — only a `type=system, subtype=compact_boundary`
    marker plus a `type=user, isCompactSummary=true` record holding the summary text. So
    it is invisible to parse_turns(). We do NOT fabricate its token cost; we capture the
    two measured signals a post-hoc modeling pass can use:

        peak_context_tokens ~= the pre-boundary context the summarizer had to read
                  = peak (input + cache_write + cache_read) over usage-bearing turns
                    since the previous boundary (context grows until compaction fires).
        summary_chars       ~= length of the isCompactSummary text (chars, not tokens —
                    converting chars→tokens is a modeling choice made later).
        model               ~= model of the last usage-bearing turn before the boundary.

    Returns list of {boundary_ts, model, peak_context_tokens, summary_chars}. Empty if
    the transcript has no compaction (the common case).
    """
    recs: list[dict] = []
    with open(transcript, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    events: list[dict] = []
    peak = 0          # peak context observed since the previous boundary
    last_model = "?"
    for i, rec in enumerate(recs):
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        usage = (msg.get("usage") or rec.get("usage")) or {}
        if usage:
            peak = max(peak, _context_size(usage))
            last_model = msg.get("model", last_model)
        if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            # summary text usually lands in the next few records
            summary = ""
            for j in range(i, min(i + 8, len(recs))):
                if recs[j].get("isCompactSummary"):
                    summary = _summary_text(recs[j])
                    break
            events.append({
                "boundary_ts": rec.get("timestamp"),
                "model": last_model,
                "peak_context_tokens": peak,
                "summary_chars": len(summary),
            })
            peak = 0  # reset so a later compaction reflects only its own cycle
    return [e for e in events if e["boundary_ts"]]


# ------------------------------------------------------------------------ ledger
def module_dir() -> str:
    """Directory of this module."""
    return os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    """Where the ledger/totals live: always `<host-repo-root>/.llm_resource_tally/`.

    Anchoring on the repo root (not this module) is what lets one rule serve every
    install mode — vendored, git submodule, and `pip install` (where the module lives
    in site-packages, nowhere near the repo). It also keeps the ledger OUT of a
    submodule's working tree, so it is committed to the host repo, never upstream.
    `record`/`rollup` run from the host repo, so `repo_root()` (cwd-based) is correct."""
    return os.path.join(repo_root(), ".llm_resource_tally")


def ledger_path() -> str:
    return os.path.join(data_dir(), "resource-ledger.jsonl")


def totals_path() -> str:
    return os.path.join(data_dir(), "lifetime-totals.yaml")


def _row_identity(r: dict):
    """Stable identity of a ledger row, independent of git SHAs surviving a rewrite.
    Two rows with the same identity are the same observation and must be counted once."""
    sid = r.get("session_id")
    if r.get("kind") == COMPACTION_KIND:
        return ("compaction", sid, r.get("boundary_ts"))
    return ("measured", sid, r.get("commit"))


def read_ledger() -> list[dict]:
    """Return the ledger as a de-duplicated view (latest `recorded_at` wins per identity).

    The file on disk stays a pure append-only log — safe to `merge=union` across branches
    and to carry through a history rewrite — because readers collapse duplicates here.
    Latest-wins also gives `record --force` the intended semantics (the re-record
    supersedes the original). First-seen order is preserved for stable `show` output."""
    p = ledger_path()
    if not os.path.exists(p):
        return []
    order: list = []
    best: dict = {}
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = _row_identity(r)
            if k not in best:
                order.append(k)
            cur = best.get(k)
            if cur is None or (r.get("recorded_at") or "") >= (cur.get("recorded_at") or ""):
                best[k] = r
    return [best[k] for k in order]


def session_watermark(rows: list[dict], session_id: str) -> str:
    """Max turn timestamp already recorded for this session ('' if none)."""
    hi = ""
    for r in rows:
        if r.get("session_id") == session_id:
            rng = r.get("turn_ts_range") or [None, None]
            if rng[1] and rng[1] > hi:
                hi = rng[1]
    return hi


def ensure_data_dir() -> str:
    """Create the data dir and drop a self-contained `.gitattributes` marking the ledger
    `merge=union`, so concurrent branches / rebases concatenate rows instead of
    conflicting (readers dedup — see read_ledger). Idempotent; returns the dir."""
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    ga = os.path.join(d, ".gitattributes")
    line = "resource-ledger.jsonl merge=union\n"
    if not os.path.exists(ga):
        with open(ga, "w", encoding="utf-8") as fh:
            fh.write("# append-only ledger: keep rows from both sides on merge/rebase;\n"
                     "# readers de-duplicate by row identity (see llm_resource_tally.py).\n"
                     + line)
    return d


def append_row(row: dict) -> None:
    """Append one JSONL row under an flock so concurrent agents don't interleave."""
    import fcntl
    ensure_data_dir()
    p = ledger_path()
    with open(p, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fcntl.flock(fh, fcntl.LOCK_UN)


def aggregate(turns: list[dict]) -> dict:
    """Sum the MEASURED usage of a set of turns. No modeling knobs: wall-clock and the
    timestamp range are measured; inference-time/energy/carbon are derived post-hoc."""
    tok = {k: 0 for k in TOKEN_KEYS}
    by_model: dict[str, dict] = {}
    web_search = web_fetch = 0
    for t in turns:
        for k in TOKEN_KEYS:
            tok[k] += t["usage"][k]
        bm = by_model.setdefault(t["model"], {k: 0 for k in TOKEN_KEYS})
        for k in TOKEN_KEYS:
            bm[k] += t["usage"][k]
        web_search += t["web_search"]
        web_fetch += t["web_fetch"]
    ts_lo = turns[0]["ts"] if turns else None
    ts_hi = turns[-1]["ts"] if turns else None
    return {
        "turns": len(turns),
        "models": sorted(by_model),
        "tokens": {
            "input": tok["input_tokens"],
            "cache_write": tok["cache_creation_input_tokens"],
            "cache_read": tok["cache_read_input_tokens"],
            "output": tok["output_tokens"],
            "billable_input": (tok["input_tokens"]
                               + tok["cache_creation_input_tokens"]
                               + tok["cache_read_input_tokens"]),
        },
        "by_model": {m: {"input": v["input_tokens"],
                         "cache_write": v["cache_creation_input_tokens"],
                         "cache_read": v["cache_read_input_tokens"],
                         "output": v["output_tokens"]}
                     for m, v in by_model.items()},
        "server_tools": {"web_search": web_search, "web_fetch": web_fetch},
        "time": {"wall_clock_s": _span_seconds(ts_lo, ts_hi)},  # measured span only
        "turn_ts_range": [ts_lo, ts_hi],
    }


def to_dt(s: str | None) -> datetime | None:
    """Parse either transcript ('...Z') or git ('+00:00') ISO timestamps to aware
    datetimes. Never compare these formats as strings — 'Z' vs '+00:00' and the
    fractional-seconds '.' both break lexicographic order across the two sources."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _span_seconds(lo: str | None, hi: str | None) -> float | None:
    dlo, dhi = to_dt(lo), to_dt(hi)
    if not dlo or not dhi:
        return None
    return round((dhi - dlo).total_seconds(), 1)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def recorded_boundary_ts(rows: list[dict], session_id: str) -> set[str]:
    """Boundary timestamps of compaction estimates already in the ledger for a session
    (dedup key — re-running never double-counts a compaction event)."""
    return {r.get("boundary_ts") for r in rows
            if r.get("session_id") == session_id
            and r.get("kind") == COMPACTION_KIND}


def compaction_row(ev: dict, sha: str, commit_ts: str | None,
                   session_id: str, activity: str | None) -> dict:
    """A compaction row records only MEASURED signals — no fabricated token counts. A
    post-hoc pass turns peak_context_tokens/summary_chars into token/energy costs."""
    return {
        "schema": SCHEMA,
        "kind": COMPACTION_KIND,        # separates it from measured turn rows in rollup
        "source": "reconstructed",      # event rebuilt from transcript markers; inputs measured
        "recorded_at": now_iso(),
        "repo": os.path.basename(repo_root()),
        "commit": sha,
        "commit_ts": commit_ts,
        "agent": "claude-code",
        "activity": activity,
        "session_id": session_id,
        "boundary_ts": ev["boundary_ts"],
        "models": [ev["model"]],
        "compaction": {
            "peak_context_tokens": ev["peak_context_tokens"],  # measured: context read
            "summary_chars": ev["summary_chars"],              # measured: summary length
        },
        "note": "context-compaction is not logged with a usage object; these are the "
                "measured signals only. Token/energy cost is imputed post-hoc.",
    }


def record_compactions(transcript: str, session_id: str, sha: str,
                       commit_ts: str | None, lo_dt, hi_dt,
                       rows: list[dict], activity: str | None) -> int:
    """Append a reconstructed row for each compaction boundary in (lo_dt, hi_dt] not
    already recorded. hi_dt=None means unbounded (trailing sweep, for reconcile)."""
    seen = recorded_boundary_ts(rows, session_id)
    n = 0
    for ev in parse_compaction_events(transcript):
        bts = ev["boundary_ts"]
        if bts in seen:
            continue
        bdt = to_dt(bts)
        if lo_dt is not None and bdt <= lo_dt:
            continue
        if hi_dt is not None and bdt > hi_dt:
            continue
        append_row(compaction_row(ev, sha, commit_ts, session_id, activity))
        n += 1
        print(f"  + compaction @ {bts}: peak_context~{ev['peak_context_tokens']:,} tok, "
              f"summary={ev['summary_chars']:,} chars [{ev['model']}] "
              f"(measured signals; token cost imputed post-hoc)")
    return n


# ---------------------------------------------------------------------- commands
def cmd_record(args) -> None:
    transcript = args.transcript or find_session_transcript(
        args.projects_dir, args.session)
    session_id = os.path.splitext(os.path.basename(transcript))[0]
    sha, commit_ts = commit_meta(args.commit)
    rows = read_ledger()

    # Attribution window = (last watermark for this session, commit timestamp].
    # Bounding the top at commit_ts (not "now") keeps work done AFTER this commit
    # rolling forward to the next commit's record instead of misattributing here.
    wm = session_watermark(rows, session_id)
    wm_dt, cut_dt = to_dt(wm), to_dt(commit_ts)

    measured_dup = (not args.force and any(
        r.get("session_id") == session_id and r.get("commit") == sha
        and r.get("kind") != COMPACTION_KIND for r in rows))
    if measured_dup:
        print(f"already recorded session {session_id[:8]} @ commit {sha[:8]} "
              f"(use --force to override); measured turns skipped.")
    else:
        new = [t for t in parse_turns(transcript)
               if (wm_dt is None or to_dt(t["ts"]) > wm_dt)
               and to_dt(t["ts"]) <= cut_dt]
        if not new:
            print(f"no new turns for session {session_id[:8]} in "
                  f"({wm or 'epoch'}, {commit_ts}].")
        else:
            agg = aggregate(new)
            row = {
                "schema": SCHEMA,
                "recorded_at": now_iso(),
                "repo": os.path.basename(repo_root()),
                "commit": sha,
                "commit_ts": commit_ts,
                "agent": "claude-code",
                "activity": args.label,   # what this work was (planning/impl/review/…), or None
                "session_id": session_id,
                **agg,
            }
            append_row(row)
            tk = agg["tokens"]
            print(f"recorded {agg['turns']} turns for {sha[:8]} "
                  f"[{','.join(agg['models'])}]"
                  f"{(' <' + args.label + '>') if args.label else ''}: "
                  f"out={tk['output']} in={tk['input']} "
                  f"cache_w={tk['cache_write']} cache_r={tk['cache_read']}; "
                  f"wall={agg['time']['wall_clock_s']}s; "
                  f"inference-time/energy/carbon modeled post-hoc.")
            print(trailer_line(row))

    # Compaction overhead in this commit's window (independent of the measured path — a
    # compaction with no accompanying measured turns still counts).
    if not args.no_estimate_compaction:
        record_compactions(transcript, session_id, sha, commit_ts,
                           wm_dt, cut_dt, rows, args.label)


def cmd_reconcile(args) -> None:
    """Attribute any un-recorded trailing turns (per session) to a pending bucket,
    so a session that did work without committing is never dropped."""
    rows = read_ledger()
    projects = args.projects_dir
    proj = os.path.join(projects, munged_project_dir(superproject_root()))
    files = sorted(glob.glob(os.path.join(proj, "*.jsonl")))
    total = 0
    pending = f"pending@{now_iso()[:10]}"
    for f in files:
        sid = os.path.splitext(os.path.basename(f))[0]
        wm = session_watermark(rows, sid)
        wm_dt = to_dt(wm)
        new = [t for t in parse_turns(f)
               if wm_dt is None or to_dt(t["ts"]) > wm_dt]
        if new:
            agg = aggregate(new)
            row = {
                "schema": SCHEMA, "recorded_at": now_iso(),
                "repo": os.path.basename(repo_root()),
                "commit": pending, "commit_ts": None,
                "agent": "claude-code", "activity": args.label,
                "session_id": sid, **agg,
                "note": "reconcile: un-committed turns swept so they are not undercounted",
            }
            append_row(row)
            total += agg["turns"]
            print(f"reconciled {agg['turns']} un-recorded turns for session {sid[:8]}"
                  f"{(' <' + args.label + '>') if args.label else ''}.")
        if not args.no_estimate_compaction:
            total += record_compactions(f, sid, pending, None, wm_dt, None,
                                        rows, args.label)
    if total == 0:
        print("nothing to reconcile; all session turns already accounted.")


def trailer_line(row: dict) -> str:
    """A git commit-trailer suggestion carrying the MEASURED usage (no modeled fields)."""
    tk = row["tokens"]
    return ("Resource-Usage: model={m}; tok in={i} cw={cw} cr={cr} out={o}; "
            "wall={w}s; inference-time/energy/carbon=modeled-post-hoc").format(
        m="+".join(row["models"]), i=tk["input"], cw=tk["cache_write"],
        cr=tk["cache_read"], o=tk["output"], w=row["time"]["wall_clock_s"])


def cmd_show(args) -> None:
    rows = read_ledger()
    if not rows:
        print("ledger is empty.")
        return
    for r in rows:
        commit = (r.get("commit") or "")[:10]
        when = r.get("recorded_at", "")[:19]
        models = ",".join(r.get("models", []))
        if r.get("kind") == COMPACTION_KIND:
            c = r.get("compaction", {})
            print(f"{commit:12} {when} ~compaction {models:20} "
                  f"peak_ctx={c.get('peak_context_tokens', 0):>9} "
                  f"summary={c.get('summary_chars', 0):>6}c")
            continue
        tk = r.get("tokens", {})
        wall = r.get("time", {}).get("wall_clock_s")
        act = r.get("activity")
        print(f"{commit:12} {when}   measured   {models:20} "
              f"out={tk.get('output', 0):>7} billable_in={tk.get('billable_input', 0):>9} "
              f"wall={wall if wall is not None else '  -'}s"
              f"{('  <' + act + '>') if act else ''}")


def cmd_rollup(args) -> None:
    """Sum the ledger's MEASUREMENTS. This is itself a post-hoc pass (regenerable from
    the ledger), so it reports only measured quantities; inference-time, energy, and
    carbon are left for a dedicated modeling pass that reads these same measurements."""
    rows = read_ledger()
    tot = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
           "billable_input": 0}
    by_model: dict[str, int] = {}
    by_activity: dict[str, int] = {}
    turns = 0
    wall = 0.0
    web_search = web_fetch = 0
    commits = set()
    # Context-compaction has no billed usage; we kept only its measured signals. Tallied
    # separately (raw peak-context/summary-chars) so a later pass can model its cost.
    compaction = {"events": 0, "peak_context_tokens": 0, "summary_chars": 0}
    for r in rows:
        if r.get("kind") == COMPACTION_KIND:
            c = r.get("compaction", {})
            compaction["events"] += 1
            compaction["peak_context_tokens"] += c.get("peak_context_tokens", 0)
            compaction["summary_chars"] += c.get("summary_chars", 0)
            continue
        tk = r.get("tokens", {})
        for k in tot:
            tot[k] += tk.get(k, 0)
        turns += r.get("turns", 0)
        for m in r.get("models", []):
            by_model[m] = by_model.get(m, 0) + r.get("by_model", {}).get(m, {}).get("output", 0)
        act = r.get("activity") or "unlabeled"
        by_activity[act] = by_activity.get(act, 0) + tk.get("output", 0)
        wall += r.get("time", {}).get("wall_clock_s") or 0
        web_search += r.get("server_tools", {}).get("web_search", 0)
        web_fetch += r.get("server_tools", {}).get("web_fetch", 0)
        c = r.get("commit") or ""
        if c and not c.startswith("pending@"):
            commits.add(c)
    totals = {
        "generated_at": now_iso(),
        "schema": SCHEMA,
        "ledger_rows": len(rows),
        "commits_accounted": len(commits),
        "turns": turns,
        "tokens": tot,
        "output_tokens_by_model": by_model,
        "output_tokens_by_activity": by_activity,
        "server_tool_calls": {"web_search": web_search, "web_fetch": web_fetch},
        "time": {"wall_clock_s": round(wall, 1)},   # measured span only
        "compaction_signals": {          # measured; token/energy cost imputed post-hoc
            "events": compaction["events"],
            "peak_context_tokens": compaction["peak_context_tokens"],
            "summary_chars": compaction["summary_chars"],
        },
        "modeled_post_hoc": "inference_seconds, energy_kwh, carbon_gco2e — derived from "
                            "the measurements above by a separate pass; not stored here.",
    }
    _write_totals_file(totals)
    print(json.dumps(totals, indent=2, ensure_ascii=False))
    print(f"# wrote {os.path.relpath(totals_path(), repo_root())}")


def _write_totals_file(totals: dict) -> None:
    """Canonical rollup output at `<data-dir>/lifetime-totals.yaml` — this is what a
    copy-pasted module always produces, independent of the host repo's layout."""
    ensure_data_dir()
    path = totals_path()
    lines = ["# Auto-generated by llm_resource_tally.py rollup — do not edit by hand.",
             "lifetime_totals:"]
    lines += _yaml_indent(totals, 2)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _yaml_indent(obj, indent: int) -> list[str]:
    pad = " " * indent
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict,)) and v:
                out.append(f"{pad}{k}:")
                out += _yaml_indent(v, indent + 2)
            else:
                out.append(f"{pad}{k}: {_scalar(v)}")
    return out


def _scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, dict) and not v:
        return "{}"
    return str(v)


# ------------------------------------------------------------------------ install
# Wire this repo up so accounting happens automatically: a git post-commit hook and
# a version-stamped managed block in AGENTS.md. All of this is OFFLINE and idempotent
# — re-running detects a prior install and upgrades it in place. The network is only
# needed by the `curl | sh` bootstrap (install.sh) and `update`.

# Managed AGENTS.md block. Markers are stable across versions; the version rides on
# the BEGIN line so a re-install can find an OLDER block and regenerate it.
AGENTS_BEGIN = ("<!-- BEGIN llm_resource_tally v{version} (managed block — regenerated by "
                "llm_resource_tally.py install; edits below will be overwritten) -->")
AGENTS_END = "<!-- END llm_resource_tally -->"
AGENTS_BEGIN_RE = re.compile(
    r"<!--\s*BEGIN llm_resource_tally(?:\s+v([0-9][0-9A-Za-z.\-+]*))?.*?-->", re.S)

AGENTS_SNIPPET = """\
## Resource accounting — the LLM compute this work costs (CRITICAL: do this every commit)

Every commit in this repo is produced by an LLM agent; we keep a **measured**
per-commit record of the tokens/model it cost (inference-time, energy & carbon are
derived from those measurements later). It is near-zero effort:

- **After cloning**, wire the hook once (offline, idempotent): `{run} install`
- Thereafter every `git commit` auto-records. To record by hand: `{run} record`
- **At the end of a work session** (captures planning/chat that produced no commit):
  `{run} reconcile && {run} rollup`
- Codex / non-Claude agents: `{run} record --transcript <path/to/session.jsonl>`

**Tag what the work was** so non-code work is still counted: pass `--label`
(e.g. `record --label implementation`, or `reconcile --label planning` to capture a
planning/design/review session that produced no commit). All LLM turns of a session are
swept from its watermark, so planning is never lost — labeling just makes it legible.

Tokens/model are MEASURED from your session transcript (deduped by message id — do
NOT hand-count). The ledger `.llm_resource_tally/resource-ledger.jsonl` (at this repo's
root) is append-only, per-session, concurrency-safe, and stores measurements only."""

# Sentinel-wrapped block appended to an existing post-commit hook (never clobbers it).
HOOK_BEGIN = "# >>> llm_resource_tally (managed — regenerated by llm_resource_tally.py install) >>>"
HOOK_END = "# <<< llm_resource_tally (managed) <<<"

# The shared post-commit script written into a vendored `<dir>/hooks/` (used when the tool
# is installed via core.hooksPath). Self-locating so it works no matter what the dir is
# named; best-effort so accounting never blocks a commit. Kept in sync with hooks/post-commit.
SHARED_HOOK = """\
#!/usr/bin/env bash
# llm_resource_tally post-commit — auto-record LLM resource usage for the commit just made.
# Self-locating (works no matter what the vendor dir is named); best-effort (never blocks a commit).
here="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || exit 0
python3 "$here/../llm_resource_tally.py" record --commit HEAD >/dev/null 2>&1 || true
"""

DEFAULT_VENDOR_DIR = "dev/llm_resource_tally"   # where the pip/curl bootstrap vendors the code


def _chmod_x(path: str) -> None:
    try:
        os.chmod(path, os.stat(path).st_mode | 0o111)
    except OSError:
        pass


def _git_config(root: str, *args: str) -> str:
    """`git config <args>` returning '' instead of raising when the key is unset."""
    try:
        return git("config", *args, cwd=root)
    except subprocess.CalledProcessError:
        return ""


def _module_in_repo(root: str) -> bool:
    """True if this module file lives inside the host repo working tree (vendored or
    submodule → callable by path). False when `pip install`ed (module in site-packages)."""
    md, r = os.path.realpath(module_dir()), os.path.realpath(root)
    return md == r or md.startswith(r + os.sep)


def _rel_dir(root: str) -> str | None:
    """Module dir relative to repo root if vendored/submodule, else None (pip install)."""
    return os.path.relpath(module_dir(), root) if _module_in_repo(root) else None


def _run_cmd(rel: str | None) -> str:
    """The command a human/hook uses to invoke the tool: an explicit path when vendored,
    the installed console script when pip'd (only used transiently to bootstrap)."""
    return f"python3 {rel}/llm_resource_tally.py" if rel else "llm_resource_tally"


def _is_pip_install() -> bool:
    """True when this module is running from an installed package (site/dist-packages),
    i.e. `pip install`ed — the bootstrap that vendors a copy into the target repo."""
    md = module_dir()
    return "site-packages" in md or "dist-packages" in md or not _module_in_repo(repo_root())


def _vendor_into(root: str, rel: str) -> str:
    """Copy the MINIMAL self-contained subset of the tool into `<root>/<rel>/` from the
    running (pip-installed) package — the module, a stamped VERSION, and the shared hook.
    After this the repo is self-contained and offline, exactly like the curl route; the
    pip package is only the delivery mechanism (PyPI in place of a curl'd tarball)."""
    dest = os.path.join(root, rel)
    os.makedirs(os.path.join(dest, "hooks"), exist_ok=True)
    shutil.copyfile(os.path.abspath(__file__), os.path.join(dest, "llm_resource_tally.py"))
    with open(os.path.join(dest, "VERSION"), "w", encoding="utf-8") as fh:
        fh.write(tool_version() + "\n")
    hook = os.path.join(dest, "hooks", "post-commit")
    with open(hook, "w", encoding="utf-8") as fh:
        fh.write(SHARED_HOOK)
    _chmod_x(os.path.join(dest, "llm_resource_tally.py"))
    _chmod_x(hook)
    return f"vendored the tool into {rel}/ (from the installed package)"


def _replace_region(text: str, begin: str, end: str, repl: str) -> str:
    if begin in text and end in text:
        s = text.index(begin)
        e = text.index(end, s) + len(end)
        return text[:s] + repl + text[e:]
    return text


def _strip_region(text: str, begin_idx: int, end_str: str) -> str:
    """Remove text[begin_idx : end-of end_str], plus one bracketing blank line each side."""
    e = text.index(end_str, begin_idx) + len(end_str)
    before, after = text[:begin_idx], text[e:]
    if before.endswith("\n"):
        before = before[:-1]
    if after.startswith("\n"):
        after = after[1:]
    return before + after


# ------------------------------------------------------------------- hook wiring
def _hooks_dir_default(root: str) -> str:
    """The repo's active hooks dir when core.hooksPath is unset (usually .git/hooks;
    handles worktrees/submodules via `git rev-parse --git-path`)."""
    hd = git("rev-parse", "--git-path", "hooks", cwd=root)
    return hd if os.path.isabs(hd) else os.path.join(root, hd)


def _has_active_git_hooks(root: str) -> bool:
    """True if .git/hooks already holds a real (non-.sample, executable) hook we would
    silently disable by hijacking core.hooksPath. Callers reach this only after ruling
    out a prior llm_resource_tally append, so any such hook here is foreign."""
    hd = _hooks_dir_default(root)
    if not os.path.isdir(hd):
        return False
    for name in os.listdir(hd):
        if name.endswith(".sample"):
            continue
        p = os.path.join(hd, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return True
    return False


def _effective_hooks_dir(root: str, existing_hp: str) -> str:
    """The dir where hooks actually run from: core.hooksPath if set, else .git/hooks."""
    if existing_hp:
        return existing_hp if os.path.isabs(existing_hp) else os.path.join(root, existing_hp)
    return _hooks_dir_default(root)


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _hook_block(rel: str) -> str:
    """Sentinel-wrapped post-commit body calling the vendored module by path. Best-effort:
    accounting must NEVER block or fail a commit, hence `|| true`. Resolves the repo root at
    run time so it is correct from any cwd and inside submodules."""
    return (f"{HOOK_BEGIN}\n"
            f'root="$(git rev-parse --show-toplevel 2>/dev/null)" || root=""\n'
            f'[ -n "$root" ] && python3 "$root/{rel}/llm_resource_tally.py" record '
            f"--commit HEAD >/dev/null 2>&1 || true\n"
            f"{HOOK_END}")


def _wire_hook(root: str, rel: str, mode: str) -> str:
    """Install the post-commit hook. Modes:
        auto      : share via core.hooksPath if that is safe (unset + no foreign hooks),
                    else append to the active post-commit (never clobbering it).
        hookspath : force core.hooksPath -> <rel>/hooks (committed, shared with clones).
        append    : force append to the active post-commit; leave core.hooksPath alone.
        none      : do nothing.
    """
    if mode == "none":
        return "skipped (--hook-mode none)"
    existing_hp = _git_config(root, "--get", "core.hooksPath")
    shared_dir = f"{rel}/hooks"

    # (1) Already wired via our own committed hooks dir → nothing to do (don't append a
    # redundant block to the shipped hook, which an update would overwrite anyway).
    if existing_hp and os.path.normpath(existing_hp) == os.path.normpath(shared_dir):
        _chmod_x(os.path.join(root, shared_dir, "post-commit"))
        return f"already shared via core.hooksPath -> {shared_dir}"

    # (2) Already appended into the active post-commit on a prior install → regenerate
    # the block in place there. This MUST precede the share decision so a re-install
    # never switches to core.hooksPath and thereby disables the very hook we live in.
    eff_hook = os.path.join(_effective_hooks_dir(root, existing_hp), "post-commit")
    if os.path.exists(eff_hook) and HOOK_BEGIN in _read_text(eff_hook):
        return _append_hook(root, rel, existing_hp)

    # (3) Fresh wiring. Share via core.hooksPath only when explicitly asked, or when it
    # is safe automatically (no existing hooksPath and no foreign hook to shadow).
    want_shared = mode == "hookspath" or (
        mode == "auto" and not existing_hp and not _has_active_git_hooks(root))
    if want_shared:
        git("config", "core.hooksPath", shared_dir, cwd=root)
        _chmod_x(os.path.join(root, shared_dir, "post-commit"))
        return f"core.hooksPath -> {shared_dir} (committed; shared with everyone who clones)"
    return _append_hook(root, rel, existing_hp)


def _append_hook(root: str, rel: str, existing_hp: str) -> str:
    hd = _effective_hooks_dir(root, existing_hp)
    os.makedirs(hd, exist_ok=True)
    hook = os.path.join(hd, "post-commit")
    where = os.path.relpath(hook, root)
    block = _hook_block(rel)
    if os.path.exists(hook):
        text = _read_text(hook)
        if HOOK_BEGIN in text:
            new = _replace_region(text, HOOK_BEGIN, HOOK_END, block)
            if new == text:
                return f"unchanged ({where})"
            with open(hook, "w", encoding="utf-8") as fh:
                fh.write(new)
            _chmod_x(hook)
            return f"regenerated managed block in {where}"
        if not text.endswith("\n"):
            text += "\n"
        with open(hook, "w", encoding="utf-8") as fh:
            fh.write(text + "\n" + block + "\n")
        _chmod_x(hook)
        return f"appended managed block to existing {where}"
    with open(hook, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\n" + block + "\n")
    _chmod_x(hook)
    return f"created {where}"


# ---------------------------------------------------------------- AGENTS.md wiring
def _managed_agents_block(run: str, version: str) -> str:
    return (AGENTS_BEGIN.format(version=version) + "\n"
            + AGENTS_SNIPPET.format(run=run) + "\n" + AGENTS_END)


def _install_agents_block(root: str, run: str, version: str, agents_name: str) -> str:
    path = os.path.join(root, agents_name)
    block = _managed_agents_block(run, version)
    if os.path.exists(path):
        text = _read_text(path)
        m = AGENTS_BEGIN_RE.search(text)
        if m and AGENTS_END in text[m.end():]:
            old = m.group(1) or "?"
            e = text.index(AGENTS_END, m.end()) + len(AGENTS_END)
            new_text = text[:m.start()] + block + text[e:]
            if new_text == text:
                return f"already current (v{version})"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
            return (f"regenerated managed block (v{version})" if old == version
                    else f"upgraded managed block v{old} -> v{version}")
        sep = "\n" if text.endswith("\n") else "\n\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + sep + block + "\n")
        return f"appended managed block (v{version})"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"# {agents_name}\n\n" + block + "\n")
    return f"created {agents_name} with managed block (v{version})"


# ---------------------------------------------------------------------- commands
def cmd_install(args) -> None:
    root = repo_root()
    # Running from a pip install? Then vendor the minimal tool into the repo first and wire
    # path-mode against that copy — so the repo ends up self-contained (offline, the vendored
    # copy is the source of truth), identical to the curl route but sourced from PyPI.
    from_pip = _is_pip_install()
    if from_pip:
        rel = args.dir or DEFAULT_VENDOR_DIR
        vendor_msg = _vendor_into(root, rel)
    else:
        rel = args.dir or _rel_dir(root)  # already vendored / submodule
        vendor_msg = None
    run = _run_cmd(rel)
    version = tool_version()
    hook_msg = _wire_hook(root, rel, args.hook_mode)
    agents_msg = _install_agents_block(root, run, version, args.agents_file)
    if rel:                              # make the vendored files executable
        for rp in (f"{rel}/llm_resource_tally.py", f"{rel}/hooks/post-commit", f"{rel}/install.sh"):
            ap = os.path.join(root, rp)
            if os.path.exists(ap):
                _chmod_x(ap)
    claude_msg = _wire_claude_hook(root, rel) if args.claude else None
    print(f"llm_resource_tally v{version} installed in {os.path.basename(root)} [{rel}]")
    if vendor_msg:
        print(f"  vendored   : {vendor_msg}")
    print(f"  hook       : {hook_msg}")
    print(f"  {args.agents_file:<11}: {agents_msg}")
    if claude_msg:
        print(f"  claude hook: {claude_msg}")
    print("  ledger     : .llm_resource_tally/ at repo root (committed; never touched by install)")
    print(f"commit the changes to share them; run `{run} reconcile && {run} rollup` at session end.")


def cmd_uninstall(args) -> None:
    root = repo_root()
    rel = args.dir or _rel_dir(root)
    msgs = []
    # 1. core.hooksPath, only if WE set it to our own dir (in-repo installs only).
    hp = _git_config(root, "--get", "core.hooksPath")
    if rel and hp and os.path.normpath(hp) == os.path.normpath(f"{rel}/hooks"):
        git("config", "--unset", "core.hooksPath", cwd=root)
        msgs.append(f"unset core.hooksPath ({hp})")
    # 2. sentinel block from the active post-commit (whichever it is now).
    hd = (hp if os.path.isabs(hp) else os.path.join(root, hp)) if hp else _hooks_dir_default(root)
    hook = os.path.join(hd, "post-commit")
    if os.path.exists(hook):
        text = _read_text(hook)
        if HOOK_BEGIN in text:
            s = text.index(HOOK_BEGIN)
            stripped = _strip_region(text, s, HOOK_END)
            # if nothing but a shebang remains, drop the file entirely
            if stripped.strip() in ("", "#!/usr/bin/env bash", "#!/bin/sh"):
                os.remove(hook)
                msgs.append(f"removed {os.path.relpath(hook, root)}")
            else:
                with open(hook, "w", encoding="utf-8") as fh:
                    fh.write(stripped)
                msgs.append(f"stripped managed block from {os.path.relpath(hook, root)}")
    # 3. AGENTS.md managed block.
    path = os.path.join(root, args.agents_file)
    if os.path.exists(path):
        text = _read_text(path)
        m = AGENTS_BEGIN_RE.search(text)
        if m and AGENTS_END in text[m.end():]:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_strip_region(text, m.start(), AGENTS_END))
            msgs.append(f"stripped managed block from {args.agents_file}")
    # 4. Claude PostToolUse hook (always attempt removal; no-op if absent).
    claude_removed = _unwire_claude_hook(root)
    if claude_removed:
        msgs.append(claude_removed)
    print("llm_resource_tally uninstalled:" if msgs else "nothing to uninstall.")
    for m in msgs:
        print(f"  - {m}")
    if msgs:
        print("  the .llm_resource_tally/ ledger and the module files were left in place.")


def cmd_update(args) -> None:
    """Re-vendor the latest version from the canonical repo, then re-run install. Needs
    network. The pinned vendored copy keeps working if this fails or the host is gone."""
    root = repo_root()
    rel = _rel_dir(root)
    if rel is None:
        sys.exit("this is a pip install — upgrade with `pip install -U llm_resource_tally` "
                 "(update re-vendors a copied-in folder; there is none here).")
    repo = args.repo or CANONICAL_REPO
    if "OWNER/" in repo:
        sys.exit("error: no canonical source configured. Set CANONICAL_REPO in "
                 "llm_resource_tally.py (or pass --repo OWNER/NAME). The vendored copy "
                 "still works fully offline via `install`.")
    ref = args.ref
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/install.sh"
    fetch = ("curl -fsSL" if shutil.which("curl")
             else "wget -qO-" if shutil.which("wget") else None)
    if not fetch:
        sys.exit("error: need curl or wget to update.")
    print(f"updating {rel} from {repo}@{ref} ...")
    env = {**os.environ, "RT_REPO": repo, "RT_REF": ref, "RT_DIR": rel}
    subprocess.run(f'{fetch} "{url}" | sh', shell=True, cwd=root, env=env, check=True)


# ------------------------------------------------------ Claude Code cross-repo hook
# A git post-commit hook can't identify the Claude session (the CLI exposes no session
# id to subprocesses) — so when a session in repo A commits into repo B, it can't
# attribute correctly. Claude Code's *native* PostToolUse hook CAN: it delivers
# session_id + transcript_path + cwd (= the repo the commit landed in) on stdin. This
# `hook` subcommand consumes that payload and records the exact session against the
# exact repo, solving cross-repo attribution. Claude-specific by design.
_GIT_COMMIT_RE = re.compile(r"\bgit\b(?:\s+-C\s+\S+)?(?:\s+-c\s+\S+)*\s+commit\b")


def _is_git_commit(command: str) -> bool:
    """Heuristic: does this shell command create a commit? (Excludes dry-run/help.)"""
    if not command or not _GIT_COMMIT_RE.search(command):
        return False
    return not re.search(r"--dry-run|--help|(?:^|\s)-h(?:\s|$)", command)


def _commit_repo_dir(command: str, cwd: str) -> str:
    """The repo the commit lands in: `git -C <path>` if present, else the tool-call cwd."""
    m = re.search(r"\bgit\s+-C\s+(\"[^\"]+\"|'[^']+'|\S+)", command)
    d = m.group(1).strip("\"'") if m else cwd
    d = os.path.expanduser(d)
    return d if os.path.isabs(d) else os.path.normpath(os.path.join(cwd, d))


def cmd_hook(args) -> None:
    """Runtime handler for a Claude Code PostToolUse(Bash) hook. Reads the hook JSON on
    stdin; if the tool call was a `git commit`, records that session's turns against the
    commit in whatever repo it landed in. MUST NEVER raise or print — a hook must not
    disrupt the session — so everything is wrapped and stdout is silenced."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    try:
        command = (payload.get("tool_input") or {}).get("command") or ""
        if not _is_git_commit(command):
            return
        cwd = payload.get("cwd") or os.getcwd()
        repo_dir = _commit_repo_dir(command, cwd)
        transcript = payload.get("transcript_path")
        if not transcript or not os.path.exists(transcript):
            return
        import types
        ns = types.SimpleNamespace(
            commit="HEAD", transcript=transcript, session=None,
            projects_dir=args.projects_dir, label=None, force=False,
            no_estimate_compaction=False)
        old_cwd, old_out = os.getcwd(), sys.stdout
        try:
            os.chdir(repo_dir)
            repo_root()                       # raises if repo_dir isn't a git repo -> skip
            sys.stdout = open(os.devnull, "w")
            cmd_record(ns)
        finally:
            try:
                sys.stdout.close()
            except Exception:
                pass
            sys.stdout = old_out
            os.chdir(old_cwd)
    except Exception:
        return


def _claude_hook_cmd(rel: str | None) -> str:
    """Command string placed in .claude/settings.json for the PostToolUse hook."""
    if rel:
        return f'python3 "$CLAUDE_PROJECT_DIR/{rel}/llm_resource_tally.py" hook'
    return "llm_resource_tally hook"


def _claude_settings_path(root: str) -> str:
    return os.path.join(root, ".claude", "settings.json")


def _entry_is_ours(entry: dict) -> bool:
    for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
        c = h.get("command", "") if isinstance(h, dict) else ""
        if "tally" in c and c.rstrip().endswith("hook"):
            return True
    return False


def _wire_claude_hook(root: str, rel: str | None) -> str:
    """Merge our PostToolUse(Bash) hook into .claude/settings.json (idempotent — replaces
    any prior entry of ours; leaves everything else untouched)."""
    path = _claude_settings_path(root)
    data: dict = {}
    if os.path.exists(path):
        try:
            data = json.loads(_read_text(path)) or {}
        except json.JSONDecodeError:
            return "skipped (.claude/settings.json is not valid JSON — wire it by hand)"
    hooks = data.setdefault("hooks", {})
    ptu = hooks.setdefault("PostToolUse", [])
    if not isinstance(ptu, list):
        return "skipped (unexpected hooks.PostToolUse shape)"
    ptu[:] = [e for e in ptu if not _entry_is_ours(e)]
    cmd = _claude_hook_cmd(rel)
    ptu.append({"matcher": "Bash",
                "hooks": [{"type": "command", "command": cmd}]})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    return f"PostToolUse(Bash) -> {cmd}"


def _unwire_claude_hook(root: str) -> str | None:
    path = _claude_settings_path(root)
    if not os.path.exists(path):
        return None
    try:
        data = json.loads(_read_text(path)) or {}
    except json.JSONDecodeError:
        return None
    ptu = (data.get("hooks") or {}).get("PostToolUse")
    if not isinstance(ptu, list):
        return None
    kept = [e for e in ptu if not _entry_is_ours(e)]
    if len(kept) == len(ptu):
        return None
    data["hooks"]["PostToolUse"] = kept
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    return "removed PostToolUse(Bash) hook from .claude/settings.json"


# ---------------------------------------------------------------------------- cli
def main() -> None:
    p = argparse.ArgumentParser(description="Measured LLM-usage accounting.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--transcript", help="explicit session transcript path")
        sp.add_argument("--session", help="session id (transcript filename stem)")
        sp.add_argument("--projects-dir", default=default_projects_dir())
        sp.add_argument("--label", default=None,
                        help="tag this work by what it is (e.g. planning, implementation, "
                             "review, debugging); stored as the row's `activity`")
        sp.add_argument("--no-estimate-compaction", action="store_true",
                        help="do not add reconstructed rows for context-compaction events")

    r = sub.add_parser("record", help="attribute new turns to a commit")
    r.add_argument("--commit", default="HEAD")
    r.add_argument("--force", action="store_true")
    common(r)
    r.set_defaults(func=cmd_record)

    rc = sub.add_parser("reconcile", help="sweep un-recorded trailing turns")
    common(rc)
    rc.set_defaults(func=cmd_reconcile)

    ru = sub.add_parser("rollup", help="refresh lifetime totals from the ledger")
    ru.set_defaults(func=cmd_rollup)

    sh = sub.add_parser("show", help="print the ledger")
    sh.set_defaults(func=cmd_show)

    ins = sub.add_parser("install",
                         help="wire git hook + AGENTS.md (offline, idempotent)")
    ins.add_argument("--dir", default=None,
                     help="module dir relative to repo root (default: auto-detect)")
    ins.add_argument("--hook-mode", choices=["auto", "hookspath", "append", "none"],
                     default="auto", help="how to install the post-commit hook "
                     "(auto: share via core.hooksPath when safe, else append)")
    ins.add_argument("--agents-file", default="AGENTS.md",
                     help="doc to carry the managed block (default AGENTS.md)")
    ins.add_argument("--claude", action="store_true",
                     help="also wire a Claude Code PostToolUse(Bash) hook into "
                          ".claude/settings.json for correct cross-repo attribution")
    ins.set_defaults(func=cmd_install)

    hk = sub.add_parser("hook", help="internal: Claude PostToolUse handler (reads JSON on stdin)")
    hk.add_argument("--projects-dir", default=default_projects_dir())
    hk.set_defaults(func=cmd_hook)

    un = sub.add_parser("uninstall",
                        help="remove hook wiring + AGENTS.md block (keeps data/)")
    un.add_argument("--dir", default=None)
    un.add_argument("--agents-file", default="AGENTS.md")
    un.set_defaults(func=cmd_uninstall)

    up = sub.add_parser("update", help="re-vendor the latest version (needs network)")
    up.add_argument("--repo", default=CANONICAL_REPO, help="GitHub owner/name source")
    up.add_argument("--ref", default="main", help="tag/branch/sha to install (default main)")
    up.set_defaults(func=cmd_update)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
