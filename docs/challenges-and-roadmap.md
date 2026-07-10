# Accounting challenges, limitations, and the path to better estimates

This document describes the conceptual limitations of estimating the LLM footprint of a software
repository and how the project can overcome them. These are not reasons to avoid estimating. They
are the requirements for making a ballpark estimate honest and progressively more useful.

## 1. Measurement coverage is itself uncertain

### Current limitation

A ledger can only record sessions that still exist and are discoverable. Hook downtime, expired
transcripts, unsupported runtimes, deleted sessions, cross-repository work, and pre-install history
can all create gaps. A total without coverage metadata may look complete when it is only a lower
bound.

### Why it matters

Model uncertainty and missing-data uncertainty are different. Narrow energy assumptions do not
help when a substantial fraction of the work was never observed.

### Path forward

Reports should include a machine-readable coverage section:

- first and last observed timestamps;
- commits with and without attributed LLM observations;
- known hook-installation periods;
- sessions rejected or left unresolved;
- transcript-retention horizon;
- unsupported or unknown backends;
- a status such as `complete`, `partial`, or `unknown` rather than a fabricated percentage when
  the denominator cannot be established.

Where possible, coverage should be reported separately from the energy interval. Missing usage
should remain visibly unknown rather than being folded silently into a low estimate.

## 2. Repository attribution is a policy, not proof of causality

### Current limitation

The default rule charges a turn to the next commit it precedes, while `reconcile` assigns
non-committing work to a pending repository bucket. This is a useful chronological proxy, but
research may support several later commits, one session may affect multiple repositories, and
failed work may be essential despite producing no artifact.

### Why it matters

The place where computation occurred, the artifact it benefited, and the cost center to which it
is charged are distinct relationships.

### Path forward

Preserve the simple automatic policy, but represent attribution as an allocation layer:

- observation identity and originating session;
- one or more target artifacts such as commits, pull requests, issues, documents, or repositories;
- allocation fractions that sum to one when work is split;
- attribution method (`next-commit`, `pending-session`, `manual`, `cross-repo-hook`, and so on);
- confidence or review status for manually resolved cases.

The report should state the active policy. Repository totals can then be recalculated without
altering the underlying observations.

## 3. Model aliases do not identify the serving system

### Current limitation

A public model name does not reveal accelerator type, quantization, tensor parallelism, batch
occupancy, speculative decoding, routing, region, or software version. The same alias can be
served by different systems at different times.

### Why it matters

Energy is determined by the execution system, not the marketing name alone. The uncertainty from
this hidden execution identity may dominate the arithmetic.

### Path forward

Attach an evidence grade to each model profile:

1. **Attested** — request-level or provider-published telemetry applicable to the observation.
2. **Calibrated** — model/provider/date-specific measurement with a documented methodology.
3. **Proxy** — a comparable open or independently benchmarked serving configuration.
4. **Generic scenario** — broad assumptions used only to constrain order of magnitude.

Profiles should carry provenance, applicable dates, region if known, and a citation or measurement
record. Reports should summarize how much estimated energy comes from each evidence grade.

## 4. The energy question must declare attributional versus marginal accounting

### Current limitation

Tokens divided by throughput and multiplied by serving power approximates an allocated share of a
serving stack. It does not establish how much additional energy the request caused. Batching,
idle power, autoscaling thresholds, and concurrent users can make average and marginal energy very
different.

### Why it matters

Two valid questions have different answers:

- **Attributional:** what share of operating the serving system should be assigned to this work?
- **Consequential:** how much additional electricity was consumed because this work occurred?

### Path forward

Name the accounting method in every assumptions profile. Initial repository reports should use an
**attributional operational** method because it is feasible from client-side observations. A
future consequential model requires provider or controlled-serving data about batching,
utilization, and scaling behavior. The two results should never be mixed under one unlabeled
“energy” field.

## 5. Independent intervals do not preserve correlated scenarios

### Current limitation

