# SPDX-License-Identifier: Apache-2.0
"""`estimate` — the modeling pass the ledger was built to feed.

The ledger stores only MEASUREMENTS (tokens, model, timestamps). Turning those into
inference-energy, carbon, and dollars requires assumptions, and assumptions change — so they
live OUTSIDE the ledger, in a versioned **assumption pack** (a JSON file, data not code). This
command reads `ledger x pack` and prints derived energy_kwh / carbon_gco2e / cost_usd, tagged
with the pack version so every number is traceable to (measurement, assumption). Nothing here
is written back into the ledger; publishing a better pack never touches recorded data.

The built-in pack ships ILLUSTRATIVE placeholders so the command has a shape out of the box.
Supply your own with `--pack` for numbers you can stand behind.
"""
from __future__ import annotations

import json
import os

from .ledger import read_ledger
from .rollup import compute_totals


def default_pack_path() -> str:
    return os.path.join(os.path.dirname(__file__), "assumptions", "default-pack.json")


def load_pack(path: str | None = None) -> dict:
    with open(path or default_pack_path(), encoding="utf-8") as fh:
        return json.load(fh)


def _merge(defaults: dict, override: dict) -> dict:
    """Shallow-merge a model's overrides onto the pack defaults (nested dicts merged one deep)."""
    out = dict(defaults)
    for k, v in override.items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def model_assumptions(pack: dict, model: str) -> dict:
    return _merge(pack.get("defaults", {}), pack.get("models", {}).get(model, {}))


def estimate(rows: list[dict], pack: dict) -> dict:
    """Derive energy/carbon/cost per model and in total from the ledger's measured tokens."""
    totals = compute_totals(rows)
    grid = pack.get("grid", {}).get("gco2e_per_kwh", 0) or 0
    pue = pack.get("pue", 1.0) or 1.0
    per_model: dict[str, dict] = {}
    agg = {"energy_kwh": 0.0, "carbon_gco2e": 0.0, "cost_usd": 0.0}
    for model, tok in totals["by_model"].items():
        a = model_assumptions(pack, model)
        billable_in = tok.get("input", 0) + tok.get("cache_write", 0) + tok.get("cache_read", 0)
        wh = pue * (tok.get("output", 0) * a.get("wh_per_output_token", 0)
                    + billable_in * a.get("wh_per_input_token", 0))
        kwh = wh / 1000.0
        price = a.get("pricing_usd_per_mtok", {})
        usd = sum(tok.get(k, 0) / 1e6 * price.get(k, 0)
                  for k in ("input", "cache_write", "cache_read", "output"))
        co2 = kwh * grid
        per_model[model] = {"energy_kwh": round(kwh, 6), "carbon_gco2e": round(co2, 3),
                            "cost_usd": round(usd, 4)}
        agg["energy_kwh"] += kwh
        agg["carbon_gco2e"] += co2
        agg["cost_usd"] += usd
    return {
        "pack_version": pack.get("pack_version"),
        "pack_description": pack.get("description"),
        "through": totals["through"],
        "pue": pue,
        "grid_gco2e_per_kwh": grid,
        "totals": {"energy_kwh": round(agg["energy_kwh"], 6),
                   "carbon_gco2e": round(agg["carbon_gco2e"], 3),
                   "cost_usd": round(agg["cost_usd"], 4)},
        "by_model": per_model,
        "provenance": "each figure = measured tokens (ledger) x assumption pack "
                      f"'{pack.get('pack_version')}'; not stored in the ledger.",
    }


def cmd_estimate(args) -> None:
    try:
        pack = load_pack(args.pack)
    except (OSError, json.JSONDecodeError) as e:
        import sys
        sys.exit(f"error: could not read assumption pack: {e}")
    result = estimate(read_ledger(), pack)
    if args.fmt == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    t = result["totals"]
    print(f"llm_resource_tally estimate — pack {result['pack_version']}")
    if "illustrative" in (result["pack_version"] or "").lower():
        print("  ⚠ ILLUSTRATIVE pack — placeholder rates; pass --pack with your own for real numbers")
    print(f"  ledger through : {result['through']}")
    print(f"  PUE {result['pue']} · grid {result['grid_gco2e_per_kwh']} gCO₂e/kWh")
    print(f"  energy         : {t['energy_kwh']:.4f} kWh")
    print(f"  carbon         : {t['carbon_gco2e']:.1f} gCO₂e")
    print(f"  cost           : ${t['cost_usd']:.2f}")
    if result["by_model"]:
        print("  by model:")
        for m, v in result["by_model"].items():
            print(f"    {m:<20} {v['energy_kwh']:.4f} kWh  {v['carbon_gco2e']:.1f} gCO₂e  "
                  f"${v['cost_usd']:.2f}")
    print(f"  {result['provenance']}")
