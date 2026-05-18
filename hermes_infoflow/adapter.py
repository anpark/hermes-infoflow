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
import contextvars
import logging
import socket as _socket
from pathlib import Path
from typing import Any, TYPE_CHECKING

import aiohttp

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
    )
    from gateway.platforms.base import cache_image_from_bytes  # type: ignore[import-not-found]

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

from .serverapi import ServerAPI
from .itypes import IncomingMessage, SendOptions, SentResult, ReplyInfo
from .webhook import parse_webhook_request
from .iftools import (
    RECALL_TOOL_SCHEMA,
    REPLY_TOOL_SCHEMA,
    make_recall_handler,
    make_reply_handler,
)
from .policy import (
    GroupConfigOverride,
    GroupPolicy,
    PolicyDecision,
    normalize_reply_mode,
)
from .iflogging import get_logger as _get_api_logger
from .message_store import MessageStore
from .recall import get_inbound_body, get_inbound_sender_imid
from .sent_store import SentMessageStore
from .utils import (
    _ImageLoadError,
    _allowed_media_roots,
    _download_inbound_image,
    _is_safe_outbound_url,
    _resolve_safe_local_path,
)
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
from .bot import Bot, get_recall_inbound_message_id_hint  # noqa: E402

if TYPE_CHECKING:
    from aiohttp import web as _web_module

logger = logging.getLogger(__name__)


