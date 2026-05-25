"""Plugin-level tool definitions for hermes-infoflow."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .llm_format import format_created_time_ms, format_dm_record, format_group_record
from .prompt_rules import INFOFLOW_DELIVERY_TOOL_RULES, delivery_success_hint

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
        "- ✅ 用户 reply 你的消息时，文本中 "
        "`<Quote message_id:'xxx'; sender:'bot:...'>...</Quote>` 里的 xxx"
        "（这是被引用消息的 ID，即你的消息；传参时不要带引号）\n"
        "- ❌ 用户当前消息本身的 inbound ID（那是用户的消息，传入会失败）\n\n"
        "典型场景：用户 reply 了你的一条消息说\"撤回这个\"，"
        "直接从引用标签中取出 message_id 传入即可。\n\n"
        "**最终回复规则：**撤回成功且用户只要求撤回时，最终输出必须是单独一行 "
        "`NO_REPLY`；如果同一条用户消息还要求其它任务，只回复其它任务结果，"
        "不要说\"已撤回\"或\"撤回成功\"。只有撤回失败时才说明失败。"
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


def tool_result_json(payload: Any) -> str:
    """Serialize a tool handler result for Chat Completions tool messages.

    OpenAI-compatible APIs require tool message ``content`` to be a string,
    not a JSON object. Hermes CLI may stringify for you; the gateway path
    persists the handler return value as-is.
    """
    return json.dumps(payload, ensure_ascii=False)


def recall_tool_success_payload(
    *,
    target: str,
    requested_message_id: str | None,
    count: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": True,
        "action": "recall_message",
        "status": "recalled",
        "target": target,
        "count": count,
        "final_response": {
            "mode": "silent_if_only_task",
            "content": "NO_REPLY",
            "if_other_tasks": "answer_only_other_tasks_without_recall_confirmation",
        },
    }
    if requested_message_id:
        payload["requested_message_id"] = str(requested_message_id)
    return payload


def recall_tool_error_payload(
    error: str,
    *,
    target: str | None = None,
    requested_message_id: str | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "action": "recall_message",
        "status": "failed",
        "error": error,
        "final_response": {
            "mode": "report_failure",
            "content": "撤回失败，消息可能已过期。",
        },
    }
    if target:
        payload["target"] = target
    if requested_message_id:
        payload["requested_message_id"] = str(requested_message_id)
    if count is not None:
        payload["count"] = count
    return payload


# Schema for the agent-callable infoflow_reply tool.
REPLY_TOOL_SCHEMA = {
    "name": "infoflow_reply",
    "description": (
        "引用回复如流中的某条消息，回复内容会附带原消息的文本预览。"
        "与普通 `send_message` 的区别在于：这里发出的消息会显示"
        "「引用了某条消息」的卡片样式。\n\n"
        "发送本地图片时，可在 `message` 中加入 `MEDIA:<本地图片绝对路径>`；"
        "工具会读取图片字节并调用如流原生图片消息 API，不会把本地路径作为正文发出。"
        "不要把 `MEDIA:` 或本地文件路径当普通文本发送。\n\n"
        f"{INFOFLOW_DELIVERY_TOOL_RULES}\n\n"
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
                "description": (
                    "回复的消息正文内容，支持 Markdown。需要发送本地图片时追加 "
                    "`MEDIA:<本地图片绝对路径>`；工具会按图片字节发送，不会发送路径文本。"
                ),
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


# Schema for the agent-callable infoflow_get_group_members tool.
GROUP_MEMBERS_TOOL_SCHEMA = {
    "name": "infoflow_get_group_members",
    "description": (
        "获取如流群聊的成员列表，返回人类成员与机器人成员。"
        "结果最多每 3 秒刷新一次（并发请求会合并为同一次远端拉取）。\n\n"
        "字段用途：\n"
        "- 人类 `user_id`（uuapName）：文本 `@uuapName` 或 "
        "`metadata.mention_user_ids`；只有本地 participants 中已有可信真名时才返回 `name`\n"
        "- 机器人 `agent_id` + `name`：文本 `@显示名` / `@agentId` 或 "
        "`metadata.mention_agent_ids`\n"
        "- 不返回 Infoflow `imid` 等内部服务 ID"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "integer",
                "description": "群聊 ID，例如 4507088",
            },
        },
        "required": ["group_id"],
    },
}


# Schema for the agent-callable infoflow_get_message_history tool.
HISTORY_TOOL_SCHEMA = {
    "name": "infoflow_get_message_history",
    "description": (
        "获取当前如流会话或指定如流会话的历史消息。"
        "成功和失败都返回 JSON 字符串；成功时是 JSON 数组字符串，"
        "每项包含 `time` 和 `content`。`content` 与当前 User Message 的"
        "结构化 envelope 一致，但不包含 Unread Message Context / Handling Strategy。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "可选。目标会话。群聊传 `infoflow:group:<群组ID>` 或 "
                    "`group:<群组ID>`；私聊传 `infoflow:<user_id>` 或 `<user_id>`。"
                    "省略时查询当前会话。非 admin 只能查询当前会话。"
                ),
            },
            "start_time": {
                "type": "string",
                "description": (
                    "可选。开始时间，严格格式 `YYYY.MM.DD HH.mm.ss`，"
                    "例如 `2025.05.21 19.56.59`。按包含计算。"
                ),
            },
            "end_time": {
                "type": "string",
                "description": (
                    "可选。结束时间，严格格式 `YYYY.MM.DD HH.mm.ss`，"
                    "例如 `2025.05.21 19.56.59`。按包含计算。"
                ),
            },
            "message_id": {
                "type": "string",
                "description": (
                    "可选。按消息 ID 查询单条消息，或作为窗口查询锚点。"
                    "提供后优先使用 message_id 模式，忽略 start_time/end_time。"
                    "只提供 message_id 时返回锚点消息本身；配合 before_count/after_count "
                    "使用时返回窗口：before_count 条锚点前消息 + 锚点消息本身 + "
                    "after_count 条锚点后消息。before_count/after_count 的计数"
                    "不包含锚点，但返回结果包含锚点消息。"
                ),
            },
            "before_count": {
                "type": "integer",
                "description": "配合 message_id 使用，返回锚点之前的消息条数；计数不包含锚点，但结果包含锚点。",
                "minimum": 0,
                "maximum": 100,
                "default": 0,
            },
            "after_count": {
                "type": "integer",
                "description": "配合 message_id 使用，返回锚点之后的消息条数；计数不包含锚点，但结果包含锚点。",
                "minimum": 0,
                "maximum": 100,
                "default": 0,
            },
            "limit": {
                "type": "integer",
                "description": "时间范围或最近历史查询的最大返回条数，默认 20，最大 100。",
                "minimum": 1,
                "maximum": 100,
                "default": 20,
            },
        },
    },
}


def _serialize_group_members_payload(
    members: list[Any],
    group_id: str,
    *,
    source: str | None = None,
    stale: bool = False,
    trusted_user_name_lookup: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Build the tool JSON payload from normalized GroupMember objects."""
    users: list[dict[str, Any]] = []
    bots: list[dict[str, Any]] = []
    for m in members:
        if m.is_bot:
            bot: dict[str, Any] = {
                "agent_id": int(m.agent_id or 0),
                "name": m.name or "",
            }
            bots.append(bot)
        else:
            uid = str(m.uid or "")
            user = {"user_id": uid}
            if trusted_user_name_lookup is not None and uid:
                name = str(trusted_user_name_lookup(uid) or "").strip()
                if name:
                    user["name"] = name
            users.append(user)
    payload: dict[str, Any] = {
        "success": True,
        "group_id": str(group_id),
        "users": users,
        "bots": bots,
        "counts": {
            "users": len(users),
            "bots": len(bots),
            "total": len(users) + len(bots),
        },
    }
    if source:
        payload["source"] = source
    if stale:
        payload["stale"] = True
    return payload


