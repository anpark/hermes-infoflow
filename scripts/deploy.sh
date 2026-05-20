#!/usr/bin/env bash
# Local development one-shot deploy:
# rsync the current repo into ~/.hermes/plugins/infoflow/ and run the
# shared deploy-common.sh to update hermes config + restart gateway.
#
# Mirrors openclaw-infoflow/scripts/deploy.sh.
#
# Usage:
#   bash scripts/deploy.sh [--dry-run]
set -euo pipefail

PLUGIN_ID="${HERMES_INFOFLOW_PLUGIN_ID:-infoflow}"
PLUGIN_DIR="${HOME}/.hermes/plugins/${PLUGIN_ID}"
CONFIG_FILE="${HERMES_CONFIG_FILE:-${HOME}/.hermes/config.yaml}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
COMMON_SCRIPT="$SCRIPT_DIR/lib/deploy-common.sh"

DRY_RUN="false"
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="true"
fi

run_cmd() {
  echo "$ $*"
  if [[ "$DRY_RUN" == "true" ]]; then
    return 0
  fi
  "$@"
}

echo "==> Syncing plugin files to $PLUGIN_DIR"
run_cmd mkdir -p "$PLUGIN_DIR"

# hermes-agent's directory-plugin loader (hermes_cli/plugins.py::
# _load_directory_module) requires ``__init__.py`` to live directly at
# ``$PLUGIN_DIR``.  The repo root already ships such a file (it re-exports
# from ``hermes_infoflow/``), so ``hermes plugins install`` works without
# any extra step.
#
# This script still flattens the layout for backward compatibility with
# older deployments and to align with the ``--mode extract`` path of
# ``hermes-infoflow-tools``. The flattening rsyncs:
#
#   1. ``hermes_infoflow/*``  →  ``$PLUGIN_DIR/*``   (with --delete)
#   2. ``plugin.yaml``        →  ``$PLUGIN_DIR/plugin.yaml``
#   3. ``scripts/``           →  ``$PLUGIN_DIR/scripts/``  (kept so
#                                 hermes-infoflow-tools --mode extract
#                                 can find deploy-common.sh on re-runs)
#
# After flattening, relative imports (``from .adapter import register``)
# resolve correctly because hermes-agent sets
# ``submodule_search_locations=[plugin_dir]`` on the loaded module.
PACKAGE_DIR="$PROJECT_DIR/hermes_infoflow"
if [[ ! -d "$PACKAGE_DIR" ]]; then
  echo "✗ expected Python package directory not found: $PACKAGE_DIR" >&2
  exit 1
fi
PLUGIN_MANIFEST="$PROJECT_DIR/plugin.yaml"
if [[ ! -f "$PLUGIN_MANIFEST" ]]; then
  echo "✗ expected plugin manifest not found: $PLUGIN_MANIFEST" >&2
  exit 1
fi

run_cmd rsync -av --delete \
  --exclude __pycache__ \
  --exclude "*.pyc" \
  "$PACKAGE_DIR/" "$PLUGIN_DIR/"
run_cmd rsync -av "$PLUGIN_MANIFEST" "$PLUGIN_DIR/plugin.yaml"
run_cmd rsync -av --delete \
  --exclude __pycache__ \
  --exclude "*.pyc" \
  "$PROJECT_DIR/scripts/" "$PLUGIN_DIR/scripts/"

COMMON_ARGS=(
  --plugin-dir "$PLUGIN_DIR"
  --plugin-id "$PLUGIN_ID"
  --config-file "$CONFIG_FILE"
)
if [[ "$DRY_RUN" == "true" ]]; then
  COMMON_ARGS+=(--dry-run)
fi

bash "$COMMON_SCRIPT" "${COMMON_ARGS[@]}"
