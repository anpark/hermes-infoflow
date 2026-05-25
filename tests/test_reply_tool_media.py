from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

from hermes_infoflow import tools as tools_mod
from hermes_infoflow.tools import make_reply_handler
from hermes_infoflow.utils import _resolve_safe_local_path

_TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1Pe"
    "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_infoflow_reply_media_sends_native_image_not_path_text(monkeypatch, tmp_path) -> None:
    image_path = tmp_path / "sky_blue.png"
    image_path.write_bytes(_TINY_PNG_BYTES)
    calls: list[tuple] = []

    class FakeAdapter:
        _http_session = object()

        async def _load_image_bytes(self, image_url: str) -> bytes:
            calls.append(("preflight", image_url))
            return _TINY_PNG_BYTES

        async def send(self, **kwargs):
            raise AssertionError(f"must not send MEDIA as text: {kwargs!r}")

        async def send_image_file(self, **kwargs):
            calls.append(("image", kwargs))
            return SimpleNamespace(
                success=True,
                message_id="IMG",
                continuation_message_ids=("CAPTION",),
            )

    async def passthrough(_adapter, coro):
        return await coro

    adapter = FakeAdapter()
    monkeypatch.setattr(tools_mod, "_get_live_adapter", lambda: adapter)
    monkeypatch.setattr(tools_mod, "_with_temp_session", passthrough)

    handler = make_reply_handler()
    raw = asyncio.run(handler({
        "target": "infoflow:group:4507088",
        "message": f"天空蓝\nMEDIA:{image_path}",
        "reply_to": "MID",
        "reply_type": "2",
    }))

    result = json.loads(raw)
    assert result["success"] is True
    assert result["message_id"] == "IMG"
    assert result["media_count"] == 1
    assert calls[0] == ("preflight", str(image_path))
    assert calls[1][0] == "image"
    image_kwargs = calls[1][1]
    assert image_kwargs["chat_id"] == "group:4507088"
    assert image_kwargs["image_path"] == str(image_path)
    assert image_kwargs["caption"] == "天空蓝"
    assert image_kwargs["reply_to"] == "MID"
    assert image_kwargs["metadata"] == {"reply_type": "2"}


def test_infoflow_reply_without_media_keeps_text_send(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeAdapter:
        _http_session = object()

        async def send(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(success=True, message_id="TXT")

        async def send_image_file(self, **kwargs):
            raise AssertionError(f"must not send text reply as image: {kwargs!r}")

    async def passthrough(_adapter, coro):
        return await coro

    monkeypatch.setattr(tools_mod, "_get_live_adapter", lambda: FakeAdapter())
    monkeypatch.setattr(tools_mod, "_with_temp_session", passthrough)

    raw = asyncio.run(make_reply_handler()({
        "target": "infoflow:chengbo05",
        "message": "普通引用回复",
        "reply_to": "MID",
        "reply_type": "2",
    }))

    result = json.loads(raw)
    assert result["success"] is True
    assert result["message_id"] == "TXT"
    assert calls == [{
        "chat_id": "chengbo05",
        "content": "普通引用回复",
        "reply_to": "MID",
        "metadata": {"reply_type": "2"},
    }]


def test_infoflow_reply_media_rejects_malformed_directive_without_path_leak(monkeypatch) -> None:
    class FakeAdapter:
        _http_session = object()

        async def _load_image_bytes(self, image_url: str) -> bytes:
            raise RuntimeError(f"refusing to read {image_url}")

        async def send(self, **kwargs):
            raise AssertionError(f"must not send malformed MEDIA as text: {kwargs!r}")

        async def send_image_file(self, **kwargs):
            raise AssertionError(f"must not send malformed MEDIA as image: {kwargs!r}")

    async def passthrough(_adapter, coro):
        return await coro

    monkeypatch.setattr(tools_mod, "_get_live_adapter", lambda: FakeAdapter())
    monkeypatch.setattr(tools_mod, "_with_temp_session", passthrough)

    raw = asyncio.run(make_reply_handler()({
        "target": "infoflow:chengbo05",
        "message": "MEDIA:/etc/passwd",
        "reply_to": "MID",
    }))

    result = json.loads(raw)
    assert "error" in result
    assert "/etc/passwd" not in result["error"]
    assert (
        "[local image path]" in result["error"]
        or "not sending local path text" in result["error"]
    )


def test_infoflow_reply_media_sanitizes_image_errors(monkeypatch, tmp_path) -> None:
    image_path = tmp_path / "bad.png"
    image_path.write_bytes(b"not an image")

    class FakeAdapter:
        _http_session = object()

        async def _load_image_bytes(self, image_url: str) -> bytes:
            raise RuntimeError(f"failed to read {image_url}")

        async def send(self, **kwargs):
            raise AssertionError(f"must not fallback to text: {kwargs!r}")

        async def send_image_file(self, **kwargs):
            raise AssertionError(f"must not send after failed preflight: {kwargs!r}")

    async def passthrough(_adapter, coro):
        return await coro

    monkeypatch.setattr(tools_mod, "_get_live_adapter", lambda: FakeAdapter())
    monkeypatch.setattr(tools_mod, "_with_temp_session", passthrough)

    raw = asyncio.run(make_reply_handler()({
        "target": "infoflow:chengbo05",
        "message": f"MEDIA:{image_path}",
        "reply_to": "MID",
    }))

    result = json.loads(raw)
    assert "error" in result
    assert str(image_path) not in result["error"]
    assert "[local image path]" in result["error"]


def test_image_cache_paths_are_allowed_media_roots(monkeypatch, tmp_path) -> None:
    hermes_home = tmp_path / ".hermes"
    image_path = hermes_home / "image_cache" / "x.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(_TINY_PNG_BYTES)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert _resolve_safe_local_path(str(image_path)) == image_path.resolve()
