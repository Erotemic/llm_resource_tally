# SPDX-License-Identifier: Apache-2.0
"""Optional typed carbon-mitigation price scenarios.

Gross modeled emissions are never changed. This module only estimates the expenditure required
to purchase nominal credited tonnes, and—when a project-specific effectiveness interval is
provided—the adjusted number of credits needed to cover the modeled high footprint bound.
"""
from __future__ import annotations

import json
import os

from .interval import Interval


def default_mitigation_path() -> str:
    return os.path.join(os.path.dirname(__file__), "assumptions", "mitigation-scenarios.json")


def load_mitigation(spec=None) -> dict | None:
    if spec in (None, False):
        return None
    path = default_mitigation_path() if spec in (True, "builtin") else str(spec)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("mitigation scenario file must contain a JSON object")
    return data


def mitigation_report(carbon_gco2e: Interval, cfg: dict | None) -> dict | None:
    if not cfg:
        return None
    scenarios = cfg.get("price_scenarios")
    if not isinstance(scenarios, dict) or not scenarios:
        return None
    footprint = carbon_gco2e * 1e-6
    high_quantity = Interval.exact(footprint.high)
    priced = {}
    for name, spec in sorted(scenarios.items()):
        if not isinstance(spec, dict) or spec.get("usd_per_tco2e") is None:
            continue
        price = Interval.coerce(spec["usd_per_tco2e"])
        item = {
            "instrument": spec.get("instrument"),
            "description": spec.get("description"),
            "usd_per_tco2e": price.to_dict("USD/tCO2e"),
            "nominal_proportional_cost_usd": (footprint * price).to_dict("USD"),
            "nominal_cost_to_cover_modeled_high_usd": (high_quantity * price).to_dict("USD"),
        }
        effective = spec.get("effective_tco2e_per_credited_tco2e")
        if effective is not None:
            effective_i = Interval.coerce(effective)
            if effective_i.low <= 0:
                raise ValueError(f"mitigation scenario {name!r} effectiveness must be positive")
            quantity = high_quantity / effective_i
            item["effective_tco2e_per_credited_tco2e"] = effective_i.to_dict(
                "effective tCO2e / credited tCO2e")
            item["effectiveness_basis"] = spec.get("effectiveness_basis")
            item["adjusted_quantity_to_cover_modeled_high_tco2e"] = quantity.to_dict(
                "credited tCO2e")
            item["adjusted_cost_to_cover_modeled_high_usd"] = (quantity * price).to_dict("USD")
        for key in ("credit_category", "removal_pathway", "storage_medium", "durability",
                    "delivery", "uncertainty_profile", "claim_guidance", "provider", "source",
                    "price_checked_at", "notes"):
            if spec.get(key) is not None:
                item[key] = spec[key]
        priced[name] = item
    return {
        "basis": "gross modeled operational carbon footprint",
        "footprint_tco2e": footprint.to_dict("tCO2e"),
        "quantity_to_cover_modeled_high_tco2e": round(footprint.high, 12),
        "price_scenarios": priced,
        "status": cfg.get("status"),
        "interpretation": (
            "These are optional mitigation-expenditure scenarios. Avoided emissions, reversible "
            "biological removal, biochar removal, and geological removal are not interchangeable. "
            "Purchasing or retiring a credit does not alter the reported gross footprint. Broad "
            "category labels do not justify an effectiveness discount; any effectiveness interval "
            "must be project-specific and externally supported."
        ),
    }
