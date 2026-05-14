#!/usr/bin/env python3
"""Safely toggle ``plugins.enabled`` in ``~/.hermes/config.yaml``.

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


def apply(data: dict[str, Any], plugin_id: str, *, remove: bool = False) -> bool:
    """Mutate ``data`` to enable/disable the given ``plugin_id``.

    Returns True iff ``data`` was modified.
    """
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
        if plugin_id not in current:
            return False
        new_list = [p for p in current if p != plugin_id]
    else:
        if plugin_id in current:
            return False
        new_list = [*current, plugin_id]

    plugins["enabled"] = new_list
    data["plugins"] = plugins
    return True


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
