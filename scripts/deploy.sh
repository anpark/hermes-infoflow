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

# ─────────────────────────────────────────────────────────────────────────────
# TEMPORARY: align ~/.hermes/hermes-agent with chbo297 fork branch.
#
# Upstream hermes-agent currently has a bug that breaks a few plugin
# capabilities (notably send_message target routing for the infoflow
# plugin). The fork at https://github.com/chbo297/hermes-agent.git
# carries a fix on the branch ``fix/send-message-plugin-target-routing``
# that has NOT yet been merged into upstream main. Until it lands
# upstream, this block ensures the locally-checked-out hermes-agent at
# ~/.hermes/hermes-agent matches the latest commit of that fork branch
# so the gateway picks up the patched runtime when it restarts.
#
# This block must be REMOVED once the fix is merged into upstream
# hermes-agent main (and this plugin starts pinning a release that
# contains it).
# ─────────────────────────────────────────────────────────────────────────────

HERMES_AGENT_DIR="${HOME}/.hermes/hermes-agent"
HERMES_AGENT_FORK_URL="https://github.com/chbo297/hermes-agent.git"
HERMES_AGENT_FORK_REMOTE="chbo297-fork"
HERMES_AGENT_FORK_BRANCH="fix/send-message-plugin-target-routing"

align_hermes_agent_with_fork_branch() {
  if [[ ! -d "$HERMES_AGENT_DIR/.git" ]]; then
    echo "==> Skipping hermes-agent fork-branch sync (no git checkout at $HERMES_AGENT_DIR)"
    echo "    If you rely on the chbo297 fork patch, clone it first:"
    echo "    git clone -b $HERMES_AGENT_FORK_BRANCH $HERMES_AGENT_FORK_URL $HERMES_AGENT_DIR"
    return 0
  fi

  echo "==> Aligning hermes-agent checkout with fork branch (TEMPORARY; see comment)"
  echo "    repo:   $HERMES_AGENT_DIR"
  echo "    remote: $HERMES_AGENT_FORK_REMOTE -> $HERMES_AGENT_FORK_URL"
  echo "    branch: $HERMES_AGENT_FORK_BRANCH"

  if ! git -C "$HERMES_AGENT_DIR" diff-index --quiet HEAD --; then
    echo "  ⚠ hermes-agent has uncommitted changes; skipping fork-branch sync." >&2
    echo "    Commit or stash them, then re-run scripts/deploy.sh." >&2
    return 0
  fi

  local prior_head=""
  prior_head="$(git -C "$HERMES_AGENT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"

  local existing_url=""
  existing_url="$(git -C "$HERMES_AGENT_DIR" remote get-url "$HERMES_AGENT_FORK_REMOTE" 2>/dev/null || true)"
  if [[ -z "$existing_url" ]]; then
    run_cmd git -C "$HERMES_AGENT_DIR" remote add "$HERMES_AGENT_FORK_REMOTE" "$HERMES_AGENT_FORK_URL"
  elif [[ "$existing_url" != "$HERMES_AGENT_FORK_URL" ]]; then
    echo "  - remote $HERMES_AGENT_FORK_REMOTE was $existing_url; updating to $HERMES_AGENT_FORK_URL"
    run_cmd git -C "$HERMES_AGENT_DIR" remote set-url "$HERMES_AGENT_FORK_REMOTE" "$HERMES_AGENT_FORK_URL"
  fi

  run_cmd git -C "$HERMES_AGENT_DIR" fetch "$HERMES_AGENT_FORK_REMOTE" "$HERMES_AGENT_FORK_BRANCH"
  run_cmd git -C "$HERMES_AGENT_DIR" switch -C "$HERMES_AGENT_FORK_BRANCH" \
    "$HERMES_AGENT_FORK_REMOTE/$HERMES_AGENT_FORK_BRANCH"

  echo "  prior HEAD was $prior_head (recoverable via 'git -C $HERMES_AGENT_DIR reflog')"
  echo "  note: if the venv at $HERMES_AGENT_DIR/venv was NOT installed editable,"
  echo "        also reinstall to pick up the new source:"
  echo "          $HERMES_AGENT_DIR/venv/bin/pip install -e $HERMES_AGENT_DIR"
}

align_hermes_agent_with_fork_branch
# ─── end TEMPORARY block ────────────────────────────────────────────────────

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
