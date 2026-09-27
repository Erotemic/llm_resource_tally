# SPDX-License-Identifier: Apache-2.0
"""Agent-backend interface.

Everything backend-specific — where session transcripts live, how to parse tokens out of
them, whether the agent has a compaction concept — lives behind a Backend. The core
(record/reconcile/rollup, the ledger, git wiring) is backend-agnostic and works with the
NORMALIZED shapes below, so adding Codex or another agent is a new Backend, nothing else.

A `parse_turns` result is a list of turns, each:
    {"id": str, "ts": iso8601, "type": str, "model": str,
     "usage": {"input_tokens", "cache_creation_input_tokens",
               "cache_read_input_tokens", "output_tokens"},
     "web_search": int, "web_fetch": int}

A `parse_compaction_events` result is a list (empty if the backend has no compaction):
    {"boundary_ts": iso8601, "model": str,
     "peak_context_tokens": int, "summary_chars": int}

A turn or compaction event may additionally carry a canonical `observation_id` and optional
`observation_aliases`: stable identities for the physical usage observation. Backends whose source
records can be copied into multiple session files (Pi forks/clones) provide them. The canonical
id is persisted in the owning ledger row (`observation_ids`); aliases keep older fingerprint
versions compatible. A workstation-local SQLite index coordinates same-machine allocation, but
the ledger remains authoritative: an index entry suppresses a copy only while its referenced
ledger allocation is still visible. Records without an `observation_id` are allocated by session
watermarks as before.
"""

from __future__ import annotations


class Backend:
    #: value stored in each row's `agent` field
    name = "?"
    #: True when every billable observation carries a stable observation_id. Such backends do not
    #: need the older cross-repository timestamp floor; per-observation allocation is stronger.
    stable_observation_ids = False

    def default_projects_dir(self) -> str:
        """Where this backend's session logs live by default."""
        raise NotImplementedError

    def find_transcript(self, projects_dir: str, session: str | None, strict: bool = False) -> str | None:
        """The current (or named) session transcript for this repo. With ``strict=True``,
        match ONLY this repo (or the named session) and return ``None`` if there is no
        confident match — never a global "most recent session" fallback and never exit. The
        passive multi-backend hook uses strict mode so an unrelated session (e.g. a Codex
        session in another repo) is never mis-attributed to this commit."""
        raise NotImplementedError

    def session_transcripts(self, projects_dir: str) -> list[str]:
        """All session transcripts attributable to this repo (for `reconcile` to sweep)."""
        raise NotImplementedError

    def session_id(self, transcript: str) -> str | None:
        """The session's own identifier when the backend has one (e.g. Pi's header uuid,
        which the filename stem only carries with a timestamp prefix). ``None`` lets the
        caller fall back to the transcript's filename stem."""
        return None

    def parse_turns(self, transcript: str) -> list[dict]:
        raise NotImplementedError

    def parse_compaction_events(self, transcript: str) -> list[dict]:
        return []  # backends without a compaction concept override nothing
