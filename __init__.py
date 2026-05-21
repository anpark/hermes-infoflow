"""hermes-infoflow: Baidu Infoflow (如流) channel plugin for Hermes Agent.

This file lives at the **repo root** so that ``hermes plugins install``
(which does a plain ``git clone + move``) finds ``__init__.py`` at the
plugin dir root — exactly where hermes-agent's directory loader
(``hermes_cli/plugins.py::_load_directory_module``) expects it.

The actual implementation lives in the ``hermes_infoflow/`` sub-package
(which is also the pip-installed package via the ``hermes_agent.plugins``
entry point).  This file simply re-exports the public API from that
sub-package so hermes-agent can ``register(ctx)`` regardless of how
the plugin was installed:

    A. ``hermes plugins install``  →  git clone → root ``__init__.py``
    B. ``hermes-infoflow-tools``   →  extract / pip → ``hermes_infoflow/``
    C. ``pip install``             →  entry-point → ``hermes_infoflow/``
    D. ``scripts/deploy.sh``       →  rsync flatten → root ``__init__.py``

**Maintenance rules (read before editing):**

1. ``hermes_infoflow/__init__.py`` is the **canonical source** of public
   exports.  This file must mirror its ``__all__`` and ``__version__``.
2. When you add a new public symbol to ``hermes_infoflow/__init__.py``,
   add the matching ``from .hermes_infoflow.xxx import yyy`` line here.
3. When you bump ``__version__`` in ``hermes_infoflow/__init__.py``,
   bump it here too.
4. Never import from sibling ``*.py`` files at the repo root via relative
   imports — they don't exist in the pip-installed wheel.  Everything
   must go through ``hermes_infoflow``.
"""

# Re-export the public API from the canonical sub-package. Prefer a relative
# import so hermes-agent can load this repo as an isolated plugin directory
# without requiring the plugin root to be on sys.path. The absolute fallback
# keeps pytest collection from importing this file as a nameless top-level
# ``__init__`` module and failing before tests run.
try:
    from .hermes_infoflow import (  # type: ignore[import-not-found] # noqa: F401
        __all__,
        __version__,
        recall_inbound_message_id_hint_scope,
        register,
    )
except ImportError:
    from hermes_infoflow import (  # noqa: F401
        __all__,
        __version__,
        recall_inbound_message_id_hint_scope,
        register,
    )

# Ensure ``__all__`` on this module matches the sub-package so that
# ``from hermes_plugins.infoflow import *`` works identically.
__all__ = list(__all__)
