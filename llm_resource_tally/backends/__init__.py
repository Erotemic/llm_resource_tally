# SPDX-License-Identifier: Apache-2.0
"""Backend registry. `claude` is the only backend today; register others here."""
from __future__ import annotations

import sys

from .base import Backend
from .claude import ClaudeBackend

_BACKENDS = {
    "claude": ClaudeBackend,
    "claude-code": ClaudeBackend,
    # "codex": CodexBackend,   # <- add here
}

DEFAULT_BACKEND = "claude"


def backend_names() -> list[str]:
    return sorted(set(_BACKENDS))


def get_backend(name: str | None = None) -> Backend:
    cls = _BACKENDS.get(name or DEFAULT_BACKEND)
    if cls is None:
        sys.exit(f"error: unknown backend {name!r}; known: {', '.join(backend_names())}")
    return cls()
