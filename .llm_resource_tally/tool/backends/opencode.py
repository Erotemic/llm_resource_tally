# SPDX-License-Identifier: Apache-2.0
"""opencode backend: read the opencode SQLite store (`~/.local/share/opencode/opencode.db`).

opencode records each assistant message in a `message` table with a JSON `data` blob carrying
`tokens {input, output, reasoning, cache:{write, read}}`, `modelID`, and `time` (epoch ms); the
`session` table maps a session id to its working `directory`. stdlib `sqlite3` reads it
read-only. Since the core is file-oriented, we address a session by a synthetic transcript
token `<data-dir>/<session-id>.opencode` whose basename stem is the session id — so
record/reconcile work unchanged, and `parse_turns` recovers the data dir + session id from it.

Not on by default (querying the DB on every commit is wasted for non-users): opt in with
`install --backend opencode`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from .base import Backend
from ..gitutil import superproject_root
from ..schema import TOKEN_KEYS

_SUFFIX = ".opencode"


def default_data_dir() -> str:
    """`OPENCODE_DATA_DIR` overrides outright; else `$XDG_DATA_HOME/opencode` or
    `~/.local/share/opencode` (where the `opencode.db` store lives)."""
    env = os.environ.get("OPENCODE_DATA_DIR")
    if env:
        return os.path.expanduser(env)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = os.path.expanduser(xdg) if xdg else os.path.expanduser("~/.local/share")
    return os.path.join(base, "opencode")


def _db_path(data_dir: str) -> str:
    return os.path.join(data_dir, "opencode.db")


def _connect(db: str):
    if not os.path.exists(db):
        return None
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _ms_to_iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _sessions_for_repo(db: str, root: str) -> list[str]:
    """Session ids whose working directory is inside `root`, most-recently-updated first."""
    con = _connect(db)
    if con is None:
        return []
    try:
        try:
            rows = con.execute("select id, directory from session "
                               "order by time_updated desc").fetchall()
        except sqlite3.Error:
            rows = con.execute("select id, directory from session").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    root_r = os.path.realpath(root)
    out = []
    for sid, directory in rows:
        if not directory:
            continue
        d = os.path.realpath(os.path.expanduser(directory))
        if d == root_r or d.startswith(root_r + os.sep):
            out.append(sid)
    return out


class OpencodeBackend(Backend):
    name = "opencode"

    def default_projects_dir(self) -> str:
        return default_data_dir()

    def _token(self, data_dir: str, sid: str) -> str:
        return os.path.join(data_dir, sid + _SUFFIX)

    def find_transcript(self, projects_dir: str, session: str | None,
                        strict: bool = False) -> str | None:
        db = _db_path(projects_dir)
        if session:
            con = _connect(db)
            found = False
            if con is not None:
                try:
                    found = con.execute("select 1 from session where id=?",
                                        (session,)).fetchone() is not None
                except sqlite3.Error:
                    found = False
                finally:
                    con.close()
            if found:
                return self._token(projects_dir, session)
            if strict:
                return None
            sys.exit(f"error: no opencode session {session} in {db}")
        sids = _sessions_for_repo(db, superproject_root())
        if sids:
            return self._token(projects_dir, sids[0])
        if strict:
            return None
        sys.exit(f"error: no opencode sessions for this repo in {db}")

    def session_transcripts(self, projects_dir: str) -> list[str]:
        sids = _sessions_for_repo(_db_path(projects_dir), superproject_root())
        return [self._token(projects_dir, sid) for sid in sids]

    def parse_turns(self, transcript: str) -> list[dict]:
        data_dir = os.path.dirname(transcript)
        sid = os.path.splitext(os.path.basename(transcript))[0]
        con = _connect(_db_path(data_dir))
        if con is None:
            return []
        by_id: dict[str, dict] = {}
        try:
            cur = con.execute("select id, time_created, data from message "
                              "where session_id=?", (sid,))
            for mid, tc, data in cur:
                try:
                    d = json.loads(data)
                except (TypeError, json.JSONDecodeError):
                    continue
                if d.get("role") != "assistant":
                    continue
                tk = d.get("tokens") or {}
                if not tk:
                    continue
                cache = tk.get("cache") or {}
                usage = {
                    "input_tokens": int(tk.get("input", 0) or 0),
                    "cache_creation_input_tokens": int(cache.get("write", 0) or 0),
                    "cache_read_input_tokens": int(cache.get("read", 0) or 0),
                    # reasoning tokens are billed output-side
                    "output_tokens": int(tk.get("output", 0) or 0) + int(tk.get("reasoning", 0) or 0),
                }
                if not any(usage.values()):
                    continue
                t = d.get("time") or {}
                ts = _ms_to_iso(t.get("completed") or t.get("created") or tc)
                by_id[mid] = {"id": mid, "ts": ts, "type": "assistant",
                              "model": d.get("modelID") or d.get("model") or "?",
                              "usage": {k: usage.get(k, 0) for k in TOKEN_KEYS},
                              "web_search": 0, "web_fetch": 0}
        except sqlite3.Error:
            return []
        finally:
            con.close()
        turns = [t for t in by_id.values() if t["ts"]]
        turns.sort(key=lambda t: t["ts"])
        return turns
