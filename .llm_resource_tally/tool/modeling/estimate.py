# SPDX-License-Identifier: Apache-2.0
"""`estimate` — the modeling pass the ledger was built to feed.

The ledger stores only MEASUREMENTS (tokens, model, timestamps). Turning those into
inference-energy, carbon, and dollars requires assumptions, and assumptions change — so they
live OUTSIDE the ledger, in a versioned **assumption pack** (a JSON file, data not code). This
command reads `ledger x pack` and prints derived energy_kwh / carbon_gco2e / cost_usd, tagged
with the pack version so every number is traceable to (measurement, assumption). Nothing here
is written back into the ledger; publishing a better pack never touches recorded data.

This whole module is the **modeling** subpackage — the part the minimal `curl | sh` install
deliberately leaves out (see the package docstring). The core measurement tool works without
it; `estimate` is the one command that needs it.

The built-in pack ships a *cited baseline* (order-of-magnitude grid + per-token energy, a
list-price pricing placeholder). Supply your own with `--pack` for numbers you can stand
behind, or use the shipped per-region grid pack (`grid-codecarbon.json`) with `--region`.
"""
from __future__ import annotations

import json
import os

from ..ledger import read_ledger
from ..schema import COMPACTION_KIND

_KINDS = ("input", "cache_write", "cache_read", "output")

# CodeCarbon's world-average grid-intensity fallback (gCO2e/kWh), used when a per-region pack is
# consulted without a `--region` (a country) selected. See the `codecarbon-energy-mix` adapter.
CODECARBON_WORLD_AVG = 475


def assumptions_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "assumptions")


def default_pack_path() -> str:
    return os.path.join(assumptions_dir(), "default-pack.json")


# --- estimation sources (adapters) ---------------------------------------------------------
# A *source* is where an assumption pack comes from, expressed as {"adapter": name, "ref": ...}.
# An *adapter* turns a `ref` into a raw pack dict. The vendored default pack loads through this
# SAME mechanism (a `json-file` source), so we never special-case it — and adding a new source
# later (a live carbon-intensity API, a regional dataset, a codecarbon export) is just a new
# adapter + a ref, with zero change to the estimator. Provenance (below) records, per number,
# where it came from — the discipline that keeps a modeled figure honest.
def _load_json_file(ref: str) -> dict:
    with open(ref, encoding="utf-8") as fh:
        return json.load(fh)


def _load_codecarbon_energy_mix(ref: str) -> dict:
    """Adapter: turn CodeCarbon's `global_energy_mix.json` (per-country carbon intensity, MIT)
    into a full assumption pack. The regional grid table replaces the baseline pack's scalar
    grid; energy/pricing/pue are inherited from the vendored default pack so only the grid
    dimension changes. This is the reference *second* adapter — proof that "point at a new
    source" is exactly `register_adapter` + a `ref`, no estimator change. `dev/build_grid_pack.py`
    calls this same adapter to freeze the committed `grid-codecarbon.json` snapshot."""
    raw = _load_json_file(ref)
    by_region = {str(code): round(float(e["carbon_intensity"]), 3)
                 for code, e in raw.items()
                 if isinstance(e, dict) and isinstance(e.get("carbon_intensity"), (int, float))}
    if not by_region:
        raise ValueError(f"{ref!r} has no per-region carbon_intensity — not a CodeCarbon "
                         "global-energy-mix file?")
    pack = _load_json_file(default_pack_path())
    pack["pack_version"] = "grid-codecarbon"
    pack["description"] = (
        f"Per-region grid carbon intensity from CodeCarbon's global energy mix "
        f"({len(by_region)} countries); energy/pricing/PUE inherited from the baseline pack. "
        "Pass `--region <ISO3>` (e.g. USA, FRA, NOR) to fix the grid to a country; without a "
        "region, CodeCarbon's world-average fallback applies.")
    pack["grid"] = {
        "gco2e_per_kwh": CODECARBON_WORLD_AVG,
        "by_region": by_region,
        "source": "CodeCarbon global_energy_mix.json (per-country carbon_intensity, gCO2e/kWh)",
    }
    prov = [p for p in normalize_provenance(pack) if p.get("applies_to") != "grid"]
    prov.insert(0, {
        "applies_to": "grid",
        "source": f"CodeCarbon global energy mix — {len(by_region)} countries",
        "adapter": "codecarbon-energy-mix",
        "ref": ref,
        "citation": "https://github.com/mlco2/codecarbon",
        "license": "MIT",
        "note": "Per-country carbon_intensity (gCO2e/kWh). `--region <ISO3>` fixes the grid to a "
                "country; default is CodeCarbon's world-average fallback (475). Datacenter region "
                "is itself an assumption — the ledger records no location.",
    })
    pack["provenance"] = prov
    return pack


ADAPTERS = {
    "json-file": _load_json_file,
    "codecarbon-energy-mix": _load_codecarbon_energy_mix,
}


def register_adapter(name: str, fn) -> None:
    """Register an adapter `name -> (ref: str) -> pack dict`. This is the whole extension point
    for a new estimation source (e.g. a future `http-json` fetch): register it here, then point
    a source's `ref` at the new location. Nothing else in the estimator changes."""
    ADAPTERS[name] = fn


