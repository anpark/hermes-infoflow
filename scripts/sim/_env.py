"""Shared bootstrap for `scripts/sim/*` simulation entry points.

Each simulation script wants to mimic the same environment that the
live Hermes gateway sets up for the Infoflow plugin:

* `~/.hermes/.env` is loaded into ``os.environ`` (env vars already set
  by the caller win — same precedence as ``hermes_cli.env_loader``).
* ``~/.hermes/hermes-agent`` is prepended to ``sys.path`` if present,
  so ``gateway.platforms.base`` becomes importable and ``InfoflowAdapter``
  can be constructed exactly as it is in production.
* The repo root is added to ``sys.path`` so ``hermes_infoflow`` resolves
  to the in-tree code we are testing rather than any pip-installed copy.

The helpers here are intentionally dependency-free (no python-dotenv) so
the scripts can be run with a bare Python interpreter.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
HERMES_AGENT_PATH = HERMES_HOME / "hermes-agent"
HERMES_ENV_FILE = HERMES_HOME / ".env"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse_env_file(path: Path) -> dict[str, str]:
    """Tiny ``KEY=VALUE`` parser (handles ``#`` comments and surrounding quotes)."""
    parsed: dict[str, str] = {}
    if not path.exists():
        return parsed
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if key:
            parsed[key] = value
    return parsed


def load_hermes_env(*, override: bool = False) -> dict[str, str]:
    """Load ``~/.hermes/.env`` into ``os.environ``.

    Returns the dict of values applied (useful for logging). Existing
    environment values are preserved unless *override* is set.
    """
    values = _parse_env_file(HERMES_ENV_FILE)
    applied: dict[str, str] = {}
    for key, value in values.items():
        if not override and os.environ.get(key):
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


def ensure_hermes_agent_on_path() -> bool:
    """Prepend the local hermes-agent checkout to ``sys.path`` if it exists."""
    if not HERMES_AGENT_PATH.exists():
        return False
    agent_str = str(HERMES_AGENT_PATH)
    if agent_str not in sys.path:
        sys.path.insert(0, agent_str)
    return True


def ensure_repo_on_path() -> None:
    repo_str = str(REPO_ROOT)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)


def bootstrap() -> dict[str, object]:
    """One-shot setup used by every simulation script.

    Returns a small status dict so the script can print diagnostics.
    Only ``INFOFLOW_*`` keys are surfaced so unrelated values from a
    shared ``~/.hermes/.env`` don't drown out the relevant output.
    """
    ensure_repo_on_path()
    hermes_agent_available = ensure_hermes_agent_on_path()
    applied = load_hermes_env(override=False)
    infoflow_keys = sorted(k for k in applied if k.startswith("INFOFLOW_"))

    return {
        "hermes_env_file": str(HERMES_ENV_FILE),
        "hermes_agent_path": str(HERMES_AGENT_PATH),
        "hermes_agent_available": hermes_agent_available,
        "repo_root": str(REPO_ROOT),
        "applied_infoflow_keys": infoflow_keys,
        "applied_total": len(applied),
    }


def required_env(*names: str) -> None:
    """Abort with a friendly message if any of *names* is missing/empty."""
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(
            f"[sim] missing required env vars: {joined}\n"
            f"      please set them in {HERMES_ENV_FILE} or export them manually."
        )


def _group_id_from_target(value: str | None) -> str:
    target = str(value or "").strip()
    if target.startswith("infoflow:"):
        target = target[len("infoflow:"):]
    if target.startswith("group:"):
        target = target[len("group:"):]
    return target if target.isdigit() else ""


def default_group_id() -> str:
    """Return the configured real-test group id, if one is available."""
    for name in ("INFOFLOW_REAL_TEST_GROUP", "INFOFLOW_OP_GROUP", "INFOFLOW_OP_CHANNEL"):
        group_id = _group_id_from_target(os.environ.get(name))
        if group_id:
            return group_id
    return ""


def test_group_id() -> str:
    """Return the single numeric group id used by sim scripts."""
    group_id = default_group_id()
    if not group_id:
        raise SystemExit(
            "[sim] group id is required; pass --group or set "
            "INFOFLOW_REAL_TEST_GROUP, INFOFLOW_OP_GROUP, or a numeric/group "
            "INFOFLOW_OP_CHANNEL."
        )
    return group_id


__all__ = [
    "HERMES_AGENT_PATH",
    "HERMES_ENV_FILE",
    "HERMES_HOME",
    "REPO_ROOT",
    "bootstrap",
    "default_group_id",
    "ensure_hermes_agent_on_path",
    "ensure_repo_on_path",
    "load_hermes_env",
    "required_env",
    "test_group_id",
]
