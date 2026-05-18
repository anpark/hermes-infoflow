"""Plugin-level tool definitions for hermes-infoflow."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .adapter import InfoflowAdapter

logger = logging.getLogger("infoflow.tools")


# ---------------------------------------------------------------------------
# Shared session lock for cross-event-loop tool handlers
# ---------------------------------------------------------------------------

# A single module-level lock prevents reply *and* recall handlers from
# clobbering each other's saved/restored ``adapter._http_session``.
_ADAPTER_SESSION_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Shared helper: resolve the live InfoflowAdapter instance
# ---------------------------------------------------------------------------


def _get_live_adapter() -> Any | None:
    """Return the running InfoflowAdapter (or None)."""
    try:
        from gateway.run import _gateway_runner_ref  # type: ignore[import-not-found]
        runner = _gateway_runner_ref()
    except Exception:
        return None

    if runner is None:
        return None
    try:
        from gateway.config import Platform  # type: ignore[import-not-found]
        from .adapter import InfoflowAdapter as _IA
        adapter = runner.adapters.get(Platform("infoflow"))
        if not isinstance(adapter, _IA):
            return None
        return adapter
    except Exception:
        return None


async def _with_temp_session(adapter: Any, coro):
    """Run *coro* with adapter._http_session temporarily nulled.

    This works around the cross-event-loop aiohttp limitation: tool
    handlers run on ``worker_loop``, but the adapter's session is bound
    to the main gateway loop.
    """
    async with _ADAPTER_SESSION_LOCK:
        saved_session = getattr(adapter, "_http_session", None)
        adapter._http_session = None
        try:
            return await coro
        finally:
            adapter._http_session = saved_session


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

# Schema for the agent-callable infoflow_recall_message tool.
RECALL_TOOL_SCHEMA = {
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


def tool_result_json(payload: dict[str, Any]) -> str:
    """Serialize a tool handler result for Chat Completions tool messages.

    OpenAI-compatible APIs require tool message ``content`` to be a string,
    not a JSON object. Hermes CLI may stringify for you; the gateway path
    persists the handler return value as-is.
    """
    return json.dumps(payload, ensure_ascii=False)


# Schema for the agent-callable infoflow_reply tool.
REPLY_TOOL_SCHEMA = {
    "name": "infoflow_reply",
    "description": (
        "Reply to or quote a specific Infoflow message with a preview摘要 of the "
        "original message. Unlike send_message, this tool supports replying to a "
        "specific message (the preview shows the original message text). "
        "If `reply_to` is omitted, automatically replies to the current inbound "
        "message that triggered this turn."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Chat target in infoflow format: uuapName (DM) or group:<id> (group). "
                    "Omit to send to the home channel."
                ),
            },
            "message": {
                "type": "string",
                "description": "The reply text to send.",
            },
            "reply_to": {
                "type": "string",
                "description": (
                    "The Infoflow message_id to reply/quote. "
                    "If omitted, automatically replies to the inbound user message that "
                    "triggered this turn."
                ),
            },
            "reply_type": {
                "type": "string",
                "enum": ["1", "2"],
                "description": (
                    "'1' = reply (引用回复, default), '2' = quote (仅引用, no notification). "
                    "Both show the original message preview."
                ),
                "default": "1",
            },
        },
        "required": ["message"],
    },
}


# ---------------------------------------------------------------------------
# Tool handler factories
# ---------------------------------------------------------------------------


def make_reply_handler():
    """Build the ``infoflow_reply`` tool handler.

    Mirrors the pattern of ``make_recall_handler``: resolves the live adapter
    via the platform registry, handles cross-event-loop aiohttp sessions,
    and returns a JSON string result.
    """

    async def _handler(args: dict, **_kwargs) -> str:
        message = args.get("message", "")
        target = args.get("target")  # may be None → home channel
        reply_to = args.get("reply_to") or None
        reply_type = args.get("reply_type", "1")

        if not message:
            return tool_result_json({"error": "message is required"})

        # If no explicit reply_to, fall back to current inbound message_id hint.
        if not reply_to:
            from .bot import _recall_hint as _recall_inbound_message_hint  # noqa: E402

            reply_to = _recall_inbound_message_hint.get(None) or ""

        if not reply_to:
            return tool_result_json({"error": (
                "No message to reply to. Provide `reply_to`, or this tool "
                "only works when invoked during a webhook-triggered turn "
                "(where the inbound message_id is available automatically)."
            )})

        adapter = _get_live_adapter()
        if adapter is None:
            return tool_result_json({"error": (
                "Infoflow adapter not running — cannot reply."
            )})

        # Use home channel if target not specified.
        if not target:
            try:
                from gateway.run import _gateway_runner_ref  # type: ignore[import-not-found]
                runner = _gateway_runner_ref()
                if runner and hasattr(runner, "config"):
                    home = getattr(runner.config, "infoflow_home_channel", None)
                    if home:
                        target = f"infoflow:{home}"
            except Exception:
                pass
        if not target:
            return tool_result_json({"error": "target is required (or set INFOFLOW_HOME_CHANNEL)"})

        # Strip ``infoflow:`` prefix if present (adapter expects raw chat_id).
        if target.startswith("infoflow:"):
            target = target[len("infoflow:"):]

        result = await _with_temp_session(adapter, adapter.send(
            chat_id=target,
            content=message,
            reply_to=reply_to,
            metadata={"reply_type": reply_type},
        ))

        if not result.success:
            return tool_result_json({"error": result.error or "reply failed"})
        return tool_result_json({
            "success": True,
            "message_id": str(result.message_id) if result.message_id else None,
        })

    return _handler


def make_recall_handler():
    """Build the ``infoflow_recall_message`` tool handler.

    Resolves the live adapter via the platform registry so we can reach
    its in-memory ``SentMessageStore``. Returns a JSON string with
    ``{"error": ...}`` or ``{"success": true, "message_id": ...}``.
    """

    async def _handler(args: dict, **_kwargs) -> str:
        target = args.get("target")
        message_id = args.get("message_id") or None
        current_inbound = args.get("current_inbound_message_id") or None
        try:
            count = int(args.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        if not target:
            return tool_result_json({"error": "target is required"})

        adapter = _get_live_adapter()
        if adapter is None:
            return tool_result_json({"error": (
                "Infoflow adapter not running in this process — cross-process "
                "recall is only supported with an explicit message_id (use the "
                "send_message tool's last-known id)."
            )})

        result = await _with_temp_session(adapter, adapter.delete_message(
            target,
            message_id,
            count=count,
            current_inbound_message_id=current_inbound,
        ))
        if not result.success:
            return tool_result_json({"error": result.error or "recall failed"})
        mid = result.message_id
        return tool_result_json({
            "success": True,
            "message_id": str(mid) if mid is not None else None,
        })

    return _handler
