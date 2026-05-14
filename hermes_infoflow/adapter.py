"""``InfoflowAdapter`` — the actual Hermes gateway platform adapter.

This module ties together :mod:`hermes_infoflow.parser`,
:mod:`hermes_infoflow.api`, :mod:`hermes_infoflow.policy`, and
:mod:`hermes_infoflow.sent_store` behind the
:class:`gateway.platforms.base.BasePlatformAdapter` interface that
hermes-agent expects.

Hermes-agent runtime symbols are imported with a soft-fallback so that
this module is also importable in a hermes-free environment (CI for our
own crypto/parser/api unit tests). When hermes is missing, the adapter
class still exists but inherits from ``object`` and ``register()``
refuses to run.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import re
import socket as _socket
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from . import api as _api
from .parser import (
    AccountConfig as _ParserAccount,
    InboundMessage,
    ParsedWebhook,
    parse_webhook,
)
from .policy import (
    GroupPolicy,
    PolicyDecision,
    evaluate_inbound,
    normalize_reply_mode,
)
from .sent_store import SentMessageStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional hermes-agent imports
# ---------------------------------------------------------------------------

try:
    from gateway.config import Platform, PlatformConfig  # type: ignore[import-not-found]
    from gateway.platforms.base import (  # type: ignore[import-not-found]
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
        cache_image_from_bytes,
    )

    HERMES_AVAILABLE = True
except ImportError:  # pragma: no cover - test-only stub path
    HERMES_AVAILABLE = False

    class _Stub:
        """Sentinel base when hermes-agent isn't importable (tests)."""

    Platform = _Stub          # type: ignore[assignment,misc]
    PlatformConfig = _Stub    # type: ignore[assignment,misc]
    BasePlatformAdapter = _Stub  # type: ignore[assignment,misc]
    MessageEvent = _Stub      # type: ignore[assignment,misc]
    MessageType = _Stub       # type: ignore[assignment,misc]
    SendResult = _Stub        # type: ignore[assignment,misc]
    cache_image_from_bytes = None  # type: ignore[assignment]


try:
    from aiohttp import web

    AIOHTTP_WEB_AVAILABLE = True
except ImportError:  # pragma: no cover - aiohttp is a declared dependency
    AIOHTTP_WEB_AVAILABLE = False
    web = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Defaults / constants
# ---------------------------------------------------------------------------

DEFAULT_PORT = 8646
DEFAULT_HOST = "0.0.0.0"
DEFAULT_WEBHOOK_PATH = "/webhook/infoflow"
MAX_MESSAGE_LENGTH = 2048  # matches OpenClaw textChunkLimit
DEFAULT_BODY_LIMIT_BYTES = 20 * 1024 * 1024
GROUP_TARGET_RE = re.compile(r"^group:(\d+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r (expected int)", name, raw)
        return default


def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _read_account_settings(config: Any) -> dict[str, Any]:
    """Merge env vars and ``config.extra`` into a flat settings dict.

    Env vars take precedence over config.extra entries — this matches the
    documented contract for other hermes platform plugins.
    """
    extra: dict[str, Any] = {}
    if config is not None:
        extra = dict(getattr(config, "extra", None) or {})

    def pick(env_name: str, key: str, default: Any = None) -> Any:
        env_val = os.getenv(env_name)
        if env_val not in (None, ""):
            return env_val
        if key in extra and extra[key] not in (None, ""):
            return extra[key]
        return default

    settings: dict[str, Any] = {
        "check_token": pick("INFOFLOW_CHECK_TOKEN", "check_token", "") or "",
        "encoding_aes_key": pick("INFOFLOW_ENCODING_AES_KEY", "encoding_aes_key", "") or "",
        "app_key": pick("INFOFLOW_APP_KEY", "app_key", "") or "",
        "app_secret": pick("INFOFLOW_APP_SECRET", "app_secret", "") or "",
        "api_host": pick("INFOFLOW_API_HOST", "api_host", "") or "",
        "robot_name": pick("INFOFLOW_ROBOT_NAME", "robot_name", "") or "",
        "host": pick("INFOFLOW_HOST", "host", DEFAULT_HOST) or DEFAULT_HOST,
        "webhook_path": pick("INFOFLOW_WEBHOOK_PATH", "webhook_path", DEFAULT_WEBHOOK_PATH)
        or DEFAULT_WEBHOOK_PATH,
        "connection_mode": (pick("INFOFLOW_CONNECTION_MODE", "connection_mode", "webhook") or "webhook").lower(),
        "reply_mode": (pick("INFOFLOW_REPLY_MODE", "reply_mode", "mention-and-watch") or "mention-and-watch"),
        "require_mention_raw": pick("INFOFLOW_REQUIRE_MENTION", "require_mention", "true"),
        "watch_mentions_raw": pick("INFOFLOW_WATCH_MENTIONS", "watch_mentions", ""),
    }

    # Numbers.
    raw_port = pick("INFOFLOW_PORT", "port", DEFAULT_PORT)
    try:
        settings["port"] = int(raw_port) if raw_port not in (None, "") else DEFAULT_PORT
    except ValueError:
        settings["port"] = DEFAULT_PORT
    raw_agent_id = pick("INFOFLOW_APP_AGENT_ID", "app_agent_id", None)
    settings["app_agent_id"] = int(raw_agent_id) if raw_agent_id not in (None, "") else None

    # Booleans.
    raw_req = settings.pop("require_mention_raw")
    if isinstance(raw_req, bool):
        settings["require_mention"] = raw_req
    else:
        settings["require_mention"] = str(raw_req).strip().lower() not in ("0", "false", "no", "off")

    # CSV-ish.
    watch_raw = settings.pop("watch_mentions_raw") or ""
    if isinstance(watch_raw, list):
        settings["watch_mentions"] = [str(x).strip() for x in watch_raw if str(x).strip()]
    else:
        settings["watch_mentions"] = [s.strip() for s in str(watch_raw).split(",") if s.strip()]

    return settings


