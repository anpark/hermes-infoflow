# Agent Notes

This repository is the `infoflow` channel plugin for hermes agent.

Important runtime context:

- This repository is a git codebase, but the plugin actually runs from:
  `~/.hermes/plugin/infoflow`
- When debugging runtime issues, do not rely only on this repository's source.
  Inspect the real runtime data and generated files under:
  `~/.hermes`
- If plugin behavior depends on hermes agent internals, inspect the local hermes agent source at:
  `~/.hermes/hermes-agent`
- Treat this repository as the source workspace, and `~/.hermes/plugin/infoflow` as the deployed/runtime copy.
- Before changing behavior, compare repository code, deployed plugin code, runtime config, logs, and persisted data when relevant.
