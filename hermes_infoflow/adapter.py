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
import contextvars
import json
import logging
import mimetypes
import os
import re
import socket as _socket
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
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
    Action,
    GroupConfigOverride,
    GroupPolicy,
    PolicyDecision,
    evaluate_inbound,
    normalize_reply_mode,
)
from .sent_store import SentMessage, SentMessageStore

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

# Optional hint for ``infoflow_recall_message`` when the agent runtime does not
# pass ``current_inbound_message_id`` explicitly (OpenClaw supplies the
# equivalent via ``toolContext.currentMessageId``).  Host code can wrap an
# agent turn with ``recall_inbound_message_id_hint_scope(...)`` so recall
# fallbacks in :meth:`InfoflowAdapter.delete_message` still see the inbound id.
_recall_inbound_message_hint: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hermes_infoflow_recall_inbound_hint",
    default=None,
)


@contextmanager
def recall_inbound_message_id_hint_scope(message_id: str | None):
    """Set the recall inbound-id hint for this block (task-local via ``ContextVar``).

    Hermes-agent (or any host) can wrap an agent turn while handling a message so
    ``infoflow_recall_message`` calls that omit ``current_inbound_message_id`` still
    resolve reply-to-bot / group ``msgseqid`` when the model passes a wrong
    ``message_id``. Mirrors OpenClaw's ``toolContext.currentMessageId``.
    """
    tok = _recall_inbound_message_hint.set(message_id)
    try:
        yield
    finally:
        _recall_inbound_message_hint.reset(tok)


# ---------------------------------------------------------------------------
# Inbound-context registry — mirrors openclaw-infoflow/src/inbound-context.ts.
#
# When the LLM later asks to "delete" a message it sometimes passes the
# inbound user message_id by mistake. Recording the inbound's quote-reply
# targets here lets the recall-correction logic swap in the correct
# bot-sent messageid. Bounded by both TTL and a hard max size.
# ---------------------------------------------------------------------------

_INBOUND_CTX_RETENTION_SECONDS = 10 * 60       # 10 minutes — matches OpenClaw
_INBOUND_CTX_MAX_ENTRIES = 500


@dataclass
class _InboundContext:
    """Snapshot of an inbound message's reply context for later recall recovery."""

    account_id: str
    target: str
    inbound_message_id: str
    reply_to_bot_message_id: str | None
    reply_targets: list[dict[str, Any]]
    inbound_body: str
    registered_at: float


_inbound_ctx_store: dict[str, _InboundContext] = {}


def _register_inbound_context(record: _InboundContext) -> None:
    """Insert ``record`` into the in-process registry, evicting old entries."""
    now = record.registered_at
    cutoff = now - _INBOUND_CTX_RETENTION_SECONDS
    # Lazy TTL sweep.
    if _inbound_ctx_store:
        expired = [k for k, v in _inbound_ctx_store.items() if v.registered_at < cutoff]
        for k in expired:
            _inbound_ctx_store.pop(k, None)
    # Hard size cap — drop oldest first.
    if len(_inbound_ctx_store) >= _INBOUND_CTX_MAX_ENTRIES:
        oldest = sorted(_inbound_ctx_store.items(), key=lambda kv: kv[1].registered_at)
        for k, _v in oldest[: len(_inbound_ctx_store) - _INBOUND_CTX_MAX_ENTRIES + 1]:
            _inbound_ctx_store.pop(k, None)
    _inbound_ctx_store[record.inbound_message_id] = record


def _lookup_inbound_context(inbound_message_id: str) -> _InboundContext | None:
    """Return the registered context for ``inbound_message_id`` (or None)."""
    if not inbound_message_id:
        return None
    rec = _inbound_ctx_store.get(inbound_message_id)
    if rec is None:
        return None
    if time.time() - rec.registered_at > _INBOUND_CTX_RETENTION_SECONDS:
        _inbound_ctx_store.pop(inbound_message_id, None)
        return None
    return rec


# ---------------------------------------------------------------------------
# Recall-intent heuristics (mirrors openclaw-infoflow/src/recall-intent.ts).
# ---------------------------------------------------------------------------