# ---------------------------------------------------------------------------
# Local file path safety
# ---------------------------------------------------------------------------


def _allowed_media_roots() -> list[Path]:
    """Directories we'll accept ``file://`` outbound images from."""
    roots = [
        Path.home() / ".hermes" / "media",
        Path(tempfile.gettempdir()),
        Path("/tmp"),
    ]
    # De-duplicate, ignore non-existent.
    seen: set[str] = set()
    keep: list[Path] = []
    for r in roots:
        try:
            r_resolved = r.expanduser().resolve()
        except OSError:
            continue
        key = str(r_resolved)
        if key in seen:
            continue
        seen.add(key)
        keep.append(r_resolved)
    return keep


def _resolve_safe_local_path(raw: str) -> Path | None:
    """Resolve ``raw`` and return it iff inside one of the allowed roots.

    Prevents LLM-driven path traversal (e.g. ``file:///etc/passwd``) when
    the agent passes a local image URL to ``send_image``.
    """
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    try:
        candidate = Path(raw).expanduser().resolve()
    except OSError:
        return None
    if not candidate.is_file():
        return None
    for root in _allowed_media_roots():
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Webhook image downloader (with Bearer- fallback)
# ---------------------------------------------------------------------------


async def _download_inbound_image(
    url: str,
    *,
    token_provider,
    session: aiohttp.ClientSession | None = None,
    max_bytes: int = 25 * 1024 * 1024,
) -> tuple[bytes, str] | None:
    """Fetch ``url`` (with a Bearer- fallback) and return ``(bytes, ext)``.

    Infoflow IMAGE bodies' ``downloadurl`` is usually a short-lived signed
    link. Try a bare GET first; if we hit 401/403, retry once with the
    Infoflow app access token as the Authorization header.
    """

    async def _try(s: aiohttp.ClientSession, headers: dict[str, str] | None) -> tuple[bytes, str] | None:
        try:
            async with s.get(
                url,
                headers=headers or {},
                timeout=aiohttp.ClientTimeout(total=30.0),
            ) as resp:
                if resp.status in (401, 403):
                    return None
                if resp.status >= 400:
                    logger.warning("[infoflow:inbound image] HTTP %s for %s", resp.status, url[:100])
                    return None
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        logger.warning("[infoflow:inbound image] payload exceeds %s bytes; aborting", max_bytes)
                        return None
                ext = mimetypes.guess_extension(content_type) or ".jpg"
                return bytes(buf), ext
        except aiohttp.ClientError as exc:
            logger.debug("[infoflow:inbound image] GET failed: %s", exc)
            return None
        except asyncio.TimeoutError:
            logger.debug("[infoflow:inbound image] GET timed out")
            return None

    own_session = session is None
    sess = session or aiohttp.ClientSession()
    try:
        result = await _try(sess, None)
        if result is None:
            try:
                token = await token_provider()
            except Exception:
                token = None
            if token:
                result = await _try(sess, {"Authorization": f"Bearer-{token}"})
        return result
    finally:
        if own_session:
            await sess.close()


