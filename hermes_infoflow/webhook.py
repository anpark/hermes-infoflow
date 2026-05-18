"""Webhook channel handler for the infoflow plugin.

Handles the HTTP transport layer — AES decryption, echostr challenge
verification, and request routing.  Delegates field parsing to
``serverapi.to_incoming()`` and business logic to ``bot``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .parser import parse_webhook

if TYPE_CHECKING:
    from .parser import InboundMessage


@dataclass
class WebhookResult:
    """Structured result from parsing a webhook HTTP request."""

    kind: str  # "message" | "echostr_ok" | "echostr_bad" | "http_error" | "ignored"
    status: int = 200
    body: str = "OK"
    raw_inbound: InboundMessage | None = None  # Set when kind == "message"


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