# ---------------------------------------------------------------------------
# History tool helpers
# ---------------------------------------------------------------------------


_HISTORY_DATETIME_RE = re.compile(
    r"^\s*(\d{4})\.(\d{2})\.(\d{2}) (\d{2})\.(\d{2})\.(\d{2})\s*$"
)


def _json_error(message: str) -> str:
    return tool_result_json({"success": False, "error": message})


_MEDIA_DIRECTIVE_RE = re.compile(
    r'''[`"']?MEDIA:\s*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|[^\s`"']+)[`"']?'''
)


def _strip_media_directives(text: str) -> str:
    cleaned = _MEDIA_DIRECTIVE_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _extract_reply_media(message: str) -> tuple[list[tuple[str, bool]], str, bool]:
    """Extract media paths from an infoflow_reply body.

    Returns ``(media_files, cleaned_text, malformed_media_directive)``. Any
    ``MEDIA:`` text must be consumed or rejected so local paths cannot leak as
    markdown if Hermes' full extractor is unavailable or cannot parse it.
    """
    if "MEDIA:" not in message:
        return [], message, False

    try:
        from gateway.platforms.base import BasePlatformAdapter  # type: ignore[import-not-found]

        media_files, cleaned = BasePlatformAdapter.extract_media(message)
    except Exception:
        media_files = []
        has_voice_tag = "[[audio_as_voice]]" in message
        cleaned = message.replace("[[audio_as_voice]]", "").replace("[[as_document]]", "")
        for match in _MEDIA_DIRECTIVE_RE.finditer(message):
            path = match.group("path").strip()
            if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
                path = path[1:-1].strip()
            path = path.lstrip("`\"'").rstrip("`\"',.;:)}]")
            if path:
                media_files.append((os.path.expanduser(path), has_voice_tag))
        cleaned = _strip_media_directives(cleaned) if media_files else cleaned

    cleaned = _strip_media_directives(cleaned) if "MEDIA:" in cleaned else cleaned.strip()
    malformed = "MEDIA:" in message and not media_files
    return media_files, cleaned, malformed


