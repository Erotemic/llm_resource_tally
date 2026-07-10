# SPDX-License-Identifier: Apache-2.0
"""Command-line interface: argument parsing and dispatch only."""
from __future__ import annotations

import argparse

from .backends import DEFAULT_BACKEND, backend_names
from .backends.claude_hook import cmd_hook
from .doctor import cmd_doctor
from .fleet import cmd_fleet
from .install import cmd_install, cmd_uninstall, cmd_update
from .modeling_bridge import cmd_estimate
from .record import cmd_record, cmd_reconcile
from .report import cmd_report
from .rollup import cmd_rollup, cmd_show
from .version import CANONICAL_REPO
from .zipapp_artifact import cmd_build_zipapp


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="llm_resource_tally",
                                description="Measured LLM-usage accounting.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--backend", default=None,
                        help=f"agent backend (default {DEFAULT_BACKEND}; known: "
                             f"{', '.join(backend_names())})")
        sp.add_argument("--transcript", help="explicit session transcript path")
        sp.add_argument("--session", help="session id (transcript filename stem)")
        sp.add_argument("--projects-dir", default=None,
                        help="transcripts base dir (default: backend-specific)")
        sp.add_argument("--label", default=None,
                        help="tag this work (e.g. planning, implementation, review); "
                             "stored as the row's `activity`")
        sp.add_argument("--no-estimate-compaction", action="store_true",
                        help="do not add reconstructed rows for context-compaction events")

    r = sub.add_parser("record", help="attribute new turns to a commit")
    r.add_argument("--commit", default="HEAD")
    r.add_argument("--force", action="store_true")
    common(r)
    r.set_defaults(func=cmd_record)

    rc = sub.add_parser("reconcile", help="sweep un-recorded trailing turns")
    common(rc)
    rc.set_defaults(func=cmd_reconcile)

    ru = sub.add_parser("rollup", help="refresh lifetime totals from the ledger")
    ru.set_defaults(func=cmd_rollup)

    sh = sub.add_parser("show", help="print the ledger")
    sh.set_defaults(func=cmd_show)

    rp = sub.add_parser("report", help="human-readable views over the ledger")
    rp.add_argument("--by", choices=["commit", "day", "activity", "agent", "model"],
                    default="commit", help="grouping (default commit)")
    rp.add_argument("--format", choices=["table", "md", "tsv", "json"], default="table",
                    dest="fmt", help="output format (default table)")
    rp.add_argument("--commits", default=None,
                    help="only rows for commits in this git range (e.g. main..HEAD) — the "
                         "cost of a branch or PR")
    rp.set_defaults(func=cmd_report)

    es = sub.add_parser("estimate",
                        help="model energy/carbon/USD from the ledger + an assumption pack "
                             "(needs the modeling package; see `install --modeling`)")
    es.add_argument("--pack", default=None,
                    help="assumption-pack JSON (default: built-in pack)")
    es.add_argument("--region", default=None,
                    help="fix grid carbon intensity to a region/ISO3 country code (e.g. USA, "
                         "FRA, NOR) using the pack's grid.by_region — see grid-codecarbon.json")
    es.add_argument("--mitigation", nargs="?", const="builtin", default=None, metavar="PATH",
                    help="also price typed mitigation scenarios; omit PATH for the built-in "
                         "avoidance/nature/biochar/geological scenarios")
    es.add_argument("--format", choices=["text", "json"], default="text", dest="fmt")
    es.set_defaults(func=cmd_estimate)

    dr = sub.add_parser("doctor", help="check hook wiring, backends, retention, ledger health")
    dr.set_defaults(func=cmd_doctor)

    fl = sub.add_parser("fleet", help="aggregate many repos' ledgers into one report")
    fl.add_argument("paths", nargs="*",
                    help="repos and/or dirs to scan for repos (default: cwd)")
    fl.add_argument("--format", choices=["table", "md", "tsv", "json"], default="table",
                    dest="fmt")
    fl.set_defaults(func=cmd_fleet)

    ins = sub.add_parser("install", help="wire git hook + AGENTS.md (offline, idempotent)")
    ins.add_argument("--dir", default=None,
                     help="tool path relative to repo root (directory for source format, .pyz file "
                          "for zipapp; default: portable installation policy)")
    ins.add_argument("--tool-format", choices=["zipapp", "source"], default=None,
                     help="installed tool representation (default: installation policy in "
                          ".llm_resource_tally/settings.json; otherwise zipapp)")
    ins.add_argument("--hook-mode", choices=["auto", "hookspath", "append", "none"],
                     default="auto", help="how to install the post-commit hook")
    ins.add_argument("--agents-file", default="AGENTS.md",
                     help="doc to carry the managed block (default AGENTS.md)")
    ins.add_argument("--claude", action="store_true",
                     help="also wire a Claude Code PostToolUse(Bash) hook into "
                          ".claude/settings.json for correct cross-repo attribution")
    ins.add_argument("--backend", default=None,
                     help="register a backend the passive hook should record; unioned into "
                          ".llm_resource_tally/settings.json (fresh repos default to claude+codex)")
    ins.add_argument("--modeling", action=argparse.BooleanOptionalAction, default=None,
                     help="include or omit the optional modeling subpackage (default: portable "
                          "installation policy)")
    ins.add_argument("--storage", choices=["committed", "ignored", "notes"], default=None,
                     help="ledger/state storage mode (default: portable installation policy; "
                          "otherwise committed)")
    ins.set_defaults(func=cmd_install)

    un = sub.add_parser("uninstall", help="remove hook wiring + AGENTS.md block (keeps data)")
    un.add_argument("--dir", default=None)
    un.add_argument("--agents-file", default="AGENTS.md")
    un.set_defaults(func=cmd_uninstall)

    up = sub.add_parser("update", help="fetch and reinstall, optionally changing repository policy")
    up.add_argument("--repo", default=CANONICAL_REPO, help="GitHub owner/name source")
    up.add_argument("--ref", default="main", help="tag/branch/sha to install (default main)")
    up.add_argument("--dir", default=None,
                    help="replace the stored repository-relative tool path")
    up.add_argument("--tool-format", choices=["zipapp", "source"], default=None,
                    help="replace the stored artifact format")
    up.add_argument("--storage", choices=["committed", "ignored", "notes"], default=None,
                    help="replace the stored ledger/state mode")
    up.add_argument("--modeling", action=argparse.BooleanOptionalAction, default=None,
                    help="include or omit modeling in the replacement artifact")
    up.set_defaults(func=cmd_update)

    bz = sub.add_parser("build-zipapp", help="build a deterministic standalone .pyz artifact")
    bz.add_argument("--output", required=True, help="destination .pyz path")
    bz.add_argument("--modeling", action="store_true",
                    help="include the optional energy/carbon modeling package and assumptions")
    bz.set_defaults(func=cmd_build_zipapp)

    hk = sub.add_parser("hook", help="internal: Claude PostToolUse handler (reads JSON on stdin)")
    hk.add_argument("--projects-dir", default=None)
    hk.set_defaults(func=cmd_hook)

    args = p.parse_args(argv)
    args.func(args)
