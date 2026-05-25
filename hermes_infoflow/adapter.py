"""``InfoflowAdapter`` — the Hermes gateway platform adapter.

**Architecture**::

    gateway (platform-agnostic)
      ↕  BasePlatformAdapter interface
    adapter.py  (format conversion: Hermes ↔ bot-layer types)
      ↕  BotProcessor / IncomingMessage / SentResult / RecallResult
    bot.py  (business logic: policy, dedup, stores, dispatch)
      ↕  ServerAPI / IncomingMessage / SentResult / RecallResult
    serverapi.py  (Infoflow API adaptation: unified fields ↔ messy wire format)
      ↕↕
    webhook.py    websocket.py  (channel transport, only webhook implemented)

**Message lifecycle tracing** — every inbound message gets a unified ``mid``
that flows through four stages, each logged with a ``[iflow:*]`` prefix:

    [iflow:raw]      webhook 收到明文
    [iflow:event]    enrichment 后的标准字段
    [iflow:decision] 策略判定（dispatch / drop / record）
    [iflow:send]     bot 回复（如果有）

Hermes-agent runtime symbols are imported with a soft-fallback so that
this module is also importable in a hermes-free environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import ipaddress as _ipaddress
import logging
import os
import re
import socket as _socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin as _urljoin
from urllib.parse import urlparse as _urlparse

_PROGRESS_LINE_RE = re.compile(r"^[┊\s]*[🔍⚙️💻🌐📁📝🧠✨]")
_GROUP_STATUS_REDIRECT_PREFIXES = (
    "⚡ Interrupting current task",
    "⚠️ Gateway shutting down",
    "⚠️ Gateway restarting",
    "Gateway shutting down",
    "Gateway restarting",
    "💾 Self-improvement review:",
)
_GROUP_STATUS_TRACKER_ONLY_PREFIXES = (
    "📦 Preflight compression:",
    "🗜️ Compacting context",
    "⚠ Compression summary failed:",
    "⚠ Compression aborted:",
    "ℹ Configured compression model",
)
_GROUP_STATUS_TRACKER_ONLY_TEXT_PREFIXES = (
    "Preflight compression:",
    "Compacting context",
    "Compression summary failed:",
    "Compression aborted:",
    "Configured compression model",
)
GATEWAY_STARTED_NOTICE = "gateway started"


@dataclass(frozen=True)
class _UnreadMessageContext:
    history_before_count: int = 0
    effective_unread_count: int = 0


def _looks_like_progress_line(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 400:
        return False
    return bool(_PROGRESS_LINE_RE.match(t)) or "┊" in t[:20]


def _group_status_redirect_kind(text: str) -> str:
    t = (text or "").lstrip()
    for prefix in _GROUP_STATUS_REDIRECT_PREFIXES:
        if t.startswith(prefix):
            return prefix
    return ""


def _group_status_tracker_only_kind(text: str) -> str:
    t = (text or "").lstrip()
    for prefix in _GROUP_STATUS_TRACKER_ONLY_PREFIXES:
        if t.startswith(prefix):
            return prefix
    normalized = _drop_leading_status_glyphs(t)
    for prefix in _GROUP_STATUS_TRACKER_ONLY_TEXT_PREFIXES:
        if normalized.startswith(prefix):
            return prefix
    return ""


def _drop_leading_status_glyphs(text: str) -> str:
    t = str(text or "").lstrip()
    while t and not t[0].isalnum():
        t = t[1:].lstrip()
    return t


def _format_group_status_admin_notice(
    *,
    group_id: str,
    content: str,
    status_kind: str,
) -> str:
    return (
        "Infoflow 群聊状态消息已拦截\n"
        f"群：group:{group_id}\n"
        f"类型：{status_kind}\n\n"
        f"{content}"
    )

import aiohttp


def _make_send_result(*, success: bool, message_id: str = "", error: str = "", retryable: bool | None = None, continuation_message_ids: tuple[str, ...] = ()):
    kwargs: dict[str, Any] = {"success": success}
    if message_id:
        kwargs["message_id"] = message_id
    if error:
        kwargs["error"] = error
    if retryable is not None:
        kwargs["retryable"] = retryable
    if continuation_message_ids:
        kwargs["continuation_message_ids"] = list(continuation_message_ids)
    try:
        return SendResult(**kwargs)
    except TypeError:
        kwargs.pop("continuation_message_ids", None)
        return SendResult(**kwargs)


def _metadata_reply_type(metadata: dict[str, Any] | None) -> str:
    raw = str((metadata or {}).get("reply_type") or (metadata or {}).get("replytype") or "1")
    return raw if raw in {"1", "2"} else "1"


# ── Context var: propagate inbound mid → send() for tracing ────
_inbound_mid: contextvars.ContextVar[str] = contextvars.ContextVar("inbound_mid", default="")

# ── Hermes symbols (soft-import for testability) ──────────────────────

HERMES_AVAILABLE = False
try:
    from gateway.platforms.base import (  # type: ignore[import-not-found]
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        Platform,
        SendResult,
        cache_image_from_bytes,
    )

    HERMES_AVAILABLE = True
except ImportError:
    BasePlatformAdapter = object  # type: ignore[assignment,misc]
    MessageEvent = dict  # type: ignore[assignment,misc]
    MessageType = Any  # type: ignore[assignment,misc]
    Platform = Any  # type: ignore[assignment,misc]
    SendResult = dict  # type: ignore[assignment,misc]

    def cache_image_from_bytes(*a, **kw):  # type: ignore[misc]
        return None

# ── Plugin modules ────────────────────────────────────────────────────

from .bot import Bot  # noqa: E402
from .dashboard import get_tracker, normalize_chat_id
from .identity import raw_id_from_key, sender_key
from .iftools import (
    GROUP_MEMBERS_TOOL_SCHEMA,
    HISTORY_TOOL_SCHEMA,
    RECALL_TOOL_SCHEMA,
    REPLY_TOOL_SCHEMA,
    make_group_members_handler,
    make_history_handler,
    make_recall_handler,
    make_reply_handler,
)
from .itypes import IncomingMessage, ReplyInfo, reply_target_to_dict
from .llm_format import (
    DMAttention,
    GroupAttention,
    dm_attention_line,
    format_message_envelope,
    group_attention_line,
    sender_line,
    unread_message_context_line,
)
from .media import IMAGE_LOAD_MAX_BYTES, prepare_infoflow_image_bytes
from .message_content import render_message_content
from .message_store import MessageStore
from .outbound import prepare_outbound_message
from .policy import (
    _DM_FORMAT_DOC,
    _GROUP_FORMAT_DOC,
    _GROUP_MENTION_RULES_DOC,
    _INFOFLOW_MESSAGE_FORMAT_DOC,
    _INFOFLOW_PERMISSION_SECURITY_DOC,
    _INFOFLOW_REFERENCE_RULES_DOC,
    _INFOFLOW_TOOL_RULES_DOC,
    GroupConfigOverride,
    GroupPolicy,
    PolicyDecision,
    normalize_reply_mode,
)
from .prompt_rules import INFOFLOW_DELIVERY_TOOL_RULES
from .recall import get_inbound_body, get_inbound_sender_id, get_inbound_sender_imid
from .recall_silence import RecallSilenceTracker
from .sent_store import SentMessageStore
from .serverapi import ServerAPI
from .settings import (
    DEFAULT_BODY_LIMIT_BYTES,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_WEBHOOK_PATH,
    GROUP_TARGET_RE,
    MAX_MESSAGE_LENGTH,
    MAX_PREVIEW_LENGTH,
    _check_requirements,
    _env_enablement,
    _interactive_setup,
    _is_connected,
    _parse_infoflow_target,
    _read_account_settings,
    _validate_config,
)
from .standalone import standalone_send
from .utils import (
    _download_inbound_image,
    _ImageLoadError,
    _is_safe_outbound_url,
    _resolve_safe_local_path,
    gw_log,
)
from .webhook import WebhookServer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# InfoflowAdapter
# ---------------------------------------------------------------------------


class InfoflowAdapter(BasePlatformAdapter):  # type: ignore[misc]
    """Hermes gateway adapter for Baidu Infoflow (如流).

    Responsibilities (and **only** these):

    * Parse Hermes config → settings dict
    * Create ``ServerAPI`` + ``Bot`` instances
    * Translate between ``types.IncomingMessage`` and Hermes ``MessageEvent``
    * Translate Hermes ``send()``/``send_image()``/``delete_message()`` calls
      into ``bot.send_message()``/``bot.send_image()``/``bot.recall_message()``
    * Run the HTTP webhook server
    """

    def __init__(self, config: Any, **kwargs):
        if not HERMES_AVAILABLE:
            raise RuntimeError(
                "InfoflowAdapter requires hermes-agent to be importable "
                "(install hermes-agent first, or run the plugin via "
                "`hermes gateway`)."
            )
        platform = Platform("infoflow")  # type: ignore[call-arg]
        super().__init__(config=config, platform=platform)

        self._settings = _read_account_settings(config)
        self._admin_uid = os.getenv("INFOFLOW_ADMIN_USER", "").strip().lower()

        # ── ServerAPI (Infoflow service layer) ─────────────────────────
        self._serverapi = ServerAPI(settings=self._settings)
        # Seed robot_id from config or persisted file
        from .bot import load_persisted_robot_id
        _loaded = load_persisted_robot_id()
        if _loaded and not self._serverapi.robot_id:
            self._serverapi.robot_id = _loaded

        # ── GroupPolicy ───────────────────────────────────────────────
        normalized_mode = normalize_reply_mode(self._settings["reply_mode"])
        if normalized_mode.warning:
            gw_log().warning("[infoflow] %s", normalized_mode.warning)

        per_group: dict[str, GroupConfigOverride] = {}
        for gid, group_cfg in (self._settings.get("groups") or {}).items():
            override = GroupConfigOverride(
                reply_mode=(
                    normalize_reply_mode(group_cfg.get("reply_mode")).value
                    if group_cfg.get("reply_mode") is not None
                    else None
                ),
                watch_mentions=(
                    tuple(str(x).strip() for x in group_cfg["watch_mentions"] if str(x).strip())
                    if isinstance(group_cfg.get("watch_mentions"), list)
                    else None
                ),
                watch_regex=(
                    tuple(str(x).strip() for x in group_cfg["watch_regex"] if str(x).strip())
                    if isinstance(group_cfg.get("watch_regex"), list)
                    else None
                ),
                follow_up=group_cfg.get("follow_up") if isinstance(group_cfg.get("follow_up"), bool) else None,
                follow_up_window=(
                    int(group_cfg["follow_up_window"])
                    if isinstance(group_cfg.get("follow_up_window"), (int, float))
                    else None
                ),
                system_prompt=(
                    str(group_cfg["system_prompt"])
                    if isinstance(group_cfg.get("system_prompt"), str)
                    else None
                ),
            )
            per_group[str(gid)] = override

        self._policy = GroupPolicy(
            reply_mode=normalized_mode.value,
            require_mention=self._settings["require_mention"],
            watch_mentions=tuple(self._settings["watch_mentions"]),
            watch_regex=tuple(self._settings["watch_regex"]),
            follow_up=self._settings["follow_up"],
            follow_up_window=self._settings["follow_up_window"],
            per_group_overrides=per_group,
        )

        # ── Dedup set + stores ────────────────────────────────────────
        self._dedup_set: set[str] = set()
        self._sent_message_ids: set[str] = set()
        self._sent_store = SentMessageStore(
            dedup_set=self._dedup_set,
            sent_message_ids=self._sent_message_ids,
            db_path=Path(self._settings["state_dir"]) / "infoflow" / "sent-messages.db",
            account_id=self._settings.get("app_key") or "default",
        )
        self._message_store = MessageStore(
            account_id=str(self._settings.get("app_agent_id") or "default"),
        )
        self._serverapi.set_group_members_observer(
            self._message_store.upsert_group_members
        )

        # ── Bot (business logic) ──────────────────────────────────────
        self._bot = Bot(
            settings=self._settings,
            policy=self._policy,
            serverapi=self._serverapi,
            sent_store=self._sent_store,
            dedup_set=self._dedup_set,
            message_store=self._message_store,
            admin_uid=self._admin_uid,
        )

        # Sync persisted robot_id to bot (so own-message echo filter works
        # immediately even when settings.robot_id was empty at startup).
        if self._serverapi.robot_id and not self._bot.robot_id:
            self._bot.robot_id = self._serverapi.robot_id

        # ── HTTP server ───────────────────────────────────────────────
        self._port: int = int(self._settings["port"])
        self._host: str = str(self._settings["host"])
        self._webhook_path: str = str(self._settings["webhook_path"]) or DEFAULT_WEBHOOK_PATH
        if not self._webhook_path.startswith("/"):
            self._webhook_path = "/" + self._webhook_path

        if not hasattr(self, "_background_tasks"):
            self._background_tasks: set[asyncio.Task[Any]] = set()

        # ── Session dashboard (plugin hooks + infoflow events) ─────
        self._tracker = get_tracker()
        self._recall_silence = RecallSilenceTracker()

        # ── HTTP webhook server (delegated to webhook.py) ──────────
        self._webhook_server = WebhookServer(
            serverapi=self._serverapi,
            sent_message_ids=self._sent_message_ids,
            webhook_path=self._webhook_path,
            host=self._host,
            port=self._port,
            body_limit=DEFAULT_BODY_LIMIT_BYTES,
            on_message=self._on_inbound_message,
            task_set=self._background_tasks,
            tracker=self._tracker,
        )

        self._http_session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Infoflow"

    def _missing_required(self) -> list[str]:
        missing = []
        for key, label in (
            ("app_key", "INFOFLOW_APP_KEY"),
            ("app_secret", "INFOFLOW_APP_SECRET"),
            ("check_token", "INFOFLOW_CHECK_TOKEN"),
            ("encoding_aes_key", "INFOFLOW_ENCODING_AES_KEY"),
        ):
            if not self._settings.get(key):
                missing.append(label)
        return missing

    async def connect(self) -> bool:
        if not AIOHTTP_WEB_AVAILABLE:
            self._set_fatal_error(
                "MISSING_AIOHTTP",
                "aiohttp is required for the Infoflow webhook server",
                retryable=False,
            )
            return False

        missing = self._missing_required()
        if missing:
            self._set_fatal_error(
                "MISSING_CREDENTIALS",
                f"Infoflow requires: {', '.join(missing)}",
                retryable=False,
            )
            return False

        if self._settings["connection_mode"] != "webhook":
            self._set_fatal_error(
                "UNSUPPORTED_CONNECTION_MODE",
                (
                    f"INFOFLOW_CONNECTION_MODE={self._settings['connection_mode']!r} is "
                    "not implemented in hermes-infoflow yet. Only 'webhook' is "
                    "supported."
                ),
                retryable=False,
            )
            return False

        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("127.0.0.1", self._port))
            self._set_fatal_error(
                "PORT_IN_USE",
                f"Infoflow webhook port {self._port} is already in use",
                retryable=True,
            )
            return False
        except (ConnectionRefusedError, OSError):
            pass

        self._http_session = aiohttp.ClientSession()
        self._serverapi.http_session = self._http_session
        try:
            await self._webhook_server.start()
        except Exception as exc:
            await self._close_http_session()
            self._set_fatal_error(
                "BIND_FAILED",
                f"Failed to start webhook server on {self._host}:{self._port}: {exc}",
                retryable=True,
            )
            return False

        self._running = True
        self._mark_connected()
        logger.info(
            "[infoflow] Webhook listening on %s:%d%s",
            self._host, self._port, self._webhook_path,
        )
        self._schedule_admin_gateway_started_notice()
        return True

    async def disconnect(self) -> None:
        self._running = False
        with contextlib.suppress(Exception):
            await self._webhook_server.stop()
        await self._close_http_session()
        self._mark_disconnected()
        gw_log().info("[infoflow] Disconnected")

    async def _close_http_session(self) -> None:
        if self._http_session is not None:
            with contextlib.suppress(Exception):
                await self._http_session.close()
            self._http_session = None
            self._serverapi.http_session = None

    def _schedule_admin_gateway_started_notice(self) -> None:
        """Best-effort startup notice; never block or fail ``connect()``."""
        if not self._admin_uid:
            return

        try:
            task = asyncio.create_task(
                self._notify_admin_gateway_started(
                    session=self._effective_session(self._http_session),
                )
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                gw_log().warning(
                    "[infoflow] failed to schedule gateway startup notice "
                    "admin=%s error=%s",
                    self._admin_uid,
                    exc,
                    exc_info=True,
                )
            return

        with contextlib.suppress(Exception):
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        with contextlib.suppress(Exception):
            task.add_done_callback(self._consume_gateway_started_notice_task)

    @staticmethod
    def _consume_gateway_started_notice_task(task: asyncio.Task[Any]) -> None:
        """Consume task errors so fire-and-forget startup notices stay isolated."""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if exc is None:
            return
        with contextlib.suppress(Exception):
            gw_log().warning(
                "[infoflow] gateway startup notice task failed: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _notify_admin_gateway_started(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Send a startup notice to the configured admin DM."""
        if not self._admin_uid:
            return

        try:
            await self._bot.send_message(
                dm_user_id=self._admin_uid,
                text=GATEWAY_STARTED_NOTICE,
                session=session,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                gw_log().warning(
                    "[infoflow] failed to send gateway startup notice "
                    "admin=%s error=%s",
                    self._admin_uid,
                    exc,
                    exc_info=True,
                )
            return

        with contextlib.suppress(Exception):
            self._push_infoflow_event(
                None,
                kind="outbound.infoflow",
                chat_id=self._admin_uid,
                extra={
                    "type": "text",
                    "chars": len(GATEWAY_STARTED_NOTICE),
                    "preview": GATEWAY_STARTED_NOTICE,
                    "gateway_startup_notice": True,
                    "attempted": True,
                },
            )

    # ------------------------------------------------------------------
    # Inbound message callback (from webhook server)
    # ------------------------------------------------------------------

    def _infoflow_chat_id(self, msg: IncomingMessage) -> str:
        if msg.is_group and msg.group_id:
            return f"group:{msg.group_id}"
        return msg.dm_user_id or msg.sender_id or ""

    def _participant_name_for_sender(self, msg: IncomingMessage) -> str:
        if msg.sender_is_bot:
            aid = str(getattr(msg, "sender_agent_id", "") or "").strip()
            if aid and not aid.startswith("IMID:"):
                rec = self._message_store.find_bot_by_agent_id(aid)
                if rec and rec.name:
                    return rec.name
                return msg.sender_name or ""
            return ""
        uid = msg.sender_id or ""
        if uid and not uid.startswith("IMID:"):
            rec = self._message_store.find_user_by_user_id(uid)
            if rec and rec.name:
                return rec.name
        return ""

    def _agent_id_for_robot_id(self, robot_id: str) -> str | None:
        rid = str(robot_id or "").strip()
        if not rid:
            return None
        rec = self._message_store.find_participant_by_imid(rid)
        if rec and rec.participant_type == "bot" and rec.agent_id:
            return rec.agent_id
        if rid == str(getattr(self._serverapi, "robot_id", "") or "").strip():
            return str(self._settings.get("app_agent_id") or "").strip() or None
        return None

    def _participant_name_for_key(self, sender_key_text: str) -> str:
        key = str(sender_key_text or "").strip()
        if key.startswith("bot:"):
            rec = self._message_store.find_bot_by_agent_id(key.removeprefix("bot:"))
            return str(getattr(rec, "name", "") or "").strip() if rec else ""
        if key.startswith("user:"):
            rec = self._message_store.find_user_by_user_id(key.removeprefix("user:"))
            return str(getattr(rec, "name", "") or "").strip() if rec else ""
        return ""

    def _sender_key_for_llm(self, msg: IncomingMessage) -> str:
        key = sender_key(msg)
        if key:
            return key
        if msg.sender_is_bot:
            return "bot:unknown"
        return "user:unknown"

    def _group_attention_for_message(self, msg: IncomingMessage) -> GroupAttention:
        if msg.message_id:
            rec = self._message_store.find_group(msg.message_id)
            if rec is not None:
                return GroupAttention(
                    mentions_you=rec.mentions_you,
                    matched_regex_pattern=rec.matched_regex_pattern,
                    mentions_everyone=rec.mentions_everyone,
                    quotes_your_message=rec.quotes_your_message,
                    mentions_other_people=rec.mentions_other_people,
                    quotes_other_peoples_message=rec.quotes_other_peoples_message,
                )
        mentions_everyone = any(
            (getattr(item, "type", "") or "").upper() == "AT"
            and bool(getattr(item, "at_all", False))
            for item in (msg.body_items or [])
        )
        return GroupAttention(
            mentions_you=bool(msg.bot_was_mentioned),
            mentions_everyone=mentions_everyone,
            quotes_your_message=bool(msg.is_reply_to_bot),
        )

    def _dm_attention_for_message(self, msg: IncomingMessage) -> DMAttention:
        if msg.message_id:
            rec = self._message_store.find_dm(msg.message_id)
            if rec is not None:
                return DMAttention(quotes_your_message=rec.quotes_your_message)
        return DMAttention(quotes_your_message=bool(msg.is_reply_to_bot))

    def _message_created_time_for_llm(self, msg: IncomingMessage) -> int:
        if not msg.message_id:
            return 0
        rec = (
            self._message_store.find_group(msg.message_id)
            if msg.is_group
            else self._message_store.find_dm(msg.message_id)
        )
        return int(getattr(rec, "created_time", 0) or 0) if rec is not None else 0

    def _format_current_message_for_llm(
        self,
        msg: IncomingMessage,
        *,
        content: str,
        handling_strategy: str = "",
    ) -> str:
        sender = self._sender_key_for_llm(msg)
        sender_name = self._participant_name_for_sender(msg)
        sender_text = sender_line(
            sender_key=sender,
            name=sender_name,
            admin_uid=self._admin_uid,
        )
        if msg.is_group:
            attention = group_attention_line(self._group_attention_for_message(msg))
        else:
            attention = dm_attention_line(self._dm_attention_for_message(msg))
        return format_message_envelope(
            attention_line=attention,
            sender_line_text=sender_text,
            message_id=msg.message_id,
            content=content,
            created_time_ms=self._message_created_time_for_llm(msg),
            handling_strategy=handling_strategy,
        )

    @staticmethod
    def _chat_key_for_target(group_id: str | None, dm_user: str | None) -> str:
        if group_id:
            return f"group:{group_id}"
        if dm_user:
            return f"dm:user:{dm_user}"
        return ""

    def _chat_key_for_event(self, event: Any) -> tuple[str, str, str]:
        group_id, dm_user = self._target_from_event(event)
        return self._chat_key_for_target(group_id, dm_user), group_id or "", dm_user or ""

    def _llm_context_key_for_event(self, event: Any) -> str:
        source = getattr(event, "source", None)
        if source is None:
            return ""
        try:
            from gateway.session import build_session_key  # type: ignore[import-not-found]

            return build_session_key(
                source,
                group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            )
        except Exception:
            platform = getattr(getattr(source, "platform", None), "value", "infoflow")
            parts = [
                "agent:main",
                str(platform or "infoflow"),
                str(getattr(source, "chat_type", "") or ""),
                str(getattr(source, "chat_id", "") or ""),
            ]
            user_id = str(getattr(source, "user_id", "") or "")
            if user_id:
                parts.append(user_id)
            return ":".join(parts)

    def _record_for_event(self, event: Any) -> Any | None:
        message_id = str(getattr(event, "message_id", "") or "")
        if not message_id:
            return None
        chat_key, group_id, dm_user = self._chat_key_for_event(event)
        del chat_key
        if group_id:
            return self._message_store.find_group(message_id)
        if dm_user:
            return self._message_store.find_dm(message_id)
        return None

    def _is_local_plugin_sent_record(self, chat_key: str, record: Any) -> bool:
        if bool(getattr(record, "local_sent", False)):
            return True
        message_id = str(getattr(record, "message_id", "") or "")
        if not message_id:
            return False
        store_keys = [str(chat_key or "")]
        peer = str(getattr(record, "peer", "") or "")
        if chat_key.startswith("dm:user:"):
            store_keys.append(chat_key.removeprefix("dm:user:"))
        if peer.startswith("user:"):
            store_keys.append(peer.removeprefix("user:"))
        elif peer:
            store_keys.append(peer)
        for store_key in dict.fromkeys(k for k in store_keys if k):
            if self._sent_store.find(store_key, message_id):
                return True
        return False

    def _is_effective_unread_record(self, chat_key: str, record: Any) -> bool:
        # External sends through this bot identity have no local send record,
        # so they remain unread context for this plugin.
        return not self._is_local_plugin_sent_record(chat_key, record)

    def _unread_message_context_for_event(self, event: Any) -> _UnreadMessageContext:
        current = self._record_for_event(event)
        if current is None:
            return _UnreadMessageContext()
        chat_key, group_id, dm_user = self._chat_key_for_event(event)
        if not chat_key:
            return _UnreadMessageContext()
        context_key = self._llm_context_key_for_event(event)
        state = self._message_store.get_llm_context_state(context_key)
        after_time = 0
        after_mid = ""
        if state is not None and state.chat_key == chat_key:
            after_time = state.last_llm_visible_created_time
            after_mid = state.last_llm_visible_message_id
        if group_id:
            records = self._message_store.group_between(
                group_id,
                after_created_time=after_time,
                after_message_id=after_mid,
                before_created_time=current.created_time,
                before_message_id=current.message_id,
            )
        elif dm_user:
            records = self._message_store.dm_between(
                dm_user,
                after_created_time=after_time,
                after_message_id=after_mid,
                before_created_time=current.created_time,
                before_message_id=current.message_id,
            )
        else:
            records = []

        first_effective_idx: int | None = None
        effective_count = 0
        for idx, record in enumerate(records):
            if not self._is_effective_unread_record(chat_key, record):
                continue
            effective_count += 1
            if first_effective_idx is None:
                first_effective_idx = idx
        if first_effective_idx is None:
            return _UnreadMessageContext()
        return _UnreadMessageContext(
            history_before_count=len(records) - first_effective_idx,
            effective_unread_count=effective_count,
        )

    def _unread_message_context_count_for_event(self, event: Any) -> int:
        return self._unread_message_context_for_event(event).history_before_count

    def _mark_event_llm_visible(self, event: Any) -> None:
        current = self._record_for_event(event)
        if current is None:
            return
        chat_key, _group_id, _dm_user = self._chat_key_for_event(event)
        context_key = self._llm_context_key_for_event(event)
        if not chat_key or not context_key:
            return
        self._message_store.update_llm_context_state(
            llm_context_key=context_key,
            chat_key=chat_key,
            message_id=current.message_id,
            created_time=current.created_time,
        )

    def _build_channel_prompt(
        self,
        msg: IncomingMessage,
        decision: PolicyDecision | None,
    ) -> str:
        bot_name = self._settings.get("robot_name") or ""
        bot_agent_id = str(
            self._settings.get("app_agent_id")
            or os.getenv("INFOFLOW_APP_AGENT_ID", "")
            or ""
        )
        identity = ""
        if bot_name or bot_agent_id:
            identity = (
                "## 身份与会话\n"
                f"你是 Infoflow host 机器人。name={bot_name or 'unknown'}; "
                f"agent_id={bot_agent_id or 'unknown'}。"
            )
        common_parts = [
            identity,
            _INFOFLOW_MESSAGE_FORMAT_DOC,
            _INFOFLOW_PERMISSION_SECURITY_DOC,
            _INFOFLOW_REFERENCE_RULES_DOC,
        ]
        if msg.is_group:
            group_prompt = ""
            if decision is not None:
                group_prompt = decision.group_system_prompt or ""
            group_behavior = (
                "## 群级行为提示\n" + group_prompt
                if group_prompt else ""
            )
            return "\n\n".join(
                part for part in [
                    *common_parts,
                    _GROUP_FORMAT_DOC,
                    _GROUP_MENTION_RULES_DOC,
                    group_behavior,
                    _INFOFLOW_TOOL_RULES_DOC,
                ] if part
            )
        return "\n\n".join(
            part for part in [
                *common_parts,
                _DM_FORMAT_DOC,
                _INFOFLOW_TOOL_RULES_DOC,
            ] if part
        )

    def _push_infoflow_event(
        self,
        msg: IncomingMessage | None,
        *,
        kind: str,
        extra: dict[str, Any] | None = None,
        chat_id: str = "",
    ) -> None:
        """Record an Infoflow-specific event on the session dashboard."""
        tracker = getattr(self, "_tracker", None)
        if tracker is None:
            return
        cid = normalize_chat_id(chat_id)
        if msg is not None:
            cid = cid or self._infoflow_chat_id(msg)
        payload: dict[str, Any] = {"chat_id": cid}
        if msg is not None:
            payload.update({
                "message_id": msg.message_id,
                "sender_id": msg.sender_id,
                "sender_name": msg.sender_name,
                "group_id": msg.group_id,
                "is_group": msg.is_group,
                "preview": render_message_content(
                    msg,
                    robot_agent_id_lookup=self._agent_id_for_robot_id,
                )[:500],
            })
        if extra:
            payload.update(extra)
        if kind == "outbound.infoflow" and extra and extra.get("type") == "text":
            preview = extra.get("preview")
            if isinstance(preview, str) and preview:
                payload["preview"] = preview[:200]
                if extra.get("is_progress_hint"):
                    payload["is_progress_hint"] = True
        tracker.push_event(
            "",
            kind,
            payload,
            platform="infoflow",
            chat_id=cid,
        )

    async def _on_inbound_message(self, msg: IncomingMessage) -> None:
        """Callback invoked by WebhookServer for each parsed message.

        Thin orchestration: enrich → policy → dispatch.
        Business logic lives in bot.py.
        """
        self._push_infoflow_event(msg, kind="inbound.infoflow")

        # Delegate to bot for enrich + policy + dedup + context
        result = await self._bot.process_inbound(msg)

        if result.should_dispatch and result.decision:
            self._bot.spawn_dispatch(msg, result.decision, self, self._background_tasks)

    # ------------------------------------------------------------------
    # build_message_event: IncomingMessage → Hermes MessageEvent
    # ------------------------------------------------------------------

    async def build_message_event(
        self,
        msg: IncomingMessage,
        decision: PolicyDecision | None = None,
    ) -> Any:
        """Translate ``types.IncomingMessage`` → Hermes ``MessageEvent``."""
        # Cache inbound images via gateway media helper
        local_media: list[str] = []
        media_types: list[str] = []
        if msg.image_urls and cache_image_from_bytes is not None:
            for url in msg.image_urls:
                downloaded = await _download_inbound_image(
                    url,
                    token_provider=lambda: self._serverapi.get_access_token(),
                    session=self._http_session,
                )
                if downloaded is None:
                    continue
                data, ext = downloaded
                try:
                    cached = cache_image_from_bytes(data, ext=ext)
                except Exception as exc:
                    gw_log().warning("[infoflow] cache_image_from_bytes failed: %s", exc)
                    continue
                local_media.append(cached)
                media_types.append(f"image/{ext.lstrip('.')}")

        chat_id = f"group:{msg.group_id}" if msg.is_group else (msg.dm_user_id or "")
        chat_type = "group" if msg.is_group else "dm"
        participant_name = self._participant_name_for_sender(msg)
        canonical_sender = sender_key(msg)
        source_user_id = raw_id_from_key(canonical_sender)
        if not source_user_id and not msg.sender_is_bot:
            source_user_id = msg.sender_id if not (msg.sender_id or "").startswith("IMID:") else ""
        _user_display = participant_name or source_user_id or "unknown"

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            user_id=source_user_id,
            user_name=_user_display,
            message_id=msg.message_id,
        )

        message_type = MessageType.PHOTO if local_media else MessageType.TEXT

        # --- Slash command fast path: use command_text directly ---
        # Skip all sender-tag / follow-up injection so gateway's
        # command dispatcher (event.text.startswith("/")) can handle it.
        if decision is not None and getattr(decision, "command_text", ""):
            event = MessageEvent(
                text=decision.command_text,
                message_type=MessageType.TEXT,
                source=source,
                raw_message={},
                message_id=msg.message_id,
            )
            return event

        text_for_agent = render_message_content(
            msg,
            robot_agent_id_lookup=self._agent_id_for_robot_id,
        )

        raw_message: dict[str, Any] = {
            "infoflow_standard_message": True,
            "infoflow_chat_key": self._chat_key_for_target(
                msg.group_id if msg.is_group else None,
                msg.dm_user_id if msg.is_dm else None,
            ),
            "raw_text": msg.text,
            "mention_user_ids": list(msg.mention_user_ids),
            "mention_robot_ids": list(getattr(msg, "mention_robot_ids", [])),
            "mention_agent_ids": list(msg.mention_agent_ids),
            "reply_targets": [reply_target_to_dict(t) for t in msg.reply_targets],
            "is_reply_to_bot": msg.is_reply_to_bot,
            "was_mentioned": msg.bot_was_mentioned,
            "image_urls": list(msg.image_urls),
            "msgseqid": msg.msgseqid,
            "raw_msgdata": msg.raw_data,
            "event_type": msg.event_type,
            "is_bot_sender": msg.sender_is_bot,
            "sender_key": canonical_sender,
            "sender_name": participant_name,
            "sender_agent_id": getattr(msg, "sender_agent_id", ""),
        }
        _handling_strategy = ""
        if decision is not None:
            raw_message["policy_action"] = decision.action.value
            raw_message["policy_reason"] = decision.reason
            raw_message["trigger_reason"] = decision.trigger_reason

            # Follow-up: enrich with sender context.
            # CRITICAL: inject follow-up instructions as a PREFIX of the user
            # message text (NOT into channel_prompt / system prompt).
            # Rationale: GLM-5-Turbo ignores follow-up directives when they're
            # buried in ~18K tokens of system prompt.  Prepending to the user
            # message guarantees the LLM sees the instruction in the most
            # recent turn, dramatically improving compliance.
            if getattr(decision, "needs_sender_context", False) and msg.group_id:
                try:
                    from .policy import build_follow_up_prompt
                    _sender_engaged = False
                    if hasattr(self._policy, "sender_engaged_recently"):
                        _engaged_key = ""
                        if msg.sender_is_bot:
                            _aid = getattr(msg, "sender_agent_id", "") or ""
                            if _aid and not _aid.startswith("IMID:"):
                                _engaged_key = str(_aid)
                        if not _engaged_key:
                            _engaged_key = msg.sender_id or msg.sender_imid
                        _sender_engaged = self._policy.sender_engaged_recently(
                            msg.group_id, _engaged_key,
                        )
                    _handling_strategy = build_follow_up_prompt(
                        sender_imid=msg.sender_imid,
                        sender_name=msg.sender_name or msg.sender_id,
                        is_bot=msg.sender_is_bot,
                        agent_id=getattr(msg, "sender_agent_id", ""),
                        is_reply_to_bot=msg.is_reply_to_bot,
                        sender_engaged=_sender_engaged,
                    )
                    _template = ("reply_to_bot" if msg.is_reply_to_bot
                                 else "engaged" if _sender_engaged else "passive")
                    gw_log().info(
                        "[iflow:dispatch] mid=%s template=%s sender_engaged=%s is_reply_to_bot=%s",
                        msg.message_id or "-", _template, _sender_engaged, msg.is_reply_to_bot,
                    )
                    gw_log().info(
                        "[iflow:dispatch] mid=%s prefix_len=%d",
                        msg.message_id or "-", len(_handling_strategy),
                    )
                except Exception as exc:
                    gw_log().warning(
                        "[infoflow] failed to build follow-up context for %s: %s",
                        msg.group_id, exc,
                    )

            # Per-message judgement prompt (watch, proactive, etc.) → user message prefix.
            # Same rationale as follow-up: per-message instructions in the system prompt
            # are ignored by GLM-5-Turbo when buried under ~18K tokens.  Injecting as a
            # user-message prefix guarantees the LLM evaluates the instruction.
            _per_msg = getattr(decision, "per_message_prompt", "")
            if _per_msg:
                _handling_strategy = (
                    f"{_per_msg}"
                    "\n但当工具/指令有明确行为要求时（如撤回成功后不发确认、静默完成等），"
                    "以工具行为要求为准。"
                )
                gw_log().info(
                    "[iflow:dispatch] mid=%s per_message_prompt_len=%d",
                    msg.message_id or "-", len(_per_msg),
                )

        text_for_agent = self._format_current_message_for_llm(
            msg,
            content=text_for_agent or "",
            handling_strategy=_handling_strategy,
        )

        # Log the base user message. Unread-message context is injected in
        # on_processing_start(), just before Hermes starts this event.
        gw_log().info(
            "[iflow:user_message:base] mid=%s len=%d text=\n%s",
            msg.message_id or "-", len(text_for_agent or ""), text_for_agent or "",
        )

        event = MessageEvent(
            text=text_for_agent,
            message_type=message_type,
            source=source,
            raw_message=raw_message,
            message_id=msg.message_id,
            media_urls=local_media,
            media_types=media_types,
        )
        event.channel_prompt = self._build_channel_prompt(msg, decision)
        if event.channel_prompt:
            gw_log().info(
                "[iflow:debug] channel_prompt len=%d FULL=\n%s",
                len(event.channel_prompt),
                event.channel_prompt,
            )
        # Quote-reply: only surface bot message ids
        if msg.reply_info:
            event.reply_to_message_id = msg.reply_info.message_id or None
            event.reply_to_text = msg.reply_info.preview or None
        return event

    def _target_from_event(self, event: Any) -> tuple[str | None, str | None]:
        source = getattr(event, "source", None)
        chat_id = getattr(source, "chat_id", "") or ""
        group_id: str | None = None
        dm_user: str | None = None
        if chat_id:
            kind, parsed_group_id, parsed_dm_user = self._parse_target(chat_id)
            if kind == "group" and parsed_group_id is not None:
                group_id = str(parsed_group_id)
            else:
                dm_user = parsed_dm_user or None
        return group_id, dm_user

    @staticmethod
    def _event_message_id(event: Any) -> str:
        source = getattr(event, "source", None)
        message_id = (
            getattr(event, "message_id", None)
            or getattr(source, "message_id", None)
            or ""
        )
        return str(message_id) if message_id else ""

    @staticmethod
    def _event_trigger_reason(event: Any) -> str:
        raw_message = getattr(event, "raw_message", None)
        if not isinstance(raw_message, dict):
            return ""
        return str(raw_message.get("trigger_reason") or "")

    def _bind_processing_context(
        self,
        event: Any,
    ) -> tuple[contextvars.Token[Any], ...]:
        """Bind Infoflow contextvars to the event Hermes is processing now."""
        from .bot import _reaction_promise_cv, _recall_hint, _send_path_cv

        message_id = self._event_message_id(event)
        group_id, dm_user = self._target_from_event(event)
        reaction_token = self._bot.reaction_token_for_context(
            group_id=group_id,
            dm_user_id=dm_user,
            reaction_message_id=message_id or None,
        )
        return (
            _inbound_mid.set(message_id),
            _send_path_cv.set(self._event_trigger_reason(event)),
            _recall_hint.set(message_id or None),
            _reaction_promise_cv.set(reaction_token),
        )

    @staticmethod
    def _reset_processing_context(
        tokens: tuple[contextvars.Token[Any], ...],
    ) -> None:
        for token in reversed(tokens):
            token.var.reset(token)

    async def _process_message_background(self, event: Any, session_key: str) -> None:
        """Run one Hermes background task with event-specific Infoflow context."""
        tokens = self._bind_processing_context(event)
        try:
            await super()._process_message_background(event, session_key)
        finally:
            self._reset_processing_context(tokens)

    async def on_processing_start(self, event: Any) -> None:
        """Inject per-event unread-message-context metadata at actual LLM start time."""
        raw_message = getattr(event, "raw_message", None)
        if not isinstance(raw_message, dict):
            return
        if not raw_message.get("infoflow_standard_message"):
            return
        if raw_message.get("infoflow_unread_message_context_applied"):
            return

        unread_context = self._unread_message_context_for_event(event)
        count = unread_context.history_before_count
        raw_message["infoflow_unread_message_context_count"] = count
        raw_message["infoflow_unread_message_context_before_count"] = count
        raw_message["infoflow_effective_unread_message_count"] = (
            unread_context.effective_unread_count
        )
        raw_message["infoflow_unread_message_context_applied"] = True
        text = str(getattr(event, "text", "") or "")
        if count > 0:
            text = f"{unread_message_context_line(count)}\n{text}"
            event.text = text
        gw_log().info(
            "[iflow:user_message] mid=%s unread_context_before_count=%d "
            "effective_unread_count=%d len=%d text=\n%s",
            self._event_message_id(event) or "-",
            count,
            unread_context.effective_unread_count,
            len(text),
            text,
        )

    async def on_processing_complete(self, event: Any, outcome: Any) -> None:
        """Clear the processing reaction after Hermes finishes the real run."""
        group_id, dm_user = self._target_from_event(event)
        outcome_label = str(
            getattr(outcome, "value", None)
            or getattr(outcome, "name", None)
            or "complete"
        ).lower()
        reaction_message_id = self._event_message_id(event)
        try:
            await self._bot.finish_processing_reaction(
                group_id=group_id,
                dm_user_id=dm_user,
                reaction_message_id=reaction_message_id or None,
                reason=f"processing_{outcome_label}",
            )
        finally:
            raw_message = getattr(event, "raw_message", None)
            if (
                outcome_label == "success"
                and isinstance(raw_message, dict)
                and raw_message.get("infoflow_standard_message")
            ):
                self._mark_event_llm_visible(event)

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_session_valid(session: aiohttp.ClientSession | None) -> bool:
        """Check whether *session* is usable on the current event loop.

        ``aiohttp`` sessions are bound to the loop that created them.
        Using a session from a *different* loop triggers
        ``RuntimeError: Timeout context manager should be used inside a task``
        because ``TimerContext.__enter__`` queries ``current_task`` on the
        session's original loop — which has no active task from the
        caller's perspective.  Compare loop identity to catch this.
        """
        if session is None:
            return False
        try:
            session_loop = session._loop  # noqa: SLF001
        except RuntimeError:
            return False
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        return session_loop is current_loop

    @staticmethod
    def _effective_session(session: aiohttp.ClientSession | None) -> aiohttp.ClientSession | None:
        """Return *session* if valid on the current loop, else ``None``."""
        return session if InfoflowAdapter._is_session_valid(session) else None

    # ------------------------------------------------------------------
    # Outbound: send
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_target(chat_id: str) -> tuple[str, int | None, str]:
        """Parse a Hermes chat_id into (kind, group_id, dm_user).

        Supports: ``group:4507088``, ``infoflow:group:4507088``,
        ``chengbo05``, ``infoflow:chengbo05``.
        """
        # Strip ``infoflow:`` prefix first so the regex always sees canonical form.
        chat_id = InfoflowAdapter._normalize_chat_id(chat_id)
        match = GROUP_TARGET_RE.match(chat_id)
        if match:
            return "group", int(match.group(1)), ""
        return "dm", None, chat_id

    @staticmethod
    def _normalize_chat_id(chat_id: str) -> str:
        """Normalize ``infoflow:group:4507088`` → ``group:4507088``."""
        if chat_id.startswith("infoflow:"):
            return chat_id[len("infoflow:"):]
        return chat_id

    def _recall_silence_tracker(self) -> RecallSilenceTracker:
        tracker = getattr(self, "_recall_silence", None)
        if tracker is None:
            tracker = RecallSilenceTracker()
            self._recall_silence = tracker
        return tracker

    @staticmethod
    def _current_inbound_mid() -> str:
        mid = _inbound_mid.get("")
        if mid:
            return mid
        with contextlib.suppress(Exception):
            from .bot import get_recall_inbound_message_id_hint

            return get_recall_inbound_message_id_hint() or ""
        return ""

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a text/markdown message (Hermes interface → bot layer)."""
        session = self._effective_session(self._http_session)
        kind, group_id, dm_user = self._parse_target(chat_id)
        inbound_mid = self._current_inbound_mid()

        if self._recall_silence_tracker().consume_if_suppress(
            inbound_mid=inbound_mid,
            chat_id=chat_id,
            text=content,
        ):
            gw_log().info(
                "[iflow:send] mid=%s target=%s suppressed recall ack preview=%r",
                inbound_mid,
                chat_id,
                (content or "")[:80],
            )
            await self._bot.finish_processing_reaction(
                group_id=str(group_id) if group_id is not None else None,
                dm_user_id=dm_user or None,
                reaction_message_id=reply_to or inbound_mid or None,
                reason="recall_ack_suppressed",
            )
            self._push_infoflow_event(
                None,
                kind="outbound.infoflow",
                chat_id=chat_id,
                extra={
                    "type": "text",
                    "chars": len(content or ""),
                    "success": True,
                    "message_id": "",
                    "error": "",
                    "suppressed_recall_ack": True,
                    "preview": (content or "")[:200],
                },
            )
            return SendResult(success=True)

        status_kind = (
            _group_status_redirect_kind(content)
            if kind == "group" and group_id is not None
            else ""
        )
        tracker_only_status_kind = (
            _group_status_tracker_only_kind(content)
            if kind == "group" and group_id is not None
            else ""
        )
        if status_kind or tracker_only_status_kind:
            effective_status_kind = status_kind or tracker_only_status_kind
            redirected = False
            if status_kind:
                redirected = await self._redirect_group_status_to_admin(
                    group_id=str(group_id),
                    content=content,
                    status_kind=status_kind,
                    session=session,
                )
            gw_log().info(
                "[iflow:send] suppressed group status target=%s kind=%s redirected_admin=%s",
                chat_id,
                effective_status_kind,
                redirected,
            )
            self._push_infoflow_event(
                None,
                kind="outbound.infoflow",
                chat_id=chat_id,
                extra={
                    "type": "text",
                    "chars": len(content or ""),
                    "success": True,
                    "message_id": "",
                    "error": "",
                    "suppressed_group_status": True,
                    "sessiontracker_only_status": bool(tracker_only_status_kind),
                    "redirected_to_admin": redirected,
                    "preview": (content or "")[:200],
                },
            )
            return SendResult(success=True)

        # Build reply_info from inbound context
        reply_info: ReplyInfo | None = None
        if reply_to:
            body = get_inbound_body(reply_to)
            if body:
                reply_info = ReplyInfo(
                    message_id=reply_to,
                    preview=body[:MAX_PREVIEW_LENGTH],
                    replytype=_metadata_reply_type(metadata),
                    sender_imid=get_inbound_sender_imid(reply_to),
                    sender_id=get_inbound_sender_id(reply_to),
                )

        content, options = await prepare_outbound_message(
            content,
            group_id=str(group_id) if group_id is not None else None,
            metadata=metadata,
            get_group_members=self._serverapi.get_group_members,
            session=session,
            bot_agent_id=self._settings.get("app_agent_id"),
        )

        # Delegate to bot
        bot_result = await self._bot.send_message(
            group_id=str(group_id) if group_id is not None else None,
            dm_user_id=dm_user or None,
            text=content,
            reply_info=reply_info,
            options=options,
            session=session,
            reaction_message_id=reply_to,
        )

        # Trace outbound with inbound mid (if available via contextvar)
        _mid = _inbound_mid.get("")
        if _mid:
            gw_log().info(
                "[iflow:send] mid=%s target=%s chars=%d success=%s",
                _mid, chat_id, len(content), bot_result.success,
            )

        extra: dict[str, Any] = {
            "type": "text",
            "chars": len(content),
            "success": bot_result.success,
            "message_id": bot_result.message_id,
            "error": bot_result.error,
        }
        if content and len(content) <= 500:
            extra["preview"] = content[:200]
            if _looks_like_progress_line(content):
                extra["is_progress_hint"] = True
        self._push_infoflow_event(
            None,
            kind="outbound.infoflow",
            chat_id=chat_id,
            extra=extra,
        )

        if bot_result.success:
            return _make_send_result(
                success=True,
                message_id=bot_result.message_id,
                continuation_message_ids=tuple(
                    getattr(bot_result, "continuation_message_ids", ()) or ()
                ),
            )
        return _make_send_result(
            success=False,
            message_id=bot_result.message_id,
            continuation_message_ids=tuple(
                getattr(bot_result, "continuation_message_ids", ()) or ()
            ),
            error=bot_result.error,
            retryable=False,
        )

    async def _redirect_group_status_to_admin(
        self,
        *,
        group_id: str,
        content: str,
        status_kind: str,
        session: aiohttp.ClientSession | None = None,
    ) -> bool:
        """Forward suppressed Hermes runtime status from a group to admin DM."""
        if not self._admin_uid:
            return False

        notice = _format_group_status_admin_notice(
            group_id=group_id,
            content=content,
            status_kind=status_kind,
        )

        # This is an out-of-band admin notice, not the final answer for the
        # current group turn. Preserve the group's processing emoji until the
        # actual queued/final response path clears it.
        from .bot import _reaction_promise_cv

        result_success = False
        result_message_id = ""
        result_error = ""
        redirect_error_logged = False
        token = _reaction_promise_cv.set(None)
        try:
            try:
                result = await self._bot.send_message(
                    dm_user_id=self._admin_uid,
                    text=notice,
                    session=session,
                )
                result_success = result.success
                result_message_id = result.message_id
                result_error = result.error
            except Exception as exc:
                result_error = str(exc)
                gw_log().warning(
                    "[iflow:send] failed to redirect group status to admin=%s group=%s error=%s",
                    self._admin_uid,
                    group_id,
                    result_error,
                    exc_info=True,
                )
                redirect_error_logged = True
        finally:
            _reaction_promise_cv.reset(token)
        self._push_infoflow_event(
            None,
            kind="outbound.infoflow",
            chat_id=self._admin_uid,
            extra={
                "type": "text",
                "chars": len(notice),
                "success": result_success,
                "message_id": result_message_id,
                "error": result_error,
                "preview": notice[:200],
                "admin_status_redirect": True,
                "source_group_id": group_id,
            },
        )
        if result_error and not result_success and not redirect_error_logged:
            gw_log().warning(
                "[iflow:send] failed to redirect group status to admin=%s group=%s error=%s",
                self._admin_uid,
                group_id,
                result_error,
            )
        return result_success

    async def send_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        return None

    # ------------------------------------------------------------------
    # Outbound: send_image
    # ------------------------------------------------------------------

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send an image (Hermes interface → bot layer)."""
        session = self._effective_session(self._http_session)
        kind, group_id, dm_user = self._parse_target(chat_id)
        try:
            raw_image_bytes = await self._load_image_bytes(image_url)
            prepared_image = prepare_infoflow_image_bytes(raw_image_bytes)
        except _ImageLoadError as exc:
            return SendResult(success=False, error=str(exc), retryable=False)

        reply_info: ReplyInfo | None = None
        if reply_to:
            body = get_inbound_body(reply_to)
            if body:
                reply_info = ReplyInfo(
                    message_id=reply_to,
                    preview=body[:MAX_PREVIEW_LENGTH],
                    replytype=_metadata_reply_type(metadata),
                    sender_imid=get_inbound_sender_imid(reply_to),
                    sender_id=get_inbound_sender_id(reply_to),
                )

        bot_result = await self._bot.send_image(
            group_id=str(group_id) if group_id is not None else None,
            dm_user_id=dm_user or None,
            image_bytes=prepared_image.data,
            caption=caption,
            reply_info=reply_info,
            session=session,
            reaction_message_id=reply_to,
        )

        # Trace outbound with inbound mid
        _mid = _inbound_mid.get("")
        if _mid:
            gw_log().info(
                "[iflow:send] mid=%s target=%s type=image success=%s",
                _mid, chat_id, bot_result.success,
            )

        self._push_infoflow_event(
            None,
            kind="outbound.infoflow",
            chat_id=chat_id,
            extra={
                "type": "image",
                "success": bot_result.success,
                "message_id": bot_result.message_id,
                "error": bot_result.error,
                "image_mime": prepared_image.mime_type,
                "image_bytes": prepared_image.final_size,
                "image_compressed": prepared_image.compressed,
            },
        )

        if bot_result.success:
            return _make_send_result(
                success=True,
                message_id=bot_result.message_id,
                continuation_message_ids=tuple(
                    getattr(bot_result, "continuation_message_ids", ()) or ()
                ),
            )
        return _make_send_result(
            success=False,
            message_id=bot_result.message_id,
            continuation_message_ids=tuple(
                getattr(bot_result, "continuation_message_ids", ()) or ()
            ),
            error=bot_result.error,
            retryable=False,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send a local image file natively without exposing its local path."""
        del kwargs
        return await self.send_image(
            chat_id=chat_id,
            image_url=image_path,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Outbound: recall (delete_message)
    # ------------------------------------------------------------------

    async def delete_message(
        self,
        chat_id: str,
        message_id: str | None = None,
        *,
        count: int = 1,
    ) -> SendResult:
        """Recall one or more bot-sent messages (Hermes interface → bot layer)."""
        session = self._effective_session(self._http_session)
        kind, group_id, dm_user = self._parse_target(chat_id)
        result = await self._bot.recall_message(
            group_id=str(group_id) if group_id is not None else None,
            dm_user_id=dm_user or None,
            message_id=message_id,
            msgseqid="",
            count=count,
            session=session,
        )

        self._push_infoflow_event(
            None,
            kind="outbound.infoflow",
            chat_id=chat_id,
            extra={
                "type": "recall",
                "success": result.success,
                "message_id": message_id,
                "count": count,
                "error": result.error,
            },
        )

        if result.success:
            inbound_mid = self._current_inbound_mid()
            self._recall_silence_tracker().mark_success(
                inbound_mid=inbound_mid,
                chat_id=chat_id,
            )
            return SendResult(success=True)
        return SendResult(success=False, error=result.error, retryable=False)

    # ------------------------------------------------------------------
    # Image loading (adapter-level: Hermes needs local media paths)
    # ------------------------------------------------------------------

    async def _load_image_bytes(self, image_url: str) -> bytes:
        """Return raw image bytes from a URL or sanitised local path."""
        if image_url.startswith("http://") or image_url.startswith("https://"):
            return await self._fetch_url_bytes(image_url)
        candidate = _resolve_safe_local_path(image_url)
        if candidate is None:
            raise _ImageLoadError(
                "refusing to read local image: not inside an allowed media root"
            )
        try:
            size = candidate.stat().st_size
            if size > IMAGE_LOAD_MAX_BYTES:
                raise _ImageLoadError(
                    f"local image payload exceeds {IMAGE_LOAD_MAX_BYTES} bytes; aborting"
                )
            return candidate.read_bytes()
        except _ImageLoadError:
            raise
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or exc.__class__.__name__
            raise _ImageLoadError(f"failed to read local image: {reason}") from exc

    async def _fetch_url_bytes(
        self, url: str, *, max_bytes: int = IMAGE_LOAD_MAX_BYTES,
    ) -> bytes:
        own_session = self._http_session is None
        session = self._http_session or aiohttp.ClientSession()
        try:
            current_url = url
            for _ in range(6):
                await self._assert_safe_fetch_url(current_url)
                async with session.get(
                    current_url,
                    timeout=aiohttp.ClientTimeout(total=30.0),
                    allow_redirects=False,
                ) as resp:
                    if 300 <= resp.status < 400:
                        location = resp.headers.get("Location")
                        if not location:
                            raise _ImageLoadError("image fetch redirect missing Location")
                        current_url = _urljoin(current_url, location)
                        continue

                    if resp.status >= 400:
                        raise _ImageLoadError(f"image fetch HTTP {resp.status}")

                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise _ImageLoadError(
                                f"image payload exceeds {max_bytes} bytes; aborting"
                            )
                    return bytes(buf)

            raise _ImageLoadError("image fetch exceeded redirect limit")
        finally:
            if own_session:
                await session.close()

    async def _assert_safe_fetch_url(self, url: str) -> None:
        ok, reason = _is_safe_outbound_url(url)
        if not ok:
            raise _ImageLoadError(f"refusing to fetch image: {reason}")

        parsed = _urlparse(url)
        host = parsed.hostname
        if not host:
            raise _ImageLoadError("refusing to fetch image: URL has no hostname")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                type=_socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise _ImageLoadError("refusing to fetch image: DNS lookup failed") from exc

        if not infos:
            raise _ImageLoadError("refusing to fetch image: DNS lookup returned no addresses")

        for info in infos:
            sockaddr = info[4]
            ip_text = sockaddr[0]
            try:
                ip = _ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
            ):
                raise _ImageLoadError(
                    "refusing to fetch image: hostname resolves to a non-public IP"
                )

    # ------------------------------------------------------------------
    # Chat info
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        kind, group_id, dm_user = self._parse_target(chat_id)
        if kind == "group":
            return {"name": f"group:{group_id}", "type": "group", "chat_id": chat_id}
        return {"name": dm_user, "type": "dm", "chat_id": chat_id}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _chat_label_for_log(msg: IncomingMessage) -> str:
        if msg.is_group and msg.group_id:
            return f"group:{msg.group_id}"
        return msg.sender_id or "unknown"


# ---------------------------------------------------------------------------
# aiohttp availability check
# ---------------------------------------------------------------------------

try:
    from aiohttp import web as _aiohttp_web_module  # noqa: E402,F401
    AIOHTTP_WEB_AVAILABLE = True
except ImportError:
    AIOHTTP_WEB_AVAILABLE = False


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Plugin entry point. Called by hermes-agent's plugin manager."""
    if not HERMES_AVAILABLE:
        raise RuntimeError(
            "hermes-infoflow.register() called without hermes-agent on PYTHONPATH"
        )

    ctx.register_platform(
        name="infoflow",
        label="Infoflow (如流)",
        adapter_factory=lambda cfg: InfoflowAdapter(cfg),
        check_fn=_check_requirements,
        validate_config=_validate_config,
        is_connected=_is_connected,
        required_env=[
            "INFOFLOW_CHECK_TOKEN",
            "INFOFLOW_ENCODING_AES_KEY",
            "INFOFLOW_APP_KEY",
            "INFOFLOW_APP_SECRET",
        ],
        install_hint=(
            "pip install hermes-infoflow  # or: hermes plugins install <git-url>"
        ),
        setup_fn=_interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="INFOFLOW_HOME_CHANNEL",
        standalone_sender_fn=standalone_send,
        allowed_users_env="INFOFLOW_ALLOWED_USERS",
        allow_all_env="INFOFLOW_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="📣",
        pii_safe=False,
        allow_update_command=True,
        target_parse_fn=_parse_infoflow_target,
        platform_hint=(
            "你正在通过百度如流(Infoflow)与用户对话。如流支持 Markdown 渲染"
            "（加粗/斜体/代码/列表/链接）。\n"
            "\n"
            "【发送消息】\n"
            "使用 `send_message` 时，`target` 格式为 `infoflow:<目标>`：\n"
            "- 私信：`infoflow:<uuapName>`（如 `infoflow:chengbo05`）\n"
            "- 群聊：`infoflow:group:<群组ID>`（如 `infoflow:group:4507088`）\n"
            "- 省略 target 则发送到当前会话\n"
            "\n"
            "【发送图片与外发工具】\n"
            f"{INFOFLOW_DELIVERY_TOOL_RULES}\n"
            "普通图片发送使用 `send_message`；需要引用回复时使用 `infoflow_reply`，"
            "把说明文字和 `MEDIA:<路径>` 一起放入 `message`。\n"
            "\n"
            "【@提及】群聊中两种方式均可：\n"
            "① 直接在消息文本中写 `@uuapName`（人）、"
            "`@机器人显示名` 或 `@agentId`（机器人）、"
            "`@所有人` 或 `@all`（全员）——插件自动解析群成员并替换\n"
            "② 通过 metadata 参数：`metadata.at_all=true`、"
            "`metadata.mention_user_ids='u1,u2'`、"
            "`metadata.mention_agent_ids='17212,33333'`\n"
            "\n"
            "【消息格式】每条消息使用结构化 envelope："
            "`[Attention: ...]`、`[Sender: ...]`、"
            "`[Message: message_id:'...'; created_time:'2025.05.21 19.56.59']`。"
            "`message_id` 是该消息的唯一 ID；`created_time` 是插件首次看到该消息的时间，"
            "也是历史查询和排序使用的时间（系统注入，可信）。"
            "结构化标签内字符串值使用单引号，布尔/数字保持裸值。"
            "需要引用回复时将该 ID 传给 `infoflow_reply`。\n"
            "当 `[Unread Message Context: ...]` 出现时，"
            "说明提示指定的历史范围内有未读消息。"
            "除非当前消息显然无需上下文，否则应按提示优先调用 "
            "`infoflow_get_message_history` 阅读参考上下文，再结合上下文判断和回复。\n"
            "\n"
            "【消息中的引用标签】\n"
            "当用户回复（reply）某条消息时，你收到的文本格式为：\n"
            "`<Quote message_id:'xxx'; sender:'user:alice'>被引用消息的内容</Quote> 用户的实际指令`\n"
            "其中 `message_id:'xxx'` 是**被引用消息的 ID**——"
            "可能是你(机器人)之前发的消息，也可能是其他人的消息。"
            "这个 ID 不是用户当前消息本身的 ID。"
            "`sender:'...'` 是被引用消息发送者的规范身份。\n"
            "\n"
            "【撤回消息】使用 `infoflow_recall_message` 撤回你(机器人)自己发出的消息。\n"
            "- 当用户 reply 了你的某条消息并说\"撤回\"时，"
            "从 `<Quote message_id:'xxx'; sender:'bot:...'>` 中提取该 ID 作为撤回目标——"
            "它就是你那条消息的 ID\n"
            "- **不要**把用户当前消息本身的 inbound message_id 当作撤回目标——"
            "那是用户发出的消息，你无权撤回\n"
            "- 撤回成功且用户只要求撤回时，最终输出单独一行 `NO_REPLY`，"
            "不要输出\"已撤回\"或\"撤回成功\"。若同一条用户消息还有其它任务，"
            "只回复其它任务结果，不要提及撤回已成功\n"
            "\n"
            "【引用回复】使用 `infoflow_reply` 引用某条消息并附带原文预览。"
            "若省略 `reply_to`，自动引用触发本轮对话的那条用户消息。"
            "引用回复图片时在 `message` 内包含 `MEDIA:<本地图片绝对路径>`，"
            "不要发送路径正文。\n"
            "\n"
            "【群成员】使用 `infoflow_get_group_members` 查询群成员列表（人类与机器人），"
            "便于在 @ 提及前确认 user_id、agent_id 或机器人显示名。\n"
            "\n"
            "【历史消息】使用 `infoflow_get_message_history` 查询聊天历史。"
            "成功返回 JSON 数组字符串，每项含 `time` 和 `content`；"
            "`time` 格式为 `YYYY.MM.DD HH.mm.ss`；"
            "`content` 与当前消息 envelope 格式一致。"
        ),
    )

    register_tool = getattr(ctx, "register_tool", None)
    if register_tool is not None:
        try:
            register_tool(
                name="infoflow_recall_message",
                toolset="hermes-infoflow",
                schema=RECALL_TOOL_SCHEMA,
                handler=make_recall_handler(),
                is_async=True,
                description="Recall a previously bot-sent Infoflow message (by id or count).",
                emoji="↩️",
            )
        except Exception as exc:
            gw_log().warning("[infoflow] failed to register recall tool: %s", exc)
        try:
            register_tool(
                name="infoflow_reply",
                toolset="hermes-infoflow",
                schema=REPLY_TOOL_SCHEMA,
                handler=make_reply_handler(),
                is_async=True,
                description=(
                    "Reply to or quote a specific Infoflow message with preview. "
                    "Automatically uses the current inbound message if reply_to is omitted."
                ),
                emoji="💬",
            )
        except Exception as exc:
            gw_log().warning("[infoflow] failed to register reply tool: %s", exc)
        try:
            register_tool(
                name="infoflow_get_group_members",
                toolset="hermes-infoflow",
                schema=GROUP_MEMBERS_TOOL_SCHEMA,
                handler=make_group_members_handler(),
                is_async=True,
                description=(
                    "Fetch Infoflow group chat member list (humans and bots)."
                ),
                emoji="👥",
            )
        except Exception as exc:
            gw_log().warning(
                "[infoflow] failed to register group members tool: %s", exc,
            )
        try:
            register_tool(
                name="infoflow_get_message_history",
                toolset="hermes-infoflow",
                schema=HISTORY_TOOL_SCHEMA,
                handler=make_history_handler(),
                is_async=True,
                description="Read Infoflow chat history for the current or authorized conversation.",
                emoji="🕘",
            )
        except Exception as exc:
            gw_log().warning(
                "[infoflow] failed to register history tool: %s", exc,
            )

    from .dashboard import make_plugin_hooks

    tracker = get_tracker()
    for hook_name, cb in make_plugin_hooks(tracker).items():
        try:
            ctx.register_hook(hook_name, cb)
        except Exception as exc:
            gw_log().warning(
                "[infoflow] failed to register dashboard hook %s: %s",
                hook_name, exc,
            )


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_WEBHOOK_PATH",
    "InfoflowAdapter",
    "MAX_MESSAGE_LENGTH",
    "register",
]
