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
CANONICAL_PLUGIN_ID="infoflow"
ENTRYPOINT_PACKAGE="hermes-infoflow"
ENTRYPOINT_POLICY="${HERMES_INFOFLOW_ENTRYPOINT_POLICY:-uninstall}"
GATEWAY_RESTART_POLICY="${HERMES_INFOFLOW_GATEWAY_RESTART:-auto}"

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

if [[ "$PLUGIN_ID" != "$CANONICAL_PLUGIN_ID" ]]; then
  echo "✗ hermes-infoflow only supports plugin id '$CANONICAL_PLUGIN_ID'." >&2
  echo "  Got --plugin-id '$PLUGIN_ID', which would create a second Hermes plugin key." >&2
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

is_macos() {
  [[ "$(uname -s 2>/dev/null || true)" == "Darwin" ]]
}

plist_extract_raw() {
  local plist="$1"
  local keypath="$2"
  command -v plutil >/dev/null 2>&1 || return 1
  plutil -extract "$keypath" raw -o - "$plist" 2>/dev/null
}

gateway_launchd_plist_candidates() {
  local plist
  if [[ -n "${HERMES_INFOFLOW_GATEWAY_LAUNCHD_PLIST:-}" ]]; then
    [[ -f "$HERMES_INFOFLOW_GATEWAY_LAUNCHD_PLIST" ]] && printf '%s\n' "$HERMES_INFOFLOW_GATEWAY_LAUNCHD_PLIST"
    return 0
  fi

  for plist in "${HOME}/Library/LaunchAgents"/ai.hermes.gateway*.plist; do
    [[ -f "$plist" ]] || continue
    printf '%s\n' "$plist"
  done
}

launchd_label_from_plist() {
  local plist="$1"
  local label=""
  label="$(plist_extract_raw "$plist" Label || true)"
  if [[ -z "$label" ]]; then
    label="$(basename "$plist" .plist)"
  fi
  printf '%s\n' "$label"
}

