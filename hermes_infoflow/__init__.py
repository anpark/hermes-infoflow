"""hermes-infoflow: Baidu Infoflow (如流) channel plugin for Hermes Agent.

This is the **canonical** source of public exports.  A mirror ``__init__.py``
at the repo root re-exports everything from this file so that
``hermes plugins install`` (which does a plain ``git clone + move``) works
without flattening.  See the root ``__init__.py`` header comment for the
full maintenance rules.

Loaded by hermes-agent via either of these paths:

1. Entry-point (raw ``pip install hermes-infoflow``)::

       [project.entry-points."hermes_agent.plugins"]
       infoflow = "hermes_infoflow"

   In this case hermes calls ``hermes_infoflow.register(ctx)`` directly
   and never reads ``plugin.yaml``. This path is kept for Hermes discovery
   compatibility, but it is not the preferred deployment shape; run
   ``hermes-infoflow-deploy`` to convert the wheel into the canonical
   directory plugin.

2. Directory install (``~/.hermes/plugins/infoflow/``).

   **Path A — ``hermes plugins install`` + normalize:**

   hermes-agent's directory loader
   (``hermes_cli/plugins.py::_load_directory_module``) loads the root
   ``__init__.py``, which re-exports ``register()`` from this sub-package.
   The loader sets ``submodule_search_locations=[plugin_dir]`` on the
   loaded module, so the clone works before normalization. Running
   ``scripts/normalize.sh`` then flattens it to the same directory layout
   used by every other supported deploy path.

   **Path B/C/D — ``hermes-infoflow-tools update`` /
   ``scripts/deploy.sh`` / ``hermes-infoflow-deploy`` (flattening):**

   These copy this package's contents into the plugin dir root, then drop
   ``plugin.yaml`` and ``scripts/`` on top. The root ``__init__.py`` gets
   overwritten by the flattened copy from this file. Relative imports inside
   the package (``from .adapter import register``) keep resolving because
   hermes-agent sets ``submodule_search_locations=[plugin_dir]`` on the
   loaded module. See ``tests/test_deploy_layout.py`` for the layout contract.

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

__version__ = "2026.5.25"
