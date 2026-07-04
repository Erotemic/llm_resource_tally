# SPDX-License-Identifier: Apache-2.0
"""`fleet` — one report across many repos' ledgers.

Each repo carries its own committed `.llm_resource_tally/ledger/`, so an org-wide view needs no
server and no retention window: point `fleet` at a set of repos (or a directory to scan) and it
reads each committed ledger and sums them. The grand total is the SUM of per-repo totals (each
repo's ledger is already de-duplicated internally) — never a re-dedup across repos, which would
wrongly collapse distinct observations that happen to share a session id.
"""
from __future__ import annotations

import glob
import json
import os

from .ledger import read_ledger, shard_paths_in
from .rollup import compute_totals, human

_NUM = ("turns", "output", "billable_input", "commits", "wall_s")


def discover_repos(root: str) -> list[str]:
    """Repos under `root` that carry a ledger (`*/.llm_resource_tally/ledger/`)."""
    hits = glob.glob(os.path.join(root, "**", ".llm_resource_tally", "ledger"), recursive=True)
    repos = {os.path.dirname(os.path.dirname(h)) for h in hits}
    return sorted(repos)


def _repo_row(repo: str) -> dict:
    dd = os.path.join(repo, ".llm_resource_tally")
    tot = compute_totals(read_ledger(shard_paths_in(dd)))
    tk = tot["tokens"]
    return {"repo": os.path.basename(os.path.abspath(repo)),
            "turns": tot["turns"], "output": tk["output"],
            "billable_input": tk["billable_input"],
            "commits": tot["commits_accounted"],
            "wall_s": round(tot["time"]["wall_clock_s"], 1),
            "models": ",".join(sorted(tot["by_model"]))}


def resolve_repos(paths: list[str]) -> list[str]:
    """Each path is either a repo (has `.llm_resource_tally/ledger`) used directly, or a
    directory scanned for repos beneath it."""
    repos: list[str] = []
    for p in paths:
        if os.path.isdir(os.path.join(p, ".llm_resource_tally", "ledger")):
            repos.append(p)
        else:
            repos.extend(discover_repos(p))
    return list(dict.fromkeys(repos))


def aggregate(paths: list[str]) -> dict:
    rows = [_repo_row(r) for r in resolve_repos(paths)]
    total = {k: 0 for k in _NUM}
    for r in rows:
        for k in _NUM:
            total[k] += r[k]
    total["wall_s"] = round(total["wall_s"], 1)
    return {"repos": rows, "total": total}


def _fmt(agg: dict, fmt: str) -> str:
    rows = agg["repos"]
    if fmt == "json":
        return json.dumps(agg, indent=2, ensure_ascii=False)
    headers = ["repo", "turns", "output", "billable_in", "commits", "wall_s"]
    keys = ["repo", "turns", "output", "billable_input", "commits", "wall_s"]
    body = [[str(r[k]) for k in keys] for r in rows]
    body.append(["TOTAL"] + [str(agg["total"][k]) for k in keys[1:]])
    if fmt == "tsv":
        return "\n".join("\t".join(r) for r in [headers, *body])
    widths = [max(len(headers[i]), *(len(r[i]) for r in body)) for i in range(len(headers))]
    if fmt == "md":
        line = lambda vals: "| " + " | ".join(v.ljust(widths[i]) for i, v in enumerate(vals)) + " |"
        sep = "| " + " | ".join("-" * widths[i] for i in range(len(headers))) + " |"
        return "\n".join([line(headers), sep, *(line(r) for r in body)])
    rs = lambda vals: "  ".join(v.ljust(widths[i]) for i, v in enumerate(vals))
    return "\n".join([rs(headers), *(rs(r) for r in body)])


def cmd_fleet(args) -> None:
    paths = args.paths or [os.getcwd()]
    agg = aggregate(paths)
    if not agg["repos"]:
        print("no repos with a ledger found under: " + ", ".join(paths))
        return
    print(_fmt(agg, args.fmt))
    if args.fmt in ("table", "md"):
        t = agg["total"]
        print(f"\n# {len(agg['repos'])} repos · {human(t['output'])} output tok · "
              f"{human(t['turns'])} turns · {t['commits']} commits")
