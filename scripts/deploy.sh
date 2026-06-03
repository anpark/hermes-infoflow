#!/usr/bin/env bash
# Local development one-shot deploy.
#
# This is intentionally a thin wrapper around hermes_infoflow/deploy.py so the
# local checkout, PyPI tools installer, pip deploy command, and normalize path
# all share one deployment orchestrator.
set -euo pipefail

CANONICAL_PLUGIN_ID="infoflow"
if [[ -n "${HERMES_INFOFLOW_PLUGIN_ID:-}" && "${HERMES_INFOFLOW_PLUGIN_ID}" != "$CANONICAL_PLUGIN_ID" ]]; then
  echo "✗ hermes-infoflow only supports plugin id '$CANONICAL_PLUGIN_ID'." >&2
  echo "  HERMES_INFOFLOW_PLUGIN_ID=${HERMES_INFOFLOW_PLUGIN_ID} would create a second Hermes plugin key." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DEPLOY_PY="$PROJECT_DIR/hermes_infoflow/deploy.py"
CONFIG_FILE="${HERMES_CONFIG_FILE:-${HERMES_HOME:-${HOME}/.hermes}/config.yaml}"
PYTHON_BIN="${HERMES_INFOFLOW_DEPLOY_PYTHON:-${PYTHON:-python3}}"

DRY_RUN="false"
PORT=""

validate_port() {
  local value="$1"
  if [[ ! "$value" =~ ^[0-9]{1,5}$ ]] || (( 10#$value < 1 || 10#$value > 65535 )); then
    echo "✗ --port must be an integer 1-65535 (got: $value)" >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    --port)
      if [[ $# -lt 2 ]]; then
        echo "✗ --port requires a value" >&2
        exit 1
      fi
      PORT="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: bash scripts/deploy.sh [--dry-run] [--port PORT]" >&2
      exit 1
      ;;
  esac
done

if [[ -n "$PORT" ]]; then
  validate_port "$PORT"
fi
if [[ ! -f "$DEPLOY_PY" ]]; then
  echo "✗ expected deploy.py not found: $DEPLOY_PY" >&2
  exit 1
fi

if [[ "${INFOFLOW_RUN_LIVE_LLM_TESTS:-}" == "1" ]]; then
  LIVE_LLM_PYTHON="${HERMES_INFOFLOW_LIVE_LLM_PYTHON:-${HERMES_HOME:-${HOME}/.hermes}/hermes-agent/venv/bin/python}"
  if [[ ! -x "$LIVE_LLM_PYTHON" ]]; then
    LIVE_LLM_PYTHON="$PYTHON_BIN"
  fi
  LIVE_LLM_TEST="$PROJECT_DIR/scripts/sim/test_prompt_behavior_glm.py"
  if [[ ! -f "$LIVE_LLM_TEST" ]]; then
    echo "✗ expected live prompt test not found: $LIVE_LLM_TEST" >&2
    exit 1
  fi
  echo "$ INFOFLOW_RUN_LIVE_LLM_TESTS=1 $LIVE_LLM_PYTHON $LIVE_LLM_TEST --quiet"
  INFOFLOW_RUN_LIVE_LLM_TESTS=1 "$LIVE_LLM_PYTHON" "$LIVE_LLM_TEST" --quiet
fi

CMD=(
  "$PYTHON_BIN"
  "$DEPLOY_PY"
  --source "$PROJECT_DIR"
  --config-file "$CONFIG_FILE"
)
if [[ -n "${HERMES_HOME:-}" ]]; then
  CMD+=(--hermes-home "$HERMES_HOME")
fi
if [[ -n "$PORT" ]]; then
  CMD+=(--port "$PORT")
fi
if [[ "$DRY_RUN" == "true" ]]; then
  CMD+=(--dry-run)
fi

echo "$ ${CMD[*]}"
exec "${CMD[@]}"
