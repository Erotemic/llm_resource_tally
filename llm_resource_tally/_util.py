# SPDX-License-Identifier: Apache-2.0
"""Small time helpers. Never compare transcript ('...Z') and git ('+00:00') ISO strings
lexicographically — the 'Z' vs '+00:00' suffix and fractional seconds both break order
across the two sources; parse to aware datetimes with to_dt first."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_stamp() -> str:
    """Compact UTC stamp (YYYYMMDDTHHMMSS) for lexically-sortable shard archive names."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def to_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def compare_timestamps(left: str | None, right: str | None) -> int:
    """Compare ISO timestamps by instant, with a deterministic fallback for legacy junk.

    Valid writer output always takes the datetime path.  The string fallback is intentionally
    limited to malformed legacy/advisory state so one bad local claim cannot crash recording.
    """
    if left == right:
        return 0
    if not left:
        return -1
    if not right:
        return 1
    try:
        ldt, rdt = to_dt(left), to_dt(right)
        return (ldt > rdt) - (ldt < rdt)
    except (TypeError, ValueError):
        return (left > right) - (left < right)


def span_seconds(lo: str | None, hi: str | None) -> float | None:
    dlo, dhi = to_dt(lo), to_dt(hi)
    if not dlo or not dhi:
        return None
    return round((dhi - dlo).total_seconds(), 1)


@contextmanager
def exclusive_file_lock(fh):
    """Best-effort exclusive advisory lock for POSIX; a no-op where ``fcntl`` is unavailable."""
    try:
        import fcntl

        fcntl.flock(fh, fcntl.LOCK_EX)
    except (ImportError, OSError):
        fcntl = None
    try:
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
