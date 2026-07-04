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

Scope a report to a branch or PR with `--commits`:

```bash
<rt> report --commits main..HEAD             # the measured cost of this branch's commits
<rt> report --commits main..HEAD --by activity --format md
```

The **`PR LLM cost` GitHub Action** ([.github/workflows/pr-ledger.yml](../.github/workflows/pr-ledger.yml))
uses exactly this to comment each PR with the measured cost of the commits it adds — the ledger
becoming visible in review, which is what keeps a team's ledger accurate.

## `fleet` — many repos, one report

Each repo carries its own committed ledger, so an org-wide view needs no server and no retention
window — just clones on disk:

```bash
<rt> fleet ~/code                  # scan a directory for repos with a ledger
<rt> fleet repo-a repo-b --format json
```

The grand total is the **sum of per-repo totals** (each repo's ledger is already de-duplicated
internally), never a re-dedup across repos.

## `estimate` — energy / carbon / USD (modeled)

> **`estimate` lives in the optional `modeling` package.** The bare `curl | sh` install vendors
> only the measurement core, so the offline footprint stays tiny. If `estimate` reports it's
> missing, add modeling once (idempotent):
>
> ```bash
> <rt> install --modeling            # offline if pip-installed; else fetches just that subpackage
> # or include it at curl time:  RT_MODELING=1 curl -fsSL …/install.sh | sh
> # or, via pip:                 pip install llm_resource_tally   (includes modeling)
> ```
>
> `report`, `fleet`, `doctor`, and all recording work without it — measurement never depends on
> the modeling layer.

Turning tokens into energy, carbon, and dollars needs **assumptions**, and assumptions change.
So they live outside the ledger, in a versioned **assumption pack** (a JSON file — data, not
code):

```bash
<rt> estimate                     # uses the built-in baseline pack
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

> **The built-in pack is a *baseline*, not gospel.** Grid intensity and per-token energy are
> sourced, order-of-magnitude estimates (see the pack's `provenance`); pricing is a list-price
> placeholder. Copy `llm_resource_tally/modeling/assumptions/default-pack.json`, refine for your
> models/region/contract, and pass it with `--pack`.

### Per-region grid (`--region`)

Where the inference actually ran fixes the grid's carbon intensity — France's grid is ~20×
cleaner than India's for the *same* tokens. The ledger records no datacenter location (it can't
measure that), so region is an assumption you assert. The modeling package ships a real
per-region table built from **CodeCarbon's** global energy mix (per-country `carbon_intensity`,
MIT-licensed), in [`grid-codecarbon.json`](../llm_resource_tally/modeling/assumptions/grid-codecarbon.json):

```bash
<rt> estimate --pack .llm_resource_tally/tool/modeling/assumptions/grid-codecarbon.json --region FRA
<rt> estimate --pack …/grid-codecarbon.json --region USA     # ISO-3166 alpha-3 codes
```

`--region` selects one country's intensity from the pack's `grid.by_region` map (energy and
pricing are inherited from the baseline; only the grid changes). Without a region, CodeCarbon's
world-average fallback applies. An unknown region is a clear error, never a silent wrong number.

The regional pack is not hand-maintained — it's frozen from the adapter (below) by
[`dev/build_grid_pack.py`](../dev/build_grid_pack.py), pinned to a CodeCarbon release. Re-run it
(bump `--ref`) to refresh, and commit the diff.

## Sources, adapters, and provenance

Where an assumption pack comes from is a **source**: `{"adapter": name, "ref": ...}`. An
**adapter** turns a `ref` into a pack dict. The vendored default loads through this same
mechanism — a `json-file` source pointing at the shipped pack — so nothing is special-cased.
Adding a new source later (a live carbon-intensity API, a regional dataset, a codecarbon
export) is **just a new adapter plus a ref**:

```python
from llm_resource_tally.modeling.estimate import register_adapter, resolve_source
register_adapter("http-json", lambda url: fetch_and_map(url))   # you write this once
pack = resolve_source({"adapter": "http-json", "ref": "https://…/grid.json"})
```

Two adapters ship today: `json-file` (a plain pack file) and `codecarbon-energy-mix`, which
turns CodeCarbon's raw `global_energy_mix.json` into a full pack (per-region grid + baseline
energy/pricing + MIT-cited grid provenance). The second one is the proof that "point at a new
source" really is *just* an adapter and a ref — and it's the same code `dev/build_grid_pack.py`
calls to freeze `grid-codecarbon.json`, so the shipped snapshot can never drift from the
adapter. A live-fetch adapter (`http-json` above) is not written yet; the interface is here so
it's a drop-in when wanted, and so custom/user estimations use the exact same path as the
vendored ones.

Every number is accountable via **provenance**. A pack carries a `provenance` list; each entry
names one contributing source and, optionally, which part of the model it backs
(`applies_to`: `grid` | `energy` | `pricing` | `pue` | `all`) plus `citation`, `license`,
`retrieved`, and a `note`. `estimate` prints it and includes it in `--format json`, so a
reader can see the grid figure came from one source and the per-token energy from another —
and, crucially, that codecarbon backs the **grid** but *not* the per-token energy (codecarbon
measures local CPU/GPU; LLM API inference is remote, so its energy comes from inference
studies instead).

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
