"""Small recall-success silence guard.

The main contract is still the tool result + ``NO_REPLY`` prompt. This module
only catches short, exact recall acknowledgements after the recall tool has
already succeeded in the same inbound turn.
"""

from __future__ import annotations

import time

_ACK_STRIP_CHARS = " \t\r\n。.!！?？~～"
_ACK_MAX_CHARS = 24
_RECALL_ACK_TEXTS = frozenset({
    "已撤回",
    "已经撤回",
    "撤回成功",
    "消息已撤回",
    "已帮你撤回",
    "已为你撤回",
    "已撤回这条消息",
    "这条消息已撤回",
    "已帮你撤回这条消息",
    "已为你撤回这条消息",
})


def normalize_recall_chat_id(chat_id: str) -> str:
    """Normalize an Infoflow chat id for turn-local recall tracking."""
    raw = str(chat_id or "")
    if raw.startswith("infoflow:"):
        return raw[len("infoflow:"):]
    return raw


def is_recall_ack_only(text: str | None) -> bool:
    """True only for a tiny whitelist of pure recall success acknowledgements."""
    raw = str(text or "").strip()
    if not raw or len(raw) > _ACK_MAX_CHARS or "\n" in raw:
        return False
    compact = "".join(raw.strip(_ACK_STRIP_CHARS).split())
    return compact in _RECALL_ACK_TEXTS


class RecallSilenceTracker:
    """Track recall success per inbound turn and suppress exact ack text."""

    def __init__(self, *, ttl_seconds: float = 60.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._state: dict[str, tuple[str, float]] = {}

    def mark_success(
        self,
        *,
        inbound_mid: str,
        chat_id: str,
        now: float | None = None,
    ) -> None:
        if not inbound_mid:
            return
        effective_now = time.monotonic() if now is None else now
        self._prune(effective_now)
        self._state[str(inbound_mid)] = (
            normalize_recall_chat_id(chat_id),
            effective_now + self._ttl_seconds,
        )

    def consume_if_suppress(
        self,
        *,
        inbound_mid: str,
        chat_id: str,
        text: str | None,
        now: float | None = None,
    ) -> bool:
        if not inbound_mid or not is_recall_ack_only(text):
            return False
        effective_now = time.monotonic() if now is None else now
        record = self._state.get(str(inbound_mid))
        if record is None:
            self._prune(effective_now)
            return False
        stored_chat_id, expires_at = record
        if expires_at <= effective_now:
            self._state.pop(str(inbound_mid), None)
            return False
        if stored_chat_id != normalize_recall_chat_id(chat_id):
            return False
        self._state.pop(str(inbound_mid), None)
        return True

    def _prune(self, now: float) -> None:
        expired = [
            mid
            for mid, (_chat_id, expires_at) in self._state.items()
            if expires_at <= now
        ]
        for mid in expired:
            self._state.pop(mid, None)
