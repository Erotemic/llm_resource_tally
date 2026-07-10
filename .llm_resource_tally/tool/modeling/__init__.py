# SPDX-License-Identifier: Apache-2.0
"""Optional post-hoc modeling layer.

The core records measurements only. This subpackage supplies versioned assumption packs,
source adapters, interval scenarios, regional CodeCarbon grid data, and optional typed
mitigation pricing.
"""
from .estimate import (cmd_estimate, estimate, grid_at, grid_for, load_pack,
                       normalize_provenance, region_intensity, register_adapter,
                       resolve_source)
from .interval import Interval
from .mitigation import load_mitigation, mitigation_report

__all__ = ["cmd_estimate", "estimate", "load_pack", "resolve_source", "register_adapter",
           "normalize_provenance", "grid_at", "grid_for", "region_intensity", "Interval",
           "load_mitigation", "mitigation_report"]
