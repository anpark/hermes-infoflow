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
from hermes_infoflow.parser import AccountConfig  # noqa: E402
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
    # Stored AND inserted into the shared dedup set.
    assert "MSG-1" in adapter._dedup_set
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


def test_delete_message_with_no_recent_returns_error(configured_env) -> None:
    adapter = InfoflowAdapter(_make_config())

    async def _go():
        return await adapter.delete_message("alice")

    result = asyncio.run(_go())
    assert result.success is False
    assert "no recent" in (result.error or "")


def test_recall_tool_handler_takes_args_dict(configured_env, monkeypatch) -> None:
    """The recall handler must accept a single ``args`` dict + kwargs,
    matching tools/registry.py's calling convention (registry.dispatch
    calls ``entry.handler(args, **kwargs)``)."""
    from hermes_infoflow.adapter import _make_recall_handler

    handler = _make_recall_handler()
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

    import asyncio
    result = asyncio.run(_go())
    assert isinstance(result, dict)
    # No live runner in tests, so we expect the cross-process error path.
    assert "error" in result


def test_send_image_private_sends_native_image(configured_env, monkeypatch, tmp_path) -> None:
    """Private-chat image sends must use msgtype=image, not drop the bytes."""
    adapter = InfoflowAdapter(_make_config())

    # Write a small fake image into an allowed media root.
    media_root = adapter._allowed_media_roots_for_test() if hasattr(adapter, "_allowed_media_roots_for_test") else None
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
