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

import asyncio
import base64
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
    from .parser import InboundMessage

logger = logging.getLogger(__name__)

_MAX_PREVIEW_LENGTH = 200

# Group member cache: {group_id: (members_list, timestamp)}
_MEMBERS_CACHE: dict[str, tuple[list[GroupMember], float]] = {}
_MEMBERS_CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# resolve_member_identity — unified member lookup with guarded fetch
# ---------------------------------------------------------------------------

from enum import Enum


class CacheRetrievalPolicy(Enum):
    """Control when ``resolve_member_identity`` hits the network."""
    RETRIEVE_FROM_CACHE_ONLY = "cache_only"
    RETRIEVE_FROM_REMOTE_ONLY = "remote_only"
    RETRIEVE_FROM_CACHE_THEN_REMOTE = "cache_then_remote"  # default


# Per-group guarded-fetch state (module-level for singleton semantics)
_guarded_state: dict[str, dict] = {}  # group_id → {task, last_ts, members}
_DEBOUNCE_SECONDS = 3.0


async def resolve_member_identity(
    group_id: str,
    *,
    bot_name: str | None = None,
    agent_id: int | None = None,
    imid: str | None = None,
    cache_policy: CacheRetrievalPolicy = CacheRetrievalPolicy.RETRIEVE_FROM_CACHE_THEN_REMOTE,
    session: aiohttp.ClientSession | None = None,
    serverapi: "ServerApi | None" = None,
) -> dict:
    """Look up a group member by *any* of the provided identity fields.

    Returns the matching ``GroupMember`` as a plain dict (same fields as the
    dataclass), or an empty dict if not found.

    Guarantees at most **one** concurrent remote fetch per group_id.
    Multiple concurrent callers share the same in-flight task.
    A 3-second debounce suppresses redundant fetches after a successful one.
    """
    import asyncio

    if not any(v is not None for v in (bot_name, agent_id, imid)):
        return {}

    gid = str(group_id)

    # --- helpers ---
    def _match(m: GroupMember) -> bool:
        if bot_name is not None and m.name == bot_name and m.is_bot:
            return True
        if agent_id is not None and m.agent_id == agent_id:
            return True
        if imid is not None and str(m.imid) == str(imid):
            return True
        return False

    def _to_dict(m: GroupMember) -> dict:
        return {
            "uid": m.uid,
            "name": m.name,
            "imid": m.imid,
            "agent_id": m.agent_id,
            "is_bot": m.is_bot,
        }

    # --- try cache first (unless REMOTE_ONLY) ---
    if cache_policy != CacheRetrievalPolicy.RETRIEVE_FROM_REMOTE_ONLY:
        cached = _MEMBERS_CACHE.get(gid)
        if cached and cached[0]:
            for m in cached[0]:
                if _match(m):
                    return _to_dict(m)

    # --- remote fetch (unless CACHE_ONLY) ---
    if cache_policy != CacheRetrievalPolicy.RETRIEVE_FROM_CACHE_ONLY:
        state = _guarded_state.setdefault(gid, {"task": None, "last_ts": 0.0, "members": None})

        # Skip if within debounce window and we already have members
        now = time.time()
        if state["members"] is not None and (now - state["last_ts"]) < _DEBOUNCE_SECONDS:
            for m in state["members"]:
                if _match(m):
                    return _to_dict(m)

        # Reuse inflight task or create a new one
        if state["task"] is None or state["task"].done():
            async def _fetch():
                if serverapi is None:
                    return []
                try:
                    members = await serverapi.get_group_members(
                        gid, session=session, force_refresh=True,
                    )
                    state["members"] = members
                    state["last_ts"] = time.time()
                    return members
                except Exception:
                    state["task"] = None  # allow retry
                    return []
            state["task"] = asyncio.ensure_future(_fetch())

        members = await state["task"]
        if members:
            for m in members:
                if _match(m):
                    return _to_dict(m)

    return {}





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
            message_id=str(parser_inbound.message_id or ""),
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
            is_at_only=parser_inbound.is_at_only,
            raw_data=parser_inbound.raw_msgdata or {},
            event_type=parser_inbound.event_type or "",
        )

    # ------------------------------------------------------------------
    # Send — group
    # ------------------------------------------------------------------

    @staticmethod
    def _build_contents(
        text: str, options: SendOptions | None
    ) -> list[_api.ContentItem]:
        """Translate bot-layer send params → Infoflow ``ContentItem[]``.

        Always emits markdown ContentItems; AT items are prepended as needed.
        """
        options = options or SendOptions()
        items: list[_api.ContentItem] = []
        at_prefix_parts: list[str] = []

        # --- AT items (independent — at_all, users, agents can coexist) ---
        if options.at_all:
            items.append(_api.ContentItem("at", "all"))
            text_lower = text.lower()
            if text and "@all" not in text_lower and "@所有人" not in text:
                at_prefix_parts.append("@all")

        if options.mention_user_ids:
            user_ids_for_api: list[str] = []
            for uid in (s.strip() for s in options.mention_user_ids.split(",") if s.strip()):
                user_ids_for_api.append(uid)
                if not (text and f"@{uid}" in text):
                    at_prefix_parts.append(f"@{uid}")
            if user_ids_for_api:
                items.append(_api.ContentItem("at", ",".join(user_ids_for_api)))

        if options.mention_agent_ids:
            agent_ids_for_api: list[str] = []
            for aid in (s.strip() for s in options.mention_agent_ids.split(",") if s.strip()):
                agent_ids_for_api.append(aid)
                if not (text and f"@{aid}" in text):
                    at_prefix_parts.append(f"@{aid}")
            if agent_ids_for_api:
                items.append(_api.ContentItem("at-agent", ",".join(agent_ids_for_api)))

        # --- Markdown (always MD) ---
        if text:
            if at_prefix_parts:
                text = " ".join(at_prefix_parts) + " " + text
            items.append(_api.ContentItem("markdown", text))
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
                    message_id=str(res.get("messageid") or ""),
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
                    message_id=str(res.get("messageid") or res.get("msgkey") or ""),
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
            contents.insert(0, _api.ContentItem("markdown", caption))

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
                    message_id=str(res.get("messageid") or ""),
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
            caption_items = [_api.ContentItem("markdown", caption)]

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
                    message_id=str(res.get("messageid") or res.get("msgkey") or ""),
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
        message_id: str,
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
                    messageid=message_id,
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