def gw_log() -> logging.Logger:
    """Return the gateway.run logger so audit lines reach gateway.log."""
    return logging.getLogger("gateway.run")


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
        self._last_followup_is_passive = False

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
        self._sent_store = SentMessageStore(
            dedup_set=self._dedup_set,
            account_id=self._settings.get("app_key") or "default",
        )
        self._message_store = MessageStore(
            account_id=str(self._settings.get("app_agent_id") or "default"),
        )

        # ── LLM judge (lightweight classifier for follow-up messages) ────
        from .llm_judge import _load_llm_config

        self._llm_config = _load_llm_config()
        gw_log().info("[infoflow] llm_judge config: model=%s base_url=%s has_key=%s",
                       self._llm_config.get("model", "?"),
                       self._llm_config.get("base_url", "?"),
                       bool(self._llm_config.get("api_key", "")))

        # ── Bot (business logic) ──────────────────────────────────────
        self._bot = Bot(
            settings=self._settings,
            policy=self._policy,
            serverapi=self._serverapi,
            sent_store=self._sent_store,
            dedup_set=self._dedup_set,
            message_store=self._message_store,
            llm_config=self._llm_config,
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

        self._http_session: aiohttp.ClientSession | None = None
        self._runner: Any = None
        self._site: Any = None

        if not hasattr(self, "_background_tasks"):
            self._background_tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Infoflow"

    def _missing_required(self) -> list[str]:
        missing = []
        for key, label in (
            ("api_host", "INFOFLOW_API_HOST"),
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
        self._bot._http_session = self._http_session  # share session with bot
        try:
            from aiohttp import web
            app = web.Application(client_max_size=DEFAULT_BODY_LIMIT_BYTES)
            app.router.add_post(self._webhook_path, self._handle_webhook)
            app.router.add_get("/health", lambda _req: web.Response(text="ok"))
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
        except Exception as exc:
            await self._close_partial_state()
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
        return True

    async def disconnect(self) -> None:
        self._running = False
        try:
            if self._site is not None:
                await self._site.stop()
        finally:
            self._site = None
        try:
            if self._runner is not None:
                await self._runner.cleanup()
        finally:
            self._runner = None
        if self._http_session is not None:
            try:
                await self._http_session.close()
            finally:
                self._http_session = None
                self._serverapi.http_session = None
                self._bot._http_session = None
        self._mark_disconnected()
        gw_log().info("[infoflow] Disconnected")

    async def _close_partial_state(self) -> None:
        try:
            if self._site is not None:
                await self._site.stop()
        except Exception:
            pass
        self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None

    # ------------------------------------------------------------------
    # Webhook handler
    # ------------------------------------------------------------------

    async def _handle_webhook(self, request: "_web_module.Request") -> "_web_module.Response":
        """Receive an Infoflow webhook hit, dispatch in the background, return 200."""
        try:
            raw_bytes = await request.read()
        except Exception as exc:
            gw_log().warning("[infoflow] failed to read webhook body: %s", exc)
            from aiohttp import web
            return web.Response(status=400, text="bad request")
        raw_body = raw_bytes.decode("utf-8", errors="replace")

        content_type = request.headers.get("Content-Type", "")
        gw_log().info(
            "[infoflow] webhook received: ct=%s body_len=%d ip=%s",
            content_type, len(raw_bytes), getattr(request, "remote", None) or "unknown",
        )

        # Parse via webhook channel → serverapi → bot-layer types
        wh_result = parse_webhook_request(
            content_type=content_type,
            raw_body=raw_body,
            parser_account=self._serverapi.parser_account,
            dedup_set=self._dedup_set,
        )

        from aiohttp import web

        if wh_result.kind != "message":
            if wh_result.kind == "echostr_ok":
                gw_log().info("[infoflow] webhook echostr verification OK")
                return web.Response(status=200, text=wh_result.body, content_type="text/plain")
            if wh_result.kind == "echostr_bad":
                gw_log().warning("[infoflow] webhook echostr verification BAD")
                return web.Response(status=403, text=wh_result.body)
            if wh_result.kind == "http_error":
                gw_log().warning("[infoflow] webhook parse error (status=%s): %s", wh_result.status, wh_result.body)
                return web.Response(status=wh_result.status, text=wh_result.body)
            # "ignored"
            return web.Response(status=200, text="OK")

        # Convert parser.InboundMessage → types.IncomingMessage
        msg = self._serverapi.to_incoming(wh_result.raw_inbound)

        # --- Stage 1: [iflow:raw] — decoded plaintext payload ---
        if msg.is_group:
            try:
                import json as _json
                _raw = wh_result.raw_inbound.raw_msgdata if hasattr(wh_result.raw_inbound, "raw_msgdata") else {}
                gw_log().info(
                    "[iflow:raw] mid=%s payload=%s",
                    msg.msgid, _json.dumps(_raw, ensure_ascii=False, default=str)[:2000],
                )
            except Exception:
                pass

        # --- Enrich sender info from group member cache (ALL group messages) ---
        # Bot: must have agent_id; Human: must have userId.
        # Fallback: IMID:{imid} prefix when cache+API both fail.
        if msg.is_group and msg.sender_imid:
            await self._enrich_sender(msg)

        # --- Stage 2: [iflow:event] — enriched message event fields ---
        if msg.is_group:
            try:
                gw_log().info(
                    "[iflow:event] mid=%s sender_id=%s sender_name=%s sender_imid=%s "
                    "sender_agent_id=%s is_bot=%s mentioned=%s "
                    "mention_users=%s mention_agents=%s reply_to_bot=%s body=%s",
                    msg.msgid, msg.sender_id, msg.sender_name, msg.sender_imid,
                    getattr(msg, "sender_agent_id", ""), msg.sender_is_bot,
                    msg.bot_was_mentioned,
                    msg.mention_user_ids, msg.mention_agent_ids,
                    msg.is_reply_to_bot, (msg.body_for_agent or "")[:200],
                )
                for _i, _b in enumerate(msg.body_items or []):
                    if hasattr(_b, "type"):
                        gw_log().info(
                            "[iflow:event] body_item[%d] type=%s name=%s userid=%s robotid=%s",
                            _i, _b.type, _b.name, _b.userid, _b.robotid,
                        )
            except Exception:
                pass

        # Delegate to bot for policy/dedup/context
        result = await self._bot.process_inbound(msg)

        if result.should_dispatch and result.decision:
            self._bot.spawn_dispatch(msg, result.decision, self, self._background_tasks)

        return web.Response(status=200, text="OK")

    # ------------------------------------------------------------------
    # _enrich_sender: resolve sender name/agent_id from group members
    # ------------------------------------------------------------------

    async def _enrich_sender(self, msg: "IncomingMessage") -> None:
        """Populate sender_name / sender_agent_id from group member cache or API.

        Called for ALL group messages in the webhook handler (before dispatch
        decision), so even non-dispatched messages get enriched for logging
        and potential future use.

        Degradation:
          - Bot without agent_id  → ``IMID:{imid}`` (agent-level ops may fail)
          - Human without userId  → ``IMID:{imid}``
        """
        from .serverapi import _MEMBERS_CACHE

        sender_info = None  # GroupMember | None

        # 1. Try cache
        cached = _MEMBERS_CACHE.get(msg.group_id)
        if cached:
            for m in cached[0]:
                if str(m.imid) == str(msg.sender_imid):
                    sender_info = m
                    break

        # 2. Cache miss → API (6s timeout, stale fallback inside serverapi)
        if sender_info is None:
            try:
                members = await self._serverapi.get_group_members(msg.group_id)
                for m in members:
                    if str(m.imid) == str(msg.sender_imid):
                        sender_info = m
                        break
            except Exception as exc:
                gw_log().warning(
                    "[infoflow] get_group_members(%s) failed: %s",
                    msg.group_id, exc,
                )

        # 3. Apply enriched info
        if sender_info:
            if sender_info.is_bot:
                if not msg.sender_name or msg.sender_name == msg.sender_imid:
                    msg.sender_name = sender_info.name or msg.sender_name
                if sender_info.agent_id:
                    msg.sender_agent_id = str(sender_info.agent_id)
            else:
                if sender_info.uid and (not msg.sender_id or msg.sender_id == msg.sender_imid):
                    msg.sender_id = sender_info.uid
                if not msg.sender_name or msg.sender_name == msg.sender_imid:
                    msg.sender_name = sender_info.name or msg.sender_id

        # 4. Degradation: ensure mandatory fields exist
        _degraded = False
        if msg.sender_is_bot and not getattr(msg, "sender_agent_id", ""):
            msg.sender_agent_id = f"IMID:{msg.sender_imid}"
            _degraded = True
        if not msg.sender_is_bot and not msg.sender_id:
            msg.sender_id = f"IMID:{msg.sender_imid}"
            _degraded = True

        gw_log().info(
            "[infoflow-enrich] mid=%s sender=%s(%s) name=%s agent_id=%s is_bot=%s degraded=%s",
            msg.msgid, msg.sender_id, msg.sender_imid, msg.sender_name,
            getattr(msg, "sender_agent_id", ""), msg.sender_is_bot, _degraded,
        )

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

        # Encode sender identity into user_name for gateway's [sender] prefix.
        # Human:  [chengbo05]          — uid is the unique identifier
        # Bot:    [chengbo5.1 🤖:6471] — agentId is the unique identifier + bot name
        if msg.sender_is_bot:
            _aid = getattr(msg, "sender_agent_id", "") or msg.sender_id
            _user_display = f"{msg.sender_name} 🤖:{_aid}"
        else:
            _user_display = msg.sender_id  # uid IS the human identity

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            user_id=msg.sender_id,
            user_name=_user_display,
            message_id=msg.msgid,
        )

        message_type = MessageType.PHOTO if local_media else MessageType.TEXT
        text_for_agent = msg.body_for_agent or msg.text or "<media:image>"

        raw_message: dict[str, Any] = {
            "raw_text": msg.text,
            "mention_user_ids": list(msg.mention_user_ids),
            "mention_agent_ids": list(msg.mention_agent_ids),
            "reply_targets": list(msg.reply_targets),
            "is_reply_to_bot": msg.is_reply_to_bot,
            "was_mentioned": msg.bot_was_mentioned,
            "image_urls": list(msg.image_urls),
            "msgseqid": msg.msgseqid,
            "raw_msgdata": msg.raw_data,
            "event_type": msg.event_type,
            "fromid": msg.sender_imid,
            "is_bot_sender": msg.sender_is_bot,
            "sender_name": msg.sender_name,
            "sender_agent_id": getattr(msg, "sender_agent_id", ""),
        }
        if decision is not None:
            raw_message["policy_action"] = decision.action.value
            raw_message["policy_reason"] = decision.reason
            raw_message["trigger_reason"] = decision.trigger_reason

            # Follow-up: enrich with sender context
            if getattr(decision, "needs_sender_context", False) and msg.group_id:
                try:
                    from .policy import build_follow_up_prompt
                    _sender_engaged = False
                    if hasattr(self._policy, "sender_mentioned_in_window"):
                        _sender_engaged = self._policy.sender_mentioned_in_window(
                            msg.group_id, msg.sender_id or msg.sender_imid,
                        )
                    # If message text contains bot name, treat as implicit engaged.
                    _bot_name = self._settings.get("robot_name") or ""
                    if (not _sender_engaged and _bot_name
                            and len(_bot_name) >= 2
                            and _bot_name.lower() in (msg.text or "").lower()):
                        _sender_engaged = True
                    prompt = build_follow_up_prompt(
                        fromid=msg.sender_imid,
                        sender_name=msg.sender_name or msg.sender_id,
                        is_bot=msg.sender_is_bot,
                        agent_id=getattr(msg, "sender_agent_id", ""),
                        is_reply_to_bot=msg.is_reply_to_bot,
                        sender_engaged=_sender_engaged,
                    )
                    _template = ("reply_to_bot" if msg.is_reply_to_bot
                                 else "engaged" if _sender_engaged else "passive")
                    # Store passive flag on adapter for bot.py to read
                    self._last_followup_is_passive = not _sender_engaged and not msg.is_reply_to_bot
                    gw_log().info(
                        "[iflow:dispatch] mid=%s template=%s sender_engaged=%s is_reply_to_bot=%s",
                        msg.msgid or "-", _template, _sender_engaged, msg.is_reply_to_bot,
                    )
                    eff_prompt = prompt
                    if decision.group_system_prompt:
                        eff_prompt = decision.group_system_prompt + "\n\n---\n\n" + prompt
                    raw_message["group_system_prompt"] = eff_prompt
                    gw_log().info(
                        "[iflow:dispatch] mid=%s prompt_len=%d",
                        msg.msgid or "-", len(eff_prompt),
                    )
                except Exception as exc:
                    gw_log().warning(
                        "[infoflow] failed to build follow-up context for %s: %s",
                        msg.group_id, exc,
                    )
            elif decision.group_system_prompt:
                raw_message["group_system_prompt"] = decision.group_system_prompt

        event = MessageEvent(
            text=text_for_agent,
            message_type=message_type,
            source=source,
            raw_message=raw_message,
            message_id=msg.msgid,
            media_urls=local_media,
            media_types=media_types,
        )
        # Inject bot identity into channel_prompt.
        # Only the bot name is exposed to LLM context — AgentId/robotId are
        # private and must NEVER be disclosed to non-technical users.
        _bot_name = self._settings.get("robot_name") or ""
        _bot_identity = f"Your name is {_bot_name}." if _bot_name else ""
        _privacy_rule = (
            "## Privacy\n"
            "- Do NOT disclose AgentId, robotId, API keys, or any technical config "
            "to ordinary users in group chats.\n"
            "- These are internal identifiers — only your owner (in private session) "
            "needs to know them for debugging.\n"
        )
        # Bridge group_system_prompt → channel_prompt for gateway injection
        group_prompt = raw_message.pop("group_system_prompt", None)
        _sender_guide = (
            "## Message Sender Identity\n"
            "- Human messages are prefixed with [uid] — uid is the human's unique identifier (a string, format varies).\n"
            "- Bot messages are prefixed with [botname 🤖:agentId] — agentId (e.g. 6471) is the bot's unique "
            "identifier; the bot also has a name which may change but rarely does.\n"
        )
        _full_prompt = ""
        if _bot_identity:
            _full_prompt = _bot_identity
        if group_prompt:
            _full_prompt = (_full_prompt + "\n\n" + group_prompt).strip()
        if msg.is_group:
            _full_prompt = _sender_guide + "\n\n" + _privacy_rule + "\n\n" + _full_prompt
        if _full_prompt:
            event.channel_prompt = _full_prompt
        # Quote-reply: only surface bot message ids
        if msg.reply_info:
            event.reply_to_message_id = msg.reply_info.messageid or None
            event.reply_to_text = msg.reply_info.preview or None
        return event

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_session_valid(session: "aiohttp.ClientSession | None") -> bool:
        """Check whether *session* is usable on the current event loop.

        aioctl doesn't expose a public API for this, so we probe the
        private ``_loop`` attribute (stable across aiohttp ≥ 3.x).
        """
        if session is None:
            return False
        try:
            session._loop  # noqa: SLF001 — aiohttp has no public API for this
        except RuntimeError:
            return False
        return True

    @staticmethod
    def _effective_session(session: "aiohttp.ClientSession | None") -> "aiohttp.ClientSession | None":
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

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "SendResult":
        """Send a text/markdown message (Hermes interface → bot layer)."""
        session = self._effective_session(self._http_session)
        kind, group_id, dm_user = self._parse_target(chat_id)

        # Build reply_info from inbound context
        reply_info: ReplyInfo | None = None
        if reply_to:
            body = get_inbound_body(reply_to)
            if body:
                reply_info = ReplyInfo(
                    messageid=reply_to,
                    preview=body[:MAX_PREVIEW_LENGTH],
                    sender_imid=get_inbound_sender_imid(reply_to),
                )

        # Build send options from Hermes metadata
        options = SendOptions()
        if metadata:
            options.at_all = bool(metadata.get("at_all"))
            options.mention_user_ids = str(metadata.get("mention_user_ids") or "")
            options.mention_agent_ids = str(metadata.get("mention_agent_ids") or "")

        # Delegate to bot
        bot_result = await self._bot.send_message(
            group_id=str(group_id) if group_id is not None else None,
            dm_user_id=dm_user or None,
            text=content,
            reply_info=reply_info,
            options=options,
            session=session,
        )

        # Trace outbound with inbound mid (if available via contextvar)
        _mid = _inbound_mid.get("")
        if _mid:
            gw_log().info(
                "[iflow:send] mid=%s target=%s chars=%d success=%s",
                _mid, chat_id, len(content), bot_result.success,
            )

        if bot_result.success:
            return SendResult(success=True, message_id=bot_result.msgid)
        return SendResult(
            success=False,
            error=bot_result.error,
            retryable=False,
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
    ) -> "SendResult":
        """Send an image (Hermes interface → bot layer)."""
        session = self._effective_session(self._http_session)
        kind, group_id, dm_user = self._parse_target(chat_id)
        try:
            image_bytes = await self._load_image_bytes(image_url)
        except _ImageLoadError as exc:
            return SendResult(success=False, error=str(exc), retryable=False)

        reply_info: ReplyInfo | None = None
        if reply_to:
            body = get_inbound_body(reply_to)
            if body:
                reply_info = ReplyInfo(
                    messageid=reply_to,
                    preview=body[:MAX_PREVIEW_LENGTH],
                    sender_imid=get_inbound_sender_imid(reply_to),
                )

        bot_result = await self._bot.send_image(
            group_id=str(group_id) if group_id is not None else None,
            dm_user_id=dm_user or None,
            image_bytes=image_bytes,
            caption=caption,
            reply_info=reply_info,
            session=session,
        )

        # Trace outbound with inbound mid
        _mid = _inbound_mid.get("")
        if _mid:
            gw_log().info(
                "[iflow:send] mid=%s target=%s type=image success=%s",
                _mid, chat_id, bot_result.success,
            )

        if bot_result.success:
            return SendResult(success=True, message_id=bot_result.msgid)
        return SendResult(success=False, error=bot_result.error, retryable=False)

    # ------------------------------------------------------------------
    # Outbound: recall (delete_message)
    # ------------------------------------------------------------------

    async def delete_message(
        self,
        chat_id: str,
        message_id: str | None = None,
        *,
        count: int = 1,
        current_inbound_message_id: str | None = None,
    ) -> "SendResult":
        """Recall one or more bot-sent messages (Hermes interface → bot layer)."""
        session = self._effective_session(self._http_session)
        kind, group_id, dm_user = self._parse_target(chat_id)
        result = await self._bot.recall_message(
            group_id=str(group_id) if group_id is not None else None,
            dm_user_id=dm_user or None,
            msgid=message_id,
            msgseqid="",
            count=count,
            current_inbound_msgid=current_inbound_message_id,
            session=session,
        )

        if result.success:
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
                f"refusing to read image from {image_url!r}: not inside an allowed media root"
            )
        try:
            return candidate.read_bytes()
        except OSError as exc:
            raise _ImageLoadError(f"failed to read image {candidate}: {exc}") from exc

    async def _fetch_url_bytes(
        self, url: str, *, max_bytes: int = 25 * 1024 * 1024,
    ) -> bytes:
        ok, reason = _is_safe_outbound_url(url)
        if not ok:
            raise _ImageLoadError(f"refusing to fetch image: {reason}")
        own_session = self._http_session is None
        session = self._http_session or aiohttp.ClientSession()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30.0)) as resp:
                if resp.status >= 400:
                    raise _ImageLoadError(f"image fetch HTTP {resp.status} for {url[:80]}")
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise _ImageLoadError(f"image payload exceeds {max_bytes} bytes; aborting")
                return bytes(buf)
        finally:
            if own_session:
                await session.close()

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
    from aiohttp import web as _aiohttp_web_module  # noqa: E402
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
            "INFOFLOW_API_HOST",
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
            "You are chatting via Baidu Infoflow (如流). Infoflow renders "
            "Markdown (bold/italic/code/lists/links). "
            "send_message targets use the format ``infoflow:<target>`` where "
            "<target> is either a uuapName (for DMs, e.g. ``infoflow:chengbo05``) "
            "or ``group:<id>`` (for groups, e.g. ``infoflow:group:4507088``). "
            "Omitting the target sends to the home channel. "
            "In group chats (chat_id=group:<id>) you can @-mention everyone via "
            "metadata.at_all=true, specific users via "
            "metadata.mention_user_ids='user1,user2' (comma-separated "
            "uuapNames), or specific bots via "
            "metadata.mention_agent_ids='17212,33333' (comma-separated "
            "numeric agentIds). The plugin auto-injects @<agentId> into "
            "the message body so the mention renders correctly. "
            "Use the infoflow_recall_message tool to recall "
            "your own previously-sent message; NEVER pass the inbound user "
            "message_id as the recall target — that is the USER's message, "
            "not a bot message, and the call will fail. "
            "Use the infoflow_reply tool to reply to or quote a specific "
            "message with a preview of the original text."
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


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_WEBHOOK_PATH",
    "InfoflowAdapter",
    "MAX_MESSAGE_LENGTH",
    "register",
]
