# SPDX-License-Identifier: Apache-2.0
"""Entry point for both `python -m llm_resource_tally` (pip) and running the vendored
package dir by path (`python3 dev/llm_resource_tally record`). In the by-path case Python
runs THIS file as top-level `__main__` with no package context, so we put the package's
PARENT on sys.path and import by absolute name; under `-m` the insert is a harmless no-op.
"""
import os
import sys

_pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _pkg_parent not in sys.path:
    sys.path.insert(0, _pkg_parent)

from llm_resource_tally.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
