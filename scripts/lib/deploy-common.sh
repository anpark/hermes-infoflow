#!/usr/bin/env bash
# Shared deployment core for hermes-infoflow.
#
# Mirrors openclaw-infoflow/scripts/lib/deploy-common.sh in spirit, but
# replaces the npm/tsc steps with Python + hermes-cli equivalents.
#
# Called both by scripts/deploy.sh (local dev) and by
# tools/hermes-infoflow-tools/hermes_infoflow_tools/cli.py (PyPI installer)
# AFTER they have already synced the plugin source into $PLUGIN_DIR.
#
# Required:
#   --plugin-dir DIR     destination (e.g. ~/.hermes/plugins/infoflow)
#   --plugin-id  ID      plugin id (default: infoflow)
#   --config-file PATH   path to ~/.hermes/config.yaml
# Optional:
#   --dry-run            print actions; don't mutate anything
set -euo pipefail

PLUGIN_DIR=""
PLUGIN_ID="infoflow"
CONFIG_FILE="${HOME}/.hermes/config.yaml"
DRY_RUN="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plugin-dir)   PLUGIN_DIR="$2";   shift 2 ;;
    --plugin-id)    PLUGIN_ID="$2";    shift 2 ;;
    --config-file)  CONFIG_FILE="$2";  shift 2 ;;
    --dry-run)      DRY_RUN="true";    shift   ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$PLUGIN_DIR" ]]; then
  echo "Missing --plugin-dir" >&2
  exit 1
fi

if [[ "$DRY_RUN" != "true" ]]; then
  if [[ ! -d "$PLUGIN_DIR" ]]; then
    echo "Plugin directory does not exist: $PLUGIN_DIR" >&2
    exit 1
  fi
  if [[ ! -f "$PLUGIN_DIR/plugin.yaml" ]]; then
    echo "Refusing to deploy: $PLUGIN_DIR/plugin.yaml not found" >&2
    exit 1
  fi
fi

run_cmd() {
  echo "$ $*"
  if [[ "$DRY_RUN" == "true" ]]; then
    return 0
  fi
  "$@"
}

echo "==> Verifying runtime dependencies"
PY="${PYTHON:-python3}"
DEP_CHECK_SCRIPT=$(cat <<'PYEOF'
import importlib, sys
missing = []
for mod in ("cryptography", "aiohttp", "yaml"):
    try:
        importlib.import_module(mod)
    except ImportError:
        missing.append(mod)
if missing:
    print("MISSING:", ",".join(missing))
    sys.exit(1)
print("OK")
PYEOF
)
if [[ "$DRY_RUN" == "true" ]]; then
  echo "$ $PY -c <dep-check>"
else
  if ! "$PY" -c "$DEP_CHECK_SCRIPT"; then
    echo
    echo "✗ One or more required Python packages are missing." >&2
    echo "  Required: cryptography, aiohttp, pyyaml" >&2
    echo "  These are normally already installed by hermes-agent itself." >&2
    echo "  If you installed hermes-agent in a venv, source it before re-running this script." >&2
    echo "  As a manual fallback (per-user install):" >&2
    echo "      $PY -m pip install --user cryptography aiohttp pyyaml" >&2
    exit 1
  fi
fi

echo "==> Enabling plugin in $CONFIG_FILE"
# Prefer the repo copy (script lives alongside this file) so dry-run on a
# brand-new install works even before the rsync has actually happened.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EDIT_SCRIPT="$SELF_DIR/edit_hermes_config.py"
if [[ ! -f "$EDIT_SCRIPT" ]]; then
  EDIT_SCRIPT="$PLUGIN_DIR/scripts/lib/edit_hermes_config.py"
fi
if [[ ! -f "$EDIT_SCRIPT" ]]; then
  echo "✗ Cannot find edit_hermes_config.py" >&2
  exit 1
fi
EDIT_ARGS=(--config-file "$CONFIG_FILE" --plugin-id "$PLUGIN_ID")
if [[ "$DRY_RUN" == "true" ]]; then
  EDIT_ARGS+=(--dry-run)
fi
run_cmd "$PY" "$EDIT_SCRIPT" "${EDIT_ARGS[@]}"

echo "==> Checking hermes gateway status"
if ! command -v hermes >/dev/null 2>&1; then
  echo "  - hermes CLI not on PATH; skipping gateway restart."
  echo "  - Start the gateway manually once you're ready: hermes gateway"
  exit 0
fi

# We only restart the gateway when it's already running. Starting it fresh
# is the user's choice — they may want to verify config before starting.
if [[ "$DRY_RUN" == "true" ]]; then
  echo "$ hermes gateway status (dry-run, skipping)"
  echo "==> Done (dry-run)"
  exit 0
fi

set +e
STATUS_OUTPUT="$(hermes gateway status 2>/dev/null || true)"
set -e

if echo "$STATUS_OUTPUT" | grep -q -E "Runtime: running|status: running|running"; then
  echo "==> Gateway is running; restarting to load the updated plugin"
  hermes gateway restart || {
    echo "✗ gateway restart failed; check 'hermes gateway status'" >&2
    exit 1
  }
  sleep 2
  if hermes gateway status 2>/dev/null | grep -q -E "Runtime: running|status: running|running"; then
    echo "✓ gateway is running"
  else
    echo "✗ gateway did not return to running; inspect 'hermes gateway status'" >&2
    exit 1
  fi
else
  echo "==> Gateway is not running; skipping restart (start manually with 'hermes gateway')"
fi

echo "==> Done"
