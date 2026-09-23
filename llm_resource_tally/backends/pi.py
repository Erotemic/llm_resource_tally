# SPDX-License-Identifier: Apache-2.0
"""Pi coding agent backend: read Pi's session JSONL files.

Pi (https://github.com/earendil-works/pi) writes one JSONL file per session, in one of two
layouts (verified against its source: ``SessionManager`` always writes new files straight into
its ``sessionDir``; the encoded-cwd dir is part of the *default* path computation, never
appended to an explicitly supplied dir):

- *default storage* (no explicit dir): ``<agent-dir>/sessions/--<munged-cwd>--/<ts>_<uuid>.jsonl``
  (``<agent-dir>`` = ``$PI_CODING_AGENT_DIR`` or ``~/.pi/agent``); the munged dir is the
  session's full cwd with a single leading ``/`` or ``\\`` stripped and every remaining
  ``/``, ``\\``, ``:`` turned into ``-`` (dots, underscores, spaces preserved). A session
  started in a repo subdirectory therefore lives in ``--<repo>-<sub>--/``.
- *explicit session dir* (``--session-dir``, ``PI_CODING_AGENT_SESSION_DIR``, or a
  ``sessionDir`` setting; tally's own ``PI_SESSIONS_DIR`` is a synonym): the named directory
  holds the ``<ts>_<uuid>.jsonl`` files **directly**, one level deep — never with an
  encoded-cwd child beneath it.

Either way, line one is a `type: "session"` header carrying the session `id`, the `cwd` the
session ran in, and — when the file is a fork/clone/branch of another session — the
`parentSession` path. Every other line is a tree entry (`message`, `compaction`,
`branch_summary`, `model_change`, ...) with a short unique `id`. v1 files are linear (no
entry ids); v2 added ids; v3 (current) renamed the `hookMessage` role. All three parse here.

Accounting decisions (verified against the Pi source and a corpus of real session files):

- *Billing model vs session-state model are distinct.* An assistant call is billed under the
  concrete model that answered, `<provider>/<responseModel ?? model>`: newer Pi records the
  answering model in `responseModel` when it differs from the requested `model` (e.g. a
  fallback), mirroring Pi's own usage keying, so one repo can mix cloud and local endpoints in
  a single ledger and `report --by model` splits them. The model *state* the assistant
  establishes for its descendants follows Pi's own `getSessionContextSettings`, which records
  the message's requested `provider`/`model` — not `responseModel` — so a compaction, branch
  summary, or nested tool usage below it inherits the logical model while the call itself
  still bills the concrete one.
- *Effective model state is resolved by ancestry, not append order.* A session file is a
  tree: every entry (v2+) carries a `parentId` (the tree root's is null; v1 files are a
  linear chain by line order). An entry that records no model of its own (a tool result,
  a compaction, a branch summary, an assistant message without provider/model) inherits the
  last model source on the path from the tree root to its parent — exactly Pi's own
  `getSessionContextSettings` walk, which applies each ancestor's `model_change` /
  assistant requested model in root-to-entry order. After in-file branching that differs from
  a naive last-seen-in-the-file model, which is what a linear scan produces.
- *Top-level `type: "usage"` entries are measured.* Pi v3 records model-attributed usage
  that is not an assistant message as its own `UsageEntry` (`kind`, `provider`, `model`,
  `usage`) — Pi documents cache warming as one example and includes these entries in its
  session usage totals. Each is billed as an ordinary measured turn under its OWN
  `provider`/`model` (a usage entry establishes no model state for descendants), with its
  `kind` preserved in the turn type (`usage:<kind>`); unknown kinds are counted, never
  rejected.
- *Exact attribution via ``$PI_SESSION_FILE``*: commands run by Pi's shell tool carry the
  session's own file path in the environment, so a commit made from a Pi session is attributed
  to exactly that session even when the session's recorded cwd differs from the repo that
  received the commit (the same trust model as the Claude ``--claude`` hook).
- *Fork/clone exact-once allocation.* `pi --fork`, `--session <file>`, and in-place branch
  switching copy the source file's entries VERBATIM — same ids, timestamps, and usage — into a
  new file with a fresh header (a new session id, and `parentSession` pointing at the source).
  The copied observations therefore appear in several files at once, so per-file billing
  cannot decide what is "new". Each measured observation instead carries a stable `claim_id`
  — a digest of its stable non-content identity (entry id, parentId, timestamp, kind,
  intrinsically recorded provider/model, normalized usage; for usage-less compaction estimates
  also the summary's digest) — and record/reconcile allocate it through the per-user
  observation claim log (`event-claims.jsonl`): the first accounting pass that encounters an
  UNCLAIMED copy bills it (whether from the parent or from a fork, so a later-deleted parent
  file loses nothing that is still observable), and every later copy of the same observation is
  suppressed. The fork header's `parentSession` remains as a lineage signal, but the fork-
  header timestamp no longer gates billing at all: a copied observation is billable from any
  copy until it is claimed. Because Pi's normal entry ids are only 8 hex characters and are
  collision-checked against the session's own entry map alone, unrelated sessions can legally
  reuse the same id — the fingerprint (never the bare id) is the identity, so two unrelated
  sessions that share an 8-character id still bill independently.
- *Compaction is measured, not estimated*: a `compaction` entry carries the real usage of the
  summarization LLM call, so it is billed as an ordinary measured turn. Only a compaction
  entry with NO usage falls back to the Claude-style reconstructed estimate row. (Pi's
  compaction runtime also assembles a `retainedTail` in memory by copying earlier messages,
  but those copies are never persisted to the session file — each usage appears in the file
  exactly once.)
- *Zero usage*: pi-ai pre-allocates a zero-filled usage struct and keeps it when an endpoint
  reports nothing. A zero-usage call that FAILED (`stopReason: "error"`) consumed nothing and
  is excluded entirely; a zero-usage call that SUCCEEDED means the endpoint is not reporting
  token counts — it is counted as a zero-token turn (so the turn count stays honest) and
  `doctor` warns about the endpoint.
- *Reasoning tokens are a subset of `output`* (both pi-ai's OpenAI and Anthropic normalizers
  place thinking/reasoning inside the output count), so they are NOT added on top.

Not on by default — opt in with `install --backend pi` — so the passive hook does not scan
Pi's session tree for users who do not run it. Ephemeral sessions (`--no-session`, SDK
`inMemory`) write no file and are therefore invisible to this backend.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys

from .base import Backend
from .._util import to_dt
from ..gitutil import superproject_root
from ..schema import TOKEN_KEYS

_ENTRY_TYPES_WITH_USAGE = ("compaction", "branch_summary")


def pi_munged_project_dir(path: str) -> str:
    """Reproduce Pi's session-dir encoding: ``--<cwd>---`` where the cwd loses its single
    leading ``/`` or ``\\`` and every remaining ``/``, ``\\``, and ``:`` becomes ``-``.
    Dots, underscores, and spaces are preserved (unlike Claude's lossy encoding), e.g.
    ``/home/u/llm_resource_tally`` -> ``--home-u-llm_resource_tally--`` and
    ``C:\\Users\\me\\repo`` -> ``--C--Users-me-repo--``."""
    body = path
    if body and body[0] in ("/", "\\"):
        body = body[1:]
    for ch in ("/", "\\", ":"):
        body = body.replace(ch, "-")
    return "--" + body + "--"


#: Pi's two session-storage layouts (see the module docstring): "default" files live in
#: per-cwd ``--<encoded-cwd>--`` children under Pi's session root; "explicit" files live
#: DIRECTLY in an explicitly named session dir (Pi passes such a dir to its SessionManager
#: verbatim and never appends an encoded-cwd child to it).
LAYOUT_DEFAULT_ROOT = "default"
LAYOUT_EXPLICIT_DIR = "explicit"

#: A caller-supplied ``--projects-dir`` that is not the resolver's own path could hold
#: either layout, so both shapes are accepted (each still bounded to one level deep).
LAYOUT_EITHER = "either"


def resolve_sessions() -> tuple[str, str]:
    """``(sessions_dir, layout)`` for this workstation, in Pi's own precedence:
    ``PI_SESSIONS_DIR`` (tally's override) > ``PI_CODING_AGENT_SESSION_DIR`` (Pi's env var)
    > the project ``.pi/settings.json`` ``sessionDir`` (relative to that repo) > the global
    ``<agent-dir>/settings.json`` ``sessionDir`` (relative to the agent dir) >
    ``<agent-dir>/sessions``, where ``<agent-dir>`` is ``$PI_CODING_AGENT_DIR`` or
    ``~/.pi/agent``. Every source except the final fallback is an *explicit* dir (layout
    ``"explicit"``, files directly inside); only the fallback is the default root (layout
    ``"default"``, per-cwd encoded children)."""
    for env in ("PI_SESSIONS_DIR", "PI_CODING_AGENT_SESSION_DIR"):
        value = os.environ.get(env)
        if value:
            return os.path.expanduser(value), LAYOUT_EXPLICIT_DIR
    agent_dir = os.path.expanduser(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent"))
    for path, base in (
        (os.path.join(superproject_root(), ".pi", "settings.json"), superproject_root()),
        (os.path.join(agent_dir, "settings.json"), agent_dir),
    ):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        session_dir = data.get("sessionDir")
        if isinstance(session_dir, str) and session_dir.strip():
            session_dir = os.path.expanduser(session_dir)
            if not os.path.isabs(session_dir):
                session_dir = os.path.normpath(os.path.join(base, session_dir))
            return session_dir, LAYOUT_EXPLICIT_DIR
    return os.path.join(agent_dir, "sessions"), LAYOUT_DEFAULT_ROOT


def default_sessions_dir() -> str:
    """The sessions dir the backend reads (the path half of :func:`resolve_sessions`)."""
    return resolve_sessions()[0]


def _real(path: str | None) -> str | None:
    return os.path.realpath(os.path.expanduser(path)) if path else None


def _path_in(path: str | None, root: str) -> bool:
    path = _real(path)
    root = _real(root)
    return bool(path and root and (path == root or path.startswith(root + os.sep)))


def _int(value) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return 0


def _usage(raw) -> dict | None:
    """Map one pi-ai ``Usage`` object onto the canonical token keys.

    ``reasoning`` is deliberately NOT read: it is a subset of ``output`` in both of pi-ai's
    provider normalizers, and adding it would double-count thinking tokens. Returns ``None``
    when there is no usage object (the record is not a billed call).
    """
    if not isinstance(raw, dict):
        return None
    if not any(k in raw for k in ("input", "output", "cacheRead", "cacheWrite")):
        return None
    return {
        "input_tokens": _int(raw.get("input")),
        "cache_creation_input_tokens": _int(raw.get("cacheWrite")),
        "cache_read_input_tokens": _int(raw.get("cacheRead")),
        "output_tokens": _int(raw.get("output")),
    }


def _is_zero(usage: dict) -> bool:
    return all(usage.get(k, 0) == 0 for k in TOKEN_KEYS)


def _billing_model(msg: dict) -> str | None:
    """The provider-qualified model an assistant call is BILLED as: the concrete model
    that actually answered. Newer Pi records it in ``responseModel`` (it can differ from
    the requested ``model``, e.g. a fallback); when present it wins, mirroring Pi's own
    ``responseModel ?? model`` usage keying. ``None`` when the message names no model."""
    provider = msg.get("provider")
    model = msg.get("responseModel")
    if not (isinstance(model, str) and model):
        model = msg.get("model")
    if isinstance(provider, str) and provider and isinstance(model, str) and model:
        return f"{provider}/{model}"
    return None


def _state_model(msg: dict) -> str | None:
    """The model state an assistant message ESTABLISHES for its descendants: Pi's own
    session-state reconstruction (``getSessionContextSettings``) records the message's
    requested ``provider`` + ``model`` — NOT the concrete ``responseModel`` that answered
    — so descendants below a fallback-served call inherit the logical model. ``None`` when
    the message names no requested model (the state then stays whatever the ancestry had)."""
    provider = msg.get("provider")
    model = msg.get("model")
    if isinstance(provider, str) and provider and isinstance(model, str) and model:
        return f"{provider}/{model}"
    return None


def _model_source(rec: dict) -> str | None:
    """The model state an entry ESTABLISHES for its descendants (Pi's session model state):
    a ``model_change`` sets the switched-to provider/modelId; an assistant message sets its
    requested provider/model (:func:`_state_model` — the state Pi's own
    ``getSessionContextSettings`` records, not the ``responseModel`` that answered). Any
    other entry (user, toolResult, compaction, branch_summary, usage, ...) changes nothing —
    its descendants inherit the nearest ancestor's state."""
    t = rec.get("type")
    if t == "model_change":
        provider = rec.get("provider")
        model = rec.get("modelId")
        if isinstance(provider, str) and provider and isinstance(model, str) and model:
            return f"{provider}/{model}"
        return None
    if t == "message":
        msg = rec.get("message")
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return _state_model(msg)
    return None


def _own_billing_model(rec: dict) -> str | None:
    """The model an entry is BILLED as when it names one of its own: an assistant message
    (the concrete model that answered — :func:`_billing_model`) or a top-level ``usage``
    entry (its provider/model). Entries that record no model of their own (tool results,
    compactions, branch summaries, a bare assistant) inherit the session state in effect at
    their parent instead — the *requested* model of the nearest model source (see
    :func:`_model_source`), which for an assistant is not the responseModel it billed."""
    t = rec.get("type")
    if t == "message":
        msg = rec.get("message")
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return _billing_model(msg)
    if t == "usage":
        provider = rec.get("provider")
        model = rec.get("model")
        if isinstance(provider, str) and provider and isinstance(model, str) and model:
            return f"{provider}/{model}"
    return None


def _index(transcript: str) -> tuple[dict[str, dict], dict[str, str | None], list[str]]:
    """``(by_id, parent_of, order)`` for every tree entry in the file, in append order.

    The parent graph is Pi's own: a v2/v3 entry's ``parentId`` (its parent entry id, or
    ``None`` for the tree root); a v1 (no-id, linear) entry's parent is the previous entry
    in append order. The opening ``type: "session"`` header is not a tree entry and is
    excluded. Entries copied from a parent session (a fork's prefix) stay in the graph even
    though the fork will not bill them: a new descendant's model state must still resolve
    through them."""
    by_id: dict[str, dict] = {}
    parent_of: dict[str, str | None] = {}
    order: list[str] = []
    prev: str | None = None
    for lineno, rec in _entries(transcript):
        if rec.get("type") == "session":
            continue  # the header is not a tree entry
        has_id = isinstance(rec.get("id"), str) and rec.get("id")
        mid = rec.get("id") if has_id else f"{lineno}:{rec.get('timestamp')}"
        if mid in by_id:
            continue  # a duplicated id (corruption) must not corrupt the graph
        if has_id:
            pid = rec.get("parentId")
            parent = pid if isinstance(pid, str) and pid else None
        else:
            parent = prev  # v1: the previous entry in append order (None for the first)
        by_id[mid] = rec
        parent_of[mid] = parent
        order.append(mid)
        prev = mid
    return by_id, parent_of, order


def _state_after(
    nid: str, by_id: dict[str, dict], parent_of: dict[str, str | None], memo: dict[str, str]
) -> str:
    """The session model in effect immediately AFTER entry ``nid``: its own model source
    when it has one, else its parent's — i.e. the last ``model_change``/assistant source on
    the path from the tree root to ``nid``, Pi's own ``getSessionContextSettings`` walk,
    memoized per file. ``"?"`` when the ancestry is a broken link or a cycle and the state
    genuinely cannot be reconstructed."""
    if nid in memo:
        return memo[nid]
    chain: list[str] = []
    seen: set[str] = set()
    cur = nid
    while True:
        if cur in memo:
            base = memo[cur]
            break
        if cur in seen or cur not in by_id:
            base = "?"  # a cycle or a broken parent link: stop, cannot reconstruct
            break
        seen.add(cur)
        chain.append(cur)
        cur = parent_of.get(cur)
        if cur is None:
            base = "?"  # reached the tree root with no model source in the chain
            break
    for node in reversed(chain):  # oldest ancestor first: each source overwrites the base
        own = _model_source(by_id[node])
        if own:
            base = own
        memo[node] = base
    return memo[nid]


def _billed_model(
    nid: str, by_id: dict[str, dict], parent_of: dict[str, str | None], memo: dict[str, str]
) -> str:
    """The model an entry is billed as: its own when it names one (an assistant with a
    model, a ``usage`` entry), else the session state in effect immediately BEFORE it (its
    parent's after-state) — or ``"?"`` when that state genuinely cannot be reconstructed."""
    own = _own_billing_model(by_id[nid])
    if own:
        return own
    parent = parent_of.get(nid)
    if parent is None or parent not in by_id:
        return "?"
    return _state_after(parent, by_id, parent_of, memo)


def _session_meta(transcript: str) -> dict:
    """The opening ``type: "session"`` header, or an empty dict when the file has none
    (v1-linear files predate the header; a torn first line must not kill discovery).
    ``parent_session`` (the ``parentSession`` pointer set on forks/clones) is a lineage
    signal only; it no longer gates which entries are billed (see the module docstring)."""
    out = {"id": None, "ts": None, "cwd": None, "parent_session": None}
    try:
        with open(transcript, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    break
                if not isinstance(rec, dict) or rec.get("type") != "session":
                    break
                out["id"] = rec.get("id") if isinstance(rec.get("id"), str) else None
                out["ts"] = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
                out["cwd"] = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
                parent = rec.get("parentSession")
                out["parent_session"] = parent if isinstance(parent, str) and parent else None
                return out
    except OSError:
        pass
    return out


def _entries(transcript: str):
    """Yield ``(index, record)`` for each parseable JSONL line (index counts all lines, so
    synthesized ids for v1 entries stay stable). Unparseable lines — including a torn last
    line of a file appended to live — are skipped, never fatal."""
    try:
        fh = open(transcript, encoding="utf-8")
    except OSError:
        return
    with fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            yield lineno, rec


def _observation_fingerprint(payload: dict) -> str:
    """The stable machine-wide identity of one physical model-usage observation:
    a sha256 over the canonical JSON encoding of its stable non-content metadata.

    Fork/clone/branch copies are verbatim, so every field digested here is identical
    across copies and the digest matches — while a fork's fresh session id lives only in
    the header line (never in an entry) and is not part of the identity. Two unrelated
    entries that happen to share Pi's 8-hex entry id differ on at least one other field
    (in practice the timestamp and/or the usage counters) and therefore on the digest. Only
    this opaque digest is ever persisted in the claim log — never message or prompt text.
    """
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _entry_fingerprint(rec: dict, kind: str, usage: dict | None, msg: dict | None = None) -> str:
    """Fingerprint of one billed entry: its stable non-content metadata. ``kind`` is the
    normalized turn kind (``assistant``, ``tool_result``, ``usage``/``usage:<kind>``,
    ``compaction``, ``branch_summary``); ``usage`` is the canonical usage dict, or ``None``
    for a usage-less estimate event (which instead digests the entry's summary)."""
    payload: dict = {
        # v1 entries have no id of their own; the timestamp plays that role (it is stable
        # across verbatim copies), so their fingerprints stay copy-invariant too.
        "id": rec.get("id") or rec.get("timestamp"),
        "parentId": rec.get("parentId"),
        "ts": rec.get("timestamp"),
        "kind": kind,
        "usage": usage,
    }
    if msg is not None:
        if kind == "assistant":
            payload["provider"] = msg.get("provider")
            payload["model"] = msg.get("responseModel") or msg.get("model")
            payload["stopReason"] = msg.get("stopReason")
        elif kind == "tool_result":
            payload["toolName"] = msg.get("toolName")
            payload["callId"] = msg.get("callId")
            payload["isError"] = msg.get("isError")
    if kind == "usage" or kind.startswith("usage:"):
        payload["provider"] = rec.get("provider")
        payload["model"] = rec.get("model")
    if kind in _ENTRY_TYPES_WITH_USAGE:
        payload["tokensBefore"] = rec.get("tokensBefore")
        summary = rec.get("summary")
        if isinstance(summary, str) and summary:
            payload["summary"] = hashlib.sha256(summary.encode("utf-8", "replace")).hexdigest()
    return _observation_fingerprint(payload)


def _turn(mid: str, ts: str, kind: str, model: str, usage: dict, claim_id: str | None = None) -> dict:
    t = {
        "id": mid,
        "ts": ts,
        "type": kind,
        "model": model,
        "usage": {k: usage.get(k, 0) for k in TOKEN_KEYS},
        "web_search": 0,
        "web_fetch": 0,
    }
    if claim_id:
        t["claim_id"] = claim_id
    return t


def _sort_key(t: dict):
    try:
        return (0, to_dt(t["ts"]), t["id"])
    except (TypeError, ValueError):
        return (1, t["ts"] or "", t["id"])


def _walk(transcript: str) -> tuple[list[dict], list[dict], dict]:
    """Parse a session file -> (measured turns, compaction-estimate events, zero-usage
    diagnostics). Turn ids are the entry ids (unique within a file); v1 entries without ids
    get a stable synthesized id.

    The file is first indexed with its ``parentId`` graph (:func:`_index`); each billed entry
    then resolves its effective model from its ANCESTRY (:func:`_billed_model`) rather than
    append order, so in-file branching attributes correctly. A ``usage`` entry is a measured
    turn billed under its own provider/model; a compaction/branch-summary with usage is a
    measured turn, without one it is an estimate event.

    Copied (fork) prefixes are NOT filtered here: every measured observation is emitted with
    a stable ``claim_id`` (:func:`_entry_fingerprint`), and the accounting layer (record/
    reconcile plus the per-user observation claim log) is what allocates each physical
    observation exactly once across all of its copies — so an unclaimed copy is always
    billable (from a fork, even if its parent file is deleted) and an already-billed copy is
    always suppressed, no matter which file or which clock skew produced it."""
    by_id, parent_of, order = _index(transcript)
    memo: dict[str, str] = {}
    turns_by_id: dict[str, dict] = {}
    events: list[dict] = []
    diag = {"calls": 0, "zero_calls": 0, "zero_failed_calls": 0}

    for nid in order:
        rec = by_id[nid]
        ts = rec.get("timestamp")
        if not isinstance(ts, str) or not ts:
            continue
        etype = rec.get("type")
        if etype == "message":
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            role = msg.get("role")
            if role == "assistant":
                usage = _usage(msg.get("usage"))
                if usage is None:
                    continue
                if _is_zero(usage) and msg.get("stopReason") == "error":
                    # A failed call recorded no consumption at all: not a turn, not a miss.
                    diag["zero_failed_calls"] += 1
                    continue
                diag["calls"] += 1
                if _is_zero(usage):
                    diag["zero_calls"] += 1
                turns_by_id[nid] = _turn(
                    nid, ts, "assistant", _billed_model(nid, by_id, parent_of, memo), usage,
                    _entry_fingerprint(rec, "assistant", usage, msg),
                )
            elif role == "toolResult":
                usage = _usage(msg.get("usage"))
                if usage is None:
                    continue
                if _is_zero(usage) and msg.get("isError"):
                    diag["zero_failed_calls"] += 1
                    continue
                diag["calls"] += 1
                if _is_zero(usage):
                    diag["zero_calls"] += 1
                # A tool's nested LLM work records no model of its own; attribute it to the
                # session model in effect at the point the tool ran (its parent's state).
                turns_by_id[nid] = _turn(
                    nid, ts, "tool_result", _billed_model(nid, by_id, parent_of, memo), usage,
                    _entry_fingerprint(rec, "tool_result", usage, msg),
                )
        elif etype == "usage":
            # A top-level UsageEntry: model-attributed usage that is not an assistant message
            # (e.g. cache warming). Billed under its OWN provider/model, kind kept verbatim;
            # it establishes no model state for descendants.
            usage = _usage(rec.get("usage"))
            if usage is None:
                continue  # no usage object: nothing measured to bill
            diag["calls"] += 1
            if _is_zero(usage):
                diag["zero_calls"] += 1
            kind = "usage"
            k = rec.get("kind")
            if isinstance(k, str) and k:
                kind = f"usage:{k}"
            turns_by_id[nid] = _turn(
                nid, ts, kind, _billed_model(nid, by_id, parent_of, memo), usage,
                _entry_fingerprint(rec, kind, usage),
            )
        elif etype in _ENTRY_TYPES_WITH_USAGE:
            usage = _usage(rec.get("usage"))
            if usage is not None:
                # Measured: the summarization LLM call's real usage (its model is the
                # session state at its parent; the entry records no model of its own).
                diag["calls"] += 1
                if _is_zero(usage):
                    diag["zero_calls"] += 1
                turns_by_id[nid] = _turn(
                    nid, ts, str(etype), _billed_model(nid, by_id, parent_of, memo), usage,
                    _entry_fingerprint(rec, str(etype), usage),
                )
            else:
                # No usage object: nothing measured to bill — expose the measured signals
                # for a reconstructed estimate row, Claude-style. The event carries the
                # same stable claim_id as the underlying entry, so a verbatim copy of the
                # estimate is allocated exactly once too.
                events.append(
                    {
                        "boundary_ts": ts,
                        "model": _billed_model(nid, by_id, parent_of, memo),
                        "peak_context_tokens": _int(rec.get("tokensBefore")),
                        "summary_chars": len(rec.get("summary") or ""),
                        "claim_id": _entry_fingerprint(rec, str(etype), None),
                    }
                )
        # model_change, user, and all other entry types: session state only, never a billed turn

    turns = [turns_by_id[nid] for nid in order if nid in turns_by_id]
    turns.sort(key=_sort_key)
    return turns, events, diag


def _session_id_of(transcript: str) -> str | None:
    """The session's own identity: the header uuid when present, else the uuid part of the
    filename (`<timestamp>_<uuid>.jsonl`), else the bare filename stem."""
    meta = _session_meta(transcript)
    if meta.get("id"):
        return meta["id"]
    stem = os.path.splitext(os.path.basename(transcript))[0]
    return stem.rsplit("_", 1)[-1] if "_" in stem else stem


class PiBackend(Backend):
    name = "pi"

    def default_projects_dir(self) -> str:
        return default_sessions_dir()

    def _repo_transcripts(self, projects_dir: str) -> list[str]:
        """Pi session files whose header ``cwd`` lies in this repo, shaped by the
        session-dir layout (see :func:`resolve_sessions`).

        Under the *default* root only this repo's ``--<encoded-cwd>--`` dir is scanned, plus
        a prefix overapproximation of *subdirectory* dirs (``--<root>-*--``: the separator
        between the munged root and the subpath is a single ``-``, and Pi's encoding is
        lossy about a literal ``-``). Under an *explicit* dir only the JSONL files directly
        in it are scanned (Pi writes them there, one level deep). A ``--projects-dir`` that
        is not the resolver's own path could be either, so both shapes are accepted. Neither
        mode recurses. Directory naming is never sufficient on its own: every candidate file
        is checked against its session header ``cwd`` before it is attributed here."""
        root = superproject_root()
        root_real = _real(root)
        if not root_real:
            return []
        resolver_path, layout = resolve_sessions()
        if _real(projects_dir) != _real(resolver_path):
            layout = LAYOUT_EITHER
        paths: set[str] = set()
        if layout in (LAYOUT_DEFAULT_ROOT, LAYOUT_EITHER):
            munged = pi_munged_project_dir(root)
            dirs = [os.path.join(projects_dir, munged)]
            dirs.extend(glob.glob(os.path.join(projects_dir, munged[:-1] + "*--")))
            for d in dirs:
                paths.update(glob.glob(os.path.join(d, "*.jsonl")))
        if layout in (LAYOUT_EXPLICIT_DIR, LAYOUT_EITHER):
            paths.update(glob.glob(os.path.join(projects_dir, "*.jsonl")))
        out = []
        for p in sorted(paths):
            cwd = _session_meta(p).get("cwd")
            if cwd and _path_in(cwd, root_real):
                out.append(p)
        return out

    def find_transcript(self, projects_dir: str, session: str | None, strict: bool = False) -> str | None:
        """Prefer ``$PI_SESSION_FILE`` — set in the environment of commands Pi's shell tool
        runs — for exact attribution of the committing session. The hint is trusted like the
        Claude hook's: it names the session whose shell executed the commit, so its recorded
        cwd is deliberately NOT required to lie in this repo (a session elsewhere that
        commits here via `git -C` is still that commit's author). Else the most recently
        modified session file for this repo (header-cwd verified), or the one named by
        ``session``. In ``strict`` mode never fall back to another project's session."""
        env_file = os.environ.get("PI_SESSION_FILE")
        if env_file:
            env_file = os.path.expanduser(env_file)
            meta = _session_meta(env_file)
            # Recognize only real Pi session files (a parseable `type: "session"` header);
            # the header's cwd is NOT containment-checked (see the trust model above).
            if os.path.isfile(env_file) and (meta.get("cwd") or meta.get("id")):
                if session is None or _session_id_of(env_file) == session:
                    return env_file
        candidates = sorted(self._repo_transcripts(projects_dir), key=os.path.getmtime, reverse=True)
        if session:
            for c in candidates:
                if _session_id_of(c) == session:
                    return c
            if strict:
                return None
            sys.exit(f"error: no Pi session {session} for this repo under {projects_dir}")
        if not candidates:
            if strict:
                return None
            sys.exit(f"error: no Pi session for this repo under {projects_dir}")
        return candidates[0]

    def session_transcripts(self, projects_dir: str) -> list[str]:
        return self._repo_transcripts(projects_dir)

    def session_id(self, transcript: str) -> str | None:
        return _session_id_of(transcript)

    def parse_turns(self, transcript: str) -> list[dict]:
        """Every measured turn for this session file: assistant messages, a tool's nested LLM
        usage, top-level ``usage`` entries (billed under their own provider/model), and
        compaction/branch-summary entries that carry measured usage. Copied (fork) prefixes
        are NOT excluded here — each turn carries a stable ``claim_id`` and the accounting
        layer allocates each physical observation exactly once across all of its copies —
        while failed zero-usage calls stay excluded and successful zero-usage calls remain
        as zero-token turns."""
        return _walk(transcript)[0]

    def parse_compaction_events(self, transcript: str) -> list[dict]:
        """Only compaction/branch-summary entries WITHOUT a usage object — Pi normally
        measures them (they are billed as turns by :meth:`parse_turns`)."""
        return _walk(transcript)[1]

    def usage_diagnostics(self, transcript: str) -> dict | None:
        """Zero-usage health for the endpoints this session used: ``{"calls", "zero_calls",
        "zero_failed_calls"}`` or ``None`` when the file has no billed calls. ``zero_calls``
        are successful calls that reported nothing (endpoint silent — totals undercount);
        ``zero_failed_calls`` are excluded failures (no consumption)."""
        diag = _walk(transcript)[2]
        if not diag["calls"]:
            return None
        return diag
