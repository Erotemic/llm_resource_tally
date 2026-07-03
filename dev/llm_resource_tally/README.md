# llm_resource_tally — measured LLM resource accounting (per commit)

A small, **self-contained, self-installing** tool (Python stdlib only — no pip installs)
that records the compute each commit cost an LLM agent, so a repo's lifetime *resource
utilization* can be estimated. Token counts and model are **measured** verbatim from the
agent's own session transcript; energy and carbon are derived later from the recorded
tokens/time and the commit timestamp (which fixes the grid's carbon intensity at that
moment).

Once installed into a repo, the tool lives in one self-contained folder:

```
dev/llm_resource_tally/          # (name/location configurable — see Install)
├── llm_resource_tally.py        # the tool
├── install.sh                   # `curl | sh` bootstrap: vendor + wire up a repo
├── VERSION                      # single source of version truth
├── hooks/post-commit            # auto-records after each commit (self-locating)
├── data/                        # the ledger + rollup (committed; see "Where data lives")
│   ├── resource-ledger.jsonl    #   append-only ledger — the source of truth
│   └── lifetime-totals.yaml     #   rollup output (generated)
└── README.md                    # you are here
```

## Install

Pick **one** route. Both leave you with the same wiring (a post-commit hook + a managed
block in `AGENTS.md`); they differ only in how the code lands in your repo.

**A. Vendor it (`curl | sh`)** — copies the code into your repo (recommended default):
```bash
curl -fsSL https://raw.githubusercontent.com/Erotemic/llm_resource_tally/main/install.sh | sh
```
Vendors the code into `dev/llm_resource_tally/` (code only — never `data/`) and then
wires the repo up offline. Override the target with env vars:
```bash
RT_DIR=tools/rt RT_REF=v1.2.3 curl -fsSL …/main/install.sh | sh
```
`RT_DIR` = where to vendor · `RT_REF` = tag/branch/sha to pin · `RT_REPO` = source `owner/name`.

**B. Add it as a submodule** — if you prefer to track upstream by ref:
```bash
git submodule add https://github.com/Erotemic/llm_resource_tally dev/llm_resource_tally
python3 dev/llm_resource_tally/llm_resource_tally.py install
```
(Run `install` again after `git submodule update --remote` to re-wire on upgrade.)

**Re-wire a repo that already has the folder (no network):**
```bash
python3 dev/llm_resource_tally/llm_resource_tally.py install
```
`install` is idempotent and offline: it sets up the git post-commit hook and writes a
version-stamped managed block into `AGENTS.md`, regenerating an older block in place on
upgrade. Hooks are wired safely — it shares via `core.hooksPath` only when that won't
shadow an existing hook, otherwise it appends a sentinel-guarded block to your active
`post-commit` (husky/lefthook/`.git/hooks` are never clobbered). Options:
`--dir DIR`, `--hook-mode {auto,hookspath,append,none}`, `--agents-file FILE`.

**Update / uninstall:**
```bash
python3 dev/llm_resource_tally/llm_resource_tally.py update      # re-vendor latest, then re-install
python3 dev/llm_resource_tally/llm_resource_tally.py uninstall   # remove wiring; keeps data/ + files
```
(`update` is for the vendored route; for a submodule, bump it with `git submodule update`.)

## Usage

```bash
python3 dev/llm_resource_tally/llm_resource_tally.py record                  # attribute new turns -> HEAD
python3 dev/llm_resource_tally/llm_resource_tally.py record --label planning # tag what the work was
python3 dev/llm_resource_tally/llm_resource_tally.py rollup                  # refresh lifetime totals
python3 dev/llm_resource_tally/llm_resource_tally.py show                    # print the ledger
python3 dev/llm_resource_tally/llm_resource_tally.py reconcile --label review  # sweep un-committed turns
```
With the hook installed you normally only ever run `rollup` (at session end).
**Codex / non-Claude agents:** point the tool at your own log with
`record --transcript <path/to/session.jsonl>`.

**Tagging (`--label`).** Every measured/reconciled row carries an `activity` field, so
work that never lands as code — planning, design, review, debugging — is still counted
*and* attributable. `rollup` breaks output tokens down `by_activity`. A pure
planning/conversation session is captured by `reconcile --label planning` even with no
commit (see "No undercount" below).

## Where the data lives

The ledger must be committed to **your** repo. So:

- **Vendored** (route A): the folder is part of your repo — data lives inside it at
  `dev/llm_resource_tally/data/`, keeping the whole tool in one self-contained folder.
- **Submodule** (route B): the folder's files belong to the *submodule's* repo, not
  yours, so the ledger is written to `.llm_resource_tally/` at your repo root instead
  (committing it under the submodule would stage it against upstream). The tool detects
  this automatically — you don't configure anything.

## Design principle: the ledger stores MEASUREMENTS only

Every modeling choice is deferred to a regenerable post-hoc pass, and **nothing that
requires an assumption is baked into the ledger**:

- **Measured & stored:** model, input/cache-write/cache-read/output tokens, server-tool
  calls, wall-clock span, turn timestamps. For context-compaction (which the harness
  does not bill), the two measured signals a later pass needs: peak preceding context
  and summary length in chars.
- **NOT stored (modeled post-hoc):** inference-seconds (needs a throughput assumption),
  compaction token cost (needs a chars→tokens assumption), energy, carbon. Where a value
  isn't observed we record `null`, never an imputed default.

Because only the raw observations are kept, any modeling decision can change later
without re-recording. The rollup is itself such a post-hoc pass, so it too reports only
measurements (plus the compaction signals) and names what is left for the modeling pass.

## Self-replicating installs

**An install is the source of truth.** Everything needed to (re)install lives in the
vendored folder and is committed into your repo, so `install`/`record` work with **zero
network**. Hosting (`Erotemic/llm_resource_tally`) is only a convenience for the first
`curl | sh` and for `update`. If it ever goes away, every repo that already has the
folder keeps working — and can seed a new repo offline:
```bash
cp -r dev/llm_resource_tally /other/repo/dev/ \
  && (cd /other/repo && python3 dev/llm_resource_tally/llm_resource_tally.py install)
```

**This repo is the source, not an install.** The tool's code lives at the repo *root*
(`llm_resource_tally.py`, `install.sh`, `hooks/`, …); that root is what `curl | sh`
vendors and what the submodule route clones. The root is inert — it is not itself wired
to a hook. To dogfood, the tool is *installed into its own repo* under
`dev/llm_resource_tally/` (a normal vendored install that tracks this repo's own
development, and is itself self-replicating). So: the repo replicates *out*; the install
*within* it replicates like any other.

## Correctness guarantees

- **No double-count under concurrency.** Usage is attributed **per session** (each
  agent = its own transcript file = disjoint turns), and rows are keyed
  `(session_id, commit)`. The ledger is append-only under an `flock`; re-recording a
  `(session, commit)` pair is a no-op without `--force`.
- **No undercount.** `record` sweeps a session's turns in
  `(last-watermark, commit_ts]`; the next commit continues from that watermark, so no
  turn is dropped or counted twice. `reconcile` sweeps any un-committed trailing
  turns into a `pending@…` row so work that never produced a commit is still counted.
- **Dedup by message id.** A transcript logs each assistant message several times with
  *identical* usage; summing raw records overcounts (~2.6× on cache reads). The tool
  dedups by `message.id` — do not hand-count tokens.
- **The ledger tip trails the commit tip by one row, by design** — recording commit
  *N* modifies the ledger, which needs commit *N+1*. This is a fixed point, not a bug.

## Context compaction (measured signals, cost imputed later)

When `/compact` fires, the harness runs a real summarization call over the *entire*
history but writes **no `usage` object** for it — only a `type=system,
subtype=compact_boundary` marker and a `type=user, isCompactSummary=true` record holding
the summary text. So it is invisible to the usage stream. Rather than fabricate a token
count, `record`/`reconcile` add a `kind: compaction-estimate`, `source: reconstructed`
row per boundary carrying only the **measured** signals — `peak_context_tokens` (peak
pre-boundary context the summarizer read) and `summary_chars` (summary length) — keyed
by boundary timestamp so it is never double-counted. `rollup` reports these raw under
`compaction_signals`; the chars→tokens and energy conversions happen in the post-hoc
modeling pass. Disable with `--no-estimate-compaction`.

The parser also counts *any* record carrying a real `usage` object (not just
`type: assistant`), so if a future harness version *does* log compaction usage, it is
measured automatically. The remaining true blind spot is an op whose usage is never
written **and** leaves no transcript marker to reconstruct from; measuring those needs
billing data.

The `AGENTS.md` block is managed automatically by `install` — you do not paste it by
hand. Re-running `install` after an `update` regenerates that block in place.
