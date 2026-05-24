from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from hermes_infoflow import message_store as ms
from hermes_infoflow.bot import Bot
from hermes_infoflow.itypes import BodyItem, IncomingMessage, SentResult
from hermes_infoflow.message_store import MessageStore
from hermes_infoflow.policy import GroupPolicy
from hermes_infoflow.recall import get_inbound_body
from hermes_infoflow.sent_store import SentMessageStore


def _bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Bot, MessageStore, SentMessageStore]:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    settings = {
        "app_key": "app-key",
        "app_agent_id": 6471,
        "robot_name": "helper",
        "robot_id": "999",
    }
    sent_store = SentMessageStore()
    message_store = MessageStore(account_id="6471")
    bot = Bot(
        settings=settings,
        policy=GroupPolicy(reply_mode="record"),
        serverapi=SimpleNamespace(robot_id="999"),
        sent_store=sent_store,
        dedup_set=sent_store.dedup_set,
        message_store=message_store,
    )
    return bot, message_store, sent_store


def _install_gateway_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BasePlatformAdapter:
        @staticmethod
        def truncate_message(text: str, limit: int) -> list[str]:
            del limit
            return [text]

    gateway_mod = ModuleType("gateway")
    platforms_mod = ModuleType("gateway.platforms")
    base_mod = ModuleType("gateway.platforms.base")
    base_mod.BasePlatformAdapter = _BasePlatformAdapter
    monkeypatch.setitem(sys.modules, "gateway", gateway_mod)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms_mod)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", base_mod)


def test_bot_init_upserts_self_participant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bot(tmp_path, monkeypatch)
    store = MessageStore(account_id="6471")
    rec = store.find_bot_by_agent_id("6471")
    assert rec is not None
    assert rec.key == "bot:6471"
    assert rec.imid == "999"
    assert rec.name == "helper"


@pytest.mark.asyncio
async def test_plugin_sent_echo_updates_message_store_before_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, store, sent_store = _bot(tmp_path, monkeypatch)
    sent_store.record("group:1", "mid-1", msgseqid="seq-1", digest="sent")
    store.persist_group(
        message_id="mid-1",
        group_id="1",
        sender="bot:6471",
        self_id="bot:6471",
        is_outgoing=True,
        content="sent",
    )

    result = await bot.process_inbound(
        IncomingMessage(
            message_id="mid-1",
            dedupe_key="mid-1",
            text="echo",
            body_for_agent="echo",
            group_id="1",
            sender_is_bot=True,
            sender_agent_id="6471",
            msgid2="msgid2-1",
            raw_data={"fromid": "999", "message": {"body": []}},
        )
    )

    assert result.should_dispatch is False
    assert result.decision.reason == "own-echo:plugin-sent"
    found = store.find_group("mid-1")
    assert found is not None
    assert found.is_outgoing is True
    assert found.content == "echo"
    assert found.msg_id2 == "msgid2-1"
    assert found.raw_json == '{"fromid": "999", "message": {"body": []}}'


@pytest.mark.asyncio
async def test_duplicate_foreign_message_is_recorded_without_outgoing_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, store, sent_store = _bot(tmp_path, monkeypatch)
    sent_store.mark_seen("human-1")

    result = await bot.process_inbound(
        IncomingMessage(
            message_id="human-1",
            dedupe_key="human-1",
            text="hello",
            body_for_agent="hello",
            group_id="1",
            sender_id="alice",
            raw_data={"fromuserid": "alice"},
        )
    )

    assert result.should_dispatch is False
    assert result.decision.reason == "duplicate"
    found = store.find_group("human-1")
    assert found is not None
    assert found.sender == "user:alice"
    assert found.is_outgoing is False


@pytest.mark.asyncio
async def test_group_at_all_is_recorded_separately_from_direct_mention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, store, _sent_store = _bot(tmp_path, monkeypatch)

    result = await bot.process_inbound(
        IncomingMessage(
            message_id="at-all-1",
            dedupe_key="at-all-1",
            text="announcement",
            body_for_agent="@所有人 announcement",
            group_id="1",
            sender_id="alice",
            bot_was_mentioned=False,
            body_items=[BodyItem(type="AT", at_all=True)],
            raw_data={"message": {"body": [{"type": "AT", "atall": True}]}},
        )
    )

    assert result.should_dispatch is False
    found = store.find_group("at-all-1")
    assert found is not None
    assert found.mentions_you is False
    assert found.mentions_everyone is True


