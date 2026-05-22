"""Tests for InfoflowAdapter — webhook fire-and-forget, send routing, path safety.

These tests need a real ``BasePlatformAdapter`` so they live behind a
``pytest.importorskip`` guard. When hermes-agent is on PYTHONPATH, they
exercise the adapter end-to-end with a fake aiohttp request.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

gateway_base = pytest.importorskip("gateway.platforms.base")  # noqa: F401  (presence guard)

from hermes_infoflow import api as _api  # noqa: E402
from hermes_infoflow import crypto as _crypto  # noqa: E402
from hermes_infoflow.adapter import InfoflowAdapter  # noqa: E402
from hermes_infoflow.sent_store import SentMessageStore  # noqa: E402
from tests._aes_helpers import aes_ecb_encrypt_b64url, aes_key_b64url  # noqa: E402


@pytest.fixture
def configured_env(monkeypatch):
    """Set the minimum env vars needed to construct an adapter.

    Also pre-registers ``infoflow`` in hermes's ``platform_registry`` so
    that ``Platform("infoflow")`` succeeds. In production this happens via
    ``ctx.register_platform`` during plugin discovery; tests reach inside
    the adapter without going through plugin discovery.
    """
    raw_key = os.urandom(16)
    aes_key = aes_key_b64url(raw_key)
    monkeypatch.setenv("INFOFLOW_API_HOST", "https://api.example.com")
    monkeypatch.setenv("INFOFLOW_APP_KEY", "k")
    monkeypatch.setenv("INFOFLOW_APP_SECRET", "s")
    monkeypatch.setenv("INFOFLOW_CHECK_TOKEN", "tok")
    monkeypatch.setenv("INFOFLOW_ENCODING_AES_KEY", aes_key)
    monkeypatch.setenv("INFOFLOW_ROBOT_NAME", "hermes")
    monkeypatch.delenv("INFOFLOW_PORT", raising=False)

    from gateway.platform_registry import PlatformEntry, platform_registry

    if not platform_registry.is_registered("infoflow"):
        platform_registry.register(
            PlatformEntry(
                name="infoflow",
                label="Infoflow (test)",
                adapter_factory=lambda cfg: InfoflowAdapter(cfg),
                check_fn=lambda: True,
            )
        )

    return raw_key, aes_key


def _make_config():
    """A minimal stand-in for the gateway's ``PlatformConfig`` dataclass."""
    return SimpleNamespace(extra={}, token=None, api_key=None, enabled=True, home_channel=None)


def _make_request(*, content_type: str, body: bytes, headers: dict[str, str] | None = None):
    """Tiny aiohttp-shaped request stand-in for ``_handle_webhook``."""
    hdrs = {"Content-Type": content_type}
    if headers:
        hdrs.update(headers)

    class _Req:
        def __init__(self):
            self.headers = hdrs
            self._body = body

        async def read(self):
            return self._body

    return _Req()


def test_adapter_parse_target_handles_group_and_dm() -> None:
    assert InfoflowAdapter._parse_target("group:42") == ("group", 42, "")
    assert InfoflowAdapter._parse_target("alice") == ("dm", None, "alice")
    assert InfoflowAdapter._parse_target("infoflow:bob") == ("dm", None, "bob")


def test_adapter_construction_reads_env(configured_env, monkeypatch) -> None:
    adapter = InfoflowAdapter(_make_config())
    assert adapter._api_account.app_key == "k"
    assert adapter._policy.reply_mode == "mention-and-watch"
    assert adapter._policy.require_mention is True


def test_handle_webhook_returns_echostr_synchronously(configured_env) -> None:
    raw_key, aes_key = configured_env
    adapter = InfoflowAdapter(_make_config())

    sig = _crypto.compute_echostr_signature(rn="r1", timestamp="100", check_token="tok")
    body = urlencode({"echostr": "HELLO", "signature": sig, "timestamp": "100", "rn": "r1"})

    request = _make_request(
        content_type="application/x-www-form-urlencoded",
        body=body.encode("utf-8"),
    )

    async def _go():
        return await adapter._handle_webhook(request)

    response = asyncio.run(_go())
    assert response.status == 200
    assert response.text == "HELLO"


