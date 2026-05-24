from __future__ import annotations

import asyncio

from hermes_infoflow import serverapi as serverapi_mod
from hermes_infoflow.parser import BodyItem as ParserBodyItem
from hermes_infoflow.parser import InboundMessage
from hermes_infoflow.serverapi import ServerAPI


def _settings() -> dict[str, object]:
    return {
        "api_host": "https://api.im.baidu.com",
        "app_key": "k",
        "app_secret": "s",
        "check_token": "tok",
        "encoding_aes_key": "aes",
        "robot_name": "helper",
        "robot_id": "999",
        "app_agent_id": 6471,
    }


def test_send_to_dm_success_uses_private_response_only(monkeypatch) -> None:
    async def fake_send_private(account, *, to_user, contents, session=None):
        return {"ok": True, "msgkey": "DM-1", "msgseqid": "SEQ-1"}

    monkeypatch.setattr(
        serverapi_mod._api,
        "send_private_message",
        fake_send_private,
    )
    api = ServerAPI(settings=_settings())

    result = asyncio.run(api.send_to_dm("alice", "hello", session=object()))

    assert result.success is True
    assert result.message_id == "DM-1"
    assert result.msgseqid == "SEQ-1"
    assert result.continuation_message_ids == ()


def test_send_image_to_dm_returns_caption_as_continuation(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_send_private(account, *, to_user, contents, session=None):
        calls.append(contents[0].type)
        if contents[0].type == "markdown":
            return {"ok": True, "msgkey": "CAP-1", "msgseqid": "CAP-SEQ"}
        return {"ok": True, "msgkey": "IMG-1", "msgseqid": "IMG-SEQ"}

    monkeypatch.setattr(
        serverapi_mod._api,
        "send_private_message",
        fake_send_private,
    )
    api = ServerAPI(settings=_settings())

    result = asyncio.run(
        api.send_image_to_dm("alice", b"image-bytes", caption="caption", session=object())
    )

    assert calls == ["markdown", "image"]
    assert result.success is True
    assert result.message_id == "IMG-1"
    assert result.msgseqid == "IMG-SEQ"
    assert result.continuation_message_ids == ("CAP-1",)
    assert result.continuation_msgseqids == ("CAP-SEQ",)
    assert result.raw_response["caption_response"]["msgkey"] == "CAP-1"


def test_send_image_to_group_does_not_guess_caption_message_id(monkeypatch) -> None:
    async def fake_send_group(account, *, group_id, contents, reply_to=None, session=None):
        assert [item.type for item in contents] == ["markdown", "image"]
        return {
            "ok": True,
            "messageid": "IMG-1",
            "msgseqid": "IMG-SEQ",
            "messageids": ["CAP-1", "IMG-1"],
            "msgseqids": ["CAP-SEQ", "IMG-SEQ"],
        }

    monkeypatch.setattr(
        serverapi_mod._api,
        "send_group_message",
        fake_send_group,
    )
    api = ServerAPI(settings=_settings())

    result = asyncio.run(
        api.send_image_to_group("4507088", b"image-bytes", caption="caption", session=object())
    )

    assert result.success is True
    assert result.message_id == "IMG-1"
    assert result.continuation_message_ids == ("CAP-1",)
    assert "caption_messageids" not in result.raw_response


def test_send_image_to_group_partial_image_success_is_not_caption(monkeypatch) -> None:
    async def fake_send_group(account, *, group_id, contents, reply_to=None, session=None):
        assert [item.type for item in contents] == ["markdown", "image"]
        return {
            "ok": False,
            "error": "caption failed",
            "messageid": "IMG-1",
            "msgseqid": "IMG-SEQ",
            "messageids": ["IMG-1"],
            "msgseqids": ["IMG-SEQ"],
        }

    monkeypatch.setattr(
        serverapi_mod._api,
        "send_group_message",
        fake_send_group,
    )
    api = ServerAPI(settings=_settings())

    result = asyncio.run(
        api.send_image_to_group("4507088", b"image-bytes", caption="caption", session=object())
    )

    assert result.success is False
    assert result.message_id == "IMG-1"
    assert result.continuation_message_ids == ()
    assert "caption_messageids" not in result.raw_response


def test_send_to_group_failure_preserves_partial_success_ids(monkeypatch) -> None:
    async def fake_send_group(account, *, group_id, contents, reply_to=None, session=None):
        return {
            "ok": False,
            "error": "second segment failed",
            "messageid": "G-2",
            "msgseqid": "S-2",
            "messageids": ["G-1", "G-2"],
            "msgseqids": ["", "S-2"],
        }

    monkeypatch.setattr(
        serverapi_mod._api,
        "send_group_message",
        fake_send_group,
    )
    api = ServerAPI(settings=_settings())

    result = asyncio.run(api.send_to_group("4507088", "hello", session=object()))

    assert result.success is False
    assert result.error == "second segment failed"
    assert result.message_id == "G-2"
    assert result.msgseqid == "S-2"
    assert result.continuation_message_ids == ("G-1",)
    assert result.continuation_msgseqids == ("",)


def test_private_bot_echo_converts_to_bot_sender() -> None:
    api = ServerAPI(settings=_settings())
    incoming = api.to_incoming(
        InboundMessage(
            chat_type="dm",
            from_user="alice",
            text="bot echo",
            body_for_agent="bot echo",
            message_id="DM-ECHO",
            fromid="999",
            is_bot_sender=True,
            sender_agent_id="6471",
        )
    )

    assert incoming.dm_user_id == "alice"
    assert incoming.sender_id == ""
    assert incoming.sender_imid == "999"
    assert incoming.sender_is_bot is True
    assert incoming.sender_agent_id == "6471"


def test_to_incoming_normalizes_body_item_field_names() -> None:
    api = ServerAPI(settings=_settings())
    incoming = api.to_incoming(
        InboundMessage(
            chat_type="group",
            from_user="alice",
            text="hello",
            body_for_agent="@Alice hello",
            message_id="G-IN",
            group_id="4507088",
            body_items=[
                ParserBodyItem(
                    type="AT",
                    name="Alice",
                    userid="alice",
                    robotid="12345",
                    atall=True,
                    downloadurl="https://example.test/a.png",
                    messageid="QUOTE-1",
                )
            ],
        )
    )

    item = incoming.body_items[0]
    assert item.user_id == "alice"
    assert item.robot_id == "12345"
    assert item.at_all is True
    assert item.download_url == "https://example.test/a.png"
    assert item.message_id == "QUOTE-1"
    assert not hasattr(item, "userid")
    assert not hasattr(item, "robotid")


def test_to_incoming_coerces_string_false_body_booleans() -> None:
    api = ServerAPI(settings=_settings())
    incoming = api.to_incoming(
        InboundMessage(
            chat_type="group",
            from_user="alice",
            text="hello",
            body_for_agent="hello",
            message_id="G-IN",
            group_id="4507088",
            body_items=[
                ParserBodyItem(
                    type="AT",
                    name="Alice",
                    userid="alice",
                    atall="false",
                    is_bot_message="false",
                )
            ],
        )
    )

    item = incoming.body_items[0]
    assert item.at_all is False
    assert item.is_bot_message is False


def test_to_incoming_normalizes_reply_target_field_names() -> None:
    api = ServerAPI(settings=_settings())
    incoming = api.to_incoming(
        InboundMessage(
            chat_type="group",
            from_user="alice",
            text="reply",
            body_for_agent="reply",
            message_id="G-REPLY",
            group_id="4507088",
            reply_targets=[
                {
                    "messageid": "BOT-MSG",
                    "preview": "old",
                    "isBotMessage": True,
                    "platformIsBotMessage": True,
                    "sender_imid": "999",
                }
            ],
        )
    )

    target = incoming.reply_targets[0]
    assert target.message_id == "BOT-MSG"
    assert target.preview == "old"
    assert target.is_bot_message is True
    assert target.platform_is_bot_message is True
    assert target.sender_imid == "999"


def test_to_incoming_coerces_string_false_reply_target_booleans() -> None:
    api = ServerAPI(settings=_settings())
    incoming = api.to_incoming(
        InboundMessage(
            chat_type="group",
            from_user="alice",
            text="reply",
            body_for_agent="reply",
            message_id="G-REPLY",
            group_id="4507088",
            reply_targets=[
                {
                    "messageid": "BOT-MSG",
                    "preview": "old",
                    "isBotMessage": "false",
                    "platformIsBotMessage": "false",
                }
            ],
        )
    )

    target = incoming.reply_targets[0]
    assert target.is_bot_message is False
    assert target.platform_is_bot_message is False