_RECALL_INTENT_RE = re.compile(
    r"(撤回|收回|删[掉了除]|取消|清除|recall|unsend|undo\s*send|delete\s+(?:that|those|the\s+(?:last|previous(?:\s+\d+)?)))",
    re.IGNORECASE,
)
_RECALL_LATEST_HINT_RE = re.compile(
    r"(上一?条|最后一?条|刚才那?条|最近一?条|last(?:\s+(?:one|message|two|few|reply))?|previous|most\s*recent)",
    re.IGNORECASE | re.UNICODE,
)


def _looks_like_recall_intent(text: str) -> bool:
    return bool(text) and bool(_RECALL_INTENT_RE.search(text))


def _looks_like_recall_latest(text: str) -> bool:
    return _looks_like_recall_intent(text) and bool(_RECALL_LATEST_HINT_RE.search(text or ""))


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
        # robot_id is auto-discovered from inbound @-mention bodies on first
        # use; users normally don't set this explicitly, but if they do we
        # honour it as a seed value.
        "robot_id": pick("INFOFLOW_ROBOT_ID", "robot_id", "") or "",
        "host": pick("INFOFLOW_HOST", "host", DEFAULT_HOST) or DEFAULT_HOST,
        "webhook_path": pick("INFOFLOW_WEBHOOK_PATH", "webhook_path", DEFAULT_WEBHOOK_PATH)
        or DEFAULT_WEBHOOK_PATH,
        "connection_mode": (pick("INFOFLOW_CONNECTION_MODE", "connection_mode", "webhook") or "webhook").lower(),
        "reply_mode": (pick("INFOFLOW_REPLY_MODE", "reply_mode", "mention-and-watch") or "mention-and-watch"),
        "require_mention_raw": pick("INFOFLOW_REQUIRE_MENTION", "require_mention", "true"),
        "watch_mentions_raw": pick("INFOFLOW_WATCH_MENTIONS", "watch_mentions", ""),
        "watch_regex_raw": pick("INFOFLOW_WATCH_REGEX", "watch_regex", ""),
        "follow_up_raw": pick("INFOFLOW_FOLLOW_UP", "follow_up", "true"),
        "follow_up_window_raw": pick("INFOFLOW_FOLLOW_UP_WINDOW", "follow_up_window", "300"),
        "groups_raw": pick("INFOFLOW_GROUPS", "groups", None),
        "state_dir_raw": pick("HERMES_STATE_DIR", "state_dir", None),
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
    def _to_bool(raw: Any, *, default: bool) -> bool:
        if isinstance(raw, bool):
            return raw
        if raw is None or raw == "":
            return default
        return str(raw).strip().lower() not in ("0", "false", "no", "off")

    settings["require_mention"] = _to_bool(settings.pop("require_mention_raw"), default=True)
    settings["follow_up"] = _to_bool(settings.pop("follow_up_raw"), default=True)

    try:
        fuw = settings.pop("follow_up_window_raw")
        settings["follow_up_window"] = int(fuw) if fuw not in (None, "") else 300
    except (TypeError, ValueError):
        settings["follow_up_window"] = 300

    # CSV-ish (mentions).
    watch_raw = settings.pop("watch_mentions_raw") or ""
    if isinstance(watch_raw, list):
        settings["watch_mentions"] = [str(x).strip() for x in watch_raw if str(x).strip()]
    else:
        settings["watch_mentions"] = [s.strip() for s in str(watch_raw).split(",") if s.strip()]

    # CSV-ish (regex) — use a sentinel separator to allow commas inside patterns.
    # Convention: separate patterns with newline OR ``|||`` (3 pipes); single
    # pipes are commonly part of regex alternation so don't split on them.
    regex_raw = settings.pop("watch_regex_raw") or ""
    if isinstance(regex_raw, list):
        settings["watch_regex"] = [str(x).strip() for x in regex_raw if str(x).strip()]
    else:
        normalized = str(regex_raw).replace("|||", "\n")
        settings["watch_regex"] = [s.strip() for s in normalized.split("\n") if s.strip()]

    # Per-group overrides. Accept either an already-decoded dict (config.extra)
    # or a JSON string (env var).
    groups_raw = settings.pop("groups_raw")
    groups_parsed: dict[str, dict[str, Any]] = {}
    if isinstance(groups_raw, dict):
        for k, v in groups_raw.items():
            if isinstance(v, dict):
                groups_parsed[str(k)] = v
    elif isinstance(groups_raw, str) and groups_raw.strip():
        try:
            decoded = json.loads(groups_raw)
            if isinstance(decoded, dict):
                for k, v in decoded.items():
                    if isinstance(v, dict):
                        groups_parsed[str(k)] = v
        except (TypeError, ValueError) as exc:
            logger.warning("Ignoring malformed INFOFLOW_GROUPS JSON: %s", exc)
    settings["groups"] = groups_parsed

    # State dir for the persistent sent-messages SQLite store. We default to
    # ``~/.hermes/state/infoflow`` so cron sub-processes can read what the
    # live adapter wrote.
    state_dir = settings.pop("state_dir_raw")
    if state_dir:
        settings["state_dir"] = str(state_dir)
    else:
        settings["state_dir"] = str(Path.home() / ".hermes" / "state")

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
        # Robot ID is auto-discovered from the first inbound @-mention. We seed
        # it from config (rare) but most users will see it populated at runtime.
        self._robot_id: str = str(self._settings.get("robot_id") or "")
        self._parser_account = _ParserAccount(
            check_token=self._settings["check_token"],
            encoding_aes_key=self._settings["encoding_aes_key"],
            robot_name=self._settings["robot_name"],
            app_agent_id=self._settings["app_agent_id"],
            robot_id=self._robot_id,
        )

        normalized_mode = normalize_reply_mode(self._settings["reply_mode"])
        if normalized_mode.warning:
            logger.warning("[infoflow] %s", normalized_mode.warning)

        # Build per-group overrides from settings.
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

        # Shared dedup set: outbound records + inbound webhook dedup
        # consult the SAME set so the bot never reacts to its own message
        # if Infoflow replays it. The store also persists to SQLite so
        # cron sub-processes can recall messages from the live adapter.
        self._dedup_set: set[str] = set()
        db_path = Path(self._settings["state_dir"]) / "infoflow" / "sent-messages.db"
        self._sent_store = SentMessageStore(
            dedup_set=self._dedup_set,
            db_path=db_path,
            account_id=self._settings.get("app_key") or "default",
        )

        self._port: int = int(self._settings["port"])
        self._host: str = str(self._settings["host"])
        self._webhook_path: str = str(self._settings["webhook_path"]) or DEFAULT_WEBHOOK_PATH
        if not self._webhook_path.startswith("/"):
            self._webhook_path = "/" + self._webhook_path

        self._http_session: aiohttp.ClientSession | None = None
        self._runner: Any = None  # web.AppRunner once started
        self._site: Any = None    # web.TCPSite once started

        # Background task pinning — guard against hermes-agent base classes
        # that don't already provide this attribute.
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
                    "supported. Remove the env var or set it to 'webhook'."
                ),
                retryable=False,
            )
            return False

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
        # The parser pulls robot_id from a frozen AccountConfig — refresh from
        # the live discovered value so each request benefits from prior finds.
        parser_account = self._current_parser_account()
        parsed = parse_webhook(
            content_type=content_type,
            raw_body=raw_body,
            account=parser_account,
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

        # Persist the discovered robotId so subsequent inbound parses know
        # who "we" are (used by ``checkBotMentioned`` and the own-message
        # guard below).
        if inbound.discovered_robot_id and inbound.discovered_robot_id != self._robot_id:
            self._robot_id = inbound.discovered_robot_id
            logger.info(
                "[infoflow] discovered robotId=%s for account %s",
                self._robot_id,
                self._parser_account.app_agent_id,
            )

        # Own-message guard: ignore inbound events Infoflow generated from
        # OUR previous outbound (``ALL_MESSAGE_FORWARD``). The dedup set
        # already covers the common case (same messageid), but the robotId
        # check is the stronger guarantee because some echos carry a fresh
        # messageid. Mirrors openclaw bot.ts:766-775.
        if (
            self._robot_id
            and inbound.chat_type == "group"
            and inbound.fromid
            and inbound.fromid == self._robot_id
        ):
            logger.debug(
                "[infoflow] ignoring own bot message (fromid=%s, robotId=%s)",
                inbound.fromid, self._robot_id,
            )
            return web.Response(status=200, text="OK")

        dedupe_key = inbound.dedupe_key()
        if dedupe_key and self._sent_store.is_duplicate(dedupe_key):
            logger.debug("[infoflow] duplicate inbound %s; dropping", dedupe_key[:40])
            return web.Response(status=200, text="OK")
        if dedupe_key:
            self._sent_store.mark_seen(dedupe_key)

        # Register inbound context for the delete-action correction path. We
        # do this BEFORE policy gating so that a "record-only" inbound still
        # has its context available if the LLM later asks to recall it.
        if inbound.message_id:
            target = (
                f"group:{inbound.group_id}"
                if inbound.chat_type == "group" and inbound.group_id
                else inbound.from_user
            )
            reply_to_bot_id: str | None = None
            for tgt in inbound.reply_targets:
                if tgt.get("isBotMessage"):
                    reply_to_bot_id = str(tgt.get("messageid") or "") or None
                    break
            _register_inbound_context(
                _InboundContext(
                    account_id=self._settings.get("app_key") or "default",
                    target=target,
                    inbound_message_id=str(inbound.message_id),
                    reply_to_bot_message_id=reply_to_bot_id,
                    reply_targets=list(inbound.reply_targets),
                    inbound_body=inbound.body_for_agent or inbound.text or "",
                    registered_at=time.time(),
                )
            )

        decision = evaluate_inbound(inbound, self._policy)
        if not decision.should_dispatch:
            # RECORD-mode messages are intentionally dropped at the
            # dispatcher level; hermes-agent's session store records the
            # ambient history once we DO dispatch, so until then we just
            # log. (OpenClaw equivalent: bot.ts's ``record-mode`` branch.)
            if decision.action == Action.RECORD:
                logger.debug(
                    "[infoflow] policy=record: from=%s group=%s reason=%s",
                    inbound.from_user, inbound.group_id, decision.reason,
                )
            else:
                logger.debug("[infoflow] policy dropped inbound: %s", decision.reason)
            return web.Response(status=200, text="OK")

        # Fire-and-forget: agent processing must not block the HTTP ACK.
        self._spawn_dispatch(inbound, decision)
        return web.Response(status=200, text="OK")

    def _current_parser_account(self) -> _ParserAccount:
        """Return a parser account view that reflects the latest discovered robotId."""
        return _ParserAccount(
            check_token=self._parser_account.check_token,
            encoding_aes_key=self._parser_account.encoding_aes_key,
            robot_name=self._parser_account.robot_name,
            app_agent_id=self._parser_account.app_agent_id,
            robot_id=self._robot_id or "",
        )

    def _spawn_dispatch(
        self,
        inbound: InboundMessage,
        decision: PolicyDecision | None = None,
    ) -> None:
        """Schedule ``handle_message`` on the running loop without awaiting it."""
        task = asyncio.create_task(self._dispatch_inbound(inbound, decision))
        # Keep a reference so the loop doesn't GC the task while it runs.
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _dispatch_inbound(
        self,
        inbound: InboundMessage,
        decision: PolicyDecision | None = None,
    ) -> None:
        hint = str(inbound.message_id) if inbound.message_id else None
        try:
            with recall_inbound_message_id_hint_scope(hint):
                event = await self._build_message_event(inbound, decision)
                await self.handle_message(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[infoflow] inbound dispatch failed")

    async def _build_message_event(
        self,
        inbound: InboundMessage,
        decision: PolicyDecision | None = None,
    ) -> Any:
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

        raw_message: dict[str, Any] = {
            "raw_text": inbound.text,
            "mention_user_ids": list(inbound.mention_user_ids),
            "mention_agent_ids": list(inbound.mention_agent_ids),
            "reply_targets": list(inbound.reply_targets),
            "is_reply_to_bot": inbound.is_reply_to_bot,
            "was_mentioned": inbound.was_mentioned,
            "image_urls": list(inbound.image_urls),
            "msgseqid": inbound.msgseqid,
            "raw_msgdata": inbound.raw_msgdata,
            "event_type": inbound.event_type,
            "fromid": inbound.fromid,
        }
        if decision is not None:
            raw_message["policy_action"] = decision.action.value
            raw_message["policy_reason"] = decision.reason
            raw_message["trigger_reason"] = decision.trigger_reason
            if decision.group_system_prompt:
                raw_message["group_system_prompt"] = decision.group_system_prompt

        event = MessageEvent(  # type: ignore[call-arg]
            text=text_for_agent,
            message_type=message_type,
            source=source,
            raw_message=raw_message,
            message_id=inbound.message_id,
            media_urls=local_media,
            media_types=media_types,
        )
        # Quote-reply: only surface *bot* message ids on the event so the LLM
        # does not treat a quoted user message id as a recall target (mirrors
        # openclaw inbound-context / replyTargets handling).
        bot_target = next(
            (t for t in inbound.reply_targets if t.get("isBotMessage")),
            None,
        )
        if bot_target is not None:
            event.reply_to_message_id = str(bot_target.get("messageid") or "") or None
            event.reply_to_text = str(bot_target.get("preview") or "") or None
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

    @staticmethod
    def _normalize_chat_id(chat_id: str) -> str:
        """Return the canonical store key for ``chat_id``.

        Strips an optional ``infoflow:`` prefix so callers passing
        ``infoflow:group:42`` and ``group:42`` see the same sent-store
        bucket and the same inbound-context scope.
        """
        if chat_id and chat_id.lower().startswith("infoflow:"):
            return chat_id[len("infoflow:"):]
        return chat_id

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
        store_key = self._normalize_chat_id(chat_id)

        # truncate_message handles smart chunking; we send each chunk
        # separately so very long agent replies still fit Infoflow's 2KB
        # per-message ceiling.
        chunks = BasePlatformAdapter.truncate_message(content, self.MAX_MESSAGE_LENGTH)
        if not chunks:
            chunks = [""]

        last_messageid: str | None = None
        first_error: str | None = None
        failed_count = 0
        succeeded_count = 0
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
                succeeded_count += 1
                mid = res.get("messageid") or res.get("msgkey")
                msgseq = res.get("msgseqid") or ""
                if mid:
                    self._sent_store.record(
                        chat_id=store_key,
                        messageid=str(mid),
                        msgseqid=str(msgseq) if msgseq else "",
                        digest=chunk[:80],
                    )
                    last_messageid = str(mid)
            else:
                failed_count += 1
                if first_error is None:
                    first_error = res.get("error") or "send failed"

        if succeeded_count and kind == "group" and group_id is not None:
            # Record the bot reply timestamp so the follow-up window kicks in.
            self._policy.record_bot_reply(str(group_id))

        # Match OpenClaw's strict semantics: any sub-message failure makes the
        # whole send a failure. Surface the first error AND the last successful
        # messageid so the agent can decide whether to retry or recall.
        if first_error is not None:
            return SendResult(  # type: ignore[call-arg]
                success=False,
                error=(
                    f"{first_error} (succeeded={succeeded_count}, "
                    f"failed={failed_count} of {len(chunks)} chunks)"
                    if succeeded_count
                    else first_error
                ),
                message_id=last_messageid,
                retryable=True,
            )
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
                chat_id=self._normalize_chat_id(chat_id),
                messageid=str(mid),
                msgseqid=str(res.get("msgseqid") or ""),
                digest="[image]",
            )
        if kind == "group" and group_id is not None:
            self._policy.record_bot_reply(str(group_id))
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
        current_inbound_message_id: str | None = None,
    ) -> "SendResult":  # type: ignore[name-defined]
        """Recall one or more bot-sent messages on ``chat_id``.

        With ``message_id`` set, recalls that single message.
        Without, recalls the ``count`` most recent messages tracked in
        the in-process ``SentMessageStore``.

        ``current_inbound_message_id`` is an optional hint (OpenClaw's
        ``toolContext.currentMessageId``). When omitted, the same value may
        still be supplied via :func:`recall_inbound_message_id_hint_scope`
        (used automatically during webhook dispatch).

        When a hint is present, aggressive guard matches openclaw: it runs
        only if ``message_id`` equals that inbound id. When both the parameter
        and the context hint are omitted, we still look up ``message_id`` as a
        possible inbound id so rare LLM calls that pass only the inbound id can
        be corrected (see tests).

        When a specific ``message_id`` is unknown to the store but a hint
        identifies the current inbound, we fall back to the bot message
        quoted by that inbound (openclaw ``resolveInboundReplyToMessageId``).
        """
        if current_inbound_message_id is None:
            current_inbound_message_id = _recall_inbound_message_hint.get(None)
        kind, group_id, dm_user = self._parse_target(chat_id)
        store_key = self._normalize_chat_id(chat_id)

        # Aggressive guard (openclaw applyAggressiveGuardForInboundMessageId):
        # when a hint is present, only treat ``message_id`` as the inbound id
        # if it matches the hint — avoids false swaps when the LLM passed a
        # legitimate bot id while the hint points at a different message.
        if message_id:
            inbound_key_for_aggressive: str | None = None
            if current_inbound_message_id:
                if message_id == current_inbound_message_id:
                    inbound_key_for_aggressive = current_inbound_message_id
            else:
                inbound_key_for_aggressive = message_id

            corrected: dict[str, Any] | None = None
            if inbound_key_for_aggressive:
                corrected = self._correct_inbound_confusion(
                    inbound_message_id=inbound_key_for_aggressive,
                    store_key=store_key,
                )
            if corrected is not None and corrected.get("kind") == "swap":
                logger.info(
                    "[infoflow:delete] auto-swap inbound id=%s -> bot msg id=%s",
                    message_id, corrected.get("message_id"),
                )
                message_id = str(corrected["message_id"])
            elif corrected is not None and corrected.get("kind") == "drop_to_count":
                logger.info(
                    "[infoflow:delete] auto-correct: drop to count=1 (recall_latest intent)"
                )
                message_id = None
                count = 1

        targets: list[tuple[str, str]] = []  # (messageid, msgseqid)

        if message_id:
            entry = self._sent_store.find(store_key, message_id)
            if kind == "group":
                need_reply_fallback = entry is None or not (entry.msgseqid or "").strip()
            else:
                need_reply_fallback = entry is None

            if need_reply_fallback and current_inbound_message_id:
                fb_entry = self._reply_to_bot_from_current_inbound(
                    current_inbound_message_id=current_inbound_message_id,
                    store_key=store_key,
                )
                if fb_entry is not None:
                    ok_use = (kind != "group") or bool((fb_entry.msgseqid or "").strip())
                    if ok_use:
                        logger.info(
                            "[infoflow:delete] fallback: message_id=%s -> bot id=%s "
                            "(via current inbound reply context)",
                            message_id,
                            fb_entry.messageid,
                        )
                        message_id = fb_entry.messageid
                        entry = fb_entry

            msgseq = (entry.msgseqid if entry else "") or ""
            targets.append((message_id, msgseq))
        else:
            for entry in self._sent_store.recent(store_key, max(1, count)):
                targets.append((entry.messageid, entry.msgseqid))

        if not targets:
            return SendResult(success=False, error=self._no_recall_error(store_key))  # type: ignore[call-arg]

        first_error: str | None = None
        recalled_ids: list[str] = []
        for mid, seq in targets:
            if kind == "group":
                if group_id is None or not seq:
                    if first_error is None:
                        candidates = self._format_recall_candidates(store_key)
                        first_error = (
                            f"messageId={mid} is not a known bot-sent group message "
                            "(msgseqid unavailable). It looks like you may have passed "
                            "an inbound user-message id instead of the bot's."
                            + (f" Recent bot messages here: {candidates}." if candidates else "")
                        )
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
                # Clean up so subsequent count-based recalls don't see this twice.
                try:
                    self._sent_store.remove(store_key, mid)
                except Exception:
                    logger.debug("sent_store.remove failed", exc_info=True)
            elif first_error is None:
                first_error = res.get("error") or "recall failed"

        if not recalled_ids:
            return SendResult(success=False, error=first_error or "recall failed")  # type: ignore[call-arg]
        return SendResult(success=True, message_id=recalled_ids[-1])  # type: ignore[call-arg]

    def _inbound_ctx_account_id(self) -> str:
        """Same id used when registering inbound context in ``_handle_webhook``."""
        return str(self._settings.get("app_key") or "default")

    def _correct_inbound_confusion(
        self,
        *,
        inbound_message_id: str,
        store_key: str,
    ) -> dict[str, Any] | None:
        """Return a correction directive, or None if context isn't actionable.

        ``store_key`` is the already-normalized chat_id (no ``infoflow:``
        prefix). Mirrors
        openclaw-infoflow/src/actions.ts::applyAggressiveGuardForInboundMessageId.
        """
        ctx = _lookup_inbound_context(inbound_message_id)
        if ctx is None:
            return None
        if ctx.account_id != self._inbound_ctx_account_id():
            return None
        # Scope check: same chat target. ``ctx.target`` is always stored in
        # normalized form by ``_handle_webhook``.
        if ctx.target != store_key:
            return None
        # Priority 1: swap to bot-message quote-reply target.
        if ctx.reply_to_bot_message_id and self._sent_store.find(
            store_key, ctx.reply_to_bot_message_id
        ):
            return {"kind": "swap", "message_id": ctx.reply_to_bot_message_id}
        # Priority 2: clear "recall the latest" intent → drop to count=1.
        if _looks_like_recall_latest(ctx.inbound_body):
            return {"kind": "drop_to_count"}
        return None

    def _reply_to_bot_from_current_inbound(
        self,
        *,
        current_inbound_message_id: str,
        store_key: str,
    ) -> SentMessage | None:
        """Resolve a stored bot-sent message from the current inbound's quote-reply.

        Mirrors openclaw-infoflow ``resolveInboundReplyToMessageId`` + store lookup.
        """
        ctx = _lookup_inbound_context(current_inbound_message_id)
        if ctx is None:
            return None
        if ctx.account_id != self._inbound_ctx_account_id():
            return None
        if ctx.target != store_key:
            return None
        bid = ctx.reply_to_bot_message_id
        if not bid:
            return None
        return self._sent_store.find(store_key, bid)

    def _format_recall_candidates(self, store_key: str, limit: int = 5) -> str:
        """Format the last ``limit`` bot-sent messages for an error hint to the LLM."""
        records = self._sent_store.recent(store_key, limit)
        if not records:
            return ""
        return "; ".join(
            f"messageId={r.messageid} preview=\"{r.digest or '(no preview)'}\""
            for r in records
        )

    def _no_recall_error(self, store_key: str) -> str:
        candidates = self._format_recall_candidates(store_key)
        if candidates:
            return (
                "no recent bot messages to recall on this chat. "
                f"Recent bot-sent messages: {candidates}."
            )
        return "no recent bot messages to recall"

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
    """Send a single message without a live adapter (cron child process).

    The result is also persisted to the shared SQLite ``sent-messages.db`` so
    the LIVE adapter (or a later cron run) can find and recall the message
    by id. Without this, cron-sent messages were "invisible" to the recall
    tool — that was Fix #6.
    """
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
    msgseq = res.get("msgseqid") or ""

    # Persist for cross-process recall — same DB the adapter would use.
    # Normalize chat_id so the in-process adapter's lookups still match
    # cron-process inserts even if the caller used an ``infoflow:`` prefix.
    if mid:
        try:
            store = SentMessageStore(
                db_path=Path(settings["state_dir"]) / "infoflow" / "sent-messages.db",
                account_id=settings.get("app_key") or "default",
            )
            store.record(
                chat_id=InfoflowAdapter._normalize_chat_id(chat_id),
                messageid=str(mid),
                msgseqid=str(msgseq) if msgseq else "",
                digest=message[:80],
            )
        except Exception:
            logger.debug("standalone_send: sent-store persist failed", exc_info=True)

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
        ("INFOFLOW_ROBOT_ID", "robot_id"),
        ("INFOFLOW_PORT", "port"),
        ("INFOFLOW_HOST", "host"),
        ("INFOFLOW_WEBHOOK_PATH", "webhook_path"),
        ("INFOFLOW_REPLY_MODE", "reply_mode"),
        ("INFOFLOW_REQUIRE_MENTION", "require_mention"),
        ("INFOFLOW_WATCH_MENTIONS", "watch_mentions"),
        ("INFOFLOW_WATCH_REGEX", "watch_regex"),
        ("INFOFLOW_FOLLOW_UP", "follow_up"),
        ("INFOFLOW_FOLLOW_UP_WINDOW", "follow_up_window"),
        ("INFOFLOW_GROUPS", "groups"),
        ("INFOFLOW_CONNECTION_MODE", "connection_mode"),
        ("HERMES_STATE_DIR", "state_dir"),
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
        "message, not the bot's. (If you do, this tool will auto-correct "
        "to the bot message you quote-replied to, when unambiguous.)"
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
            "current_inbound_message_id": {
                "type": "string",
                "description": (
                    "Optional: the message_id of the inbound message currently "
                    "being processed. When provided, enables auto-correction if "
                    "the LLM accidentally passed this same id as message_id."
                ),
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
        message_id = args.get("message_id") or None
        current_inbound = args.get("current_inbound_message_id") or None
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
        result = await adapter.delete_message(
            target,
            message_id,
            count=count,
            current_inbound_message_id=current_inbound,
        )
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
