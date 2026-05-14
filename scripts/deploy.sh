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
RSYNC_OPTS=(
  -av --delete
  --exclude tests
  --exclude tools
  --exclude .git
  --exclude .github
  --exclude __pycache__
  --exclude .venv
  --exclude .pytest_cache
  --exclude dist
  --exclude build
  --exclude "*.egg-info"
)
run_cmd rsync "${RSYNC_OPTS[@]}" "$PROJECT_DIR/" "$PLUGIN_DIR/"

COMMON_ARGS=(
  --plugin-dir "$PLUGIN_DIR"
  --plugin-id "$PLUGIN_ID"
  --config-file "$CONFIG_FILE"
)
if [[ "$DRY_RUN" == "true" ]]; then
  COMMON_ARGS+=(--dry-run)
fi

bash "$COMMON_SCRIPT" "${COMMON_ARGS[@]}"
