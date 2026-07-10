# SPDX-License-Identifier: Apache-2.0
"""`estimate` — post-hoc energy, carbon, and expenditure modeling.

Measured ledger rows remain immutable. Assumption packs are versioned data with provenance.
Scalar packs preserve the historical central estimate; packs may additionally use
``[low, central, high]`` values, producing transparent scenario bounds. The bundled
``generic-wide`` pack is deliberately coarse and dependency-free, while CodeCarbon-derived grid
packs remain the preferred built-in source for regional carbon intensity.
"""
from __future__ import annotations

import hashlib
import json
import os

from ..ledger import read_ledger
from ..schema import COMPACTION_KIND
from .interval import Interval, ZERO, contains_interval
from .mitigation import load_mitigation, mitigation_report

_KINDS = ("input", "cache_write", "cache_read", "output")
CODECARBON_WORLD_AVG = 475


def assumptions_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "assumptions")


def default_pack_path() -> str:
    return os.path.join(assumptions_dir(), "default-pack.json")


def generic_wide_pack_path() -> str:
    return os.path.join(assumptions_dir(), "generic-wide-pack.json")


def grid_codecarbon_pack_path() -> str:
    return os.path.join(assumptions_dir(), "grid-codecarbon.json")


BUILTIN_PACKS = {
    "default": default_pack_path,
    "baseline": default_pack_path,
    "generic-wide": generic_wide_pack_path,
    "grid-codecarbon": grid_codecarbon_pack_path,
}


def _load_json_file(ref: str) -> dict:
    with open(ref, encoding="utf-8") as fh:
        return json.load(fh)


def _load_codecarbon_energy_mix(ref: str) -> dict:
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
        "Pass `--region <ISO3>` to assert a serving country; without one, the world-average "
        "fallback applies.")
    pack["grid"] = {
        "gco2e_per_kwh": CODECARBON_WORLD_AVG,
        "by_region": by_region,
        "source": "CodeCarbon global_energy_mix.json (per-country carbon_intensity)",
    }
    prov = [p for p in normalize_provenance(pack) if p.get("applies_to") != "grid"]
    prov.insert(0, {
        "applies_to": "grid",
        "source": f"CodeCarbon global energy mix — {len(by_region)} countries",
        "adapter": "codecarbon-energy-mix",
        "ref": ref,
        "citation": "https://github.com/mlco2/codecarbon",
        "license": "MIT",
        "note": "The selected datacenter region remains an assumption; the ledger records no "
                "provider serving location.",
    })
    pack["provenance"] = prov
    return pack


ADAPTERS = {
    "json-file": _load_json_file,
    "codecarbon-energy-mix": _load_codecarbon_energy_mix,
}


def register_adapter(name: str, fn) -> None:
    ADAPTERS[name] = fn


def _builtin_path(name: str) -> str | None:
    key = name.removeprefix("builtin:")
    fn = BUILTIN_PACKS.get(key)
    return fn() if fn else None


def resolve_source(spec) -> dict:
    if spec is None:
        spec = {"adapter": "json-file", "ref": default_pack_path()}
    elif isinstance(spec, str):
        spec = {"adapter": "json-file", "ref": _builtin_path(spec) or spec}
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
    return resolve_source(spec)


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
    out = dict(defaults)
    for key, value in override.items():
        out[key] = ({**out[key], **value}
                    if isinstance(value, dict) and isinstance(out.get(key), dict) else value)
    return out


def model_assumptions(pack: dict, model: str) -> dict:
    return _merge(pack.get("defaults", {}), pack.get("models", {}).get(model, {}))


def _grid_series(pack: dict) -> list | None:
    series = pack.get("grid", {}).get("intensity_by_date")
    if isinstance(series, list) and series:
        return sorted((str(e.get("from", "")), e.get("gco2e_per_kwh", 0))
                      for e in series if isinstance(e, dict))
    return None


def _grid_at_value(pack: dict, ts: str | None):
    series = _grid_series(pack)
    if not series:
        return pack.get("grid", {}).get("gco2e_per_kwh", 0) or 0
    date = (ts or "")[:10]
    value = series[0][1]
    for start, candidate in series:
        if start <= date:
            value = candidate
        else:
            break
    return value


