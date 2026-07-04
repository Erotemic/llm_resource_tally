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
from .schema import COMPACTION_KIND

_KINDS = ("input", "cache_write", "cache_read", "output")


def default_pack_path() -> str:
    return os.path.join(os.path.dirname(__file__), "assumptions", "default-pack.json")


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


ADAPTERS = {"json-file": _load_json_file}


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


def _row_ts(r: dict) -> str | None:
    """The timestamp that fixes this row's grid intensity: the commit's committer-date if it
    has one, else the end of its turn window, else when it was recorded."""
    return (r.get("commit_ts") or (r.get("turn_ts_range") or [None, None])[1]
            or r.get("recorded_at"))


def estimate(rows: list[dict], pack: dict) -> dict:
    """Derive energy/carbon/cost from the ledger's measured tokens. Computed PER ROW so each
    commit's carbon can use the grid intensity at its own timestamp, then aggregated per model
    and in total."""
    pue = pack.get("pue", 1.0) or 1.0
    per_model: dict[str, dict] = {}
    agg = {"energy_kwh": 0.0, "carbon_gco2e": 0.0, "cost_usd": 0.0}
    through = ""
    time_keyed = _grid_series(pack) is not None
    for r in rows:
        rec = r.get("recorded_at") or ""
        if rec > through:
            through = rec
        if r.get("kind") == COMPACTION_KIND:
            continue
        grid = grid_at(pack, _row_ts(r))
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

    return {
        "pack_version": pack.get("pack_version"),
        "pack_description": pack.get("description"),
        "disclaimer": pack.get("disclaimer"),
        "through": through or None,
        "pue": pue,
        "grid_model": "time-series (per commit timestamp)" if time_keyed else "scalar",
        "grid_gco2e_per_kwh": None if time_keyed else (pack.get("grid", {}).get("gco2e_per_kwh", 0) or 0),
        "totals": _round(agg),
        "by_model": {m: _round(v) for m, v in per_model.items()},
        "provenance": normalize_provenance(pack),
        "method": "each figure = measured tokens (ledger) x assumption pack "
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
    if result.get("disclaimer"):
        print(f"  ⚠ {result['disclaimer']}")
    print(f"  ledger through : {result['through']}")
    grid = (f"{result['grid_gco2e_per_kwh']} gCO₂e/kWh" if result["grid_gco2e_per_kwh"] is not None
            else result["grid_model"])
    print(f"  PUE {result['pue']} · grid {grid}")
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
