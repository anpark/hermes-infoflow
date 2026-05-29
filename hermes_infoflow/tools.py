"""Plugin-level tool definitions for hermes-infoflow."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .llm_format import format_created_time_ms, format_dm_record, format_group_record
from .media import prepare_infoflow_image_bytes
from .prompt_rules import INFOFLOW_DELIVERY_TOOL_RULES
from .send_service import InfoflowSendService
from .settings import parse_infoflow_admin_users
from .utils import _ImageLoadError

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


def _cron_auto_delivery_target() -> dict[str, str | None] | None:
    """Return the active cron auto-delivery target, if this is a cron run."""
    try:
        from gateway.session_context import get_session_env  # type: ignore[import-not-found]
    except Exception:
        get_value = os.getenv
    else:
        get_value = get_session_env

    platform = (
        str(get_value("HERMES_CRON_AUTO_DELIVER_PLATFORM", "") or "")
        .strip()
        .lower()
    )
    chat_id = str(get_value("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "") or "").strip()
    thread_id = (
        str(get_value("HERMES_CRON_AUTO_DELIVER_THREAD_ID", "") or "").strip()
        or None
    )
    if not platform or not chat_id:
        return None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }


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


# Schema for the agent-callable infoflow_send_message tool.
SEND_MESSAGE_TOOL_SCHEMA = {
    "name": "infoflow_send_message",
    "description": (
        "向如流私聊或群聊发送消息。`target` 必填。"
        "支持正文、Markdown 倾向、链接、图片、群聊 @、引用消息，"
        "以及这些能力的组合。\n\n"
        "当前会话普通文字回复通常直接输出最终回复；需要指定 target、"
        "跨会话发送、发送图片/链接、群聊 @、引用消息，或控制图文顺序时使用本工具。\n\n"
        "目标：群聊用 `group:<群组ID>` 或纯数字群 ID；私聊用 "
        "`user:<uuapName>` 或 `<uuapName>`，可加 `infoflow:` 前缀。"
        "`bot:<agentId>` 不能作为私聊 target。\n\n"
        "`format` 默认 `auto`，通常不用传。`message` 是正文，可包含 "
        "`MEDIA:<本地图片绝对路径>` 控制图文顺序；只发送链接、图片、"
        "群聊 @ 或引用时，`message` 可为空字符串。\n\n"
        "`message` 支持 Markdown 语法；普通正文保持 `format=auto` 即可。\n\n"
        "链接：`links` 支持 URL、`[展示文本](URL)`、`{href, label}`，"
        "可单独发送或与正文/图片/@/引用组合。\n\n"
        "图片：`MEDIA:<本地图片绝对路径>` 写在 `message` 中可控制图文顺序；"
        "`image_paths` 会追加到 `message` 之后。重复路径会去重，"
        "不会把本地路径作为正文发出。\n\n"
        "引用消息：`reply_to` 传 message_id、`{message_id, preview}`，"
        "或这些值的数组。引用整条消息时只传 message_id；"
        "只想展示原文中的某一句或某一段时，传 `{message_id, preview}`，"
        "preview 填该片段。群聊一次最多引用一条，私聊可引用多条。\n\n"
        "群聊 @：正文写 `@uuapName`、`@agentId`、`@all`，或使用 "
        "`at_all`、`mention_user_ids`、`mention_agent_ids`。"
        "私聊没有真正 @，正文 `@xxx` 按普通文本展示。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "必填。发送目标。群聊：`group:4507088` 或 `4507088`；"
                    "私聊：`user:chengbo05` 或 `chengbo05`；均可加 "
                    "`infoflow:` 前缀。"
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "消息正文。支持 Markdown 语法，可包含 "
                    "`MEDIA:<本地图片绝对路径>` 控制图文顺序；只发送"
                    "链接、图片、群聊 @ 或引用时可为空字符串。"
                ),
            },
            "format": {
                "type": "string",
                "enum": ["auto", "text", "markdown"],
                "description": (
                    "默认 `auto`，通常不用传。`auto` 会让普通正文优先 "
                    "Markdown 展示，并在引用、链接、图片、群聊 @ 等组合下"
                    "自动选择可正常展示的发送格式。"
                ),
                "default": "auto",
            },
            "image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "可选。本地图片绝对路径列表；按列表顺序追加到 message "
                    "中的文本/inline MEDIA 之后发送。"
                ),
                "default": [],
            },
            "links": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "properties": {
                                "href": {"type": "string"},
                                "label": {"type": "string"},
                            },
                            "required": ["href"],
                        },
                    ]
                },
                "description": (
                    "可点击链接列表。支持 URL、`[展示文本](URL)`、"
                    "`{href, label}`；可单独发送或与正文、图片、群聊 @、引用组合。"
                ),
                "default": [],
            },
            "reply_to": {
                "anyOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {
                            "message_id": {"type": "string"},
                            "preview": {"type": "string"},
                        },
                        "required": ["message_id"],
                    },
                    {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "message_id": {"type": "string"},
                                        "preview": {"type": "string"},
                                    },
                                    "required": ["message_id"],
                                },
                            ],
                        },
                    },
                ],
                "description": (
                    "要引用的消息。支持 message_id、`{message_id, preview}`，"
                    "或数组。引用整条消息时只传 message_id；指定原文片段时用 "
                    "preview。群聊最终只引用一条，私聊可传数组引用多条。"
                )
            },
            "at_all": {
                "type": "boolean",
                "description": "可选，仅群聊有效。是否 @all。",
                "default": False,
            },
            "mention_user_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，仅群聊有效。要 @ 的人类用户 uuapName。",
                "default": [],
            },
            "mention_agent_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "可选，仅群聊有效。要 @ 的机器人 agentId；也可写 "
                    "`bot:<agentId>`。"
                ),
                "default": [],
            },
        },
        "required": ["target"],
    },
}


# Schema for the agent-callable infoflow_get_group_members tool.
GROUP_MEMBERS_TOOL_SCHEMA = {
    "name": "infoflow_get_group_members",
    "description": (
        "获取如流群聊的成员列表，返回人类成员与机器人成员。"
        "结果最多每 3 秒刷新一次（并发请求会合并为同一次远端拉取）。\n\n"
        "字段用途：\n"
        "- 人类 `user_id`（uuapName）：群聊正文可写 `@user_id`，"
        "或传给 `infoflow_send_message.mention_user_ids`；"
        "只有本地 participants 中已有可信真名时才返回 `name`\n"
        "- 机器人 `agent_id` + `name`：群聊正文优先写 `@agentId`，"
        "或把 agent_id 传给 `infoflow_send_message.mention_agent_ids`；"
        "`name` 主要用于识别机器人"
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


# Schema for the agent-callable infoflow_create_group tool.
CREATE_GROUP_TOOL_SCHEMA = {
    "name": "infoflow_create_group",
    "description": (
        "创建如流群聊，并在建群时一次性拉入多个人类成员和机器人。"
        "这是建群/拉群工具，不用于向已有群追加成员。\n\n"
        "字段说明：\n"
        "- `group_owner`、`member_users`、`managers` 可传 `chengbo05` "
        "或 `chengbo05@baidu.com`；工具会把无域名的 uuapName 规范成 "
        "`@baidu.com` 邮箱\n"
        "- `robot_ids`、`robot_managers` 必须是如流机器人 agentId 整数，"
        "不是机器人名称；可先用 `infoflow_get_group_members` "
        "在已知群里确认机器人 agentId\n"
        "- `friendly_level`: 1=不允许任何人进群，2=群主和管理员验证，"
        "3=不需要验证；tool 默认 3，并会传给如流 API\n"
        "- `search_ability`: 0=不可搜索，1=可搜索；默认 1\n\n"
        "默认行为：tool 会自动把当前 Infoflow 插件自己的 "
        "`INFOFLOW_APP_AGENT_ID` 加入 `robot_ids` 和 `robot_managers`，"
        "让机器人自己成为新群机器人管理员，便于后续操作群。\n\n"
        "限制：管理员总数（`managers` + `robot_managers`）最多 4 个；"
        "`managers` 必须同时出现在 `member_users` 中，`robot_managers` "
        "必须同时出现在 `robot_ids` 中。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "group_name": {
                "type": "string",
                "description": "群名称。",
            },
            "group_owner": {
                "type": "string",
                "description": "群主 uuapName 或邮箱，例如 `chengbo05` / `chengbo05@baidu.com`。",
            },
            "member_users": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要拉入群的人类成员 uuapName 或邮箱列表。",
                "default": [],
            },
            "robot_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "要拉入群的机器人 agentId 列表。",
                "default": [],
            },
            "friendly_level": {
                "type": "integer",
                "enum": [1, 2, 3],
                "description": "加群方式：1=禁止进群，2=群主/管理员验证，3=无需验证；省略时默认 3。",
                "default": 3,
            },
            "search_ability": {
                "type": "integer",
                "enum": [0, 1],
                "description": "是否可被搜索：0=不可搜索，1=可搜索。",
                "default": 1,
            },
            "managers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "普通管理员 uuapName 或邮箱列表；必须已在 member_users 中，且不能是群主。",
                "default": [],
            },
            "robot_managers": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "机器人管理员 agentId 列表；必须已在 robot_ids 中。省略时自动使用当前机器人的 agentId。",
                "default": [],
            },
            "group_sidebar": {
                "type": "object",
                "description": "可选侧边栏配置，例如 {\"autoOpen\": 0, \"customSidebar\": 1, \"link\": \"https://...\"}。",
            },
        },
        "required": ["group_name", "group_owner"],
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


def _dedupe_preserve_order(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _coerce_string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[,，\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = []
        for item in value:
            if isinstance(item, str) and ("," in item or "，" in item):
                raw_items.extend(re.split(r"[,，\s]+", item))
            else:
                raw_items.append(str(item))
    else:
        raw_items = [str(value)]
    return [item.strip() for item in raw_items if item and item.strip()]


def _coerce_int_list(value: Any, field_name: str) -> tuple[list[int], str | None]:
    raw_items = _coerce_string_list(value)
    result: list[int] = []
    for raw in raw_items:
        try:
            item = int(raw)
        except (TypeError, ValueError):
            return [], f"{field_name} must contain integer Infoflow agentIds"
        if item <= 0:
            return [], f"{field_name} must contain positive Infoflow agentIds"
        result.append(item)
    return _dedupe_preserve_order(result), None


def _normalize_baidu_email(value: Any, field_name: str) -> tuple[str, str | None]:
    raw = str(value or "").strip().lower()
    if not raw:
        return "", f"{field_name} is required"
    raw = raw.removeprefix("mailto:").strip()
    if any(ch.isspace() for ch in raw):
        return "", f"{field_name} must be a uuapName or email, not whitespace-separated text"
    if "@" not in raw:
        raw = f"{raw}@baidu.com"
    if raw.startswith("@") or raw.endswith("@"):
        return "", f"{field_name} must be a valid uuapName or email"
    return raw, None


def _normalize_baidu_email_list(value: Any, field_name: str) -> tuple[list[str], str | None]:
    normalized: list[str] = []
    for item in _coerce_string_list(value):
        email, error = _normalize_baidu_email(item, field_name)
        if error:
            return [], error
        normalized.append(email)
    return _dedupe_preserve_order(normalized), None


def _normalize_create_group_args(args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    group_name = str(args.get("group_name") or args.get("name") or "").strip()
    if not group_name:
        return {}, "group_name is required"

    group_owner, error = _normalize_baidu_email(
        args.get("group_owner") or args.get("owner"),
        "group_owner",
    )
    if error:
        return {}, error

    member_users, error = _normalize_baidu_email_list(
        args.get("member_users", args.get("members")),
        "member_users",
    )
    if error:
        return {}, error

    robot_ids, error = _coerce_int_list(
        args.get("robot_ids", args.get("robots")),
        "robot_ids",
    )
    if error:
        return {}, error

    managers, error = _normalize_baidu_email_list(args.get("managers"), "managers")
    if error:
        return {}, error

    robot_managers, error = _coerce_int_list(
        args.get("robot_managers"),
        "robot_managers",
    )
    if error:
        return {}, error

    friendly_level = _clamp_int(args.get("friendly_level", 3), 3, 1, 3)
    if args.get("friendly_level") not in (None, ""):
        try:
            friendly_level = int(args.get("friendly_level"))
        except (TypeError, ValueError):
            return {}, "friendly_level must be 1, 2, or 3"
        if friendly_level not in (1, 2, 3):
            return {}, "friendly_level must be 1, 2, or 3"

    search_ability = _clamp_int(args.get("search_ability", 1), 1, 0, 1)
    if args.get("search_ability") not in (None, ""):
        try:
            search_ability = int(args.get("search_ability"))
        except (TypeError, ValueError):
            return {}, "search_ability must be 0 or 1"
        if search_ability not in (0, 1):
            return {}, "search_ability must be 0 or 1"

    if group_owner in managers:
        return {}, "group_owner cannot also be listed in managers"
    member_set = set(member_users)
    missing_managers = [m for m in managers if m not in member_set]
    if missing_managers:
        return {}, "managers must also be included in member_users"

    group_sidebar = args.get("group_sidebar")
    if group_sidebar is not None and not isinstance(group_sidebar, dict):
        return {}, "group_sidebar must be an object"

    return {
        "group_name": group_name,
        "group_owner": group_owner,
        "member_users": member_users,
        "robot_ids": robot_ids,
        "friendly_level": friendly_level,
        "search_ability": search_ability,
        "managers": managers,
        "robot_managers": robot_managers,
        "group_sidebar": group_sidebar,
    }, None


def _own_agent_id_for_adapter(adapter: Any) -> int | None:
    settings = getattr(adapter, "_settings", None)
    raw = settings.get("app_agent_id") if isinstance(settings, dict) else None
    if raw in (None, ""):
        serverapi = getattr(adapter, "_serverapi", None)
        account = getattr(serverapi, "_api_account", None)
        raw = getattr(account, "app_agent_id", None)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _finalize_create_group_defaults(
    normalized: dict[str, Any],
    *,
    own_agent_id: int | None,
) -> tuple[dict[str, Any], str | None]:
    data = dict(normalized)
    robot_ids = list(data.get("robot_ids") or [])
    robot_managers = list(data.get("robot_managers") or [])

    if own_agent_id is None:
        return {}, (
            "INFOFLOW_APP_AGENT_ID is required so the bot can be added as "
            "a robot manager for the new group."
        )

    if own_agent_id not in robot_ids:
        robot_ids.append(own_agent_id)
    if own_agent_id not in robot_managers:
        robot_managers.append(own_agent_id)

    robot_ids = _dedupe_preserve_order(robot_ids)
    robot_managers = _dedupe_preserve_order(robot_managers)
    managers = list(data.get("managers") or [])

    if len(managers) + len(robot_managers) > 4:
        return {}, (
            "managers and robot_managers can contain at most 4 total admins "
            "after adding the bot itself as robot manager"
        )
    robot_set = set(robot_ids)
    missing_robot_managers = [r for r in robot_managers if r not in robot_set]
    if missing_robot_managers:
        return {}, "robot_managers must also be included in robot_ids"

    data["robot_ids"] = robot_ids
    data["robot_managers"] = robot_managers
    return data, None


def _sensitive_tool_allowed(adapter: Any) -> tuple[bool, str | None]:
    """Best-effort channel authorization for side-effectful Infoflow tools."""
    try:
        from .bot import get_recall_inbound_message_id_hint  # noqa: E402
    except Exception:
        return True, None

    current_message_id = get_recall_inbound_message_id_hint() or ""
    if not current_message_id:
        return True, None

    store = getattr(adapter, "_message_store", None)
    finder = getattr(store, "find_any", None)
    if not callable(finder):
        return False, "Current Infoflow message context is required to authorize this tool."
    record = finder(current_message_id)
    if record is None:
        return False, "Current Infoflow message context is required to authorize this tool."
    admin_uid = str(getattr(adapter, "_admin_uid", "") or "")
    if _record_is_admin(record, admin_uid):
        return True, None
    return False, "Only Infoflow admin users can create groups."


_MEDIA_DIRECTIVE_RE = re.compile(
    r'''[`"']?MEDIA:\s*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|[^\s`"']+)[`"']?'''
)


def _sanitize_media_error(error: Any, media_files: list[tuple[str, bool]]) -> str:
    text = str(error or "image send failed")
    for raw_path, _is_voice in media_files:
        raw = str(raw_path or "")
        if raw:
            text = text.replace(raw, "[local image path]")
            text = text.replace(os.path.expanduser(raw), "[local image path]")
    if "MEDIA:" in text:
        text = _MEDIA_DIRECTIVE_RE.sub("MEDIA:[local image path]", text)
    return text


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


def _chat_id_from_target_tuple(target: tuple[str, str, str, str]) -> str:
    kind, group_id, user_id, _key = target
    if kind == "group" and group_id:
        return f"group:{group_id}"
    if kind == "dm" and user_id:
        return user_id
    return ""


def _current_infoflow_chat_id(adapter: Any) -> str:
    try:
        from .bot import get_recall_inbound_message_id_hint  # noqa: E402
    except Exception:
        return ""

    current_message_id = get_recall_inbound_message_id_hint() or ""
    if not current_message_id:
        return ""

    store = getattr(adapter, "_message_store", None)
    finder = getattr(store, "find_any", None)
    if callable(finder):
        record = finder(current_message_id)
        if record is not None:
            chat_id = _chat_id_from_target_tuple(_target_from_record(record))
            if chat_id:
                return chat_id

    try:
        from .recall import get_inbound_target  # noqa: E402
    except Exception:
        return ""
    return get_inbound_target(current_message_id)


def _same_target(a: tuple[str, str, str, str], b: tuple[str, str, str, str]) -> bool:
    return a[0] == b[0] and a[3] == b[3]


def _record_is_admin(record: Any, admin_uid: str) -> bool:
    admins = parse_infoflow_admin_users(admin_uid)
    if not admins:
        return False
    sender = str(getattr(record, "sender", "") or "").strip().lower()
    if sender.startswith("bot:"):
        return False
    if sender.startswith("user:"):
        sender = sender.removeprefix("user:")
    return sender in admins


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


_SEND_AT_RE = re.compile(r"@([^\s@\n]{1,30})(?=[\s]|$)")
_SEND_PREVIEW_LIMIT = 160


def _send_failure_payload(
    *,
    reason: str,
    error: str,
    target: str = "",
    chat_type: str = "",
    sent_messages: list[dict[str, str]] | None = None,
    warnings: list[dict[str, str]] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "success": False,
        "reason": reason,
        "error": error,
    }
    if target:
        payload["target"] = target
    if chat_type:
        payload["chat_type"] = chat_type
    if warnings:
        payload["warnings"] = warnings
    if sent_messages:
        payload["sent_messages"] = sent_messages
        payload["retry_note"] = (
            "partial_failure: messages listed in sent_messages were already sent; "
            "do not resend them automatically."
        )
    return tool_result_json(payload)


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item or "").strip()]
    return [str(value)] if str(value or "").strip() else []


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _parse_send_target(target: Any) -> tuple[dict[str, str] | None, str | None]:
    raw = str(target or "").strip()
    if not raw:
        return None, "target is required"
    if raw.startswith("infoflow:"):
        raw = raw[len("infoflow:"):].strip()
    if raw.startswith("bot:"):
        return None, (
            "unsupported_target: bot:<agentId> is recognized but Infoflow "
            "does not support bot private-chat target sending"
        )
    if raw.startswith("group:"):
        group_id = raw[len("group:"):].strip()
        if not group_id or not group_id.isdigit():
            return None, "target group id must be numeric"
        return {
            "chat_type": "group",
            "group_id": group_id,
            "dm_user": "",
            "target": f"group:{group_id}",
            "store_key": f"group:{group_id}",
        }, None
    if raw.startswith("dm:user:"):
        raw = raw[len("dm:user:"):].strip()
    elif raw.startswith("user:"):
        raw = raw[len("user:"):].strip()
    if not raw:
        return None, "target user id is empty"
    if raw.isdigit():
        return {
            "chat_type": "group",
            "group_id": raw,
            "dm_user": "",
            "target": f"group:{raw}",
            "store_key": f"group:{raw}",
        }, None
    return {
        "chat_type": "private",
        "group_id": "",
        "dm_user": raw,
        "target": f"user:{raw}",
        "store_key": raw,
    }, None


def _safe_preview(text: Any, *, limit: int = _SEND_PREVIEW_LIMIT) -> str:
    preview = str(text or "")
    preview = re.sub(r"data:image/[^,\s]+,[A-Za-z0-9+/=]+", "[image]", preview)
    preview = preview.replace("\x00", "")
    preview = re.sub(r"\s+", " ", preview).strip()
    if len(preview) > limit:
        return preview[: max(0, limit - 1)].rstrip() + "…"
    return preview


def _sent_result_ids(result: Any) -> list[tuple[str, str]]:
    ids: list[tuple[str, str]] = []
    continuation_ids = list(getattr(result, "continuation_message_ids", ()) or ())
    continuation_seqs = list(getattr(result, "continuation_msgseqids", ()) or ())
    for idx, mid in enumerate(continuation_ids):
        mid_s = str(mid or "")
        if not mid_s:
            continue
        seq = continuation_seqs[idx] if idx < len(continuation_seqs) else ""
        ids.append((mid_s, str(seq or "")))
    primary = str(getattr(result, "message_id", "") or "")
    if primary and all(mid != primary for mid, _seq in ids):
        ids.append((primary, str(getattr(result, "msgseqid", "") or "")))
    return ids


def _send_service_for_adapter(adapter: Any) -> Any | None:
    serverapi = getattr(adapter, "_serverapi", None)
    if serverapi is None:
        return None
    message_store = getattr(adapter, "_message_store", None)
    service = getattr(adapter, "_send_service", None)
    if (
        service is not None
        and getattr(service, "_serverapi", None) is serverapi
        and getattr(service, "_message_store", None) is message_store
    ):
        return service
    try:
        from .recall import get_inbound_body, get_inbound_sender_imid
    except Exception:
        get_inbound_body = None  # type: ignore[assignment]
        get_inbound_sender_imid = None  # type: ignore[assignment]
    service = InfoflowSendService(
        serverapi=serverapi,
        message_store=message_store,
        inbound_body_lookup=get_inbound_body,
        inbound_sender_imid_lookup=get_inbound_sender_imid,
    )
    with contextlib.suppress(Exception):
        adapter._send_service = service
    return service


def _record_tool_sent(
    adapter: Any,
    *,
    target: dict[str, str],
    result: Any,
    kind: str,
    preview: str,
) -> list[dict[str, str]]:
    receipts: list[dict[str, str]] = []
    group_id = target["group_id"] or None
    dm_user = target["dm_user"] or None
    store_key = target["store_key"]
    sent_store = getattr(adapter, "_sent_store", None)
    bot = getattr(adapter, "_bot", None)
    record_sent = getattr(bot, "_record_sent", None)
    result_receipts = list(getattr(result, "sent_messages", ()) or ())
    if result_receipts:
        raw_receipts = [
            (
                str(getattr(receipt, "message_id", "") or ""),
                str(getattr(receipt, "msgseqid", "") or ""),
                str(getattr(receipt, "kind", "") or kind or "text"),
                str(getattr(receipt, "preview", "") or preview or ""),
            )
            for receipt in result_receipts
        ]
    else:
        raw_receipts = [
            (mid, seq, kind, preview)
            for mid, seq in _sent_result_ids(result)
        ]
    for mid, seq, receipt_kind, receipt_preview in raw_receipts:
        if not mid:
            continue
        if sent_store is not None:
            with contextlib.suppress(Exception):
                sent_store.record(
                    chat_id=store_key,
                    messageid=mid,
                    msgseqid=seq,
                    digest=receipt_preview[:80],
                )
        if callable(record_sent):
            with contextlib.suppress(Exception):
                record_sent(
                    message_id=mid,
                    text=receipt_preview or ("[image]" if receipt_kind == "image" else ""),
                    group_id=group_id,
                    dm_user_id=dm_user,
                )
        receipts.append({
            "message_id": mid,
            "kind": receipt_kind,
            "preview": _safe_preview(receipt_preview),
        })
    return receipts


def _push_send_tool_event(
    adapter: Any,
    *,
    target: dict[str, str],
    success: bool,
    sent_messages: list[dict[str, str]],
    error: str = "",
) -> None:
    push = getattr(adapter, "_push_infoflow_event", None)
    if not callable(push):
        return
    with contextlib.suppress(Exception):
        push(
            None,
            kind="outbound.infoflow",
            chat_id=target["target"],
            extra={
                "type": "send_message_tool",
                "success": success,
                "sent_count": len(sent_messages),
                "message_id": sent_messages[-1]["message_id"] if sent_messages else "",
                "error": error,
            },
        )


# ---------------------------------------------------------------------------
# Tool handler factories
# ---------------------------------------------------------------------------


def make_send_message_handler():
    """Build the ``infoflow_send_message`` tool handler."""

    async def _handler(args: dict, **_kwargs) -> str:
        target, target_error = _parse_send_target(args.get("target"))
        if target_error or target is None:
            return _send_failure_payload(
                reason="invalid_target",
                error=target_error or "invalid target",
            )

        adapter = _get_live_adapter()
        if adapter is None:
            return _send_failure_payload(
                reason="adapter_unavailable",
                error="Infoflow adapter not running — cannot send.",
                target=target["target"],
                chat_type=target["chat_type"],
            )

        if "richtext_links" in args:
            return _send_failure_payload(
                reason="invalid_parameter",
                error="unsupported link parameter; use links",
                target=target["target"],
                chat_type=target["chat_type"],
            )

        send_service = _send_service_for_adapter(adapter)
        if send_service is None:
            return _send_failure_payload(
                reason="adapter_unavailable",
                error="Infoflow send service is unavailable",
                target=target["target"],
                chat_type=target["chat_type"],
            )

        sent_messages: list[dict[str, str]] = []
        session = getattr(adapter, "_effective_session", lambda s: None)(
            getattr(adapter, "_http_session", None)
        )
        warnings: list[dict[str, str]] = []

        if target["chat_type"] == "group":
            result = await send_service.send_group(
                target["group_id"],
                message=args.get("message"),
                format=args.get("format", "auto"),
                links=args.get("links"),
                image_paths=args.get("image_paths"),
                reply_to=args.get("reply_to"),
                at_all=args.get("at_all"),
                mention_user_ids=args.get("mention_user_ids"),
                mention_agent_ids=args.get("mention_agent_ids"),
                session=session,
            )
        else:
            result = await send_service.send_private(
                target["dm_user"],
                message=args.get("message"),
                format=args.get("format", "auto"),
                links=args.get("links"),
                image_paths=args.get("image_paths"),
                reply_to=args.get("reply_to"),
                at_all=args.get("at_all"),
                mention_user_ids=args.get("mention_user_ids"),
                mention_agent_ids=args.get("mention_agent_ids"),
                session=session,
            )

        warnings.extend(list(getattr(result, "warnings", ()) or ()))
        sent_messages.extend(
            _record_tool_sent(
                adapter,
                target=target,
                result=result,
                kind="",
                preview="",
            )
        )
        if not result.success:
            _push_send_tool_event(
                adapter,
                target=target,
                success=False,
                sent_messages=sent_messages,
                error=result.error or "send failed",
            )
            error_code = str(getattr(result, "error_code", "") or "")
            return _send_failure_payload(
                reason="partial_failure" if sent_messages else (error_code or "send_failed"),
                error=result.error or "send failed",
                target=target["target"],
                chat_type=target["chat_type"],
                sent_messages=sent_messages,
                warnings=warnings,
            )

        _push_send_tool_event(
            adapter,
            target=target,
            success=True,
            sent_messages=sent_messages,
        )
        payload: dict[str, Any] = {
            "success": True,
            "target": target["target"],
            "chat_type": target["chat_type"],
            "sent_messages": sent_messages,
        }
        if warnings:
            payload["warnings"] = warnings
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


def make_create_group_handler():
    """Build the ``infoflow_create_group`` tool handler."""

    async def _handler(args: dict, **_kwargs) -> str:
        normalized, error = _normalize_create_group_args(args)
        if error:
            return _json_error(error)

        adapter = _get_live_adapter()
        if adapter is None:
            return _json_error("Infoflow adapter not running — cannot create group.")

        allowed, auth_error = _sensitive_tool_allowed(adapter)
        if not allowed:
            return _json_error(auth_error or "Not authorized to create Infoflow groups.")

        own_agent_id = _own_agent_id_for_adapter(adapter)
        normalized, error = _finalize_create_group_defaults(
            normalized,
            own_agent_id=own_agent_id,
        )
        if error:
            return _json_error(error)

        result = await adapter._serverapi.create_group(
            group_name=normalized["group_name"],
            group_owner=normalized["group_owner"],
            member_list=normalized["member_users"] or None,
            robot_list=normalized["robot_ids"] or None,
            friendly_level=normalized["friendly_level"],
            search_ability=normalized["search_ability"],
            managers=normalized["managers"] or None,
            robot_managers=normalized["robot_managers"] or None,
            group_sidebar=normalized["group_sidebar"],
        )
        if not result.get("ok"):
            return tool_result_json({
                "success": False,
                "error": result.get("error") or result.get("errmsg") or "create group failed",
                "errcode": result.get("errcode"),
                "errmsg": result.get("errmsg"),
            })

        failed = {
            "members": result.get("failMembers") or [],
            "robots": result.get("failRobotIds") or [],
            "managers": result.get("failManager") or [],
            "robot_managers": result.get("failRobotManager") or [],
        }
        if own_agent_id in failed["robots"] or own_agent_id in failed["robot_managers"]:
            return tool_result_json({
                "success": False,
                "error": (
                    "group created but the bot itself was not added as robot "
                    "manager; bot group-management capability is not guaranteed"
                ),
                "group_id": str(result.get("groupid") or ""),
                "group_name": normalized["group_name"],
                "failed": failed,
            })
        payload = {
            "success": True,
            "group_id": str(result.get("groupid") or ""),
            "group_name": normalized["group_name"],
            "group_owner": normalized["group_owner"],
            "requested": {
                "member_users": normalized["member_users"],
                "robot_ids": normalized["robot_ids"],
                "friendly_level": normalized["friendly_level"],
                "search_ability": normalized["search_ability"],
                "managers": normalized["managers"],
                "robot_managers": normalized["robot_managers"],
            },
            "failed": failed,
        }
        if any(failed.values()):
            payload["partial_failure"] = True
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
            if (
                current_target is not None
                and not _same_target(explicit_target, current_target)
                and not current_is_admin
            ):
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
