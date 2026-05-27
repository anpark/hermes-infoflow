from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

from hermes_infoflow import tools as tools_mod
from hermes_infoflow.tools import SEND_MESSAGE_TOOL_SCHEMA, make_send_message_handler

_TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1Pe"
    "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _SentStore:
    def __init__(self) -> None:
        self.records: list[dict[str, str]] = []

    def record(self, **kwargs):
        self.records.append(kwargs)


class _Bot:
    def __init__(self) -> None:
        self.records: list[dict[str, str | None]] = []

    def _record_sent(self, **kwargs) -> None:
        self.records.append(kwargs)


class _MessageStore:
    def find_any(self, message_id: str):
        if message_id == "MID":
            return SimpleNamespace(content="引用预览", msg_id2="MSGID2")
        return None


class _ServerAPI:
    def __init__(self) -> None:
        self.group_calls: list[dict] = []
        self.private_calls: list[dict] = []

    async def get_group_members(self, group_id: str, **_kwargs):
        assert group_id == "4507088"
        return [
            SimpleNamespace(uid="chengbo05", is_bot=False, agent_id=0, name="成博"),
            SimpleNamespace(uid="17212", is_bot=True, agent_id=17212, name="Robot A"),
        ]

    async def send_group_structured(self, group_id: str, **kwargs):
        self.group_calls.append({"group_id": group_id, **kwargs})
        idx = len(self.group_calls)
        return SimpleNamespace(
            success=True,
            message_id=f"G{idx}",
            msgseqid=f"S{idx}",
            continuation_message_ids=(),
            continuation_msgseqids=(),
            error="",
        )

    async def send_private_structured(self, user_id: str, **kwargs):
        self.private_calls.append({"user_id": user_id, **kwargs})
        idx = len(self.private_calls)
        return SimpleNamespace(
            success=True,
            message_id=f"P{idx}",
            msgseqid="",
            continuation_message_ids=(),
            continuation_msgseqids=(),
            error="",
        )


class _Adapter:
    def __init__(self) -> None:
        self._settings = {"app_agent_id": "999"}
        self._http_session = None
        self._serverapi = _ServerAPI()
        self._sent_store = _SentStore()
        self._message_store = _MessageStore()
        self._bot = _Bot()
        self.events: list[dict] = []

    @staticmethod
    def _effective_session(_session):
        return None

    async def _load_image_bytes(self, _image_url: str) -> bytes:
        return _TINY_PNG_BYTES

    def _push_infoflow_event(self, _event, **kwargs) -> None:
        self.events.append(kwargs)


def test_infoflow_send_message_schema_exposes_expected_inputs() -> None:
    assert SEND_MESSAGE_TOOL_SCHEMA["name"] == "infoflow_send_message"
    props = SEND_MESSAGE_TOOL_SCHEMA["parameters"]["properties"]
    assert SEND_MESSAGE_TOOL_SCHEMA["parameters"]["required"] == ["target"]
    assert "image_paths" in props
    assert "reply_to" in props
    assert "mention_agent_ids" in props
    assert "bot:<agentId>" in props["mention_agent_ids"]["description"]


def test_infoflow_send_message_rejects_bot_private_target(monkeypatch) -> None:
    monkeypatch.setattr(tools_mod, "_get_live_adapter", lambda: _Adapter())

    raw = asyncio.run(make_send_message_handler()({
        "target": "bot:17212",
        "message": "hello",
    }))

    result = json.loads(raw)
    assert result["success"] is False
    assert result["reason"] == "invalid_target"
    assert "unsupported_target" in result["error"]


def test_infoflow_send_message_rejects_private_mentions(monkeypatch) -> None:
    monkeypatch.setattr(tools_mod, "_get_live_adapter", lambda: _Adapter())

    raw = asyncio.run(make_send_message_handler()({
        "target": "user:chengbo05",
        "message": "hello",
        "mention_user_ids": ["chengbo05"],
    }))

    result = json.loads(raw)
    assert result["success"] is False
    assert result["reason"] == "invalid_mentions"


def test_infoflow_send_message_private_reply_only_uses_text_payload(monkeypatch) -> None:
    adapter = _Adapter()
    monkeypatch.setattr(tools_mod, "_get_live_adapter", lambda: adapter)

    raw = asyncio.run(make_send_message_handler()({
        "target": "infoflow:user:chengbo05",
        "reply_to": "MID",
    }))

    result = json.loads(raw)
    assert result == {
        "success": True,
        "target": "user:chengbo05",
        "chat_type": "private",
        "sent_messages": [
            {"message_id": "P1", "kind": "text", "preview": ""}
        ],
    }
    call = adapter._serverapi.private_calls[0]
    assert call["user_id"] == "chengbo05"
    assert call["text"] == ""
    assert call["reply_targets"] == [
        {"message_id": "MID", "preview": "引用预览", "msgid2": "MSGID2"}
    ]
    assert adapter._sent_store.records[0]["chat_id"] == "chengbo05"
    assert adapter._bot.records[0]["dm_user_id"] == "chengbo05"


