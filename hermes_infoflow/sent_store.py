"""Recent-outbound-message tracking + self-reflection dedup.

Two responsibilities that *must* share one underlying set:

1. **Self-reflection guard** — Infoflow occasionally replays bot-sent
   messages back to the webhook (e.g. ``MESSAGE_RECEIVE`` events or the
   ``ALL_MESSAGE_FORWARD`` mirror). Recording every outbound ``message_id``
   we send into the same dedup set inbound webhooks consult lets us drop
   them as duplicates and avoid an infinite reply loop.

2. **By-count / by-id recall** — ``infoflow_recall_message count=N``
   looks up the N most recently sent messages on a chat to recall.
   ``message_id`` queries verify the LLM passed a valid bot-sent id.

The class supports two storage layers, chosen at construction time:

* **Always-on in-memory layer**: a ``set[str]`` membership store + a
  ``deque[Sent]`` per chat. Drives the hot path (dedup check on every
  inbound webhook). Bounded by both a TTL (``ttl_seconds``) and a hard
  size cap (``max_dedup_entries``) so a runaway workload can't blow up
  the process's RSS.

* **Optional SQLite layer**: when ``db_path`` is supplied, each ``record``
  is also persisted to a local SQLite file (using Python's bundled
  ``sqlite3``). Subsequent reads (``find`` / ``recent``) merge in entries
  from the database, so cron sub-processes and adapter restarts can still
  recall messages within the 7-day retention window. This mirrors
  openclaw-infoflow/src/sent-message-store.ts.

The dedup-set TTL still applies (5 minutes by default) — SQLite is for
recall-history persistence, not for the inbound-replay dedup window.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 5 * 60
DEFAULT_PER_CHAT_LIMIT = 50
DEFAULT_MAX_DEDUP_ENTRIES = 1000           # matches OpenClaw DEDUP_MAX_SIZE
DEFAULT_DB_RETENTION_SECONDS = 7 * 24 * 60 * 60   # 7 days; matches OpenClaw AUTO_CLEANUP_DAYS


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

    Bounded by two limits: ``ttl_seconds`` (lazy-swept on every mutation)
    and ``max_dedup_entries`` (hard ceiling — when exceeded, oldest entries
    are evicted in insertion order via the parallel ``_expiry`` mapping).

    When ``db_path`` is provided, every ``record`` also flushes to a local
    SQLite file. ``find`` / ``recent`` then transparently merge in-memory
    and persisted rows so cron sub-processes can still see history from
    the live adapter.
    """

    dedup_set: set[str] = field(default_factory=set)
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    per_chat_limit: int = DEFAULT_PER_CHAT_LIMIT
    max_dedup_entries: int = DEFAULT_MAX_DEDUP_ENTRIES
    db_path: Path | str | None = None
    db_retention_seconds: float = DEFAULT_DB_RETENTION_SECONDS
    account_id: str = "default"

    _entries: dict[str, deque[SentMessage]] = field(default_factory=dict)
    # Insertion-ordered expiry map: messageid -> expiry_ts (s). Using
    # OrderedDict lets us prune both by TTL and by size in O(1) per drop.
    _expiry: OrderedDict[str, float] = field(default_factory=OrderedDict)
    _db_initialized: bool = False
    _db_lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

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
            # Drop any prior expiry mapping so it re-inserts at the tail (FIFO).
            self._expiry.pop(messageid, None)
            self._expiry[messageid] = ts + self.ttl_seconds
            self._enforce_max_size()
        self._persist(entry)
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
        self._expiry.pop(messageid, None)
        self._expiry[messageid] = ts + self.ttl_seconds
        self._enforce_max_size()

    def is_duplicate(self, messageid: str, *, now: float | None = None) -> bool:
        """Return True iff this ``messageid`` is already in the dedup set."""
        if not messageid:
            return False
        ts = now if now is not None else time.time()
        self._sweep(ts)
        return messageid in self.dedup_set

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def recent(self, chat_id: str, count: int = 1) -> list[SentMessage]:
        """Return up to ``count`` most-recently-sent messages on ``chat_id``.

        Merges in-memory entries with persisted DB rows (when configured),
        de-duplicated by ``messageid`` and sorted newest-first.
        """
        if count <= 0:
            return []
        merged: dict[str, SentMessage] = {}
        buf = self._entries.get(chat_id)
        if buf:
            for entry in reversed(buf):
                if entry.messageid not in merged:
                    merged[entry.messageid] = entry
                if len(merged) >= count:
                    return list(merged.values())[:count]
        for db_entry in self._db_recent(chat_id, count):
            if db_entry.messageid in merged:
                continue
            merged[db_entry.messageid] = db_entry
            if len(merged) >= count:
                break
        ordered = sorted(merged.values(), key=lambda e: e.sent_at_ms, reverse=True)
        return ordered[:count]

    def find(self, chat_id: str, messageid: str) -> SentMessage | None:
        """Return the matching sent entry (or None) for ``messageid`` on ``chat_id``."""
        if not messageid:
            return None
        buf = self._entries.get(chat_id)
        if buf:
            for entry in reversed(buf):
                if entry.messageid == messageid:
                    return entry
        return self._db_find(chat_id, messageid)

    def find_any(self, messageid: str) -> SentMessage | None:
        """Find ``messageid`` across all chats (in-memory + DB)."""
        if not messageid:
            return None
        for buf in self._entries.values():
            for entry in reversed(buf):
                if entry.messageid == messageid:
                    return entry
        return self._db_find_any(messageid)

    def all_messageids(self) -> Iterable[str]:
        """Iterate every tracked ``messageid`` (across all chats, in-memory only)."""
        for buf in self._entries.values():
            for entry in buf:
                yield entry.messageid

    def remove(self, chat_id: str, messageid: str) -> None:
        """Drop ``messageid`` from both in-memory state and the SQLite store."""
        buf = self._entries.get(chat_id)
        if buf:
            kept = deque((e for e in buf if e.messageid != messageid), maxlen=self.per_chat_limit)
            self._entries[chat_id] = kept
        self.dedup_set.discard(messageid)
        self._expiry.pop(messageid, None)
        self._db_delete(chat_id, messageid)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sweep(self, now: float) -> None:
        """Drop dedup entries whose TTL has elapsed."""
        if not self._expiry:
            return
        # OrderedDict iteration is insertion-ordered, so the first entries are
        # the ones inserted earliest — we can stop as soon as we see a non-expired one
        # since later entries necessarily have later expiry.
        to_drop: list[str] = []
        for mid, exp in self._expiry.items():
            if exp <= now:
                to_drop.append(mid)
            else:
                break
        for mid in to_drop:
            self.dedup_set.discard(mid)
            self._expiry.pop(mid, None)

    def _enforce_max_size(self) -> None:
        """Drop oldest entries while the size cap is exceeded."""
        if self.max_dedup_entries <= 0:
            return
        while len(self._expiry) > self.max_dedup_entries:
            mid, _ = self._expiry.popitem(last=False)
            self.dedup_set.discard(mid)

    # ----- SQLite backing -----

    # Connection-acquisition timeout (s) — generous, since we hold the
    # connection only for the duration of a single statement under WAL.
    _DB_TIMEOUT_SECONDS = 30.0
    # busy_timeout (ms) — how long SQLite will spin on a locked DB before
    # raising. WAL mode mostly avoids this but multiple writer processes can
    # still collide on the WAL header.
    _DB_BUSY_TIMEOUT_MS = 5_000

    def _ensure_db(self) -> sqlite3.Connection | None:
        if self.db_path is None:
            return None
        try:
            db_file = Path(self.db_path).expanduser()
            db_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            conn = sqlite3.connect(
                str(db_file),
                isolation_level=None,
                timeout=self._DB_TIMEOUT_SECONDS,
                # Per-instance connections are short-lived but the same process
                # may run multiple threads (asyncio + cron worker spawning).
                # Allow cross-thread reuse of the connection handle.
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            logger.warning("[infoflow:sent_store] sqlite connect failed: %s", exc)
            return None

        # PRAGMA statements may transiently fail with "database is locked"
        # when multiple connections initialize concurrently. WAL / busy_timeout
        # / synchronous settings persist at the DB level once any process
        # has set them, so silently swallowing here is safe — a peer just
        # got there first. The connect itself is what matters.
        for pragma in (
            "PRAGMA journal_mode=WAL",
            f"PRAGMA busy_timeout={self._DB_BUSY_TIMEOUT_MS}",
            "PRAGMA synchronous=NORMAL",
        ):
            try:
                conn.execute(pragma)
            except sqlite3.Error:
                pass

        if not self._db_initialized:
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sent_messages (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id  TEXT NOT NULL,
                        chat_id     TEXT NOT NULL,
                        messageid   TEXT NOT NULL,
                        msgseqid    TEXT NOT NULL DEFAULT '',
                        digest      TEXT NOT NULL DEFAULT '',
                        sent_at     INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_sent "
                    "ON sent_messages(account_id, chat_id, sent_at DESC)"
                )
                self._db_initialized = True
            except sqlite3.Error as exc:
                # Idempotent CREATE IF NOT EXISTS: if a peer already created
                # the table, our statement is a no-op. A "locked" failure is
                # benign (peer will finish its schema setup soon). Don't mark
                # _db_initialized so we retry on next call.
                logger.debug(
                    "[infoflow:sent_store] schema init deferred: %s", exc
                )
        return conn

    def _persist(self, entry: SentMessage) -> None:
        if self.db_path is None or not entry.messageid:
            return
        with self._db_lock:
            conn = self._ensure_db()
            if conn is None:
                return
            try:
                conn.execute(
                    "INSERT INTO sent_messages "
                    "(account_id, chat_id, messageid, msgseqid, digest, sent_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.account_id,
                        entry.chat_id,
                        entry.messageid,
                        entry.msgseqid,
                        entry.digest,
                        entry.sent_at_ms,
                    ),
                )
                # Best-effort retention sweep. We anchor the cutoff to the
                # entry's own timestamp (not wall-clock) so callers driving
                # tests with deterministic small timestamps don't accidentally
                # nuke their own freshly-inserted records.
                cutoff_ms = entry.sent_at_ms - int(self.db_retention_seconds * 1000)
                conn.execute(
                    "DELETE FROM sent_messages WHERE account_id = ? AND sent_at < ?",
                    (self.account_id, cutoff_ms),
                )
            except sqlite3.Error as exc:
                logger.warning("[infoflow:sent_store] persist failed: %s", exc)
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _db_recent(self, chat_id: str, count: int) -> list[SentMessage]:
        if self.db_path is None:
            return []
        with self._db_lock:
            conn = self._ensure_db()
            if conn is None:
                return []
            try:
                cur = conn.execute(
                    "SELECT chat_id, messageid, msgseqid, digest, sent_at "
                    "FROM sent_messages WHERE account_id = ? AND chat_id = ? "
                    "ORDER BY sent_at DESC LIMIT ?",
                    (self.account_id, chat_id, count),
                )
                return [
                    SentMessage(
                        chat_id=row[0],
                        messageid=row[1],
                        msgseqid=row[2] or "",
                        digest=row[3] or "",
                        sent_at_ms=int(row[4] or 0),
                    )
                    for row in cur.fetchall()
                ]
            except sqlite3.Error as exc:
                logger.warning("[infoflow:sent_store] db_recent failed: %s", exc)
                return []
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _db_find(self, chat_id: str, messageid: str) -> SentMessage | None:
        if self.db_path is None:
            return None
        with self._db_lock:
            conn = self._ensure_db()
            if conn is None:
                return None
            try:
                cur = conn.execute(
                    "SELECT chat_id, messageid, msgseqid, digest, sent_at "
                    "FROM sent_messages WHERE account_id = ? AND chat_id = ? "
                    "AND messageid = ? LIMIT 1",
                    (self.account_id, chat_id, messageid),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return SentMessage(
                    chat_id=row[0],
                    messageid=row[1],
                    msgseqid=row[2] or "",
                    digest=row[3] or "",
                    sent_at_ms=int(row[4] or 0),
                )
            except sqlite3.Error as exc:
                logger.warning("[infoflow:sent_store] db_find failed: %s", exc)
                return None
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _db_find_any(self, messageid: str) -> SentMessage | None:
        if self.db_path is None:
            return None
        with self._db_lock:
            conn = self._ensure_db()
            if conn is None:
                return None
            try:
                cur = conn.execute(
                    "SELECT chat_id, messageid, msgseqid, digest, sent_at "
                    "FROM sent_messages WHERE account_id = ? AND messageid = ? LIMIT 1",
                    (self.account_id, messageid),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return SentMessage(
                    chat_id=row[0],
                    messageid=row[1],
                    msgseqid=row[2] or "",
                    digest=row[3] or "",
                    sent_at_ms=int(row[4] or 0),
                )
            except sqlite3.Error as exc:
                logger.warning("[infoflow:sent_store] db_find_any failed: %s", exc)
                return None
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def _db_delete(self, chat_id: str, messageid: str) -> None:
        if self.db_path is None:
            return
        with self._db_lock:
            conn = self._ensure_db()
            if conn is None:
                return
            try:
                conn.execute(
                    "DELETE FROM sent_messages WHERE account_id = ? AND chat_id = ? "
                    "AND messageid = ?",
                    (self.account_id, chat_id, messageid),
                )
            except sqlite3.Error as exc:
                logger.warning("[infoflow:sent_store] db_delete failed: %s", exc)
            finally:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass


__all__ = [
    "DEFAULT_DB_RETENTION_SECONDS",
    "DEFAULT_MAX_DEDUP_ENTRIES",
    "DEFAULT_PER_CHAT_LIMIT",
    "DEFAULT_TTL_SECONDS",
    "SentMessage",
    "SentMessageStore",
]
