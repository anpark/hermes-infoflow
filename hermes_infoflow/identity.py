"""Identity normalization helpers for Infoflow participants.

Canonical participant ids are deliberately typed:

* ``bot:<agent_id>`` for bot accounts
* ``user:<user_id>`` for human accounts

Infoflow ``imid`` / ``robotId`` values are useful for correlation but are not
stable participant identities for the plugin.  In particular, never emit
``bot:<imid>`` when a group bot sender cannot be mapped to an ``agent_id``.
"""

from __future__ import annotations

from typing import Any


def _clean(value: Any) -> str:
    return str(value or "").strip()


def is_degraded_id(value: Any) -> bool:
    return _clean(value).startswith("IMID:")


def is_identity_key(value: Any) -> bool:
    v = _clean(value)
    return v.startswith("bot:") or v.startswith("user:")


def bot_key(agent_id: Any) -> str:
    value = _clean(agent_id)
    if not value or value.startswith("IMID:"):
        return ""
    if value.startswith("bot:"):
        return value
    if value.startswith("user:"):
        return ""
    return f"bot:{value}"


def user_key(user_id: Any) -> str:
    value = _clean(user_id)
    if not value or value.startswith("IMID:"):
        return ""
    if value.startswith("user:"):
        return value
    if value.startswith("bot:"):
        return ""
    return f"user:{value}"


def self_key(settings: dict[str, Any]) -> str:
    return bot_key(settings.get("app_agent_id"))


def sender_key(msg: Any) -> str:
    if getattr(msg, "sender_is_bot", False):
        return bot_key(getattr(msg, "sender_agent_id", "") or "")
    return user_key(getattr(msg, "sender_id", "") or "")


def private_peer_key(user_id: Any) -> str:
    return user_key(user_id)


def raw_id_from_key(value: Any) -> str:
    v = _clean(value)
    if v.startswith("bot:") or v.startswith("user:"):
        return v.split(":", 1)[1]
    return v


__all__ = [
    "bot_key",
    "is_degraded_id",
    "is_identity_key",
    "private_peer_key",
    "raw_id_from_key",
    "self_key",
    "sender_key",
    "user_key",
]
