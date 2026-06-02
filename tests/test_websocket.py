from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_infoflow.itypes import IncomingMessage
from hermes_infoflow.parser import AccountConfig
from hermes_infoflow.websocket import (
    FRAME_CONTROL,
    FRAME_DATA,
    Frame,
    FrameCodec,
    Header,
    WebSocketReceiver,
)


class _FakeWS:
    closed = False

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


def test_frame_codec_round_trips_json_payload() -> None:
    frame = FrameCodec.json_frame(
        seq_id=123,
        log_id="log-1",
        service=7,
        method=FRAME_DATA,
        headers=[Header(key="type", value="event")],
        payload={"message": "ok", "id": 1234567890123456789},
    )

    decoded = FrameCodec.decode(FrameCodec.encode(frame))

    assert decoded.seq_id == 123
    assert decoded.log_id == "log-1"
    assert decoded.service == 7
    assert decoded.method == FRAME_DATA
    assert [(h.key, h.value) for h in decoded.headers] == [("type", "event")]
    assert json.loads(decoded.payload.decode("utf-8")) == {
        "message": "ok",
        "id": 1234567890123456789,
    }


def _receiver(received: list[IncomingMessage]) -> tuple[WebSocketReceiver, _FakeWS]:
    def to_incoming(raw):
        return IncomingMessage(
            message_id=str(raw.message_id or ""),
            text=raw.text,
            group_id=raw.group_id,
            sender_id=raw.from_user,
            bot_was_mentioned=raw.was_mentioned,
            dedupe_key=raw.dedupe_key() or "",
            raw_data=raw.raw_msgdata,
            event_type=raw.event_type,
        )

    async def on_message(msg: IncomingMessage) -> None:
        received.append(msg)

    receiver = WebSocketReceiver(
        serverapi=SimpleNamespace(
            http_session=None,
            parser_account=AccountConfig(
                check_token="",
                encoding_aes_key="",
                robot_name="helper",
            ),
            to_incoming=to_incoming,
        ),
        sent_message_ids=set(),
        settings={"app_key": "k", "app_secret": "s"},
        on_message=on_message,
    )
    ws = _FakeWS()
    receiver._ws = ws
    return receiver, ws


@pytest.mark.asyncio
async def test_data_frame_schedules_message_and_sends_ack() -> None:
    received: list[IncomingMessage] = []
    receiver, ws = _receiver(received)
    payload = {
        "eventtype": "MESSAGE_RECEIVE",
        "groupid": "42",
        "message": {
            "header": {
                "fromuserid": "alice",
                "groupid": "42",
                "messageid": "mid-1",
            },
            "body": [{"type": "TEXT", "content": "hello"}],
        },
    }
    frame = FrameCodec.json_frame(seq_id=99, method=FRAME_DATA, payload=payload)

    await receiver._handle_frame_bytes(FrameCodec.encode(frame))
    await asyncio.sleep(0)

    assert [m.message_id for m in received] == ["mid-1"]
    assert len(ws.sent) == 1
    ack = FrameCodec.decode(ws.sent[0])
    assert ack.method == FRAME_DATA
    assert [(h.key, h.value) for h in ack.headers] == [("type", "event_ack")]
    assert json.loads(ack.payload.decode("utf-8")) == {
        "ackSeqId": 99,
        "code": 0,
        "message": "ok",
    }


