"""Recall helpers for the Infoflow adapter.

Extracted from ``adapter.py`` so that inbound-context bookkeeping,
recall-intent heuristics, and correction logic live in one focused
module.  The adapter delegates here; tests can import directly.

Mirrors openclaw-infoflow:

* ``inbound-context.ts``  → :class:`_InboundContext`, registry helpers
* ``recall-intent.ts``    → regexes and ``_looks_like_*`` heuristics
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — avoid circular import at runtime
    from .sent_store import SentMessage, SentMessageStore

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Inbound-context registry (mirrors openclaw-infoflow/src/inbound-context.ts).
#
# When the LLM later asks to "delete" a message it sometimes passes the
# inbound user message_id by mistake.  Recording the inbound's quote-reply
# targets here lets the recall-correction logic swap in the correct
# bot-sent messageid.  Bounded by both TTL and a hard max size.
# ---------------------------------------------------------------------------

_INBOUND_CTX_RETENTION_SECONDS = 10 * 60  # 10 minutes — matches OpenClaw
_INBOUND_CTX_MAX_ENTRIES = 500


@dataclass
class _InboundContext:
    """Snapshot of an inbound message's reply context for later recall recovery."""

    account_id: str
    target: str
    inbound_message_id: str
    reply_to_bot_message_id: str | None
    reply_targets: list[dict[str, Any]]
    inbound_body: str
    registered_at: float
    sender_imid: str = ""
    sender_id: str = ""  # uuapName for humans, "" for bots
    sender_agent_id: str = ""  # agentId (int-as-str) for bots, "" for humans
    msgseqid: str | None = None
    msgid2: str | None = None  # top-level webhook msgid2 (group; emoji API)


_inbound_ctx_store: dict[str, _InboundContext] = {}


def _register_inbound_context(record: _InboundContext) -> None:
    """Insert ``record`` into the in-process registry, evicting old entries."""
    now = record.registered_at
    cutoff = now - _INBOUND_CTX_RETENTION_SECONDS
    # Lazy TTL sweep.
    if _inbound_ctx_store:
        expired = [k for k, v in _inbound_ctx_store.items() if v.registered_at < cutoff]
        for k in expired:
            _inbound_ctx_store.pop(k, None)
    # Hard size cap — drop oldest first.
    if len(_inbound_ctx_store) >= _INBOUND_CTX_MAX_ENTRIES:
        oldest = sorted(_inbound_ctx_store.items(), key=lambda kv: kv[1].registered_at)
        for k, _v in oldest[: len(_inbound_ctx_store) - _INBOUND_CTX_MAX_ENTRIES + 1]:
            _inbound_ctx_store.pop(k, None)
    _inbound_ctx_store[record.inbound_message_id] = record


def _lookup_inbound_context(inbound_message_id: str) -> _InboundContext | None:
    """Return the registered context for ``inbound_message_id`` (or None)."""
    if not inbound_message_id:
        return None
    rec = _inbound_ctx_store.get(inbound_message_id)
    if rec is None:
        return None
    if time.time() - rec.registered_at > _INBOUND_CTX_RETENTION_SECONDS:
        _inbound_ctx_store.pop(inbound_message_id, None)
        return None
    return rec


# ---------------------------------------------------------------------------
# Recall-intent heuristics (mirrors openclaw-infoflow/src/recall-intent.ts).
# ---------------------------------------------------------------------------

_RECALL_INTENT_RE = re.compile(
    r"(撤回|收回|删[掉了除]|取消|清除|recall|unsend|undo\s*send|delete\s+(?:that|those|the\s+(?:last|previous(?:\s+\d+)?)))",
    re.IGNORECASE,
)
_RECALL_LATEST_HINT_RE = re.compile(
    r"(上一?条|最后一?条|刚才那?条|最近一?条|last(?:\s+(?:one|message|two|few|reply))?|previous|most\s*recent)",
    re.IGNORECASE | re.UNICODE,
)


def _looks_like_recall_intent(text: str) -> bool:
    return bool(text) and bool(_RECALL_INTENT_RE.search(text))


