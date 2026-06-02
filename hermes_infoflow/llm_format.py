"""LLM-facing Infoflow message envelope formatting.

``message_content.py`` renders only the untrusted body after ``[Message]``.
This module renders the trusted envelope that wraps that body for current
messages and history-tool results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .inbound_files import inbound_file_from_raw_dict, render_attachments_block
from .llm_tags import string_field
from .settings import parse_infoflow_admin_users


def _bool_text(value: bool) -> str:
    return "true" if bool(value) else "false"


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def participant_kind(sender_key: str) -> str:
    if sender_key.startswith("bot:"):
        return "bot"
    return "human"


def participant_id_from_key(sender_key: str) -> str:
    sender_key = _clean(sender_key)
    if sender_key.startswith("bot:"):
        return sender_key.removeprefix("bot:")
    if sender_key.startswith("user:"):
        return sender_key.removeprefix("user:")
    return sender_key


def permission_for_sender(sender_key: str, admin_uid: object = "") -> str:
    admins = parse_infoflow_admin_users(admin_uid)
    if not admins:
        return "restricted"
    if participant_kind(_clean(sender_key)) != "human":
        return "restricted"
    raw_id = participant_id_from_key(sender_key).lower()
    return "admin" if raw_id in admins else "restricted"


def format_created_time_ms(created_time_ms: int) -> str:
    if int(created_time_ms or 0) <= 0:
        return ""
    dt = datetime.fromtimestamp(int(created_time_ms) / 1000).astimezone()
    return (
        f"{dt.year}.{dt.month:02d}.{dt.day:02d} "
        f"{dt.hour:02d}.{dt.minute:02d}.{dt.second:02d}"
    )


@dataclass(frozen=True)
class GroupAttention:
    mentions_you: bool = False
    matched_regex_pattern: str = ""
    mentions_everyone: bool = False
    quotes_your_message: bool = False
    mentions_other_people: bool = False
    quotes_other_peoples_message: bool = False


@dataclass(frozen=True)
class DMAttention:
    quotes_your_message: bool = False


class ParticipantLookup(Protocol):
    def __call__(self, sender_key: str) -> str | None: ...


def group_attention_line(attention: GroupAttention) -> str:
    pattern = _clean(attention.matched_regex_pattern)
    parts = [
        f"mentions_you={_bool_text(attention.mentions_you)}",
        f"matches_attention_regex={_bool_text(bool(pattern))}",
    ]
    if pattern:
        parts.append(string_field("matched_regex_pattern", pattern))
    parts.extend([
        f"mentions_everyone={_bool_text(attention.mentions_everyone)}",
        f"quotes_your_message={_bool_text(attention.quotes_your_message)}",
        f"mentions_other_people={_bool_text(attention.mentions_other_people)}",
        (
            "quotes_other_peoples_message="
            f"{_bool_text(attention.quotes_other_peoples_message)}"
        ),
    ])
    return f"[Attention: {'; '.join(parts)}]"


def dm_attention_line(attention: DMAttention) -> str:
    return (
        "[Attention: "
        f"quotes_your_message={_bool_text(attention.quotes_your_message)}]"
    )


def sender_line(
    *,
    sender_key: str,
    name: str = "",
    admin_uid: object = "",
) -> str:
    sender_key = _clean(sender_key)
    name = _clean(name)
    kind = participant_kind(sender_key)
    raw_id = participant_id_from_key(sender_key) or "unknown"
    parts: list[str]
    if kind == "bot":
        parts = [string_field("type", "bot"), string_field("agent_id", raw_id)]
    else:
        parts = [string_field("type", "human"), string_field("user_id", raw_id)]
    if name:
        parts.append(string_field("name", name))
    parts.append(string_field("permission", permission_for_sender(sender_key, admin_uid)))
    return f"[Sender: {'; '.join(parts)}]"


def message_line(message_id: str, *, created_time_ms: int = 0) -> str:
    mid = _clean(message_id) or "unknown"
    parts = [string_field("message_id", mid)]
    created = format_created_time_ms(created_time_ms)
    if created:
        parts.append(string_field("created_time", created))
    return f"[Message: {'; '.join(parts)}]"


UNREAD_MESSAGE_CONTEXT_REQUIRED_READ_LIMIT = 7


def unread_message_context_line(count: int) -> str:
    n = max(0, int(count))
    read_count = min(n, UNREAD_MESSAGE_CONTEXT_REQUIRED_READ_LIMIT)
    if n <= UNREAD_MESSAGE_CONTEXT_REQUIRED_READ_LIMIT:
        return (
            "[Unread Message Context: 请优先调用 infoflow_get_message_history，"
            "使用当前 Message 标签中的 message_id 作为锚点，"
            f"设置 before_count={n}、after_count=0。"
            "该范围内有未读历史消息；如果当前消息依赖上文或附件，"
            "必须先阅读参考上下文后再判断如何回复。]"
        )
    return (
        "[Unread Message Context: 当前消息前较大历史范围内有未读消息。"
        "请优先调用 infoflow_get_message_history，使用当前 Message 标签中的 "
        f"message_id 作为锚点，设置 before_count={read_count}、after_count=0。"
        "如果当前消息依赖上文或附件，必须先阅读参考上下文后再判断如何回复；"
        "如果上下文仍不足，再按需继续扩大历史范围。]"
    )


def format_message_envelope(
    *,
    attention_line: str,
    sender_line_text: str,
    message_id: str,
    content: str,
    created_time_ms: int = 0,
    handling_strategy: str = "",
    unread_message_context_count: int = 0,
    attachments_block: str = "",
) -> str:
    lines: list[str] = []
    if unread_message_context_count > 0:
        lines.append(unread_message_context_line(unread_message_context_count))
    if handling_strategy:
        lines.extend([
            "[Handling Strategy]",
            handling_strategy.strip(),
            "[/Handling Strategy]",
        ])
    lines.extend([
        attention_line,
        sender_line_text,
    ])
    if attachments_block:
        lines.append(str(attachments_block).strip())
    lines.extend([
        message_line(message_id, created_time_ms=created_time_ms),
        content or "",
    ])
    return "\n".join(lines)


def format_group_record(
    record: object,
    *,
    sender_name_lookup: ParticipantLookup | None = None,
    admin_uid: object = "",
) -> str:
    sender = _clean(getattr(record, "sender", ""))
    name = ""
    if sender_name_lookup is not None:
        name = _clean(sender_name_lookup(sender))
    attention = GroupAttention(
        mentions_you=bool(getattr(record, "mentions_you", False)),
        matched_regex_pattern=_clean(getattr(record, "matched_regex_pattern", "")),
        mentions_everyone=bool(getattr(record, "mentions_everyone", False)),
        quotes_your_message=bool(getattr(record, "quotes_your_message", False)),
        mentions_other_people=bool(getattr(record, "mentions_other_people", False)),
        quotes_other_peoples_message=bool(
            getattr(record, "quotes_other_peoples_message", False)
        ),
    )
    return format_message_envelope(
        attention_line=group_attention_line(attention),
        sender_line_text=sender_line(
            sender_key=sender,
            name=name,
            admin_uid=admin_uid,
        ),
        message_id=_clean(getattr(record, "message_id", "")),
        created_time_ms=int(getattr(record, "created_time", 0) or 0),
        content=str(getattr(record, "content", "") or ""),
        attachments_block=_attachments_block_from_record(record),
    )


def format_dm_record(
    record: object,
    *,
    sender_name_lookup: ParticipantLookup | None = None,
    admin_uid: object = "",
) -> str:
    sender = _clean(getattr(record, "sender", ""))
    name = ""
    if sender_name_lookup is not None:
        name = _clean(sender_name_lookup(sender))
    attention = DMAttention(
        quotes_your_message=bool(getattr(record, "quotes_your_message", False)),
    )
    return format_message_envelope(
        attention_line=dm_attention_line(attention),
        sender_line_text=sender_line(
            sender_key=sender,
            name=name,
            admin_uid=admin_uid,
        ),
        message_id=_clean(getattr(record, "message_id", "")),
        created_time_ms=int(getattr(record, "created_time", 0) or 0),
        content=str(getattr(record, "content", "") or ""),
        attachments_block=_attachments_block_from_record(record),
    )


def _attachments_block_from_record(record: object) -> str:
    raw_json = str(getattr(record, "raw_json", "") or "")
    if not raw_json:
        return ""
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    raw_files = payload.get("_hermes_infoflow_files")
    if not isinstance(raw_files, list):
        return ""
    files = []
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        file = inbound_file_from_raw_dict(item)
        if file is not None:
            files.append(file)
    return render_attachments_block(files)
