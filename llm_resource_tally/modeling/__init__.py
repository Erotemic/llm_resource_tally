# SPDX-License-Identifier: Apache-2.0
"""llm_resource_tally.modeling — the OPTIONAL modeling layer.

The core tool (measure -> ledger) is deliberately minimal: the `curl | sh` bootstrap vendors it
WITHOUT this subpackage so the offline footprint stays tiny and dependency-free. Modeling —
turning measured tokens into energy/carbon/USD via a versioned **assumption pack** — is a
separately-shipped unit:

  * `pip install llm_resource_tally`  includes it (pip is heavy anyway); or
  * `<rt> install --modeling`         vendors just this subpackage into an existing repo; or
  * `RT_MODELING=1 … | sh`            includes it at curl time.

When it is absent, `estimate` prints a one-line install hint instead of failing obscurely
(see `cli.py`). Everything modeling-specific lives here:

  estimate.py            energy/carbon/USD over an assumption pack; source/adapter/provenance
  assumptions/*.json     the vendored packs (baseline + per-region grid-codecarbon)
"""
from .estimate import (cmd_estimate, estimate, grid_at, grid_for, load_pack,  # noqa: F401
                       normalize_provenance, region_intensity, register_adapter, resolve_source)

__all__ = ["cmd_estimate", "estimate", "load_pack", "resolve_source", "register_adapter",
           "normalize_provenance", "grid_at", "grid_for", "region_intensity"]