def resolve_source(spec) -> dict:
    """Resolve a source spec into a pack dict. `spec` is `None` (the vendored default), a path
    string (shorthand for a `json-file` source), or a dict `{"adapter": name, "ref": ...,
    "provenance": [...]}`. A spec-level `provenance` seeds the pack's if the pack lacks its own,
    so an adapter that produces raw numbers can still declare where they came from."""
    if spec is None:
        spec = {"adapter": "json-file", "ref": default_pack_path()}
    elif isinstance(spec, str):
        spec = {"adapter": "json-file", "ref": spec}
    adapter = spec.get("adapter", "json-file")
    fn = ADAPTERS.get(adapter)
    if fn is None:
        raise ValueError(f"unknown estimation adapter {adapter!r}; "
                         f"known: {', '.join(sorted(ADAPTERS))}")
    pack = fn(spec["ref"])
    if not isinstance(pack, dict):
        raise ValueError(f"adapter {adapter!r} did not produce a pack object")
    if "provenance" not in pack and spec.get("provenance"):
        pack["provenance"] = spec["provenance"]
    return pack


def load_pack(spec=None) -> dict:
    """Load an assumption pack from a source (default: the vendored pack). Back-compatible with
    a plain path; see `resolve_source` for the full source-spec form."""
    return resolve_source(spec)


# --- provenance protocol -------------------------------------------------------------------
# Every number an estimate produces should be traceable to a stated origin. A pack carries a
# `provenance` list; each entry documents one contributing source and (optionally) which part
# of the model it backs (`applies_to`: grid | energy | pricing | pue | all). This is the
# uniform record whether the numbers are vendored, user-supplied, or later fetched.
PROVENANCE_FIELDS = ("applies_to", "source", "adapter", "ref", "citation", "license",
                     "retrieved", "note")


def normalize_provenance(pack: dict) -> list[dict]:
    p = pack.get("provenance")
    if isinstance(p, dict):
        p = [p]
    if not isinstance(p, list):
        return []
    return [{k: e[k] for k in PROVENANCE_FIELDS if isinstance(e, dict) and e.get(k) is not None}
            for e in p if isinstance(e, dict)]


def _merge(defaults: dict, override: dict) -> dict:
    """Shallow-merge a model's overrides onto the pack defaults (nested dicts merged one deep)."""
    out = dict(defaults)
    for k, v in override.items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def model_assumptions(pack: dict, model: str) -> dict:
    return _merge(pack.get("defaults", {}), pack.get("models", {}).get(model, {}))


def _grid_series(pack: dict) -> list | None:
    """A pack may pin grid intensity over time as `grid.intensity_by_date`: a list of
    `{"from": "YYYY-MM-DD", "gco2e_per_kwh": N}`. Returns it sorted by date, or None."""
    series = pack.get("grid", {}).get("intensity_by_date")
    if isinstance(series, list) and series:
        pts = [(str(e.get("from", "")), float(e.get("gco2e_per_kwh", 0)))
               for e in series if isinstance(e, dict)]
        return sorted(pts)
    return None


def region_intensity(pack: dict, region: str) -> float:
    """Grid carbon intensity for a named region from `grid.by_region`. A region is a *fixed
    location* assumption (the datacenter), so it overrides any time series — the ledger records
    no location, so the user asserts it. Unknown region is a clear error, never a silent
    wrong number."""
    by = pack.get("grid", {}).get("by_region")
    if not isinstance(by, dict) or region not in by:
        have = sorted(by) if isinstance(by, dict) else []
        sample = ", ".join(have[:10]) + ("…" if len(have) > 10 else "")
        raise ValueError(f"region {region!r} not in this pack's grid.by_region"
                         + (f" (available e.g.: {sample})" if have else
                            " (this pack has no per-region grid; use grid-codecarbon.json)"))
    return float(by[region])


def grid_at(pack: dict, ts: str | None) -> float:
    """Grid carbon intensity (gCO₂e/kWh) in effect at `ts`. If the pack pins a time series we
    pick the latest entry on or before the date (earliest entry for dates before it), which is
    what lets each commit's carbon reflect the grid at the moment it was made — the whole point
    of storing per-commit timestamps. Otherwise a scalar `grid.gco2e_per_kwh` applies."""
    series = _grid_series(pack)
    if not series:
        return pack.get("grid", {}).get("gco2e_per_kwh", 0) or 0
    date = (ts or "")[:10]
    val = series[0][1]
    for frm, v in series:
        if frm <= date:
            val = v
        else:
            break
    return val


def grid_for(pack: dict, ts: str | None, region: str | None) -> float:
    """Grid intensity for a row: a selected `region` wins (fixed location), else the time
    series, else the scalar."""
    return region_intensity(pack, region) if region else grid_at(pack, ts)


def _row_ts(r: dict) -> str | None:
    """The timestamp that fixes this row's grid intensity: the commit's committer-date if it
    has one, else the end of its turn window, else when it was recorded."""
    return (r.get("commit_ts") or (r.get("turn_ts_range") or [None, None])[1]
            or r.get("recorded_at"))


