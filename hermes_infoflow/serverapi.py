"""Infoflow service API adapter.

Provides a **clean, unified interface** over Infoflow's messy REST API.
Translates between bot-layer types (:mod:`types`) and Infoflow wire formats.

Responsibilities
----------------
* **Incoming**: convert ``parser.InboundMessage`` → ``types.IncomingMessage``
* **Outbound**: build Infoflow payloads from bot-layer params and call
  ``api.py`` functions
* **Common capabilities**: group members, image download, token refresh
* **Session management**: own an ``aiohttp.ClientSession`` bound to the
  main event loop; accept an optional ``session`` parameter on every
  async method so callers on *other* loops (e.g. tool dispatchers) get
  a fresh session automatically
"""

from __future__ import annotations

import base64
import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from . import api as _api
from .itypes import (
    GroupMember,
    IncomingMessage,
    RecallResult,
    ReplyInfo,
    SendOptions,
    SentResult,
)

if TYPE_CHECKING:
    from .parser import AccountConfig, InboundMessage

logger = logging.getLogger(__name__)

_MAX_PREVIEW_LENGTH = 200
_DEFAULT_MARKDOWN_TOKENS = ("**", "__", "`", "* ", "- ", "# ", "](", "```")

# Group member cache: {group_id: (members_list, timestamp)}
_MEMBERS_CACHE: dict[str, tuple[list[GroupMember], float]] = {}
_MEMBERS_CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Parser account proxy (duck-typed for parser.py)
# ---------------------------------------------------------------------------


@dataclass
class _ParserAccountView:
    """Lightweight view passed to ``parser.parse_webhook()``.

    Mirrors the fields consumed by parser functions without pulling in
    the full ``AccountConfig`` class.
    """

    check_token: str
    encoding_aes_key: str
    robot_name: str
    app_agent_id: str
    robot_id: str


# ---------------------------------------------------------------------------
# ServerAPI
# ---------------------------------------------------------------------------