def _looks_like_recall_latest(text: str) -> bool:
    return _looks_like_recall_intent(text) and bool(_RECALL_LATEST_HINT_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Standalone correction / format helpers
#
# These are extracted from the adapter's ``self`` methods and accept
# explicit ``sent_store`` / ``account_id`` parameters so they stay
# dependency-free (no circular import back into ``adapter``).
# ---------------------------------------------------------------------------


def correct_inbound_confusion(
    *,
    inbound_message_id: str,
    store_key: str,
    account_id: str,
    sent_store: SentMessageStore,
) -> dict[str, Any] | None:
    """Return a correction directive, or None if context isn't actionable.

    ``store_key`` is the already-normalized chat_id (no ``infoflow:``
    prefix).  Mirrors openclaw-infoflow ``applyAggressiveGuardForInboundMessageId``.
    """
    ctx = _lookup_inbound_context(inbound_message_id)
    if ctx is None:
        return None
    if ctx.account_id != account_id:
        return None
    # Scope check: same chat target.
    if ctx.target != store_key:
        return None
    # Priority 1: swap to bot-message quote-reply target.
    if ctx.reply_to_bot_message_id and sent_store.find(
        store_key, ctx.reply_to_bot_message_id
    ):
        return {"kind": "swap", "message_id": ctx.reply_to_bot_message_id}
    # Priority 2: clear "recall the latest" intent → drop to count=1.
    if _looks_like_recall_latest(ctx.inbound_body):
        return {"kind": "drop_to_count"}
    return None


def reply_to_bot_from_current_inbound(
    *,
    current_inbound_message_id: str,
    store_key: str,
    account_id: str,
    sent_store: SentMessageStore,
) -> SentMessage | None:
    """Resolve a stored bot-sent message from the current inbound's quote-reply.

    Mirrors openclaw-infoflow ``resolveInboundReplyToMessageId`` + store lookup.
    """
    ctx = _lookup_inbound_context(current_inbound_message_id)
    if ctx is None:
        return None
    if ctx.account_id != account_id:
        return None
    if ctx.target != store_key:
        return None
    bid = ctx.reply_to_bot_message_id
    if not bid:
        return None
    return sent_store.find(store_key, bid)


def format_recall_candidates(sent_store: SentMessageStore, store_key: str, limit: int = 5) -> str:
    """Format the last ``limit`` bot-sent messages for an error hint to the LLM."""
    records = sent_store.recent(store_key, limit)
    if not records:
        return ""
    return "; ".join(
        f"messageId={r.messageid} preview=\"{r.digest or '(no preview)'}\""
        for r in records
    )


def no_recall_error(sent_store: SentMessageStore, store_key: str) -> str:
    """Human-readable error when no bot messages are available for recall."""
    candidates = format_recall_candidates(sent_store, store_key)
    if candidates:
        return (
            "no recent bot messages to recall on this chat. "
            f"Recent bot-sent messages: {candidates}."
        )
    return "no recent bot messages to recall"


def get_inbound_body(message_id: str) -> str | None:
    """Return the original inbound body text for ``message_id``, or None.

    Used by the adapter to populate the reply preview when the bot
    replies to a user message. The body is the pure text (no @mention
    tags) as stored in ``_InboundContext.inbound_body``.
    """
    ctx = _lookup_inbound_context(message_id)
    if ctx is None:
        return None
    return ctx.inbound_body or None


def get_inbound_target(message_id: str) -> str:
    """Return the Infoflow chat target for an inbound message, or empty string."""
    ctx = _lookup_inbound_context(message_id)
    if ctx is None:
        return ""
    return ctx.target or ""


def get_inbound_sender_imid(message_id: str) -> str:
    """Return the sender's imid for ``message_id``, or empty string."""
    ctx = _lookup_inbound_context(message_id)
    if ctx is None:
        return ""
    return ctx.sender_imid or ""


def get_inbound_sender_id(message_id: str) -> str:
    """Return the sender's canonical identity for ``message_id``.

    Human: uuapName (e.g. "chengbo05").
    Bot: agentId as str (e.g. "12345").
    Falls back to imid if neither is available.
    """
    ctx = _lookup_inbound_context(message_id)
    if ctx is None:
        return ""
    if ctx.sender_agent_id:
        return ctx.sender_agent_id
    if ctx.sender_id:
        return ctx.sender_id
    return ctx.sender_imid or ""


def get_inbound_msgseqid(message_id: str) -> str | None:
    """Return the msgseqid for an inbound message, or None.

    Used by the adapter to include msgseqid in reply payloads for
    more accurate sender identification by the Infoflow server.
    """
    ctx = _lookup_inbound_context(message_id)
    if ctx is None:
        return None
    return ctx.msgseqid
