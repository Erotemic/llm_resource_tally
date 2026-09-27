# Carbon-credit and carbon-removal cost scenarios

The repository footprint report measures or estimates **gross attributed emissions**. A separate
question is what it would cost to purchase and retire carbon credits, or to fund carbon removal,
for an equivalent quantity of CO2e. The `estimate` command can calculate those costs as optional
scenarios, but it does not subtract them from the gross footprint.

This separation is intentional:

- the gross footprint describes the estimated physical burden of the observed LLM work;
- a credit or removal purchase is a separate financial action;
- the quality and durability of that action depend on the specific project and contract;
- a purchase should not be represented as making the original energy use or emissions disappear.

## Credit types are not interchangeable

A certificate labelled “one tonne CO2e” can represent several materially different claims. The
repository should therefore report the **type of climate action**, not only the price and nominal
tonnage.

| Type | What physically happened? | Storage / durability | Main uncertainty | Appropriate interpretation |
|---|---|---|---|---|
| Emission avoidance or reduction | A project claims that future emissions were lower than a counterfactual baseline | Usually no new carbon store; durability depends on the intervention continuing | Baseline selection, additionality, leakage, rebound, and double counting | A financed reduction or avoidance contribution; not removal of the repo's emitted CO2 |
| Nature-based removal | Plants or soils take CO2 from the atmosphere | Biological stores can last decades to centuries but may reverse through fire, disease, harvesting, or land-use change | Measurement, saturation, reversal, leakage, and stewardship | Real removal with reversible storage and continuing monitoring obligations |
| Biochar carbon removal | Biomass removes atmospheric CO2; pyrolysis converts part of its carbon into stable pyrogenic carbon | Common crediting claims are centennial; some measured fractions may qualify for longer durability | Feedstock counterfactual, stable-carbon fraction, pyrolysis and transport emissions, lifecycle deductions, end use, and chain of custody | Physical removal with comparatively durable storage, subject to pathway-specific MRV |
| Geological or mineral removal | Atmospheric or biogenic CO2 is captured and stored underground or mineralized | Typically designed for centuries to millennia with low reversal risk | Net lifecycle removal, energy source, capture efficiency, storage monitoring, and delivery risk | Durable removal; distinguish delivered tonnes from future purchases |
| Emerging durable pathways | Examples include enhanced rock weathering and some ocean-based methods | Potentially long-lived | The principal challenge may be measurement and attribution rather than physical storage | Report pathway and MRV evidence explicitly; do not infer quality from durability alone |

Avoidance and reduction projects can provide real climate benefits. The important accounting point
is that they answer a different question from removal. Preventing a tonne that might otherwise have
been emitted does not extract a tonne already emitted by the repository. The Oxford Principles for
Net Zero Aligned Carbon Offsetting therefore distinguish emission reductions and avoided emissions
from carbon removals, and recommend shifting compensation for residual emissions toward removals
with lower reversal risk and more durable storage.

Durability is also a continuum rather than a binary label. A well-managed biological store may last
for a long time; a poorly documented engineered-removal project may underperform its claim. The
project, methodology, monitoring evidence, delivery status, and reversal provisions remain more
important than a broad category name.

### Why biochar is a distinct middle tier

Biochar is often materially more expensive than avoidance credits, but substantially less expensive
than current retail direct-air-capture removal. Its climate claim is also structurally different:
photosynthesis first removes CO2, and pyrolysis stabilizes a measured fraction of the biomass carbon.
For example, Puro.earth describes its biochar methodology as requiring at least 200-year storage,
while Isometric supports 200- or 1,000-year certificates depending on the durability measurement
method. Those are methodology claims, not guarantees for every product sold as biochar.

A serious biochar purchase should retain evidence for:

- eligible and sustainably sourced feedstock, including what would otherwise have happened to it;
- pyrolysis operating data and direct process emissions;
- laboratory measurements used to estimate stable carbon and decay;
- lifecycle emissions for energy, transport, equipment, and application;
- the final storage environment and chain of custody;
- whether the credited tonne is already produced and stored or promised for future delivery;
- independent verification, registry issuance, serial number, and retirement.

Biochar therefore has lower counterfactual uncertainty than many avoidance credits because there is
a physical carbon product to measure, but it is not uncertainty-free. Its net-removal claim still
depends on feedstock additionality, conservative lifecycle accounting, stable-carbon estimation,
and verified end use.

### Nominal tonnes versus effective climate effect

The model always reports the nominal cost of purchasing the credited quantity. It may also accept a
**project-specific** interval named `effective_tco2e_per_credited_tco2e`. When supplied, it estimates
how many credited tonnes would be required to cover the modeled high footprint after applying that
external effectiveness assessment.

