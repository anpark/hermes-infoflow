"""Shared config.yaml editing helpers for deploy and installer tools."""

from __future__ import annotations

from typing import Any

INFOFLOW_TOOLSET = "infoflow"
LEGACY_INFOFLOW_TOOLSETS = ("hermes-infoflow",)

DEFAULT_INFOFLOW_PLATFORM_TOOLSETS = (
    "browser",
    "clarify",
    "code_execution",
    "computer_use",
    "cronjob",
    "delegation",
    "file",
    INFOFLOW_TOOLSET,
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


def normalize_toolsets(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SystemExit(f"{label} must be a list; got {type(value).__name__}")
    return [
        str(item)
        for item in value
        if isinstance(item, (str, int)) and str(item).strip()
    ]


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def migrate_legacy_infoflow_toolsets(items: list[str]) -> list[str]:
    return dedupe([
        INFOFLOW_TOOLSET if item in LEGACY_INFOFLOW_TOOLSETS else item
        for item in items
    ])


def migrate_platform_toolsets(data: dict[str, Any]) -> bool:
    platform_toolsets = data.get("platform_toolsets")
    if not isinstance(platform_toolsets, dict):
        return False

    changed = False
    for platform, value in list(platform_toolsets.items()):
        if not isinstance(value, list):
            continue
        current = normalize_toolsets(value, f"platform_toolsets.{platform}")
        migrated = migrate_legacy_infoflow_toolsets(current)
        if current != migrated:
            platform_toolsets[platform] = migrated
            changed = True
    return changed


def migrate_known_plugin_toolsets(data: dict[str, Any]) -> bool:
    known_plugin_toolsets = data.get("known_plugin_toolsets")
    if not isinstance(known_plugin_toolsets, dict):
        return False

    changed = False
    for platform, value in list(known_plugin_toolsets.items()):
        if not isinstance(value, list):
            continue
        current = normalize_toolsets(value, f"known_plugin_toolsets.{platform}")
        migrated = migrate_legacy_infoflow_toolsets(current)
        if current != migrated:
            known_plugin_toolsets[platform] = migrated
            changed = True
    return changed


def reference_toolsets(platform_toolsets: dict[str, Any]) -> list[str]:
    cli_toolsets = migrate_legacy_infoflow_toolsets(
        normalize_toolsets(
            platform_toolsets.get("cli"),
            "platform_toolsets.cli",
        )
    )
    return dedupe([*cli_toolsets, *DEFAULT_INFOFLOW_PLATFORM_TOOLSETS])


def ensure_platform_toolsets(data: dict[str, Any], plugin_id: str) -> bool:
    """Ensure ``platform_toolsets.<plugin_id>`` can call the standard tools."""
    platform_toolsets = data.setdefault("platform_toolsets", {})
    if not isinstance(platform_toolsets, dict):
        raise SystemExit("platform_toolsets: must be a mapping; refusing to edit")

    required = reference_toolsets(platform_toolsets)
    original = normalize_toolsets(
        platform_toolsets.get(plugin_id),
        f"platform_toolsets.{plugin_id}",
    )
    current = migrate_legacy_infoflow_toolsets(original)
    merged = dedupe([*current, *required])
    if original == merged:
        return False
    platform_toolsets[plugin_id] = merged
    data["platform_toolsets"] = platform_toolsets
    return True


def apply(data: dict[str, Any], plugin_id: str, *, remove: bool = False) -> bool:
    """Mutate ``data`` to enable/disable the given ``plugin_id``.

    Enabling also grants Infoflow sessions the current CLI baseline tool
    permissions. This is intentional for the current product phase: the
    Infoflow integration prioritizes operational efficiency over fine-grained
    per-tool security gating.

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
    current = [str(item) for item in enabled if isinstance(item, (str, int))]

    if remove:
        if plugin_id in current:
            plugins["enabled"] = [p for p in current if p != plugin_id]
            data["plugins"] = plugins
            changed = True
    else:
        changed = migrate_platform_toolsets(data) or changed
        changed = migrate_known_plugin_toolsets(data) or changed
        if plugin_id not in current:
            plugins["enabled"] = [*current, plugin_id]
            data["plugins"] = plugins
            changed = True
        changed = ensure_platform_toolsets(data, plugin_id) or changed
    return changed