def _sanitize_media_error(error: Any, media_files: list[tuple[str, bool]]) -> str:
    text = str(error or "image reply failed")
    for raw_path, _is_voice in media_files:
        raw = str(raw_path or "")
        if raw:
            text = text.replace(raw, "[local image path]")
            text = text.replace(os.path.expanduser(raw), "[local image path]")
    if "MEDIA:" in text:
        text = _MEDIA_DIRECTIVE_RE.sub("MEDIA:[local image path]", text)
    return text


async def _preflight_reply_media(adapter: Any, media_files: list[tuple[str, bool]]) -> str | None:
    load_image_bytes = getattr(adapter, "_load_image_bytes", None)
    if not callable(load_image_bytes):
        return None

    from .media import prepare_infoflow_image_bytes  # noqa: E402
    from .utils import _ImageLoadError  # noqa: E402

    for media_path, is_voice in media_files:
        if is_voice:
            return "infoflow_reply only supports image MEDIA attachments"
        try:
            raw = await load_image_bytes(str(media_path))
            prepare_infoflow_image_bytes(raw)
        except _ImageLoadError as exc:
            return _sanitize_media_error(exc, media_files)
        except Exception as exc:
            return _sanitize_media_error(exc, media_files)
    return None


async def _send_reply_media(
    *,
    adapter: Any,
    target: str,
    message: str,
    reply_to: str,
    reply_type: str,
) -> str | None:
    media_files, cleaned_message, malformed_media = _extract_reply_media(message)
    if malformed_media:
        return tool_result_json({
            "error": (
                "MEDIA directive must point to a supported local image path; "
                "not sending local path text"
            )
        })
    if not media_files:
        return None

    preflight_error = await _preflight_reply_media(adapter, media_files)
    if preflight_error:
        return tool_result_json({"error": preflight_error})

    sent_ids: list[str] = []
    last_message_id: str | None = None
    for index, (media_path, _is_voice) in enumerate(media_files):
        caption = cleaned_message if index == 0 and cleaned_message else None
        result = await _with_temp_session(adapter, adapter.send_image_file(
            chat_id=target,
            image_path=str(media_path),
            caption=caption,
            reply_to=reply_to,
            metadata={"reply_type": reply_type},
        ))
        if not result.success:
            return tool_result_json({
                "error": _sanitize_media_error(
                    result.error or "image reply failed",
                    media_files,
                )
            })
        for mid in tuple(getattr(result, "continuation_message_ids", ()) or ()):
            if mid:
                sent_ids.append(str(mid))
        if result.message_id:
            last_message_id = str(result.message_id)
            sent_ids.append(last_message_id)

    payload: dict[str, Any] = {
        "success": True,
        "message_id": last_message_id,
        "media_count": len(media_files),
    }
    payload.update(delivery_success_hint())
    if sent_ids:
        payload["message_ids"] = sent_ids
    return tool_result_json(payload)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(minimum, min(maximum, n))


