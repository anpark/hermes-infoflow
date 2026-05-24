"""Small coercion helpers shared across parser/service boundaries."""

from __future__ import annotations

from typing import Any

_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"", "0", "false", "no", "off", "none", "null"}


def coerce_bool(value: Any, *, default: bool = False) -> bool:
    """Return a conservative boolean for wire/config/test fixture values."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
        return default
    return bool(value)


def first_present(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first key present in a mapping, preserving explicit false."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


__all__ = ["coerce_bool", "first_present"]