def test_handle_webhook_returns_200_before_dispatch_completes(configured_env, monkeypatch) -> None:
    """Webhook MUST be fire-and-forget — handle_message can take seconds and
    we still return 200 immediately.
    """
    raw_key, aes_key = configured_env
    adapter = InfoflowAdapter(_make_config())

    # Build a valid inbound group message that the policy will dispatch.
    payload = {
        "message": {
            "header": {"fromuserid": "carol", "groupid": 123, "messageid": 1, "msgseqid": 2},
            "body": [
                {"type": "AT", "name": "hermes", "robotid": "42"},
                {"type": "TEXT", "content": "ping"},
            ],
        }
    }
    ct = aes_ecb_encrypt_b64url(json.dumps(payload), raw_key)
    request = _make_request(content_type="text/plain", body=ct.encode("utf-8"))

    handle_finished = asyncio.Event()
    handle_started = asyncio.Event()
    saw_event = {}

    async def slow_handle_message(event):
        handle_started.set()
        saw_event["event"] = event
        await asyncio.sleep(0.5)
        handle_finished.set()

    monkeypatch.setattr(adapter, "handle_message", slow_handle_message)

    async def _go():
        # Webhook returns immediately even though handle_message takes 500ms.
        response = await adapter._handle_webhook(request)
        # Confirm handle_message has been *started* (task scheduled) but not finished.
        await asyncio.wait_for(handle_started.wait(), timeout=1.0)
        assert not handle_finished.is_set(), "handle_message must not block the webhook"
        # Now wait it out and assert the event arrived.
        await asyncio.wait_for(handle_finished.wait(), timeout=2.0)
        return response

    response = asyncio.run(_go())
    assert response.status == 200
    event = saw_event["event"]
    assert "ping" in event.text
    # Metadata round-trips: mention info, image_urls, raw_msgdata.
    assert event.raw_message["was_mentioned"] is True


def test_outbound_send_records_dedup_id(configured_env, monkeypatch) -> None:
    adapter = InfoflowAdapter(_make_config())

    async def fake_send_private(account, to_user, contents, *, session=None):
        return {"ok": True, "msgkey": "MSG-1"}

    monkeypatch.setattr(_api, "send_private_message", fake_send_private)

    async def _go():
        return await adapter.send("alice", "hello")

    result = asyncio.run(_go())
    assert result.success is True
    assert result.message_id == "MSG-1"
    # Stored, inserted into replay dedup, and tracked as bot-sent for reply parsing.
    assert "MSG-1" in adapter._dedup_set
    assert "MSG-1" in adapter._sent_message_ids
    assert adapter._sent_store.find("alice", "MSG-1") is not None


def test_send_image_rejects_path_traversal(configured_env) -> None:
    adapter = InfoflowAdapter(_make_config())

    async def _go():
        return await adapter.send_image("alice", "file:///etc/passwd")

    result = asyncio.run(_go())
    assert result.success is False
    assert "media root" in (result.error or "")


def test_delete_message_by_count_uses_sent_store(configured_env, monkeypatch) -> None:
    adapter = InfoflowAdapter(_make_config())
    adapter._sent_store.record("alice", "MID-1")

    captured = {}

    async def fake_recall_private(account, *, msgkey, session=None):
        captured["msgkey"] = msgkey
        return {"ok": True}

    monkeypatch.setattr(_api, "recall_private_message", fake_recall_private)

    async def _go():
        return await adapter.delete_message("alice", count=1)

    result = asyncio.run(_go())
    assert result.success is True
    assert captured["msgkey"] == "MID-1"


