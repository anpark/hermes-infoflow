"""Helpers for LLM-visible structured tags."""

from __future__ import annotations


def quote_tag_value(value: object) -> str:
    """Return a single-quoted field value for LLM-visible metadata tags."""
    text = " ".join(str(value or "").split())
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def string_field(name: str, value: object) -> str:
    return f"{name}:{quote_tag_value(value)}"