```json
{
  "biochar_project_example": {
    "credit_category": "carbon_removal",
    "removal_pathway": "biochar",
    "usd_per_tco2e": [120, 160, 220],
    "effective_tco2e_per_credited_tco2e": [0.8, 0.95, 1.0],
    "effectiveness_basis": "project-specific independent assessment; replace this example"
  }
}
```

The tool does **not** assign these factors from the credit category alone. There is no defensible
universal rule that every avoided-emission credit is worth a fixed fraction, or that every biochar
credit is exactly one effective tonne. Any effectiveness interval must identify its project,
methodology, evidence, and assessor. Omitting the field is preferable to inventing a discount.

## How the model prices mitigation

An assumptions file may contain:

```json
{
  "mitigation": {
    "price_scenarios": {
      "avoided_or_reduced_emissions": {
        "credit_category": "emission_avoidance_or_reduction",
        "usd_per_tco2e": [8, 25, 55]
      },
      "nature_based_removal": {
        "credit_category": "carbon_removal",
        "removal_pathway": "nature_based",
        "usd_per_tco2e": [30, 60, 150]
      },
      "biochar_carbon_removal": {
        "credit_category": "carbon_removal",
        "removal_pathway": "biochar",
        "usd_per_tco2e": [100, 150, 250]
      },
      "geological_or_mineral_removal": {
        "credit_category": "carbon_removal",
        "removal_pathway": "geological_or_mineral",
        "usd_per_tco2e": [300, 700, 1200]
      }
    }
  }
}
```

For each scenario, the report preserves its type metadata and provides:

- the modeled footprint in tonnes CO2e;
- a nominal proportional cost interval, combining footprint and price uncertainty;
- the nominal quantity required to cover the modeled high footprint bound;
- the nominal cost of that high-bound quantity across the price interval;
- when a project-specific effectiveness interval is supplied, an adjusted purchase quantity and
  adjusted cost interval.

The built-in ranges are broad category examples, not live quotes or claims that projects within a
category have equal quality. Future-delivery contracts, wholesale offtakes, and retail checkout
prices are not directly comparable. Edit the assumptions
with a current project price before using the result for a purchase decision.

Small repository footprints can be far below one tonne. A mathematically proportional cost may be
only cents, while a marketplace may require a one-tonne purchase or a fixed minimum contribution.
The model does not silently round to a provider minimum. Record transaction fees, minimums, taxes,
and the number of retired tonnes separately.

## Quality checks

Certification alone does not make every credit equally reliable. The project-level questions that
matter include:

- **additionality:** would the activity have occurred without credit revenue?
- **quantification:** is the credited quantity measured conservatively?
- **permanence:** how long is carbon stored, and how is reversal handled?
- **double counting:** is each unit uniquely issued, claimed, and retired once?
- **leakage:** does the activity shift emissions elsewhere?
- **delivery:** is the credit already issued, or is removal promised for a future date?
- **safeguards:** are material social and environmental harms addressed?

The [ICVCM Core Carbon Principles](https://icvcm.org/core-carbon-principles/) provide a useful
benchmark covering governance, tracking, independent verification, additionality, permanence,
quantification, and double counting. The
[Carbon Offset Guide](https://offsetguide.org/what-makes-high-quality-carbon-credits/) gives a
project-level explanation of the same core risks. Buyers should also inspect the underlying
registry entry, methodology, vintage, serial numbers, and retirement evidence.

The [Oxford Offsetting Principles](https://www.smithschool.ox.ac.uk/research/oxford-offsetting-principles)
provide a complementary portfolio-level framework: prioritize direct reductions, transition from
avoidance toward removal for residual emissions, and shift toward storage with lower reversal risk.
For biochar-specific diligence, examples of pathway standards include the
[Puro.earth biochar methodology](https://puro.earth/methodologies/biochar/) and the
[Isometric biochar protocol](https://isometric.com/pathways/biochar). These are useful sources of
methodological requirements; their inclusion here is not an endorsement of every project certified
under them.

## Dated provider research

A [structured July 2026 snapshot](carbon-provider-snapshot-2026-07-10.json) preserves the
provider examples, links, and price observations collected for this document. It is historical
research; use current project evidence and quotes for a purchase.

## Reporting a purchase

A complete repository report should retain both statements:

```text
Gross modeled footprint:             [low, central, high] tCO2e
Credits/removal purchased and retired: X tCO2e under project/registry Y
```

Also record:

- provider and project name;
- registry and project ID, where applicable;
- methodology and vintage;
- quantity purchased and quantity retired;
- retirement serial number or certificate;
- purchase and delivery dates;
- price per tonne, fees, and total paid;
- whether the instrument is an avoided-emission credit, nature-based removal, or durable removal;
- whether delivery is ex-post or promised in the future.

This permits later review without changing the historical gross-footprint estimate.