def test_delete_message_with_no_recent_returns_error(configured_env, monkeypatch) -> None:
    adapter = InfoflowAdapter(_make_config())

    # Ensure no stale data leaks from previous tests (SQLite may persist).
    adapter._sent_store = SentMessageStore(dedup_set=set())

    async def _go():
        return await adapter.delete_message("alice")

    result = asyncio.run(_go())
    assert result.success is False
    assert "no recent" in (result.error or "")


def test_recall_tool_handler_takes_args_dict(configured_env, monkeypatch) -> None:
    """The recall handler must accept a single ``args`` dict + kwargs,
    matching tools/registry.py's calling convention (registry.dispatch
    calls ``entry.handler(args, **kwargs)``)."""
    from hermes_infoflow.tools import make_recall_handler

    handler = make_recall_handler()
    import inspect
    sig = inspect.signature(handler)
    # First param is named `args` and is positional.
    params = list(sig.parameters.values())
    assert params[0].name == "args"
    assert params[0].kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    )

    # When no runner / no adapter, returns a clean error dict rather than crashing.
    async def _go():
        return await handler({"target": "alice", "count": 1})

    result = asyncio.run(_go())
    assert isinstance(result, str)
    parsed = json.loads(result)
    # No live runner in tests, so we expect the cross-process error path.
    assert "error" in parsed


def test_send_image_private_sends_native_image(configured_env, monkeypatch, tmp_path) -> None:
    """Private-chat image sends must use msgtype=image, not drop the bytes."""
    adapter = InfoflowAdapter(_make_config())

    # Write a small fake image into an allowed media root.
    adapter._allowed_media_roots_for_test() if hasattr(adapter, "_allowed_media_roots_for_test") else None
    # Simplest: monkeypatch _load_image_bytes to return fixed bytes.
    async def fake_load(self, url):
        return b"\x89PNG\r\n\x1a\n" + b"x" * 100
    monkeypatch.setattr(InfoflowAdapter, "_load_image_bytes", fake_load)

    captured = {}
    async def fake_send_private(account, to_user, contents, *, session=None):
        captured.setdefault("calls", []).append(
            {"to_user": to_user, "types": [c.type for c in contents], "first_content": contents[0].content[:20]}
        )
        return {"ok": True, "msgkey": "MSG-IMG"}

    monkeypatch.setattr(_api, "send_private_message", fake_send_private)

    import asyncio
    async def _go():
        return await adapter.send_image("alice", "https://example.com/x.png", caption="see this")

    result = asyncio.run(_go())
    assert result.success is True
    assert result.message_id == "MSG-IMG"
    # Caption call + image call (in that order)
    calls = captured["calls"]
    # Two calls: one for the caption (text/markdown), one for the image
    assert len(calls) == 2
    assert "image" not in calls[0]["types"]
    assert calls[1]["types"] == ["image"]


def test_fetch_url_bytes_rejects_internal_ip(configured_env) -> None:
    """Outbound image fetch must refuse private / metadata hosts before issuing the request."""
    adapter = InfoflowAdapter(_make_config())
    import asyncio

    async def _go(url):
        try:
            await adapter._fetch_url_bytes(url)
            return "ok"
        except Exception as exc:
            return str(exc)

    # AWS metadata endpoint
    assert "refusing" in asyncio.run(_go("http://169.254.169.254/latest/meta-data/")).lower()
    # Loopback
    assert "refusing" in asyncio.run(_go("http://127.0.0.1:8642/")).lower()
    # Private RFC1918
    assert "refusing" in asyncio.run(_go("http://10.0.0.1/secret")).lower()
    # Non-http scheme
    assert "refusing" in asyncio.run(_go("ftp://example.com/x.png")).lower()


# ---------------------------------------------------------------------------
# Fix #2: send() must report failure when any chunk fails
# ---------------------------------------------------------------------------


