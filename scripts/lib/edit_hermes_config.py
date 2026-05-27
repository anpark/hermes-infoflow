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
  permissions as the CLI platform, including the ``infoflow`` plugin toolset.
* Preserves every other key by round-tripping through PyYAML.

Usage::

    python3 edit_hermes_config.py \\
        --config-file ~/.hermes/config.yaml \\
        --plugin-id infoflow [--remove] [--dry-run]
"""

from __future__ import annotations

import argparse
import importlib.util
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


def _load_config_editor():
    self_dir = Path(__file__).resolve().parent
    candidates = [
        # Source checkout: scripts/lib/edit_hermes_config.py -> hermes_infoflow/config_editor.py
        self_dir.parent.parent / "hermes_infoflow" / "config_editor.py",
        # Flattened deploy layout: plugin_dir/scripts/lib/edit_hermes_config.py -> plugin_dir/config_editor.py
        self_dir.parent.parent / "config_editor.py",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("hermes_infoflow_config_editor", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    try:
        from hermes_infoflow import config_editor as module
    except Exception as exc:  # pragma: no cover - defensive deploy path
        raise SystemExit("Cannot find hermes_infoflow config_editor.py") from exc
    return module


_CONFIG_EDITOR = _load_config_editor()
DEFAULT_INFOFLOW_PLATFORM_TOOLSETS = _CONFIG_EDITOR.DEFAULT_INFOFLOW_PLATFORM_TOOLSETS


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


def ensure_platform_toolsets(data: dict[str, Any], plugin_id: str) -> bool:
    """Ensure ``platform_toolsets.<plugin_id>`` can call the standard tools."""
    return _CONFIG_EDITOR.ensure_platform_toolsets(data, plugin_id)


def apply(data: dict[str, Any], plugin_id: str, *, remove: bool = False) -> bool:
    """Mutate ``data`` to enable/disable the given ``plugin_id``.

    Returns True iff ``data`` was modified.
    """
    return _CONFIG_EDITOR.apply(data, plugin_id, remove=remove)


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