# ---------------------------------------------------------------------------
# Outbound URL safety (SSRF guard for send_image)
# ---------------------------------------------------------------------------


import ipaddress as _ipaddress


_DENIED_HOSTNAMES = frozenset({
    "localhost",
    "169.254.169.254",   # AWS / GCP metadata
    "metadata.google.internal",
    "metadata",
})


def _is_safe_outbound_url(url: str) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a candidate outbound URL.

    Restricts the agent's ``send_image`` to public http(s) targets. Blocks
    private/loopback/link-local IPs and known cloud metadata hostnames to
    prevent the LLM from being tricked into exfiltrating internal data
    via an "image" send.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"only http/https allowed, got {parsed.scheme!r}"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "URL has no hostname"
    if host in _DENIED_HOSTNAMES:
        return False, f"hostname {host!r} is on the deny list"
    # Reject IP literals that resolve to private / loopback / link-local space.
    try:
        ip = _ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved
    ):
        return False, f"IP {host} is in a non-public range"
    return True, ""


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class InfoflowAdapter(BasePlatformAdapter):
    """Hermes gateway adapter for Baidu Infoflow (如流)."""

    # Hermes-side hint for smart message chunking. Adopted by
    # ``tools/send_message_tool._send_to_platform`` via the
    # platform-registry lookup.
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

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

        # Build the api-layer account view + parser view of the same data.
        self._api_account = _api.InfoflowAccountAPI(
            api_host=self._settings["api_host"],
            app_key=self._settings["app_key"],
            app_secret=self._settings["app_secret"],
            app_agent_id=self._settings["app_agent_id"],
        )
        self._parser_account = _ParserAccount(
            check_token=self._settings["check_token"],
            encoding_aes_key=self._settings["encoding_aes_key"],
            robot_name=self._settings["robot_name"],
            app_agent_id=self._settings["app_agent_id"],
            robot_id="",
        )

        normalized_mode = normalize_reply_mode(self._settings["reply_mode"])
        if normalized_mode.warning:
            logger.warning("[infoflow] %s", normalized_mode.warning)
        self._policy = GroupPolicy(
            reply_mode=normalized_mode.value,
            require_mention=self._settings["require_mention"],
            watch_mentions=self._settings["watch_mentions"],
        )

        # Shared dedup set: outbound records + inbound webhook dedup
        # consult the SAME set so the bot never reacts to its own message
        # if Infoflow replays it.
        self._dedup_set: set[str] = set()
        self._sent_store = SentMessageStore(dedup_set=self._dedup_set)

        self._port: int = int(self._settings["port"])
        self._host: str = str(self._settings["host"])
        self._webhook_path: str = str(self._settings["webhook_path"]) or DEFAULT_WEBHOOK_PATH
        if not self._webhook_path.startswith("/"):
            self._webhook_path = "/" + self._webhook_path

        self._http_session: aiohttp.ClientSession | None = None
        self._runner: Any = None  # web.AppRunner once started
        self._site: Any = None    # web.TCPSite once started

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
            logger.warning(
                "[infoflow] INFOFLOW_CONNECTION_MODE=%r is not yet supported; falling back to webhook",
                self._settings["connection_mode"],
            )

        # Friendly port-occupancy precheck (matches wecom_callback pattern).
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
            # Expected: nothing is listening yet.
            pass

        self._http_session = aiohttp.ClientSession()
        try:
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
            self._host,
            self._port,
            self._webhook_path,
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
        self._mark_disconnected()
        logger.info("[infoflow] Disconnected")

    async def _close_partial_state(self) -> None:
        """Best-effort teardown when ``connect()`` aborts midway."""
        try:
            if self._site is not None:
                await self._site.stop()
        except Exception:
            pass
        self._site = None
        try:
            if self._runner is not None:
                await self._runner.cleanup()
        except Exception:
            pass
        self._runner = None
        if self._http_session is not None:
            try:
                await self._http_session.close()
            except Exception:
                pass
            self._http_session = None

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":  # type: ignore[name-defined]
        """Receive an Infoflow webhook hit, dispatch in the background, return 200.

        Critical correctness rule: this handler **must** return within a few
        seconds. Infoflow retries POSTs that aren't ACKed quickly, which
        would multiply duplicate messages into the agent if we awaited
        ``handle_message`` synchronously.
        """
        try:
            raw_bytes = await request.read()
        except Exception as exc:
            logger.warning("[infoflow] failed to read webhook body: %s", exc)
            return web.Response(status=400, text="bad request")
        raw_body = raw_bytes.decode("utf-8", errors="replace")

        content_type = request.headers.get("Content-Type", "")
        parsed = parse_webhook(
            content_type=content_type,
            raw_body=raw_body,
            account=self._parser_account,
            sent_message_ids=self._dedup_set,
        )

        if parsed.kind == "echostr_ok":
            return web.Response(status=200, text=parsed.body, content_type="text/plain")
        if parsed.kind == "echostr_bad":
            return web.Response(status=403, text=parsed.body)
        if parsed.kind == "http_error":
            return web.Response(status=parsed.status_code, text=parsed.body)
        if parsed.kind == "ignored" or parsed.inbound is None:
            return web.Response(status=200, text="OK")

        inbound = parsed.inbound
        dedupe_key = inbound.dedupe_key()
        if dedupe_key and self._sent_store.is_duplicate(dedupe_key):
            logger.debug("[infoflow] duplicate inbound %s; dropping", dedupe_key[:40])
            return web.Response(status=200, text="OK")
        if dedupe_key:
            self._sent_store.mark_seen(dedupe_key)

        decision = evaluate_inbound(inbound, self._policy)
        if not decision.should_dispatch:
            logger.debug("[infoflow] policy dropped inbound: %s", decision.reason)
            return web.Response(status=200, text="OK")

        # Fire-and-forget: agent processing must not block the HTTP ACK.
        self._spawn_dispatch(inbound)
        return web.Response(status=200, text="OK")

    def _spawn_dispatch(self, inbound: InboundMessage) -> None:
        """Schedule ``handle_message`` on the running loop without awaiting it."""
        task = asyncio.create_task(self._dispatch_inbound(inbound))
        # Keep a reference so the loop doesn't GC the task while it runs.
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _dispatch_inbound(self, inbound: InboundMessage) -> None:
        try:
            event = await self._build_message_event(inbound)
            await self.handle_message(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[infoflow] inbound dispatch failed")

    async def _build_message_event(self, inbound: InboundMessage) -> Any:
        """Translate :class:`InboundMessage` → hermes ``MessageEvent``."""
        # Cache inbound images via the gateway media helper so the agent can read them.
        local_media: list[str] = []
        media_types: list[str] = []
        if inbound.image_urls and cache_image_from_bytes is not None:
            for url in inbound.image_urls:
                downloaded = await _download_inbound_image(
                    url,
                    token_provider=lambda: _api.get_app_access_token(
                        self._api_account, session=self._http_session
                    ),
                    session=self._http_session,
                )
                if downloaded is None:
                    continue
                data, ext = downloaded
                try:
                    cached = cache_image_from_bytes(data, ext=ext)
                except Exception as exc:
                    logger.warning("[infoflow] cache_image_from_bytes failed: %s", exc)
                    continue
                local_media.append(cached)
                media_types.append(f"image/{ext.lstrip('.')}")

        chat_id = f"group:{inbound.group_id}" if inbound.chat_type == "group" else inbound.from_user
        chat_type = "group" if inbound.chat_type == "group" else "dm"

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            user_id=inbound.from_user,
            user_name=inbound.sender_name or inbound.from_user,
            message_id=inbound.message_id,
        )

        message_type = MessageType.PHOTO if local_media else MessageType.TEXT  # type: ignore[attr-defined]
        text_for_agent = inbound.body_for_agent or inbound.text or "<media:image>"

        event = MessageEvent(  # type: ignore[call-arg]
            text=text_for_agent,
            message_type=message_type,
            source=source,
            raw_message={
                "raw_text": inbound.text,
                "mention_user_ids": list(inbound.mention_user_ids),
                "mention_agent_ids": list(inbound.mention_agent_ids),
                "reply_targets": list(inbound.reply_targets),
                "is_reply_to_bot": inbound.is_reply_to_bot,
                "was_mentioned": inbound.was_mentioned,
                "image_urls": list(inbound.image_urls),
                "msgseqid": inbound.msgseqid,
                "raw_msgdata": inbound.raw_msgdata,
            },
            message_id=inbound.message_id,
            media_urls=local_media,
            media_types=media_types,
        )
        if inbound.reply_targets:
            first = inbound.reply_targets[0]
            event.reply_to_message_id = str(first.get("messageid") or "") or None
            event.reply_to_text = str(first.get("preview") or "") or None
        return event

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_target(chat_id: str) -> tuple[str, int | None, str]:
        """Return ``(kind, group_id, dm_user)`` from a chat_id.

        ``kind`` is ``"group"`` or ``"dm"``. For groups, ``group_id`` is the
        numeric id and ``dm_user`` is "".
        """
        target = chat_id
        if target.lower().startswith("infoflow:"):
            target = target[len("infoflow:"):]
        m = GROUP_TARGET_RE.match(target)
        if m:
            return "group", int(m.group(1)), ""
        return "dm", None, target

    def _build_contents(self, content: str, metadata: dict[str, Any] | None) -> list[_api.ContentItem]:
        """Translate a plain-text/markdown body + metadata into ``ContentItem``s.

        ``metadata`` supports::

            at_all: bool                    # group only — @everyone
            mention_user_ids: "u1,u2"       # group only — @-mention specific users
            markdown: bool                  # treat ``content`` as markdown (default: auto-detect)
        """
        metadata = metadata or {}
        items: list[_api.ContentItem] = []
        if metadata.get("at_all"):
            items.append(_api.ContentItem("at", "all"))
        elif metadata.get("mention_user_ids"):
            ids = str(metadata["mention_user_ids"])
            items.append(_api.ContentItem("at", ids))
        if content:
            markdown = metadata.get("markdown")
            if markdown is None:
                markdown = self._looks_like_markdown(content)
            items.append(_api.ContentItem("markdown" if markdown else "text", content))
        return items

    @staticmethod
    def _looks_like_markdown(content: str) -> bool:
        return any(token in content for token in ("**", "__", "`", "* ", "- ", "# ", "](", "```"))

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "SendResult":  # type: ignore[name-defined]
        kind, group_id, dm_user = self._parse_target(chat_id)
        if kind == "group" and group_id is None:
            return SendResult(success=False, error="invalid group chat id")  # type: ignore[call-arg]

        # truncate_message handles smart chunking; we send each chunk
        # separately so very long agent replies still fit Infoflow's 2KB
        # per-message ceiling.
        chunks = BasePlatformAdapter.truncate_message(content, self.MAX_MESSAGE_LENGTH)
        if not chunks:
            chunks = [""]

        last_messageid: str | None = None
        last_error: str | None = None
        reply_ctx = (
            _api.ReplyContext(messageid=reply_to)
            if (reply_to and kind == "group")
            else None
        )

        for idx, chunk in enumerate(chunks):
            contents = self._build_contents(
                chunk,
                metadata if idx == 0 else None,  # mention metadata only applies to the first chunk
            )
            if kind == "group":
                res = await _api.send_group_message(
                    self._api_account,
                    group_id=group_id,  # type: ignore[arg-type]
                    contents=contents,
                    reply_to=reply_ctx if idx == 0 else None,
                    session=self._http_session,
                )
            else:
                res = await _api.send_private_message(
                    self._api_account,
                    to_user=dm_user,
                    contents=contents,
                    session=self._http_session,
                )

            if res.get("ok"):
                mid = res.get("messageid") or res.get("msgkey")
                msgseq = res.get("msgseqid") or ""
                if mid:
                    self._sent_store.record(
                        chat_id=chat_id,
                        messageid=str(mid),
                        msgseqid=str(msgseq) if msgseq else "",
                        digest=chunk[:80],
                    )
                    last_messageid = str(mid)
            else:
                last_error = res.get("error") or "send failed"

        if last_error and last_messageid is None:
            return SendResult(success=False, error=last_error, retryable=True)  # type: ignore[call-arg]
        return SendResult(success=True, message_id=last_messageid)  # type: ignore[call-arg]

    async def send_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        # Infoflow has no native typing indicator.
        return None

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "SendResult":  # type: ignore[name-defined]
        kind, group_id, dm_user = self._parse_target(chat_id)
        if kind == "group" and group_id is None:
            return SendResult(success=False, error="invalid group chat id")  # type: ignore[call-arg]

        try:
            image_bytes = await self._load_image_bytes(image_url)
        except _ImageLoadError as exc:
            return SendResult(success=False, error=str(exc), retryable=False)  # type: ignore[call-arg]

        b64 = base64.b64encode(image_bytes).decode("ascii")
        contents = [_api.ContentItem("image", b64)]
        if caption:
            contents.insert(0, _api.ContentItem("markdown" if self._looks_like_markdown(caption) else "text", caption))

        if kind == "group":
            res = await _api.send_group_message(
                self._api_account,
                group_id=group_id,  # type: ignore[arg-type]
                contents=contents,
                reply_to=_api.ReplyContext(messageid=reply_to) if reply_to else None,
                session=self._http_session,
            )
        else:
            # Private path: split caption (text/markdown) and image into two separate
            # sends. The image send uses msgtype="image" (see api._build_private_payload),
            # the caption uses text/markdown. Caption first so the recipient sees context
            # before the image.
            caption_items = [c for c in contents if c.type.lower() != "image"]
            image_payload = [c for c in contents if c.type.lower() == "image"]
            res_caption = {"ok": True}
            if caption_items:
                res_caption = await _api.send_private_message(
                    self._api_account,
                    to_user=dm_user,
                    contents=caption_items,
                    session=self._http_session,
                )
            res = await _api.send_private_message(
                self._api_account,
                to_user=dm_user,
                contents=image_payload,
                session=self._http_session,
            )
            # Surface the first error (caption send) if present, so the agent learns.
            if not res_caption.get("ok") and res.get("ok"):
                res = {"ok": False, "error": res_caption.get("error"), **{k: v for k, v in res.items() if k != "ok"}}

        if not res.get("ok"):
            return SendResult(success=False, error=res.get("error") or "image send failed", retryable=True)  # type: ignore[call-arg]
        mid = res.get("messageid") or res.get("msgkey")
        if mid:
            self._sent_store.record(
                chat_id=chat_id,
                messageid=str(mid),
                msgseqid=str(res.get("msgseqid") or ""),
                digest="[image]",
            )
        return SendResult(success=True, message_id=str(mid) if mid else None)  # type: ignore[call-arg]

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
        self,
        url: str,
        *,
        max_bytes: int = 25 * 1024 * 1024,
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
                        raise _ImageLoadError(
                            f"image payload exceeds {max_bytes} bytes; aborting"
                        )
                return bytes(buf)
        finally:
            if own_session:
                await session.close()

    async def delete_message(
        self,
        chat_id: str,
        message_id: str | None = None,
        *,
        count: int = 1,
    ) -> "SendResult":  # type: ignore[name-defined]
        """Recall one or more bot-sent messages on ``chat_id``.

        With ``message_id`` set, recalls that single message.
        Without, recalls the ``count`` most recent messages tracked in
        the in-process ``SentMessageStore``.
        """
        kind, group_id, dm_user = self._parse_target(chat_id)
        targets: list[tuple[str, str]] = []  # (messageid, msgseqid)

        if message_id:
            entry = self._sent_store.find(chat_id, message_id)
            msgseq = entry.msgseqid if entry else ""
            targets.append((message_id, msgseq))
        else:
            for entry in self._sent_store.recent(chat_id, max(1, count)):
                targets.append((entry.messageid, entry.msgseqid))

        if not targets:
            return SendResult(success=False, error="no recent bot messages to recall")  # type: ignore[call-arg]

        last_error: str | None = None
        recalled_ids: list[str] = []
        for mid, seq in targets:
            if kind == "group":
                if group_id is None or not seq:
                    last_error = "group recall requires group_id and msgseqid"
                    continue
                res = await _api.recall_group_message(
                    self._api_account,
                    group_id=group_id,
                    messageid=mid,
                    msgseqid=seq,
                    session=self._http_session,
                )
            else:
                res = await _api.recall_private_message(
                    self._api_account,
                    msgkey=mid,
                    session=self._http_session,
                )
            if res.get("ok"):
                recalled_ids.append(mid)
            else:
                last_error = res.get("error") or "recall failed"

        if not recalled_ids:
            return SendResult(success=False, error=last_error or "recall failed")  # type: ignore[call-arg]
        return SendResult(success=True, message_id=recalled_ids[-1])  # type: ignore[call-arg]

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        kind, group_id, dm_user = self._parse_target(chat_id)
        if kind == "group":
            return {"name": f"group:{group_id}", "type": "group", "chat_id": chat_id}
        return {"name": dm_user, "type": "dm", "chat_id": chat_id}


