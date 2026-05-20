"""hermes-infoflow: Baidu Infoflow (如流) channel plugin for Hermes Agent.

This is the **canonical** source of public exports.  A mirror ``__init__.py``
at the repo root re-exports everything from this file so that
``hermes plugins install`` (which does a plain ``git clone + move``) works
without flattening.  See the root ``__init__.py`` header comment for the
full maintenance rules.

Loaded by hermes-agent via either of these paths:

1. Entry-point (``pip install hermes-infoflow``)::

       [project.entry-points."hermes_agent.plugins"]
       infoflow = "hermes_infoflow"

   In this case hermes calls ``hermes_infoflow.register(ctx)`` directly
   and never reads ``plugin.yaml`` — users must export ``INFOFLOW_*``
   env vars themselves.

2. Directory install (``~/.hermes/plugins/infoflow/``).

   **Path A — ``hermes plugins install`` (no flattening):**

   hermes-agent's directory loader
   (``hermes_cli/plugins.py::_load_directory_module``) loads the root
   ``__init__.py``, which re-exports ``register()`` from this sub-package.
   The loader sets ``submodule_search_locations=[plugin_dir]`` on the
   loaded module, so the ``from hermes_infoflow import ...`` at the root
   resolves against ``plugin_dir/hermes_infoflow/``.

   **Path B/D — ``hermes-infoflow-tools --mode extract`` /
   ``scripts/deploy.sh`` (flattening):**

   These rsync this package's contents into the plugin dir root, then drop
   ``plugin.yaml`` on top.  The root ``__init__.py`` gets overwritten by
   the flattened copy from this file.  Relative imports inside the package
   (``from .adapter import register``) keep resolving because hermes-agent
   sets ``submodule_search_locations=[plugin_dir]`` on the loaded module.
   See ``tests/test_deploy_layout.py`` for the layout contract.

Both paths converge on the same ``register(ctx)`` entry point. The plugin
name MUST be the string ``"infoflow"`` in three places:

    * pyproject.toml entry-point key
    * plugin.yaml ``name`` field
    * register(ctx)(name="infoflow", ...) inside adapter.py

so the plugin manager deduplicates them to a single logical plugin.

**Maintenance:** when adding a new public symbol or bumping ``__version__``,
update BOTH this file and the root ``__init__.py``.
"""

from .adapter import register
from .bot import recall_inbound_message_id_hint_scope

__all__ = ["recall_inbound_message_id_hint_scope", "register"]

__version__ = "0.2.2"
