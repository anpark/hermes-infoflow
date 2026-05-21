"""Plugin-level tool definitions for hermes-infoflow."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

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
        "撤回你(机器人)之前在如流中发送的消息。"
        "**你只能撤回自己发的消息，不能撤回别人的。**\n\n"
        "`target` 参数指定会话：传入 uuapName（私信）或 `group:<群组ID>`（群聊）。\n\n"
        "两种撤回模式：\n"
        "- 精确撤回：提供 `message_id` 撤回指定消息\n"
        "- 批量撤回：不提供 `message_id`，改为传入 `count` "
        "撤回该会话中你(机器人)最近的 N 条消息\n\n"
        "**message_id 来源：**\n"
        "- ✅ 你发送消息后返回的 ID\n"
        "- ✅ 用户 reply 你的消息时，文本中 `<引用 message_id:xxx>` 里的 xxx"
        "（这是被引用消息的 ID，即你的消息）\n"
        "- ❌ 用户当前消息本身的 inbound ID（那是用户的消息，传入会失败）\n\n"
        "典型场景：用户 reply 了你的一条消息说\"撤回这个\"，"
        "直接从引用标签中取出 message_id 传入即可。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "目标会话。私信传 `infoflow:<uuapName>`，群聊传 `infoflow:group:<群组ID>`",
            },
            "message_id": {
                "type": "string",
                "description": "要撤回的消息 ID（必须是你自己发出消息后返回的 ID）。与 `count` 二选一",
            },
            "count": {
                "type": "integer",
                "description": "撤回该会话中你(机器人)最近发送的 N 条消息（1-10）。省略 `message_id` 时使用",
                "minimum": 1,
                "maximum": 10,
                "default": 1,
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
        "引用回复如流中的某条消息，回复内容会附带原消息的文本预览。"
        "与普通 `send_message` 的区别在于：这里发出的消息会显示"
        "「引用了某条消息」的卡片样式。\n\n"
        "使用场景：\n"
        "- 需要针对特定历史消息进行回应时\n"
        "- 需要让对方明确知道你在回复哪条消息时\n\n"
        "若省略 `reply_to`，自动引用触发本轮对话的那条用户消息。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "目标会话（可选）。私信传 `infoflow:<uuapName>`，"
                    "群聊传 `infoflow:group:<群组ID>`。省略则发送到当前会话"
                ),
            },
            "message": {
                "type": "string",
                "description": "回复的消息正文内容，支持 Markdown",
            },
            "reply_to": {
                "type": "string",
                "description": "要引用的消息 ID（可选）。省略时自动引用触发本轮的用户消息",
            },
            "reply_type": {
                "type": "string",
                "enum": ["1", "2"],
                "description": (
                    "`1` = 回复并通知（默认，对方收到通知，显示引用原文预览）；"
                    "`2` = 仅引用（显示引用原文预览，但对方不收到通知）"
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
        try:
            count = int(args.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        if not target:
            return tool_result_json({"error": "target is required"})

        # Strip ``infoflow:`` prefix if present (adapter expects raw chat_id).
        if target.startswith("infoflow:"):
            target = target[len("infoflow:"):]

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
        ))
        if not result.success:
            return tool_result_json({"error": result.error or "recall failed"})
        mid = result.message_id
        return tool_result_json({
            "success": True,
            "message_id": str(mid) if mid is not None else None,
        })

    return _handler