Componentwise `[low, central, high]` arithmetic is transparent, but the extrema may combine
assumptions that cannot occur together. Hardware changes power and throughput simultaneously;
region changes PUE, grid intensity, and sometimes hardware availability.

### Why it matters

An interval assembled from independent extrema can be wider than any realistic system, while its
central value need not be an expected value.

### Path forward

Retain simple intervals for exploratory use, then add named joint scenarios such as:

- provider A / region X / H100-class stack / high batch occupancy;
- provider A / unknown region / low occupancy;
- open-model benchmark / measured hardware configuration.

Each scenario should be internally coherent and produce a complete report. The envelope across
scenarios becomes the defensible range. Probability distributions or Monte Carlo analysis are
useful only when the inputs have defensible distributions and dependencies.

## 6. Carbon intensity requires time, place, and accounting method

### Current limitation

A grid-intensity coefficient is ambiguous without the serving region and the electricity-accounting
method. Commit time is not necessarily inference time, and provider region is often hidden.

### Why it matters

Average versus marginal intensity, location-based versus market-based Scope 2 accounting, and
renewable-energy claims can produce materially different numbers.

### Path forward

Carbon profiles should state:

- region or `unknown`;
- timestamp resolution and which observed timestamp was used;
- average or marginal grid factor;
- location-based or market-based method;
- treatment of power-purchase agreements and certificates;
- whether transmission losses are included;
- source and vintage of the grid data.

When region is unknown, produce a regional scenario range rather than silently selecting the
user's location or the commit's location.

## 7. The system boundary is initially narrow

### Current limitation

The first model estimates operational serving electricity and facility overhead. It excludes
training, hardware manufacture, networking, client devices, water, storage, and the infrastructure
used by server-side tools unless explicitly modeled.

### Why it matters

A narrow boundary is useful, but it must not be mistaken for complete life-cycle impact. Training
and embodied emissions also require controversial allocation rules across users and model lifetime.

### Path forward

Represent scope components explicitly:

- `serving_compute.operational`;
- `datacenter_overhead`;
- `server_tools.operational`;
- `network.operational`;
- `client_device.operational`;
- `model_training.amortized`;
- `hardware_embodied.amortized`;
- `water.operational`.

Every result should identify included components. Boundary extensions should be separate modules
with visible allocation policies, not hidden multipliers in the serving estimate.

## 8. Economic accounts must not be conflated

### Current limitation

API expenditure and modeled electricity expense answer different questions. API price is the
user's expenditure and already finances provider electricity among many other costs. Adding the
two double-counts overlapping economic scope.

### Path forward

Keep separate accounts:

- user API expenditure;
- hypothetical serving-electricity expense;
- physical energy and CO2e;
- optional monetized environmental externality, clearly labeled as such.

Do not report a combined “total cost” unless an explicitly defined non-overlapping accounting
framework supports it. The project's primary outcome is physical footprint, not a synthetic dollar
sum.

## 9. Cross-repository aggregation can double-count observations

### Current limitation

The same work can be associated with a parent and submodule, cherry-picked, copied to a fork, or
manually recorded in more than one ledger. Per-repository deduplication does not by itself make an
organization-wide sum safe.

### Path forward

Use globally stable observation identities and preserve origin provenance. Organization-level
aggregation should deduplicate observations before applying repository allocation fractions.
Reports should distinguish gross repository-attributed totals from deduplicated portfolio totals.

## 10. Auditability and privacy must advance together

### Current limitation

The ledger avoids message content, but timestamps, model choices, token volume, activity labels,
and commit associations can still reveal work patterns. A digest identifies an observation but
does not by itself prove which transcript or parser produced it.

### Path forward

Record privacy-preserving derivation metadata:

- backend and transcript-schema versions;
- parser and tool versions;
- digest of the relevant source records or canonical normalized observation;
- assumptions and grid-data digests;
- optional signatures or provider attestations;
- a documented retention and sharing policy for ledger data.

Threat-model committed and notes-based ledgers separately. Auditability should not require storing
prompts or source-code content.

