# Related work

The Claude-Code usage-tracking space is real and worth knowing before you adopt this. Most
existing tools are **ephemeral viewers**; this one is a **committed, per-commit ledger**. The
distinction that motivated building it is the *combination* of three things no other tool does
together: a version-controlled ledger **committed inside each repo**, **token/energy/carbon**
accounting (not dollars), and a **lifetime-cumulative** framing over a repo's whole history.

- **[ccusage](https://github.com/ccusage/ccusage)** (~17k★) — a read-only CLI that parses the
  local JSONL logs Claude Code (and 11+ other agent CLIs) already write and prints token/USD
  reports grouped by session, day/week/month, or project. It **persists nothing** and never
  touches git — an ad-hoc analytics viewer, not durable accounting. Reach for it when you just
  want to *see* recent spend; reach for this when you want a versioned record that travels with
  the code.
- **[claude-budget](https://github.com/mooracle/claude-budget)** — a close analog: it also
  attributes Claude usage to individual **git commits**, via a hook. It writes a **USD cost
  trailer into the commit message** (e.g. `Claude-Cost: 0.42`), Claude-only. This tool instead
  writes a structured **token** ledger to a committed file (dollars/energy/carbon are derived
  post-hoc from the measured tokens + commit timestamp), keeps raw measurements only, and is
  built to grow more backends than Claude.
- **[llm-usage-metrics](https://github.com/ayagmar/llm-usage-metrics)** — the closest in spirit:
  a multi-tool CLI that *correlates* spend with git, reporting **$/commit** and **$/1k lines**
  and even replaying your token mix against alternative models. But it **computes those ratios
  on demand** from one machine's local session logs — it persists no per-commit record into the
  repo. This tool inverts that: it writes the durable per-commit record *first* (committed to
  version control, so it travels with the repo and survives the machine), and leaves ratios and
  pricing to a regenerable modeling pass over that record.
- **[Claude Code Analytics](https://code.claude.com/docs/en/analytics)** (official) — Anthropic's
  cloud dashboards for org admins: usage, accept-rate, spend, and a GitHub integration that
  labels *merged PRs* `claude-code-assisted`. Hosted in Anthropic's cloud, Claude-only, **PR
  granularity, nothing persisted into the repo**. Complementary at a different altitude: org-wide
  rollups you don't own vs. a per-commit record the repo carries itself.
- **Live monitors** — [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor),
  `claude-usage` dashboards, and Claude Code's own `/usage`, `/cost`, `/context` — real-time
  burn-rate and limit warnings. Complementary: those answer "am I about to hit a cap *now*?";
  this answers "what did this repo cost to build, cumulatively?"
- **Attribution trailers** (`Co-authored-by:` model lines, `llm-git`) record *which model*
  authored a commit, not what it *consumed* — orthogonal, and easily used alongside this.
