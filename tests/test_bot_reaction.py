"""Tests for processing-emoji reaction lifecycle in hermes_infoflow.bot."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_infoflow.bot import Bot
from hermes_infoflow.itypes import IncomingMessage, RecallResult
from hermes_infoflow.policy import Action, GroupPolicy, PolicyDecision


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


def _dm_msg(**kwargs) -> IncomingMessage:
    defaults = dict(
        message_id="1865798223458853292",
        text="hi",
        dm_user_id="chengbo05",
        sender_id="chengbo05",
        msgid2="300016044",
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
    assert h["chat_type"] == "group"
    assert h["from_uid"] == "bob"
    assert h["msgid2"] == "300014580"
    assert h["base_msg_id"] == "1865794273048386548"
    assert h["emoji_code"] == "d135"
    assert h["group_id"] == "4507088"


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
async def test_dispatch_adds_reaction_and_deletes_on_no_reply_send() -> None:
    bot = _bot()
    msg = _group_msg()
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="bot-mentioned",
    )
    adapter = MagicMock()
    adapter.build_message_event = AsyncMock(return_value={"event": True})

    async def _handle_message(_event):
        await bot.send_message(group_id="4507088", text="NO_REPLY")

    adapter.handle_message = AsyncMock(side_effect=_handle_message)

    await bot.dispatch_inbound(msg, decision, adapter)

    bot._serverapi.add_message_reaction.assert_awaited_once()
    bot._serverapi.delete_message_reaction.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_does_not_delete_when_handle_message_returns_before_send() -> None:
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
    bot._schedule_reaction_fallback_cleanup = MagicMock()

    await bot.dispatch_inbound(msg, decision, adapter)

    bot._serverapi.add_message_reaction.assert_awaited_once()
    bot._serverapi.delete_message_reaction.assert_not_awaited()
    bot._schedule_reaction_fallback_cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_background_send_inherits_reaction_and_deletes_later() -> None:
    bot = _bot()
    msg = _group_msg()
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="bot-mentioned",
    )
    adapter = MagicMock()
    adapter.build_message_event = AsyncMock(return_value={"event": True})
    release = asyncio.Event()
    background_task: asyncio.Task | None = None

    async def _delayed_send():
        await release.wait()
        await bot.send_message(group_id="4507088", text="NO_REPLY")

    async def _handle_message(_event):
        nonlocal background_task
        background_task = asyncio.create_task(_delayed_send())

    adapter.handle_message = AsyncMock(side_effect=_handle_message)
    bot._schedule_reaction_fallback_cleanup = MagicMock()

    await bot.dispatch_inbound(msg, decision, adapter)
    bot._serverapi.delete_message_reaction.assert_not_awaited()

    release.set()
    assert background_task is not None
    await background_task

    bot._serverapi.delete_message_reaction.assert_awaited_once()


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
async def test_dispatch_handle_error_schedules_fallback_cleanup() -> None:
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
    bot._schedule_reaction_fallback_cleanup = MagicMock()

    with patch("hermes_infoflow.bot.gw_log"):
        await bot.dispatch_inbound(msg, decision, adapter)

    bot._serverapi.delete_message_reaction.assert_not_awaited()
    bot._schedule_reaction_fallback_cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# DM reaction lifecycle
# ---------------------------------------------------------------------------


def test_reaction_handle_dm_always_eligible() -> None:
    bot = _bot()
    msg = _dm_msg()
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="direct-message",
    )
    h = bot._build_reaction_handle(msg, decision)
    assert h is not None
    assert h["chat_type"] == "dm"
    assert h["group_id"] is None
    assert h["from_uid"] == "chengbo05"
    assert h["msgid2"] == "300016044"
    assert h["base_msg_id"] == "1865798223458853292"


def test_reaction_handle_dm_without_msgid2_still_eligible() -> None:
    """DM emoji API accepts an empty ``msgId2`` — never block on it."""
    bot = _bot()
    msg = _dm_msg(msgid2="")
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="direct-message",
    )
    h = bot._build_reaction_handle(msg, decision)
    assert h is not None
    assert h["msgid2"] == ""


def test_reaction_handle_dm_skipped_when_dm_user_degraded() -> None:
    bot = _bot()
    msg = _dm_msg(dm_user_id="IMID:abc")
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="direct-message",
    )
    assert bot._build_reaction_handle(msg, decision) is None


def test_reaction_handle_dm_skipped_for_slash_command() -> None:
    bot = _bot()
    msg = _dm_msg()
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="slash_command",
        command_text="/new",
    )
    assert bot._build_reaction_handle(msg, decision) is None


@pytest.mark.asyncio
async def test_dispatch_dm_adds_reaction_and_deletes_on_reply() -> None:
    bot = _bot()
    msg = _dm_msg()
    decision = PolicyDecision(
        should_dispatch=True,
        action=Action.DISPATCH,
        trigger_reason="direct-message",
    )
    adapter = MagicMock()
    adapter.build_message_event = AsyncMock(return_value={"event": True})

    # Use NO_REPLY sentinel so the test exits the send path via
    # _delete_current_reaction_promise without needing the gateway truncate util.
    async def _handle_message(_event):
        await bot.send_message(dm_user_id="chengbo05", text="NO_REPLY")

    adapter.handle_message = AsyncMock(side_effect=_handle_message)

    await bot.dispatch_inbound(msg, decision, adapter)

    bot._serverapi.add_message_reaction.assert_awaited_once()
    add_kwargs = bot._serverapi.add_message_reaction.await_args.kwargs
    assert add_kwargs["chat_type"] == "dm"
    assert add_kwargs["group_id"] is None
    assert add_kwargs["from_uid"] == "chengbo05"

    bot._serverapi.delete_message_reaction.assert_awaited_once()
    del_kwargs = bot._serverapi.delete_message_reaction.await_args.kwargs
    assert del_kwargs["chat_type"] == "dm"
    assert del_kwargs["from_uid"] == "chengbo05"
    assert del_kwargs["group_id"] is None