collect_launchd_gateway_labels() {
  local label plist existing duplicate
  local labels=()
  local seen=()

  if [[ -n "${HERMES_INFOFLOW_GATEWAY_LAUNCHD_LABEL:-}" ]]; then
    labels+=("$HERMES_INFOFLOW_GATEWAY_LAUNCHD_LABEL")
  fi

  while IFS= read -r plist; do
    [[ -n "$plist" ]] || continue
    label="$(launchd_label_from_plist "$plist")"
    [[ -n "$label" ]] && labels+=("$label")
  done < <(gateway_launchd_plist_candidates)

  if [[ ${#labels[@]} -gt 0 ]]; then
    for label in "${labels[@]}"; do
      duplicate=0
      if [[ ${#seen[@]} -gt 0 ]]; then
        for existing in "${seen[@]}"; do
          if [[ "$existing" == "$label" ]]; then
            duplicate=1
            break
          fi
        done
      fi
      if [[ "$duplicate" == "0" ]]; then
        seen+=("$label")
        printf '%s\n' "$label"
      fi
    done
  fi
}

detect_launchd_gateway_python() {
  local arg0 arg1 plist
  while IFS= read -r plist; do
    [[ -n "$plist" ]] || continue
    arg0="$(plist_extract_raw "$plist" ProgramArguments.0 || true)"
    arg1="$(plist_extract_raw "$plist" ProgramArguments.1 || true)"
    if [[ -n "$arg0" && -x "$arg0" && "$(basename "$arg0")" == python* ]]; then
      printf '%s\n' "$arg0"
      return 0
    fi
    if [[ -n "$arg0" && "$(basename "$arg0" 2>/dev/null || true)" == "env" ]] \
      && [[ "$arg1" == python* ]] \
      && command -v "$arg1" >/dev/null 2>&1; then
      command -v "$arg1"
      return 0
    fi
  done < <(gateway_launchd_plist_candidates)
  return 1
}

_add_hermes_linked_python() {
  local py="$1"
  local existing
  [[ -z "$py" ]] && return 0
  if [[ ${#HERMES_LINKED_PYTHONS[@]} -gt 0 ]]; then
    for existing in "${HERMES_LINKED_PYTHONS[@]}"; do
      if [[ "$existing" == "$py" ]]; then
        return 0
      fi
    done
  fi
  HERMES_LINKED_PYTHONS+=("$py")
}

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
        if [[ "$hermes_linked" == "1" ]]; then
          _add_hermes_linked_python "$py"
        fi
        return 0
      fi
    done
  fi
  CANDIDATE_PYTHONS+=("$py")
  if [[ "$hermes_linked" == "1" ]]; then
    _add_hermes_linked_python "$py"
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
    if [[ "$interp" != */env ]] \
      && [[ -x "$interp" ]] \
      && [[ "$(basename "$interp")" == python* ]]; then
      printf '%s\n' "$interp"
      return 0
    fi
  fi

  # Common pipx / manual venv layouts when the CLI wrapper uses ``#!/usr/bin/env``.
  local guess
  for guess in \
    "${HOME}/.local/pipx/venvs/hermes-agent/bin/python" \
    "${HOME}/.local/share/pipx/venvs/hermes-agent/bin/python" \
    "${HOME}/.hermes/hermes-agent/venv/bin/python" \
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

  local hermes_py
  if hermes_py="$(detect_launchd_gateway_python)"; then
    _add_candidate "$hermes_py" 1
  fi
  if hermes_py="$(detect_hermes_python)"; then
    _add_candidate "$hermes_py" 1
  fi
  if hermes_py="$(detect_pipx_hermes_python)"; then
    _add_candidate "$hermes_py" 1
  fi

  if [[ -n "${PYTHON:-}" ]]; then
    _add_candidate "$PYTHON" 0
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

collect_hermes_runtime_pythons() {
  local py existing duplicate
  local runtimes=()
  local seen_runtimes=()

  if py="$(detect_launchd_gateway_python 2>/dev/null)"; then
    runtimes+=("$py")
  fi
  if py="$(detect_hermes_python 2>/dev/null)"; then
    runtimes+=("$py")
  fi
  if py="$(detect_pipx_hermes_python 2>/dev/null)"; then
    runtimes+=("$py")
  fi

  if [[ ${#runtimes[@]} -gt 0 ]]; then
    for py in "${runtimes[@]}"; do
      [[ -n "$py" ]] || continue
      duplicate=0
      if [[ ${#seen_runtimes[@]} -gt 0 ]]; then
        for existing in "${seen_runtimes[@]}"; do
          if [[ "$existing" == "$py" ]]; then
            duplicate=1
            break
          fi
        done
      fi
      if [[ "$duplicate" == "0" ]]; then
        seen_runtimes+=("$py")
        printf '%s\n' "$py"
      fi
    done
  fi
}

python_infoflow_entrypoint_version() {
  local py="$1"
  "$py" - <<'PY'
import importlib.metadata as md

try:
    dist = md.distribution("hermes-infoflow")
except md.PackageNotFoundError:
    raise SystemExit(1)

for ep in dist.entry_points:
    if ep.group == "hermes_agent.plugins" and ep.name == "infoflow":
        print(dist.version)
        raise SystemExit(0)

raise SystemExit(1)
PY
}

cleanup_shadowing_entrypoint_installs() {
  local py version found=0

  case "$ENTRYPOINT_POLICY" in
    uninstall|warn|keep) ;;
    *)
      echo "✗ HERMES_INFOFLOW_ENTRYPOINT_POLICY must be uninstall, warn, or keep (got: $ENTRYPOINT_POLICY)" >&2
      exit 1
      ;;
  esac

  echo "==> Checking for pip entry-point installs that could shadow the directory plugin"
  if [[ "$ENTRYPOINT_POLICY" == "keep" ]]; then
    echo "  - keeping entry-point installs (HERMES_INFOFLOW_ENTRYPOINT_POLICY=keep)"
    return 0
  fi

  while IFS= read -r py; do
    [[ -n "$py" ]] || continue
    if version="$(python_infoflow_entrypoint_version "$py" 2>/dev/null)"; then
      found=1
      if [[ "$ENTRYPOINT_POLICY" == "warn" ]]; then
        echo "  ⚠ $ENTRYPOINT_PACKAGE $version is installed in $py and may shadow $PLUGIN_DIR" >&2
        echo "    Re-run with HERMES_INFOFLOW_ENTRYPOINT_POLICY=uninstall to remove it automatically." >&2
        continue
      fi
      if ! python_has_pip "$py"; then
        echo "  ⚠ $ENTRYPOINT_PACKAGE $version is installed in $py but pip is unavailable; cannot uninstall." >&2
        echo "    Remove it manually or set HERMES_INFOFLOW_ENTRYPOINT_POLICY=keep if intentional." >&2
        exit 1
      fi
      echo "  - removing $ENTRYPOINT_PACKAGE $version from Hermes runtime: $py"
      echo "$ $py -m pip uninstall -y $ENTRYPOINT_PACKAGE"
      if [[ "$DRY_RUN" == "true" ]]; then
        continue
      fi
      if ! "$py" -m pip uninstall -y "$ENTRYPOINT_PACKAGE"; then
        echo "✗ failed to uninstall $ENTRYPOINT_PACKAGE from $py" >&2
        exit 1
      fi
    fi
  done < <(collect_hermes_runtime_pythons)

  if [[ "$found" == "0" ]]; then
    echo "  - no Hermes-runtime $ENTRYPOINT_PACKAGE entry point detected"
  fi
}

pipx_has_hermes_agent() {
  # Prefer a concrete venv path over parsing ``pipx list`` output.
  detect_pipx_hermes_python >/dev/null 2>&1
}

warn_if_deploy_python_differs_from_hermes() {
  local py="$1"
  local ref="" ref_label=""
  if ref="$(detect_launchd_gateway_python 2>/dev/null)"; then
    ref_label="launchd gateway"
  elif ref="$(detect_pipx_hermes_python 2>/dev/null)"; then
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

print_no_pip_guidance() {
  local target="$1"
  echo "  - $target has no working pip; cannot auto-install." >&2
  echo "    If this is the Hermes gateway venv, repair pip first, then install deps:" >&2
  echo "      $target -m ensurepip --upgrade" >&2
  echo "      $target -m pip install ${PLUGIN_PIP_PACKAGES[*]}" >&2
  echo "    If ensurepip is unavailable, reinstall or repair the Hermes agent environment." >&2
}

auto_install_plugin_deps() {
  local target="$1"
  local pipx_py=""
  local target_real="$target"
  local pipx_real=""
  local pip_args=()

  if [[ "$HERMES_DEPLOY_AUTO_PIP" == "0" ]]; then
    return 1
  fi

  echo "==> Auto-installing plugin dependencies (cryptography, aiohttp, pyyaml)"

  # Best case: target deps belong in hermes-agent's pipx venv.
  if pipx_has_hermes_agent && pipx_py="$(detect_pipx_hermes_python)"; then
    pipx_real="$pipx_py"
    if command -v realpath >/dev/null 2>&1; then
      target_real="$(realpath "$target" 2>/dev/null || echo "$target")"
      pipx_real="$(realpath "$pipx_py" 2>/dev/null || echo "$pipx_py")"
    fi
  else
    pipx_py=""
  fi

  if [[ -n "$pipx_py" && "$target_real" == "$pipx_real" ]]; then
    echo "$ pipx inject hermes-agent ${PLUGIN_PIP_PACKAGES[*]}"
    if [[ "$DRY_RUN" == "true" ]]; then
      return 0
    fi
    if pipx inject hermes-agent "${PLUGIN_PIP_PACKAGES[@]}"; then
      python_deps_ok "$target" && return 0
    else
      echo "  - pipx inject hermes-agent failed" >&2
    fi
  fi

  if ! python_has_pip "$target"; then
    print_no_pip_guidance "$target"
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

select_dependency_ready_python() {
  local candidate
  if [[ ${#HERMES_LINKED_PYTHONS[@]} -gt 0 ]]; then
    for candidate in "${HERMES_LINKED_PYTHONS[@]}"; do
      if python_deps_ok "$candidate"; then
        printf '%s\n' "$candidate"
        return 0
      fi
    done
    return 1
  fi

  for candidate in "${CANDIDATE_PYTHONS[@]}"; do
    if python_deps_ok "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

collect_candidate_pythons

PY=""
if [[ "$DRY_RUN" == "true" ]]; then
  PY="${CANDIDATE_PYTHONS[0]:-python3}"
  echo "  candidate interpreters: ${CANDIDATE_PYTHONS[*]:-<none>}"
  echo "  hermes-linked interpreters: ${HERMES_LINKED_PYTHONS[*]:-<none>}"
  echo "$ $PY -c <dep-check>"
else
  if [[ ${#CANDIDATE_PYTHONS[@]} -gt 0 ]] && PY="$(select_dependency_ready_python)"; then
    echo "  using interpreter: $PY"
  fi

  if [[ -z "$PY" ]]; then
    install_target="$(pick_install_target)"
    if [[ -n "$install_target" ]] && auto_install_plugin_deps "$install_target"; then
      collect_candidate_pythons
      if [[ ${#CANDIDATE_PYTHONS[@]} -gt 0 ]] && PY="$(select_dependency_ready_python)"; then
        echo "  using interpreter after auto-install: $PY"
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
      if python_has_pip "$install_target"; then
        echo "         ${install_target} -m pip install cryptography aiohttp pyyaml" >&2
      else
        echo "         ${install_target} -m ensurepip --upgrade" >&2
        echo "         ${install_target} -m pip install cryptography aiohttp pyyaml" >&2
      fi
    else
      echo "         python3 -m pip install --user cryptography aiohttp pyyaml" >&2
    fi
    exit 1
  fi

  if [[ -n "$PY" ]]; then
    warn_if_deploy_python_differs_from_hermes "$PY"
  fi
fi

cleanup_shadowing_entrypoint_installs

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

gateway_status_indicates_running() {
  local text="$1"
  if printf '%s\n' "$text" | grep -Eiq "not[[:space:]-]+running|stopped|inactive"; then
    return 1
  fi
  printf '%s\n' "$text" | grep -Eiq \
    "Runtime:[[:space:]]*running|status:[[:space:]]*running|state[[:space:]]*=[[:space:]]*running|Gateway .*running"
}

launchd_target_for_label() {
  local label="$1"
  printf 'gui/%s/%s\n' "$(id -u)" "$label"
}

launchd_gateway_loaded() {
  local label="$1"
  local target
  target="$(launchd_target_for_label "$label")"
  launchctl print "$target" >/dev/null 2>&1
}

launchd_gateway_running() {
  local label="$1"
  local output rc target
  target="$(launchd_target_for_label "$label")"
  set +e
  output="$(launchctl print "$target" 2>/dev/null)"
  rc=$?
  set -e
  [[ "$rc" -eq 0 ]] || return 1
  printf '%s\n' "$output" | grep -Eq "state = running|pid = [1-9][0-9]*"
}

wait_for_launchd_gateway_running() {
  local label="$1"
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if launchd_gateway_running "$label"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

hermes_gateway_running_via_cli() {
  local output rc
  command -v hermes >/dev/null 2>&1 || return 1
  set +e
  output="$(hermes gateway status 2>&1)"
  rc=$?
  set -e
  [[ "$rc" -eq 0 ]] || return 1
  gateway_status_indicates_running "$output"
}

wait_for_gateway_running() {
  local attempt label
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if is_macos && command -v launchctl >/dev/null 2>&1; then
      while IFS= read -r label; do
        [[ -n "$label" ]] || continue
        if launchd_gateway_running "$label"; then
          return 0
        fi
      done < <(collect_launchd_gateway_labels)
    fi
    if hermes_gateway_running_via_cli; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restart_running_launchd_gateway() {
  local label target found=0 loaded=0
  if ! is_macos || ! command -v launchctl >/dev/null 2>&1; then
    return 2
  fi

  while IFS= read -r label; do
    [[ -n "$label" ]] || continue
    found=1
    target="$(launchd_target_for_label "$label")"
    if launchd_gateway_running "$label"; then
      echo "==> Gateway is running under launchd ($label); restarting to load the updated plugin"
      echo "$ launchctl kickstart -k $target"
      if ! launchctl kickstart -k "$target"; then
        echo "✗ launchctl kickstart failed for $target" >&2
        return 1
      fi
      if wait_for_launchd_gateway_running "$label"; then
        echo "✓ gateway is running ($label)"
        return 0
      fi
      echo "✗ gateway did not return to running; inspect 'launchctl print $target'" >&2
      return 1
    fi
    if launchd_gateway_loaded "$label"; then
      loaded=1
      echo "  - launchd gateway $label is loaded but not running; skipping restart."
    fi
  done < <(collect_launchd_gateway_labels)

  if [[ "$found" == "0" ]]; then
    echo "  - no launchd gateway plist/label found."
  elif [[ "$loaded" == "0" ]]; then
    echo "  - no running launchd gateway found."
  fi
  return 2
}

restart_gateway_with_hermes_cli() {
  local status rc
  if ! command -v hermes >/dev/null 2>&1; then
    echo "  - hermes CLI not on PATH; skipping gateway restart."
    echo "  - Start the gateway manually once you're ready: hermes gateway"
    return 2
  fi

  set +e
  status="$(hermes gateway status 2>&1)"
  rc=$?
  set -e

  if ! gateway_status_indicates_running "$status"; then
    if [[ "$rc" -ne 0 ]]; then
      echo "  - 'hermes gateway status' failed; not using it as proof that gateway is stopped." >&2
      printf '%s\n' "$status" | sed -n '1,3p' >&2
    else
      echo "  - Hermes gateway is not running; skipping restart."
    fi
    return 2
  fi

  echo "==> Gateway is running; restarting via hermes CLI"
  if ! hermes gateway restart; then
    echo "✗ gateway restart failed; check 'hermes gateway status'" >&2
    return 1
  fi
  if wait_for_gateway_running; then
    echo "✓ gateway is running"
    return 0
  fi
  echo "✗ gateway did not return to running; inspect 'hermes gateway status'" >&2
  return 1
}

echo "==> Checking hermes gateway status"
case "$GATEWAY_RESTART_POLICY" in
  auto|launchctl|hermes|skip) ;;
  *)
    echo "✗ HERMES_INFOFLOW_GATEWAY_RESTART must be auto, launchctl, hermes, or skip (got: $GATEWAY_RESTART_POLICY)" >&2
    exit 1
    ;;
esac

# We only restart the gateway when it's already running. Starting it fresh
# is the user's choice — they may want to verify config before starting.
if [[ "$DRY_RUN" == "true" ]]; then
  echo "  gateway restart policy: $GATEWAY_RESTART_POLICY"
  echo "$ launchctl print gui/\$(id -u)/<label> || hermes gateway status (dry-run, skipping)"
  echo "==> Done (dry-run)"
  exit 0
fi

case "$GATEWAY_RESTART_POLICY" in
  skip)
    echo "==> Skipping gateway restart (HERMES_INFOFLOW_GATEWAY_RESTART=skip)"
    ;;
  launchctl)
    if restart_running_launchd_gateway; then
      :
    else
      restart_rc=$?
      echo "✗ no running launchd-managed gateway was restarted." >&2
      exit 1
    fi
    ;;
  hermes)
    if restart_gateway_with_hermes_cli; then
      :
    else
      restart_rc=$?
      if [[ "$restart_rc" == "1" ]]; then
        exit 1
      fi
      echo "==> Gateway is not running; skipping restart (start manually with 'hermes gateway')"
    fi
    ;;
  auto)
    if restart_running_launchd_gateway; then
      :
    else
      restart_rc=$?
      if [[ "$restart_rc" == "1" ]]; then
        exit 1
      fi
      if restart_gateway_with_hermes_cli; then
        :
      else
        restart_rc=$?
        if [[ "$restart_rc" == "1" ]]; then
          exit 1
        fi
        echo "==> Gateway is not running; skipping restart (start manually with 'hermes gateway')"
      fi
    fi
    ;;
esac

echo "==> Done"