def test_send_partial_failure_returns_error_with_last_messageid(
    configured_env, monkeypatch
) -> None:
    """If ANY chunk fails, ``send()`` must surface a failure (with last good id).

    Mirrors OpenClaw send.ts firstError semantics.
    """
    adapter = InfoflowAdapter(_make_config())
    # Force tiny chunk size so a normal message splits into >1 chunk.
    monkeypatch.setattr(adapter, "MAX_MESSAGE_LENGTH", 5)
    call_count = {"n": 0}

    async def fake_send_private(account, to_user, contents, *, session=None):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return {"ok": False, "error": "transient"}
        return {"ok": True, "msgkey": f"MID-{call_count['n']}"}

    monkeypatch.setattr(_api, "send_private_message", fake_send_private)

    async def _go():
        return await adapter.send("alice", "abcdefghijklmnop")

    result = asyncio.run(_go())
    assert result.success is False
    assert "transient" in (result.error or "")
    # The last successful messageid is still surfaced.
    assert result.message_id is not None


def test_send_records_bot_reply_for_follow_up(configured_env, monkeypatch) -> None:
    adapter = InfoflowAdapter(_make_config())

    async def fake_send_group(account, *, group_id, contents, reply_to=None, session=None):
        return {"ok": True, "messageid": "M1", "msgseqid": "S1"}

    monkeypatch.setattr(_api, "send_group_message", fake_send_group)

    async def _go():
        return await adapter.send("group:42", "hello")

    result = asyncio.run(_go())
    assert result.success is True
    # The policy's follow-up bookkeeping is updated.
    assert "42" in adapter._policy.last_reply_at


# ---------------------------------------------------------------------------
# Fix #1: fromid == robotId → ignore own bot message
# ---------------------------------------------------------------------------


def test_webhook_ignores_own_bot_message_by_fromid(configured_env, monkeypatch) -> None:
    """An inbound whose root-level ``fromid`` equals our discovered robotId must be dropped."""
    raw_key, _ = configured_env
    adapter = InfoflowAdapter(_make_config())
    adapter._robot_id = "8675309"  # simulate previously-discovered id

    payload = {
        "fromid": "8675309",
        "eventtype": "ALL_MESSAGE_FORWARD",
        "message": {
            "header": {"fromuserid": "ourbot", "groupid": 1, "messageid": 9},
            "body": [{"type": "TEXT", "content": "echo"}],
        },
    }
    ct = aes_ecb_encrypt_b64url(json.dumps(payload), raw_key)
    request = _make_request(content_type="text/plain", body=ct.encode("utf-8"))

    dispatched = {"called": False}

    async def trapping_handle_message(event):
        dispatched["called"] = True

    monkeypatch.setattr(adapter, "handle_message", trapping_handle_message)

    async def _go():
        return await adapter._handle_webhook(request)

    response = asyncio.run(_go())
    assert response.status == 200
    # No background dispatch was scheduled.
    assert dispatched["called"] is False


def test_webhook_persists_discovered_robot_id(configured_env, monkeypatch) -> None:
    raw_key, _ = configured_env
    adapter = InfoflowAdapter(_make_config())
    assert adapter._robot_id == ""

    payload = {
        "message": {
            "header": {"fromuserid": "bob", "groupid": 1, "messageid": 11},
            "body": [
                {"type": "AT", "name": "hermes", "robotid": "777"},
                {"type": "TEXT", "content": "hi"},
            ],
        }
    }
    ct = aes_ecb_encrypt_b64url(json.dumps(payload), raw_key)
    request = _make_request(content_type="text/plain", body=ct.encode("utf-8"))

    async def stub_handle_message(event):
        return None

    monkeypatch.setattr(adapter, "handle_message", stub_handle_message)

    async def _go():
        return await adapter._handle_webhook(request)

    asyncio.run(_go())
    assert adapter._robot_id == "777"


# ---------------------------------------------------------------------------
# Fix #4: delete_message LLM-confusion correction
# ---------------------------------------------------------------------------