# ---------------------------------------------------------------------------
# Standalone (out-of-process) sender for cron / send_message_tool
# ---------------------------------------------------------------------------


async def _standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Send a single message without a live adapter (cron child process)."""
    settings = _read_account_settings(pconfig)
    account = _api.InfoflowAccountAPI(
        api_host=settings["api_host"],
        app_key=settings["app_key"],
        app_secret=settings["app_secret"],
        app_agent_id=settings["app_agent_id"],
    )
    if not (account.api_host and account.app_key and account.app_secret):
        return {"error": "Infoflow standalone send: INFOFLOW_API_HOST/APP_KEY/APP_SECRET are required"}

    kind, group_id, dm_user = InfoflowAdapter._parse_target(chat_id)
    is_markdown = InfoflowAdapter._looks_like_markdown(message)
    contents = [_api.ContentItem("markdown" if is_markdown else "text", message)]

    try:
        if kind == "group":
            if group_id is None:
                return {"error": "Infoflow standalone send: invalid group target"}
            res = await _api.send_group_message(
                account,
                group_id=group_id,
                contents=contents,
            )
        else:
            res = await _api.send_private_message(account, to_user=dm_user, contents=contents)
    except Exception as exc:
        return {"error": f"Infoflow standalone send failed: {exc}"}

    if not res.get("ok"):
        return {"error": res.get("error") or "send failed"}
    mid = res.get("messageid") or res.get("msgkey")
    return {"success": True, "message_id": str(mid) if mid else None}


# ---------------------------------------------------------------------------
# Env-enablement + plugin entry point
# ---------------------------------------------------------------------------


def _env_enablement() -> dict | None:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Returning ``None`` skips auto-enable; otherwise the returned dict
    becomes ``PlatformConfig.extra`` (the special key ``home_channel``
    becomes the structured ``HomeChannel`` field).
    """
    api_host = os.getenv("INFOFLOW_API_HOST", "").strip()
    app_key = os.getenv("INFOFLOW_APP_KEY", "").strip()
    app_secret = os.getenv("INFOFLOW_APP_SECRET", "").strip()
    check_token = os.getenv("INFOFLOW_CHECK_TOKEN", "").strip()
    encoding_aes_key = os.getenv("INFOFLOW_ENCODING_AES_KEY", "").strip()
    if not (api_host and app_key and app_secret and check_token and encoding_aes_key):
        return None
    seed: dict[str, Any] = {
        "api_host": api_host,
        "app_key": app_key,
        "app_secret": app_secret,
        "check_token": check_token,
        "encoding_aes_key": encoding_aes_key,
    }
    if os.getenv("INFOFLOW_APP_AGENT_ID", "").strip():
        try:
            seed["app_agent_id"] = int(os.environ["INFOFLOW_APP_AGENT_ID"].strip())
        except ValueError:
            pass
    for env_key, settings_key in (
        ("INFOFLOW_ROBOT_NAME", "robot_name"),
        ("INFOFLOW_PORT", "port"),
        ("INFOFLOW_HOST", "host"),
        ("INFOFLOW_WEBHOOK_PATH", "webhook_path"),
        ("INFOFLOW_REPLY_MODE", "reply_mode"),
        ("INFOFLOW_REQUIRE_MENTION", "require_mention"),
        ("INFOFLOW_WATCH_MENTIONS", "watch_mentions"),
        ("INFOFLOW_CONNECTION_MODE", "connection_mode"),
    ):
        val = os.getenv(env_key, "").strip()
        if val:
            seed[settings_key] = val
    home = os.getenv("INFOFLOW_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("INFOFLOW_HOME_CHANNEL_NAME", "").strip() or home,
        }
    return seed