@pytest.mark.asyncio
async def test_raw_wrapped_group_payload_is_not_treated_as_private() -> None:
    received: list[IncomingMessage] = []
    receiver, ws = _receiver(received)
    payload = {
        "raw": {
            "eventtype": "MESSAGE_RECEIVE",
            "groupid": "42",
            "message": {
                "header": {
                    "fromuserid": "alice",
                    "groupid": "42",
                    "messageid": "mid-raw",
                },
                "body": [{"type": "TEXT", "content": "hello"}],
            },
        }
    }
    frame = FrameCodec.json_frame(seq_id=98, method=FRAME_DATA, payload=payload)

    await receiver._handle_frame_bytes(FrameCodec.encode(frame))
    await asyncio.sleep(0)

    assert [(m.message_id, m.group_id) for m in received] == [("mid-raw", "42")]
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_valid_ignored_payload_is_acked() -> None:
    received: list[IncomingMessage] = []
    receiver, ws = _receiver(received)
    frame = FrameCodec.json_frame(
        seq_id=97,
        method=FRAME_DATA,
        payload={"content": "missing sender"},
    )

    await receiver._handle_frame_bytes(FrameCodec.encode(frame))
    await asyncio.sleep(0)

    assert received == []
    assert len(ws.sent) == 1
    ack = FrameCodec.decode(ws.sent[0])
    assert json.loads(ack.payload.decode("utf-8"))["ackSeqId"] == 97


@pytest.mark.asyncio
async def test_malformed_json_payload_is_not_acked() -> None:
    received: list[IncomingMessage] = []
    receiver, ws = _receiver(received)
    frame = Frame(seq_id=100, method=FRAME_DATA, payload=b"{")

    await receiver._handle_frame_bytes(FrameCodec.encode(frame))
    await asyncio.sleep(0)

    assert received == []
    assert ws.sent == []


@pytest.mark.asyncio
async def test_group_forward_duplicate_can_upgrade_to_later_mention() -> None:
    received: list[IncomingMessage] = []
    receiver, ws = _receiver(received)
    first_payload = {
        "eventtype": "ALL_MESSAGE_FORWARD",
        "groupid": "42",
        "message": {
            "header": {
                "fromuserid": "alice",
                "groupid": "42",
                "messageid": "same-mid",
                "clientmsgid": "same-client",
            },
            "body": [{"type": "TEXT", "content": "ambient"}],
        },
    }
    second_payload = {
        "eventtype": "ALL_MESSAGE_FORWARD",
        "groupid": "42",
        "message": {
            "header": {
                "fromuserid": "alice",
                "groupid": "42",
                "messageid": "same-mid",
                "clientmsgid": "same-client",
            },
            "body": [
                {"type": "AT", "name": "helper", "robotid": "8675309"},
                {"type": "TEXT", "content": "direct"},
            ],
        },
    }

    await receiver._handle_frame_bytes(
        FrameCodec.encode(
            FrameCodec.json_frame(seq_id=101, method=FRAME_DATA, payload=first_payload)
        )
    )
    await receiver._handle_frame_bytes(
        FrameCodec.encode(
            FrameCodec.json_frame(seq_id=102, method=FRAME_DATA, payload=second_payload)
        )
    )
    await asyncio.sleep(0)

    assert [(m.message_id, m.text, m.bot_was_mentioned) for m in received] == [
        ("same-mid", "ambient", False),
        ("same-mid", "direct", True),
    ]
    assert len(ws.sent) == 2


@pytest.mark.asyncio
async def test_control_ping_replies_with_pong() -> None:
    received: list[IncomingMessage] = []
    receiver, ws = _receiver(received)
    frame = Frame(
        seq_id=101,
        log_id="log-1",
        method=FRAME_CONTROL,
        headers=[Header(key="type", value="ping")],
    )

    await receiver._handle_frame_bytes(FrameCodec.encode(frame))

    assert received == []
    assert len(ws.sent) == 1
    pong = FrameCodec.decode(ws.sent[0])
    assert pong.method == FRAME_CONTROL
    assert pong.log_id == "log-1"
    assert [(h.key, h.value) for h in pong.headers] == [("type", "pong")]


@pytest.mark.asyncio
async def test_reconnect_loop_catches_connect_failures() -> None:
    received: list[IncomingMessage] = []
    receiver, _ws = _receiver(received)
    calls = 0

    async def connect_once() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("connect failed")

    receiver._connect_once = connect_once
    receiver._client_config = {"reconnect_count": 0}
    receiver._ws = None
    receiver._stopped = False

    await receiver._listen_with_reconnect()

    assert calls == 1
    assert receiver._stopped is True
