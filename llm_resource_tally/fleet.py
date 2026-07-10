# SPDX-License-Identifier: Apache-2.0
"""`fleet` — one measurement report across many repositories.

Committed/ignored file ledgers and git-notes ledgers are both readable. Totals are summed per
repository; observations are not de-duplicated across repositories because allocation to a repo
is itself part of the accounting record.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess

from .gitutil import git
from .ledger import read_ledger
from .rollup import compute_totals, human
from .storage import notes_ref

_NUM = ("turns", "output", "billable_input", "commits", "wall_s")


def _has_notes(repo: str) -> bool:
    try:
        return bool(git("notes", f"--ref={notes_ref(repo)}", "list", cwd=repo).strip())
    except (subprocess.CalledProcessError, OSError):
        return False


def _is_repo(path: str) -> bool:
    try:
        git("rev-parse", "--git-dir", cwd=path)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def _has_ledger(repo: str) -> bool:
    return (os.path.isdir(os.path.join(repo, ".llm_resource_tally", "ledger"))
            or _has_notes(repo))


def discover_repos(root: str) -> list[str]:
    """Repositories under ``root`` carrying either file or git-notes measurements."""
    candidates = {os.path.dirname(os.path.dirname(h)) for h in
                  glob.glob(os.path.join(root, "**", ".llm_resource_tally", "ledger"),
                            recursive=True)}
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames or ".git" in filenames:
            candidates.add(dirpath)
            if ".git" in dirnames:
                dirnames.remove(".git")
        dirnames[:] = [d for d in dirnames if d not in {".git", ".venv", "venv", "node_modules"}]
    return sorted(r for r in candidates if _is_repo(r) and _has_ledger(r))


def _repo_row(repo: str) -> dict:
    tot = compute_totals(read_ledger(root=repo))
    tk = tot["tokens"]
    return {"repo": os.path.basename(os.path.abspath(repo)),
            "path": os.path.abspath(repo),
            "turns": tot["turns"], "output": tk["output"],
            "billable_input": tk["billable_input"],
            "commits": tot["commits_accounted"],
            "wall_s": round(tot["time"]["wall_clock_s"], 1),
            "models": ",".join(sorted(tot["by_model"]))}


def resolve_repos(paths: list[str]) -> list[str]:
    repos: list[str] = []
    for path in paths:
        path = os.path.abspath(path)
        if _is_repo(path) and _has_ledger(path):
            repos.append(path)
        elif os.path.isdir(path):
            repos.extend(discover_repos(path))
    return list(dict.fromkeys(repos))


def aggregate(paths: list[str]) -> dict:
    rows = [_repo_row(r) for r in resolve_repos(paths)]
    total = {k: 0 for k in _NUM}
    for row in rows:
        for key in _NUM:
            total[key] += row[key]
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
