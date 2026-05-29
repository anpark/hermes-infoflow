from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_infoflow.bot import Bot
from hermes_infoflow.policy import GroupPolicy


def _bot() -> Bot:
    serverapi = MagicMock()
    serverapi.robot_id = "999"
    serverapi.send_group_message_intent = AsyncMock()
    serverapi.send_private_message_intent = AsyncMock()
    return Bot(
        settings={"app_key": "k", "app_agent_id": "6471", "robot_id": "999"},
        policy=GroupPolicy(),
        serverapi=serverapi,
        sent_store=MagicMock(),
        dedup_set=set(),
        message_store=MagicMock(),
    )


@pytest.mark.asyncio
async def test_mixed_no_reply_is_suppressed_and_forwarded_to_ops(monkeypatch) -> None:
    monkeypatch.setenv("INFOFLOW_OP_CHANNEL", "4507088")
    bot = _bot()

    result = await bot.send_message(
        group_id="24978967504",
        text="这需要 chengbo05 亲自看。\n\nNO_REPLY",
    )

    assert result.success is True
    bot._serverapi.send_group_message_intent.assert_awaited_once()
    args, kwargs = bot._serverapi.send_group_message_intent.await_args
    assert args[0] == "4507088"
    assert "NO_REPLY suppressed" in kwargs["message"]
    assert "target: group:24978967504" in kwargs["message"]
    assert "这需要 chengbo05 亲自看。" in kwargs["message"]
    assert kwargs["session"] is None


@pytest.mark.asyncio
async def test_plain_no_reply_is_suppressed_without_ops_noise(monkeypatch) -> None:
    monkeypatch.setenv("INFOFLOW_OP_CHANNEL", "4507088")
    bot = _bot()

    result = await bot.send_message(group_id="24978967504", text="NO_REPLY")

    assert result.success is True
    bot._serverapi.send_group_message_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_no_reply_skips_ops_forward_when_ops_is_source(
    monkeypatch,
) -> None:
    monkeypatch.setenv("INFOFLOW_OP_CHANNEL", "24978967504")
    bot = _bot()

    result = await bot.send_message(
        group_id="24978967504",
        text="这需要 chengbo05 亲自看。\n\nNO_REPLY",
    )

    assert result.success is True
    bot._serverapi.send_group_message_intent.assert_not_awaited()
    bot._serverapi.send_private_message_intent.assert_not_awaited()
