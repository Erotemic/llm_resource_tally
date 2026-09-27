# SPDX-License-Identifier: Apache-2.0
"""Durable observation ownership with a workstation-local coordination index.

Backends such as Pi can identify one physical model-usage observation even when the source entry
is copied into several session files. The owning repository ledger persists that canonical identity
(``observation_ids`` / compact ``oi``). This module's SQLite database is only an index from those
identities and compatibility aliases to repository owners; an indexed owner is revalidated against
the visible ledger before it can suppress a candidate.

Allocation is serialized on POSIX across reservation -> ledger append -> finalization. The protocol
remains workstation-local and intentionally degrades to same-repository ledger idempotence if the
index is unavailable. It is not organization-wide exactly-once accounting.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from contextlib import contextmanager

from ._util import exclusive_file_lock, to_dt


def _home() -> str:
    env = os.environ.get("LLM_RESOURCE_TALLY_HOME")
    return os.path.expanduser(env) if env else os.path.expanduser("~/.llm_resource_tally")


def _realpath(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _open_allocation_lock() -> object:
    """Open the per-user stable-observation allocation lock file.

    The lock is always taken before any per-repository ledger lock. ``flock`` is released on
    process death; where advisory locking is unavailable the caller continues best-effort.
    """
    os.makedirs(_home(), exist_ok=True)
    return open(os.path.join(_home(), "observation-claims.lock"), "a", encoding="utf-8")


# ---------------------------------------------------------------------------
# Observation allocation index
def event_claims_path() -> str:
    """Path of the legacy pre-index log, read as compatibility input only."""
    return os.path.join(_home(), "event-claims.jsonl")


def observation_index_path() -> str:
    return os.path.join(_home(), "observation-claims.sqlite3")


def observation_index_health() -> tuple[bool, str]:
    """Read-only integrity signal for ``doctor``; never creates or repairs the index."""
    path = observation_index_path()
    if not os.path.exists(path):
        return True, "not created yet"
    try:
        uri = "file:" + os.path.abspath(path) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        return False, str(exc)
    if not row or row[0] != "ok":
        return False, str(row[0] if row else "quick_check returned no result")
    return True, "ok"


def _open_observation_db() -> sqlite3.Connection:
    os.makedirs(_home(), exist_ok=True)
    conn = sqlite3.connect(observation_index_path(), timeout=30.0)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS observation_claims (
               agent TEXT NOT NULL,
               claim_id TEXT NOT NULL,
               repo TEXT NOT NULL,
               state TEXT NOT NULL CHECK(state IN ('pending','committed')),
               observed_ts TEXT,
               PRIMARY KEY(agent, claim_id)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS observation_aliases (
               agent TEXT NOT NULL,
               alias_id TEXT NOT NULL,
               claim_id TEXT NOT NULL,
               PRIMARY KEY(agent, alias_id),
               FOREIGN KEY(agent, claim_id)
                 REFERENCES observation_claims(agent, claim_id) ON DELETE CASCADE
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS legacy_claims (
               agent TEXT NOT NULL,
               alias_id TEXT NOT NULL,
               repo TEXT NOT NULL,
               PRIMARY KEY(agent, alias_id, repo)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS observation_meta (
               key TEXT PRIMARY KEY,
               value TEXT NOT NULL
           )"""
    )
    _migrate_legacy_event_claims(conn)
    return conn


def _migrate_legacy_event_claims(conn: sqlite3.Connection) -> None:
    """Import changed legacy JSONL claims without treating them as durable authority.

    Older tally versions may still be used after this index has been created (for example a
    temporary downgrade), so remember the legacy file's stat signature rather than a one-shot
    boolean.  Re-scan only when that file changes.  Old rows do not identify their owning ledger
    row: they suppress a candidate only while the referenced repository still has a compatible
    visible allocation; otherwise the bridge row is deleted as stale.
    """
    path = event_claims_path()
    try:
        st = os.stat(path)
        signature = f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        signature = "missing"
    previous = conn.execute(
        "SELECT value FROM observation_meta WHERE key='legacy_event_claims_signature_v2'"
    ).fetchone()
    if previous and previous[0] == signature:
        return

    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        fh = None
    if fh is not None:
        with fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                agent = d.get("agent")
                alias = d.get("claim_id") or d.get("event_id")
                repo = d.get("repo")
                if all(isinstance(x, str) and x for x in (agent, alias, repo)):
                    conn.execute(
                        "INSERT OR IGNORE INTO legacy_claims(agent,alias_id,repo) VALUES(?,?,?)",
                        (agent, alias, _realpath(repo)),
                    )
    conn.execute(
        "INSERT OR REPLACE INTO observation_meta(key,value) VALUES('legacy_event_claims_signature_v2',?)",
        (signature,),
    )
    conn.commit()


