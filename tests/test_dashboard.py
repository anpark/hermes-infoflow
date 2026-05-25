"""Tests for the Infoflow session dashboard (SessionTracker + HTTP routes)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import types

import pytest

from hermes_infoflow.dashboard import (
    MAX_TEXT_PREVIEW,
    SessionTracker,
    dashboard_enabled,
    get_tracker,
    make_plugin_hooks,
    register_routes,
    sessiontracker_full_user_message_enabled,
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
    kinds = [e.kind for e in tracker.snapshot("pending:bob")]
    assert kinds == ["inbound", "display.user"]


def test_pre_gateway_dispatch_display_user_filters_injected_prompt(
    tracker: SessionTracker,
) -> None:
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
    full_text = (
        "Infoflow injected prompt\n\n"
        "[Sender: alice | human]\n"
        "[message_id: mid-1]\n"
        "[Message]\n"
        "  真实用户消息\n第二行  "
    )
    event = SimpleNamespace(source=source, text=full_text)
    entry = SimpleNamespace(session_id="gw-sess-1", session_key="agent:main:infoflow:dm:alice")

    session_store = SimpleNamespace(_entries={"agent:main:infoflow:dm:alice": entry})
    session_store._ensure_loaded = lambda: None  # type: ignore[method-assign]
    gateway = SimpleNamespace(
        _session_key_for_source=lambda src: "agent:main:infoflow:dm:alice",
    )

    hooks = make_plugin_hooks(tracker)
    hooks["pre_gateway_dispatch"](
        event=event,
        gateway=gateway,
        session_store=session_store,
    )

    events = tracker.snapshot("gw-sess-1")
    inbound = next(e for e in events if e.kind == "inbound")
    display = next(e for e in events if e.kind == "display.user")
    assert inbound.payload["text"] == full_text
    assert display.payload["text"] == "  真实用户消息\n第二行  "
    assert "full_text" not in display.payload


def test_pre_gateway_dispatch_display_user_records_full_prompt_for_admin_view(
    tracker: SessionTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setenv("INFOFLOW_SESSIONTRACKER_FULL_USER_MESSAGE", "true")

    class _Platform:
        value = "infoflow"

    source = SimpleNamespace(
        platform=_Platform(),
        chat_id="alice",
        chat_type="dm",
        user_id="alice",
        user_name="Alice",
    )
    user_body = "真实用户消息\n" + ("x" * (MAX_TEXT_PREVIEW + 1))
    full_text = (
        "Infoflow injected prompt\n\n"
        "[Sender: alice | human]\n"
        "[message_id: mid-1]\n"
        "[Message]\n"
        + user_body
    )
    event = SimpleNamespace(source=source, text=full_text)
    entry = SimpleNamespace(session_id="gw-sess-1", session_key="agent:main:infoflow:dm:alice")

    session_store = SimpleNamespace(_entries={"agent:main:infoflow:dm:alice": entry})
    session_store._ensure_loaded = lambda: None  # type: ignore[method-assign]
    gateway = SimpleNamespace(
        _session_key_for_source=lambda src: "agent:main:infoflow:dm:alice",
    )

    hooks = make_plugin_hooks(tracker)
    hooks["pre_gateway_dispatch"](
        event=event,
        gateway=gateway,
        session_store=session_store,
    )

    display = next(e for e in tracker.snapshot("gw-sess-1") if e.kind == "display.user")
    assert display.payload["text"].startswith("真实用户消息\n")
    assert display.payload["text"].endswith(
        f"... ({len(user_body)} chars total)"
    )
    assert display.payload["full_text"] == full_text


def test_sessiontracker_full_user_message_enabled_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INFOFLOW_SESSIONTRACKER_FULL_USER_MESSAGE", raising=False)
    assert sessiontracker_full_user_message_enabled() is False
    monkeypatch.setenv("INFOFLOW_SESSIONTRACKER_FULL_USER_MESSAGE", "true")
    assert sessiontracker_full_user_message_enabled() is True
    monkeypatch.setenv("INFOFLOW_SESSIONTRACKER_FULL_USER_MESSAGE", "false")
    assert sessiontracker_full_user_message_enabled() is False


def test_pre_gateway_dispatch_display_user_keeps_non_infoflow_message_marker(
    tracker: SessionTracker,
) -> None:
    from types import SimpleNamespace

    class _Platform:
        value = "telegram"

    source = SimpleNamespace(
        platform=_Platform(),
        chat_id="tg-chat",
        chat_type="dm",
        user_id="alice",
        user_name="Alice",
    )
    text = "用户原文第一行\n[Message]\n用户原文第二行"
    event = SimpleNamespace(source=source, text=text)
    entry = SimpleNamespace(session_id="tg-sess-1", session_key="agent:main:telegram:dm:tg-chat")

    session_store = SimpleNamespace(_entries={"agent:main:telegram:dm:tg-chat": entry})
    session_store._ensure_loaded = lambda: None  # type: ignore[method-assign]
    gateway = SimpleNamespace(
        _session_key_for_source=lambda src: "agent:main:telegram:dm:tg-chat",
    )

    hooks = make_plugin_hooks(tracker)
    hooks["pre_gateway_dispatch"](
        event=event,
        gateway=gateway,
        session_store=session_store,
    )

    display = next(e for e in tracker.snapshot("tg-sess-1") if e.kind == "display.user")
    assert display.payload["text"] == text
    assert display.payload["chat_id"] == "tg-chat"
    assert tracker.lookup_session_id("tg-chat") == "tg-sess-1"


def test_post_gateway_session_resolved_ignores_non_infoflow_platform(
    tracker: SessionTracker,
) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["post_gateway_session_resolved"](
        session_id="tg-sess-1",
        platform="telegram",
        chat_id="tg-chat",
        chat_type="dm",
        user_id="alice",
        is_new_session=True,
    )

    assert tracker.get_meta("tg-sess-1") is None
    assert tracker.lookup_tracker_session_id("alice") is None
    assert tracker.lookup_tracker_session_id("tg-chat") is None


def test_pre_gateway_dispatch_non_infoflow_does_not_create_tracker_bucket(
    tracker: SessionTracker,
) -> None:
    source = types.SimpleNamespace(
        platform=types.SimpleNamespace(value="telegram"),
        chat_id="group:1",
        chat_type="group",
        user_id="alice",
        user_name="Alice",
    )
    event = types.SimpleNamespace(source=source, text="telegram group message")
    entry = types.SimpleNamespace(
        session_id="tg-group-sess",
        session_key="agent:main:telegram:group:1",
    )
    session_store = types.SimpleNamespace(_entries={"agent:main:telegram:group:1": entry})
    session_store._ensure_loaded = lambda: None  # type: ignore[method-assign]
    gateway = types.SimpleNamespace(
        _session_key_for_source=lambda src: "agent:main:telegram:group:1",
    )

    hooks = make_plugin_hooks(tracker)
    hooks["pre_gateway_dispatch"](
        event=event,
        gateway=gateway,
        session_store=session_store,
    )

    assert tracker.lookup_session_id("group:1") == "tg-group-sess"
    assert tracker.lookup_tracker_session_id("group:1") is None
    assert [e.kind for e in tracker.snapshot("tg-group-sess")] == [
        "inbound",
        "display.user",
    ]


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


def test_lookup_prefers_active_over_ended_with_more_lines(tracker: SessionTracker) -> None:
    for i in range(10):
        tracker.push_event(
            "old-ended",
            "display.tool_line",
            {"line": f"line {i}"},
            platform="infoflow",
            chat_id="bob",
        )
    meta_old = tracker.get_meta("old-ended")
    assert meta_old is not None
    meta_old.status = "ended"
    meta_old.last_event_at = 9999.0

    tracker.push_event(
        "new-active",
        "session.start",
        {},
        platform="infoflow",
        chat_id="bob",
    )
    meta_new = tracker.get_meta("new-active")
    assert meta_new is not None
    meta_new.status = "active"
    meta_new.last_event_at = 10000.0

    assert tracker.lookup_session_id("bob") == "new-active"


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


def test_session_finalize_first_creates_ended_meta(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["on_session_finalize"](session_id="old-finalized", platform="infoflow")
    meta = tracker.get_meta("old-finalized")
    assert meta is not None
    assert meta.status == "ended"


def test_tracker_session_aggregates_multiple_hermes_sessions(
    tracker: SessionTracker,
) -> None:
    tracker.bind_chat("group:9", "old-hermes")
    tracker.push_event(
        "old-hermes",
        "display.user",
        {"text": "first"},
        platform="infoflow",
    )
    tracker.push_event(
        "old-hermes",
        "session.end",
        {"completed": True},
        platform="infoflow",
    )

    tracker.bind_chat("group:9", "new-hermes")
    tracker.push_event(
        "new-hermes",
        "session.start",
        {"model": "m"},
        platform="infoflow",
    )
    tracker.push_event(
        "new-hermes",
        "display.hermes",
        {"text": "reply"},
        platform="infoflow",
    )

    tracker_sid = tracker.lookup_tracker_session_id("group:9")
    assert tracker_sid == "chat:group:9"
    assert tracker.latest_hermes_session_id("group:9") == "new-hermes"

    events = tracker.snapshot(tracker_sid or "")
    assert [e.kind for e in events if e.kind.startswith("display.")] == [
        "display.user",
        "display.hermes",
    ]
    hermes_ids = [
        e.payload.get("hermes_session_id")
        for e in events
        if e.kind in {"display.user", "display.hermes"}
    ]
    assert hermes_ids == ["old-hermes", "new-hermes"]


def test_pre_gateway_dispatch_stale_session_aggregates_into_new_session(
    tracker: SessionTracker,
) -> None:
    from types import SimpleNamespace

    class _Platform:
        value = "infoflow"

    source = SimpleNamespace(
        platform=_Platform(),
        chat_id="group:9",
        chat_type="group",
        user_id="alice",
        user_name="Alice",
    )
    event = SimpleNamespace(source=source, text="hello")
    old_entry = SimpleNamespace(
        session_id="old-hermes",
        session_key="agent:main:infoflow:group:9",
        suspended=False,
    )

    def _session_key_for_source(src: object) -> str:
        return "agent:main:infoflow:group:9"

    def _ensure_loaded() -> None:
        return None

    def _should_reset(entry: object, src: object) -> str:
        return "idle"

    session_store = SimpleNamespace(
        _entries={"agent:main:infoflow:group:9": old_entry},
    )
    session_store._ensure_loaded = _ensure_loaded  # type: ignore[method-assign]
    session_store._should_reset = _should_reset  # type: ignore[method-assign]
    gateway = SimpleNamespace(_session_key_for_source=_session_key_for_source)

    hooks = make_plugin_hooks(tracker)
    hooks["pre_gateway_dispatch"](
        event=event,
        gateway=gateway,
        session_store=session_store,
    )
    assert tracker.lookup_session_id("group:9") == "pending:group:9"

    hooks["on_session_start"](
        session_id="new-hermes",
        model="m",
        platform="infoflow",
    )
    hooks["post_llm_call"](
        session_id="new-hermes",
        assistant_response="ok",
        model="m",
        platform="infoflow",
    )

    tracker_sid = tracker.lookup_tracker_session_id("group:9")
    assert tracker_sid == "chat:group:9"
    assert tracker.latest_hermes_session_id("group:9") == "new-hermes"
    events = tracker.snapshot(tracker_sid or "")
    assert [e.kind for e in events if e.kind in {"display.user", "display.hermes"}] == [
        "display.user",
        "display.hermes",
    ]


def test_post_gateway_session_resolved_binds_concurrent_pending_chats(
    tracker: SessionTracker,
) -> None:
    hooks = make_plugin_hooks(tracker)

    hooks["pre_gateway_dispatch"](
        event=types.SimpleNamespace(
            source=types.SimpleNamespace(
                platform=types.SimpleNamespace(value="infoflow"),
                chat_id="group:1",
                chat_type="group",
                user_id="alice",
                user_name="Alice",
            ),
            text="first",
        ),
        gateway=None,
        session_store=None,
    )
    hooks["pre_gateway_dispatch"](
        event=types.SimpleNamespace(
            source=types.SimpleNamespace(
                platform=types.SimpleNamespace(value="infoflow"),
                chat_id="group:2",
                chat_type="group",
                user_id="bob",
                user_name="Bob",
            ),
            text="second",
        ),
        gateway=None,
        session_store=None,
    )

    hooks["post_gateway_session_resolved"](
        session_id="hermes-1",
        platform="infoflow",
        chat_id="group:1",
        chat_type="group",
        user_id="alice",
        is_new_session=True,
    )
    hooks["post_gateway_session_resolved"](
        session_id="hermes-2",
        platform="infoflow",
        chat_id="group:2",
        chat_type="group",
        user_id="bob",
        is_new_session=True,
    )
    hooks["post_llm_call"](
        session_id="hermes-1",
        assistant_response="reply one",
        model="m",
        platform="infoflow",
    )
    hooks["post_llm_call"](
        session_id="hermes-2",
        assistant_response="reply two",
        model="m",
        platform="infoflow",
    )

    events_1 = tracker.snapshot("chat:group:1")
    events_2 = tracker.snapshot("chat:group:2")
    assert [e.payload.get("text") for e in events_1 if e.kind.startswith("display.")] == [
        "first",
        "reply one",
    ]
    assert [e.payload.get("text") for e in events_2 if e.kind.startswith("display.")] == [
        "second",
        "reply two",
    ]


def test_post_gateway_session_resolved_updates_tracker_active_idle_state(
    tracker: SessionTracker,
) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["post_gateway_session_resolved"](
        session_id="hermes-active",
        platform="infoflow",
        chat_id="group:9",
        chat_type="group",
        user_id="alice",
        is_new_session=False,
    )

    tracker_meta = tracker.get_meta("chat:group:9")
    hermes_meta = tracker.get_meta("hermes-active")
    assert tracker_meta is not None
    assert hermes_meta is not None
    assert tracker_meta.status == "active"
    assert hermes_meta.status == "active"

    hooks["on_session_end"](
        session_id="hermes-active",
        platform="infoflow",
        model="m",
        completed=True,
    )
    assert tracker.get_meta("hermes-active").status == "ended"  # type: ignore[union-attr]
    assert tracker.get_meta("chat:group:9").status == "idle"  # type: ignore[union-attr]


def test_on_stream_delta_pushes_hermes_stream(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["on_stream_delta"](
        session_id="sess-s1",
        platform="infoflow",
        model="gpt-x",
        delta_text="Hel",
        content_type="text",
        message_so_far="Hel",
        stream_id="stream-1",
    )
    hooks["on_stream_delta"](
        session_id="sess-s1",
        platform="infoflow",
        model="gpt-x",
        delta_text="lo",
        content_type="text",
        message_so_far="Hello",
        stream_id="stream-1",
    )
    snap = [e for e in tracker.snapshot("sess-s1") if e.kind == "display.hermes_stream"]
    assert len(snap) == 2
    assert snap[-1].payload["text"] == "Hello"
    assert snap[-1].payload["stream_id"] == "stream-1"
    assert snap[-1].payload["final"] is False


def test_on_stream_delta_captures_thinking_stream(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["on_stream_delta"](
        session_id="sess-th",
        platform="infoflow",
        model="m",
        delta_text="step one. ",
        content_type="thinking",
        message_so_far="",
        stream_id="thinking-1",
    )
    hooks["on_stream_delta"](
        session_id="sess-th",
        platform="infoflow",
        model="m",
        delta_text="step two.",
        content_type="thinking",
        message_so_far="",
        stream_id="thinking-1",
    )
    hooks["on_stream_delta"](
        session_id="sess-th",
        platform="infoflow",
        model="m",
        delta_text="",
        content_type="thinking",
        message_so_far="",
        stream_id="thinking-1",
        final=True,
    )
    snap = tracker.snapshot("sess-th")
    thinking = [e for e in snap if e.kind == "display.thinking_stream"]
    assert len(thinking) == 3
    assert thinking[0].payload["text"] == "step one. "
    assert thinking[-1].payload["text"] == "step one. step two."
    assert thinking[-1].payload["stream_id"] == "thinking-1"
    assert thinking[-1].payload["final"] is True
    assert all(e.kind != "display.hermes_stream" for e in snap)


def test_pre_api_request_pushes_status_line(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["pre_api_request"](
        session_id="sess-api",
        platform="infoflow",
        model="claude-sonnet-4-6",
        api_call_count=2,
        approx_input_tokens=12345,
        tool_count=17,
    )
    events = [e for e in tracker.snapshot("sess-api") if e.kind == "display.status"]
    assert len(events) == 1
    line = events[0].payload["line"]
    assert "requesting claude-sonnet-4-6" in line
    assert "call #2" in line
    assert "~12,345 input tokens" in line
    assert "17 tools" in line


def test_post_llm_call_finalizes_stream_box_when_streaming(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["on_stream_delta"](
        session_id="sess-s2",
        platform="infoflow",
        model="m",
        delta_text="Hi",
        content_type="text",
        message_so_far="Hi",
        stream_id="stream-2",
    )
    hooks["post_llm_call"](
        session_id="sess-s2",
        user_message="ping",
        assistant_response="Hi there",
        conversation_history=[],
        model="m",
        platform="infoflow",
    )
    kinds = [e.kind for e in tracker.snapshot("sess-s2")]
    # No raw display.hermes when streaming finalizes the stream box.
    assert "display.hermes" not in kinds
    finals = [
        e for e in tracker.snapshot("sess-s2")
        if e.kind == "display.hermes_stream" and e.payload.get("final")
    ]
    assert len(finals) == 1
    assert finals[0].payload["text"] == "Hi there"
    assert finals[0].payload["stream_id"] == "stream-2"


def test_post_llm_call_falls_back_to_hermes_when_no_stream(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["post_llm_call"](
        session_id="sess-noS",
        user_message="ping",
        assistant_response="reply",
        conversation_history=[],
        model="m",
        platform="infoflow",
    )
    kinds = [e.kind for e in tracker.snapshot("sess-noS")]
    assert "display.hermes" in kinds
    assert "display.hermes_stream" not in kinds


def test_on_tool_progress_pushes_lines(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["on_tool_progress"](
        session_id="sess-tp",
        task_id="",
        tool_name="search_files",
        tool_call_id="tc-1",
        stage="start",
        text="todos",
        duration_ms=None,
        is_error=False,
    )
    hooks["on_tool_progress"](
        session_id="sess-tp",
        task_id="",
        tool_name="search_files",
        tool_call_id="tc-1",
        stage="end",
        text="",
        duration_ms=1234.0,
        is_error=False,
    )
    events = [e for e in tracker.snapshot("sess-tp") if e.kind == "display.tool_progress"]
    assert len(events) == 2
    assert events[0].payload["stage"] == "start"
    assert events[1].payload["stage"] == "end"
    assert "1.2s" in events[1].payload["line"]


def test_on_tool_progress_end_uses_cli_cute_message(
    tracker: SessionTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_mod = types.ModuleType("agent")
    display_mod = types.ModuleType("agent.display")

    def fake_cute(tool_name: str, args: dict, duration: float, result: object = None) -> str:
        return f"┊ cute {tool_name} {args.get('query')} {duration:.1f}s {result}"

    display_mod.get_cute_tool_message = fake_cute  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", agent_mod)
    monkeypatch.setitem(sys.modules, "agent.display", display_mod)

    hooks = make_plugin_hooks(tracker)
    hooks["on_tool_progress"](
        session_id="sess-cute",
        task_id="",
        tool_name="search_files",
        tool_call_id="tc-cute",
        stage="end",
        text="",
        args={"query": "todo"},
        result="ok",
        duration_ms=1200.0,
        is_error=False,
    )
    events = [
        e for e in tracker.snapshot("sess-cute")
        if e.kind == "display.tool_progress"
    ]
    assert len(events) == 1
    assert events[0].payload["line"] == "┊ cute search_files todo 1.2s ok"


def test_on_tool_progress_end_force_error_tag_when_formatter_missed_it(
    tracker: SessionTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If hermes-agent flags is_error=True but the cute formatter did not
    produce a failure marker (e.g. multimodal dict result bypassing the
    string heuristic), the dashboard must still surface " [error]"."""
    agent_mod = types.ModuleType("agent")
    display_mod = types.ModuleType("agent.display")

    def fake_cute(tool_name: str, args: dict, duration: float, result: object = None) -> str:
        return f"┊ cute {tool_name} {duration:.1f}s"

    display_mod.get_cute_tool_message = fake_cute  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", agent_mod)
    monkeypatch.setitem(sys.modules, "agent.display", display_mod)

    hooks = make_plugin_hooks(tracker)
    hooks["on_tool_progress"](
        session_id="sess-err",
        task_id="",
        tool_name="vision_analyze",
        tool_call_id="tc-err",
        stage="end",
        text="",
        args={"question": "?"},
        result={"image": "..."},  # multimodal dict, formatter cannot detect
        duration_ms=500.0,
        is_error=True,
    )
    events = [
        e for e in tracker.snapshot("sess-err")
        if e.kind == "display.tool_progress"
    ]
    assert events[-1].payload["line"].endswith(" [error]")


