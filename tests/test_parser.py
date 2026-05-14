"""Tests for hermes_infoflow.parser end-to-end webhook parsing."""

from __future__ import annotations

import json
import os
from urllib.parse import urlencode

import pytest

from hermes_infoflow import crypto, parser
from tests._aes_helpers import aes_ecb_encrypt_b64url, aes_key_b64url


@pytest.fixture
def account() -> tuple[parser.AccountConfig, bytes]:
    raw_key = os.urandom(16)
    return (
        parser.AccountConfig(
            check_token="tok",
            encoding_aes_key=aes_key_b64url(raw_key),
            robot_name="hermes",
            app_agent_id=42,
        ),
        raw_key,
    )


# ---------------------------------------------------------------------------
# echostr probe
# ---------------------------------------------------------------------------


def test_echostr_ok(account):
    acct, _ = account
    sig = crypto.compute_echostr_signature(rn="r1", timestamp="100", check_token="tok")
    body = urlencode({"echostr": "HELLO", "signature": sig, "timestamp": "100", "rn": "r1"})
    res = parser.parse_webhook(
        content_type="application/x-www-form-urlencoded",
        raw_body=body,
        account=acct,
    )
    assert res.kind == "echostr_ok"
    assert res.body == "HELLO"
    assert res.status_code == 200


def test_echostr_bad_signature(account):
    acct, _ = account
    body = urlencode(
        {"echostr": "HELLO", "signature": "0" * 32, "timestamp": "100", "rn": "r1"}
    )
    res = parser.parse_webhook(
        content_type="application/x-www-form-urlencoded",
        raw_body=body,
        account=acct,
    )
    assert res.kind == "echostr_bad"
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Private (DM) form-urlencoded
# ---------------------------------------------------------------------------


def test_private_message_decrypts_and_extracts_msg_id(account):
    acct, raw_key = account
    inner = json.dumps(
        {
            "FromUserId": "alice",
            "FromUserName": "Alice",
            "MsgType": "text",
            "Content": "hello bot",
            "MsgId": 1_859_713_223_686_736_431,
            "CreateTime": 1_700_000_000,
        }
    )
    ct = aes_ecb_encrypt_b64url(inner, raw_key)
    body = urlencode({"messageJson": json.dumps({"Encrypt": ct})})
    res = parser.parse_webhook(
        content_type="application/x-www-form-urlencoded",
        raw_body=body,
        account=acct,
    )
    assert res.kind == "message"
    inbound = res.inbound
    assert inbound.chat_type == "dm"
    assert inbound.from_user == "alice"
    assert inbound.text == "hello bot"
    assert inbound.message_id == "1859713223686736431"
    assert isinstance(inbound.message_id, str)
    assert inbound.was_mentioned is True


def test_private_message_missing_encrypt_field(account):
    acct, _ = account
    body = urlencode({"messageJson": json.dumps({"NotEncrypt": "..."})})
    res = parser.parse_webhook(
        content_type="application/x-www-form-urlencoded",
        raw_body=body,
        account=acct,
    )
    assert res.kind == "http_error"
    assert res.status_code == 400


def test_private_image_message_promotes_to_placeholder(account):
    acct, raw_key = account
    inner = json.dumps(
        {
            "FromUserId": "alice",
            "MsgType": "image",
            "PicUrl": "https://media.infoflow/img1.jpg",
            "MsgId": 12345,
        }
    )
    ct = aes_ecb_encrypt_b64url(inner, raw_key)
    body = urlencode({"messageJson": json.dumps({"Encrypt": ct})})
    res = parser.parse_webhook(
        content_type="application/x-www-form-urlencoded",
        raw_body=body,
        account=acct,
    )
    assert res.kind == "message"
    assert res.inbound.image_urls == ["https://media.infoflow/img1.jpg"]
    assert res.inbound.text == "<media:image>"


# ---------------------------------------------------------------------------
# Group text/plain
# ---------------------------------------------------------------------------


def test_group_message_extracts_mention_and_msgseqid(account):
    acct, raw_key = account
    payload = {
        "message": {
            "header": {
                "fromuserid": "bob",
                "groupid": 123456,
                "messageid": 1_859_713_223_686_736_432,
                "msgseqid": 1_859_713_223_686_736_433,
                "servertime": 1_700_000_000_000,
            },
            "body": [
                {"type": "AT", "name": "hermes", "robotid": "42"},
                {"type": "TEXT", "content": "ping"},
                {
                    "type": "IMAGE",
                    "downloadurl": "https://media.infoflow/img.jpg",
                },
            ],
        }
    }
    ct = aes_ecb_encrypt_b64url(json.dumps(payload), raw_key)
    res = parser.parse_webhook(
        content_type="text/plain",
        raw_body=ct,
        account=acct,
    )
    assert res.kind == "message"
    inbound = res.inbound
    assert inbound.chat_type == "group"
    assert inbound.group_id == "123456"
    assert inbound.was_mentioned is True
    # large-integer IDs round-tripped to str
    assert inbound.message_id == "1859713223686736432"
    assert inbound.msgseqid == "1859713223686736433"
    assert "[at:@hermes]" in inbound.body_for_agent
    assert "ping" in inbound.body_for_agent
    assert inbound.image_urls == ["https://media.infoflow/img.jpg"]


def test_group_message_reply_to_bot_marked_when_in_sent_set(account):
    acct, raw_key = account
    sent_ids = {"77777777777777777"}
    payload = {
        "message": {
            "header": {"fromuserid": "carol", "groupid": 999, "messageid": 1},
            "body": [
                {
                    "type": "replyData",
                    "messageid": "77777777777777777",
                    "preview": "earlier bot reply",
                },
                {"type": "TEXT", "content": "thanks!"},
            ],
        }
    }
    ct = aes_ecb_encrypt_b64url(json.dumps(payload), raw_key)
    res = parser.parse_webhook(
        content_type="text/plain",
        raw_body=ct,
        account=acct,
        sent_message_ids=sent_ids,
    )
    assert res.kind == "message"
    assert res.inbound.is_reply_to_bot is True
    assert res.inbound.reply_targets[0]["messageid"] == "77777777777777777"


def test_unsupported_content_type(account):
    acct, _ = account
    res = parser.parse_webhook(
        content_type="application/json",
        raw_body="{}",
        account=acct,
    )
    assert res.kind == "http_error"
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# Precision protection
# ---------------------------------------------------------------------------


def test_patch_precise_ids_replaces_large_ints_with_str() -> None:
    raw = '{"messageid":1859713223686736431,"msgseqid":1859713223686736432,"x":1}'
    obj = json.loads(raw)
    parser.patch_precise_ids(raw, obj)
    assert obj["messageid"] == "1859713223686736431"
    assert obj["msgseqid"] == "1859713223686736432"
    assert obj["x"] == 1  # untouched
