# SPDX-License-Identifier: Apache-2.0
"""Repo-root shim: makes the WHOLE repo runnable by path (`python3 <repo-dir> …`), which
is what the git-submodule install route relies on — a submodule is a clone of this repo,
so its top level is the repo root, not the package. Vendored/pip installs instead point
directly at the package (llm_resource_tally/), which has its own __main__.py; this shim
just forwards into that package sitting beside it. Not shipped in the wheel."""
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from llm_resource_tally.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
