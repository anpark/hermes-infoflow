"""Recent-outbound-message tracking + self-reflection dedup.

Two responsibilities that *must* share one underlying set:

1. **Self-reflection guard** — Infoflow occasionally replays bot-sent
   messages back to the webhook (e.g. ``MESSAGE_RECEIVE`` events or the
   ``ALL_MESSAGE_FORWARD`` mirror). Recording every outbound ``message_id``
   we send into the same dedup set inbound webhooks consult lets us drop
   them as duplicates and avoid an infinite reply loop.

2. **By-count recall** — ``infoflow_recall_message count=N`` (without a
   specific message_id) looks up the N most recently sent messages on a
   chat to recall. Keeps a small ring buffer per chat_id.

A single ``set[str]`` membership store + a ``deque[Sent]`` per chat
satisfy both. Entries expire after 5 minutes (matches OpenClaw's dedup
TTL); a lazy sweep runs on every record/touch.

Process-local only. Cross-process recall (e.g. cron sub-process) by-count
is intentionally unsupported — that path can only recall by explicit id.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

DEFAULT_TTL_SECONDS = 5 * 60
DEFAULT_PER_CHAT_LIMIT = 50


@dataclass(frozen=True)
class SentMessage:
    """One outbound message record for by-count recall."""

    chat_id: str
    messageid: str
    msgseqid: str = ""
    digest: str = ""        # short text fingerprint for debug / future filters
    sent_at_ms: int = 0


@dataclass
class SentMessageStore:
    """Combined "recent sent" buffer and dedup membership set.

    ``dedup_set`` is the *shared* set the inbound webhook handler also
    consults. Pass the same set in to both this store and the adapter so
    self-reflection (bot reading its own message back) drops as a dup.

    The dedup set is intentionally not bounded: we sweep TTL-expired
    entries on every ``record()`` / ``mark_seen()`` call. In a sane
    production load (a few messages per second) the working set stays
    in the low hundreds.
    """

    dedup_set: set[str] = field(default_factory=set)
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    per_chat_limit: int = DEFAULT_PER_CHAT_LIMIT
    _entries: dict[str, deque[SentMessage]] = field(default_factory=dict)
    _expiry: dict[str, float] = field(default_factory=dict)  # messageid → expiry_ts (s)

    # -- Mutation ---------------------------------------------------------

    def record(
        self,
        chat_id: str,
        messageid: str,
        *,
        msgseqid: str = "",
        digest: str = "",
        now: float | None = None,
    ) -> SentMessage:
        """Add an outbound message; also marks ``messageid`` as seen-for-dedup."""
        ts = now if now is not None else time.time()
        self._sweep(ts)
        entry = SentMessage(
            chat_id=chat_id,
            messageid=messageid,
            msgseqid=msgseqid,
            digest=digest,
            sent_at_ms=int(ts * 1000),
        )
        buf = self._entries.setdefault(chat_id, deque(maxlen=self.per_chat_limit))
        buf.append(entry)
        if messageid:
            self.dedup_set.add(messageid)
            self._expiry[messageid] = ts + self.ttl_seconds
        return entry

    def mark_seen(self, messageid: str, *, now: float | None = None) -> None:
        """Mark a foreign message_id as seen so future replays are dropped.

        Inbound parser calls this with the dedup key after dispatch so a
        repeated webhook arrives as a no-op.
        """
        if not messageid:
            return
        ts = now if now is not None else time.time()
        self._sweep(ts)
        self.dedup_set.add(messageid)
        self._expiry[messageid] = ts + self.ttl_seconds

    def is_duplicate(self, messageid: str, *, now: float | None = None) -> bool:
        """Return True iff this ``messageid`` is already in the dedup set."""
        if not messageid:
            return False
        ts = now if now is not None else time.time()
        self._sweep(ts)
        return messageid in self.dedup_set

    # -- Query ------------------------------------------------------------

    def recent(self, chat_id: str, count: int = 1) -> list[SentMessage]:
        """Return the ``count`` most-recently-sent messages on ``chat_id``."""
        if count <= 0:
            return []
        buf = self._entries.get(chat_id)
        if not buf:
            return []
        return list(reversed(list(buf)[-count:]))

    def find(self, chat_id: str, messageid: str) -> SentMessage | None:
        """Return the matching sent entry (or None)."""
        buf = self._entries.get(chat_id)
        if not buf:
            return None
        for entry in reversed(buf):
            if entry.messageid == messageid:
                return entry
        return None

    def all_messageids(self) -> Iterable[str]:
        """Iterate every tracked ``messageid`` (across all chats)."""
        for buf in self._entries.values():
            for entry in buf:
                yield entry.messageid

    # -- Internals --------------------------------------------------------

    def _sweep(self, now: float) -> None:
        """Drop dedup entries whose TTL has elapsed."""
        if not self._expiry:
            return
        expired = [mid for mid, exp in self._expiry.items() if exp <= now]
        for mid in expired:
            self.dedup_set.discard(mid)
            self._expiry.pop(mid, None)


__all__ = ["DEFAULT_TTL_SECONDS", "DEFAULT_PER_CHAT_LIMIT", "SentMessage", "SentMessageStore"]
