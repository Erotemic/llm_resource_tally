# Development

The project is stdlib-only at runtime. The authoritative repository is always an ordinary source
tree; zipapps are deterministic deployment artifacts for host repositories.

```text
llm_resource_tally/                                  measurement core
  cli.py        parsing and dispatch              ledger.py      shards/read/dedup/append
  record.py     record and reconcile              schema.py      compact row codec
  rollup.py     rollup and show                    gitutil.py     repository anchoring
  install.py    orchestration                      config.py      backend settings
  vendoring.py  format/target resolution          storage.py     local/committed/ignored/notes
  publish.py    local-spool publication
  zipapp_artifact.py deterministic .pyz build/copy/inspect/enrichment
  wiring_*.py   git, AGENTS, and Claude hooks
  backends/     transcript readers                modeling_bridge.py optional-layer seam
  modeling/                                         optional modeling package
    estimate.py       central + interval estimates and source adapters
    interval.py       non-negative scenario arithmetic
    mitigation.py     typed mitigation price scenarios
    assumptions/*.json baseline, generic-wide, CodeCarbon grid, mitigation scenarios

dev/build_grid_pack.py  freezes CodeCarbon regional data through the production adapter
```

## Core/modeling split

A minimal bootstrap omits `modeling/`. Core therefore never imports it at module load;
`modeling_bridge.py` lazily dispatches `estimate` and can add modeling to a source artifact or
atomically rebuild a minimal zipapp. `RT_MODELING=1`, `install --modeling`, and a full direct build
include the package and its assumption resources.

Bundled JSON is accessed with `importlib.resources`, not `__file__` paths. This is required for
zipimport and also gives wheels and source trees one code path. External user packs remain normal
filesystem paths.

## Invocation forms

The same CLI can run as:

```text
python3 .llm_resource_tally/tool ...  host-repository zipapp or source tree
python3 <source-repository> ...           checkout/submodule root shim
python -m llm_resource_tally ...          installed package
llm_resource_tally ...                    console script
```

`build-zipapp` writes sorted members with fixed timestamps, embeds `VERSION` and
`ZIPAPP-METADATA.json`, prepends a Python shebang, and atomically replaces the destination.
The default `zipapp` format uses uncompressed `ZIP_STORED` members so Git can efficiently delta
successive revisions; `--tool-format zipapp-deflate` uses `ZIP_DEFLATED` members when minimizing
the checked-out artifact matters more. A fixed source tree and format therefore have a stable
SHA-256. `SOURCE_DATE_EPOCH` may select the normalized ZIP timestamp. Metadata records a source
tree digest unconditionally; `source_commit` is recorded only when the package source and `VERSION`
are clean at `HEAD`, so an artifact built from uncommitted code does not claim false commit
provenance. A test also requires this repository's tracked `.llm_resource_tally/tool` digest to
match the authoritative source tree.

## Layers and tests

The modules fall into three layers: **measure** (`backends`, `record`, `ledger`, `schema`,
`claims`), **wire** (`install`, `doctor`, `config`, artifact deployment), and **report** (`rollup`,
`report`, `fleet`, optional modeling).

Run:

```bash
pytest -q tests/test_zipapp.py
pytest -q tests/test_consolidated_features.py
pytest -q tests/test_e2e.py
```

The end-to-end tests create real temporary git repositories. The real-pip test is best-effort
because isolated environments may lack build dependencies or network access. CI covers Python
3.10 through 3.14 plus the current Python 3.15 prerelease until 3.15 becomes stable.

**Platform:** POSIX (Linux/macOS). Git hooks are Bash and ledger locking uses `fcntl`; both are
isolated enough for a future Windows shim, but Windows is not currently supported.