def estimate(rows: list[dict], pack: dict, region: str | None = None) -> dict:
    """Derive energy/carbon/cost from the ledger's measured tokens. Computed PER ROW so each
    commit's carbon can use the grid intensity at its own timestamp (or a fixed `region`), then
    aggregated per model and in total."""
    pue = pack.get("pue", 1.0) or 1.0
    per_model: dict[str, dict] = {}
    agg = {"energy_kwh": 0.0, "carbon_gco2e": 0.0, "cost_usd": 0.0}
    through = ""
    time_keyed = region is None and _grid_series(pack) is not None
    for r in rows:
        rec = r.get("recorded_at") or ""
        if rec > through:
            through = rec
        if r.get("kind") == COMPACTION_KIND:
            continue
        grid = grid_for(pack, _row_ts(r), region)
        bm = r.get("by_model") or {(r.get("models") or ["unknown"])[0]: r.get("tokens", {})}
        for model, tok in bm.items():
            a = model_assumptions(pack, model)
            billable_in = sum(tok.get(k, 0) for k in ("input", "cache_write", "cache_read"))
            wh = pue * (tok.get("output", 0) * a.get("wh_per_output_token", 0)
                        + billable_in * a.get("wh_per_input_token", 0))
            kwh = wh / 1000.0
            price = a.get("pricing_usd_per_mtok", {})
            usd = sum(tok.get(k, 0) / 1e6 * price.get(k, 0) for k in _KINDS)
            co2 = kwh * grid
            pm = per_model.setdefault(model, {"energy_kwh": 0.0, "carbon_gco2e": 0.0,
                                              "cost_usd": 0.0})
            pm["energy_kwh"] += kwh
            pm["carbon_gco2e"] += co2
            pm["cost_usd"] += usd
            agg["energy_kwh"] += kwh
            agg["carbon_gco2e"] += co2
            agg["cost_usd"] += usd

    def _round(d: dict) -> dict:
        return {"energy_kwh": round(d["energy_kwh"], 6), "carbon_gco2e": round(d["carbon_gco2e"], 3),
                "cost_usd": round(d["cost_usd"], 4)}

    if region:
        grid_model, grid_scalar = f"region {region}", region_intensity(pack, region)
    elif time_keyed:
        grid_model, grid_scalar = "time-series (per commit timestamp)", None
    else:
        grid_model, grid_scalar = "scalar", (pack.get("grid", {}).get("gco2e_per_kwh", 0) or 0)

    return {
        "pack_version": pack.get("pack_version"),
        "pack_description": pack.get("description"),
        "disclaimer": pack.get("disclaimer"),
        "through": through or None,
        "pue": pue,
        "region": region,
        "grid_model": grid_model,
        "grid_gco2e_per_kwh": grid_scalar,
        "totals": _round(agg),
        "by_model": {m: _round(v) for m, v in per_model.items()},
        "provenance": normalize_provenance(pack),
        "method": "each figure = measured tokens (ledger) x assumption pack "
                  f"'{pack.get('pack_version')}'; not stored in the ledger.",
    }


def cmd_estimate(args) -> None:
    try:
        pack = load_pack(args.pack)
        result = estimate(read_ledger(), pack, region=getattr(args, "region", None))
    except (OSError, json.JSONDecodeError) as e:
        import sys
        sys.exit(f"error: could not read assumption pack: {e}")
    except ValueError as e:
        import sys
        sys.exit(f"error: {e}")
    if args.fmt == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    t = result["totals"]
    print(f"llm_resource_tally estimate — pack {result['pack_version']}")
    if result.get("disclaimer"):
        print(f"  ⚠ {result['disclaimer']}")
    print(f"  ledger through : {result['through']}")
    if result["grid_gco2e_per_kwh"] is not None:
        label = f"region {result['region']}" if result.get("region") else "grid"
        grid = f"{label} {result['grid_gco2e_per_kwh']} gCO₂e/kWh"
    else:
        grid = result["grid_model"]
    print(f"  PUE {result['pue']} · {grid}")
    print(f"  energy         : {t['energy_kwh']:.4f} kWh")
    print(f"  carbon         : {t['carbon_gco2e']:.1f} gCO₂e")
    print(f"  cost           : ${t['cost_usd']:.2f}")
    if result["by_model"]:
        print("  by model:")
        for m, v in result["by_model"].items():
            print(f"    {m:<20} {v['energy_kwh']:.4f} kWh  {v['carbon_gco2e']:.1f} gCO₂e  "
                  f"${v['cost_usd']:.2f}")
    if result["provenance"]:
        print("  provenance:")
        for p in result["provenance"]:
            tag = f"{p['applies_to']}: " if p.get("applies_to") else ""
            cite = f" <{p['citation']}>" if p.get("citation") else ""
            lic = f" [{p['license']}]" if p.get("license") else ""
            print(f"    - {tag}{p.get('source', '?')}{lic}{cite}")
            if p.get("note"):
                print(f"        {p['note']}")
    print(f"  {result['method']}")
