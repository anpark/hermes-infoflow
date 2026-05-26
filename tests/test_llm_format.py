"""Tests for Infoflow LLM envelope formatting."""

from __future__ import annotations

from hermes_infoflow.llm_format import (
    GroupAttention,
    group_attention_line,
    message_line,
    permission_for_sender,
    sender_line,
    unread_message_context_line,
)
from hermes_infoflow.llm_tags import quote_tag_value


def test_unread_message_context_line_requires_reading_all_small_gap() -> None:
    line = unread_message_context_line(2)
    assert line.startswith("[Unread Message Context:")
    assert "before_count=2、after_count=0" in line
    assert "该范围内有未读历史消息" in line
    assert "阅读参考上下文后再判断如何回复" in line


def test_unread_message_context_line_caps_required_initial_read_for_large_gap() -> None:
    line = unread_message_context_line(12)
    assert line.startswith("[Unread Message Context:")
    assert "较大历史范围内有未读消息" in line
    assert "before_count=7、after_count=0" in line
    assert "先阅读参考上下文后再判断如何回复" in line
    assert "再按需继续扩大历史范围" in line


def test_structured_string_values_are_single_quoted() -> None:
    assert message_line("mid-1") == "[Message: message_id:'mid-1']"
    assert (
        sender_line(sender_key="user:alice", name="Alice O'Brien", admin_uid="")
        == "[Sender: type:'human'; user_id:'alice'; name:'Alice O\\'Brien'; permission:'restricted']"
    )


def test_sender_permission_accepts_any_admin_from_comma_list() -> None:
    assert permission_for_sender("user:bob", "alice,bob") == "admin"
    assert permission_for_sender("user:alice", " root,ALICE ") == "admin"
    assert permission_for_sender("bot:bob", "alice,bob") == "restricted"
    assert permission_for_sender("user:carol", "alice,bob") == "restricted"


def test_attention_regex_pattern_is_quoted_but_booleans_are_bare() -> None:
    line = group_attention_line(
        GroupAttention(
            mentions_you=True,
            matched_regex_pattern="部署|O'Brien",
            quotes_your_message=True,
        )
    )
    assert "mentions_you=true" in line
    assert "matches_attention_regex=true" in line
    assert "matched_regex_pattern:'部署|O\\'Brien'" in line
    assert "quotes_your_message=true" in line


def test_quote_tag_value_escapes_single_quote_and_backslash() -> None:
    assert quote_tag_value(r"a\b'c") == r"'a\\b\'c'"
