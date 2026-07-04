# Backfilling historical usage

The hook only records **going forward**, from the moment you install it. If a repo already has
history — commits made before the tool was wired, or a stretch where the hook wasn't running —
that usage isn't in the ledger yet. Backfilling recovers it from the transcripts your agent
already wrote, subject to one hard limit: **you can only recover what's still on disk.**

## The one prerequisite: the transcripts still exist

Everything here reads the agent's own session logs; nothing is reconstructed from thin air. So
backfill reaches exactly as far back as those logs are retained:

- **Claude Code** prunes transcripts after `cleanupPeriodDays` — **default 30 days**. Raise it
  *now* in `~/.claude/settings.json` (e.g. `"cleanupPeriodDays": 3650`) if you want a deep
  baseline later; it can't recover what was already pruned.
- **Codex** keeps sessions under `~/.codex/sessions/` until its own cleanup removes them.

Check what you still have before planning a backfill:
```bash
ls ~/.claude/projects/*/         # Claude session .jsonl files (dirs are munged repo paths)
ls ~/.codex/sessions/            # Codex rollout-*.jsonl files
```

## Approach A — bulk sweep (easy; lifetime totals)

`reconcile` sweeps **every** still-on-disk session for this repo and records all of each
session's un-recorded turns into a `pending@<date>` bucket. Run from the repo:

```bash
<rt> reconcile --label backfill
<rt> rollup
```

This is the fastest way to make your **lifetime totals** whole. What it does *not* do is tie that
usage to specific historical commits — swept turns land in the pending bucket, not on the commits
they produced. It's idempotent (turns are deduped by message id), so re-running only adds what's
new. It walks your [registered backends](backends.md), so Claude and Codex are both swept.

## Approach B — per-commit precision (manual)

If you want historical usage attributed to the exact commits, map each session to the commit it
produced and record them **oldest → newest** (so the per-session watermark advances correctly):

```bash
<rt> record --backend claude --session <session-id> --commit <sha> --label backfill
<rt> record --backend codex  --session <session-id> --commit <sha> --label backfill
# …repeat per (session, commit), oldest first…
<rt> rollup
```

`record` attributes the session's turns in the window `(previous watermark, commit's timestamp]`,
so processing in commit order fills each commit with the turns that preceded it. Point at a
non-default transcript location with `--transcript <path>` or `--projects-dir <dir>` if your logs
aren't in the standard place.

## Limitations (and the honesty rule)

- **Retention is a hard floor.** Nothing older than your transcripts survive is recoverable —
  full stop. This is the main reason to raise `cleanupPeriodDays` today.
- **Session→commit mapping is yours to supply.** The tool can measure a session, but it can't
  infer *which historical commit* a session produced — especially across cross-repo or
  interleaved work. Approach A sidesteps this (bucket, not per-commit); Approach B needs you to
  supply the pairing.
- **Measured signals only.** Backfilled rows use the same schema as live ones: tokens/model are
  verbatim; inference-time, energy, carbon, and compaction token cost stay `null` and are modeled
  post-hoc. Backfill never fabricates numbers.
- **Beyond the horizon: estimate, don't invent.** Usage older than any surviving transcript
  should live in a *separate, clearly-labeled* modeling layer (an estimate), never as fabricated
  rows in the measured ledger. Keeping the ledger measurements-only is what lets every downstream
  number be trusted and recomputed.
- **Safe to re-run.** Dedup by message id (and reader-side row dedup) means repeating a backfill
  never double-counts.
