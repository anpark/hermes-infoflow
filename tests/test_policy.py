"""Tests for hermes_infoflow.policy."""

from __future__ import annotations

import pytest

from hermes_infoflow import policy
from hermes_infoflow.parser import BodyItem, InboundMessage


def _group(**kwargs) -> InboundMessage:
    base = {
        "chat_type": "group",
        "from_user": "bob",
        "text": "hello",
        "body_for_agent": "hello",
    }
    base.update(kwargs)
    return InboundMessage(**base)


def _dm(**kwargs) -> InboundMessage:
    base = {
        "chat_type": "dm",
        "from_user": "alice",
        "text": "hi",
        "body_for_agent": "hi",
    }
    base.update(kwargs)
    return InboundMessage(**base)


def test_normalize_reply_mode_passthrough() -> None:
    assert policy.normalize_reply_mode("ignore").value == "ignore"
    assert policy.normalize_reply_mode("mention-only").value == "mention-only"
    assert policy.normalize_reply_mode("MENTION-AND-WATCH").value == "mention-and-watch"


@pytest.mark.parametrize("legacy", ["record", "proactive"])
def test_normalize_reply_mode_warns_on_unsupported(legacy: str) -> None:
    nm = policy.normalize_reply_mode(legacy)
    assert nm.value == "mention-and-watch"
    assert legacy in nm.warning


def test_normalize_reply_mode_unknown_falls_back() -> None:
    nm = policy.normalize_reply_mode("garbage")
    assert nm.value == "mention-and-watch"
    assert "garbage" in nm.warning


def test_dm_always_dispatches() -> None:
    p = policy.GroupPolicy(reply_mode="ignore", require_mention=True)
    decision = policy.evaluate_inbound(_dm(), p)
    assert decision.should_dispatch is True


def test_reply_mode_ignore_drops_group() -> None:
    p = policy.GroupPolicy(reply_mode="ignore")
    assert policy.evaluate_inbound(_group(was_mentioned=True), p).should_dispatch is False


def test_mention_only_requires_mention() -> None:
    p = policy.GroupPolicy(reply_mode="mention-only", require_mention=True)
    assert policy.evaluate_inbound(_group(was_mentioned=False), p).should_dispatch is False
    assert policy.evaluate_inbound(_group(was_mentioned=True), p).should_dispatch is True
    assert policy.evaluate_inbound(_group(is_reply_to_bot=True), p).should_dispatch is True


def test_mention_and_watch_hits_watch_list() -> None:
    p = policy.GroupPolicy(
        reply_mode="mention-and-watch",
        require_mention=True,
        watch_mentions=["alice"],
    )
    msg = _group(
        was_mentioned=False,
        body_items=[BodyItem(type="AT", name="Alice", userid="alice")],
    )
    assert policy.evaluate_inbound(msg, p).should_dispatch is True


def test_mention_and_watch_drops_when_nothing_matches() -> None:
    p = policy.GroupPolicy(
        reply_mode="mention-and-watch",
        require_mention=True,
        watch_mentions=["alice"],
    )
    msg = _group(was_mentioned=False, body_items=[])
    assert policy.evaluate_inbound(msg, p).should_dispatch is False


def test_require_mention_false_lets_everything_through() -> None:
    p = policy.GroupPolicy(
        reply_mode="mention-and-watch",
        require_mention=False,
    )
    assert policy.evaluate_inbound(_group(was_mentioned=False), p).should_dispatch is True
