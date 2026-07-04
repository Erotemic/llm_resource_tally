#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the committed per-region grid pack `grid-codecarbon.json` from CodeCarbon's global
energy mix (per-country carbon intensity, MIT-licensed).

This is a *dev* build tool, not shipped in the package. It deliberately reuses the runtime
`codecarbon-energy-mix` adapter (in `llm_resource_tally.modeling.estimate`) so the committed
snapshot and the adapter can never diverge: the snapshot is just `resolve_source(...)` frozen to
disk, with the source `ref`/`retrieved` stamped into provenance. Stdlib only.

    python3 dev/build_grid_pack.py                       # fetch pinned ref, write the pack
    python3 dev/build_grid_pack.py --from mix.json       # use a local global_energy_mix.json
    python3 dev/build_grid_pack.py --ref v3.2.8 --retrieved 2026-07-04

Re-run it to refresh the data (e.g. bump --ref to a newer CodeCarbon release); commit the diff.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.request
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from llm_resource_tally.modeling.estimate import (normalize_provenance,  # noqa: E402
                                                  resolve_source)

CODECARBON_REPO = "mlco2/codecarbon"
DATA_PATH = "codecarbon/data/private_infra/global_energy_mix.json"
DEFAULT_REF = "v3.2.8"                      # pin to a CodeCarbon release for a reproducible build
OUT = os.path.join(REPO, "llm_resource_tally", "modeling", "assumptions", "grid-codecarbon.json")


def source_url(ref: str) -> str:
    return f"https://raw.githubusercontent.com/{CODECARBON_REPO}/{ref}/{DATA_PATH}"


def fetch(ref: str) -> str:
    """Download global_energy_mix.json to a temp file; return its path."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    url = source_url(ref)
    print(f"fetching {url}", file=sys.stderr)
    urllib.request.urlretrieve(url, path)   # noqa: S310 (github over https)
    return path


def build(mix_path: str, ref: str, retrieved: str) -> dict:
    # The adapter does the real work (raw energy mix -> full pack with grid.by_region + baseline
    # energy/pricing + provenance). We only stamp the human-meaningful source ref + retrieved
    # date, which the adapter can't know (it saw a temp path).
    pack = resolve_source({"adapter": "codecarbon-energy-mix", "ref": mix_path})
    pack["grid"]["by_region"] = dict(sorted(pack["grid"]["by_region"].items()))  # stable diffs
    pack["grid"]["source"] = (f"CodeCarbon global_energy_mix.json @ {CODECARBON_REPO}@{ref} "
                              "(per-country carbon_intensity, gCO2e/kWh)")
    for p in pack.get("provenance", []):
        if p.get("applies_to") == "grid":
            p["ref"] = source_url(ref)
            p["retrieved"] = retrieved
    return pack


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", default=None,
                    help="path to a local global_energy_mix.json (default: fetch pinned --ref)")
    ap.add_argument("--ref", default=DEFAULT_REF, help=f"CodeCarbon git ref (default {DEFAULT_REF})")
    ap.add_argument("--retrieved", default=date.today().isoformat(),
                    help="retrieval date to record (default: today)")
    ap.add_argument("--out", default=OUT, help=f"output pack path (default {OUT})")
    args = ap.parse_args()

    mix_path = args.src or fetch(args.ref)
    pack = build(mix_path, args.ref, args.retrieved)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(pack, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    n = len(pack["grid"]["by_region"])
    prov = ", ".join(p.get("applies_to", "?") for p in normalize_provenance(pack))
    print(f"wrote {args.out}\n  regions: {n}  ·  provenance: {prov}", file=sys.stderr)
    if not args.src:
        os.unlink(mix_path)


if __name__ == "__main__":
    main()
