"""Tests for Session Tracker (resolve, terminal formatting, HTTP routes)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hermes_infoflow.api import InfoflowAccountAPI, InfoflowAPIError
from hermes_infoflow.dashboard import (
    SessionEvent,
    SessionTracker,
    make_plugin_hooks,
    normalize_chat_id,
    sessiontracker_enabled,
)
from hermes_infoflow.sessiontracker import (
    TERMINAL_EVENT_KINDS,
    _code_user_cache,
    canonical_for_stream_access,
    event_to_terminal_dict,
    format_terminal_line,
    register_sessiontracker_routes,
    resolve_target,
    session_matches_target,
)


@pytest.fixture
def tracker() -> SessionTracker:
    return SessionTracker(buffer_size=100)


@pytest.fixture
def account() -> InfoflowAccountAPI:
    return InfoflowAccountAPI(
        api_host="https://api.example.com",
        app_key="k",
        app_secret="s",
        app_agent_id=123,
    )


@pytest.fixture(autouse=True)
def _clear_code_user_cache() -> None:
    _code_user_cache.clear()
    yield
    _code_user_cache.clear()


def test_normalize_chat_id() -> None:
    assert normalize_chat_id("infoflow:group:99") == "group:99"
    assert normalize_chat_id("group:99") == "group:99"
    assert normalize_chat_id("alice") == "alice"


def test_format_terminal_line_llm_response_is_metadata() -> None:
    """llm.response is dashboard metadata, not a terminal line (avoids duplicate)."""
    ev = SessionEvent(1, 0.0, "llm.response", {"assistant_response": "Hello back"})
    assert format_terminal_line(ev) is None


def test_format_terminal_line_display_kinds() -> None:
    tool_ev = SessionEvent(1, 0.0, "display.tool_line", {"line": "┊ 💻 $ ls"})
    assert format_terminal_line(tool_ev) == {"line_kind": "tool", "text": "┊ 💻 $ ls"}

    hermes_ev = SessionEvent(2, 0.0, "display.hermes", {"text": "Hello"})
    assert format_terminal_line(hermes_ev) == {
        "line_kind": "hermes",
        "text": "Hello",
        "final": True,
    }

    status_ev = SessionEvent(3, 0.0, "display.status", {"line": "⚕ gpt-4"})
    assert format_terminal_line(status_ev) == {"line_kind": "status", "text": "⚕ gpt-4"}

    user_ev = SessionEvent(
        4, 0.0, "display.user",
        {"text": "ping", "full_text": "full ping"},
    )
    assert format_terminal_line(user_ev) == {"line_kind": "user", "text": "ping"}
    assert format_terminal_line(user_ev, show_full_user_message=True) == {
        "line_kind": "user",
        "text": "full ping",
    }

    stream_ev = SessionEvent(
        5, 0.0, "display.hermes_stream",
        {"text": "Hel", "stream_id": "s1", "final": False},
    )
    assert format_terminal_line(stream_ev) == {
        "line_kind": "hermes",
        "text": "Hel",
        "stream_id": "s1",
        "final": False,
    }

    interim_ev = SessionEvent(6, 0.0, "display.interim", {"text": "thinking…"})
    assert format_terminal_line(interim_ev) == {
        "line_kind": "interim",
        "text": "thinking…",
    }

    progress_ev = SessionEvent(
        7, 0.0, "display.tool_progress",
        {"line": "┊ ⚡ search", "tool_call_id": "c1", "stage": "start"},
    )
    assert format_terminal_line(progress_ev) == {
        "line_kind": "tool_progress",
        "text": "┊ ⚡ search",
        "tool_call_id": "c1",
        "stage": "start",
    }


def test_format_terminal_line_outbound_progress() -> None:
    ev = SessionEvent(
        4, 0.0, "outbound.infoflow",
        {"is_progress_hint": True, "preview": "┊ 💻 running…"},
    )
    block = format_terminal_line(ev)
    assert block is not None
    assert block["line_kind"] == "tool"


def test_format_terminal_line_suppressed_group_status() -> None:
    ev = SessionEvent(
        4, 0.0, "outbound.infoflow",
        {
            "suppressed_group_status": True,
            "preview": "📦 Preflight compression: ~109,133 tokens >= 102,400 threshold.",
        },
    )
    block = format_terminal_line(ev)
    assert block is not None
    assert block["line_kind"] == "status"
    assert block["text"].startswith("📦 Preflight compression:")


@pytest.mark.asyncio
async def test_resolve_target_group(tracker: SessionTracker, account: InfoflowAccountAPI) -> None:
    tracker.bind_chat("group:4507088", "sess-g1")
    info = await resolve_target(
        tracker, chat_type=2, chat_id="4507088", code="", account=account,
    )
    assert info["canonical_chat_id"] == "group:4507088"
    assert info["session_id"] == "sess-g1"
    assert "群" in info["label"]


@pytest.mark.asyncio
async def test_resolve_target_dm_mock_getuserinfo(
    tracker: SessionTracker, account: InfoflowAccountAPI,
) -> None:
    with patch(
        "hermes_infoflow.sessiontracker.get_user_info_by_code",
        new_callable=AsyncMock,
        return_value="chengbo05",
    ) as mock_gu:
        info = await resolve_target(
            tracker,
            chat_type=7,
            chat_id="3950087625",
            code="abc123",
            account=account,
        )
    mock_gu.assert_awaited_once()
    assert info["canonical_chat_id"] == "chengbo05"
    assert info["session_id"] == ""


@pytest.mark.asyncio
async def test_resolve_target_dm_reuses_cached_code(
    tracker: SessionTracker, account: InfoflowAccountAPI,
) -> None:
    with patch(
        "hermes_infoflow.sessiontracker.get_user_info_by_code",
        new_callable=AsyncMock,
        return_value="chengbo05",
    ) as mock_gu:
        info1 = await resolve_target(
            tracker,
            chat_type=7,
            chat_id="3950087625",
            code="abc123",
            account=account,
        )
        info2 = await resolve_target(
            tracker,
            chat_type=7,
            chat_id="3950087625",
            code="abc123",
            account=account,
        )
    mock_gu.assert_awaited_once()
    assert info1["canonical_chat_id"] == "chengbo05"
    assert info2["canonical_chat_id"] == "chengbo05"


@pytest.mark.asyncio
async def test_resolve_target_dm_different_code_calls_api_again(
    tracker: SessionTracker, account: InfoflowAccountAPI,
) -> None:
    with patch(
        "hermes_infoflow.sessiontracker.get_user_info_by_code",
        new_callable=AsyncMock,
        return_value="chengbo05",
    ) as mock_gu:
        await resolve_target(
            tracker,
            chat_type=7,
            chat_id="3950087625",
            code="abc123",
            account=account,
        )
        await resolve_target(
            tracker,
            chat_type=7,
            chat_id="3950087625",
            code="def456",
            account=account,
        )
    assert mock_gu.await_count == 2


@pytest.mark.asyncio
async def test_resolve_target_dm_does_not_cache_failed_code(
    tracker: SessionTracker, account: InfoflowAccountAPI,
) -> None:
    with patch(
        "hermes_infoflow.sessiontracker.get_user_info_by_code",
        new_callable=AsyncMock,
        side_effect=[
            InfoflowAPIError("oauth_code超时或者失效"),
            "chengbo05",
        ],
    ) as patched:
        with pytest.raises(InfoflowAPIError):
            await resolve_target(
                tracker,
                chat_type=7,
                chat_id="3950087625",
                code="same-code",
                account=account,
            )
        info = await resolve_target(
            tracker,
            chat_type=7,
            chat_id="3950087625",
            code="same-code",
            account=account,
        )
    assert patched.await_count == 2
    assert info["canonical_chat_id"] == "chengbo05"


def test_bind_latest_pending_on_session_start(tracker: SessionTracker) -> None:
    tracker.push_event("", "inbound.infoflow", {"x": 1}, platform="infoflow", chat_id="bob")
    hooks = make_plugin_hooks(tracker)
    hooks["on_session_start"](session_id="real-1", model="m", platform="infoflow")
    assert tracker.lookup_session_id("bob") == "real-1"
    assert len(tracker.snapshot("real-1")) >= 2


def test_post_tool_call_single_terminal_line(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["post_tool_call"](
        session_id="t1",
        tool_name="read_file",
        args={"path": "/tmp/x"},
        result="ok",
        duration_ms=1200,
        tool_call_id="tc",
    )
    terminal_lines = [
        event_to_terminal_dict(e)
        for e in tracker.snapshot("t1")
        if e.kind in TERMINAL_EVENT_KINDS
    ]
    terminal_lines = [x for x in terminal_lines if x is not None]
    assert len(terminal_lines) == 1
    assert terminal_lines[0]["line_kind"] == "tool"


def test_bind_latest_skips_multiple_pending(tracker: SessionTracker) -> None:
    tracker.push_event("", "inbound", {"x": 1}, platform="infoflow", chat_id="alice")
    tracker.push_event("", "inbound", {"x": 2}, platform="infoflow", chat_id="bob")
    hooks = make_plugin_hooks(tracker)
    hooks["on_session_start"](session_id="real-1", model="m", platform="infoflow")
    assert tracker.lookup_session_id("alice") == "pending:alice"
    assert tracker.lookup_session_id("bob") == "pending:bob"


@pytest.mark.asyncio
async def test_resolve_pending_status_waiting(tracker: SessionTracker) -> None:
    tracker.push_event("", "inbound", {"x": 1}, platform="infoflow", chat_id="group:99")
    info = await resolve_target(
        tracker, chat_type=2, chat_id="99", code="", account=None,
    )
    assert info["session_id"] == "pending:group:99"
    assert info["status"] == "waiting"


def test_push_event_updates_chat_map_from_meta(tracker: SessionTracker) -> None:
    tracker.push_event(
        "sess-x",
        "display.tool_line",
        {"line": "x"},
        platform="infoflow",
        chat_id="group:42",
    )
    assert tracker.lookup_session_id("group:42") == "sess-x"


@pytest.mark.asyncio
async def test_stream_access_uses_session_meta_when_code_expired(
    tracker: SessionTracker,
) -> None:
    tracker.push_event(
        "sess-dm",
        "display.hermes",
        {"text": "hi"},
        platform="infoflow",
        chat_id="chengbo05",
    )
    meta = tracker.get_meta("sess-dm")
    assert meta is not None
    meta.user_id = "chengbo05"
    meta.chat_id = "chengbo05"

    with patch(
        "hermes_infoflow.sessiontracker.get_user_info_by_code",
        new_callable=AsyncMock,
        side_effect=InfoflowAPIError("oauth_code超时或者失效"),
    ):
        canonical = await canonical_for_stream_access(
            tracker,
            session_id="sess-dm",
            chat_type=7,
            chat_id="3950087625",
            code="expired-code",
            account=None,
        )
    assert canonical == "chengbo05"
    assert session_matches_target(tracker, "sess-dm", "chengbo05")


def test_session_matches_target(tracker: SessionTracker) -> None:
    tracker.bind_chat("group:9", "sess-9")
    assert session_matches_target(tracker, "sess-9", "group:9")
    assert session_matches_target(tracker, "pending:group:9", "group:9")
    assert not session_matches_target(tracker, "sess-9", "group:8")


def test_post_llm_call_emits_display_hermes(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["post_llm_call"](
        session_id="t2",
        assistant_response="Done.",
        model="test-model",
        platform="infoflow",
    )
    kinds = [e.kind for e in tracker.snapshot("t2")]
    assert "display.hermes" in kinds
    block = event_to_terminal_dict(tracker.snapshot("t2")[-2])
    assert block is not None
    assert block["line_kind"] == "hermes"


@pytest.mark.asyncio
async def test_sessiontracker_routes_resolve_and_stream() -> None:
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    tr = SessionTracker(buffer_size=50)
    tr.bind_chat("group:1", "st-sess")
    tr.push_event("st-sess", "display.tool_line", {"line": "┊ test"})
    app = web.Application()
    register_sessiontracker_routes(app, tr, base_path="/webhook/infoflow")

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/webhook/infoflow/sessiontracker?chatType=2&chatId=1",
        )
        assert resp.status == 200
        assert "Session Tracker" in await resp.text()

        resp = await client.get(
            "/webhook/infoflow/sessiontracker/api/resolve?chatType=2&chatId=1",
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["canonical_chat_id"] == "group:1"
        assert body["session_id"] == "st-sess"

        resp = await client.get(
            "/webhook/infoflow/sessiontracker/api/resolve?chatType=7&chatId=1",
        )
        assert resp.status == 400

        resp = await client.get(
            "/webhook/infoflow/sessiontracker/api/history"
            "?session_id=st-sess&chatType=2&chatId=1",
        )
        assert resp.status == 200
        body = await resp.json()
        assert len(body.get("lines", [])) >= 1

        resp = await client.get(
            "/webhook/infoflow/sessiontracker/api/stream"
            "?session_id=st-sess&chatType=2&chatId=1",
        )
        assert resp.status == 200
        assert resp.content_type == "text/event-stream"

        resp = await client.get(
            "/webhook/infoflow/sessiontracker/api/stream"
            "?session_id=st-sess&chatType=2&chatId=999",
        )
        assert resp.status == 403


async def test_sessiontracker_history_full_user_message_requires_admin_viewer_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setenv("INFOFLOW_SESSIONTRACKER_FULL_USER_MESSAGE", "true")
    monkeypatch.setenv("INFOFLOW_ADMIN_USER", "admin")
    monkeypatch.setenv("INFOFLOW_APP_KEY", "app-key")
    monkeypatch.setenv("INFOFLOW_APP_SECRET", "app-secret")
    monkeypatch.setenv("INFOFLOW_APP_AGENT_ID", "123")
    _code_user_cache.clear()

    async def _fake_get_user_info_by_code(
        account: InfoflowAccountAPI,
        code: str,
        *,
        session=None,
    ) -> str:
        del account, session
        return "admin" if code == "admin-code" else "alice"

    monkeypatch.setattr(
        "hermes_infoflow.sessiontracker.get_user_info_by_code",
        _fake_get_user_info_by_code,
    )

    tr = SessionTracker(buffer_size=50)
    tr.bind_chat("group:1", "st-sess")
    tr.push_event(
        "st-sess",
        "display.user",
        {
            "text": "safe message",
            "full_text": "full injected message\n[Message]\nsafe message",
            "chat_id": "group:1",
        },
        platform="infoflow",
        chat_id="group:1",
    )
    app = web.Application()
    register_sessiontracker_routes(app, tr, base_path="/webhook/infoflow")

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/webhook/infoflow/sessiontracker/api/history"
            "?session_id=st-sess&chatType=2&chatId=1",
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["lines"][0]["text"] == "safe message"

        resp = await client.get(
            "/webhook/infoflow/sessiontracker/api/history"
            "?session_id=st-sess&chatType=2&chatId=1&code=user-code",
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["lines"][0]["text"] == "safe message"

        resp = await client.get(
            "/webhook/infoflow/sessiontracker/api/history"
            "?session_id=st-sess&chatType=2&chatId=1&code=admin-code",
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["lines"][0]["text"] == "full injected message\n[Message]\nsafe message"


def test_sessiontracker_enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFOFLOW_SESSIONTRACKER_ENABLED", "false")
    assert sessiontracker_enabled() is False
    monkeypatch.setenv("INFOFLOW_SESSIONTRACKER_ENABLED", "true")
    assert sessiontracker_enabled() is True
