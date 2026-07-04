# SPDX-License-Identifier: Apache-2.0
"""`report` — human-first views over the committed ledger.

`show` is the raw per-row dump; `report` aggregates the ledger into the views people actually
want (per commit / day / activity / agent / model) in a few formats (aligned table, Markdown,
TSV, JSON). It reads only the committed ledger, so it works on any clone, years later, with no
session logs present — which is the whole point of a committed ledger over an ephemeral viewer.
Measurements only; energy/carbon/USD come from `estimate`.
"""
from __future__ import annotations

import json

from .ledger import read_ledger
from .schema import COMPACTION_KIND

_KINDS = ("input", "cache_write", "cache_read", "output", "billable_input")
_COLUMNS = [("group", "group"), ("turns", "turns"), ("output", "output"),
            ("billable_input", "billable_in"), ("wall_clock_s", "wall_s"),
            ("models", "models")]


def _blank() -> dict:
    g = {k: 0 for k in _KINDS}
    g.update(turns=0, wall=0.0, models=set())
    return g


def _add(g: dict, r: dict) -> None:
    tk = r.get("tokens", {})
    for k in _KINDS:
        g[k] += tk.get(k, 0)
    g["turns"] += r.get("turns", 0)
    g["wall"] += r.get("time", {}).get("wall_clock_s") or 0
    g["models"].update(r.get("models", []))


def _key(r: dict, by: str) -> str:
    if by == "commit":
        return (r.get("commit") or "")[:10]
    if by == "day":
        return (r.get("recorded_at") or "")[:10]
    if by == "activity":
        return r.get("activity") or "unlabeled"
    return r.get("agent") or "unknown"                 # agent


def aggregate_rows(rows: list[dict], by: str) -> list[dict]:
    """Group measured rows by the chosen dimension into flat dicts ready to format."""
    groups: dict[str, dict] = {}
    for r in rows:
        if r.get("kind") == COMPACTION_KIND:
            continue
        if by == "model":
            for m, mtok in r.get("by_model", {}).items():
                g = groups.setdefault(m, _blank())
                for k in ("input", "cache_write", "cache_read", "output"):
                    g[k] += mtok.get(k, 0)
                g["billable_input"] += (mtok.get("input", 0) + mtok.get("cache_write", 0)
                                        + mtok.get("cache_read", 0))
                g["models"].add(m)
            continue
        _add(groups.setdefault(_key(r, by), _blank()), r)
    out = []
    for key, g in groups.items():
        row = {"group": key, "turns": g["turns"]}
        row.update({k: g[k] for k in _KINDS})
        row["wall_clock_s"] = round(g["wall"], 1)
        row["models"] = ",".join(sorted(g["models"]))
        out.append(row)
    return out


def _fmt(rows: list[dict], fmt: str) -> str:
    headers = [h for _, h in _COLUMNS]
    keys = [k for k, _ in _COLUMNS]
    cells = [[str(r.get(k, "")) for k in keys] for r in rows]
    if fmt == "json":
        return json.dumps(rows, indent=2, ensure_ascii=False)
    if fmt == "tsv":
        return "\n".join("\t".join([*headers] if i == 0 else row)
                         for i, row in enumerate([headers, *cells]))
    if fmt == "md":
        widths = [max(len(headers[i]), *(len(row[i]) for row in cells)) if cells
                  else len(headers[i]) for i in range(len(headers))]
        def line(vals):
            return "| " + " | ".join(v.ljust(widths[i]) for i, v in enumerate(vals)) + " |"
        sep = "| " + " | ".join("-" * widths[i] for i in range(len(headers))) + " |"
        return "\n".join([line(headers), sep, *(line(r) for r in cells)])
    # table
    widths = [max(len(headers[i]), *(len(row[i]) for row in cells)) if cells
              else len(headers[i]) for i in range(len(headers))]
    def row_str(vals):
        return "  ".join(v.ljust(widths[i]) for i, v in enumerate(vals))
    return "\n".join([row_str(headers), *(row_str(r) for r in cells)])


def _resolve_commits(expr: str) -> set:
    """Full SHAs in a git range/expr (e.g. `main..HEAD`), for `--commits` filtering."""
    from .gitutil import git
    try:
        return set(git("rev-list", expr).split())
    except Exception:
        return set()


def cmd_report(args) -> None:
    rows = read_ledger()
    if getattr(args, "commits", None):
        shas = _resolve_commits(args.commits)
        rows = [r for r in rows if (r.get("commit") or "") in shas]
    grouped = aggregate_rows(rows, args.by)
    if not grouped:
        print("[]" if args.fmt == "json" else "ledger is empty.")
        return
    print(_fmt(grouped, args.fmt))