def _region_value(pack: dict, region: str):
    by = pack.get("grid", {}).get("by_region")
    if not isinstance(by, dict) or region not in by:
        have = sorted(by) if isinstance(by, dict) else []
        sample = ", ".join(have[:10]) + ("…" if len(have) > 10 else "")
        raise ValueError(f"region {region!r} not in this pack's grid.by_region"
                         + (f" (available e.g.: {sample})" if have else
                            " (this pack has no per-region grid; use grid-codecarbon)"))
    return by[region]


def region_intensity(pack: dict, region: str) -> float:
    return Interval.coerce(_region_value(pack, region)).central


def grid_at(pack: dict, ts: str | None) -> float:
    return Interval.coerce(_grid_at_value(pack, ts)).central


def grid_for(pack: dict, ts: str | None, region: str | None) -> float:
    return region_intensity(pack, region) if region else grid_at(pack, ts)


def _grid_interval(pack: dict, ts: str | None, region: str | None) -> Interval:
    return Interval.coerce(_region_value(pack, region) if region else _grid_at_value(pack, ts))


def _row_ts(row: dict) -> str | None:
    return (row.get("commit_ts") or (row.get("turn_ts_range") or [None, None])[1]
            or row.get("recorded_at"))


def _api_cost(tokens: dict, assumptions: dict) -> Interval:
    rates = assumptions.get("pricing_usd_per_mtok", {})
    total = ZERO
    for kind in _KINDS:
        total += Interval.exact(float(tokens.get(kind, 0) or 0)) * Interval.coerce(
            rates.get(kind, 0)) * 1e-6
    return total


def _per_token_metrics(tokens: dict, assumptions: dict, pack: dict) -> dict:
    billable = sum(float(tokens.get(k, 0) or 0) for k in ("input", "cache_write", "cache_read"))
    wh = (Interval.exact(float(tokens.get("output", 0) or 0))
          * Interval.coerce(assumptions.get("wh_per_output_token", 0))
          + Interval.exact(billable) * Interval.coerce(
              assumptions.get("wh_per_input_token", 0)))
    energy = wh * Interval.coerce(pack.get("pue", 1.0)) * 0.001
    return {"inference_seconds": ZERO, "energy_kwh": energy,
            "api_cost_usd": _api_cost(tokens, assumptions)}


def _serving_metrics(tokens: dict, turns: float, assumptions: dict, pack: dict,
                     output_tokens: Interval | None = None) -> dict:
    inp = Interval.exact(float(tokens.get("input", 0) or 0))
    cw = Interval.exact(float(tokens.get("cache_write", 0) or 0))
    cr = Interval.exact(float(tokens.get("cache_read", 0) or 0))
    out = output_tokens or Interval.exact(float(tokens.get("output", 0) or 0))
    effective_input = (inp
                       + cw * Interval.coerce(assumptions.get("cache_write_work_factor", 1))
                       + cr * Interval.coerce(assumptions.get("cache_read_work_factor", 1)))
    seconds = (effective_input / Interval.coerce(assumptions["prefill_tokens_per_second"])
               + out / Interval.coerce(assumptions["decode_tokens_per_second"])
               + Interval.exact(turns)
               * Interval.coerce(assumptions.get("fixed_seconds_per_turn", 0)))
    energy = (seconds * Interval.coerce(assumptions["server_power_kw"])
              * (1 / 3600) * Interval.coerce(pack.get("pue", 1.0)))
    price_tokens = dict(tokens)
    price_tokens["output"] = out.central
    return {"inference_seconds": seconds, "energy_kwh": energy,
            "api_cost_usd": _api_cost(price_tokens, assumptions)}


def _base_metrics() -> dict:
    return {"inference_seconds": ZERO, "energy_kwh": ZERO, "carbon_gco2e": ZERO,
            "electricity_cost_usd": ZERO, "api_cost_usd": ZERO}


def _add_metrics(left: dict, right: dict) -> dict:
    return {key: left.get(key, ZERO) + right.get(key, ZERO) for key in _base_metrics()}


def _finish_metrics(partial: dict, grid: Interval, pack: dict) -> dict:
    energy = partial["energy_kwh"]
    return {**partial,
            "carbon_gco2e": energy * grid,
            "electricity_cost_usd": energy
            * Interval.coerce(pack.get("electricity_usd_per_kwh", 0))}


def _model_kind(pack: dict) -> str:
    return (pack.get("energy_model") or {}).get("type", "per-token")


