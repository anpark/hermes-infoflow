"""Tests for processing-emoji reaction lifecycle in hermes_infoflow.bot."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_infoflow.bot import Bot
from hermes_infoflow.itypes import IncomingMessage
from hermes_infoflow.policy import Action, GroupPolicy, PolicyDecision
from hermes_infoflow.itypes import RecallResult


def _group_msg(**kwargs) -> IncomingMessage:
    defaults = dict(
        message_id="1865794273048386548",
        text="ping",
        group_id="4507088",
        sender_id="bob",
        msgid2="300014580",
        bot_was_mentioned=True,
    )
    defaults.update(kwargs)
    return IncomingMessage(**defaults)


def _bot() -> Bot:
    policy = GroupPolicy()
    serverapi = MagicMock()
    serverapi.add_message_reaction = AsyncMock(
        return_value=RecallResult(success=True)
    )
    serverapi.delete_message_reaction = AsyncMock(
        return_value=RecallResult(success=True)
    )
    return Bot(
        settings={"app_key": "k", "api_host": "http://localhost"},
        policy=policy,
        serverapi=serverapi,
        sent_store=MagicMock(),
        dedup_set=set(),
        message_store=MagicMock(),
    )


# ---------------------------------------------------------------------------
# _build_reaction_handle eligibility
# ---------------------------------------------------------------------------


def test_reaction_handle_bot_mentioned() -> None:
    bot = _bot()
    msg = _group_msg()
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="bot-mentioned",
    )
    h = bot._build_reaction_handle(msg, decision)
    assert h is not None
    assert h["from_uid"] == "bob"
    assert h["msgid2"] == "300014580"
    assert h["base_msg_id"] == "1865794273048386548"
    assert h["emoji_code"] == "d135"


def test_reaction_handle_followup_engaged() -> None:
    bot = _bot()
    bot._policy.record_sender_mention("4507088", "bob")
    msg = _group_msg(bot_was_mentioned=False)
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="followUp",
    )
    h = bot._build_reaction_handle(msg, decision)
    assert h is not None


def test_reaction_handle_followup_reply_to_bot() -> None:
    bot = _bot()
    msg = _group_msg(bot_was_mentioned=False, is_reply_to_bot=True)
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="followUp",
    )
    h = bot._build_reaction_handle(msg, decision)
    assert h is not None


def test_reaction_handle_followup_passive_skipped() -> None:
    bot = _bot()
    msg = _group_msg(bot_was_mentioned=False)
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="followUp",
    )
    assert bot._build_reaction_handle(msg, decision) is None


def test_reaction_handle_slash_command_skipped() -> None:
    bot = _bot()
    msg = _group_msg()
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="slash_command",
        command_text="/new",
    )
    assert bot._build_reaction_handle(msg, decision) is None


def test_reaction_handle_missing_msgid2_skipped() -> None:
    bot = _bot()
    msg = _group_msg(msgid2="")
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="bot-mentioned",
    )
    assert bot._build_reaction_handle(msg, decision) is None


def test_reaction_from_uid_bot_sender() -> None:
    bot = _bot()
    msg = _group_msg(sender_is_bot=True, sender_id="", sender_agent_id="6471")
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="bot-mentioned",
    )
    h = bot._build_reaction_handle(msg, decision)
    assert h is not None
    assert h["from_uid"] == "6471"


def test_reaction_from_uid_degraded_bot_skipped() -> None:
    bot = _bot()
    msg = _group_msg(sender_is_bot=True, sender_id="", sender_agent_id="IMID:123")
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="bot-mentioned",
    )
    assert bot._build_reaction_handle(msg, decision) is None


def test_reaction_from_uid_degraded_human_skipped() -> None:
    bot = _bot()
    msg = _group_msg(sender_id="IMID:9999")
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="bot-mentioned",
    )
    assert bot._build_reaction_handle(msg, decision) is None


# ---------------------------------------------------------------------------
# dispatch_inbound lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_adds_and_deletes_reaction() -> None:
    bot = _bot()
    msg = _group_msg()
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="bot-mentioned",
    )
    adapter = MagicMock()
    adapter.build_message_event = AsyncMock(return_value={"event": True})
    adapter.handle_message = AsyncMock()

    await bot.dispatch_inbound(msg, decision, adapter)

    bot._serverapi.add_message_reaction.assert_awaited_once()
    bot._serverapi.delete_message_reaction.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_add_fail_skips_delete() -> None:
    bot = _bot()
    bot._serverapi.add_message_reaction = AsyncMock(
        return_value=RecallResult(success=False, error="fail")
    )
    msg = _group_msg()
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="bot-mentioned",
    )
    adapter = MagicMock()
    adapter.build_message_event = AsyncMock(return_value={})
    adapter.handle_message = AsyncMock()

    await bot.dispatch_inbound(msg, decision, adapter)

    bot._serverapi.add_message_reaction.assert_awaited_once()
    bot._serverapi.delete_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_deletes_reaction_on_handle_error() -> None:
    bot = _bot()
    msg = _group_msg()
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="bot-mentioned",
    )
    adapter = MagicMock()
    adapter.build_message_event = AsyncMock(return_value={})
    adapter.handle_message = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("hermes_infoflow.bot.gw_log"):
        await bot.dispatch_inbound(msg, decision, adapter)

    bot._serverapi.delete_message_reaction.assert_awaited_once()
