# Mission and method: estimating the LLM footprint of building software

## The question

Large language models are becoming a practical part of software development. They can accelerate
implementation, debugging, research, review, documentation, and maintenance. That usefulness does
not make their physical cost irrelevant. The project asks a deliberately concrete question:

> **Approximately how much LLM computation, electricity, and associated greenhouse-gas emissions
> were used to build and maintain this repository?**

The goal is not false precision. The goal is to turn an invisible externality into a defensible
order-of-magnitude estimate that can improve as better evidence becomes available.

A repository is a useful accounting boundary because it is a durable artifact around which work,
commits, releases, and maintenance accumulate. It is not a perfect causal boundary, but it is much
more actionable than discussing “AI energy use” only as an industry-wide abstraction.

## Why ballpark estimates matter

Waiting for perfect provider telemetry would leave most real development unmeasured. A broad but
explicit interval is more useful than either:

- pretending the cost is zero because it is not directly metered; or
- presenting a precise-looking number based on hidden assumptions.

The tool therefore separates three layers:

1. **Observation** — model names, token categories, timestamps, tool-call counts, and other signals
   recorded by the agent runtime.
2. **Allocation** — the policy that associates observed work with a repository, commit, pending
   work bucket, activity, or other artifact.
3. **Modeling** — explicit assumptions that turn observed work into intervals for serving time,
   operational electricity, and CO2e.

Measured rows are never rewritten with estimates. Modeling reports are regenerable, versioned by
an assumptions digest, and expected to change as the evidence improves.

## What the primary result means

The principal quantity is:

> **Gross attributed operational LLM-serving footprint for work recorded in this repository.**

“Gross” means the result does not subtract a counterfactual such as human workstation use, avoided
experiments, or avoided travel. “Attributed” means repository assignment follows a declared
accounting policy rather than a claim of perfect causality. “Operational” means the initial model
covers serving electricity and data-center overhead; training, hardware manufacture, networks,
client devices, and water are separate boundary extensions.

This phrasing is intentionally narrower than “the climate impact of AI development.” It states what
the evidence supports while still answering the practical question that motivated the project.

## Design principles

### Measure first, model later

Record the signals emitted by the agent runtime verbatim. Keep assumptions out of the measured
ledger so historical data can be reevaluated under a better model.

### Preserve uncertainty

Report ranges rather than a single authoritative-looking value. The default assumptions are a
wide exploratory scenario, not a provider benchmark. Narrower ranges require stronger evidence.

### Make boundaries visible

Every report should state what it includes, what it excludes, what is unknown, and which allocation
policy was used. Two numbers with different system boundaries are not directly comparable.

### Prefer a useful lower-resolution answer to an unknowable exact answer

The project should support increasingly strong evidence tiers, but it must remain useful when only
client-side token telemetry is available. A broad estimate with a clear evidence grade is still an
estimate; an unreported footprint is effectively treated as zero.

### Avoid moralizing individual work

The ledger is infrastructure for measurement and aggregate reasoning, not a score of developer
virtue. The useful questions are comparative and systemic: which workflows are expensive, where
better models or tools reduce waste, how much AI-assisted development contributes to an
organization's footprint, and whether that trajectory is compatible with climate goals.

## Intended uses

The data should eventually support questions such as:

- What is the lifetime operational LLM footprint of this repository?
- How much uncertainty comes from missing sessions versus serving-model assumptions?
- Which agents, models, activities, or development phases dominate the estimate?
- How does footprint scale with repository growth, releases, and maintenance?
- Does a workflow change reduce resource use for equivalent outcomes?
- What fraction of an organization's software-development footprint is attributable to LLM use?
- How sensitive are conclusions to grid region, serving hardware, batching, or model routing?

It should not be used to claim exact provider energy consumption, net climate benefit, or developer
efficiency without the additional evidence those claims require.

## Success criterion

The project succeeds when LLM-assisted software development has a transparent accounting trail
that is good enough to constrain the plausible answer. The interval may initially be wide. It
should nonetheless be reproducible, auditable, difficult to accidentally double-count, and clear
about which new evidence would narrow it.

## Optional mitigation accounting

After estimating gross attributed emissions, the tool can estimate the separate cost of purchasing
and retiring carbon credits or funding carbon removal. This is useful for budgeting and for making
the scale of the footprint concrete. The mitigation action remains a separate account: it does not
rewrite the gross footprint, and the report should preserve the provider, project, price, delivery,
and retirement evidence. See [Carbon-credit and carbon-removal cost scenarios](carbon-credits-and-removal.md).
