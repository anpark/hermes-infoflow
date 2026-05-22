"""Tests for the Infoflow session dashboard (SessionTracker + HTTP routes)."""

from __future__ import annotations

import asyncio

import pytest

from hermes_infoflow.dashboard import (
    SessionTracker,
    dashboard_enabled,
    get_tracker,
    make_plugin_hooks,
    register_routes,
)


@pytest.fixture
def tracker() -> SessionTracker:
    return SessionTracker(buffer_size=100)


def test_push_event_and_snapshot(tracker: SessionTracker) -> None:
    ev = tracker.push_event("sess-1", "session.start", {"model": "test"}, platform="infoflow")
    assert ev is not None
    assert ev.seq == 1
    assert ev.kind == "session.start"

    snap = tracker.snapshot("sess-1", cursor=0)
    assert len(snap) == 1
    assert tracker.snapshot("sess-1", cursor=1) == []


def test_bind_chat_merges_pending(tracker: SessionTracker) -> None:
    tracker.push_event("", "inbound.infoflow", {"x": 1}, platform="infoflow", chat_id="alice")
    tracker.bind_chat("alice", "real-session")
    meta = tracker.get_meta("real-session")
    assert meta is not None
    assert meta.chat_id == "alice"
    assert len(tracker.snapshot("real-session")) >= 1


def test_list_sessions_scope(tracker: SessionTracker) -> None:
    tracker.push_event("a", "session.start", {}, platform="infoflow")
    tracker.push_event("b", "session.start", {}, platform="telegram")
    infoflow_only = tracker.list_sessions(scope="infoflow")
    assert len(infoflow_only) == 1
    assert infoflow_only[0]["session_id"] == "a"
    assert len(tracker.list_sessions(scope="all")) == 2


@pytest.mark.asyncio
async def test_subscribe_receives_events(tracker: SessionTracker) -> None:
    q = tracker.subscribe("sub-1")
    tracker.push_event("sub-1", "tool.start", {"tool_name": "terminal"})
    ev = await asyncio.wait_for(q.get(), timeout=1.0)
    assert ev is not None
    assert ev.kind == "tool.start"
    tracker.unsubscribe("sub-1", q)