def _check_requirements() -> bool:
    """Return True iff the minimum env vars are present.

    hermes-agent calls this during gateway start; missing env vars surface
    a clear "platform not configured" message instead of a crash.
    """
    required = (
        "INFOFLOW_API_HOST",
        "INFOFLOW_APP_KEY",
        "INFOFLOW_APP_SECRET",
        "INFOFLOW_CHECK_TOKEN",
        "INFOFLOW_ENCODING_AES_KEY",
    )
    return all(os.getenv(name) for name in required)


def _validate_config(config: Any) -> bool:
    settings = _read_account_settings(config)
    for key in ("api_host", "app_key", "app_secret", "check_token", "encoding_aes_key"):
        if not settings.get(key):
            return False
    return True


def _is_connected(config: Any) -> bool:
    return _validate_config(config)


def _interactive_setup() -> None:  # pragma: no cover - manual flow
    """``hermes gateway setup`` flow stub.

    The real flow lives upstream in ``hermes_cli/setup.py``; this hook just
    prints clear guidance pointing at the env vars to set.
    """
    print(
        "Set these env vars (or hermes config set):\n"
        "  INFOFLOW_API_HOST=https://api.infoflow.example.com\n"
        "  INFOFLOW_APP_KEY=<your appKey>\n"
        "  INFOFLOW_APP_SECRET=<your appSecret>\n"
        "  INFOFLOW_CHECK_TOKEN=<your checkToken>\n"
        "  INFOFLOW_ENCODING_AES_KEY=<your EncodingAESKey>\n"
        "Optional: INFOFLOW_APP_AGENT_ID, INFOFLOW_ROBOT_NAME, INFOFLOW_PORT, "
        "INFOFLOW_HOME_CHANNEL"
    )