def _observation_refs(observations) -> list[dict]:
    """Normalize backend observations for allocation.

    ``observation_id`` is the canonical identity. ``observation_aliases`` are older fingerprint
    versions that must also suppress the same call.  Timestamp/model/usage are used only
    to validate pre-index legacy JSONL claims; they are never persisted as transcript text.
    """
    out = []
    seen = set()
    for obs in observations:
        if isinstance(obs, str):
            obs = {"observation_id": obs}
        if not isinstance(obs, dict):
            continue
        claim = obs.get("observation_id")
        if not isinstance(claim, str) or not claim or claim in seen:
            continue
        aliases = []
        for alias in obs.get("observation_aliases") or []:
            if isinstance(alias, str) and alias and alias != claim and alias not in aliases:
                aliases.append(alias)
        out.append(
            {
                "observation_id": claim,
                "observation_aliases": aliases,
                "ts": obs.get("ts") or obs.get("boundary_ts"),
                "model": obs.get("model"),
                "type": obs.get("type") or obs.get("kind"),
                "usage": obs.get("usage") if isinstance(obs.get("usage"), dict) else None,
            }
        )
        seen.add(claim)
    return out


def _visible_rows(
    repo: str, cache: dict[str, list[dict] | None]
) -> list[dict] | None:
    """Visible ledger rows for ``repo``; ``None`` means the owner is unreadable.

    A missing repository is evidence that this workstation-local owner pointer is stale, so it
    returns an empty list.  An existing repository whose ledger cannot currently be read is
    different: treating a transient read/parse/git failure as "no owner" would delete the index
    row and can permanently double-bill once the original ledger becomes readable again.  Callers
    therefore preserve/suppress an indexed claim while ownership is temporarily unverifiable.
    """
    repo = _realpath(repo)
    if repo not in cache:
        if not os.path.isdir(repo):
            cache[repo] = []
        else:
            try:
                from .ledger import read_ledger

                cache[repo] = read_ledger(root=repo)
            except (OSError, ValueError, subprocess.CalledProcessError):
                cache[repo] = None
    return cache[repo]


def _visible_observation_ids(
    repo: str, agent: str, cache: dict[str, list[dict] | None]
) -> set[str] | None:
    rows = _visible_rows(repo, cache)
    if rows is None:
        return None
    out: set[str] = set()
    for row in rows:
        if row.get("agent") != agent:
            continue
        for claim in row.get("observation_ids") or []:
            if isinstance(claim, str) and claim:
                out.add(claim)
    return out


def _legacy_claim_visible(
    repo: str, agent: str, obs: dict, cache: dict[str, list[dict] | None]
) -> bool | None:
    """Conservative bridge for pre-index claims whose old ledger rows lack observation ids."""
    ts = obs.get("ts")
    if not isinstance(ts, str) or not ts:
        return False
    model = obs.get("model")
    usage = obs.get("usage") or {}
    token_map = {
        "input_tokens": "input",
        "cache_creation_input_tokens": "cache_write",
        "cache_read_input_tokens": "cache_read",
        "output_tokens": "output",
    }
    try:
        point = to_dt(ts)
    except (TypeError, ValueError):
        return False
    rows = _visible_rows(repo, cache)
    if rows is None:
        return None
    for row in rows:
        if row.get("agent") != agent:
            continue
        if row.get("kind") == "compaction-estimate":
            if row.get("boundary_ts") == ts:
                return True
            continue
        rng = row.get("turn_ts_range") or [None, None]
        if not rng[0] or not rng[1]:
            continue
        try:
            if not (to_dt(rng[0]) <= point <= to_dt(rng[1])):
                continue
        except (TypeError, ValueError):
            continue
        if isinstance(model, str) and model and model != "?" and model not in (row.get("models") or []):
            continue
        totals = row.get("tokens") or {}
        if any(int(totals.get(dst, 0) or 0) < int(usage.get(src, 0) or 0) for src, dst in token_map.items()):
            continue
        return True
    return False


def _delete_claim(conn: sqlite3.Connection, agent: str, claim_id: str) -> None:
    conn.execute("DELETE FROM observation_claims WHERE agent=? AND claim_id=?", (agent, claim_id))


