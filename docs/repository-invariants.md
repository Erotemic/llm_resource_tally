# Repository invariants

This repository uses its own recorder. Accounting-only commits are expected: they demonstrate
the publication workflow and preserve the measured cost of maintaining the tool. Published ledger
rows and lifetime totals are tracked so that accounting survives this workstation and can be
reviewed with the code. Do not squash away accounting commits or treat these files as cleanup noise.

`llm_resource_tally/` is the authoritative source. The checked-in `.llm_resource_tally/tool` is
the generated deployment artifact used by this repository, just as it is by consumer repositories.
It must stay in sync with source; reviewers should normally read source and use the parity test
instead of studying the zipapp as a second implementation.

The self-vendored artifact deliberately uses uncompressed `ZIP_STORED` members. Source and tool
change together often, and uncompressed members allow Git to delta successive artifacts. The
`zipapp-deflate` option serves other installations; it is not this repository's format.

Cleanup here optimizes the amount of architecture a maintainer must understand, not checkout or
Git-pack bytes. Keep evidence and correctness tests; remove stale explanations and unnecessary
concepts instead.
