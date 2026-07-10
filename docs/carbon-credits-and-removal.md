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

## Established providers and purchasing platforms

The following are examples to investigate, not endorsements. Prices and inventories change, and
quality must be assessed at the project level. Price observations below were checked on
**2026-07-10** unless otherwise noted.

### Gold Standard Marketplace

- Marketplace: <https://marketplace.goldstandard.org/collections/projects>
- Registry: <https://registry.goldstandard.org/>
- Role: direct marketplace for projects certified under Gold Standard.
- Useful evidence: project type, location, vintage, seller, price, and registry linkage.
- Price snapshot: examples listed at roughly **$11-$52 per tonne**; listed nature-based removal
  examples were approximately **$39-$52 per tonne**.

Gold Standard is useful when the goal is a traceable certified credit with public project details.
The exact methodology and project still need review; the standard name alone is not a substitute
for project-level diligence.

### Cool Effect

- Projects and prices: <https://www.cooleffect.org/projects>
- Role: U.S. nonprofit platform offering selected projects from standards including Gold Standard,
  American Carbon Registry, Climate Action Reserve, and Verra's Verified Carbon Standard.
- Useful evidence: standard, registry project ID, vintage, project type, and public price.
- Price snapshot: currently listed projects with visible prices ranged from about
  **$8.24-$49.44 per tonne**. The platform states that more than 90% of each contribution goes to
  projects.

Cool Effect is convenient for comparing several project types and registries. As with any curated
platform, review the project documentation rather than treating curation as a guarantee.

### Puro.earth

- Biochar methodology: <https://puro.earth/methodologies/biochar/>
- Registry: <https://registry.puro.earth/>
- Role: carbon-removal standard, registry, and market infrastructure with biochar and other durable
  removal pathways.
- Useful evidence: methodology version, supplier facility, verification, issuance, durability class,
  serial numbers, and retirement.
- Price use: obtain a live supplier or intermediary quote; the methodology page is not a retail
  price list.

Puro.earth currently describes biochar certificates as storing carbon for at least 200 years and
requires carbon-removal pathways to meet scientific measurability and long-term storage criteria.
Treat that as the methodology's durability basis and still inspect the particular facility and
certificate.

### Carbonfuture

- Platform: <https://www.carbonfuture.earth/>
- Role: procurement, due diligence, digital MRV, and delivery infrastructure for durable removal,
  with substantial biochar activity.
- Useful evidence: supplier and methodology details, lifecycle data, delivery tracking, audit data,
  purchase and retirement records, and chain of custody from production to end use.
- Price use: request a project or portfolio quote; public pages do not provide a stable small-buyer
  per-tonne checkout price.

Carbonfuture is particularly relevant to biochar diligence because its platform tracks physical
deliveries and end use. That tracking is useful evidence, but buyers should still examine the
underlying standard, verifier, feedstock rules, and project calculations.

### Isometric

- Biochar pathway and protocol: <https://isometric.com/pathways/biochar>
- Registry: <https://registry.isometric.com/>
- Role: carbon-removal protocol, certification, and registry infrastructure rather than primarily a
  consumer marketplace.
- Useful evidence: durability class, feedstock eligibility, laboratory measurement method, lifecycle
  assessment, storage pathway, monthly issuance, and registry certificate.
- Price use: use an actual supplier or marketplace quote rather than inferring a price from the
  protocol.

Isometric currently distinguishes 200-year biochar certificates based on H/C measurements and
projected decay curves from 1,000-year certificates based on random-reflectance measurements of the
inertinite fraction. This illustrates why even two biochar credits can represent different
durability claims.

### Climeworks

- Provider: <https://climeworks.com/>
- Role: direct air capture with durable geologic storage.
- Useful evidence: a physically durable removal pathway and provider-specific delivery records.
- Price context: a publicly reported retail price was approximately **$1,000 per tonne** in
  October 2024; current availability and pricing should be checked directly.

Climeworks represents the high-cost end of current retail removal. It is a useful price scenario
for durable removal, but contracted delivery dates, net-removal accounting, and current operating
performance should be reviewed.

### Terraset

- Nonprofit: <https://www.terrasetclimate.org/>
- Role: philanthropic financing and pre-purchases for carbon-removal projects.
- Portfolio examples shown by Terraset include Capture6, Charm Industrial, Climeworks, Heirloom,
  Planetary, and Vaulted Deep.
- Pricing use: Terraset is donation- and portfolio-oriented rather than a stable retail
  per-tonne marketplace. Do not turn a donation into an offset-price input unless the resulting
  delivered tonnes, timing, and retirement or claim rights are documented.

Terraset may be appropriate when the aim is to support early removal capacity rather than buy an
immediately issued credit at a known unit price.

### Frontier

- Portfolio: <https://frontierclimate.com/portfolio>
- Role: expert-reviewed advance purchases and offtakes for durable carbon-removal technologies.
- Useful evidence: public supplier descriptions, pathway, contracted quantities, delivered
  quantities, and selection process.
- Pricing use: Frontier is primarily a buyer platform and due-diligence reference, not a small
  retail offset store. It is useful for identifying suppliers and understanding durable-removal
  pathways; use an actual supplier quote or purchase contract for the price assumption.

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
