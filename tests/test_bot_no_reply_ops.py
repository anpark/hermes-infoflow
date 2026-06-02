from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_infoflow.adapter import _inbound_mid
from hermes_infoflow.bot import Bot, _send_path_cv
from hermes_infoflow.itypes import SentResult
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


def _install_gateway_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BasePlatformAdapter:
        @staticmethod
        def truncate_message(text: str, limit: int) -> list[str]:
            return [text[:limit]]

    gateway_mod = ModuleType("gateway")
    platforms_mod = ModuleType("gateway.platforms")
    base_mod = ModuleType("gateway.platforms.base")
    base_mod.BasePlatformAdapter = _BasePlatformAdapter
    monkeypatch.setitem(sys.modules, "gateway", gateway_mod)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms_mod)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base_mod)


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
async def test_bot_mentioned_plain_no_reply_recovers_without_silent_tool(monkeypatch) -> None:
    _install_gateway_stub(monkeypatch)
    bot = _bot()
    bot._serverapi.send_group_message_intent.return_value = SentResult(
        success=True,
        message_id="OUT-1",
    )

    path_token = _send_path_cv.set("bot-mentioned")
    mid_token = _inbound_mid.set("IN-1")
    try:
        result = await bot.send_message(group_id="12605371", text="NO_REPLY")
    finally:
        _inbound_mid.reset(mid_token)
        _send_path_cv.reset(path_token)

    assert result.success is True
    bot._serverapi.send_group_message_intent.assert_awaited_once()
    _args, kwargs = bot._serverapi.send_group_message_intent.await_args
    assert kwargs["message"] == "收到，我在。你想让我处理什么，直接说就行。"


@pytest.mark.asyncio
async def test_bot_mentioned_no_reply_stays_silent_after_tool_success() -> None:
    bot = _bot()
    bot.mark_silent_tool_success("IN-1")

    path_token = _send_path_cv.set("bot-mentioned")
    mid_token = _inbound_mid.set("IN-1")
    try:
        result = await bot.send_message(group_id="12605371", text="NO_REPLY")
    finally:
        _inbound_mid.reset(mid_token)
        _send_path_cv.reset(path_token)

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
