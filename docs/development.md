# Development

The tool is a stdlib-only package (no runtime deps):

```
llm_resource_tally/                                  ← measurement CORE (minimal curl install)
  cli.py        argument parsing / dispatch        ledger.py    rolling shards, read/dedup/append
  record.py     record / reconcile                 schema.py    compact on-disk row codec
  rollup.py     rollup / show                       gitutil.py   git helpers (repo_root anchors data)
  install.py    install/vendor + hook & AGENTS      config.py    per-repo settings.json (backends)
  backends/     agent-specific transcript readers  modeling_bridge.py  seam to the optional layer
  modeling/                                          ← OPTIONAL modeling package (opt-in, not in curl)
    estimate.py       energy/carbon/USD over an assumption pack; sources/adapters/provenance
    assumptions/*.json  baseline pack + grid-codecarbon (per-region grid)
dev/build_grid_pack.py  builds grid-codecarbon.json from CodeCarbon data (dev tool, reuses the adapter)
```

The **modeling split** is the one structural rule: the `curl | sh` bootstrap vendors the core
*without* `modeling/` (see [install.sh](../install.sh)) so the offline footprint stays tiny.
Core must therefore import cleanly without modeling — it is never imported at module load;
`modeling_bridge.py` is the only seam, lazily dispatching `estimate` (and vendoring the
subpackage on `install --modeling`). `pip install` and `RT_MODELING=1` include it.

`__main__.py` lets the same package run three ways — `python3 .llm_resource_tally/tool …`
(vendored, by path), `python -m llm_resource_tally` (pip), and the `llm_resource_tally` console
script — by registering the directory as the canonical package regardless of its basename. A
second, tiny `__main__.py` at the repo *root* makes the whole repo runnable by path too, which
the git-submodule route relies on (a submodule clones the whole repo, not just the package); it
is not shipped in the wheel.

The modules fall into three layers: **measure** (`backends/`, `record`, `ledger`, `schema`,
`claims`), **wire** (`install`, `doctor`, `config`), and **report** (`rollup`, `report`,
`fleet`, and the optional `modeling/`). New work usually lands in exactly one layer.

Tests are `pytest tests/` (they spin up throwaway git repos and exercise the CLI end-to-end,
including a real venv `pip install`); CI runs them across Python 3.10–3.13
([.github/workflows/test.yml](../.github/workflows/test.yml)).

**Platform:** POSIX (Linux/macOS). The git hook is a `bash` script and the ledger append lock
uses `fcntl`; both are isolated (`_lock`/`_unlock` in `ledger.py`, the hook body in
`install.py`) so a future Windows shim is a small, contained change. It is untested on Windows
today.