def _row_parts(row: dict, pack: dict, region: str | None) -> tuple[dict, dict]:
    grid = _grid_interval(pack, _row_ts(row), region)
    kind = _model_kind(pack)
    if row.get("kind") == COMPACTION_KIND:
        if kind != "serving-stack":
            return {}, _base_metrics()
        model = (row.get("models") or ["unknown"])[0]
        assumptions = model_assumptions(pack, model)
        cp = row.get("compaction") or {}
        summary = (Interval.exact(float(cp.get("summary_chars", 0) or 0))
                   / Interval.coerce(pack.get("summary_chars_per_token", 4)))
        tokens = {"input": cp.get("peak_context_tokens", 0), "cache_write": 0,
                  "cache_read": 0, "output": 0}
        metrics = _finish_metrics(_serving_metrics(tokens, 1.0, assumptions, pack, summary),
                                  grid, pack)
        return {model: metrics}, metrics

    by_model = row.get("by_model") or {
        (row.get("models") or ["unknown"])[0]: row.get("tokens", {})}
    weights = {model: sum(float(tok.get(k, 0) or 0) for k in _KINDS)
               for model, tok in by_model.items()}
    total_weight = sum(weights.values())
    turns = float(row.get("turns", 0) or 0)
    parts = {}
    total = _base_metrics()
    for model, tokens in by_model.items():
        assumptions = model_assumptions(pack, model)
        share = weights[model] / total_weight if total_weight else 1 / max(1, len(by_model))
        partial = (_serving_metrics(tokens, turns * share, assumptions, pack)
                   if kind == "serving-stack" else _per_token_metrics(tokens, assumptions, pack))
        metrics = _finish_metrics(partial, grid, pack)
        parts[model] = metrics
        total = _add_metrics(total, metrics)

    calls = sum(float(v or 0) for v in (row.get("server_tools") or {}).values())
    if calls and pack.get("server_tool_kwh_per_call") is not None:
        tool_energy = Interval.exact(calls) * Interval.coerce(pack["server_tool_kwh_per_call"])
        total = _add_metrics(total, _finish_metrics(
            {"inference_seconds": ZERO, "energy_kwh": tool_energy, "api_cost_usd": ZERO},
            grid, pack))
    return parts, total


_UNITS = {"inference_seconds": "s", "energy_kwh": "kWh", "carbon_gco2e": "gCO2e",
          "electricity_cost_usd": "USD", "api_cost_usd": "USD"}


def _central(metrics: dict) -> dict:
    return {key: round(value.central, 6 if key == "energy_kwh" else 4)
            for key, value in metrics.items()}


def _interval_json(metrics: dict) -> dict:
    return {key: value.to_dict(_UNITS[key]) for key, value in metrics.items()}