def test_infoflow_send_message_group_media_order_and_reply_once(
    monkeypatch,
    tmp_path,
) -> None:
    image1 = tmp_path / "one.png"
    image2 = tmp_path / "two.png"
    image3 = tmp_path / "three.png"
    for path in (image1, image2, image3):
        path.write_bytes(_TINY_PNG_BYTES)
    adapter = _Adapter()
    monkeypatch.setattr(tools_mod, "_get_live_adapter", lambda: adapter)

    raw = asyncio.run(make_send_message_handler()({
        "target": "4507088",
        "message": f"正文1 MEDIA:{image1} 正文2 MEDIA:{image2}",
        "image_paths": [str(image2), str(image3)],
        "reply_to": {"message_id": "MID"},
    }))

    result = json.loads(raw)
    assert result["success"] is True
    assert result["target"] == "group:4507088"
    assert result["chat_type"] == "group"
    assert result["sent_messages"] == [
        {"message_id": "G1", "kind": "mixed", "preview": "正文1 [image] 正文2"},
        {"message_id": "G2", "kind": "image", "preview": "[image]"},
        {"message_id": "G3", "kind": "image", "preview": "[image]"},
    ]
    assert len(adapter._serverapi.group_calls) == 3
    first = adapter._serverapi.group_calls[0]
    assert first["group_id"] == "4507088"
    assert first["msgtype"] == "IMAGE"
    assert [item["type"] for item in first["body"]] == ["TEXT", "IMAGE", "TEXT"]
    assert first["body"][0]["content"] == "正文1 "
    assert first["body"][2]["content"] == " 正文2 "
    assert first["reply_target"] == {
        "message_id": "MID",
        "preview": "引用预览",
        "msgid2": "MSGID2",
    }
    assert adapter._serverapi.group_calls[1]["reply_target"] is None
    assert adapter._serverapi.group_calls[2]["reply_target"] is None
    assert all("msgseqid" not in item for item in result["sent_messages"])
    assert all(str(tmp_path) not in item["preview"] for item in result["sent_messages"])


def test_infoflow_send_message_group_at_items_keep_body_order(monkeypatch) -> None:
    adapter = _Adapter()
    monkeypatch.setattr(tools_mod, "_get_live_adapter", lambda: adapter)

    raw = asyncio.run(make_send_message_handler()({
        "target": "group:4507088",
        "message": "hi @chengbo05 then @17212",
    }))

    result = json.loads(raw)
    assert result["success"] is True
    assert result["sent_messages"] == [
        {"message_id": "G1", "kind": "mixed", "preview": "hi @chengbo05 then @17212"}
    ]
    body = adapter._serverapi.group_calls[0]["body"]
    assert body == [
        {"type": "TEXT", "content": "hi "},
        {"type": "AT", "atuserids": ["chengbo05"]},
        {"type": "TEXT", "content": " then "},
        {"type": "AT", "atagentids": [17212]},
    ]


def test_infoflow_send_message_records_failed_result_ids_as_partial(monkeypatch) -> None:
    adapter = _Adapter()

    async def fail_with_id(group_id: str, **kwargs):
        adapter._serverapi.group_calls.append({"group_id": group_id, **kwargs})
        return SimpleNamespace(
            success=False,
            message_id="G-PARTIAL",
            msgseqid="S-PARTIAL",
            continuation_message_ids=(),
            continuation_msgseqids=(),
            error="downstream timeout after send",
        )

    adapter._serverapi.send_group_structured = fail_with_id
    monkeypatch.setattr(tools_mod, "_get_live_adapter", lambda: adapter)

    raw = asyncio.run(make_send_message_handler()({
        "target": "group:4507088",
        "message": "可能已发出",
    }))

    result = json.loads(raw)
    assert result["success"] is False
    assert result["reason"] == "partial_failure"
    assert result["sent_messages"] == [
        {"message_id": "G-PARTIAL", "kind": "text", "preview": "可能已发出"}
    ]
    assert "do not resend" in result["retry_note"]
    assert adapter._sent_store.records[0]["messageid"] == "G-PARTIAL"
