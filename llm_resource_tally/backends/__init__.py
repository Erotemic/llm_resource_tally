# SPDX-License-Identifier: Apache-2.0
"""Backend registry."""

from __future__ import annotations

import sys

from .base import Backend
from .claude import ClaudeBackend
from .codex import CodexBackend
from .opencode import OpencodeBackend

_BACKENDS = {
    "claude": ClaudeBackend,
    "claude-code": ClaudeBackend,
    "codex": CodexBackend,
    "opencode": OpencodeBackend,
}

DEFAULT_BACKEND = "claude"


def backend_names() -> list[str]:
    return sorted(set(_BACKENDS))


def canonical_backend_name(name: str) -> str:
    """Normalize aliases to the first registry selector for the same backend implementation."""
    cls = _BACKENDS.get(name)
    if cls is None:
        raise ValueError(f"unknown backend {name!r}; known: {', '.join(backend_names())}")
    return next(key for key, candidate in _BACKENDS.items() if candidate is cls)


def get_backend(name: str | None = None) -> Backend:
    cls = _BACKENDS.get(name or DEFAULT_BACKEND)
    if cls is None:
        sys.exit(f"error: unknown backend {name!r}; known: {', '.join(backend_names())}")
    return cls()