def _digest(pack: dict) -> str:
    raw = json.dumps(pack, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def estimate(rows: list[dict], pack: dict, region: str | None = None,
             mitigation: dict | None = None) -> dict:
    per_model: dict[str, dict] = {}
    total = _base_metrics()
    through = ""
    for row in rows:
        through = max(through, row.get("recorded_at") or "")
        model_parts, metrics = _row_parts(row, pack, region)
        total = _add_metrics(total, metrics)
        for model, part in model_parts.items():
            per_model[model] = _add_metrics(per_model.get(model, _base_metrics()), part)

    time_keyed = region is None and _grid_series(pack) is not None
    if region:
        grid_model, grid_scalar = f"region {region}", region_intensity(pack, region)
    elif time_keyed:
        grid_model, grid_scalar = "time-series (per commit timestamp)", None
    else:
        grid_model = "scalar/scenario"
        grid_scalar = Interval.coerce(pack.get("grid", {}).get("gco2e_per_kwh", 0)).central

    central = _central(total)
    # Historical field name means API expenditure, not provider electricity expense.
    totals = {"energy_kwh": central["energy_kwh"],
              "carbon_gco2e": round(total["carbon_gco2e"].central, 3),
              "cost_usd": round(total["api_cost_usd"].central, 4),
              "api_cost_usd": round(total["api_cost_usd"].central, 4),
              "electricity_cost_usd": round(total["electricity_cost_usd"].central, 4),
              "inference_seconds": round(total["inference_seconds"].central, 3)}
    by_model = {}
    for model, metrics in per_model.items():
        c = _central(metrics)
        by_model[model] = {"energy_kwh": c["energy_kwh"],
                           "carbon_gco2e": round(metrics["carbon_gco2e"].central, 3),
                           "cost_usd": round(metrics["api_cost_usd"].central, 4),
                           "api_cost_usd": round(metrics["api_cost_usd"].central, 4),
                           "electricity_cost_usd": round(metrics["electricity_cost_usd"].central, 4),
                           "inference_seconds": round(metrics["inference_seconds"].central, 3)}
    result = {
        "pack_version": pack.get("pack_version"),
        "pack_description": pack.get("description"),
        "assumptions_sha256": _digest(pack),
        "disclaimer": pack.get("disclaimer"),
        "through": through or None,
        "pue": Interval.coerce(pack.get("pue", 1)).central,
        "region": region,
        "grid_model": grid_model,
        "grid_gco2e_per_kwh": grid_scalar,
        "energy_model": _model_kind(pack),
        "totals": totals,
        "by_model": by_model,
        "intervals": {"totals": _interval_json(total),
                      "by_model": {m: _interval_json(v) for m, v in per_model.items()},
                      "contains_nontrivial_bounds": contains_interval(pack)},
        "provenance": normalize_provenance(pack),
        "accounting": {
            "primary_quantity": "gross attributed operational LLM-serving footprint",
            "economic_accounts": "API expenditure and modeled electricity expense overlap and are reported separately; do not sum them as total cost."
        },
        "scope": "Operational serving electricity plus configured server-tool energy; excludes training, embodied hardware, client devices, and network energy unless a pack explicitly includes them.",
        "method": ("measured ledger × versioned assumption pack; values are regenerable and are not stored in measured rows. Scenario bounds are not statistical confidence intervals."),
    }
    priced = mitigation_report(total["carbon_gco2e"], mitigation)
    if priced:
        result["mitigation"] = priced
    return result


def _fmt_interval(item: dict, digits: int = 3) -> str:
    return f"[{item['low']:.{digits}f}, {item['central']:.{digits}f}, {item['high']:.{digits}f}]"


def cmd_estimate(args) -> None:
    try:
        pack = load_pack(args.pack)
        mitigation = load_mitigation(getattr(args, "mitigation", None))
        result = estimate(read_ledger(), pack, region=getattr(args, "region", None),
                          mitigation=mitigation)
    except (OSError, json.JSONDecodeError) as exc:
        import sys
        sys.exit(f"error: could not read assumption data: {exc}")
    except ValueError as exc:
        import sys
        sys.exit(f"error: {exc}")
    if args.fmt == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    t = result["totals"]
    print(f"llm_resource_tally estimate — pack {result['pack_version']}")
    if result.get("disclaimer"):
        print(f"  ! {result['disclaimer']}")
    print(f"  ledger through : {result['through']}")
    grid = (f"region {result['region']} {result['grid_gco2e_per_kwh']} gCO2e/kWh"
            if result.get("region") else result["grid_model"])
    print(f"  energy model   : {result['energy_model']} · PUE {result['pue']} · {grid}")
    print(f"  energy         : {t['energy_kwh']:.4f} kWh")
    print(f"  carbon         : {t['carbon_gco2e']:.1f} gCO2e")
    print(f"  API expenditure: ${t['api_cost_usd']:.2f}")
    print(f"  electricity    : ${t['electricity_cost_usd']:.4f} modeled provider-side expense")
    if result["intervals"]["contains_nontrivial_bounds"]:
        ints = result["intervals"]["totals"]
        print(f"  scenario bounds: energy {_fmt_interval(ints['energy_kwh'], 4)} kWh; "
              f"carbon {_fmt_interval(ints['carbon_gco2e'], 1)} gCO2e")
    if result["by_model"]:
        print("  by model:")
        for model, values in result["by_model"].items():
            print(f"    {model:<20} {values['energy_kwh']:.4f} kWh  "
                  f"{values['carbon_gco2e']:.1f} gCO2e  API ${values['api_cost_usd']:.2f}")
    if result.get("mitigation"):
        print("  optional mitigation price scenarios (gross footprint remains unchanged):")
        for name, item in result["mitigation"]["price_scenarios"].items():
            cost = item["nominal_cost_to_cover_modeled_high_usd"]
            print(f"    {name:<30} {_fmt_interval(cost, 2)} USD")
    if result["provenance"]:
        print("  provenance:")
        for prov in result["provenance"]:
            tag = f"{prov['applies_to']}: " if prov.get("applies_to") else ""
            cite = f" <{prov['citation']}>" if prov.get("citation") else ""
            lic = f" [{prov['license']}]" if prov.get("license") else ""
            print(f"    - {tag}{prov.get('source', '?')}{lic}{cite}")
            if prov.get("note"):
                print(f"        {prov['note']}")
    print(f"  {result['method']}")