def _parse_history_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    match = _HISTORY_DATETIME_RE.match(raw)
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
        )
    except ValueError:
        return None


def _to_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def _history_bounds(args: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    start_text = str(args.get("start_time") or "").strip()
    end_text = str(args.get("end_time") or "").strip()

    if str(args.get("date") or "").strip():
        return None, None, "date is not supported; use start_time/end_time in YYYY.MM.DD HH.mm.ss format"

    start_dt = _parse_history_datetime(start_text) if start_text else None
    end_dt = _parse_history_datetime(end_text) if end_text else None
    if start_text and start_dt is None:
        return None, None, "start_time must use format YYYY.MM.DD HH.mm.ss"
    if end_text and end_dt is None:
        return None, None, "end_time must use format YYYY.MM.DD HH.mm.ss"
    start_ms = _to_ms(start_dt)
    end_ms = _to_ms(end_dt)
    if end_ms is not None:
        end_ms += 1000
    return start_ms, end_ms, None


def _format_history_time(ms: int) -> str:
    return format_created_time_ms(ms)


def _parse_history_target(target: Any) -> tuple[str, str, str, str] | None:
    raw = str(target or "").strip()
    if not raw:
        return None
    if raw.startswith("infoflow:"):
        raw = raw[len("infoflow:"):]
    if raw.startswith("group:"):
        group_id = raw[len("group:"):].strip()
        if not group_id:
            return None
        return "group", group_id, "", f"group:{group_id}"
    if raw.startswith("dm:user:"):
        user_id = raw[len("dm:user:"):].strip()
    elif raw.startswith("user:"):
        user_id = raw[len("user:"):].strip()
    else:
        user_id = raw
    if not user_id:
        return None
    return "dm", "", user_id, f"dm:user:{user_id}"


def _target_from_record(record: Any) -> tuple[str, str, str, str]:
    group_id = str(getattr(record, "group_id", "") or "")
    if group_id:
        return "group", group_id, "", f"group:{group_id}"
    peer = str(getattr(record, "peer", "") or "")
    user_id = peer.removeprefix("user:")
    return "dm", "", user_id, f"dm:user:{user_id}"


def _same_target(a: tuple[str, str, str, str], b: tuple[str, str, str, str]) -> bool:
    return a[0] == b[0] and a[3] == b[3]


def _record_is_admin(record: Any, admin_uid: str) -> bool:
    admin = str(admin_uid or "").strip().lower()
    if not admin:
        return False
    sender = str(getattr(record, "sender", "") or "").strip().lower()
    return sender == f"user:{admin}"


def _records_to_history_payload(adapter: Any, records: list[Any]) -> list[dict[str, str]]:
    admin_uid = str(getattr(adapter, "_admin_uid", "") or "")
    lookup = getattr(adapter, "_participant_name_for_key", None)
    if not callable(lookup):
        lookup = None
    payload: list[dict[str, str]] = []
    for record in records:
        if str(getattr(record, "group_id", "") or ""):
            content = format_group_record(
                record,
                sender_name_lookup=lookup,
                admin_uid=admin_uid,
            )
        else:
            content = format_dm_record(
                record,
                sender_name_lookup=lookup,
                admin_uid=admin_uid,
            )
        payload.append({
            "time": _format_history_time(int(getattr(record, "created_time", 0) or 0)),
            "content": content,
        })
    return payload


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

        media_result = await _send_reply_media(
            adapter=adapter,
            target=target,
            message=message,
            reply_to=reply_to,
            reply_type=reply_type,
        )
        if media_result is not None:
            return media_result

        result = await _with_temp_session(adapter, adapter.send(
            chat_id=target,
            content=message,
            reply_to=reply_to,
            metadata={"reply_type": reply_type},
        ))

        if not result.success:
            return tool_result_json({"error": result.error or "reply failed"})
        payload = {
            "success": True,
            "message_id": str(result.message_id) if result.message_id else None,
        }
        payload.update(delivery_success_hint())
        return tool_result_json(payload)

    return _handler


def make_recall_handler():
    """Build the ``infoflow_recall_message`` tool handler.

    Resolves the live adapter via the platform registry so we can reach
    its in-memory ``SentMessageStore``. Returns a JSON string with explicit
    success/failure status and final-response guidance for the model.
    """

    async def _handler(args: dict, **_kwargs) -> str:
        target = args.get("target")
        message_id = args.get("message_id") or None
        try:
            count = int(args.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        if not target:
            return tool_result_json(recall_tool_error_payload(
                "target is required",
                requested_message_id=message_id,
                count=count,
            ))

        # Strip ``infoflow:`` prefix if present (adapter expects raw chat_id).
        if target.startswith("infoflow:"):
            target = target[len("infoflow:"):]

        adapter = _get_live_adapter()
        if adapter is None:
            return tool_result_json(recall_tool_error_payload(
                (
                    "Infoflow adapter not running in this process — cross-process "
                    "recall is only supported with an explicit message_id (use the "
                    "send_message tool's last-known id)."
                ),
                target=target,
                requested_message_id=message_id,
                count=count,
            ))

        result = await _with_temp_session(adapter, adapter.delete_message(
            target,
            message_id,
            count=count,
        ))
        if not result.success:
            return tool_result_json(recall_tool_error_payload(
                result.error or "recall failed",
                target=target,
                requested_message_id=message_id,
                count=count,
            ))
        return tool_result_json(recall_tool_success_payload(
            target=target,
            requested_message_id=message_id,
            count=count,
        ))

    return _handler


def make_group_members_handler():
    """Build the ``infoflow_get_group_members`` tool handler."""

    async def _handler(args: dict, **_kwargs) -> str:
        raw_gid = args.get("group_id")
        if raw_gid is None or raw_gid == "":
            return tool_result_json({"error": "group_id is required"})
        try:
            group_id = int(raw_gid)
        except (TypeError, ValueError):
            return tool_result_json({"error": "group_id must be an integer"})

        adapter = _get_live_adapter()
        if adapter is None:
            return tool_result_json({"error": (
                "Infoflow adapter not running — cannot fetch group members."
            )})

        from .serverapi import GroupMembersFetchStatus  # noqa: E402

        fetch_result = await adapter._serverapi.fetch_group_members_detailed(
            str(group_id),
            force_refresh=True,
        )
        if fetch_result.status == GroupMembersFetchStatus.FAILED:
            return tool_result_json({
                "error": (
                    fetch_result.error
                    or "failed to fetch group members"
                ),
            })

        payload = _serialize_group_members_payload(
            fetch_result.members,
            str(group_id),
            source=fetch_result.status.value,
            stale=fetch_result.status == GroupMembersFetchStatus.OK_STALE,
            trusted_user_name_lookup=_trusted_user_name_lookup(adapter),
        )
        return tool_result_json(payload)

    return _handler


def make_history_handler():
    """Build the ``infoflow_get_message_history`` tool handler."""

    async def _handler(args: dict, **_kwargs) -> str:
        adapter = _get_live_adapter()
        if adapter is None:
            return _json_error("Infoflow adapter not running — cannot read message history.")
        store = getattr(adapter, "_message_store", None)
        if store is None:
            return _json_error("Infoflow message store is unavailable.")

        from .bot import get_recall_inbound_message_id_hint  # noqa: E402

        current_message_id = get_recall_inbound_message_id_hint() or ""
        current_record = store.find_any(current_message_id) if current_message_id else None
        current_target = _target_from_record(current_record) if current_record else None
        admin_uid = str(getattr(adapter, "_admin_uid", "") or "")
        current_is_admin = bool(current_record and _record_is_admin(current_record, admin_uid))

        explicit_target = _parse_history_target(args.get("target"))
        if args.get("target") and explicit_target is None:
            return _json_error("target must be infoflow:group:<id>, group:<id>, or a user_id")

        message_id = str(args.get("message_id") or "").strip()
        before_count = _clamp_int(args.get("before_count", 0), 0, 0, 100)
        after_count = _clamp_int(args.get("after_count", 0), 0, 0, 100)
        limit = _clamp_int(args.get("limit", 20), 20, 1, 100)
        start_ms = end_ms = None
        if not message_id:
            start_ms, end_ms, bounds_error = _history_bounds(args)
            if bounds_error:
                return _json_error(bounds_error)
            if start_ms is not None and end_ms is not None and end_ms <= start_ms:
                return _json_error("end_time must be later than start_time")

        target = explicit_target or current_target
        if target is None and not message_id:
            return _json_error(
                "No current Infoflow context. Provide target during an Infoflow turn."
            )
        if explicit_target is not None:
            if current_record is None:
                return _json_error(
                    "Current Infoflow message context is required to authorize target access."
                )
            if current_target is not None and not _same_target(explicit_target, current_target):
                if not current_is_admin:
                    return _json_error("Only admin can query history outside the current conversation.")

        records: list[Any]
        if message_id:
            anchor = store.find_any(message_id)
            if anchor is None:
                return tool_result_json([])
            anchor_target = _target_from_record(anchor)
            if target is not None and not _same_target(anchor_target, target):
                return _json_error("message_id does not belong to target")
            if current_target is not None and not _same_target(anchor_target, current_target):
                if not current_is_admin:
                    return _json_error("Only admin can query history outside the current conversation.")
            elif current_record is None:
                return _json_error(
                    "Current Infoflow message context is required to authorize message_id lookup."
                )

            if anchor_target[0] == "group":
                records = store.group_window_around(
                    anchor,
                    before_count=before_count,
                    after_count=after_count,
                )
            else:
                records = store.dm_window_around(
                    anchor,
                    before_count=before_count,
                    after_count=after_count,
                )
            return tool_result_json(_records_to_history_payload(adapter, records))

        assert target is not None
        kind, group_id, dm_user, _chat_key = target
        if kind == "group":
            if start_ms is not None or end_ms is not None:
                records = store.query_group_range(
                    group_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    limit=limit,
                )
            else:
                records = list(reversed(store.recent_group(group_id, limit=limit)))
        else:
            if start_ms is not None or end_ms is not None:
                records = store.query_dm_range(
                    dm_user,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    limit=limit,
                )
            else:
                records = list(reversed(store.recent_dm(dm_user, limit=limit)))
        return tool_result_json(_records_to_history_payload(adapter, records))

    return _handler


def _trusted_user_name_lookup(adapter: Any) -> Callable[[str], str | None] | None:
    """Return a participants-backed human name lookup, if the live adapter has one."""
    store = getattr(adapter, "_message_store", None)
    finder = getattr(store, "find_user_by_user_id", None)
    if not callable(finder):
        return None

    def _lookup(user_id: str) -> str | None:
        rec = finder(user_id)
        return str(getattr(rec, "name", "") or "").strip() or None

    return _lookup