def _candidate_is_claimed(
    conn: sqlite3.Connection,
    agent: str,
    obs: dict,
    row_cache: dict[str, list[dict] | None],
) -> bool:
    ids = [obs["observation_id"], *obs.get("observation_aliases", [])]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT DISTINCT c.claim_id,c.repo,c.state
              FROM observation_aliases a
              JOIN observation_claims c ON c.agent=a.agent AND c.claim_id=a.claim_id
             WHERE a.agent=? AND a.alias_id IN ({placeholders})""",
        [agent, *ids],
    ).fetchall()
    for canonical, repo, state in rows:
        visible = _visible_observation_ids(repo, agent, row_cache)
        if visible is None:
            # The owner repository still exists but its ledger is temporarily unverifiable.
            # Preserve the pointer and suppress for now; a later healthy reconcile can decide
            # whether the owner really disappeared.  Fail-closed here prevents double billing.
            return True
        if canonical in visible:
            if state != "committed":
                conn.execute(
                    "UPDATE observation_claims SET state='committed' WHERE agent=? AND claim_id=?",
                    (agent, canonical),
                )
            return True
        # The index points at an allocation that no longer exists.  Ledger is authoritative:
        # remove the stale tombstone so a retained transcript can heal the gap.
        _delete_claim(conn, agent, canonical)

    legacy = conn.execute(
        f"SELECT alias_id,repo FROM legacy_claims WHERE agent=? AND alias_id IN ({placeholders})",
        [agent, *ids],
    ).fetchall()
    for alias, repo in legacy:
        visible = _legacy_claim_visible(repo, agent, obs, row_cache)
        if visible is not False:
            # True = compatible owner found; None = owner exists but cannot currently be
            # inspected.  Both suppress without deleting the bridge row.
            return True
        conn.execute(
            "DELETE FROM legacy_claims WHERE agent=? AND alias_id=? AND repo=?",
            (agent, alias, repo),
        )
    return False


def _index_claim(
    conn: sqlite3.Connection,
    agent: str,
    obs: dict,
    repo: str,
    *,
    owner_observation_id: str | None = None,
    state: str = "pending",
) -> None:
    """Index one observation allocation and the durable identities that name its owner.

    ``owner_observation_id`` is normally the candidate's canonical identity. It can instead be an
    older durable id already persisted in a visible ledger row; mapping the current canonical id
    to that owner lets a deleted/rebuilt workstation index relearn ownership without rewriting
    history. Weak pre-ledger compatibility fingerprints remain lookup probes only.
    """
    owner = owner_observation_id or obs["observation_id"]
    conn.execute(
        """INSERT INTO observation_claims(agent,claim_id,repo,state,observed_ts)
           VALUES(?,?,?,?,?)
           ON CONFLICT(agent,claim_id) DO UPDATE SET
             repo=excluded.repo, state=excluded.state, observed_ts=excluded.observed_ts""",
        (agent, owner, _realpath(repo), state, obs.get("ts")),
    )
    # Persist only durable identity versions here. Candidate ``observation_aliases`` are compatibility
    # probes for old ledgers / the legacy JSONL bridge; indexing every weak historical alias for
    # every new observation would let two unrelated modern calls collide through an old fingerprint
    # format. If ``owner`` is an older durable id, map the current canonical id to it so future
    # canonical candidates still find that owner.
    for alias in dict.fromkeys([owner, obs["observation_id"]]):
        conn.execute(
            """INSERT INTO observation_aliases(agent,alias_id,claim_id) VALUES(?,?,?)
               ON CONFLICT(agent,alias_id) DO UPDATE SET claim_id=excluded.claim_id""",
            (agent, alias, owner),
        )


@contextmanager
def allocate_observations(agent: str, observations, repo: str):
    """Reserve and finalize stable usage observations against ledger-backed ownership.

    Yields the canonical observation ids that are genuinely fresh.  The caller must persist each
    billed canonical id in the ledger row's ``observation_ids`` field before leaving the
    context.  Existing allocations suppress only when their referenced repository still exposes
    the owning observation id.  Thus the index can coordinate forks across repositories
    without becoming a non-recoverable source of truth.

    On POSIX the per-user file lock serializes the complete reserve -> ledger append ->
    finalize sequence.  The SQLite reservation is committed *before* yielding so a process
    crash leaves enough information for the next lock holder to distinguish "row append
    happened" from "reservation only".  SQLite is an index, not the accounting ledger.
    """
    refs = _observation_refs(observations)
    if not agent or not refs:
        yield set()
        return

    fh = None
    lock = None
    conn = None
    fresh: list[dict] = list(refs)  # best-effort fallback if the index cannot be opened
    body_error = None
    try:
        try:
            fh = _open_allocation_lock()
            lock = exclusive_file_lock(fh)
            lock.__enter__()
        except OSError:
            if fh is not None:
                fh.close()
            fh = None
            lock = None

        try:
            conn = _open_observation_db()
            cache: dict[str, list[dict] | None] = {}
            fresh = []
            # The ledger is authoritative even if the workstation index was deleted.  Relearn
            # ownership from this repository before consulting cross-repository index pointers.
            # Checking aliases as well as the canonical id also makes a future fingerprint
            # migration able to recognize rows written by an older version.
            local_visible = _visible_observation_ids(repo, agent, cache)
            if local_visible is None:
                # The current repository's accounting truth cannot be inspected.  Do not
                # manufacture a new allocation merely because the workstation index happens
                # to be empty/deleted: the unreadable ledger may already own it.  Foreign
                # owner lookup cannot make that ambiguity safe, so fail closed for this pass.
                fresh = []
            else:
                for obs in refs:
                    durable_id = next(
                        (
                            cid
                            for cid in [obs["observation_id"], *obs.get("observation_aliases", [])]
                            if cid in local_visible
                        ),
                        None,
                    )
                    if durable_id is not None:
                        _index_claim(
                            conn, agent, obs, repo, owner_observation_id=durable_id, state="committed"
                        )
                        continue
                    if not _candidate_is_claimed(conn, agent, obs, cache):
                        _index_claim(conn, agent, obs, repo)
                        fresh.append(obs)
            # Pending reservations must survive a crash in the caller body so the next holder
            # can inspect whether the corresponding ledger row landed.
            conn.commit()
        except (OSError, sqlite3.Error):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            conn = None
            # Losing the workstation index must not destroy same-repository idempotence.
            # Stable ownership is persisted in the ledger itself, so even in degraded mode
            # suppress observations whose canonical id (or an older compatible identity) is
            # already visible here. Cross-repository coordination is the only capability lost.
            visible = _visible_observation_ids(repo, agent, {})
            if visible is None:
                # The current ledger is unverifiable. Fail closed rather than append another
                # copy whose ownership we cannot distinguish from the unreadable one.
                fresh = []
            else:
                fresh = [
                    obs
                    for obs in refs
                    if not any(
                        cid in visible
                        for cid in [obs["observation_id"], *obs.get("observation_aliases", [])]
                    )
                ]

        try:
            yield {obs["observation_id"] for obs in fresh}
        except BaseException as exc:
            body_error = exc

        if conn is not None:
            try:
                # Finalize only ids that are actually visible in the ledger; drop
                # reservations for rows that never landed.  This same recovery rule handles
                # a process that died in a previous allocation attempt and left ``pending``
                # rows behind.
                cache: dict[str, list[dict] | None] = {}
                visible = _visible_observation_ids(repo, agent, cache)
                for obs in fresh:
                    claim = obs["observation_id"]
                    if visible is None:
                        # Do not turn an unverifiable owner into a false absence.  Leave the
                        # reservation pending; the next healthy allocator will finalize or
                        # reclaim it after re-reading the ledger.
                        continue
                    if claim in visible:
                        conn.execute(
                            "UPDATE observation_claims SET state='committed' WHERE agent=? AND claim_id=?",
                            (agent, claim),
                        )
                    else:
                        _delete_claim(conn, agent, claim)
                conn.commit()
            except sqlite3.Error:
                # Durable ledger rows still contain the ids.  A later healthy allocator can
                # rebuild the index from a stale/pending reservation or allocate again only
                # after verifying that no referenced ledger owner exists.
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass

        if body_error is not None:
            raise body_error
    finally:
        if conn is not None:
            conn.close()
        if lock is not None:
            lock.__exit__(None, None, None)
        if fh is not None:
            fh.close()


def claimed_observation_ids(agent: str) -> set[str]:
    """Currently indexed canonical ids whose referenced ledger allocation is still visible."""
    try:
        conn = _open_observation_db()
    except (OSError, sqlite3.Error):
        return set()
    cache: dict[str, list[dict] | None] = {}
    out: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT claim_id,repo FROM observation_claims WHERE agent=?", (agent,)
        ).fetchall()
        for claim, repo in rows:
            visible = _visible_observation_ids(repo, agent, cache)
            if visible is None:
                continue
            if claim in visible:
                out.add(claim)
            else:
                _delete_claim(conn, agent, claim)
        conn.commit()
        return out
    finally:
        conn.close()