def test_pre_tool_call_terminal_no_longer_pushes_preparing_line(
    tracker: SessionTracker,
) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["pre_tool_call"](
        session_id="sess-prep",
        tool_name="terminal",
        args={"command": "ls"},
        tool_call_id="tc-prep",
        task_id="",
    )
    kinds = [e.kind for e in tracker.snapshot("sess-prep")]
    assert kinds == ["tool.start"]


def test_on_interim_assistant_pushes_interim_line(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["on_interim_assistant"](
        session_id="sess-ia",
        platform="infoflow",
        model="m",
        message_text="let me check the file",
        already_streamed=False,
        reason="pre_tool",
    )
    events = [e for e in tracker.snapshot("sess-ia") if e.kind == "display.interim"]
    assert len(events) == 1
    assert events[0].payload["text"] == "let me check the file"
    assert events[0].payload["reason"] == "pre_tool"


def test_on_interim_assistant_skips_when_already_streamed(tracker: SessionTracker) -> None:
    """When the sentence was emitted via on_stream_delta, suppress the interim
    line so we don't render the same content twice."""
    hooks = make_plugin_hooks(tracker)
    hooks["on_interim_assistant"](
        session_id="sess-ia2",
        platform="infoflow",
        model="m",
        message_text="checking…",
        already_streamed=True,
        reason="pre_tool",
    )
    events = [e for e in tracker.snapshot("sess-ia2") if e.kind == "display.interim"]
    assert events == []


def test_on_stream_delta_final_marks_stream_box(tracker: SessionTracker) -> None:
    hooks = make_plugin_hooks(tracker)
    hooks["on_stream_delta"](
        session_id="sess-end",
        platform="infoflow",
        model="m",
        delta_text="Hi",
        content_type="text",
        message_so_far="Hi",
        stream_id="stream-end-1",
        final=False,
    )
    hooks["on_stream_delta"](
        session_id="sess-end",
        platform="infoflow",
        model="m",
        delta_text="",
        content_type="text",
        message_so_far="Hi there",
        stream_id="stream-end-1",
        final=True,
    )
    finals = [
        e for e in tracker.snapshot("sess-end")
        if e.kind == "display.hermes_stream" and e.payload.get("final")
    ]
    assert len(finals) == 1
    assert finals[0].payload["text"] == "Hi there"
    assert finals[0].payload["stream_id"] == "stream-end-1"


def test_post_llm_call_skips_display_hermes_when_text_matches_finalized_stream(
    tracker: SessionTracker,
) -> None:
    """If the stream already finalized with the exact same text, post_llm_call
    must not push an extra display.hermes (it would double-render)."""
    hooks = make_plugin_hooks(tracker)
    hooks["on_stream_delta"](
        session_id="sess-dup",
        platform="infoflow",
        model="m",
        delta_text="Hello",
        content_type="text",
        message_so_far="Hello world",
        stream_id="stream-dup-1",
        final=False,
    )
    hooks["on_stream_delta"](
        session_id="sess-dup",
        platform="infoflow",
        model="m",
        delta_text="",
        content_type="text",
        message_so_far="Hello world",
        stream_id="stream-dup-1",
        final=True,
    )
    hooks["post_llm_call"](
        session_id="sess-dup",
        user_message="hi",
        assistant_response="Hello world",
        conversation_history=[],
        model="m",
        platform="infoflow",
    )
    snap = tracker.snapshot("sess-dup")
    assert all(e.kind != "display.hermes" for e in snap)
    finals = [
        e for e in snap
        if e.kind == "display.hermes_stream" and e.payload.get("final")
    ]
    assert len(finals) == 1


def test_post_llm_call_pushes_display_hermes_when_text_diverges(
    tracker: SessionTracker,
) -> None:
    """If post-stream transformations change the final text vs. what was
    streamed, render the corrected version once."""
    hooks = make_plugin_hooks(tracker)
    hooks["on_stream_delta"](
        session_id="sess-div",
        platform="infoflow",
        model="m",
        delta_text="Hi",
        content_type="text",
        message_so_far="Hi raw",
        stream_id="stream-div-1",
        final=False,
    )
    hooks["on_stream_delta"](
        session_id="sess-div",
        platform="infoflow",
        model="m",
        delta_text="",
        content_type="text",
        message_so_far="Hi raw",
        stream_id="stream-div-1",
        final=True,
    )
    hooks["post_llm_call"](
        session_id="sess-div",
        user_message="hi",
        assistant_response="Hi polished",
        conversation_history=[],
        model="m",
        platform="infoflow",
    )
    snap = tracker.snapshot("sess-div")
    hermes_events = [e for e in snap if e.kind == "display.hermes"]
    assert len(hermes_events) == 1
    assert hermes_events[0].payload["text"] == "Hi polished"


def test_post_tool_call_skips_tool_line_when_progress_pipeline_active(
    tracker: SessionTracker,
) -> None:
    """In hermes-agent the real ordering is start → post_tool_call → end.

    When on_tool_progress(start) has fired for this tool_call_id, the richer
    progress pipeline is in use and post_tool_call must NOT push the older
    display.tool_line. Otherwise the UI shows both the in-place updating
    progress line AND a duplicate completion line.
    """
    hooks = make_plugin_hooks(tracker)
    hooks["on_tool_progress"](
        session_id="sess-td",
        task_id="",
        tool_name="search_files",
        tool_call_id="tc-dup",
        stage="start",
        text="todos",
        duration_ms=None,
        is_error=False,
    )
    hooks["post_tool_call"](
        tool_name="search_files",
        args={"query": "foo"},
        result="ok",
        task_id="",
        session_id="sess-td",
        tool_call_id="tc-dup",
        duration_ms=500,
    )
    hooks["on_tool_progress"](
        session_id="sess-td",
        task_id="",
        tool_name="search_files",
        tool_call_id="tc-dup",
        stage="end",
        text="",
        duration_ms=500.0,
        is_error=False,
    )
    kinds = [e.kind for e in tracker.snapshot("sess-td")]
    # Both start and end progress lines, but no duplicate display.tool_line.
    assert kinds.count("display.tool_progress") == 2
    assert "display.tool_line" not in kinds
    assert "tool.end" in kinds


def test_concurrent_tools_do_not_drop_progress_dedup(
    tracker: SessionTracker,
) -> None:
    """When two tools run in parallel (start A, start B, post A, post B,
    end A, end B), neither post_tool_call should emit display.tool_line —
    both tool_call_ids must remain tracked across each other's lifecycles."""
    hooks = make_plugin_hooks(tracker)
    for tid in ("tc-A", "tc-B"):
        hooks["on_tool_progress"](
            session_id="sess-conc", task_id="", tool_name="search_files",
            tool_call_id=tid, stage="start", text="x",
            duration_ms=None, is_error=False,
        )
    for tid in ("tc-A", "tc-B"):
        hooks["post_tool_call"](
            tool_name="search_files", args={"q": tid}, result="ok",
            task_id="", session_id="sess-conc",
            tool_call_id=tid, duration_ms=10,
        )
    for tid in ("tc-A", "tc-B"):
        hooks["on_tool_progress"](
            session_id="sess-conc", task_id="", tool_name="search_files",
            tool_call_id=tid, stage="end", text="",
            duration_ms=10.0, is_error=False,
        )
    snap = tracker.snapshot("sess-conc")
    kinds = [e.kind for e in snap]
    assert "display.tool_line" not in kinds
    assert kinds.count("display.tool_progress") == 4  # 2 starts + 2 ends
    assert kinds.count("tool.end") == 2


def test_session_end_clears_dedup_state(tracker: SessionTracker) -> None:
    """on_session_end / on_session_finalize must drop the per-session
    bookkeeping so long-lived processes don't accumulate one entry per
    session for streams / tool progress that never reach post_llm_call.

    Reaches into the hook closures via a controlled probe: after end,
    a brand-new tool_call_id with on_tool_progress(start) then
    post_tool_call should still suppress display.tool_line — proving the
    started-set is functional, not leaking entries from the prior session.
    """
    hooks = make_plugin_hooks(tracker)
    # Seed prior-session state via on_tool_progress(start) for sid=old.
    hooks["on_tool_progress"](
        session_id="old", task_id="", tool_name="t",
        tool_call_id="tc-old", stage="start", text="",
        duration_ms=None, is_error=False,
    )
    hooks["on_stream_delta"](
        session_id="old", platform="infoflow", model="m",
        delta_text="hi", content_type="text",
        message_so_far="hi", stream_id="s-old", final=False,
    )
    # End the session.
    hooks["on_session_end"](
        session_id="old", platform="infoflow", model="m",
    )
    # Restart with the *same* sid (Hermes may rotate ids but reuse a key).
    # post_tool_call for the unseen tool_call_id must render display.tool_line
    # because the new session has no on_tool_progress(start) for it.
    hooks["post_tool_call"](
        tool_name="t", args={}, result="ok",
        task_id="", session_id="old",
        tool_call_id="tc-fresh", duration_ms=5,
    )
    kinds_after = [e.kind for e in tracker.snapshot("old")]
    # Fresh tool_call_id is not in any stale started-set → tool_line renders.
    assert "display.tool_line" in kinds_after


def test_post_tool_call_still_renders_tool_line_when_no_progress_hook(
    tracker: SessionTracker,
) -> None:
    """If on_tool_progress never fired (e.g. older hermes-agent without the
    hook), post_tool_call must still emit display.tool_line."""
    hooks = make_plugin_hooks(tracker)
    hooks["post_tool_call"](
        tool_name="search_files",
        args={"query": "bar"},
        result="ok",
        task_id="",
        session_id="sess-tline",
        tool_call_id="tc-only",
        duration_ms=300,
    )
    kinds = [e.kind for e in tracker.snapshot("sess-tline")]
    assert "display.tool_line" in kinds
    assert "tool.end" in kinds


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
async def test_sse_dedup_when_event_arrives_between_snapshot_and_drain() -> None:
    """Regression: the api_session_events handler must subscribe BEFORE
    building the snapshot, otherwise events that arrive in the gap window
    between snapshot iteration and queue join are silently dropped.

    We simulate the race by pushing a second event after the response has
    been prepared but before the test client finishes reading. With the
    pre-subscribe fix the second event must arrive on the SSE stream,
    and the snapshot events must NOT be re-delivered through the queue.
    """
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    tr = SessionTracker(buffer_size=50)
    app = web.Application()
    register_routes(app, tr, base_path="/webhook/infoflow")

    sid = "sse-race"
    tr.push_event(sid, "session.start", {"model": "t"}, platform="infoflow")
    tr.push_event(sid, "display.tool_line", {"line": "first"}, platform="infoflow")

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            f"/webhook/infoflow/dashboard/api/sessions/{sid}/events?cursor=0"
        )
        assert resp.status == 200

        async def _push_late_event_then_close() -> None:
            await asyncio.sleep(0.05)
            tr.push_event(sid, "display.tool_line", {"line": "late"}, platform="infoflow")
            await asyncio.sleep(0.05)
            tr.push_event(sid, "session.end", {"completed": True}, platform="infoflow")
            tr.push_event(sid, "session.end", {"completed": True}, platform="infoflow")

        pusher = asyncio.create_task(_push_late_event_then_close())

        snapshot_seen = False
        late_seen = False
        snapshot_seq_max = 0
        live_seqs: list[int] = []
        try:
            async for raw in resp.content:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("event: snapshot"):
                    snapshot_seen = True
                    continue
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line[len("data:"):].strip())
                if "events" in payload:
                    for ev in payload["events"]:
                        snapshot_seq_max = max(snapshot_seq_max, int(ev.get("seq", 0)))
                    continue
                seq = int(payload.get("seq", 0))
                live_seqs.append(seq)
                if payload.get("kind") == "display.tool_line" and (
                    payload.get("payload", {}).get("line") == "late"
                ):
                    late_seen = True
                if len(live_seqs) >= 3:
                    break
        finally:
            pusher.cancel()
            with contextlib.suppress(Exception):
                await pusher
            resp.close()

        assert snapshot_seen, "initial snapshot must be sent"
        assert late_seen, (
            "event pushed after the snapshot must reach the SSE consumer "
            "(pre-subscribe fix)"
        )
        # No live event should repeat a seq already covered by the snapshot.
        assert all(s > snapshot_seq_max for s in live_seqs), (
            f"snapshot covered up to seq {snapshot_seq_max} but live stream "
            f"replayed seqs {live_seqs}"
        )


@pytest.mark.asyncio
async def test_dashboard_sse_unsubscribes_when_snapshot_write_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    tr = SessionTracker(buffer_size=50)
    app = web.Application()
    register_routes(app, tr, base_path="/webhook/infoflow")

    sid = "dashboard-disconnect"
    tr.push_event(sid, "session.start", {"model": "t"}, platform="infoflow")
    tr.push_event(sid, "display.tool_line", {"line": "first"}, platform="infoflow")
    calls: list[str] = []

    async def _disconnecting_write_sse(*args: object, **kwargs: object) -> bool:
        calls.append(str(kwargs.get("context") or ""))
        return False

    monkeypatch.setattr("hermes_infoflow.dashboard.write_sse", _disconnecting_write_sse)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            f"/webhook/infoflow/dashboard/api/sessions/{sid}/events?cursor=0"
        )
        assert resp.status == 200
        await resp.read()

    assert sid not in tr._subscribers  # noqa: SLF001
    assert calls == ["dashboard snapshot"]


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