class ServerAPI:
    """Unified Infoflow service interface.

    Construction
    ~~~~~~~~~~~~
    Created once by :class:`InfoflowAdapter` during ``__init__`` and
    shared with :class:`Bot` for all Infoflow interactions.
    """

    def __init__(self, *, settings: dict[str, Any]) -> None:
        self._settings = settings
        api_host = settings.get("api_host", "")
        if not api_host or "baidu" not in api_host:
            logger.warning(
                "[serverapi] api_host looks invalid: %r — "
                "INFOFLOW_API_HOST should be like http://apiin.im.baidu.com",
                api_host,
            )
        self._api_account = _api.InfoflowAccountAPI(
            api_host=settings["api_host"],
            app_key=settings["app_key"],
            app_secret=settings["app_secret"],
            app_agent_id=settings.get("app_agent_id"),
        )
        self._robot_id: str = str(settings.get("robot_id") or "")
        self._parser_account = _ParserAccountView(
            check_token=settings["check_token"],
            encoding_aes_key=settings["encoding_aes_key"],
            robot_name=settings.get("robot_name", ""),
            app_agent_id=str(settings.get("app_agent_id", "")),
            robot_id=self._robot_id,
        )
        self._http_session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def robot_id(self) -> str:
        return self._robot_id

    @robot_id.setter
    def robot_id(self, value: str) -> None:
        self._robot_id = value
        self._parser_account.robot_id = value

    @property
    def parser_account(self) -> _ParserAccountView:
        """Return the parser account view (refreshed with latest robot_id)."""
        return self._parser_account

    @property
    def http_session(self) -> aiohttp.ClientSession | None:
        return self._http_session

    @http_session.setter
    def http_session(self, session: aiohttp.ClientSession | None) -> None:
        self._http_session = session

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _ensure_session(
        self, session: aiohttp.ClientSession | None
    ):
        """Yield a usable session, cleaning up ad-hoc ones on exit.

        Prefers the caller-supplied *session*, then the persistent
        ``self._http_session``.  If neither is available (e.g. when
        invoked from a different event loop), a temporary session is
        created and **automatically closed** when the context exits.
        """
        if session is not None:
            yield session
            return
        if self._http_session is not None:
            try:
                sess_loop = self._http_session._loop  # noqa: SLF001
                current_loop = asyncio.get_running_loop()
                if sess_loop is current_loop:
                    yield self._http_session
                    return
            except RuntimeError:
                pass
        # Last resort: ad-hoc session — close it when the caller is done.
        async with aiohttp.ClientSession() as sess:
            yield sess

    # ------------------------------------------------------------------
    # Incoming message conversion
    # ------------------------------------------------------------------

    def to_incoming(self, parser_inbound: InboundMessage) -> IncomingMessage:
        """Convert ``parser.InboundMessage`` → ``types.IncomingMessage``.

        This is the **single point** where the plugin-internal canonical
        format is produced from whatever ``parser.py`` returns.
        """
        # Extract bot-layer ReplyInfo (only for bot-sent targets)
        reply_info: ReplyInfo | None = None
        if parser_inbound.reply_targets:
            bot_target = next(
                (t for t in parser_inbound.reply_targets if t.get("isBotMessage")),
                None,
            )
            if bot_target:
                reply_info = ReplyInfo(
                    messageid=str(bot_target.get("messageid") or ""),
                    preview=str(bot_target.get("preview") or ""),
                )

        return IncomingMessage(
            msgid=str(parser_inbound.message_id or ""),
            text=parser_inbound.text or "",
            group_id=(
                parser_inbound.group_id
                if parser_inbound.chat_type == "group" and parser_inbound.group_id
                else None
            ),
            dm_user_id=(
                parser_inbound.from_user
                if parser_inbound.chat_type != "group"
                else None
            ),
            sender_id=parser_inbound.from_user or "",
            sender_name=parser_inbound.sender_name or "",
            sender_imid=parser_inbound.fromid or "",
            sender_is_bot=parser_inbound.is_bot_sender,
            bot_was_mentioned=parser_inbound.was_mentioned,
            mention_user_ids=list(parser_inbound.mention_user_ids),
            mention_agent_ids=[int(x) for x in parser_inbound.mention_agent_ids if str(x).isdigit()],
            reply_info=reply_info,
            reply_targets=list(parser_inbound.reply_targets),
            is_reply_to_bot=parser_inbound.is_reply_to_bot,
            body_for_agent=parser_inbound.body_for_agent or "",
            image_urls=list(parser_inbound.image_urls),
            body_items=list(parser_inbound.body_items),
            dedupe_key=parser_inbound.dedupe_key() or "",
            msgseqid=str(parser_inbound.msgseqid or ""),
            timestamp=(parser_inbound.timestamp_ms or 0) / 1000.0,
            discovered_robot_id=parser_inbound.discovered_robot_id or None,
            raw_data=parser_inbound.raw_msgdata or {},
            event_type=parser_inbound.event_type or "",
        )

    # ------------------------------------------------------------------
    # Send — group
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_markdown(content: str) -> bool:
        return any(tok in content for tok in _DEFAULT_MARKDOWN_TOKENS)

    @staticmethod
    def _build_contents(
        text: str, options: SendOptions | None
    ) -> list[_api.ContentItem]:
        """Translate bot-layer send params → Infoflow ``ContentItem[]``.

        Handles AT items (must precede text in the body array) and
        markdown auto-detection.
        """
        options = options or SendOptions()
        items: list[_api.ContentItem] = []
        at_prefix_parts: list[str] = []

        # --- AT items ---
        if options.at_all:
            items.append(_api.ContentItem("at", "all"))
        else:
            user_ids_for_api: list[str] = []
            if options.mention_user_ids:
                for uid in (s.strip() for s in options.mention_user_ids.split(",") if s.strip()):
                    # Always generate AT item; only prepend @uid if text lacks it
                    user_ids_for_api.append(uid)
                    if not (text and f"@{uid}" in text):
                        at_prefix_parts.append(f"@{uid}")
            if user_ids_for_api:
                items.append(_api.ContentItem("at", ",".join(user_ids_for_api)))
            if options.mention_agent_ids:
                agent_ids_for_api: list[str] = []
                for aid in (s.strip() for s in options.mention_agent_ids.split(",") if s.strip()):
                    # Always generate AT item; only prepend @aid if text lacks it
                    agent_ids_for_api.append(aid)
                    if not (text and f"@{aid}" in text):
                        at_prefix_parts.append(f"@{aid}")
                if agent_ids_for_api:
                    items.append(_api.ContentItem("at-agent", ",".join(agent_ids_for_api)))

        # --- Text / Markdown ---
        if text:
            if at_prefix_parts:
                text = " ".join(at_prefix_parts) + " " + text
            markdown = options.markdown
            if markdown is None:
                markdown = ServerAPI._looks_like_markdown(text)
            items.append(_api.ContentItem("markdown" if markdown else "text", text))
        return items

    async def send_to_group(
        self,
        group_id: str,
        text: str,
        *,
        reply_info: ReplyInfo | None = None,
        options: SendOptions | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> SentResult:
        """Send a text/markdown message to a group."""
        contents = self._build_contents(text, options)

        reply_ctx = None
        if reply_info:
            reply_ctx = _api.ReplyContext(
                messageid=reply_info.messageid,
                preview=reply_info.preview[:_MAX_PREVIEW_LENGTH],
                replytype=reply_info.replytype,
                imid=reply_info.sender_imid or self._robot_id,
            )

        async with self._ensure_session(session) as sess:
            try:
                res = await _api.send_group_message(
                    self._api_account,
                    group_id=int(group_id),
                    contents=contents,
                    reply_to=reply_ctx,
                    session=sess,
                )
            except Exception as exc:
                return SentResult(success=False, error=str(exc))

            if res.get("ok"):
                return SentResult(
                    success=True,
                    msgid=str(res.get("messageid") or ""),
                    msgseqid=str(res.get("msgseqid") or ""),
                    raw_response=res,
                )
            return SentResult(success=False, error=res.get("error") or "send failed", raw_response=res)

    # ------------------------------------------------------------------
    # Send — DM
    # ------------------------------------------------------------------

    async def send_to_dm(
        self,
        user_id: str,
        text: str,
        *,
        options: SendOptions | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> SentResult:
        """Send a text/markdown message to a user (DM)."""
        contents = self._build_contents(text, options)

        async with self._ensure_session(session) as sess:
            try:
                res = await _api.send_private_message(
                    self._api_account,
                    to_user=user_id,
                    contents=contents,
                    session=sess,
                )
            except Exception as exc:
                return SentResult(success=False, error=str(exc))

            # Enrich opaque API errors with actionable hints.
            if not res.get("ok") and user_id:
                err = res.get("error", "")
                if "可见范围" in err or "关注" in err:
                    res["error"] = (
                        f"{err} "
                        f"(to_user={user_id!r} — Infoflow private send requires the "
                        f"recipient to have an existing conversation with the bot, "
                        f"or the chat_id must be a uuapName rather than an accountId)"
                    )

            if res.get("ok"):
                return SentResult(
                    success=True,
                    msgid=str(res.get("messageid") or res.get("msgkey") or ""),
                    msgseqid=str(res.get("msgseqid") or ""),
                    raw_response=res,
                )
            return SentResult(success=False, error=res.get("error") or "send failed", raw_response=res)

    # ------------------------------------------------------------------
    # Send image — group
    # ------------------------------------------------------------------

    async def send_image_to_group(
        self,
        group_id: str,
        image_bytes: bytes,
        *,
        caption: str | None = None,
        reply_info: ReplyInfo | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> SentResult:
        """Send an image (optionally with caption) to a group."""
        b64 = base64.b64encode(image_bytes).decode("ascii")
        contents: list[_api.ContentItem] = [_api.ContentItem("image", b64)]
        if caption:
            md = self._looks_like_markdown(caption)
            contents.insert(0, _api.ContentItem("markdown" if md else "text", caption))

        reply_ctx = None
        if reply_info:
            reply_ctx = _api.ReplyContext(
                messageid=reply_info.messageid,
                preview=reply_info.preview[:_MAX_PREVIEW_LENGTH],
                replytype=reply_info.replytype,
                imid=reply_info.sender_imid or self._robot_id,
            )

        async with self._ensure_session(session) as sess:
            try:
                res = await _api.send_group_message(
                    self._api_account,
                    group_id=int(group_id),
                    contents=contents,
                    reply_to=reply_ctx,
                    session=sess,
                )
            except Exception as exc:
                return SentResult(success=False, error=str(exc))

            if res.get("ok"):
                return SentResult(
                    success=True,
                    msgid=str(res.get("messageid") or ""),
                    msgseqid=str(res.get("msgseqid") or ""),
                    raw_response=res,
                )
            return SentResult(success=False, error=res.get("error") or "image send failed", raw_response=res)

    # ------------------------------------------------------------------
    # Send image — DM
    # ------------------------------------------------------------------

    async def send_image_to_dm(
        self,
        user_id: str,
        image_bytes: bytes,
        *,
        caption: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> SentResult:
        """Send an image to a user (DM). Caption and image sent separately."""
        b64 = base64.b64encode(image_bytes).decode("ascii")

        caption_items: list[_api.ContentItem] = []
        image_items: list[_api.ContentItem] = [_api.ContentItem("image", b64)]
        if caption:
            md = self._looks_like_markdown(caption)
            caption_items = [_api.ContentItem("markdown" if md else "text", caption)]

        async with self._ensure_session(session) as sess:
            try:
                # Send caption first (text), then image.
                res_caption: dict[str, Any] = {"ok": True}
                if caption_items:
                    res_caption = await _api.send_private_message(
                        self._api_account, to_user=user_id, contents=caption_items, session=sess,
                    )
                res = await _api.send_private_message(
                    self._api_account, to_user=user_id, contents=image_items, session=sess,
                )
                # Surface caption error if image succeeded.
                if not res_caption.get("ok") and res.get("ok"):
                    res = {"ok": False, "error": res_caption.get("error"), **{k: v for k, v in res.items() if k != "ok"}}
            except Exception as exc:
                return SentResult(success=False, error=str(exc))

            if res.get("ok"):
                return SentResult(
                    success=True,
                    msgid=str(res.get("messageid") or res.get("msgkey") or ""),
                    msgseqid=str(res.get("msgseqid") or ""),
                    raw_response=res,
                )
            return SentResult(success=False, error=res.get("error") or "image send failed", raw_response=res)

    # ------------------------------------------------------------------
    # Recall — group
    # ------------------------------------------------------------------

    async def recall_group_message(
        self,
        group_id: str,
        msgid: str,
        msgseqid: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> RecallResult:
        """Recall (withdraw) a bot-sent group message."""
        async with self._ensure_session(session) as sess:
            try:
                res = await _api.recall_group_message(
                    self._api_account,
                    group_id=int(group_id),
                    messageid=msgid,
                    msgseqid=msgseqid,
                    session=sess,
                )
            except Exception as exc:
                return RecallResult(success=False, error=str(exc))

            if res.get("ok"):
                return RecallResult(success=True, raw_response=res)
            return RecallResult(success=False, error=res.get("error") or "recall failed", raw_response=res)

    # ------------------------------------------------------------------
    # Recall — DM
    # ------------------------------------------------------------------

    async def recall_private_message(
        self,
        msgkey: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> RecallResult:
        """Recall (withdraw) a bot-sent private message by its ``msgkey``."""
        async with self._ensure_session(session) as sess:
            try:
                res = await _api.recall_private_message(
                    self._api_account,
                    msgkey=msgkey,
                    session=sess,
                )
            except Exception as exc:
                return RecallResult(success=False, error=str(exc))

            if res.get("ok"):
                return RecallResult(success=True, raw_response=res)
            return RecallResult(success=False, error=res.get("error") or "recall failed", raw_response=res)

    # ------------------------------------------------------------------
    # Group members (common capability)
    # ------------------------------------------------------------------

    async def get_group_members(
        self,
        group_id: str,
        *,
        session: aiohttp.ClientSession | None = None,
        force_refresh: bool = False,
    ) -> list[GroupMember]:
        """Return cached-then-fresh group member list.

        Results are cached per group_id for ``_MEMBERS_CACHE_TTL`` seconds.
        Pass ``force_refresh=True`` to bypass the cache.
        """
        gid = str(group_id)
        # Check cache
        if not force_refresh:
            cached = _MEMBERS_CACHE.get(gid)
            if cached and (time.time() - cached[1]) < _MEMBERS_CACHE_TTL:
                logger.debug("[serverapi] get_group_members(%s) cache hit", gid)
                return cached[0]

        async with self._ensure_session(session) as sess:
            try:
                api_members = await _api.get_group_members(
                    self._api_account, group_id=group_id, session=sess,
                    timeout=6.0,
                )
                members = [
                    GroupMember(
                        uid=str(m.uid or ""),
                        name=m.name or "",
                        agent_id=int(m.agent_id or 0),
                        is_bot=m.is_bot,
                        imid=getattr(m, "imid", "") or "",
                    )
                    for m in api_members
                ]
                # Update cache
                _MEMBERS_CACHE[gid] = (members, time.time())
                logger.debug(
                    "[serverapi] get_group_members(%s): %d members cached",
                    gid, len(members),
                )
                return members
            except Exception as exc:
                logger.warning("[serverapi] get_group_members(%s) failed: %s", group_id, exc)
                # Return stale cache if available
                cached = _MEMBERS_CACHE.get(gid)
                if cached:
                    logger.info("[serverapi] get_group_members(%s) returning stale cache", gid)
                    return cached[0]
                return []

    # ------------------------------------------------------------------
    # Image download
    # ------------------------------------------------------------------

    async def download_image(
        self,
        url: str,
        *,
        session: aiohttp.ClientSession | None = None,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> bytes | None:
        """Download an image from a URL (with auth token).

        Returns raw bytes or ``None`` on failure.
        """
        async with self._ensure_session(session) as sess:
            try:
                token = await self.get_access_token(session=sess)
                async with sess.get(
                    url,
                    headers={"Authorization": f"Bearer-{token}"},
                    timeout=aiohttp.ClientTimeout(total=30.0),
                ) as resp:
                    if resp.status >= 400:
                        return None
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            return None
                    return bytes(buf)
            except Exception as exc:
                logger.warning("[serverapi] download_image(%s) failed: %s", url[:80], exc)
                return None

    # ------------------------------------------------------------------
    # Access token
    # ------------------------------------------------------------------

    async def get_access_token(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> str:
        """Return a valid app access token (cached / refreshed)."""
        async with self._ensure_session(session) as sess:
            return await _api.get_app_access_token(self._api_account, session=sess)