# Schema for the agent-callable infoflow_recall_message tool.
_RECALL_TOOL_SCHEMA = {
    "name": "infoflow_recall_message",
    "description": (
        "Recall a previously bot-sent Infoflow message. Pass `target` "
        "as either a uuapName (DM) or `group:<id>` (group). Provide "
        "`message_id` to recall a specific message, OR omit it and pass "
        "`count` to recall the N most recent bot messages on that chat. "
        "NEVER pass the inbound user message_id; that targets the user's "
        "message, not the bot's, and the call will fail."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "uuapName (DM) or group:<id> (group)",
            },
            "message_id": {
                "type": "string",
                "description": "Optional: the specific bot message id to recall",
            },
            "count": {
                "type": "integer",
                "description": "Number of most-recent bot messages to recall (default 1)",
                "minimum": 1,
                "maximum": 10,
                "default": 1,
            },
        },
        "required": ["target"],
    },
}


def _make_recall_handler():
    """Build the ``infoflow_recall_message`` tool handler.

    Resolves the live adapter via the platform registry so we can reach
    its in-memory ``SentMessageStore``. Returns ``{"error": ...}`` /
    ``{"success": True, ...}``.
    """

    async def _handler(args: dict, **_kwargs) -> dict[str, Any]:
        target = args.get("target")
        message_id = args.get("message_id")
        try:
            count = int(args.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        if not target:
            return {"error": "target is required"}

        try:
            from gateway.run import _gateway_runner_ref  # type: ignore[import-not-found]
            runner = _gateway_runner_ref()
        except Exception:
            runner = None

        adapter: InfoflowAdapter | None = None
        if runner is not None:
            try:
                from gateway.config import Platform  # type: ignore[import-not-found]
                adapter = runner.adapters.get(Platform("infoflow"))
            except Exception:
                adapter = None
        if not isinstance(adapter, InfoflowAdapter):
            adapter = None

        if adapter is None:
            return {"error": (
                "Infoflow adapter not running in this process — cross-process "
                "recall is only supported with an explicit message_id (use the "
                "send_message tool's last-known id)."
            )}
        result = await adapter.delete_message(target, message_id, count=count)
        if not result.success:
            return {"error": result.error or "recall failed"}
        return {"success": True, "message_id": result.message_id}

    return _handler


def register(ctx: Any) -> None:
    """Plugin entry point. Called by hermes-agent's plugin manager.

    Registers:
      * The ``infoflow`` platform with ``platform_registry`` (gateway adapter).
      * The ``infoflow_recall_message`` agent tool in the
        ``hermes-infoflow`` toolset.
    """
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
        standalone_sender_fn=_standalone_send,
        allowed_users_env="INFOFLOW_ALLOWED_USERS",
        allow_all_env="INFOFLOW_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="📣",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Baidu Infoflow (如流). Infoflow renders "
            "Markdown (bold/italic/code/lists/links). In group chats "
            "(chat_id=group:<id>) you can @-mention everyone via "
            "metadata.at_all=true, or specific users via "
            "metadata.mention_user_ids='user1,user2' (comma-separated "
            "uuapNames). Use the infoflow_recall_message tool to recall "
            "your own previously-sent message; NEVER pass the inbound user "
            "message_id as the recall target — that is the USER's message, "
            "not a bot message, and the call will fail."
        ),
    )

    # ``ctx.register_tool`` is exposed by hermes_cli.plugins.PluginContext.
    register_tool = getattr(ctx, "register_tool", None)
    if register_tool is not None:
        try:
            register_tool(
                name="infoflow_recall_message",
                toolset="hermes-infoflow",
                schema=_RECALL_TOOL_SCHEMA,
                handler=_make_recall_handler(),
                is_async=True,
                description="Recall a previously bot-sent Infoflow message (by id or count).",
                emoji="↩️",
            )
        except Exception as exc:
            logger.warning("[infoflow] failed to register recall tool: %s", exc)


# ---------------------------------------------------------------------------
# Module-level helper exceptions
# ---------------------------------------------------------------------------


class _ImageLoadError(Exception):
    """Raised when ``send_image`` cannot load its source bytes."""


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_WEBHOOK_PATH",
    "InfoflowAdapter",
    "MAX_MESSAGE_LENGTH",
    "register",
]
