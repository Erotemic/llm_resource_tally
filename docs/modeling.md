# Reporting & modeling

The ledger stores **measurements only**. Everything in this page is a *post-hoc pass* over
those measurements — regenerable, never written back into the ledger.

## `report` — readable views (measured)

`show` dumps raw rows; `report` aggregates them into the views you actually want:

```bash
<rt> report                       # per commit (default)
<rt> report --by day              # per calendar day
<rt> report --by activity         # per --label (planning / implementation / review / …)
<rt> report --by agent            # claude-code vs codex vs …
<rt> report --by model            # per model (token kinds only; turns aren't per-model)
<rt> report --by commit --format md   # table | md | tsv | json
```

Because it reads only the committed ledger, `report` works on any clone, years later, with no
session logs present. That is the whole point of a committed ledger over an ephemeral viewer.

## `estimate` — energy / carbon / USD (modeled)

Turning tokens into energy, carbon, and dollars needs **assumptions**, and assumptions change.
So they live outside the ledger, in a versioned **assumption pack** (a JSON file — data, not
code):

```bash
<rt> estimate                     # uses the built-in illustrative pack
<rt> estimate --pack my-rates.json --format json
```

A pack maps each model to an energy model (`wh_per_output_token`, `wh_per_input_token`) and a
price table (`pricing_usd_per_mtok` by token kind), plus a PUE multiplier and a grid intensity
(`gCO₂e/kWh`). `estimate` computes, per model and in total:

- `energy_kwh = PUE × (output × wh_per_output_token + billable_input × wh_per_input_token) / 1000`
- `carbon_gco2e = energy_kwh × grid(at the row's commit timestamp)`
- `cost_usd = Σ_kind tokens[kind] / 1e6 × pricing_usd_per_mtok[kind]`

Every figure is traceable to *(measured tokens, pack version)* — the honesty rule made visible.
Publishing a better pack never touches recorded data; re-run `estimate` and the numbers update.

**Grid intensity can vary over time.** A scalar `grid.gco2e_per_kwh` applies one number to
everything. But the ledger stores each commit's timestamp precisely so carbon can reflect the
grid *at the moment the work happened*. Give the pack a time series instead and `estimate`
computes per row, picking the intensity in effect at each commit:

```json
"grid": {
  "intensity_by_date": [
    {"from": "2024-01-01", "gco2e_per_kwh": 420},
    {"from": "2026-01-01", "gco2e_per_kwh": 350}
  ]
}
```

This is the payoff of a per-commit, committed ledger: a decarbonizing grid shows up in the
history instead of being flattened to a single average.

> **The built-in pack ships ILLUSTRATIVE placeholder rates** so the command has a shape out of
> the box. Copy `llm_resource_tally/assumptions/default-pack.json`, put in your contracted
> prices and a real energy/grid model (ideally keyed to each commit's timestamp, which the
> ledger records), and pass it with `--pack`. Do not treat the defaults as authoritative.

Historical usage older than any surviving transcript should live in a *separate, clearly
labeled* pack/estimate too — an estimate, never fabricated rows in the measured ledger.

## `doctor` — is it wired and will it keep working?

```bash
<rt> doctor
```

A read-only pass: is the post-commit hook armed, are the Claude native hooks wired, do the
registered backends find a session for this repo, does the ledger read cleanly — and, most
importantly, **is the agent's transcript retention high enough to backfill later**. Retention
is the one silent, unrecoverable failure (Claude Code prunes after `cleanupPeriodDays`, default
30), so `doctor` warns when it is set too low. `install` runs `doctor` at the end.