@pytest.mark.asyncio
async def test_at_only_message_registers_rendered_reply_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, store, _sent_store = _bot(tmp_path, monkeypatch)

    result = await bot.process_inbound(
        IncomingMessage(
            message_id="at-only-reply-context",
            dedupe_key="at-only-reply-context",
            text="",
            group_id="1",
            sender_id="alice",
            bot_was_mentioned=True,
            is_at_only=True,
            body_items=[BodyItem(type="AT", name="helper", robot_id="999")],
            raw_data={"message": {"body": [{"type": "AT", "robotid": "999"}]}},
        )
    )

    assert result.should_dispatch is False
    found = store.find_group("at-only-reply-context")
    assert found is not None
    assert found.content.startswith("（仅@了以下对象，无正文：@helper")
    assert get_inbound_body("at-only-reply-context") == found.content


@pytest.mark.asyncio
async def test_group_robot_id_equal_to_app_agent_id_is_not_self_mention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, store, _sent_store = _bot(tmp_path, monkeypatch)

    result = await bot.process_inbound(
        IncomingMessage(
            message_id="robot-id-app-agent-id",
            dedupe_key="robot-id-app-agent-id",
            text="ping",
            body_for_agent="@other bot (robotid:6471) ping",
            group_id="1",
            sender_id="alice",
            bot_was_mentioned=False,
            body_items=[
                BodyItem(type="AT", name="other bot", robot_id="6471"),
                BodyItem(type="TEXT", content="ping"),
            ],
            raw_data={"message": {"body": [{"type": "AT", "robotid": "6471"}]}},
        )
    )

    assert result.should_dispatch is False
    found = store.find_group("robot-id-app-agent-id")
    assert found is not None
    assert found.mentions_you is False
    assert found.mentions_other_people is True
    assert found.content == "@other bot ping"
    assert "robotid" not in found.content


@pytest.mark.asyncio
async def test_group_robot_id_maps_to_agent_id_via_participants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, store, _sent_store = _bot(tmp_path, monkeypatch)
    store.upsert_participant(
        participant_type="bot",
        agent_id="7000",
        imid="12345",
        name="other bot",
    )
    msg = IncomingMessage(
        message_id="mapped-robot-id",
        dedupe_key="mapped-robot-id",
        text="ping",
        body_for_agent="@other bot (robotid:12345) ping",
        group_id="1",
        sender_id="alice",
        bot_was_mentioned=False,
        mention_robot_ids=["12345"],
        body_items=[
            BodyItem(type="AT", name="other bot", robot_id="12345"),
            BodyItem(type="TEXT", content="ping"),
        ],
        raw_data={"message": {"body": [{"type": "AT", "robotid": "12345"}]}},
    )

    result = await bot.process_inbound(msg)

    assert result.should_dispatch is False
    assert msg.mention_robot_ids == ["12345"]
    assert msg.mention_agent_ids == [7000]
    found = store.find_group("mapped-robot-id")
    assert found is not None
    assert found.mentions_other_people is True
    assert found.content == "@other bot (agent_id:7000) ping"
    assert "robotid" not in found.content
    assert "12345" not in found.content


@pytest.mark.asyncio
async def test_external_private_bot_echo_is_recorded_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, store, _sent_store = _bot(tmp_path, monkeypatch)

    result = await bot.process_inbound(
        IncomingMessage(
            message_id="dm-bot-echo",
            dedupe_key="dm-bot-echo",
            text="external bot echo",
            body_for_agent="external bot echo",
            dm_user_id="alice",
            sender_is_bot=True,
            sender_agent_id="6471",
            sender_imid="999",
            raw_data={"FromId": "999", "ToUserId": "alice"},
        )
    )

    assert result.should_dispatch is False
    assert result.decision.reason == "own-echo:external"
    found = store.find_dm("dm-bot-echo")
    assert found is not None
    assert found.peer == "user:alice"
    assert found.sender == "bot:6471"
    assert found.is_outgoing is True


