"""Shared config.yaml editing helpers for deploy and installer tools."""

from __future__ import annotations

from typing import Any

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


def reference_toolsets(platform_toolsets: dict[str, Any]) -> list[str]:
    cli_toolsets = normalize_toolsets(
        platform_toolsets.get("cli"),
        "platform_toolsets.cli",
    )
    return dedupe([*cli_toolsets, *DEFAULT_INFOFLOW_PLATFORM_TOOLSETS])


def ensure_platform_toolsets(data: dict[str, Any], plugin_id: str) -> bool:
    """Ensure ``platform_toolsets.<plugin_id>`` can call the standard tools."""
    platform_toolsets = data.setdefault("platform_toolsets", {})
    if not isinstance(platform_toolsets, dict):
        raise SystemExit("platform_toolsets: must be a mapping; refusing to edit")

    required = reference_toolsets(platform_toolsets)
    current = normalize_toolsets(
        platform_toolsets.get(plugin_id),
        f"platform_toolsets.{plugin_id}",
    )
    merged = dedupe([*current, *required])
    if current == merged:
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
        if plugin_id not in current:
            plugins["enabled"] = [*current, plugin_id]
            data["plugins"] = plugins
            changed = True
        changed = ensure_platform_toolsets(data, plugin_id) or changed
    return changed