## 11. A footprint is not an efficiency metric without an outcome denominator

### Current limitation

A large repository footprint may correspond to more functionality, a longer lifetime, or higher
quality. Raw kWh or CO2e alone cannot establish that one development process was more efficient.

### Path forward

Keep absolute lifetime footprint as the primary result, then support optional functional units:

- per merged pull request;
- per release;
- per resolved issue;
- per accepted or test-passing task;
- per active development month;
- per user-selected outcome metric.

The tool should never infer product value automatically. It should make denominators explicit so
comparisons are not disguised value judgments.

## 12. Gross footprint is not net climate impact

### Current limitation

The ledger does not know the counterfactual. LLM use may replace human workstation time, prevent
experiments, create additional maintenance, or enable work that would not otherwise occur.

### Path forward

Describe the core output as **gross attributed footprint**. Counterfactual or avoided-emissions
studies belong in a separate analysis layer with explicit alternatives. Do not subtract speculative
benefits from the measured burden in the primary report.


## 13. Mitigation purchases need their own evidence trail

### Current limitation

A price per tonne can estimate what it would cost to buy a credit or fund removal, but price does
not establish quality. Different instruments represent avoided emissions, short- or long-duration
removal, and future or already-delivered tonnes. Checkout minimums can also dominate the cost of a
small repository footprint.

### Path forward

Keep mitigation outside the gross-footprint totals. Typed price scenarios now distinguish
emission avoidance/reduction, nature-based removal, biochar removal, and geological or mineral
removal, and preserve durability, delivery, uncertainty, and claim guidance. The next step is to
record actual purchases with project, registry, methodology, vintage, serial number, delivery
status, quantity retired, fees, total cost, and any project-specific effectiveness assessment. A
future command may record this evidence, but it should never replace or subtract from the historical
gross estimate. See [Carbon-credit and carbon-removal cost scenarios](carbon-credits-and-removal.md).

# Development sequence for a stronger accounting framework

The following sequence improves the credibility of the estimate without waiting for impossible
perfect data.

## Stage 1 — reliable observed-work accounting

- stable global observation identities;
- repeated-run and cross-storage deduplication;
- coverage and unresolved-attribution reporting;
- explicit repository allocation method;
- provenance for parser/tool versions.

Deliverable: a trustworthy measured lower-bound ledger with visible gaps.

## Stage 2 — coherent operational-energy scenarios

- evidence-graded provider/model profiles;
- named joint hardware/throughput/power/PUE scenarios;
- attributional versus consequential method labels;
- server-tool and compaction treatment;
- sensitivity breakdown showing dominant assumptions.

Deliverable: reproducible operational-energy intervals whose width has an explanation.

## Stage 3 — defensible carbon conversion

- time- and region-aware grid datasets;
- unknown-region scenario envelopes;
- average/marginal and location/market method labels;
- grid-data provenance and versioning.

Deliverable: operational CO2e ranges suitable for repository and organization reporting.

## Stage 4 — portfolio aggregation and comparison

- cross-repository global deduplication;
- fractional allocation across artifacts and repositories;
- functional units and outcome denominators;
- uncertainty-aware aggregation across projects.

Deliverable: organization-level estimates that do not multiply-count shared work.

## Stage 5 — optional life-cycle boundary extensions

- training-energy allocation scenarios;
- embodied hardware and network modules;
- operational water estimates;
- separate counterfactual studies of net impact.

Deliverable: broader life-cycle analyses that remain separable from the well-supported operational
core.

# Reporting discipline

Until those stages are complete, reports should always include:

- the phrase **gross attributed operational LLM-serving footprint**;
- measurement coverage and known gaps;
- active attribution policy;
- included and excluded boundary components;
- assumptions profile, digest, evidence grade, and accounting method;
- interval interpretation and dominant uncertainty sources;
- a warning that the result is not a provider meter reading, life-cycle total, or net-impact claim.

The purpose of these qualifications is not to weaken the result. It is to make the ballpark number
credible enough to improve decisions and climate accounting.
