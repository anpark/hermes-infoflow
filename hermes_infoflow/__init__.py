"""hermes-infoflow: Baidu Infoflow (如流) channel plugin for Hermes Agent.

Loaded by hermes-agent via either of these paths:

1. Entry-point (``pip install hermes-infoflow``)::

       [project.entry-points."hermes_agent.plugins"]
       infoflow = "hermes_infoflow"

   In this case hermes calls ``hermes_infoflow.register(ctx)`` directly
   and never reads ``plugin.yaml`` — users must export ``INFOFLOW_*``
   env vars themselves.

2. Directory install (``~/.hermes/plugins/infoflow/``).

   hermes-agent's directory loader
   (``hermes_cli/plugins.py::_load_directory_module``) requires
   ``__init__.py`` to live directly at the plugin dir root, NOT nested
   inside a ``hermes_infoflow/`` subdirectory. ``scripts/deploy.sh`` and
   ``hermes-infoflow-tools update --mode extract`` are responsible for
   flattening the layout at install time — they rsync this package's
   contents into the plugin dir root, then drop ``plugin.yaml`` on top.
   See ``tests/test_deploy_layout.py`` for the layout contract.

   Internal imports inside the package are all relative
   (``from .adapter import register``); hermes-agent sets
   ``submodule_search_locations=[plugin_dir]`` on the loaded module so
   relative imports keep resolving after flattening.

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