@pytest.mark.asyncio
async def test_send_message_records_partial_ids_from_failed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_gateway_stub(monkeypatch)
    bot, store, sent_store = _bot(tmp_path, monkeypatch)

    async def fake_send_to_group(*args, **kwargs):
        return SentResult(
            success=False,
            message_id="G-2",
            msgseqid="S-2",
            continuation_message_ids=("G-1",),
            continuation_msgseqids=("S-1",),
            error="second segment failed",
        )

    bot._serverapi = SimpleNamespace(robot_id="999", send_to_group=fake_send_to_group)

    result = await bot.send_message(group_id="1", text="hello")

    assert result.success is False
    assert result.message_id == "G-2"
    assert result.continuation_message_ids == ("G-1",)
    assert store.find_group("G-1") is not None
    assert store.find_group("G-2") is not None
    assert sent_store.find("group:1", "G-1") is not None
    assert sent_store.find("group:1", "G-2") is not None


@pytest.mark.asyncio
async def test_send_image_records_dm_caption_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, store, sent_store = _bot(tmp_path, monkeypatch)

    async def fake_send_image_to_dm(*args, **kwargs):
        return SentResult(
            success=False,
            message_id="CAP-1",
            msgseqid="CAP-SEQ",
            raw_response={"caption_response": {"msgkey": "CAP-1", "msgseqid": "CAP-SEQ"}},
            error="image failed",
        )

    bot._serverapi = SimpleNamespace(robot_id="999", send_image_to_dm=fake_send_image_to_dm)

    result = await bot.send_image(
        dm_user_id="alice",
        image_bytes=b"img",
        caption="caption text",
    )

    assert result.success is False
    found = store.find_dm("CAP-1")
    assert found is not None
    assert found.content == "caption text"
    assert sent_store.find("alice", "CAP-1") is not None


@pytest.mark.asyncio
async def test_send_image_records_group_caption_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, store, sent_store = _bot(tmp_path, monkeypatch)

    async def fake_send_image_to_group(*args, **kwargs):
        return SentResult(
            success=True,
            message_id="IMG-1",
            msgseqid="IMG-SEQ",
            continuation_message_ids=("CAP-1",),
            continuation_msgseqids=("CAP-SEQ",),
            raw_response={"caption_messageids": ["CAP-1"]},
        )

    bot._serverapi = SimpleNamespace(robot_id="999", send_image_to_group=fake_send_image_to_group)

    result = await bot.send_image(
        group_id="1",
        image_bytes=b"img",
        caption="caption text",
    )

    assert result.success is True
    caption = store.find_group("CAP-1")
    image = store.find_group("IMG-1")
    assert caption is not None
    assert caption.content == "caption text"
    assert image is not None
    assert image.content == "[image]"
    assert sent_store.find("group:1", "CAP-1") is not None
    assert sent_store.find("group:1", "IMG-1") is not None


@pytest.mark.asyncio
async def test_send_image_records_group_caption_as_image_without_explicit_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, store, sent_store = _bot(tmp_path, monkeypatch)

    async def fake_send_image_to_group(*args, **kwargs):
        return SentResult(
            success=True,
            message_id="IMG-1",
            msgseqid="IMG-SEQ",
            continuation_message_ids=("CAP-1",),
            continuation_msgseqids=("CAP-SEQ",),
            raw_response={
                "messageids": ["CAP-1", "IMG-1"],
                "msgseqids": ["CAP-SEQ", "IMG-SEQ"],
            },
        )

    bot._serverapi = SimpleNamespace(robot_id="999", send_image_to_group=fake_send_image_to_group)

    result = await bot.send_image(
        group_id="1",
        image_bytes=b"img",
        caption="caption text",
    )

    assert result.success is True
    caption = store.find_group("CAP-1")
    image = store.find_group("IMG-1")
    assert caption is not None
    assert caption.content == "[image]"
    assert image is not None
    assert image.content == "[image]"
    assert sent_store.find("group:1", "CAP-1") is not None
    assert sent_store.find("group:1", "IMG-1") is not None
