"""Persistent message fact store for Infoflow.

The store is intentionally not a transcript store for the LLM.  It records
platform message facts in a compact, queryable SQLite schema.  The LLM-facing
message body is generated elsewhere and stored here as ``content`` so messages
that are not dispatched to the model still have the same normalized body text.

Schema compatibility policy: legacy schemas are migrated only for compatible
additive changes.  If the local schema otherwise does not match this version,
old tables are dropped and recreated.

``message_id`` is the authoritative primary key.  Callers intentionally skip
messages without one instead of inventing local IDs, because echo callbacks
and recall APIs must reconcile against the exact Infoflow message identifier.

``created_time`` is first-seen time for that exact ``message_id``.  Outgoing
messages can arrive in either order: send API result first, or echo callback
first.  Whichever path writes the row first owns ``created_time``; later echo
or send-result upserts enrich fields without moving the row in time ordering.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .itypes import GroupMember

logger = logging.getLogger(__name__)

_RETENTION_SECONDS = 7 * 24 * 60 * 60
_ROW_LIMIT = 5000
_DB_TIMEOUT_SECONDS = 30.0
_DB_BUSY_TIMEOUT_MS = 5_000
_STATE_BASE_DIR = Path("~/.hermes/state/infoflow")
_SCHEMA_VERSION = 4

_PRIVATE_COLUMNS = (
    "message_id, peer, self, sender, is_outgoing, local_sent, "
    "quotes_your_message, msg_id2, content, created_time, msg_time, raw_json"
)
_GROUP_COLUMNS = (
    "message_id, group_id, sender, self, is_outgoing, local_sent, "
    "mentions_you, matched_regex_pattern, mentions_everyone, quotes_your_message, "
    "mentions_other_people, quotes_other_peoples_message, msg_id2, content, "
    "created_time, msg_time, raw_json"
)
_PARTICIPANT_COLUMNS = (
    "id, participant_type, agent_id, user_id, imid, name, updated_time"
)
_LLM_CONTEXT_COLUMNS = (
    "llm_context_key, chat_key, last_llm_visible_message_id, "
    "last_llm_visible_created_time, updated_time"
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _participant_key(kind: str, value: str) -> str:
    value = str(value or "").strip()
    if not value or value.startswith("IMID:"):
        return ""
    if value.startswith("bot:") or value.startswith("user:"):
        return value
    return f"{kind}:{value}"


@dataclass(frozen=True)
class PrivateMessageRecord:
    message_id: str
    peer: str
    self_id: str = ""
    sender: str = ""
    is_outgoing: bool = False
    local_sent: bool = False
    quotes_your_message: bool = False
    msg_id2: str = ""
    content: str = ""
    created_time: int = 0
    msg_time: int = 0
    raw_json: str = ""

    # Back-compat aliases for older internal callers/tests.
    @property
    def dm_user_id(self) -> str:
        return self.peer.removeprefix("user:")

    @property
    def sender_id(self) -> str:
        return self.sender.removeprefix("user:").removeprefix("bot:")

    @property
    def sender_name(self) -> str:
        return ""

    @property
    def sender_imid(self) -> str:
        return ""

    @property
    def sender_is_bot(self) -> bool:
        return self.sender.startswith("bot:")

    @property
    def msgid2(self) -> str:
        return self.msg_id2

    @property
    def is_inbound(self) -> bool:
        return not self.is_outgoing

    @property
    def text(self) -> str:
        return self.content

    @property
    def digest(self) -> str:
        return self.content[:200]

    @property
    def created_at(self) -> float:
        return self.created_time / 1000.0


@dataclass(frozen=True)
class GroupMessageRecord:
    message_id: str
    group_id: str
    sender: str = ""
    self_id: str = ""
    is_outgoing: bool = False
    local_sent: bool = False
    mentions_you: bool = False
    matched_regex_pattern: str = ""
    mentions_everyone: bool = False
    quotes_your_message: bool = False
    mentions_other_people: bool = False
    quotes_other_peoples_message: bool = False
    msg_id2: str = ""
    content: str = ""
    created_time: int = 0
    msg_time: int = 0
    raw_json: str = ""

    # Back-compat aliases for older internal callers/tests.
    @property
    def sender_id(self) -> str:
        return self.sender.removeprefix("user:").removeprefix("bot:")

    @property
    def sender_name(self) -> str:
        return ""

    @property
    def sender_imid(self) -> str:
        return ""

    @property
    def sender_is_bot(self) -> bool:
        return self.sender.startswith("bot:")

    @property
    def is_inbound(self) -> bool:
        return not self.is_outgoing

    @property
    def bot_was_mentioned(self) -> bool:
        return self.mentions_you

    @property
    def msgid2(self) -> str:
        return self.msg_id2

    @property
    def text(self) -> str:
        return self.content

    @property
    def digest(self) -> str:
        return self.content[:200]

    @property
    def created_at(self) -> float:
        return self.created_time / 1000.0


@dataclass(frozen=True)
class ParticipantRecord:
    id: int
    participant_type: str
    agent_id: str = ""
    user_id: str = ""
    imid: str = ""
    name: str = ""
    updated_time: int = 0

    @property
    def key(self) -> str:
        if self.participant_type == "bot":
            return _participant_key("bot", self.agent_id)
        if self.participant_type == "user":
            return _participant_key("user", self.user_id)
        return ""


@dataclass(frozen=True)
class LLMContextState:
    llm_context_key: str
    chat_key: str
    last_llm_visible_message_id: str = ""
    last_llm_visible_created_time: int = 0
    updated_time: int = 0


# Compatibility names kept for callers that still import the old classes.
DMMessageRecord = PrivateMessageRecord


@dataclass
class MessageStore:
    """SQLite-backed per-account message store."""

    account_id: str = "default"
    _db_initialized: bool = field(default=False, init=False, repr=False)
    _db_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def _db_dir(self) -> Path:
        d = _STATE_BASE_DIR.expanduser() / self.account_id
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    @property
    def _db_path(self) -> Path:
        return self._db_dir / "messages.db"

    # ------------------------------------------------------------------
    # Public writes
    # ------------------------------------------------------------------

    def persist_dm(
        self,
        *,
        message_id: str,
        peer: str = "",
        self_id: str = "",
        sender: str = "",
        is_outgoing: bool | None = None,
        local_sent: bool = False,
        quotes_your_message: bool = False,
        msg_id2: str = "",
        content: str = "",
        created_time: int | None = None,
        msg_time: int = 0,
        raw_json: str = "",
        # Back-compat input names:
        dm_user_id: str = "",
        sender_id: str = "",
        sender_name: str = "",
        sender_imid: str = "",
        sender_is_bot: bool = False,
        is_inbound: bool = True,
        text: str = "",
        digest: str = "",
        msgid2: str = "",
    ) -> PrivateMessageRecord | None:
        del sender_name, sender_imid, digest
        if not message_id:
            return None
        if not peer and dm_user_id:
            peer = _participant_key("user", dm_user_id)
        if not sender and sender_id:
            sender = _participant_key("bot" if sender_is_bot else "user", sender_id)
        if is_outgoing is None:
            is_outgoing = not is_inbound
        record = PrivateMessageRecord(
            message_id=str(message_id),
            peer=str(peer or ""),
            self_id=str(self_id or ""),
            sender=str(sender or ""),
            is_outgoing=bool(is_outgoing),
            local_sent=bool(local_sent),
            quotes_your_message=bool(quotes_your_message),
            msg_id2=str(msg_id2 or msgid2 or ""),
            content=str(content or text or ""),
            created_time=int(created_time or _now_ms()),
            msg_time=int(msg_time or 0),
            raw_json=str(raw_json or ""),
        )
        self._upsert_private(record)
        return record

    def persist_group(
        self,
        *,
        message_id: str,
        group_id: str,
        sender: str = "",
        self_id: str = "",
        is_outgoing: bool | None = None,
        local_sent: bool = False,
        mentions_you: bool = False,
        matched_regex_pattern: str = "",
        mentions_everyone: bool = False,
        quotes_your_message: bool = False,
        mentions_other_people: bool = False,
        quotes_other_peoples_message: bool = False,
        msg_id2: str = "",
        content: str = "",
        created_time: int | None = None,
        msg_time: int = 0,
        raw_json: str = "",
        # Back-compat input names:
        sender_id: str = "",
        sender_name: str = "",
        sender_imid: str = "",
        sender_is_bot: bool = False,
        is_inbound: bool = True,
        bot_was_mentioned: bool = False,
        msgid2: str = "",
        text: str = "",
        digest: str = "",
    ) -> GroupMessageRecord | None:
        del sender_name, sender_imid, digest
        if not message_id:
            return None
        if not sender and sender_id:
            sender = _participant_key("bot" if sender_is_bot else "user", sender_id)
        if is_outgoing is None:
            is_outgoing = not is_inbound
        record = GroupMessageRecord(
            message_id=str(message_id),
            group_id=str(group_id or ""),
            sender=str(sender or ""),
            self_id=str(self_id or ""),
            is_outgoing=bool(is_outgoing),
            local_sent=bool(local_sent),
            mentions_you=bool(mentions_you or bot_was_mentioned),
            matched_regex_pattern=str(matched_regex_pattern or ""),
            mentions_everyone=bool(mentions_everyone),
            quotes_your_message=bool(quotes_your_message),
            mentions_other_people=bool(mentions_other_people),
            quotes_other_peoples_message=bool(quotes_other_peoples_message),
            msg_id2=str(msg_id2 or msgid2 or ""),
            content=str(content or text or ""),
            created_time=int(created_time or _now_ms()),
            msg_time=int(msg_time or 0),
            raw_json=str(raw_json or ""),
        )
        self._upsert_group(record)
        return record

    def upsert_participant(
        self,
        *,
        participant_type: str,
        agent_id: str = "",
        user_id: str = "",
        imid: str = "",
        name: str = "",
        updated_time: int | None = None,
    ) -> ParticipantRecord | None:
        ptype = str(participant_type or "").strip()
        if ptype not in {"bot", "user"}:
            return None
        agent_id = str(agent_id or "").strip()
        user_id = str(user_id or "").strip()
        if ptype == "bot" and not agent_id:
            return None
        if ptype == "user" and not user_id:
            return None
        ts = int(updated_time or _now_ms())
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return None
            try:
                conn.execute(
                    """
                    INSERT INTO participants (
                        participant_type, agent_id, user_id, imid, name, updated_time
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(participant_type, agent_id) WHERE participant_type = 'bot'
                    DO UPDATE SET
                        imid = CASE WHEN excluded.imid != '' THEN excluded.imid ELSE participants.imid END,
                        name = CASE WHEN excluded.name != '' THEN excluded.name ELSE participants.name END,
                        updated_time = excluded.updated_time
                    """,
                    (ptype, agent_id, user_id, imid, name, ts),
                ) if ptype == "bot" else conn.execute(
                    """
                    INSERT INTO participants (
                        participant_type, agent_id, user_id, imid, name, updated_time
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(participant_type, user_id) WHERE participant_type = 'user'
                    DO UPDATE SET
                        imid = CASE WHEN excluded.imid != '' THEN excluded.imid ELSE participants.imid END,
                        name = CASE WHEN excluded.name != '' THEN excluded.name ELSE participants.name END,
                        updated_time = excluded.updated_time
                    """,
                    (ptype, agent_id, user_id, imid, name, ts),
                )
                row = self._participant_row(conn, ptype=ptype, agent_id=agent_id, user_id=user_id)
                rec = self._row_to_participant(row) if row else None
                if rec and rec.participant_type == "bot" and rec.imid and rec.agent_id:
                    self._backfill_group_sender_by_imid(conn, rec.imid, rec.key)
                return rec
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] participant upsert failed: %s", exc)
                return None
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def upsert_group_members(self, group_id: str, members: list[GroupMember]) -> None:
        """Persist latest group member identities.

        Human ``name`` from this API is not authoritative, so only ``user_id``
        is updated for humans.  Bot ``agent_id``/``imid``/``name`` are used.
        """
        del group_id  # currently global participant facts, not membership facts.
        for member in members:
            if member.is_bot:
                self.upsert_participant(
                    participant_type="bot",
                    agent_id=str(member.agent_id or member.uid or ""),
                    imid=str(member.imid or ""),
                    name=member.name or "",
                )
            elif member.uid:
                self.upsert_participant(
                    participant_type="user",
                    user_id=str(member.uid),
                )

    # ------------------------------------------------------------------
    # Public reads
    # ------------------------------------------------------------------

    def find_dm(self, message_id: str) -> PrivateMessageRecord | None:
        row = self._query_one(
            f"SELECT {_PRIVATE_COLUMNS} FROM private_messages WHERE message_id = ?",
            (message_id,),
        ) if message_id else None
        return self._row_to_private(row) if row else None

    def find_group(self, message_id: str) -> GroupMessageRecord | None:
        row = self._query_one(
            f"SELECT {_GROUP_COLUMNS} FROM group_messages WHERE message_id = ?",
            (message_id,),
        ) if message_id else None
        return self._row_to_group(row) if row else None

    def find_any(self, message_id: str) -> PrivateMessageRecord | GroupMessageRecord | None:
        return self.find_dm(message_id) or self.find_group(message_id)

    def recent_dm(
        self,
        dm_user_id: str = "",
        *,
        inbound_only: bool = False,
        sent_only: bool = False,
        limit: int = 20,
    ) -> list[PrivateMessageRecord]:
        conditions: list[str] = []
        params: list[Any] = []
        if dm_user_id:
            conditions.append("peer = ?")
            params.append(_participant_key("user", dm_user_id))
        if inbound_only:
            conditions.append("is_outgoing = 0")
        elif sent_only:
            conditions.append("is_outgoing = 1")
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self._query_all(
            f"SELECT {_PRIVATE_COLUMNS} FROM private_messages{where} "
            "ORDER BY created_time DESC LIMIT ?",
            (*params, limit),
        )
        return [self._row_to_private(r) for r in rows]

    def recent_group(
        self,
        group_id: str = "",
        *,
        inbound_only: bool = False,
        sent_only: bool = False,
        bot_mentioned_only: bool = False,
        limit: int = 20,
    ) -> list[GroupMessageRecord]:
        conditions: list[str] = []
        params: list[Any] = []
        if group_id:
            conditions.append("group_id = ?")
            params.append(str(group_id))
        if inbound_only:
            conditions.append("is_outgoing = 0")
        elif sent_only:
            conditions.append("is_outgoing = 1")
        if bot_mentioned_only:
            conditions.append("mentions_you = 1")
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self._query_all(
            f"SELECT {_GROUP_COLUMNS} FROM group_messages{where} "
            "ORDER BY created_time DESC LIMIT ?",
            (*params, limit),
        )
        return [self._row_to_group(r) for r in rows]

    def find_group_sent(self, group_id: str, message_id: str) -> GroupMessageRecord | None:
        row = self._query_one(
            f"SELECT {_GROUP_COLUMNS} FROM group_messages "
            "WHERE message_id = ? AND group_id = ? AND is_outgoing = 1",
            (message_id, group_id),
        ) if message_id else None
        return self._row_to_group(row) if row else None

    def recent_group_sent(self, group_id: str, limit: int = 10) -> list[GroupMessageRecord]:
        return self.recent_group(group_id, sent_only=True, limit=limit)

    def query_dm_range(
        self,
        dm_user_id: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 50,
    ) -> list[PrivateMessageRecord]:
        peer = _participant_key("user", dm_user_id)
        conditions = ["peer = ?"]
        params: list[Any] = [peer]
        if start_ms is not None:
            conditions.append("created_time >= ?")
            params.append(int(start_ms))
        if end_ms is not None:
            conditions.append("created_time < ?")
            params.append(int(end_ms))
        rows = self._query_all(
            f"SELECT {_PRIVATE_COLUMNS} FROM private_messages "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY created_time ASC, message_id ASC LIMIT ?",
            (*params, max(1, int(limit))),
        )
        return [self._row_to_private(r) for r in rows]

    def query_group_range(
        self,
        group_id: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 50,
    ) -> list[GroupMessageRecord]:
        conditions = ["group_id = ?"]
        params: list[Any] = [str(group_id or "")]
        if start_ms is not None:
            conditions.append("created_time >= ?")
            params.append(int(start_ms))
        if end_ms is not None:
            conditions.append("created_time < ?")
            params.append(int(end_ms))
        rows = self._query_all(
            f"SELECT {_GROUP_COLUMNS} FROM group_messages "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY created_time ASC, message_id ASC LIMIT ?",
            (*params, max(1, int(limit))),
        )
        return [self._row_to_group(r) for r in rows]

    def dm_window_around(
        self,
        anchor: PrivateMessageRecord,
        *,
        before_count: int = 0,
        after_count: int = 0,
    ) -> list[PrivateMessageRecord]:
        before = self._private_before(
            anchor.peer,
            anchor.created_time,
            anchor.message_id,
            max(0, int(before_count)),
        )
        after = self._private_after(
            anchor.peer,
            anchor.created_time,
            anchor.message_id,
            max(0, int(after_count)),
        )
        return [*reversed(before), anchor, *after]

    def group_window_around(
        self,
        anchor: GroupMessageRecord,
        *,
        before_count: int = 0,
        after_count: int = 0,
    ) -> list[GroupMessageRecord]:
        before = self._group_before(
            anchor.group_id,
            anchor.created_time,
            anchor.message_id,
            max(0, int(before_count)),
        )
        after = self._group_after(
            anchor.group_id,
            anchor.created_time,
            anchor.message_id,
            max(0, int(after_count)),
        )
        return [*reversed(before), anchor, *after]

    def count_group_between(
        self,
        group_id: str,
        *,
        after_created_time: int = 0,
        after_message_id: str = "",
        before_created_time: int,
        before_message_id: str,
    ) -> int:
        return self._count_between(
            "group_messages",
            "group_id",
            str(group_id or ""),
            after_created_time=after_created_time,
            after_message_id=after_message_id,
            before_created_time=before_created_time,
            before_message_id=before_message_id,
        )

    def count_dm_between(
        self,
        dm_user_id: str,
        *,
        after_created_time: int = 0,
        after_message_id: str = "",
        before_created_time: int,
        before_message_id: str,
    ) -> int:
        return self._count_between(
            "private_messages",
            "peer",
            _participant_key("user", dm_user_id),
            after_created_time=after_created_time,
            after_message_id=after_message_id,
            before_created_time=before_created_time,
            before_message_id=before_message_id,
        )

    def group_between(
        self,
        group_id: str,
        *,
        after_created_time: int = 0,
        after_message_id: str = "",
        before_created_time: int,
        before_message_id: str,
        limit: int | None = None,
    ) -> list[GroupMessageRecord]:
        rows = self._between_rows(
            "group_messages",
            _GROUP_COLUMNS,
            "group_id",
            str(group_id or ""),
            after_created_time=after_created_time,
            after_message_id=after_message_id,
            before_created_time=before_created_time,
            before_message_id=before_message_id,
            limit=limit,
        )
        return [self._row_to_group(r) for r in rows]

    def dm_between(
        self,
        dm_user_id: str,
        *,
        after_created_time: int = 0,
        after_message_id: str = "",
        before_created_time: int,
        before_message_id: str,
        limit: int | None = None,
    ) -> list[PrivateMessageRecord]:
        rows = self._between_rows(
            "private_messages",
            _PRIVATE_COLUMNS,
            "peer",
            _participant_key("user", dm_user_id),
            after_created_time=after_created_time,
            after_message_id=after_message_id,
            before_created_time=before_created_time,
            before_message_id=before_message_id,
            limit=limit,
        )
        return [self._row_to_private(r) for r in rows]

    def get_llm_context_state(self, llm_context_key: str) -> LLMContextState | None:
        row = self._query_one(
            f"SELECT {_LLM_CONTEXT_COLUMNS} FROM llm_context_state "
            "WHERE llm_context_key = ?",
            (str(llm_context_key or ""),),
        ) if llm_context_key else None
        return self._row_to_llm_context(row) if row else None

    def update_llm_context_state(
        self,
        *,
        llm_context_key: str,
        chat_key: str,
        message_id: str,
        created_time: int,
        updated_time: int | None = None,
    ) -> LLMContextState | None:
        if not llm_context_key or not message_id:
            return None
        ts = int(updated_time or _now_ms())
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return None
            try:
                conn.execute(
                    """
                    INSERT INTO llm_context_state (
                        llm_context_key, chat_key, last_llm_visible_message_id,
                        last_llm_visible_created_time, updated_time
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(llm_context_key) DO UPDATE SET
                        chat_key = excluded.chat_key,
                        last_llm_visible_message_id = excluded.last_llm_visible_message_id,
                        last_llm_visible_created_time = excluded.last_llm_visible_created_time,
                        updated_time = excluded.updated_time
                    """,
                    (
                        str(llm_context_key),
                        str(chat_key or ""),
                        str(message_id),
                        int(created_time or 0),
                        ts,
                    ),
                )
                row = conn.execute(
                    f"SELECT {_LLM_CONTEXT_COLUMNS} FROM llm_context_state "
                    "WHERE llm_context_key = ?",
                    (str(llm_context_key),),
                ).fetchone()
                return self._row_to_llm_context(row) if row else None
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] context upsert failed: %s", exc)
                return None
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def find_participant_by_imid(self, imid: str) -> ParticipantRecord | None:
        row = self._query_one(
            f"SELECT {_PARTICIPANT_COLUMNS} FROM participants WHERE imid = ? "
            "ORDER BY updated_time DESC LIMIT 1",
            (str(imid or ""),),
        ) if imid else None
        return self._row_to_participant(row) if row else None

    def find_bot_by_agent_id(self, agent_id: str) -> ParticipantRecord | None:
        row = self._query_one(
            f"SELECT {_PARTICIPANT_COLUMNS} FROM participants "
            "WHERE participant_type = 'bot' AND agent_id = ?",
            (str(agent_id or ""),),
        ) if agent_id else None
        return self._row_to_participant(row) if row else None

    def find_user_by_user_id(self, user_id: str) -> ParticipantRecord | None:
        row = self._query_one(
            f"SELECT {_PARTICIPANT_COLUMNS} FROM participants "
            "WHERE participant_type = 'user' AND user_id = ?",
            (str(user_id or ""),),
        ) if user_id else None
        return self._row_to_participant(row) if row else None

    def remove_dm(self, message_id: str) -> None:
        if message_id:
            self._execute("DELETE FROM private_messages WHERE message_id = ?", (message_id,))

    def remove_group(self, message_id: str) -> None:
        if message_id:
            self._execute("DELETE FROM group_messages WHERE message_id = ?", (message_id,))

    # ------------------------------------------------------------------
    # SQLite internals
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection | None:
        try:
            conn = sqlite3.connect(
                str(self._db_path),
                isolation_level=None,
                timeout=_DB_TIMEOUT_SECONDS,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            logger.warning("[infoflow:message_store] sqlite connect failed: %s", exc)
            return None
        for pragma in (
            "PRAGMA journal_mode=WAL",
            f"PRAGMA busy_timeout={_DB_BUSY_TIMEOUT_MS}",
            "PRAGMA synchronous=NORMAL",
        ):
            with contextlib.suppress(sqlite3.Error):
                conn.execute(pragma)
        if not self._db_initialized:
            self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        try:
            if self._schema_mismatch(conn):
                self._drop_message_tables(conn)
            self._create_schema(conn)
            self._migrate_schema(conn)
            self._db_initialized = True
        except sqlite3.Error as exc:
            logger.warning("[infoflow:message_store] schema init failed: %s", exc)

    def _schema_mismatch(self, conn: sqlite3.Connection) -> bool:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "dm_messages" in tables:
            return True
        current: dict[str, set[str]] = {
            "private_messages": {
                "message_id", "peer", "self", "sender", "is_outgoing",
                "local_sent", "quotes_your_message", "msg_id2", "content",
                "created_time", "msg_time", "raw_json",
            },
            "group_messages": {
                "message_id", "group_id", "sender", "self", "is_outgoing",
                "local_sent", "mentions_you", "matched_regex_pattern",
                "mentions_everyone", "quotes_your_message", "mentions_other_people",
                "quotes_other_peoples_message", "msg_id2", "content",
                "created_time", "msg_time", "raw_json",
            },
            "participants": {
                "id", "participant_type", "agent_id", "user_id", "imid",
                "name", "updated_time",
            },
            "llm_context_state": {
                "llm_context_key", "chat_key", "last_llm_visible_message_id",
                "last_llm_visible_created_time", "updated_time",
            },
        }
        legacy_message_cols = {
            table: cols - {"local_sent"}
            for table, cols in current.items()
            if table in {"private_messages", "group_messages"}
        }
        for table, cols in current.items():
            if table not in tables:
                continue
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if existing == cols:
                continue
            if existing == legacy_message_cols.get(table, set()):
                continue
            if existing != cols:
                return True
        return False

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        for table in ("private_messages", "group_messages"):
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if existing and "local_sent" not in existing:
                conn.execute(
                    f"ALTER TABLE {table} "
                    "ADD COLUMN local_sent INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _drop_message_tables(conn: sqlite3.Connection) -> None:
        for table in (
            "dm_messages",
            "private_messages",
            "group_messages",
            "participants",
            "llm_context_state",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS private_messages (
                message_id TEXT PRIMARY KEY,
                peer TEXT NOT NULL DEFAULT '',
                self TEXT NOT NULL DEFAULT '',
                sender TEXT NOT NULL DEFAULT '',
                is_outgoing INTEGER NOT NULL DEFAULT 0,
                local_sent INTEGER NOT NULL DEFAULT 0,
                quotes_your_message INTEGER NOT NULL DEFAULT 0,
                msg_id2 TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                created_time INTEGER NOT NULL,
                msg_time INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS group_messages (
                message_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL DEFAULT '',
                sender TEXT NOT NULL DEFAULT '',
                self TEXT NOT NULL DEFAULT '',
                is_outgoing INTEGER NOT NULL DEFAULT 0,
                local_sent INTEGER NOT NULL DEFAULT 0,
                mentions_you INTEGER NOT NULL DEFAULT 0,
                matched_regex_pattern TEXT NOT NULL DEFAULT '',
                mentions_everyone INTEGER NOT NULL DEFAULT 0,
                quotes_your_message INTEGER NOT NULL DEFAULT 0,
                mentions_other_people INTEGER NOT NULL DEFAULT 0,
                quotes_other_peoples_message INTEGER NOT NULL DEFAULT 0,
                msg_id2 TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                created_time INTEGER NOT NULL,
                msg_time INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_type TEXT NOT NULL CHECK(participant_type IN ('bot', 'user')),
                agent_id TEXT,
                user_id TEXT,
                imid TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                updated_time INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_context_state (
                llm_context_key TEXT PRIMARY KEY,
                chat_key TEXT NOT NULL DEFAULT '',
                last_llm_visible_message_id TEXT NOT NULL DEFAULT '',
                last_llm_visible_created_time INTEGER NOT NULL DEFAULT 0,
                updated_time INTEGER NOT NULL
            )
            """
        )
        conn.execute("DROP INDEX IF EXISTS uniq_participant_bot")
        conn.execute("DROP INDEX IF EXISTS uniq_participant_user")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_participant_bot "
            "ON participants(participant_type, agent_id) "
            "WHERE participant_type='bot'"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_participant_user "
            "ON participants(participant_type, user_id) "
            "WHERE participant_type='user'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_private_peer_time "
            "ON private_messages(peer, created_time DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_private_sender_time "
            "ON private_messages(sender, created_time DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_group_time "
            "ON group_messages(group_id, created_time DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_sender_time "
            "ON group_messages(sender, created_time DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_context_chat_time "
            "ON llm_context_state(chat_key, updated_time DESC)"
        )
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _upsert_private(self, rec: PrivateMessageRecord) -> None:
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return
            try:
                conn.execute(
                    """
                    INSERT INTO private_messages (
                        message_id, peer, self, sender, is_outgoing,
                        local_sent, quotes_your_message, msg_id2, content,
                        created_time, msg_time, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        peer = CASE WHEN excluded.peer != '' THEN excluded.peer ELSE private_messages.peer END,
                        self = CASE WHEN excluded.self != '' THEN excluded.self ELSE private_messages.self END,
                        sender = CASE WHEN excluded.sender != '' THEN excluded.sender ELSE private_messages.sender END,
                        is_outgoing = CASE
                            WHEN private_messages.is_outgoing = 1 OR excluded.is_outgoing = 1 THEN 1
                            ELSE 0
                        END,
                        local_sent = CASE
                            WHEN private_messages.local_sent = 1 OR excluded.local_sent = 1 THEN 1
                            ELSE 0
                        END,
                        quotes_your_message = CASE
                            WHEN private_messages.quotes_your_message = 1 OR excluded.quotes_your_message = 1 THEN 1
                            ELSE 0
                        END,
                        msg_id2 = CASE WHEN excluded.msg_id2 != '' THEN excluded.msg_id2 ELSE private_messages.msg_id2 END,
                        content = CASE
                            WHEN private_messages.raw_json != '' AND excluded.raw_json = ''
                            THEN private_messages.content
                            WHEN excluded.content != '' THEN excluded.content
                            ELSE private_messages.content
                        END,
                        msg_time = CASE WHEN excluded.msg_time != 0 THEN excluded.msg_time ELSE private_messages.msg_time END,
                        raw_json = CASE WHEN excluded.raw_json != '' THEN excluded.raw_json ELSE private_messages.raw_json END
                    """,
                    (
                        rec.message_id, rec.peer, rec.self_id, rec.sender,
                        int(rec.is_outgoing), int(rec.local_sent),
                        int(rec.quotes_your_message), rec.msg_id2,
                        rec.content, rec.created_time, rec.msg_time, rec.raw_json,
                    ),
                )
                self._auto_cleanup_table(conn, "private_messages")
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] private upsert failed: %s", exc)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def _upsert_group(self, rec: GroupMessageRecord) -> None:
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return
            try:
                conn.execute(
                    """
                    INSERT INTO group_messages (
                        message_id, group_id, sender, self, is_outgoing,
                        local_sent, mentions_you, matched_regex_pattern,
                        mentions_everyone, quotes_your_message,
                        mentions_other_people, quotes_other_peoples_message,
                        msg_id2, content, created_time, msg_time, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        group_id = CASE WHEN excluded.group_id != '' THEN excluded.group_id ELSE group_messages.group_id END,
                        sender = CASE WHEN excluded.sender != '' THEN excluded.sender ELSE group_messages.sender END,
                        self = CASE WHEN excluded.self != '' THEN excluded.self ELSE group_messages.self END,
                        is_outgoing = CASE
                            WHEN group_messages.is_outgoing = 1 OR excluded.is_outgoing = 1 THEN 1
                            ELSE 0
                        END,
                        local_sent = CASE
                            WHEN group_messages.local_sent = 1 OR excluded.local_sent = 1 THEN 1
                            ELSE 0
                        END,
                        mentions_you = CASE
                            WHEN group_messages.mentions_you = 1 OR excluded.mentions_you = 1 THEN 1
                            ELSE 0
                        END,
                        matched_regex_pattern = CASE
                            WHEN excluded.matched_regex_pattern != '' THEN excluded.matched_regex_pattern
                            ELSE group_messages.matched_regex_pattern
                        END,
                        mentions_everyone = CASE
                            WHEN group_messages.mentions_everyone = 1 OR excluded.mentions_everyone = 1 THEN 1
                            ELSE 0
                        END,
                        quotes_your_message = CASE
                            WHEN group_messages.quotes_your_message = 1 OR excluded.quotes_your_message = 1 THEN 1
                            ELSE 0
                        END,
                        mentions_other_people = CASE
                            WHEN group_messages.mentions_other_people = 1 OR excluded.mentions_other_people = 1 THEN 1
                            ELSE 0
                        END,
                        quotes_other_peoples_message = CASE
                            WHEN group_messages.quotes_other_peoples_message = 1 OR excluded.quotes_other_peoples_message = 1 THEN 1
                            ELSE 0
                        END,
                        msg_id2 = CASE WHEN excluded.msg_id2 != '' THEN excluded.msg_id2 ELSE group_messages.msg_id2 END,
                        content = CASE
                            WHEN group_messages.raw_json != '' AND excluded.raw_json = ''
                            THEN group_messages.content
                            WHEN excluded.content != '' THEN excluded.content
                            ELSE group_messages.content
                        END,
                        msg_time = CASE WHEN excluded.msg_time != 0 THEN excluded.msg_time ELSE group_messages.msg_time END,
                        raw_json = CASE WHEN excluded.raw_json != '' THEN excluded.raw_json ELSE group_messages.raw_json END
                    """,
                    (
                        rec.message_id, rec.group_id, rec.sender, rec.self_id,
                        int(rec.is_outgoing), int(rec.local_sent),
                        int(rec.mentions_you), rec.matched_regex_pattern,
                        int(rec.mentions_everyone), int(rec.quotes_your_message),
                        int(rec.mentions_other_people),
                        int(rec.quotes_other_peoples_message), rec.msg_id2,
                        rec.content, rec.created_time, rec.msg_time, rec.raw_json,
                    ),
                )
                self._auto_cleanup_table(conn, "group_messages")
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] group upsert failed: %s", exc)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def _query_one(self, sql: str, params: tuple | list = ()) -> tuple | None:
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return None
            try:
                return conn.execute(sql, tuple(params)).fetchone()
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] query failed: %s", exc)
                return None
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def _query_all(self, sql: str, params: tuple | list = ()) -> list[tuple]:
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return []
            try:
                return conn.execute(sql, tuple(params)).fetchall()
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] list failed: %s", exc)
                return []
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def _execute(self, sql: str, params: tuple | list = ()) -> None:
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return
            try:
                conn.execute(sql, tuple(params))
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] execute failed: %s", exc)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def _private_before(
        self,
        peer: str,
        created_time: int,
        message_id: str,
        limit: int,
    ) -> list[PrivateMessageRecord]:
        if limit <= 0:
            return []
        rows = self._query_all(
            f"SELECT {_PRIVATE_COLUMNS} FROM private_messages "
            "WHERE peer = ? AND "
            "(created_time < ? OR (created_time = ? AND message_id < ?)) "
            "ORDER BY created_time DESC, message_id DESC LIMIT ?",
            (peer, int(created_time), int(created_time), str(message_id), limit),
        )
        return [self._row_to_private(r) for r in rows]

    def _private_after(
        self,
        peer: str,
        created_time: int,
        message_id: str,
        limit: int,
    ) -> list[PrivateMessageRecord]:
        if limit <= 0:
            return []
        rows = self._query_all(
            f"SELECT {_PRIVATE_COLUMNS} FROM private_messages "
            "WHERE peer = ? AND "
            "(created_time > ? OR (created_time = ? AND message_id > ?)) "
            "ORDER BY created_time ASC, message_id ASC LIMIT ?",
            (peer, int(created_time), int(created_time), str(message_id), limit),
        )
        return [self._row_to_private(r) for r in rows]

    def _group_before(
        self,
        group_id: str,
        created_time: int,
        message_id: str,
        limit: int,
    ) -> list[GroupMessageRecord]:
        if limit <= 0:
            return []
        rows = self._query_all(
            f"SELECT {_GROUP_COLUMNS} FROM group_messages "
            "WHERE group_id = ? AND "
            "(created_time < ? OR (created_time = ? AND message_id < ?)) "
            "ORDER BY created_time DESC, message_id DESC LIMIT ?",
            (str(group_id), int(created_time), int(created_time), str(message_id), limit),
        )
        return [self._row_to_group(r) for r in rows]

    def _group_after(
        self,
        group_id: str,
        created_time: int,
        message_id: str,
        limit: int,
    ) -> list[GroupMessageRecord]:
        if limit <= 0:
            return []
        rows = self._query_all(
            f"SELECT {_GROUP_COLUMNS} FROM group_messages "
            "WHERE group_id = ? AND "
            "(created_time > ? OR (created_time = ? AND message_id > ?)) "
            "ORDER BY created_time ASC, message_id ASC LIMIT ?",
            (str(group_id), int(created_time), int(created_time), str(message_id), limit),
        )
        return [self._row_to_group(r) for r in rows]

    def _count_between(
        self,
        table: str,
        partition_column: str,
        partition_value: str,
        *,
        after_created_time: int = 0,
        after_message_id: str = "",
        before_created_time: int,
        before_message_id: str,
    ) -> int:
        if table not in {"private_messages", "group_messages"}:
            return 0
        if partition_column not in {"peer", "group_id"}:
            return 0
        row = self._query_one(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE {partition_column} = ? AND "
            "(created_time > ? OR (created_time = ? AND message_id > ?)) AND "
            "(created_time < ? OR (created_time = ? AND message_id < ?))",
            (
                partition_value,
                int(after_created_time or 0),
                int(after_created_time or 0),
                str(after_message_id or ""),
                int(before_created_time or 0),
                int(before_created_time or 0),
                str(before_message_id or ""),
            ),
        )
        return int(row[0] or 0) if row else 0

    def _between_rows(
        self,
        table: str,
        columns: str,
        partition_column: str,
        partition_value: str,
        *,
        after_created_time: int = 0,
        after_message_id: str = "",
        before_created_time: int,
        before_message_id: str,
        limit: int | None = None,
    ) -> list[tuple]:
        if table not in {"private_messages", "group_messages"}:
            return []
        if partition_column not in {"peer", "group_id"}:
            return []
        params: list[Any] = [
            partition_value,
            int(after_created_time or 0),
            int(after_created_time or 0),
            str(after_message_id or ""),
            int(before_created_time or 0),
            int(before_created_time or 0),
            str(before_message_id or ""),
        ]
        sql = (
            f"SELECT {columns} FROM {table} "
            f"WHERE {partition_column} = ? AND "
            "(created_time > ? OR (created_time = ? AND message_id > ?)) AND "
            "(created_time < ? OR (created_time = ? AND message_id < ?)) "
            "ORDER BY created_time ASC, message_id ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        return self._query_all(sql, params)

    @staticmethod
    def _row_to_private(row: tuple) -> PrivateMessageRecord:
        return PrivateMessageRecord(
            message_id=str(row[0] or ""),
            peer=str(row[1] or ""),
            self_id=str(row[2] or ""),
            sender=str(row[3] or ""),
            is_outgoing=bool(row[4]),
            local_sent=bool(row[5]),
            quotes_your_message=bool(row[6]),
            msg_id2=str(row[7] or ""),
            content=str(row[8] or ""),
            created_time=int(row[9] or 0),
            msg_time=int(row[10] or 0),
            raw_json=str(row[11] or ""),
        )

    @staticmethod
    def _row_to_group(row: tuple) -> GroupMessageRecord:
        return GroupMessageRecord(
            message_id=str(row[0] or ""),
            group_id=str(row[1] or ""),
            sender=str(row[2] or ""),
            self_id=str(row[3] or ""),
            is_outgoing=bool(row[4]),
            local_sent=bool(row[5]),
            mentions_you=bool(row[6]),
            matched_regex_pattern=str(row[7] or ""),
            mentions_everyone=bool(row[8]),
            quotes_your_message=bool(row[9]),
            mentions_other_people=bool(row[10]),
            quotes_other_peoples_message=bool(row[11]),
            msg_id2=str(row[12] or ""),
            content=str(row[13] or ""),
            created_time=int(row[14] or 0),
            msg_time=int(row[15] or 0),
            raw_json=str(row[16] or ""),
        )

    @staticmethod
    def _row_to_participant(row: tuple) -> ParticipantRecord:
        return ParticipantRecord(
            id=int(row[0] or 0),
            participant_type=str(row[1] or ""),
            agent_id=str(row[2] or ""),
            user_id=str(row[3] or ""),
            imid=str(row[4] or ""),
            name=str(row[5] or ""),
            updated_time=int(row[6] or 0),
        )

    @staticmethod
    def _row_to_llm_context(row: tuple) -> LLMContextState:
        return LLMContextState(
            llm_context_key=str(row[0] or ""),
            chat_key=str(row[1] or ""),
            last_llm_visible_message_id=str(row[2] or ""),
            last_llm_visible_created_time=int(row[3] or 0),
            updated_time=int(row[4] or 0),
        )

    @staticmethod
    def _participant_row(
        conn: sqlite3.Connection,
        *,
        ptype: str,
        agent_id: str,
        user_id: str,
    ) -> tuple | None:
        if ptype == "bot":
            return conn.execute(
                f"SELECT {_PARTICIPANT_COLUMNS} FROM participants "
                "WHERE participant_type = 'bot' AND agent_id = ?",
                (agent_id,),
            ).fetchone()
        return conn.execute(
            f"SELECT {_PARTICIPANT_COLUMNS} FROM participants "
            "WHERE participant_type = 'user' AND user_id = ?",
            (user_id,),
        ).fetchone()

    @staticmethod
    def _backfill_group_sender_by_imid(
        conn: sqlite3.Connection,
        imid: str,
        sender_key: str,
    ) -> None:
        if not imid or not sender_key:
            return
        with contextlib.suppress(sqlite3.Error):
            conn.execute(
                """
                UPDATE group_messages
                SET sender = ?
                WHERE sender = ''
                  AND raw_json != ''
                  AND (
                    CAST(json_extract(raw_json, '$.fromid') AS TEXT) = ?
                    OR CAST(json_extract(raw_json, '$.message.header.fromid') AS TEXT) = ?
                  )
                """,
                (sender_key, imid, imid),
            )

    _ALLOWED_TABLES = frozenset({"private_messages", "group_messages"})

    def _auto_cleanup_table(self, conn: sqlite3.Connection, table: str) -> None:
        if table not in self._ALLOWED_TABLES:
            return
        cutoff = _now_ms() - int(_RETENTION_SECONDS * 1000)
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"DELETE FROM {table} WHERE created_time < ?", (cutoff,))
        with contextlib.suppress(sqlite3.Error):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count > _ROW_LIMIT:
                delete_n = count - _ROW_LIMIT
                conn.execute(
                    f"DELETE FROM {table} WHERE message_id IN "
                    f"(SELECT message_id FROM {table} ORDER BY created_time ASC LIMIT ?)",
                    (delete_n,),
                )


__all__ = [
    "DMMessageRecord",
    "GroupMessageRecord",
    "LLMContextState",
    "MessageStore",
    "ParticipantRecord",
    "PrivateMessageRecord",
]
