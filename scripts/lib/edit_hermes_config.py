#!/usr/bin/env python3
"""Safely update Infoflow plugin config in ``~/.hermes/config.yaml``.

hermes-agent reads enabled plugins from a **YAML list** at
``plugins.enabled`` (see ``hermes_cli/plugins.py:218-221``):

    plugins:
      enabled:
        - infoflow
        - other-plugin

So we cannot use OpenClaw's ``plugins.entries.<id>.enabled = true``
shape — that would be silently ignored. This script:

* Loads the YAML file (or starts a fresh one if missing).
* Ensures ``plugins.enabled`` is a list.
* Appends the requested plugin id IFF it isn't already present.
* Ensures ``platform_toolsets.<plugin-id>`` has the same baseline tool
  permissions as the CLI platform, including ``hermes-infoflow``.
* Preserves every other key by round-tripping through PyYAML.

Usage::

    python3 edit_hermes_config.py \\
        --config-file ~/.hermes/config.yaml \\
        --plugin-id infoflow [--remove] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - bootstrap path
    sys.stderr.write(
        "Missing PyYAML. Install with: pip install --user pyyaml\n"
        f"({exc})\n"
    )
    sys.exit(2)


DEFAULT_INFOFLOW_PLATFORM_TOOLSETS = (
    "browser",
    "clarify",
    "code_execution",
    "computer_use",
    "cronjob",
    "delegation",
    "file",
    "hermes-infoflow",
    "image_gen",
    "memory",
    "messaging",
    "session_search",
    "skills",
    "terminal",
    "todo",
    "tts",
    "vision",
    "web",
)


def _load(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    parsed = yaml.safe_load(text)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise SystemExit(f"refusing to edit {path}: top-level YAML is not a mapping")
    return parsed


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    path.write_text(serialized, encoding="utf-8")


def _normalize_toolsets(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SystemExit(f"{label} must be a list; got {type(value).__name__}")
    return [
        str(item)
        for item in value
        if isinstance(item, (str, int)) and str(item).strip()
    ]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _reference_toolsets(platform_toolsets: dict[str, Any]) -> list[str]:
    cli_toolsets = _normalize_toolsets(platform_toolsets.get("cli"), "platform_toolsets.cli")
    return _dedupe([*cli_toolsets, *DEFAULT_INFOFLOW_PLATFORM_TOOLSETS])


def ensure_platform_toolsets(data: dict[str, Any], plugin_id: str) -> bool:
    """Ensure ``platform_toolsets.<plugin_id>`` can call the standard tools."""
    platform_toolsets = data.setdefault("platform_toolsets", {})
    if not isinstance(platform_toolsets, dict):
        raise SystemExit("platform_toolsets: must be a mapping; refusing to edit")

    required = _reference_toolsets(platform_toolsets)
    current = _normalize_toolsets(
        platform_toolsets.get(plugin_id),
        f"platform_toolsets.{plugin_id}",
    )
    merged = _dedupe([*current, *required])
    if current == merged:
        return False
    platform_toolsets[plugin_id] = merged
    data["platform_toolsets"] = platform_toolsets
    return True


def apply(data: dict[str, Any], plugin_id: str, *, remove: bool = False) -> bool:
    """Mutate ``data`` to enable/disable the given ``plugin_id``.

    Returns True iff ``data`` was modified.
    """
    changed = False
    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise SystemExit("plugins: must be a mapping; refusing to edit")
    enabled = plugins.get("enabled")
    if enabled is None:
        enabled = []
    if not isinstance(enabled, list):
        raise SystemExit(
            f"plugins.enabled must be a list; got {type(enabled).__name__}"
        )
    # Normalize to list[str] (PyYAML may parse bare strings as plain str).
    current = [str(item) for item in enabled if isinstance(item, (str, int))]

    if remove:
        if plugin_id in current:
            plugins["enabled"] = [p for p in current if p != plugin_id]
            data["plugins"] = plugins
            changed = True
    else:
        if plugin_id not in current:
            plugins["enabled"] = [*current, plugin_id]
            data["plugins"] = plugins
            changed = True
        changed = ensure_platform_toolsets(data, plugin_id) or changed
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", required=True, help="Path to ~/.hermes/config.yaml")
    parser.add_argument("--plugin-id", required=True, help="Plugin id (e.g. infoflow)")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove the plugin id from plugins.enabled instead of adding it",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resulting YAML without writing it",
    )
    args = parser.parse_args(argv)

    path = Path(args.config_file).expanduser()
    data = _load(path)
    changed = apply(data, args.plugin_id, remove=args.remove)

    if not changed:
        print(f"[edit_hermes_config] no change needed ({path}: plugin already in expected state)")
        return 0

    if args.dry_run:
        print(f"[edit_hermes_config] (dry-run) would write to {path}:")
        print(
            yaml.safe_dump(
                data,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )
        )
        return 0

    _save(path, data)
    print(f"[edit_hermes_config] updated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
