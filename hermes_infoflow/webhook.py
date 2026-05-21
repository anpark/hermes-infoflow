"""Webhook channel handler for the infoflow plugin.

Handles the HTTP transport layer — AES decryption, echostr challenge
verification, and request routing.  Delegates field parsing to
``serverapi.to_incoming()`` and business logic to ``bot``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .parser import parse_webhook
from .utils import gw_log

if TYPE_CHECKING:
    from .dashboard import SessionTracker
    from .itypes import IncomingMessage
    from .serverapi import ServerAPI


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebhookResult — structured parse output (unchanged)
# ---------------------------------------------------------------------------


async def _health_handler(_req: Any) -> Any:
    from aiohttp import web
    return web.Response(text="ok")


@dataclass
class WebhookResult:

    kind: str  # "message" | "echostr_ok" | "echostr_bad" | "http_error" | "ignored"
    status: int = 200
    body: str = "OK"
    raw_inbound: Any = None  # parser.InboundMessage when kind == "message"


def parse_webhook_request(
    *,
    content_type: str,
    raw_body: str,
    parser_account: object,
    dedup_set: set[str] | None = None,
) -> WebhookResult:
    """Parse an inbound HTTP webhook request.

    Parameters
    ----------
    content_type:
        Raw ``Content-Type`` header value (case-insensitive matching is
        performed internally by :func:`parser.parse_webhook`).
    raw_body:
        Decoded request body string.
    parser_account:
        A ``parser.AccountConfig``-like object with ``check_token``,
        ``encoding_aes_key``, ``robot_name``, ``app_agent_id``, and
        ``robot_id`` fields.
    dedup_set:
        Shared dedup set consulted by the parser to filter bot-echo
        messages.

    Returns
    -------
    WebhookResult
        * ``kind == "message"``: successful parse, ``raw_inbound`` holds
          the ``parser.InboundMessage`` (convert via ``serverapi.to_incoming()``).
        * Other kinds: HTTP response info (status, body) for the caller
          to return directly.
    """
    parsed = parse_webhook(
        content_type=content_type,
        raw_body=raw_body,
        account=parser_account,  # type: ignore[arg-type]
        sent_message_ids=dedup_set,
    )

    if parsed.kind == "echostr_ok":
        return WebhookResult(kind="echostr_ok", status=200, body=parsed.body)
    if parsed.kind == "echostr_bad":
        return WebhookResult(kind="echostr_bad", status=403, body=parsed.body)
    if parsed.kind == "http_error":
        return WebhookResult(
            kind="http_error",
            status=parsed.status_code,
            body=parsed.body,
        )
    if parsed.kind == "ignored" or parsed.inbound is None:
        return WebhookResult(kind="ignored")

    return WebhookResult(kind="message", raw_inbound=parsed.inbound)


# ---------------------------------------------------------------------------
# WebhookServer — self-contained HTTP transport layer
# ---------------------------------------------------------------------------


class WebhookServer:
    """Self-contained HTTP server for receiving Infoflow webhook callbacks.

    Responsibilities:
    - Manage aiohttp web server lifecycle (start / stop)
    - Receive and parse HTTP webhook requests
    - Handle echostr challenge verification
    - Convert parser output → ``IncomingMessage`` via ``serverapi.to_incoming()``
    - Invoke the ``on_message`` callback for business-layer processing

    Does NOT:
    - Make outbound API calls
    - Hold business logic (policy, dedup, enrich, dispatch)
    """

    def __init__(
        self,
        *,
        serverapi: ServerAPI,
        dedup_set: set[str],
        webhook_path: str,
        host: str,
        port: int,
        body_limit: int,
        on_message: Callable[[IncomingMessage], Awaitable[None]],
        task_set: set[asyncio.Task[Any]] | None = None,
        tracker: SessionTracker | None = None,
    ) -> None:
        self._serverapi = serverapi
        self._dedup_set = dedup_set
        self._tracker = tracker
        self._webhook_path = webhook_path
        self._host = host
        self._port = port
        self._body_limit = body_limit
        self._on_message = on_message
        self._task_set = task_set
        self._runner: Any = None
        self._site: Any = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the webhook HTTP server (idempotent)."""
        from aiohttp import web

        app = web.Application(client_max_size=self._body_limit)
        app.router.add_post(self._webhook_path, self._handle_request)
        app.router.add_get("/health", _health_handler)
        if self._tracker is not None:
            from .dashboard import dashboard_enabled, register_routes

            if dashboard_enabled():
                register_routes(app, self._tracker, base_path=self._webhook_path)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info(
            "[infoflow] Webhook listening on %s:%d%s",
            self._host, self._port, self._webhook_path,
        )

    async def stop(self) -> None:
        """Stop the webhook HTTP server."""
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
        gw_log().info("[infoflow] Webhook server stopped")

    @property
    def is_running(self) -> bool:
        return self._site is not None

    # -- request handling --------------------------------------------------

    async def handle_request(
        self, request: Any,
    ) -> tuple[IncomingMessage | None, Any]:
        """Parse a webhook HTTP request.

        Returns
        -------
        (msg, response)
            * ``msg`` is ``None`` for non-message requests (echostr, error,
              ignored) — the caller should return ``response`` directly.
            * ``msg`` is an ``IncomingMessage`` for valid messages — the
              caller should process it and return ``response``.
        """
        from aiohttp import web

        # 1. Read body
        try:
            raw_bytes = await request.read()
        except Exception as exc:
            gw_log().warning("[infoflow] failed to read webhook body: %s", exc)
            return None, web.Response(status=400, text="bad request")
        raw_body = raw_bytes.decode("utf-8", errors="replace")

        # 2. Transport-level log
        content_type = request.headers.get("Content-Type", "")
        gw_log().info(
            "[infoflow] webhook received: ct=%s body_len=%d ip=%s",
            content_type, len(raw_bytes),
            getattr(request, "remote", None) or "unknown",
        )

        # 3. Protocol parse
        wh_result = parse_webhook_request(
            content_type=content_type,
            raw_body=raw_body,
            parser_account=self._serverapi.parser_account,
            dedup_set=self._dedup_set,
        )

        # 4. Non-message responses (echostr, error, ignored)
        if wh_result.kind == "echostr_ok":
            gw_log().info("[infoflow] webhook echostr verification OK")
            return None, web.Response(
                status=200, text=wh_result.body, content_type="text/plain",
            )
        if wh_result.kind == "echostr_bad":
            gw_log().warning("[infoflow] webhook echostr verification BAD")
            return None, web.Response(status=403, text=wh_result.body)
        if wh_result.kind == "http_error":
            gw_log().warning(
                "[infoflow] webhook parse error (status=%s): %s",
                wh_result.status, wh_result.body,
            )
            return None, web.Response(status=wh_result.status, text=wh_result.body)
        if wh_result.kind != "message":
            return None, web.Response(status=200, text="OK")

        # 5. Convert parser.InboundMessage → types.IncomingMessage
        msg = self._serverapi.to_incoming(wh_result.raw_inbound)

        # 6. [iflow:raw] — protocol-layer log
        try:
            import json as _json

            _raw = (
                wh_result.raw_inbound.raw_msgdata
                if hasattr(wh_result.raw_inbound, "raw_msgdata")
                else {}
            )
            gw_log().info(
                "[iflow:raw] mid=%s payload=%s",
                msg.message_id,
                _json.dumps(_raw, ensure_ascii=False, default=str)[:2000],
            )
        except Exception:
            pass

        return msg, web.Response(status=200, text="OK")

    # -- aiohttp route handler (internal) ----------------------------------

    async def _handle_request(self, request: Any) -> Any:
        """aiohttp route handler — parse, dispatch to on_message, return response."""
        msg, response = await self.handle_request(request)
        if msg is not None and self._on_message is not None:
            task = asyncio.ensure_future(self._on_message(msg))
            if self._task_set is not None:
                self._task_set.add(task)
                task.add_done_callback(self._task_set.discard)
        return response
