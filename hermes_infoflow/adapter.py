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
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin as _urljoin
from urllib.parse import urlparse as _urlparse

_PROGRESS_LINE_RE = re.compile(r"^[┊\s]*[🔍⚙️💻🌐📁📝🧠✨]")
_GROUP_STATUS_REDIRECT_PREFIXES = (
    "⏳ Still working",
    "⚡ Interrupting current task",
    "⚠️ No activity",
    "⚠️ No response from provider",
    "⚠️ Gateway shutting down",
    "⚠️ Gateway restarting",
    "Gateway shutting down",
    "Gateway restarting",
    "💾 Self-improvement review:",
)
_GROUP_STATUS_REDIRECT_PATTERNS = (
    re.compile(r"^⚠️\s+.+\sstream\s+drop\b", re.IGNORECASE),
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


@dataclass(frozen=True)
class _UnreadMessageContext:
    history_before_count: int = 0
    effective_unread_count: int = 0


@dataclass
class _BusySteerReplyScope:
    chat_key: str
    message_ids: list[str]


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
    for pattern in _GROUP_STATUS_REDIRECT_PATTERNS:
        if pattern.search(t):
            return "stream drop"
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


def _format_group_status_ops_notice(
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


def _reply_to_from_inbound(message_id: str | None) -> tuple[list[dict[str, str]], str]:
    mid = str(message_id or "").strip()
    if not mid:
        return [], ""
    return [{"message_id": mid}], get_inbound_sender_id(mid)


def _truthy_metadata_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _metadata_list_values(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return [
        item.strip()
        for item in str(value or "").split(",")
        if item.strip()
    ]


def _metadata_with_group_mention(
    metadata: dict[str, Any] | None,
    *,
    mention_kind: str,
    mention_id: str,
) -> dict[str, Any] | None:
    mention_id = str(mention_id or "").strip()
    if not mention_id:
        return metadata
    key = (
        "mention_agent_ids"
        if mention_kind == "agent"
        else "mention_user_ids"
        if mention_kind == "user"
        else ""
    )
    if not key:
        return metadata
    updated = dict(metadata or {})
    values = _metadata_list_values(updated.get(key))
    if mention_id not in values:
        values.append(mention_id)
    updated[key] = values
    return updated


def _apply_automatic_reply_policy(
    *,
    kind: str,
    inbound_mid: str,
    original_reply_to: str | None,
    outbound_reply_to: str | None,
    metadata: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None, str]:
    """Drop Hermes' automatic reply anchor, preserving sender intent.

    Hermes final-response paths pass reply anchors into adapter.send().
    The first anchor is usually the current inbound message id; streaming
    continuations may use a previously sent bot message id. Infoflow's native
    reply support conflicts with Markdown, so the plugin treats these adapter
    anchors as platform strategy unless metadata explicitly opts back in.
    Explicit replies from Infoflow tools remain explicit because they do not
    enter this path.
    """
    inbound = str(inbound_mid or "").strip()
    original = str(original_reply_to or "").strip()
    if (
        not inbound
        or not original
        or _truthy_metadata_flag((metadata or {}).get("infoflow_explicit_reply"))
    ):
        return outbound_reply_to, metadata, ""

    redirected = str(outbound_reply_to or "").strip()
    if original == inbound and redirected and redirected != original:
        return outbound_reply_to, metadata, ""

    is_current_inbound_anchor = original == inbound
    reply_to_sender_id = (
        get_inbound_sender_id(inbound) if is_current_inbound_anchor else ""
    )
    if kind != "group":
        return None, metadata, reply_to_sender_id

    if not is_current_inbound_anchor:
        return None, metadata, ""

    mention_kind, mention_id = get_inbound_sender_mention(inbound)
    return (
        None,
        _metadata_with_group_mention(
            metadata,
            mention_kind=mention_kind,
            mention_id=mention_id,
        ),
        reply_to_sender_id,
    )


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
    CREATE_GROUP_TOOL_SCHEMA,
    DOWNLOAD_ATTACHMENT_TOOL_SCHEMA,
    FILE_DELIVERY_TOOL_SCHEMA,
    GROUP_MEMBERS_TOOL_SCHEMA,
    HISTORY_TOOL_SCHEMA,
    RECALL_TOOL_SCHEMA,
    SEND_MESSAGE_TOOL_SCHEMA,
    make_create_group_handler,
    make_download_attachment_handler,
    make_file_delivery_handler,
    make_group_members_handler,
    make_history_handler,
    make_recall_handler,
    make_send_message_handler,
)
from .inbound_files import inbound_file_to_raw_dict, render_attachments_block
from .itypes import IncomingMessage, reply_target_to_dict
from .llm_format import (
    DMAttention,
    GroupAttention,
    dm_attention_line,
    format_message_envelope,
    group_attention_line,
    sender_line,
    unread_message_context_line,
)
from .log_cleanup import cleanup_old_logs
from .media import IMAGE_LOAD_MAX_BYTES, prepare_infoflow_image_bytes
from .message_content import render_message_content
from .message_store import MessageStore
from .outbound import prepare_outbound_message
from .policy import (
    _DM_FORMAT_DOC,
    _GROUP_FORMAT_DOC,
    _GROUP_MENTION_RULES_DOC,
    _INFOFLOW_FIELD_DOC,
    _INFOFLOW_MESSAGE_FORMAT_DOC,
    _INFOFLOW_PERMISSION_SECURITY_DOC,
    _INFOFLOW_REFERENCE_RULES_DOC,
    _INFOFLOW_SESSION_HISTORY_DOC,
    _INFOFLOW_TOOL_RULES_DOC,
    GroupConfigOverride,
    GroupPolicy,
    PolicyDecision,
    normalize_reply_mode,
)
from .prompt_rules import INFOFLOW_DELIVERY_TOOL_RULES, infoflow_file_delivery_prompt
from .recall import (
    get_inbound_body,
    get_inbound_sender_id,
    get_inbound_sender_imid,
    get_inbound_sender_mention,
)
from .recall_silence import RecallSilenceTracker
from .send_service import InfoflowSendService
from .sent_store import SentMessageStore
from .serverapi import ServerAPI
from .settings import (
    DEFAULT_BODY_LIMIT_BYTES,
    DEFAULT_HOST,
    DEFAULT_IDLE_SESSION_RESET_SECONDS,
    DEFAULT_PORT,
    DEFAULT_WEBHOOK_PATH,
    GROUP_TARGET_RE,
    MAX_MESSAGE_LENGTH,
    _check_requirements,
    _env_enablement,
    _interactive_setup,
    _is_connected,
    _parse_infoflow_target,
    _read_account_settings,
    _validate_config,
    infoflow_admin_users_from_env,
    infoflow_op_channel_from_env,
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
_LOG_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60


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
        # Hermes injects this back-reference only when the adapter declares
        # the attribute. It is needed for Infoflow-specific session rotation.
        self.gateway_runner = None

        self._settings = _read_account_settings(config)
        self._admin_users = infoflow_admin_users_from_env()
        self._admin_uid = ",".join(self._admin_users)
        self._op_channel = infoflow_op_channel_from_env()

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
        self._serverapi.set_image_loader(self._load_image_bytes)
        self._send_service = InfoflowSendService(
            serverapi=self._serverapi,
            message_store=self._message_store,
            inbound_body_lookup=get_inbound_body,
            inbound_sender_imid_lookup=get_inbound_sender_imid,
        )

        # ── Bot (business logic) ──────────────────────────────────────
        self._bot = Bot(
            settings=self._settings,
            policy=self._policy,
            serverapi=self._serverapi,
            send_service=self._send_service,
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
        self._log_cleanup_task: asyncio.Task[Any] | None = None
        self._infoflow_original_busy_session_handler = None
        self._busy_steer_reaction_by_session: dict[str, Any] = {}
        self._busy_steer_reply_scope_by_session: dict[
            str,
            _BusySteerReplyScope,
        ] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Infoflow"

    def set_busy_session_handler(
        self,
        handler: Callable[[Any, str], Awaitable[bool]] | None,
    ) -> None:
        """Wrap Hermes' busy handler so Infoflow text follow-ups can steer.

        The wrapper is deliberately narrow: only ordinary Infoflow text messages
        in isolated sessions steer. Everything else falls back to Hermes' normal
        busy path.
        """
        self._infoflow_original_busy_session_handler = handler
        if handler is None:
            return super().set_busy_session_handler(None)
        if getattr(handler, "_infoflow_busy_steer_wrapper", False) is True:
            return super().set_busy_session_handler(handler)

        async def _wrapped_busy_session_handler(event: Any, session_key: str) -> bool:
            return await self._handle_infoflow_busy_message(
                event,
                session_key,
                handler,
            )

        setattr(_wrapped_busy_session_handler, "_infoflow_busy_steer_wrapper", True)
        return super().set_busy_session_handler(_wrapped_busy_session_handler)

    async def _call_original_busy_session_handler(
        self,
        handler: Callable[[Any, str], Awaitable[bool]] | None,
        event: Any,
        session_key: str,
    ) -> bool:
        if handler is None:
            return False
        return bool(await handler(event, session_key))

    @staticmethod
    def _current_processing_reaction_token() -> Any | None:
        from .bot import _reaction_promise_cv

        return _reaction_promise_cv.get(None)

    def _remember_busy_steer_reaction(self, session_key: str) -> Any | None:
        token = self._current_processing_reaction_token()
        if token is not None and session_key:
            self._busy_steer_reaction_by_session[session_key] = token
        return token

    def _remember_busy_steer_reply_scope(self, session_key: str, event: Any) -> None:
        message_id = self._event_message_id(event)
        chat_key, _group_id, _dm_user = self._chat_key_for_event(event)
        if not session_key or not message_id or not chat_key:
            return
        scope = self._busy_steer_reply_scope_by_session.get(session_key)
        if scope is None or scope.chat_key != chat_key:
            self._busy_steer_reply_scope_by_session[session_key] = (
                _BusySteerReplyScope(chat_key=chat_key, message_ids=[message_id])
            )
            return
        if message_id not in scope.message_ids:
            scope.message_ids.append(message_id)
            if len(scope.message_ids) > 20:
                del scope.message_ids[:-20]

    async def _finish_busy_steer_reaction_for_event(
        self,
        event: Any,
        outcome_label: str,
    ) -> None:
        session_key = self._llm_context_key_for_event(event)
        if not session_key:
            return
        token = self._busy_steer_reaction_by_session.pop(session_key, None)
        self._busy_steer_reply_scope_by_session.pop(session_key, None)
        if token is not None:
            finish_token = getattr(self._bot, "finish_processing_reaction_token", None)
            if callable(finish_token):
                await finish_token(
                    token,
                    reason=f"busy_steer_parent_{outcome_label}",
                )

    @staticmethod
    def _config_bool(raw: Any, *, default: bool) -> bool:
        if isinstance(raw, bool):
            return raw
        if raw is None or raw == "":
            return default
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}

    def _busy_steer_candidate(
        self,
        event: Any,
        session_key: str,
    ) -> tuple[bool, str, Any | None]:
        if not self._config_bool(
            self._settings.get("busy_text_steer_enabled"),
            default=True,
        ):
            return False, "disabled", None

        raw_message = getattr(event, "raw_message", None)
        if not isinstance(raw_message, dict) or not raw_message.get(
            "infoflow_standard_message"
        ):
            return False, "non_infoflow_standard_message", None

        if getattr(event, "message_type", None) != MessageType.TEXT:
            return False, "non_text_message", None
        if getattr(event, "media_urls", None):
            return False, "media_message", None

        text = str(getattr(event, "text", "") or "").strip()
        if not text:
            return False, "empty_text", None

        get_command = getattr(event, "get_command", None)
        if callable(get_command):
            with contextlib.suppress(Exception):
                if get_command():
                    return False, "command", None

        source = getattr(event, "source", None)
        if source is None:
            return False, "missing_source", None

        chat_type = str(getattr(source, "chat_type", "") or "")
        if chat_type != "dm":
            if not str(getattr(source, "user_id", "") or "").strip():
                return False, "missing_group_user_id", None
            thread_id = str(getattr(source, "thread_id", "") or "").strip()
            if thread_id:
                isolated = self._config_bool(
                    self.config.extra.get("thread_sessions_per_user", False),
                    default=False,
                )
                if not isolated:
                    return False, "shared_thread_session", None
            else:
                isolated = self._config_bool(
                    self.config.extra.get("group_sessions_per_user", True),
                    default=True,
                )
                if not isolated:
                    return False, "shared_group_session", None

        gateway = getattr(self, "gateway_runner", None)
        if gateway is None:
            return False, "missing_gateway_runner", None
        if bool(getattr(gateway, "_draining", False)):
            return False, "gateway_draining", None

        authorizer = getattr(gateway, "_is_user_authorized", None)
        if not callable(authorizer):
            return False, "missing_authorizer", None
        try:
            if not bool(authorizer(source)):
                return False, "unauthorized", None
        except Exception as exc:
            gw_log().warning(
                "[infoflow:busy-steer] auth check failed session_key=%s mid=%s: %s",
                session_key,
                self._event_message_id(event) or "-",
                exc,
                exc_info=True,
            )
            return False, "auth_error", None

        running_agents = getattr(gateway, "_running_agents", None)
        if not isinstance(running_agents, dict):
            return False, "missing_running_agents", None
        running_agent = running_agents.get(session_key)
        steer = getattr(running_agent, "steer", None)
        if not callable(steer):
            return False, "missing_running_agent_steer", None

        return True, "", running_agent

    async def _handle_infoflow_busy_message(
        self,
        event: Any,
        session_key: str,
        original_handler: Callable[[Any, str], Awaitable[bool]] | None,
    ) -> bool:
        can_steer, reason, running_agent = self._busy_steer_candidate(
            event,
            session_key,
        )
        if not can_steer:
            gw_log().debug(
                "[infoflow:busy-steer] fallback session_key=%s mid=%s reason=%s",
                session_key,
                self._event_message_id(event) or "-",
                reason,
            )
            if reason in {"unauthorized", "auth_error"} and original_handler is None:
                return True
            return await self._call_original_busy_session_handler(
                original_handler,
                event,
                session_key,
            )

        original_text = str(getattr(event, "text", "") or "")
        raw_message = getattr(event, "raw_message", None)
        raw_snapshot = dict(raw_message) if isinstance(raw_message, dict) else None
        accepted = False
        tokens = self._bind_processing_context(event)
        try:
            await self._run_processing_hook("on_processing_start", event)
            steer_text = str(getattr(event, "text", "") or "").strip()
            try:
                accepted = bool(running_agent.steer(steer_text))
            except Exception as exc:
                gw_log().warning(
                    "[infoflow:busy-steer] steer failed session_key=%s mid=%s: %s",
                    session_key,
                    self._event_message_id(event) or "-",
                    exc,
                    exc_info=True,
                )
                accepted = False

            if accepted:
                reaction_token = self._remember_busy_steer_reaction(session_key)
                self._remember_busy_steer_reply_scope(session_key, event)
                try:
                    self._mark_event_llm_visible(event)
                except Exception as exc:
                    gw_log().warning(
                        "[infoflow:busy-steer] failed to mark LLM-visible "
                        "session_key=%s mid=%s: %s",
                        session_key,
                        self._event_message_id(event) or "-",
                        exc,
                        exc_info=True,
                    )
                gw_log().info(
                    "[infoflow:busy-steer] session_key=%s mid=%s chat_type=%s "
                    "accepted=true reaction_token=%s",
                    session_key,
                    self._event_message_id(event) or "-",
                    str(getattr(getattr(event, "source", None), "chat_type", "") or ""),
                    bool(reaction_token),
                )
                return True
        finally:
            self._reset_processing_context(tokens)

        event.text = original_text
        if raw_snapshot is not None and isinstance(getattr(event, "raw_message", None), dict):
            event.raw_message.clear()
            event.raw_message.update(raw_snapshot)
        gw_log().info(
            "[infoflow:busy-steer] session_key=%s mid=%s accepted=false fallback=true",
            session_key,
            self._event_message_id(event) or "-",
        )
        return await self._call_original_busy_session_handler(
            original_handler,
            event,
            session_key,
        )

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

        self._run_log_cleanup_once()

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
        self._start_log_cleanup_task()
        self._mark_connected()
        logger.info(
            "[infoflow] Webhook listening on %s:%d%s",
            self._host, self._port, self._webhook_path,
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        await self._stop_log_cleanup_task()
        with contextlib.suppress(Exception):
            await self._webhook_server.stop()
        await self._close_http_session()
        self._mark_disconnected()
        gw_log().info("[infoflow] Disconnected")

    def _run_log_cleanup_once(self) -> None:
        try:
            removed_logs = cleanup_old_logs(settings=self._settings)
            if removed_logs:
                gw_log().info(
                    "[infoflow:log_cleanup] removed=%d retention_days=14",
                    len(removed_logs),
                )
        except Exception as exc:
            gw_log().warning("[infoflow:log_cleanup] failed: %s", exc)

    def _start_log_cleanup_task(self) -> None:
        task = self._log_cleanup_task
        if task is not None and not task.done():
            return
        self._log_cleanup_task = asyncio.create_task(self._periodic_log_cleanup())

    async def _stop_log_cleanup_task(self) -> None:
        task = self._log_cleanup_task
        self._log_cleanup_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _periodic_log_cleanup(self) -> None:
        while self._running:
            await asyncio.sleep(_LOG_CLEANUP_INTERVAL_SECONDS)
            if self._running:
                self._run_log_cleanup_once()

    async def _close_http_session(self) -> None:
        if self._http_session is not None:
            with contextlib.suppress(Exception):
                await self._http_session.close()
            self._http_session = None
            self._serverapi.http_session = None

    async def _broadcast_ops_notice(
        self,
        text: str,
        *,
        session: aiohttp.ClientSession | None = None,
        extra: dict[str, Any] | None = None,
        log_context: str = "ops notice",
    ) -> bool:
        """Best-effort send to the configured operation channel."""
        if not self._op_channel:
            gw_log().info(
                "[infoflow] no INFOFLOW_OP_CHANNEL configured for %s",
                log_context,
            )
            return False

        from .bot import _reaction_promise_cv

        target = self._op_channel
        result_success = False
        result_message_id = ""
        result_error = ""
        token = _reaction_promise_cv.set(None)
        try:
            kind, group_id, dm_user = self._parse_target(target)
            try:
                result = await self._bot.send_message(
                    group_id=(
                        str(group_id)
                        if kind == "group" and group_id is not None
                        else None
                    ),
                    dm_user_id=dm_user or None,
                    text=text,
                    session=session,
                )
                result_success = result.success
                result_message_id = result.message_id
                result_error = result.error
            except Exception as exc:
                result_error = str(exc)
                gw_log().warning(
                    "[infoflow] failed to send %s target=%s error=%s",
                    log_context,
                    target,
                    result_error,
                    exc_info=True,
                )

            event_extra = {
                "type": "text",
                "chars": len(text or ""),
                "success": result_success,
                "message_id": result_message_id,
                "error": result_error,
                "preview": (text or "")[:200],
                "ops_notice": True,
                "op_channel": target,
            }
            if extra:
                event_extra.update(extra)
            with contextlib.suppress(Exception):
                self._push_infoflow_event(
                    None,
                    kind="outbound.infoflow",
                    chat_id=target,
                    extra=event_extra,
                )
        finally:
            _reaction_promise_cv.reset(token)
        return result_success

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
            attachments_block=render_attachments_block(list(getattr(msg, "files", []) or [])),
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

    def _session_key_for_dm_record(self, dm_record: Any, message_id: str) -> str:
        dm_user = raw_id_from_key(getattr(dm_record, "peer", "")) or str(
            getattr(dm_record, "dm_user_id", "") or ""
        )
        if not dm_user:
            return ""
        sender_id = raw_id_from_key(getattr(dm_record, "sender", "")) or dm_user
        source = self.build_source(
            chat_id=dm_user,
            chat_name=dm_user,
            chat_type="dm",
            user_id=sender_id,
            user_name=sender_id,
            message_id=message_id,
        )
        return self._llm_context_key_for_event(SimpleNamespace(source=source))

    def _session_key_for_group_record(self, group_record: Any, message_id: str) -> str:
        group_id = str(getattr(group_record, "group_id", "") or "").strip()
        sender_id = raw_id_from_key(getattr(group_record, "sender", "")) or str(
            getattr(group_record, "sender_id", "") or ""
        )
        if not group_id or not sender_id:
            return ""
        source = self.build_source(
            chat_id=f"group:{group_id}",
            chat_name=f"group:{group_id}",
            chat_type="group",
            user_id=sender_id,
            user_name=sender_id,
            message_id=message_id,
        )
        return self._llm_context_key_for_event(SimpleNamespace(source=source))

    def _session_key_for_stored_message_id(
        self,
        message_id: str,
        chat_id: str = "",
    ) -> str:
        mid = str(message_id or "").strip()
        if not mid:
            return ""

        if chat_id:
            kind, group_id, dm_user = self._parse_target(chat_id)
            if kind == "group" and group_id is not None:
                group_record = self._message_store.find_group(mid)
                if group_record is None:
                    return ""
                if str(getattr(group_record, "group_id", "") or "") != str(group_id):
                    return ""
                return self._session_key_for_group_record(group_record, mid)

            dm_record = self._message_store.find_dm(mid)
            if dm_record is None:
                return ""
            record_dm_user = raw_id_from_key(getattr(dm_record, "peer", "")) or str(
                getattr(dm_record, "dm_user_id", "") or ""
            )
            if dm_user and record_dm_user and record_dm_user != dm_user:
                return ""
            return self._session_key_for_dm_record(dm_record, mid)

        dm_record = self._message_store.find_dm(mid)
        if dm_record is not None:
            return self._session_key_for_dm_record(dm_record, mid)

        group_record = self._message_store.find_group(mid)
        if group_record is not None:
            return self._session_key_for_group_record(group_record, mid)

        return ""

    def _resolve_busy_steer_outbound_reply_to(
        self,
        chat_id: str,
        reply_to: str | None,
    ) -> str | None:
        original_reply_to = str(reply_to or "").strip()
        if not original_reply_to:
            return None

        context_inbound_mid = str(_inbound_mid.get("") or "").strip()
        if not context_inbound_mid or original_reply_to != context_inbound_mid:
            return original_reply_to

        session_key = self._session_key_for_stored_message_id(
            original_reply_to,
            chat_id,
        )
        if not session_key:
            return original_reply_to
        scope = self._busy_steer_reply_scope_by_session.get(session_key)
        if scope is None:
            return original_reply_to

        _kind, group_id, dm_user = self._parse_target(chat_id)
        target_chat_key = self._chat_key_for_target(
            str(group_id) if group_id is not None else None,
            dm_user or None,
        )
        if target_chat_key != scope.chat_key:
            return original_reply_to

        scoped_message_ids = [mid for mid in scope.message_ids if mid]
        if len(scoped_message_ids) == 1:
            scoped_reply_to = scoped_message_ids[0]
            if scoped_reply_to != original_reply_to:
                gw_log().info(
                    "[infoflow:busy-steer] redirect auto reply anchor "
                    "session_key=%s from_mid=%s to_mid=%s",
                    session_key,
                    original_reply_to,
                    scoped_reply_to,
                )
            return scoped_reply_to

        if len(scoped_message_ids) > 1:
            gw_log().info(
                "[infoflow:busy-steer] suppress auto reply anchor "
                "session_key=%s from_mid=%s steer_count=%d",
                session_key,
                original_reply_to,
                len(scoped_message_ids),
            )
            return None

        return original_reply_to

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
        state = self._message_store.get_llm_context_state(context_key)
        if state is not None and state.chat_key == chat_key:
            current_pos = (int(current.created_time or 0), str(current.message_id or ""))
            visible_pos = (
                int(state.last_llm_visible_created_time or 0),
                str(state.last_llm_visible_message_id or ""),
            )
            if current_pos <= visible_pos:
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
            _INFOFLOW_FIELD_DOC,
            _INFOFLOW_PERMISSION_SECURITY_DOC,
            _INFOFLOW_SESSION_HISTORY_DOC,
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
            "files": [
                inbound_file_to_raw_dict(file)
                for file in list(getattr(msg, "files", []) or [])
            ],
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
        """Inject per-event session-boundary and unread-context metadata."""
        raw_message = getattr(event, "raw_message", None)
        if not isinstance(raw_message, dict):
            return
        if not raw_message.get("infoflow_standard_message"):
            return
        if raw_message.get("infoflow_unread_message_context_applied"):
            return

        session_boundary_line = await self._maybe_apply_idle_session_reset(event)
        unread_context = self._unread_message_context_for_event(event)
        count = unread_context.history_before_count
        raw_message["infoflow_unread_message_context_count"] = count
        raw_message["infoflow_unread_message_context_before_count"] = count
        raw_message["infoflow_effective_unread_message_count"] = (
            unread_context.effective_unread_count
        )
        raw_message["infoflow_unread_message_context_applied"] = True
        text = str(getattr(event, "text", "") or "")
        prefix_lines = []
        if session_boundary_line:
            prefix_lines.append(session_boundary_line)
        if count > 0:
            prefix_lines.append(unread_message_context_line(count))
        if prefix_lines:
            text = "\n".join([*prefix_lines, text])
            event.text = text
        gw_log().info(
            "[iflow:user_message] mid=%s idle_reset=%s "
            "unread_context_before_count=%d effective_unread_count=%d "
            "len=%d text=\n%s",
            self._event_message_id(event) or "-",
            bool(session_boundary_line),
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
            try:
                raw_message = getattr(event, "raw_message", None)
                if (
                    outcome_label == "success"
                    and isinstance(raw_message, dict)
                    and raw_message.get("infoflow_standard_message")
                ):
                    self._mark_event_llm_visible(event)
            finally:
                await self._finish_busy_steer_reaction_for_event(
                    event,
                    outcome_label,
                )

    # ------------------------------------------------------------------
    # Infoflow idle session reset
    # ------------------------------------------------------------------

    def _idle_session_reset_seconds(self) -> int:
        raw = self._settings.get(
            "idle_session_reset_seconds",
            DEFAULT_IDLE_SESSION_RESET_SECONDS,
        )
        try:
            return int(raw)
        except (TypeError, ValueError):
            return DEFAULT_IDLE_SESSION_RESET_SECONDS

    def _resolve_gateway_session_key(self, event: Any) -> str:
        source = getattr(event, "source", None)
        if source is None:
            return ""
        gateway = getattr(self, "gateway_runner", None)
        if gateway is not None:
            resolver = getattr(gateway, "_session_key_for_source", None)
            if callable(resolver):
                try:
                    session_key = resolver(source)
                    if isinstance(session_key, str) and session_key:
                        return session_key
                except Exception:
                    pass
            session_store = getattr(gateway, "session_store", None)
            generator = getattr(session_store, "_generate_session_key", None)
            if callable(generator):
                try:
                    session_key = generator(source)
                    if isinstance(session_key, str) and session_key:
                        return session_key
                except Exception:
                    pass
        return self._llm_context_key_for_event(event)

    @staticmethod
    def _seconds_since_session_update(entry: Any) -> float | None:
        updated_at = getattr(entry, "updated_at", None)
        if isinstance(updated_at, str):
            with contextlib.suppress(ValueError):
                updated_at = datetime.fromisoformat(updated_at)
        if not isinstance(updated_at, datetime):
            return None
        now = datetime.now(updated_at.tzinfo) if updated_at.tzinfo else datetime.now()
        return max(0.0, (now - updated_at).total_seconds())

    @staticmethod
    def _get_session_entry(session_store: Any, session_key: str) -> Any | None:
        ensure_loaded = getattr(session_store, "_ensure_loaded", None)
        if callable(ensure_loaded):
            with contextlib.suppress(Exception):
                ensure_loaded()
        entries = getattr(session_store, "_entries", None)
        if isinstance(entries, dict):
            return entries.get(session_key)
        return None

    @staticmethod
    def _has_running_work(gateway: Any, session_store: Any, session_key: str) -> bool:
        running_agents = getattr(gateway, "_running_agents", None)
        if isinstance(running_agents, dict) and session_key in running_agents:
            return True
        has_active_processes = getattr(session_store, "_has_active_processes_fn", None)
        if callable(has_active_processes):
            try:
                return bool(has_active_processes(session_key))
            except Exception:
                gw_log().warning(
                    "[infoflow] active process check failed for %s; skip idle reset",
                    session_key,
                    exc_info=True,
                )
                return True
        return False

    @staticmethod
    def _cleanup_cached_agent(gateway: Any, session_key: str) -> None:
        cached_agent = None
        cache_lock = getattr(gateway, "_agent_cache_lock", None)
        cache = getattr(gateway, "_agent_cache", None)
        if cache_lock is not None and cache is not None:
            try:
                with cache_lock:
                    cached = cache.get(session_key)
                    cached_agent = (
                        cached[0]
                        if isinstance(cached, tuple)
                        else cached if cached else None
                    )
            except Exception:
                cached_agent = None
        cleanup = getattr(gateway, "_cleanup_agent_resources", None)
        if cached_agent is not None and callable(cleanup):
            with contextlib.suppress(Exception):
                cleanup(cached_agent)
        evict = getattr(gateway, "_evict_cached_agent", None)
        if callable(evict):
            with contextlib.suppress(Exception):
                evict(session_key)

    @staticmethod
    def _clear_gateway_session_state(gateway: Any, session_key: str) -> None:
        model_overrides = getattr(gateway, "_session_model_overrides", None)
        if isinstance(model_overrides, dict):
            model_overrides.pop(session_key, None)
        reasoning_clear = getattr(gateway, "_set_session_reasoning_override", None)
        if callable(reasoning_clear):
            with contextlib.suppress(Exception):
                reasoning_clear(session_key, None)
        pending_notes = getattr(gateway, "_pending_model_notes", None)
        if isinstance(pending_notes, dict):
            pending_notes.pop(session_key, None)
        security_clear = getattr(gateway, "_clear_session_boundary_security_state", None)
        if callable(security_clear):
            with contextlib.suppress(Exception):
                security_clear(session_key)
        invalidate = getattr(gateway, "_invalidate_session_run_generation", None)
        if callable(invalidate):
            with contextlib.suppress(Exception):
                invalidate(session_key, reason="infoflow_idle_reset")
        queued_events = getattr(gateway, "_queued_events", None)
        if isinstance(queued_events, dict):
            queued_events.pop(session_key, None)

    async def _emit_idle_session_reset_hooks(
        self,
        gateway: Any,
        source: Any,
        session_key: str,
        old_session_id: str,
        new_session_id: str,
    ) -> None:
        platform_value = "infoflow"
        platform = getattr(source, "platform", None)
        if platform is not None:
            platform_value = str(getattr(platform, "value", platform) or "infoflow")
        user_id = getattr(source, "user_id", None)

        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook

            if old_session_id:
                _invoke_hook(
                    "on_session_finalize",
                    session_id=old_session_id,
                    platform=platform_value,
                )
        except Exception:
            pass

        hooks = getattr(gateway, "hooks", None)
        emit = getattr(hooks, "emit", None)
        if callable(emit):
            for name in ("session:end", "session:reset"):
                try:
                    result = emit(
                        name,
                        {
                            "platform": platform_value,
                            "user_id": user_id,
                            "session_key": session_key,
                        },
                    )
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass

        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook

            if new_session_id:
                _invoke_hook(
                    "on_session_reset",
                    session_id=new_session_id,
                    platform=platform_value,
                )
        except Exception:
            pass

    @staticmethod
    def _session_boundary_line(idle_seconds: int) -> str:
        return (
            "[Session Boundary: 该 Infoflow 会话因超过 "
            f"{idle_seconds} 秒无新的 LLM 会话处理，已切换为新的 LLM session。"
            "之前聊天历史没有放入当前上下文；如果当前问题依赖之前内容，"
            "请调用 infoflow_get_message_history，使用当前 Message 标签中的 "
            "message_id 作为锚点查询历史。若当前消息可独立回答，请直接处理。]"
        )

    async def _maybe_apply_idle_session_reset(self, event: Any) -> str:
        raw_message = getattr(event, "raw_message", None)
        if not isinstance(raw_message, dict):
            return ""
        if raw_message.get("infoflow_idle_session_reset_applied"):
            return ""

        idle_seconds = self._idle_session_reset_seconds()
        if idle_seconds <= 0:
            return ""

        gateway = getattr(self, "gateway_runner", None)
        session_store = getattr(gateway, "session_store", None) if gateway else None
        if gateway is None or session_store is None:
            return ""

        session_key = self._resolve_gateway_session_key(event)
        if not session_key:
            return ""
        entry = self._get_session_entry(session_store, session_key)
        if entry is None:
            return ""

        elapsed = self._seconds_since_session_update(entry)
        if elapsed is None or elapsed <= idle_seconds:
            return ""
        if self._has_running_work(gateway, session_store, session_key):
            return ""

        old_session_id = str(getattr(entry, "session_id", "") or "")
        try:
            self._cleanup_cached_agent(gateway, session_key)
            reset = getattr(session_store, "reset_session", None)
            if not callable(reset):
                return ""
            new_entry = reset(session_key)
            if new_entry is None:
                return ""
            new_session_id = str(getattr(new_entry, "session_id", "") or "")
            self._clear_gateway_session_state(gateway, session_key)
            await self._emit_idle_session_reset_hooks(
                gateway,
                getattr(event, "source", None),
                session_key,
                old_session_id,
                new_session_id,
            )
        except Exception as exc:
            gw_log().warning(
                "[infoflow] idle session reset failed for %s: %s",
                session_key,
                exc,
                exc_info=True,
            )
            return ""

        raw_message["infoflow_idle_session_reset_applied"] = True
        raw_message["infoflow_idle_session_reset_seconds"] = idle_seconds
        raw_message["infoflow_idle_session_reset_old_session_id"] = old_session_id
        raw_message["infoflow_idle_session_reset_new_session_id"] = new_session_id
        gw_log().info(
            "[infoflow] idle session reset applied session_key=%s old=%s new=%s "
            "idle_seconds=%d elapsed_seconds=%.1f",
            session_key,
            old_session_id or "-",
            new_session_id or "-",
            idle_seconds,
            elapsed,
        )
        return self._session_boundary_line(idle_seconds)

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
        if chat_id.startswith("dm:user:"):
            chat_id = chat_id[len("dm:user:"):]
        elif chat_id.startswith("user:"):
            chat_id = chat_id[len("user:"):]
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
                redirected = await self._broadcast_group_status_to_ops(
                    group_id=str(group_id),
                    content=content,
                    status_kind=status_kind,
                    session=session,
                )
            gw_log().info(
                "[iflow:send] suppressed group status target=%s kind=%s redirected_ops=%s",
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
                    "redirected_to_ops": redirected,
                    "preview": (content or "")[:200],
                },
            )
            return SendResult(success=True)

        original_reply_to = reply_to
        outbound_reply_to = self._resolve_busy_steer_outbound_reply_to(
            chat_id,
            reply_to,
        )
        outbound_reply_to, metadata, auto_reply_sender_id = _apply_automatic_reply_policy(
            kind=kind,
            inbound_mid=inbound_mid,
            original_reply_to=original_reply_to,
            outbound_reply_to=outbound_reply_to,
            metadata=metadata,
        )

        # Build normalized reply_to from inbound context. Preview enrichment
        # happens in InfoflowSendService before ServerAPI sees the intent.
        normalized_reply_to, reply_to_sender_id = _reply_to_from_inbound(
            outbound_reply_to
        )
        if auto_reply_sender_id:
            reply_to_sender_id = auto_reply_sender_id

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
            reply_to=normalized_reply_to or None,
            reply_to_sender_id=reply_to_sender_id,
            options=options,
            session=session,
            reaction_message_id=original_reply_to,
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

    async def _broadcast_group_status_to_ops(
        self,
        *,
        group_id: str,
        content: str,
        status_kind: str,
        session: aiohttp.ClientSession | None = None,
    ) -> bool:
        """Forward suppressed Hermes runtime status to operation channels."""
        notice = _format_group_status_ops_notice(
            group_id=group_id,
            content=content,
            status_kind=status_kind,
        )
        return await self._broadcast_ops_notice(
            notice,
            session=session,
            extra={
                "ops_status_broadcast": True,
                "source_group_id": group_id,
            },
            log_context=f"group status notice group={group_id}",
        )

    async def _redirect_group_status_to_admin(
        self,
        *,
        group_id: str,
        content: str,
        status_kind: str,
        session: aiohttp.ClientSession | None = None,
    ) -> bool:
        """Backward-compatible alias; status notices now go to ops channels."""
        return await self._broadcast_group_status_to_ops(
            group_id=group_id,
            content=content,
            status_kind=status_kind,
            session=session,
        )

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
        inbound_mid = self._current_inbound_mid()
        try:
            raw_image_bytes = await self._load_image_bytes(image_url)
            prepared_image = prepare_infoflow_image_bytes(raw_image_bytes)
        except _ImageLoadError as exc:
            return SendResult(success=False, error=str(exc), retryable=False)

        original_reply_to = reply_to
        outbound_reply_to = self._resolve_busy_steer_outbound_reply_to(
            chat_id,
            reply_to,
        )
        outbound_reply_to, metadata, auto_reply_sender_id = _apply_automatic_reply_policy(
            kind=kind,
            inbound_mid=inbound_mid,
            original_reply_to=original_reply_to,
            outbound_reply_to=outbound_reply_to,
            metadata=metadata,
        )
        normalized_reply_to, reply_to_sender_id = _reply_to_from_inbound(
            outbound_reply_to
        )
        if auto_reply_sender_id:
            reply_to_sender_id = auto_reply_sender_id

        caption_text, options = await prepare_outbound_message(
            caption or "",
            group_id=str(group_id) if group_id is not None else None,
            metadata=metadata,
            get_group_members=self._serverapi.get_group_members,
            session=session,
            bot_agent_id=self._settings.get("app_agent_id"),
        )

        bot_result = await self._bot.send_image(
            group_id=str(group_id) if group_id is not None else None,
            dm_user_id=dm_user or None,
            image_bytes=prepared_image.data,
            caption=caption_text,
            reply_to=normalized_reply_to or None,
            reply_to_sender_id=reply_to_sender_id,
            options=options,
            session=session,
            reaction_message_id=original_reply_to,
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
        cron_deliver_env_var="INFOFLOW_OP_CHANNEL",
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
            "当前会话普通文字回复优先直接输出最终回复。"
            "需要指定 target、跨会话发送、发送链接、"
            "群聊 @ 或引用消息时，使用 `infoflow_send_message`。"
            "分享本地图片或文件前，先调用 `file_delivery` 获取 URL；"
            "不需要 Markdown 排版、只发送本地图片时使用 `image_paths`。\n"
            "`infoflow_send_message.target` 必填：\n"
            "- 私信：`infoflow:<uuapName>` 或 `user:<uuapName>`（如 `infoflow:chengbo05`）\n"
            "- 群聊：`infoflow:group:<群组ID>`（如 `infoflow:group:4507088`）\n"
            "- `bot:<agentId>` 不能作为私聊发送目标；机器人只能在群聊 @ 字段中使用 agentId\n"
            "- 不要用裸 `infoflow` 作为当前会话目标；裸平台名会路由到 home channel\n"
            "\n"
            "【发送参数】\n"
            "`message` 是正文。只发送链接、群聊 @ 或引用时，"
            "`message` 可为空字符串。\n"
            "`message` 支持 Markdown 语法；普通正文保持 `format=auto` 即可。\n"
            "分享本地图片或文件时，先交给 `file_delivery` 获取 URL；"
            "不要把本地路径直接写入正文。\n"
            "不需要 Markdown 排版、只发送本地图片时，使用 `infoflow_send_message.image_paths`。\n"
            "HTTP/HTTPS 图片 URL（包括内网 URL）不是本地路径；jpg/png/gif/webp "
            "需要以内联方式显示时，保持 `format=auto` 或使用 `format=markdown`，"
            "并在 `message` 中写 `![图片说明](URL)`；"
            "其它文件 URL 使用普通链接。\n"
            "`links` 支持 URL、`[可见文字](URL)`、`{href, label}`，"
            "可单独发送或与正文、群聊 @、引用组合。\n"
            "`format` 默认 `auto`，优先以 Markdown 发送；`markdown` 表示希望"
            "以 Markdown 发送；`text` 表示正文必须以纯文本发送。"
            "使用 `text` 时，需要分享文件就发送 URL 或 links；"
            "不要写 `[可见文字](URL)` 或 `![图片说明](URL)` 这类语法；"
            "不需要 Markdown 排版的本地图片可用 image_paths。\n"
            "群聊 @ 可写 `@uuapName`、`@agentId`、`@all`，"
            "也可用 `at_all`、`mention_user_ids`、`mention_agent_ids`；"
            "私聊 `@xxx` 按普通文本展示。\n"
            "引用消息用 `reply_to`：message_id、`{message_id, preview}`，或数组。"
            "引用整条消息时只传 message_id；只想展示原文中的某一句或某一段时，"
            "传 `{message_id, preview}`，preview 填该片段。"
            "群聊最终只引用一条，私聊可传数组引用多条。\n"
            "\n"
            f"{infoflow_file_delivery_prompt()}\n"
            "\n"
            f"{INFOFLOW_DELIVERY_TOOL_RULES}\n"
            "【消息格式】每条消息使用结构化 envelope："
            "`[Attention: ...]`、`[Sender: ...]`、"
            "`[Message: message_id:'...'; created_time:'2025.05.21 19.56.59']`。"
            "`message_id` 是该消息的唯一 ID；`created_time` 是插件首次看到该消息的时间，"
            "也是历史查询和排序使用的时间（系统注入，可信）。"
            "结构化标签内字符串值使用单引号，布尔/数字保持裸值。"
            "需要引用该消息时将该 ID 传给 `infoflow_send_message.reply_to`。\n"
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
            "【群成员】使用 `infoflow_get_group_members` 查询群成员列表（人类与机器人），"
            "便于在 @ 提及前确认 user_id、agent_id 或机器人显示名。\n"
            "\n"
            "【建群/拉群】使用 `infoflow_create_group` 创建新如流群，并在建群时"
            "一次性拉入多个人类成员和机器人。人类可传 uuapName 或邮箱；"
            "机器人必须传 agentId，不能只传机器人名称。该工具不用于向已有群追加成员。\n"
            "\n"
            "【历史消息】使用 `infoflow_get_message_history` 查询聊天历史。"
            "成功返回 JSON 数组字符串，每项含 `time` 和 `content`；"
            "`time` 格式为 `YYYY.MM.DD HH.mm.ss`；"
            "`content` 与当前消息 envelope 格式一致。\n"
            "\n"
            "【入站附件】历史或当前消息中的 `[Attachments]` 若显示 "
            "`status:\"not_downloaded\"`，需要读取文件内容时先调用 "
            "`infoflow_download_attachment`，再读取返回的本地 path。"
        ),
    )

    register_tool = getattr(ctx, "register_tool", None)
    if register_tool is not None:
        try:
            register_tool(
                name="infoflow_recall_message",
                toolset="infoflow",
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
                name="infoflow_send_message",
                toolset="infoflow",
                schema=SEND_MESSAGE_TOOL_SCHEMA,
                handler=make_send_message_handler(),
                is_async=True,
                description=(
                    "Send an Infoflow message to a required target with text, "
                    "reply references, links, images, or group @ mentions."
                ),
                emoji="💬",
            )
        except Exception as exc:
            gw_log().warning("[infoflow] failed to register send_message tool: %s", exc)
        try:
            register_tool(
                name="file_delivery",
                toolset="infoflow",
                schema=FILE_DELIVERY_TOOL_SCHEMA,
                handler=make_file_delivery_handler(),
                is_async=True,
                description="Publish a local file as an Infoflow-shareable URL.",
                emoji="🔗",
            )
        except Exception as exc:
            gw_log().warning("[infoflow] failed to register file_delivery tool: %s", exc)
        try:
            register_tool(
                name="infoflow_get_group_members",
                toolset="infoflow",
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
                name="infoflow_create_group",
                toolset="infoflow",
                schema=CREATE_GROUP_TOOL_SCHEMA,
                handler=make_create_group_handler(),
                is_async=True,
                description=(
                    "Create a new Infoflow group and invite initial human/robot members."
                ),
                emoji="➕",
            )
        except Exception as exc:
            gw_log().warning(
                "[infoflow] failed to register create group tool: %s", exc,
            )
        try:
            register_tool(
                name="infoflow_get_message_history",
                toolset="infoflow",
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
        try:
            register_tool(
                name="infoflow_download_attachment",
                toolset="infoflow",
                schema=DOWNLOAD_ATTACHMENT_TOOL_SCHEMA,
                handler=make_download_attachment_handler(),
                is_async=True,
                description="Download an inbound Infoflow file attachment on demand.",
                emoji="📎",
            )
        except Exception as exc:
            gw_log().warning(
                "[infoflow] failed to register download attachment tool: %s", exc,
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
