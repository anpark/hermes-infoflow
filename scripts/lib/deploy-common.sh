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
#   --port PORT          webhook listen port (writes INFOFLOW_PORT to .env)
#   --dry-run            print actions; don't mutate anything
set -euo pipefail

PLUGIN_DIR=""
PLUGIN_ID="infoflow"
CONFIG_FILE="${HOME}/.hermes/config.yaml"
PORT=""
DRY_RUN="false"
DEFAULT_INFOFLOW_PORT=26521

validate_port() {
  local value="$1"
  if [[ ! "$value" =~ ^[0-9]{1,5}$ ]] || (( 10#$value < 1 || 10#$value > 65535 )); then
    echo "✗ --port must be an integer 1-65535 (got: $value)" >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plugin-dir)   PLUGIN_DIR="$2";   shift 2 ;;
    --plugin-id)    PLUGIN_ID="$2";    shift 2 ;;
    --config-file)  CONFIG_FILE="$2";  shift 2 ;;
    --port)
      if [[ $# -lt 2 ]]; then
        echo "✗ --port requires a value" >&2
        exit 1
      fi
      PORT="$2"
      shift 2
      ;;
    --dry-run)      DRY_RUN="true";    shift   ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done
if [[ -n "$PORT" ]]; then
  validate_port "$PORT"
fi

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

# Pip package names (``yaml`` imports from the ``pyyaml`` distribution).
PLUGIN_PIP_PACKAGES=(cryptography aiohttp pyyaml)

# Set HERMES_DEPLOY_AUTO_PIP=0 to refuse automatic ``pip install`` / ``pipx inject``.
HERMES_DEPLOY_AUTO_PIP="${HERMES_DEPLOY_AUTO_PIP:-1}"

CANDIDATE_PYTHONS=()
HERMES_LINKED_PYTHONS=()

_add_candidate() {
  local py="$1"
  local hermes_linked="${2:-0}"
  [[ -z "$py" ]] && return 0
  if [[ "$py" != /* ]] && ! command -v "$py" >/dev/null 2>&1; then
    return 0
  fi
  if [[ "$py" != /* ]]; then
    py="$(command -v "$py")"
  fi
  [[ -x "$py" ]] || return 0
  local existing
  if [[ ${#CANDIDATE_PYTHONS[@]} -gt 0 ]]; then
    for existing in "${CANDIDATE_PYTHONS[@]}"; do
      if [[ "$existing" == "$py" ]]; then
        return 0
      fi
    done
  fi
  CANDIDATE_PYTHONS+=("$py")
  if [[ "$hermes_linked" == "1" ]]; then
    HERMES_LINKED_PYTHONS+=("$py")
  fi
}

# pipx-installed hermes-agent exposes its venv Python here.
detect_pipx_hermes_python() {
  local py
  command -v pipx >/dev/null 2>&1 || return 1
  py="$(pipx environment hermes-agent -P python 2>/dev/null)" || return 1
  [[ -n "$py" && -x "$py" ]] || return 1
  printf '%s\n' "$py"
}

# Resolve the Python interpreter behind ``hermes`` on PATH (shebang or pipx).
detect_hermes_python() {
  local hermes_bin shebang interp pipx_py
  command -v hermes >/dev/null 2>&1 || return 1

  if pipx_py="$(detect_pipx_hermes_python)"; then
    printf '%s\n' "$pipx_py"
    return 0
  fi

  hermes_bin="$(command -v hermes)"
  if command -v realpath >/dev/null 2>&1; then
    hermes_bin="$(realpath "$hermes_bin" 2>/dev/null || echo "$hermes_bin")"
  fi
  shebang="$(head -n 1 "$hermes_bin" 2>/dev/null || true)"
  if [[ "$shebang" =~ ^#![[:space:]]*([^[:space:]]+) ]]; then
    interp="${BASH_REMATCH[1]}"
    if [[ "$interp" != */env ]] && [[ -x "$interp" ]]; then
      printf '%s\n' "$interp"
      return 0
    fi
  fi

  # Common pipx / manual venv layouts when the CLI wrapper uses ``#!/usr/bin/env``.
  local guess
  for guess in \
    "${HOME}/.local/pipx/venvs/hermes-agent/bin/python" \
    "${HOME}/.local/share/pipx/venvs/hermes-agent/bin/python" \
    "${HOME}/.hermes/venv/bin/python" \
    "${HOME}/.hermes/.venv/bin/python"
  do
    if [[ -x "$guess" ]]; then
      printf '%s\n' "$guess"
      return 0
    fi
  done
  return 1
}

collect_candidate_pythons() {
  CANDIDATE_PYTHONS=()
  HERMES_LINKED_PYTHONS=()

  if [[ -n "${PYTHON:-}" ]]; then
    _add_candidate "$PYTHON" 1
    return 0
  fi

  local hermes_py
  if hermes_py="$(detect_hermes_python)"; then
    _add_candidate "$hermes_py" 1
  fi
  if hermes_py="$(detect_pipx_hermes_python)"; then
    _add_candidate "$hermes_py" 1
  fi

  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    _add_candidate "${VIRTUAL_ENV}/bin/python" 0
  fi

  local ver
  for ver in python3 python3.14 python3.13 python3.12 python3.11; do
    _add_candidate "$ver" 0
  done
}

python_deps_ok() {
  local py="$1"
  "$py" -c "$DEP_CHECK_SCRIPT" >/dev/null 2>&1
}

python_has_pip() {
  local py="$1"
  "$py" -m pip --version >/dev/null 2>&1
}

pipx_has_hermes_agent() {
  # Prefer a concrete venv path over parsing ``pipx list`` output.
  detect_pipx_hermes_python >/dev/null 2>&1
}

warn_if_deploy_python_differs_from_hermes() {
  local py="$1"
  local ref="" ref_label=""
  if ref="$(detect_pipx_hermes_python 2>/dev/null)"; then
    ref_label="pipx hermes-agent"
  elif ref="$(detect_hermes_python 2>/dev/null)"; then
    ref_label="hermes CLI"
  else
    return 0
  fi
  local py_real="$py" ref_real="$ref"
  if command -v realpath >/dev/null 2>&1; then
    py_real="$(realpath "$py" 2>/dev/null || echo "$py")"
    ref_real="$(realpath "$ref" 2>/dev/null || echo "$ref")"
  fi
  if [[ "$py_real" == "$ref_real" ]]; then
    return 0
  fi
  echo "  ⚠ warning: deploy verified deps on: $py" >&2
  echo "    but $ref_label Python is: $ref" >&2
  echo "    Gateway loads plugins with the hermes-agent interpreter — if the plugin" >&2
  echo "    fails at runtime, re-run with:" >&2
  echo "      PYTHON=\$(pipx environment hermes-agent -P python) bash scripts/deploy.sh" >&2
}

auto_install_plugin_deps() {
  local target="$1"
  local pip_args=()

  if [[ "$HERMES_DEPLOY_AUTO_PIP" == "0" ]]; then
    return 1
  fi

  echo "==> Auto-installing plugin dependencies (cryptography, aiohttp, pyyaml)"

  # Best case: deps belong in hermes-agent's pipx venv (same runtime as gateway).
  if pipx_has_hermes_agent; then
    echo "$ pipx inject hermes-agent ${PLUGIN_PIP_PACKAGES[*]}"
    if [[ "$DRY_RUN" == "true" ]]; then
      return 0
    fi
    if pipx inject hermes-agent "${PLUGIN_PIP_PACKAGES[@]}"; then
      local pipx_py
      if pipx_py="$(detect_pipx_hermes_python)"; then
        python_deps_ok "$pipx_py" && return 0
      fi
    else
      echo "  - pipx inject hermes-agent failed" >&2
    fi
  fi

  if ! python_has_pip "$target"; then
    echo "  - $target has no working pip; cannot auto-install." >&2
    return 1
  fi

  # Only use --user for a plain system interpreter (not a venv / pipx path).
  if [[ -z "${VIRTUAL_ENV:-}" ]] \
    && [[ "$target" != *"/pipx/"* ]] \
    && [[ "$target" != *"/venv/"* ]] \
    && [[ "$target" != *"/.venv/"* ]]; then
    pip_args+=(--user)
  fi

  echo "$ $target -m pip install ${pip_args[*]:-} ${PLUGIN_PIP_PACKAGES[*]}"
  if [[ "$DRY_RUN" == "true" ]]; then
    return 0
  fi
  if ! "$target" -m pip install "${pip_args[@]}" "${PLUGIN_PIP_PACKAGES[@]}"; then
    echo "  - pip install failed for $target" >&2
    return 1
  fi
  return 0
}

pick_install_target() {
  local py
  if [[ ${#HERMES_LINKED_PYTHONS[@]} -gt 0 ]]; then
    printf '%s\n' "${HERMES_LINKED_PYTHONS[0]}"
    return 0
  fi
  if [[ ${#CANDIDATE_PYTHONS[@]} -gt 0 ]]; then
    for py in "${CANDIDATE_PYTHONS[@]}"; do
      if python_has_pip "$py"; then
        printf '%s\n' "$py"
        return 0
      fi
    done
    printf '%s\n' "${CANDIDATE_PYTHONS[0]}"
  fi
}

collect_candidate_pythons

PY=""
if [[ "$DRY_RUN" == "true" ]]; then
  PY="${CANDIDATE_PYTHONS[0]:-python3}"
  echo "  candidate interpreters: ${CANDIDATE_PYTHONS[*]:-<none>}"
  echo "  hermes-linked interpreters: ${HERMES_LINKED_PYTHONS[*]:-<none>}"
  echo "$ $PY -c <dep-check>"
else
  if [[ ${#CANDIDATE_PYTHONS[@]} -gt 0 ]]; then
    for candidate in "${CANDIDATE_PYTHONS[@]}"; do
      if python_deps_ok "$candidate"; then
        PY="$candidate"
        echo "  using interpreter: $PY"
        break
      fi
    done
  fi

  if [[ -z "$PY" ]]; then
    install_target="$(pick_install_target)"
    if [[ -n "$install_target" ]] && auto_install_plugin_deps "$install_target"; then
      collect_candidate_pythons
      if [[ ${#CANDIDATE_PYTHONS[@]} -gt 0 ]]; then
        for candidate in "${CANDIDATE_PYTHONS[@]}"; do
          if python_deps_ok "$candidate"; then
            PY="$candidate"
            echo "  using interpreter after auto-install: $PY"
            break
          fi
        done
      fi
    fi
  fi

  if [[ -z "$PY" ]]; then
    install_target="${install_target:-$(pick_install_target)}"
    if [[ -n "${install_target:-}" ]]; then
      "$install_target" -c "$DEP_CHECK_SCRIPT" || true
    elif [[ ${#CANDIDATE_PYTHONS[@]} -gt 0 ]]; then
      "${CANDIDATE_PYTHONS[0]}" -c "$DEP_CHECK_SCRIPT" || true
    fi
    echo
    echo "✗ One or more required Python packages are missing." >&2
    echo "  Required: cryptography, aiohttp, pyyaml" >&2
    echo "  Tried interpreters: ${CANDIDATE_PYTHONS[*]:-<none>}" >&2
    echo "  Hermes-linked interpreters: ${HERMES_LINKED_PYTHONS[*]:-<none>}" >&2
    if [[ "$HERMES_DEPLOY_AUTO_PIP" == "0" ]]; then
      echo "  Auto-install was disabled (HERMES_DEPLOY_AUTO_PIP=0)." >&2
    else
      echo "  Auto-install was attempted but did not succeed." >&2
    fi
    echo "  Fixes (any one):" >&2
    echo "    1) install hermes-agent (recommended — gateway uses its venv):" >&2
    echo "         pipx install hermes-agent" >&2
    echo "    2) point deploy at that Python explicitly:" >&2
    echo "         PYTHON=\$(pipx environment hermes-agent -P python) bash scripts/deploy.sh" >&2
    echo "    3) install manually:" >&2
    if [[ -n "${install_target:-}" ]]; then
      echo "         ${install_target} -m pip install cryptography aiohttp pyyaml" >&2
    else
      echo "         python3 -m pip install --user cryptography aiohttp pyyaml" >&2
    fi
    exit 1
  fi

  if [[ -n "$PY" ]]; then
    warn_if_deploy_python_differs_from_hermes "$PY"
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

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_ENV_FILE="$HERMES_HOME/.env"
echo "==> Configuring INFOFLOW_PORT in $HERMES_ENV_FILE"
EDIT_ENV_SCRIPT="$SELF_DIR/edit_hermes_env.py"
if [[ ! -f "$EDIT_ENV_SCRIPT" ]]; then
  EDIT_ENV_SCRIPT="$PLUGIN_DIR/scripts/lib/edit_hermes_env.py"
fi
if [[ ! -f "$EDIT_ENV_SCRIPT" ]]; then
  echo "✗ Cannot find edit_hermes_env.py" >&2
  exit 1
fi
ENV_ARGS=(--env-file "$HERMES_ENV_FILE")
if [[ -n "$PORT" ]]; then
  ENV_ARGS+=(--set "INFOFLOW_PORT=$PORT")
else
  ENV_ARGS+=(--ensure "INFOFLOW_PORT=$DEFAULT_INFOFLOW_PORT")
fi
if [[ "$DRY_RUN" == "true" ]]; then
  ENV_ARGS+=(--dry-run)
fi
run_cmd "$PY" "$EDIT_ENV_SCRIPT" "${ENV_ARGS[@]}"

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
