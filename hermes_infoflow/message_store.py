"""统一消息存储 —— 按账户隔离的收发消息持久化层。

目录结构（每个账户一个子文件夹）::

    ~/.hermes/state/infoflow/
      {account_id}/
        messages.db          ← SQLite 数据库
          dm_messages         ← 私聊消息表
          group_messages      ← 群聊消息表

每张表各自独立，不需要 ``chat_type`` 字段来区分会话类型。

自动清理策略：
* 保留最近 7 天的记录。
* 每张表总行数超过 5 000 行时，按时间戳从旧到新裁剪。

PRAGMA 设置与 ``sent_store.py`` 保持一致：WAL 模式、
busy_timeout=5000 ms、synchronous=NORMAL。
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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

_RETENTION_SECONDS = 7 * 24 * 60 * 60   # 7 天
_ROW_LIMIT = 5000                        # 每张表的行数上限
_DB_TIMEOUT_SECONDS = 30.0               # 连接超时（与 sent_store 一致）
_DB_BUSY_TIMEOUT_MS = 5_000              # SQLite busy 等待（与 sent_store 一致）
_STATE_BASE_DIR = Path("~/.hermes/state/infoflow")


# ---------------------------------------------------------------------------
# 数据记录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DMMessageRecord:
    """一条私聊消息的持久化记录。

    Attributes:
        message_id:       如流消息 ID（统一 string，避免精度问题）。
        dm_user_id:       对方 uuapName（如 ``"chengbo05"``）。
        sender_id:        实际发送者的 ID（人类 uuapName 或机器人 agentId）。
        sender_name:      发送者昵称（如 ``"成博"``、``"chengbo5.2"``）。
        sender_imid:      发送者的 Infoflow imid（如 ``"4100110898"``）。
        sender_is_bot:    **发送者**是否为机器人。
        is_inbound:       True = 收到的消息；False = bot 发出的消息。
        text:             消息文本内容。
        digest:           短文本摘要（bot 发出的消息用于旧接口兼容）。
        created_at:       时间戳（time.time()，秒）。
        raw_json:         解码后的原始 webhook JSON（仅入站消息有值）。
    """

    message_id: str
    dm_user_id: str
    sender_id: str = ""
    sender_name: str = ""
    sender_imid: str = ""
    sender_is_bot: bool = False
    is_inbound: bool = True
    text: str = ""
    digest: str = ""
    created_at: float = 0.0
    raw_json: str = ""


@dataclass(frozen=True)
class GroupMessageRecord:
    """一条群聊消息的持久化记录。

    Attributes:
        message_id:         如流消息 ID（统一 string）。
        group_id:           群 ID（如 ``"4507088"``）。
        sender_id:          实际发送者的 ID。
        sender_name:        发送者昵称。
        sender_imid:        发送者的 Infoflow imid。
        sender_is_bot:      **发送者**是否为机器人。
        is_inbound:         True = 收到的消息；False = bot 发出的消息。
        bot_was_mentioned:  **bot 自己**是否在此消息中被 @mentioned。
                           （仅入站消息有效；bot 发出的消息固定为 False。）
        text:               消息文本内容。
        digest:             短文本摘要。
        created_at:         时间戳（秒）。
        raw_json:           原始 webhook JSON。
    """

    message_id: str
    group_id: str
    sender_id: str = ""
    sender_name: str = ""
    sender_imid: str = ""
    sender_is_bot: bool = False
    is_inbound: bool = True
    bot_was_mentioned: bool = False
    text: str = ""
    digest: str = ""
    created_at: float = 0.0
    raw_json: str = ""


# ---------------------------------------------------------------------------
# MessageStore
# ---------------------------------------------------------------------------

@dataclass
class MessageStore:
    """按账户隔离的统一消息存储。

    ``account_id``（如 ``"6533"``）决定数据库文件路径：
    ``~/.hermes/state/infoflow/{account_id}/messages.db``。

    线程安全：通过 ``threading.Lock`` 保护所有数据库操作。
    """

    account_id: str = "default"
    _dm_db_initialized: bool = field(default=False, init=False, repr=False)
    _group_db_initialized: bool = field(default=False, init=False, repr=False)
    _db_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ------------------------------------------------------------------
    # DB 路径
    # ------------------------------------------------------------------

    @property
    def _db_dir(self) -> Path:
        """账户专属目录，自动创建。"""
        d = _STATE_BASE_DIR.expanduser() / self.account_id
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    @property
    def _db_path(self) -> Path:
        return self._db_dir / "messages.db"

    # ------------------------------------------------------------------
    # 私聊：写入 / 查询
    # ------------------------------------------------------------------

    def persist_dm(
        self,
        *,
        message_id: str,
        dm_user_id: str,
        sender_id: str = "",
        sender_name: str = "",
        sender_imid: str = "",
        sender_is_bot: bool = False,
        is_inbound: bool = True,
        text: str = "",
        digest: str = "",
        raw_json: str = "",
    ) -> DMMessageRecord | None:
        """持久化一条私聊消息（收到的或发出的）。"""
        if not message_id:
            return None
        record = DMMessageRecord(
            message_id=message_id,
            dm_user_id=dm_user_id,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_imid=sender_imid,
            sender_is_bot=sender_is_bot,
            is_inbound=is_inbound,
            text=text[:5000],
            digest=digest[:200],
            created_at=time.time(),
            raw_json=raw_json[:50000],
        )
        self._upsert_dm(record)
        return record

    def find_dm(self, message_id: str) -> DMMessageRecord | None:
        """按 message_id 查找私聊消息。"""
        if not message_id:
            return None
        row = self._query_one_dm(
            "SELECT * FROM dm_messages WHERE message_id = ?", (message_id,),
        )
        return self._row_to_dm(row) if row else None

    def recent_dm(
        self,
        dm_user_id: str = "",
        *,
        inbound_only: bool = False,
        sent_only: bool = False,
        limit: int = 20,
    ) -> list[DMMessageRecord]:
        """查询最近的私聊消息。"""
        return self._list_dm(dm_user_id, inbound_only=inbound_only,
                             sent_only=sent_only, limit=limit)

    def remove_dm(self, message_id: str) -> None:
        """删除一条私聊消息。"""
        if not message_id:
            return
        self._execute_dm("DELETE FROM dm_messages WHERE message_id = ?",
                         (message_id,))

    # ------------------------------------------------------------------
    # 群聊：写入 / 查询
    # ------------------------------------------------------------------

    def persist_group(
        self,
        *,
        message_id: str,
        group_id: str,
        sender_id: str = "",
        sender_name: str = "",
        sender_imid: str = "",
        sender_is_bot: bool = False,
        is_inbound: bool = True,
        bot_was_mentioned: bool = False,
        text: str = "",
        digest: str = "",
        raw_json: str = "",
    ) -> GroupMessageRecord | None:
        """持久化一条群聊消息（收到的或发出的）。"""
        if not message_id:
            return None
        record = GroupMessageRecord(
            message_id=message_id,
            group_id=group_id,
            sender_id=sender_id,
            sender_name=sender_name,
            sender_imid=sender_imid,
            sender_is_bot=sender_is_bot,
            is_inbound=is_inbound,
            bot_was_mentioned=bot_was_mentioned,
            text=text[:5000],
            digest=digest[:200],
            created_at=time.time(),
            raw_json=raw_json[:50000],
        )
        self._upsert_group(record)
        return record

    def find_group(self, message_id: str) -> GroupMessageRecord | None:
        """按 message_id 查找群聊消息。"""
        if not message_id:
            return None
        row = self._query_one_group(
            "SELECT * FROM group_messages WHERE message_id = ?", (message_id,),
        )
        return self._row_to_group(row) if row else None

    def recent_group(
        self,
        group_id: str = "",
        *,
        inbound_only: bool = False,
        sent_only: bool = False,
        bot_mentioned_only: bool = False,
        limit: int = 20,
    ) -> list[GroupMessageRecord]:
        """查询最近的群聊消息。"""
        return self._list_group(
            group_id, inbound_only=inbound_only, sent_only=sent_only,
            bot_mentioned_only=bot_mentioned_only, limit=limit,
        )

    def remove_group(self, message_id: str) -> None:
        """删除一条群聊消息。"""
        if not message_id:
            return
        self._execute_group("DELETE FROM group_messages WHERE message_id = ?",
                            (message_id,))

    def find_group_sent(self, group_id: str, message_id: str) -> GroupMessageRecord | None:
        """在指定群中查找 bot 发出的消息（兼容旧 SentMessageStore.find 接口）。"""
        if not message_id:
            return None
        row = self._query_one_group(
            "SELECT * FROM group_messages "
            "WHERE message_id = ? AND group_id = ? AND is_inbound = 0",
            (message_id, group_id),
        )
        return self._row_to_group(row) if row else None

    def recent_group_sent(self, group_id: str, limit: int = 10) -> list[GroupMessageRecord]:
        """查询指定群中 bot 最近发出的消息（兼容旧 SentMessageStore.recent 接口）。"""
        return self._list_group(group_id, sent_only=True, limit=limit)

    # ------------------------------------------------------------------
    # 通用查找（跨表）
    # ------------------------------------------------------------------

    def find_any(self, message_id: str) -> DMMessageRecord | GroupMessageRecord | None:
        """在两张表中查找消息（先查私聊再查群聊）。"""
        dm = self.find_dm(message_id)
        if dm is not None:
            return dm
        return self.find_group(message_id)

    # ------------------------------------------------------------------
    # SQLite 连接管理（内部）
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection | None:
        """获取数据库连接并设置 PRAGMA。"""
        try:
            db_file = self._db_path
            conn = sqlite3.connect(
                str(db_file),
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

        # 确保两张表都存在（幂等）。
        if not self._dm_db_initialized or not self._group_db_initialized:
            self._ensure_tables(conn)
        return conn

    def _ensure_tables(self, conn: sqlite3.Connection) -> None:
        """创建表和索引（幂等）。"""
        try:
            if not self._dm_db_initialized:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS dm_messages (
                        message_id      TEXT PRIMARY KEY,
                        dm_user_id      TEXT NOT NULL DEFAULT '',
                        sender_id       TEXT NOT NULL DEFAULT '',
                        sender_name     TEXT NOT NULL DEFAULT '',
                        sender_imid     TEXT NOT NULL DEFAULT '',
                        sender_is_bot   INTEGER NOT NULL DEFAULT 0,
                        is_inbound      INTEGER NOT NULL DEFAULT 1,
                        text            TEXT NOT NULL DEFAULT '',
                        digest          TEXT NOT NULL DEFAULT '',
                        created_at      REAL NOT NULL,
                        raw_json        TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dm_user_time "
                    "ON dm_messages(dm_user_id, created_at DESC)"
                )
                self._dm_db_initialized = True

            if not self._group_db_initialized:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS group_messages (
                        message_id         TEXT PRIMARY KEY,
                        group_id           TEXT NOT NULL DEFAULT '',
                        sender_id          TEXT NOT NULL DEFAULT '',
                        sender_name        TEXT NOT NULL DEFAULT '',
                        sender_imid        TEXT NOT NULL DEFAULT '',
                        sender_is_bot      INTEGER NOT NULL DEFAULT 0,
                        is_inbound         INTEGER NOT NULL DEFAULT 1,
                        bot_was_mentioned  INTEGER NOT NULL DEFAULT 0,
                        text               TEXT NOT NULL DEFAULT '',
                        digest             TEXT NOT NULL DEFAULT '',
                        created_at         REAL NOT NULL,
                        raw_json           TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_group_time "
                    "ON group_messages(group_id, created_at DESC)"
                )
                self._group_db_initialized = True
        except sqlite3.Error as exc:
            logger.debug("[infoflow:message_store] schema init deferred: %s", exc)

    # ------------------------------------------------------------------
    # 私聊内部方法
    # ------------------------------------------------------------------

    def _upsert_dm(self, rec: DMMessageRecord) -> None:
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return
            try:
                conn.execute(
                    """
                    INSERT INTO dm_messages (
                        message_id, dm_user_id, sender_id, sender_name, sender_imid,
                        sender_is_bot, is_inbound, text, digest, created_at, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        dm_user_id      = excluded.dm_user_id,
                        sender_id       = excluded.sender_id,
                        sender_name     = excluded.sender_name,
                        sender_imid     = excluded.sender_imid,
                        sender_is_bot   = excluded.sender_is_bot,
                        is_inbound      = excluded.is_inbound,
                        text            = excluded.text,
                        digest          = excluded.digest,
                        created_at      = excluded.created_at,
                        raw_json        = excluded.raw_json
                    """,
                    (
                        rec.message_id, rec.dm_user_id, rec.sender_id,
                        rec.sender_name, rec.sender_imid,
                        int(rec.sender_is_bot), int(rec.is_inbound),
                        rec.text, rec.digest, rec.created_at, rec.raw_json,
                    ),
                )
                self._auto_cleanup_table(conn, "dm_messages")
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] dm upsert failed: %s", exc)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def _query_one_dm(self, sql: str, params: tuple | list = ()) -> tuple | None:
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return None
            try:
                return conn.execute(sql, tuple(params)).fetchone()
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] dm query failed: %s", exc)
                return None
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def _execute_dm(self, sql: str, params: tuple | list = ()) -> None:
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return
            try:
                conn.execute(sql, tuple(params))
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] dm execute failed: %s", exc)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def _list_dm(
        self,
        dm_user_id: str = "",
        *,
        inbound_only: bool = False,
        sent_only: bool = False,
        limit: int = 20,
    ) -> list[DMMessageRecord]:
        conditions: list[str] = []
        params: list[Any] = []
        if dm_user_id:
            conditions.append("dm_user_id = ?")
            params.append(dm_user_id)
        if inbound_only:
            conditions.append("is_inbound = 1")
        elif sent_only:
            conditions.append("is_inbound = 0")
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM dm_messages{where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return []
            try:
                rows = conn.execute(sql, tuple(params)).fetchall()
                return [self._row_to_dm(r) for r in rows]
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] dm list failed: %s", exc)
                return []
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    @staticmethod
    def _row_to_dm(row: tuple) -> DMMessageRecord:
        """数据库行 → DMMessageRecord。

        列顺序与 CREATE TABLE dm_messages 一致：
        message_id, dm_user_id, sender_id, sender_name, sender_imid,
        sender_is_bot, is_inbound, text, digest, created_at, raw_json
        """
        return DMMessageRecord(
            message_id=str(row[0] or ""),
            dm_user_id=str(row[1] or ""),
            sender_id=str(row[2] or ""),
            sender_name=str(row[3] or ""),
            sender_imid=str(row[4] or ""),
            sender_is_bot=bool(row[5]),
            is_inbound=bool(row[6]),
            text=str(row[7] or ""),
            digest=str(row[8] or ""),
            created_at=float(row[9] or 0),
            raw_json=str(row[10] or ""),
        )

    # ------------------------------------------------------------------
    # 群聊内部方法
    # ------------------------------------------------------------------

    def _upsert_group(self, rec: GroupMessageRecord) -> None:
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return
            try:
                conn.execute(
                    """
                    INSERT INTO group_messages (
                        message_id, group_id, sender_id, sender_name, sender_imid,
                        sender_is_bot, is_inbound, bot_was_mentioned,
                        text, digest, created_at, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        group_id          = excluded.group_id,
                        sender_id         = excluded.sender_id,
                        sender_name       = excluded.sender_name,
                        sender_imid       = excluded.sender_imid,
                        sender_is_bot     = excluded.sender_is_bot,
                        is_inbound        = excluded.is_inbound,
                        bot_was_mentioned = excluded.bot_was_mentioned,
                        text              = excluded.text,
                        digest            = excluded.digest,
                        created_at        = excluded.created_at,
                        raw_json          = excluded.raw_json
                    """,
                    (
                        rec.message_id, rec.group_id, rec.sender_id,
                        rec.sender_name, rec.sender_imid,
                        int(rec.sender_is_bot), int(rec.is_inbound),
                        int(rec.bot_was_mentioned),
                        rec.text, rec.digest, rec.created_at, rec.raw_json,
                    ),
                )
                self._auto_cleanup_table(conn, "group_messages")
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] group upsert failed: %s", exc)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def _query_one_group(self, sql: str, params: tuple | list = ()) -> tuple | None:
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return None
            try:
                return conn.execute(sql, tuple(params)).fetchone()
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] group query failed: %s", exc)
                return None
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def _execute_group(self, sql: str, params: tuple | list = ()) -> None:
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return
            try:
                conn.execute(sql, tuple(params))
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] group execute failed: %s", exc)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    def _list_group(
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
            params.append(group_id)
        if inbound_only:
            conditions.append("is_inbound = 1")
        elif sent_only:
            conditions.append("is_inbound = 0")
        if bot_mentioned_only:
            conditions.append("bot_was_mentioned = 1")
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM group_messages{where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._db_lock:
            conn = self._connect()
            if conn is None:
                return []
            try:
                rows = conn.execute(sql, tuple(params)).fetchall()
                return [self._row_to_group(r) for r in rows]
            except sqlite3.Error as exc:
                logger.warning("[infoflow:message_store] group list failed: %s", exc)
                return []
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    @staticmethod
    def _row_to_group(row: tuple) -> GroupMessageRecord:
        """数据库行 → GroupMessageRecord。

        列顺序与 CREATE TABLE group_messages 一致：
        message_id, group_id, sender_id, sender_name, sender_imid,
        sender_is_bot, is_inbound, bot_was_mentioned,
        text, digest, created_at, raw_json
        """
        return GroupMessageRecord(
            message_id=str(row[0] or ""),
            group_id=str(row[1] or ""),
            sender_id=str(row[2] or ""),
            sender_name=str(row[3] or ""),
            sender_imid=str(row[4] or ""),
            sender_is_bot=bool(row[5]),
            is_inbound=bool(row[6]),
            bot_was_mentioned=bool(row[7]),
            text=str(row[8] or ""),
            digest=str(row[9] or ""),
            created_at=float(row[10] or 0),
            raw_json=str(row[11] or ""),
        )

    # ------------------------------------------------------------------
    # 自动清理
    # ------------------------------------------------------------------

    _ALLOWED_TABLES = frozenset({"dm_messages", "group_messages"})

    def _auto_cleanup_table(self, conn: sqlite3.Connection, table: str) -> None:
        """按时间 + 行数自动清理过期数据。"""
        if table not in self._ALLOWED_TABLES:
            return
        try:
            cutoff = time.time() - _RETENTION_SECONDS
            conn.execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
        except sqlite3.Error:
            pass
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count > _ROW_LIMIT:
                delete_n = count - _ROW_LIMIT
                conn.execute(
                    f"DELETE FROM {table} WHERE message_id IN "
                    f"(SELECT message_id FROM {table} ORDER BY created_at ASC LIMIT ?)",
                    (delete_n,),
                )
        except sqlite3.Error:
            pass


__all__ = [
    "DMMessageRecord",
    "GroupMessageRecord",
    "MessageStore",
]
