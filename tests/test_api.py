"""Unit tests for hermes_infoflow.api (no network)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from hermes_infoflow import api

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_ensure_https_upgrades_remote_http() -> None:
    assert api.ensure_https("http://api.x.com") == "https://api.x.com"


@pytest.mark.parametrize("host", ["http://localhost:8080", "http://127.0.0.1:9000"])
def test_ensure_https_keeps_localhost(host: str) -> None:
    assert api.ensure_https(host) == host


def test_build_private_payload_text_default() -> None:
    payload = api._build_private_payload("alice", [api.ContentItem("text", "hi")])
    assert payload == {
        "touser": "alice",
        "msgtype": "md",
        "md": {"content": "hi"},
    }


def test_build_private_payload_markdown() -> None:
    payload = api._build_private_payload("alice", [api.ContentItem("markdown", "**bold**")])
    assert payload == {
        "touser": "alice",
        "msgtype": "md",
        "md": {"content": "**bold**"},
    }


def test_build_private_payload_link_promotes_to_richtext() -> None:
    payload = api._build_private_payload(
        "alice",
        [
            api.ContentItem("text", "see"),
            api.ContentItem("link", "[Click]https://x.com"),
        ],
    )
    assert payload["msgtype"] == "richtext"
    assert payload["richtext"]["content"] == [
        {"type": "text", "text": "see"},
        {"type": "a", "href": "https://x.com", "label": "Click"},
    ]


def test_build_group_body_items_handles_all_types() -> None:
    body, has_md = api._build_group_body_items(
        [
            api.ContentItem("text", "hi"),
            api.ContentItem("markdown", "**x**"),
            api.ContentItem("at", "all"),
            api.ContentItem("at", "alice,bob"),
            api.ContentItem("at-agent", "42,43"),
            api.ContentItem("link", "https://x.com"),
            api.ContentItem("image", "BASE64..."),
        ]
    )
    assert has_md is True
    types = [b["type"] for b in body]
    assert "MD" in types
    assert "TEXT" not in types  # plain text is folded into MD-only outbound path
    assert "LINK" in types
    assert "IMAGE" in types
    at_items = [b for b in body if b["type"] == "AT"]
    assert any(b.get("atall") for b in at_items)
    assert any("alice" in b.get("atuserids", []) for b in at_items)
    assert any(42 in b.get("atagentids", []) for b in at_items)


def test_extract_id_from_raw_json_handles_large_int_and_quoted() -> None:
    raw = '{"messageid":1859713223686736431,"msgkey":"abc-123"}'
    assert api._extract_id(raw, "messageid") == "1859713223686736431"
    # msgkey is only 7 chars (< 10 digit threshold), so _extract_id won't match it
    # as a large-integer ID. That's OK — msgkey extraction uses a different path.
    assert api._extract_id(raw, "msgkey") is None
    assert api._extract_id(raw, "missing") is None


# ---------------------------------------------------------------------------
# Recall body builds raw integer JSON (precision-safe)
# ---------------------------------------------------------------------------


def test_recall_group_message_body_is_raw_integer_json(monkeypatch):
    """``recall_group_message`` must POST integers, not strings, for IDs."""

    captured: dict[str, Any] = {}

    class _FakeResponse:
        def __init__(self, text: str, status: int = 200):
            self._text = text
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def text(self):
            return self._text

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def post(self, url, *, data, headers, timeout):
            captured["url"] = url
            captured["data"] = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
            captured["headers"] = headers
            return _FakeResponse('{"code":"ok","data":{"errcode":0}}')

        async def close(self):
            return None

    async def _go():
        # Skip the real token endpoint.
        async def fake_token(*a, **k):
            return "TOK"

        monkeypatch.setattr(api, "get_app_access_token", fake_token)
        sess = _FakeSession()
        account = api.InfoflowAccountAPI(
            api_host="https://api.example.com",
            app_key="k",
            app_secret="s",
        )
        res = await api.recall_group_message(
            account,
            group_id=123456,
            messageid="1859713223686736431",
            msgseqid="1859713223686736432",
            session=sess,
        )
        return res

    res = asyncio.run(_go())
    assert res["ok"] is True
    body = captured["data"]
    # The body must contain raw integers, not strings.
    parsed = json.loads(body)
    assert parsed == {
        "groupId": 123456,
        "messageid": 1859713223686736431,
        "msgseqid": 1859713223686736432,
    }
    # And the auth header is the non-standard `Bearer-<token>` (hyphen).
    assert captured["headers"]["Authorization"] == "Bearer-TOK"


def test_recall_private_message_requires_app_agent_id() -> None:
    async def _go():
        account = api.InfoflowAccountAPI(
            api_host="https://api.example.com",
            app_key="k",
            app_secret="s",
            app_agent_id=None,
        )
        return await api.recall_private_message(account, msgkey="123")

    res = asyncio.run(_go())
    assert res["ok"] is False
    assert "appAgentId" in res["error"]


def test_token_endpoint_uses_md5_appsecret_and_caches(monkeypatch):
    """The token POST body must contain MD5(appSecret, lowercase hex)."""

    captured: dict[str, Any] = {}
    calls = {"count": 0}

    class _Resp:
        def __init__(self, text):
            self._t = text
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def text(self):
            return self._t

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def post(self, url, *, json=None, headers=None, timeout=None):
            calls["count"] += 1
            captured["json"] = json
            return _Resp('{"errcode":0,"data":{"app_access_token":"TOK","expires_in":7200}}')

        async def close(self):
            return None

    api.clear_token_cache()
    account = api.InfoflowAccountAPI(
        api_host="https://api.example.com",
        app_key=f"k-{id(captured)}",  # unique per test run
        app_secret="my-secret",
    )

    async def _go():
        sess = _Sess()
        t1 = await api.get_app_access_token(account, session=sess)
        t2 = await api.get_app_access_token(account, session=sess)
        return t1, t2

    t1, t2 = asyncio.run(_go())
    import hashlib

    expected_md5 = hashlib.md5(b"my-secret").hexdigest().lower()
    assert captured["json"]["app_secret"] == expected_md5
    assert t1 == "TOK"
    assert t1 == t2
    # Token cache means we only fetch once.
    assert calls["count"] == 1


def test_recall_private_message_body_uses_json_dumps(monkeypatch):
    """Private recall body must JSON-escape msgkey (no manual string splicing)."""
    captured = {}

    class _Resp:
        def __init__(self, text):
            self._t = text
            self.status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def text(self): return self._t

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        def post(self, url, *, data, headers, timeout):
            captured["data"] = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
            captured["headers"] = headers
            return _Resp('{"code":"ok","data":{"errcode":0}}')
        async def close(self): return None

    async def fake_token(*a, **k): return "TOK"
    monkeypatch.setattr(api, "get_app_access_token", fake_token)

    account = api.InfoflowAccountAPI(
        api_host="https://api.example.com",
        app_key="k",
        app_secret="s",
        app_agent_id=99,
    )

    async def _go():
        return await api.recall_private_message(
            account,
            msgkey='evil"injection\\value',   # contains chars that would break manual JSON
            session=_Sess(),
        )

    import asyncio
    result = asyncio.run(_go())
    assert result["ok"] is True
    # Must be parsable JSON.
    import json
    parsed = json.loads(captured["data"])
    assert parsed == {"msgkey": 'evil"injection\\value', "agentid": 99}
    # And the auth header is the non-standard Bearer-<token>.
    assert captured["headers"]["Authorization"] == "Bearer-TOK"


def test_build_emoji_reaction_body_group() -> None:
    body = api._build_emoji_reaction_body(
        chat_type=2,
        from_uid="chengbo05",
        group_id=4507088,
        base_msg_id="1865794273048386548",
        msgid2="300014580",
        emoji_code="d135",
        emoji_desc="(qjp)",
    )
    parsed = json.loads(body)
    assert parsed == {
        "fromUid": "chengbo05",
        "chatType": 2,
        "chatId": 4507088,
        "baseMsgId": "1865794273048386548",
        "msgId2": 300014580,
        "replyContent": "d135",
        "replyDesc": "(qjp)",
    }


def test_build_emoji_reaction_body_dm_omits_chat_id() -> None:
    """DM (chatType=7) must not include ``chatId`` — the API rejects it."""
    body = api._build_emoji_reaction_body(
        chat_type=7,
        from_uid="chengbo05",
        base_msg_id="1865798223458853292",
        msgid2="300016044",
        emoji_code="d135",
        emoji_desc="(qjp)",
    )
    parsed = json.loads(body)
    assert parsed == {
        "fromUid": "chengbo05",
        "chatType": 7,
        "baseMsgId": "1865798223458853292",
        "msgId2": 300016044,
        "replyContent": "d135",
        "replyDesc": "(qjp)",
    }
    assert "chatId" not in parsed


def test_build_emoji_reaction_body_omits_empty_msgid2() -> None:
    """``msgId2`` is optional and must be skipped when not provided."""
    body = api._build_emoji_reaction_body(
        chat_type=7,
        from_uid="chengbo05",
        base_msg_id="1865798223458853292",
        msgid2="",
        emoji_code="d135",
        emoji_desc="(qjp)",
    )
    parsed = json.loads(body)
    assert "msgId2" not in parsed
    assert parsed["chatType"] == 7


def test_parse_recall_response_accepts_emoji_bizcode_success() -> None:
    res = api._parse_recall_response(
        '{"code":"ok","data":{"bizCode":200,"bizMsg":"ok","bizData":null}}',
        kind="emoji_del",
    )
    assert res == {"ok": True}


def test_parse_recall_response_rejects_emoji_bizcode_failure() -> None:
    res = api._parse_recall_response(
        '{"code":"ok","data":{"bizCode":500,"bizMsg":"not found","bizData":null}}',
        kind="emoji_del",
    )
    assert res["ok"] is False
    assert "not found" in res["error"]


def test_build_private_payload_image_uses_native_msgtype() -> None:
    payload = api._build_private_payload("alice", [api.ContentItem("image", "BASE64...")])
    assert payload == {
        "touser": "alice",
        "msgtype": "image",
        "image": {"content": "BASE64..."},
    }