def test_plugin_hooks_do_not_raise(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["on_session_start"](session_id="h1", model="m", platform="infoflow")
    hooks["pre_llm_call"](
        session_id="h1",
        user_message="hi",
        conversation_history=[],
        is_first_turn=True,
        model="m",
        platform="infoflow",
        sender_id="u1",
    )
    hooks["post_tool_call"](
        tool_name="terminal",
        args={"command": "ls"},
        result="ok",
        task_id="t",
        session_id="h1",
        tool_call_id="tc1",
        duration_ms=10,
    )
    assert len(tracker.snapshot("h1")) >= 3


def test_pre_gateway_dispatch_with_mock_gateway(tracker: SessionTracker) -> None:
    from types import SimpleNamespace

    class _Platform:
        value = "infoflow"

    source = SimpleNamespace(
        platform=_Platform(),
        chat_id="alice",
        chat_type="dm",
        user_id="alice",
        user_name="Alice",
    )
    event = SimpleNamespace(source=source, text="hello")

    entry = SimpleNamespace(session_id="gw-sess-1", session_key="agent:main:infoflow:dm:alice")

    def _session_key_for_source(src: object) -> str:
        return "agent:main:infoflow:dm:alice"

    def _ensure_loaded() -> None:
        return None

    session_store = SimpleNamespace(
        _entries={"agent:main:infoflow:dm:alice": entry},
    )
    session_store._ensure_loaded = _ensure_loaded  # type: ignore[method-assign]
    gateway = SimpleNamespace(_session_key_for_source=_session_key_for_source)

    hooks = make_plugin_hooks(tracker)
    hooks["pre_gateway_dispatch"](
        event=event,
        gateway=gateway,
        session_store=session_store,
    )
    assert tracker.get_meta("gw-sess-1") is not None
    assert tracker.lookup_session_id("alice") == "gw-sess-1"
    kinds = [e.kind for e in tracker.snapshot("gw-sess-1")]
    assert "inbound" in kinds


def test_pre_gateway_dispatch_peek_without_create(tracker: SessionTracker) -> None:
    from types import SimpleNamespace

    class _Platform:
        value = "infoflow"

    source = SimpleNamespace(
        platform=_Platform(),
        chat_id="bob",
        chat_type="dm",
        user_id="bob",
    )
    event = SimpleNamespace(source=source, text="hi")
    created = {"called": False}

    def get_or_create_session(src: object) -> object:
        created["called"] = True
        return SimpleNamespace(session_id="should-not", session_key="")

    def _session_key_for_source(src: object) -> str:
        return "agent:main:infoflow:dm:bob"

    session_store = SimpleNamespace(_entries={}, get_or_create_session=get_or_create_session)
    session_store._ensure_loaded = lambda: None  # type: ignore[method-assign]
    gateway = SimpleNamespace(_session_key_for_source=_session_key_for_source)

    hooks = make_plugin_hooks(tracker)
    hooks["pre_gateway_dispatch"](event=event, gateway=gateway, session_store=session_store)

    assert created["called"] is False
    assert tracker.lookup_session_id("bob") == "pending:bob"
    assert len(tracker.snapshot("pending:bob")) == 1


def test_pre_llm_call_binds_from_meta_chat_id(tracker: SessionTracker) -> None:
    tracker.push_event("", "inbound", {"x": 1}, platform="infoflow", chat_id="carol")
    tracker.bind_chat("carol", "sess-carol")
    hooks = make_plugin_hooks(tracker)
    hooks["pre_llm_call"](
        session_id="sess-carol",
        user_message="hi",
        conversation_history=[],
        is_first_turn=True,
        model="m",
        platform="infoflow",
        sender_id="carol",
    )
    assert tracker.lookup_session_id("carol") == "sess-carol"


def test_lookup_prefers_session_with_terminal_lines(tracker: SessionTracker) -> None:
    tracker.push_event(
        "empty-ended",
        "session.end",
        {},
        platform="infoflow",
        chat_id="bob",
    )
    meta_empty = tracker.get_meta("empty-ended")
    assert meta_empty is not None
    meta_empty.status = "ended"
    meta_empty.last_event_at = 1000.0

    tracker.push_event(
        "rich-ended",
        "display.hermes",
        {"text": "visible"},
        platform="infoflow",
        chat_id="bob",
    )
    meta_rich = tracker.get_meta("rich-ended")
    assert meta_rich is not None
    meta_rich.status = "ended"
    meta_rich.last_event_at = 500.0

    assert tracker.lookup_session_id("bob") == "rich-ended"


def test_lookup_session_id_prefers_active_over_stale_map(tracker: SessionTracker) -> None:
    tracker.bind_chat("group:9", "old-ended")
    tracker.push_event(
        "old-ended",
        "display.tool_line",
        {"line": "old"},
        platform="infoflow",
        chat_id="group:9",
    )
    meta_old = tracker.get_meta("old-ended")
    assert meta_old is not None
    meta_old.status = "ended"

    tracker.push_event(
        "new-active",
        "display.tool_line",
        {"line": "┊ ok"},
        platform="infoflow",
        chat_id="group:9",
    )
    meta_new = tracker.get_meta("new-active")
    assert meta_new is not None
    meta_new.status = "active"

    assert tracker.lookup_session_id("group:9") == "new-active"


@pytest.mark.asyncio
async def test_dashboard_routes_localhost_only() -> None:
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    tr = SessionTracker(buffer_size=50)
    app = web.Application()
    register_routes(app, tr, base_path="/webhook/infoflow")

    async with TestClient(TestServer(app)) as client:
        # TestClient uses 127.0.0.1 by default
        resp = await client.get("/webhook/infoflow/dashboard")
        assert resp.status == 200
        assert "Hermes Sessions" in await resp.text()

        resp = await client.get("/webhook/infoflow/dashboard/api/sessions")
        assert resp.status == 200
        assert isinstance(await resp.json(), list)

        tr.push_event("route-sid", "session.start", {}, platform="infoflow")
        resp = await client.get("/webhook/infoflow/dashboard/api/sessions/route-sid")
        assert resp.status == 200
        body = await resp.json()
        assert body["meta"]["session_id"] == "route-sid"


def test_dashboard_enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFOFLOW_DASHBOARD_ENABLED", "false")
    assert dashboard_enabled() is False
    monkeypatch.setenv("INFOFLOW_DASHBOARD_ENABLED", "true")
    assert dashboard_enabled() is True


def test_get_tracker_singleton() -> None:
    t1 = get_tracker()
    t2 = get_tracker()
    assert t1 is t2


@pytest.mark.asyncio
async def test_localhost_guard_rejects_remote() -> None:
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from hermes_infoflow.dashboard import _require_localhost

    hit = {"ok": False}

    @_require_localhost
    async def handler(request: web.Request) -> web.Response:
        hit["ok"] = True
        return web.Response(text="ok")

    req = make_mocked_request("GET", "/webhook/infoflow/dashboard", headers={})
    req._remote = "203.0.113.1"  # noqa: SLF001

    resp = await handler(req)
    assert resp.status == 403
    assert hit["ok"] is False