def test_delete_message_corrects_inbound_id_via_reply_target(
    configured_env, monkeypatch
) -> None:
    """When LLM passes the inbound message_id and inbound is a quote-reply to a
    bot message, ``delete_message`` swaps in the bot message id automatically."""
    from hermes_infoflow.adapter import _InboundContext, _register_inbound_context

    adapter = InfoflowAdapter(_make_config())
    adapter._sent_store.record("alice", "BOT-MSG", msgseqid="")

    # Register inbound context: user quote-replied to BOT-MSG.
    _register_inbound_context(
        _InboundContext(
            account_id=adapter._sent_store.account_id,
            target="alice",
            inbound_message_id="INBOUND-7",
            reply_to_bot_message_id="BOT-MSG",
            reply_targets=[
                {"messageid": "BOT-MSG", "preview": "hi", "isBotMessage": True}
            ],
            inbound_body="please undo the previous",
            registered_at=__import__("time").time(),
        )
    )

    captured = {}

    async def fake_recall_private(account, *, msgkey, session=None):
        captured["msgkey"] = msgkey
        return {"ok": True}

    monkeypatch.setattr(_api, "recall_private_message", fake_recall_private)

    async def _go():
        return await adapter.delete_message(
            "alice",
            message_id="INBOUND-7",  # the LLM's mistake
        )

    result = asyncio.run(_go())
    assert result.success is True
    # Auto-correction swapped in the bot's real message id.
    assert captured["msgkey"] == "BOT-MSG"


def test_delete_message_drops_to_count_one_on_recall_latest_intent(
    configured_env, monkeypatch
) -> None:
    """When LLM passes inbound id and the user said '撤回上一条', drop to count=1."""
    from hermes_infoflow.adapter import _InboundContext, _register_inbound_context

    adapter = InfoflowAdapter(_make_config())
    adapter._sent_store.record("alice", "LATEST-BOT-MSG")

    _register_inbound_context(
        _InboundContext(
            account_id=adapter._sent_store.account_id,
            target="alice",
            inbound_message_id="INBOUND-X",
            reply_to_bot_message_id=None,
            reply_targets=[],
            inbound_body="撤回上一条",
            registered_at=__import__("time").time(),
        )
    )

    captured = {}

    async def fake_recall_private(account, *, msgkey, session=None):
        captured["msgkey"] = msgkey
        return {"ok": True}

    monkeypatch.setattr(_api, "recall_private_message", fake_recall_private)

    async def _go():
        return await adapter.delete_message(
            "alice",
            message_id="INBOUND-X",
        )

    result = asyncio.run(_go())
    assert result.success is True
    assert captured["msgkey"] == "LATEST-BOT-MSG"


def test_delete_message_surface_candidates_when_unknown(
    configured_env, monkeypatch
) -> None:
    """Group recall of an unknown messageid surfaces candidate hints (not just an opaque error)."""
    adapter = InfoflowAdapter(_make_config())
    adapter._sent_store.record("group:99", "REAL-MID", msgseqid="REAL-SEQ", digest="real msg")

    async def _go():
        return await adapter.delete_message(
            "group:99",
            message_id="UNKNOWN",
        )

    result = asyncio.run(_go())
    assert result.success is False
    assert "REAL-MID" in (result.error or "")


