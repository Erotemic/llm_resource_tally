# SPDX-License-Identifier: Apache-2.0
"""llm_resource_tally — measured, per-commit LLM resource accounting for a git repo.

Stdlib-only. Three layers — MEASURE (read transcripts -> ledger), WIRE (install hooks), and
REPORT (post-hoc passes over the ledger). Package layout:
  cli.py            argument parsing / dispatch
  record.py         record / reconcile (backend-agnostic)          [measure]
  ledger.py         rolling JSONL shards, read/dedup/append, aggregate  [measure]
  schema.py         compact on-disk row codec (<-> rich in-memory rows) [measure]
  claims.py         per-user cross-repo double-count guard          [measure]
  backends/         agent-specific transcript readers               [measure]
  install.py        install / uninstall / update / vendor + git-hook & AGENTS wiring  [wire]
  doctor.py         wiring/health/retention diagnosis               [wire]
  config.py         per-repo settings.json (registered backends)    [wire]
  gitutil.py        git helpers (repo_root anchors the ledger)
  rollup.py         rollup / show (measured post-hoc passes)         [report]
  report.py         human-readable grouped views                    [report]
  estimate.py       energy/carbon/USD modeling over an assumption pack  [report]
"""
from .version import tool_version                       # noqa: F401
from .backends import get_backend                       # noqa: F401
from .backends.claude import munged_project_dir         # noqa: F401
from .ledger import read_ledger                         # noqa: F401
from .rollup import compute_totals                      # noqa: F401
from .estimate import estimate, load_pack               # noqa: F401
from .cli import main                                   # noqa: F401

__all__ = ["main", "tool_version", "get_backend", "munged_project_dir", "read_ledger",
           "compute_totals", "estimate", "load_pack"]
