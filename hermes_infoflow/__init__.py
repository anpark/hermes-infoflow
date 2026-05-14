"""hermes-infoflow: Baidu Infoflow (如流) channel plugin for Hermes Agent.

Loaded by hermes-agent via either of these paths:

1. Entry-point::

       [project.entry-points."hermes_agent.plugins"]
       infoflow = "hermes_infoflow"

   In this case hermes calls ``hermes_infoflow.register(ctx)`` directly and
   never reads ``plugin.yaml``.

2. Directory install (~/.hermes/plugins/infoflow/)::

       hermes_cli/plugins.py::_load_directory_module imports the directory
       as ``hermes_plugins.infoflow``, which then re-uses this same
       ``register`` symbol.

Both paths converge on the same ``register(ctx)`` entry point. The plugin
name MUST be the string ``"infoflow"`` in three places:

    * pyproject.toml entry-point key
    * plugin.yaml ``name`` field
    * register(ctx)(name="infoflow", ...) inside adapter.py

so the plugin manager deduplicates them to a single logical plugin.
"""

from .adapter import register

__all__ = ["register"]

__version__ = "0.1.0"