def test_delete_message_group_falls_back_via_current_inbound_reply(
    configured_env, monkeypatch
) -> None:
    """When message_id is wrong but current_inbound quotes a bot message, recall that bot id."""
    from hermes_infoflow.adapter import _InboundContext, _register_inbound_context

    adapter = InfoflowAdapter(_make_config())
    adapter._sent_store.record(
        "group:42", "BOT999", msgseqid="SEQ-99", digest="joke"
    )
    _register_inbound_context(
        _InboundContext(
            account_id=adapter._inbound_ctx_account_id(),
            target="group:42",
            inbound_message_id="USER123",
            reply_to_bot_message_id="BOT999",
            reply_targets=[
                {"messageid": "BOT999", "preview": "joke", "isBotMessage": True}
            ],
            inbound_body="撤回那条",
            registered_at=__import__("time").time(),
        )
    )

    captured: dict[str, str] = {}

    async def fake_recall_group(account, *, group_id, messageid, msgseqid, session=None):
        captured["messageid"] = messageid
        captured["msgseqid"] = msgseqid
        captured["group_id"] = str(group_id)
        return {"ok": True}

    monkeypatch.setattr(_api, "recall_group_message", fake_recall_group)

    async def _go():
        return await adapter.delete_message(
            "group:42",
            message_id="HALLUCINATED",
        )

    result = asyncio.run(_go())
    assert result.success is True
    assert captured["messageid"] == "BOT999"
    assert captured["msgseqid"] == "SEQ-99"
    assert captured["group_id"] == "42"


def test_delete_message_group_no_fallback_when_target_mismatches_inbound_ctx(
    configured_env, monkeypatch
) -> None:
    """Do not apply reply-to fallback from inbound ctx registered for another chat."""
    from hermes_infoflow.adapter import _InboundContext, _register_inbound_context

    adapter = InfoflowAdapter(_make_config())
    adapter._sent_store.record(
        "group:42", "BOT999", msgseqid="SEQ-99", digest="joke"
    )
    _register_inbound_context(
        _InboundContext(
            account_id=adapter._inbound_ctx_account_id(),
            target="group:OTHER",
            inbound_message_id="USER123",
            reply_to_bot_message_id="BOT999",
            reply_targets=[],
            inbound_body="x",
            registered_at=__import__("time").time(),
        )
    )
    called: list[str] = []

    async def fake_recall_group(account, **kwargs):
        called.append("yes")
        return {"ok": True}

    monkeypatch.setattr(_api, "recall_group_message", fake_recall_group)

    async def _go():
        return await adapter.delete_message(
            "group:42",
            message_id="HALLUCINATED",
        )

    result = asyncio.run(_go())
    assert result.success is False
    assert not called


def test_delete_message_group_falls_back_via_context_hint(
    configured_env, monkeypatch
) -> None:
    """ContextVar hint (webhook dispatch) supplies current inbound without tool arg."""
    from hermes_infoflow.adapter import (
        _InboundContext,
        _register_inbound_context,
        recall_inbound_message_id_hint_scope,
    )

    adapter = InfoflowAdapter(_make_config())
    adapter._sent_store.record(
        "group:42", "BOT999", msgseqid="SEQ-99", digest="joke"
    )
    _register_inbound_context(
        _InboundContext(
            account_id=adapter._inbound_ctx_account_id(),
            target="group:42",
            inbound_message_id="USER123",
            reply_to_bot_message_id="BOT999",
            reply_targets=[
                {"messageid": "BOT999", "preview": "joke", "isBotMessage": True}
            ],
            inbound_body="撤回",
            registered_at=__import__("time").time(),
        )
    )

    captured: dict[str, str] = {}

    async def fake_recall_group(account, *, group_id, messageid, msgseqid, session=None):
        captured["messageid"] = messageid
        captured["msgseqid"] = msgseqid
        return {"ok": True}

    monkeypatch.setattr(_api, "recall_group_message", fake_recall_group)

    async def _go():
        with recall_inbound_message_id_hint_scope("USER123"):
            return await adapter.delete_message(
                "group:42",
            message_id="HALLUCINATED",
        )

    result = asyncio.run(_go())
    assert result.success is True
    assert captured["messageid"] == "BOT999"
    assert captured["msgseqid"] == "SEQ-99"


# ---------------------------------------------------------------------------
# Fix #15: non-webhook connection mode is rejected at connect time
# ---------------------------------------------------------------------------


