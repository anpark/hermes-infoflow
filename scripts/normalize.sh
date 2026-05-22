#!/usr/bin/env bash
# Normalize any hermes-infoflow checkout/plugin dir into the canonical
# ~/.hermes/plugins/infoflow directory layout, then run deploy-common.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(dirname "$SCRIPT_DIR")"

DEPLOY_PY=""
if [[ -f "$SOURCE_DIR/hermes_infoflow/deploy.py" ]]; then
  DEPLOY_PY="$SOURCE_DIR/hermes_infoflow/deploy.py"
elif [[ -f "$SOURCE_DIR/deploy.py" ]]; then
  DEPLOY_PY="$SOURCE_DIR/deploy.py"
else
  echo "✗ Cannot find hermes-infoflow deploy.py under $SOURCE_DIR" >&2
  exit 1
fi

PYTHON_BIN="${PYTHON:-python3}"
exec "$PYTHON_BIN" "$DEPLOY_PY" --source "$SOURCE_DIR" "$@"
