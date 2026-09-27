# SPDX-License-Identifier: Apache-2.0
"""Measured LLM usage for Git repositories.

Core reads transcripts into a ledger, wiring installs recorders, and reporting derives views.
The optional modeling package is loaded only when requested so minimal installs remain usable.
See docs/development.md for the module map.
"""

from .version import tool_version  # noqa: F401
from .backends import get_backend  # noqa: F401
from .backends.claude import munged_project_dir  # noqa: F401
from .ledger import read_ledger  # noqa: F401
from .rollup import compute_totals  # noqa: F401
from .cli import main  # noqa: F401

__all__ = ["main", "tool_version", "get_backend", "munged_project_dir", "read_ledger", "compute_totals"]