def test_connect_rejects_websocket_mode(configured_env, monkeypatch) -> None:
    monkeypatch.setenv("INFOFLOW_CONNECTION_MODE", "websocket")
    adapter = InfoflowAdapter(_make_config())

    async def _go():
        return await adapter.connect()

    result = asyncio.run(_go())
    assert result is False
    # Adapter must surface a clear fatal error (not just a warning).
    # The exact attribute name depends on hermes-agent's base class; we just
    # confirm connect() returned False without spinning up the server.
    assert adapter._site is None


# ---------------------------------------------------------------------------
# BUG HH regression — chat_id normalization across send / delete
# ---------------------------------------------------------------------------


def test_send_then_delete_with_infoflow_prefix(configured_env, monkeypatch) -> None:
    """Sending via ``infoflow:alice`` and recalling via plain ``alice`` must hit
    the same store entry. Without normalization the lookup misses.
    """
    adapter = InfoflowAdapter(_make_config())

    async def fake_send_private(account, to_user, contents, *, session=None):
        return {"ok": True, "msgkey": "MID-X"}

    captured = {}

    async def fake_recall_private(account, *, msgkey, session=None):
        captured["msgkey"] = msgkey
        return {"ok": True}

    monkeypatch.setattr(_api, "send_private_message", fake_send_private)
    monkeypatch.setattr(_api, "recall_private_message", fake_recall_private)

    async def _go():
        await adapter.send("infoflow:alice", "hello")
        # Now recall using the canonical form.
        return await adapter.delete_message("alice", count=1)

    result = asyncio.run(_go())
    assert result.success is True
    assert captured["msgkey"] == "MID-X"


def test_send_with_canonical_then_delete_with_prefix(configured_env, monkeypatch) -> None:
    """And the symmetric direction: send canonical, recall via prefixed form."""
    adapter = InfoflowAdapter(_make_config())

    async def fake_send_private(account, to_user, contents, *, session=None):
        return {"ok": True, "msgkey": "MID-Y"}

    captured = {}

    async def fake_recall_private(account, *, msgkey, session=None):
        captured["msgkey"] = msgkey
        return {"ok": True}

    monkeypatch.setattr(_api, "send_private_message", fake_send_private)
    monkeypatch.setattr(_api, "recall_private_message", fake_recall_private)

    async def _go():
        await adapter.send("alice", "hello")
        return await adapter.delete_message("infoflow:alice", count=1)

    result = asyncio.run(_go())
    assert result.success is True
    assert captured["msgkey"] == "MID-Y"


# ---------------------------------------------------------------------------
# BUG EE regression — implicit recall correction without explicit hint
# ---------------------------------------------------------------------------


def test_delete_message_corrects_without_explicit_current_inbound_id(
    configured_env, monkeypatch
) -> None:
    """LLM rarely supplies ``current_inbound_message_id``. The correction must
    fire as long as ``message_id`` itself is a known inbound context id.
    """
    from hermes_infoflow.adapter import _InboundContext, _register_inbound_context

    adapter = InfoflowAdapter(_make_config())
    adapter._sent_store.record("alice", "BOT-MSG-2", msgseqid="")

    _register_inbound_context(
        _InboundContext(
            account_id=adapter._sent_store.account_id,
            target="alice",
            inbound_message_id="INBOUND-99",
            reply_to_bot_message_id="BOT-MSG-2",
            reply_targets=[
                {"messageid": "BOT-MSG-2", "preview": "hi", "isBotMessage": True}
            ],
            inbound_body="please undo",
            registered_at=__import__("time").time(),
        )
    )

    captured = {}

    async def fake_recall_private(account, *, msgkey, session=None):
        captured["msgkey"] = msgkey
        return {"ok": True}

    monkeypatch.setattr(_api, "recall_private_message", fake_recall_private)

    async def _go():
        # LLM passes the inbound id but NOT current_inbound_message_id.
        return await adapter.delete_message("alice", message_id="INBOUND-99")

    result = asyncio.run(_go())
    assert result.success is True
    assert captured["msgkey"] == "BOT-MSG-2"
