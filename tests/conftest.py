"""Shared pytest fixtures and import-time shims for hermes-infoflow tests.

The hermes-infoflow plugin imports from ``gateway.*`` / ``hermes_cli.*`` at
runtime (those come from hermes-agent's source tree). For unit tests of the
*plugin itself* — crypto, parser, policy, api, config-writer, tools CLI —
we deliberately avoid importing the real hermes-agent codebase so the tests
can run in a clean Python environment.

Adapter / registration tests that *do* need ``gateway.platforms.base`` are
guarded with ``pytest.importorskip`` so they no-op when hermes-agent isn't
installed alongside this plugin.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the in-tree package importable without an editable install. Also
# expose tools/hermes-infoflow-tools/ so tests can import hermes_infoflow_tools.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_PKG = _REPO_ROOT / "tools" / "hermes-infoflow-tools"
for path in (_REPO_ROOT, _TOOLS_PKG):
    s = str(path)
    if s not in sys.path:
        sys.path.insert(0, s)
