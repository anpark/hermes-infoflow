"""Shared rendering for stored message content and LLM [Message] body."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .coerce import coerce_bool
from .llm_tags import string_field

_AT_ONLY_HINT = (
    "\n\n[注意] 用户 @ 了你但没有输入正文。请优先阅读并理解上下文，"
    "主动寻找刚才的问题、讨论话题或待办事项，并基于上下文进行回答、补充或参与讨论。"
    "只有在上下文中没有可识别的问题、话题或待办时，才询问用户有什么需要帮忙的。"
)


RobotAgentLookup = Callable[[str], str | None]


def _first_attr(obj: Any, *names: str) -> str:
    for name in names:
        value = obj.get(name, "") if isinstance(obj, dict) else getattr(obj, name, "")
        if value not in (None, ""):
            return str(value)
    return ""


def _bool_attr(obj: Any, *names: str) -> bool:
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if coerce_bool(value):
            return True
    return False


def _format_at(item: Any, robot_agent_id_lookup: RobotAgentLookup | None) -> str:
    if _bool_attr(item, "at_all", "atall"):
        return "@所有人"

    name = _first_attr(item, "name")
    user_id = _first_attr(item, "user_id", "userid")
    robot_id = _first_attr(item, "robot_id", "robotid")
    if user_id:
        display = name or user_id
        if name and name != user_id:
            return f"@{display} (user_id:{user_id})"
        return f"@{display}"

    if robot_id:
        agent_id = ""
        if robot_agent_id_lookup is not None:
            agent_id = str(robot_agent_id_lookup(robot_id) or "").strip()
        display = name or agent_id or "未知机器人"
        if agent_id:
            return f"@{display} (agent_id:{agent_id})"
        return f"@{display}"

    return f"@{name or '?'}"


def _render_reply_target(target: Any) -> str:
    message_id = _first_attr(target, "message_id", "messageid")
    preview = _first_attr(target, "preview", "content")
    if not message_id:
        return ""
    sender = _first_attr(target, "sender_key", "sender")
    fields = [string_field("message_id", message_id)]
    if sender:
        fields.append(string_field("sender", sender))
    return f"<Quote {'; '.join(fields)}>{preview}</Quote>"


def _body_has_reply_item(body_items: list[Any]) -> bool:
    return any(
        (_first_attr(item, "type").upper() in {"REPLYDATA", "REPLY"})
        for item in body_items
    )


def _has_media_image_marker(text: str) -> bool:
    return any(line.strip().startswith("<media:image>") for line in text.splitlines())


def _render_body_items(
    body_items: list[Any],
    *,
    robot_agent_id_lookup: RobotAgentLookup | None,
    reply_targets: list[Any] | None = None,
) -> tuple[str, bool]:
    parts: list[str] = []
    has_image = False
    reply_target_by_id = {
        mid: target
        for target in (reply_targets or [])
        if (mid := _first_attr(target, "message_id", "messageid"))
    }
    for item in body_items:
        item_type = _first_attr(item, "type").upper()
        if item_type in {"TEXT", "MD"}:
            parts.append(_first_attr(item, "content"))
        elif item_type == "AT":
            parts.append(_format_at(item, robot_agent_id_lookup) + " ")
        elif item_type == "LINK":
            label = _first_attr(item, "label", "content")
            if label:
                parts.append(f" {label} ")
        elif item_type in {"REPLYDATA", "REPLY"}:
            message_id = _first_attr(item, "message_id", "messageid")
            rendered = _render_reply_target(reply_target_by_id.get(message_id, item))
            if rendered:
                parts.append(rendered + "\n")
        elif item_type == "IMAGE":
            has_image = True
    return "".join(parts).strip(), has_image


def _at_only_description(
    body_items: list[Any],
    *,
    robot_agent_id_lookup: RobotAgentLookup | None,
) -> str:
    mention_parts: list[str] = []
    if body_items:
        at_all = any(
            _first_attr(b, "type").upper() == "AT"
            and _bool_attr(b, "at_all", "atall")
            for b in body_items
        )
        if at_all:
            mention_parts.append("@所有人")
        for item in body_items:
            if _first_attr(item, "type").upper() != "AT":
                continue
            if _bool_attr(item, "at_all", "atall"):
                continue
            mention_parts.append(_format_at(item, robot_agent_id_lookup))
    if mention_parts:
        return f"（仅@了以下对象，无正文：{' '.join(mention_parts)}）"
    return "<空消息>"


def render_message_content(
    msg: Any,
    *,
    robot_agent_id_lookup: RobotAgentLookup | None = None,
) -> str:
    """Return the normalized body stored in DB and placed after ``[Message]``."""
    body_items = list(getattr(msg, "body_items", None) or [])
    image_urls = list(getattr(msg, "image_urls", None) or [])
    reply_targets = list(getattr(msg, "reply_targets", None) or [])

    if body_items:
        text, body_has_image = _render_body_items(
            body_items,
            robot_agent_id_lookup=robot_agent_id_lookup,
            reply_targets=reply_targets,
        )
        if body_has_image and not image_urls:
            image_urls = ["<inline-image>"]
    else:
        text = getattr(msg, "text", "") or ""

    if reply_targets and not _body_has_reply_item(body_items):
        prefix_parts = [
            rendered for target in reply_targets
            if (rendered := _render_reply_target(target))
        ]
        if prefix_parts:
            text = "\n".join(prefix_parts + ([text] if text else []))

    is_at_only = bool(getattr(msg, "is_at_only", False))
    if is_at_only:
        text = _at_only_description(
            body_items,
            robot_agent_id_lookup=robot_agent_id_lookup,
        )
    elif not text.strip():
        if image_urls:
            text = "<media:image>" if len(image_urls) == 1 else f"<media:image> ({len(image_urls)} images)"
        else:
            text = _at_only_description(
                body_items,
                robot_agent_id_lookup=robot_agent_id_lookup,
            )
    elif image_urls and not _has_media_image_marker(text):
        marker = "<media:image>" if len(image_urls) == 1 else f"<media:image> ({len(image_urls)} images)"
        text = f"{text}\n{marker}"
    if is_at_only:
        text = (text or "") + _AT_ONLY_HINT
    return text
