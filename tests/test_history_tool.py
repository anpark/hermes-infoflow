"""Tests for ``infoflow_get_message_history``."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_infoflow import message_store as ms
from hermes_infoflow import tools
from hermes_infoflow.bot import recall_inbound_message_id_hint_scope
from hermes_infoflow.message_store import MessageStore


def _today_at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.now().astimezone().replace(
        hour=hour,
        minute=minute,
        second=second,
        microsecond=0,
    )


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _time_arg(dt: datetime) -> str:
    return (
        f"{dt.year}.{dt.month:02d}.{dt.day:02d} "
        f"{dt.hour:02d}.{dt.minute:02d}.{dt.second:02d}"
    )


def _adapter_for(store: MessageStore, *, admin_uid: str = "admin") -> SimpleNamespace:
    def _lookup(key: str) -> str:
        if key.startswith("user:"):
            rec = store.find_user_by_user_id(key.removeprefix("user:"))
            return rec.name if rec else ""
        if key.startswith("bot:"):
            rec = store.find_bot_by_agent_id(key.removeprefix("bot:"))
            return rec.name if rec else ""
        return ""

    return SimpleNamespace(
        _message_store=store,
        _admin_uid=admin_uid,
        _participant_name_for_key=_lookup,
    )


def test_history_tool_message_window_returns_json_array(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    store.upsert_participant(participant_type="user", user_id="alice", name="Alice")
    base_dt = _today_at(9, 20)
    created_times: dict[str, datetime] = {}
    for idx, (mid, sender) in enumerate(
        (("m1", "user:bob"), ("m2", "user:alice"), ("m3", "user:admin")),
        start=1,
    ):
        created = base_dt.replace(minute=20 + idx, second=idx)
        created_times[mid] = created
        store.persist_group(
            message_id=mid,
            group_id="4507088",
            sender=sender,
            content=f"text {idx}",
            created_time=_ms(created),
        )

    adapter = _adapter_for(store)
    monkeypatch.setattr(tools, "_get_live_adapter", lambda: adapter)

    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("m3"):
        result = asyncio.run(handler({
            "message_id": "m2",
            "before_count": 1,
            "after_count": 1,
        }))

    parsed = json.loads(result)
    assert isinstance(parsed, list)
    assert [item["content"].splitlines()[2] for item in parsed] == [
        f"[Message: message_id:'m1'; created_time:'{_time_arg(created_times['m1'])}']",
        f"[Message: message_id:'m2'; created_time:'{_time_arg(created_times['m2'])}']",
        f"[Message: message_id:'m3'; created_time:'{_time_arg(created_times['m3'])}']",
    ]
    assert (
        "[Sender: type:'human'; user_id:'alice'; name:'Alice'; permission:'restricted']"
        in parsed[1]["content"]
    )
    assert "[Unread Message Context:" not in parsed[1]["content"]
    assert "[Handling Strategy]" not in parsed[1]["content"]


def test_history_tool_rejects_cross_conversation_for_restricted_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:bob",
        content="current",
        created_time=_ms(_today_at(9, 30)),
    )
    store.persist_group(
        message_id="other",
        group_id="999",
        sender="user:alice",
        content="other",
        created_time=_ms(_today_at(9, 31)),
    )

    monkeypatch.setattr(tools, "_get_live_adapter", lambda: _adapter_for(store))
    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({"target": "group:999"}))

    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "Only admin" in parsed["error"]


def test_history_tool_allows_admin_explicit_target_time_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    store.upsert_participant(participant_type="user", user_id="alice", name="Alice")
    today = _today_at(9, 31, 45)
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:carol",
        content="current",
        created_time=_ms(_today_at(9, 30)),
    )
    store.persist_group(
        message_id="other",
        group_id="999",
        sender="user:alice",
        content="other",
        created_time=_ms(today),
    )

    monkeypatch.setattr(
        tools,
        "_get_live_adapter",
        lambda: _adapter_for(store, admin_uid="admin,carol"),
    )
    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({
            "target": "infoflow:group:999",
            "start_time": _time_arg(today),
            "end_time": _time_arg(today),
        }))

    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["time"] == _time_arg(today)
    assert f"[Message: message_id:'other'; created_time:'{_time_arg(today)}']" in parsed[0]["content"]


def test_history_tool_end_time_is_second_inclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    current = _today_at(9, 30)
    included = _today_at(19, 56, 59)
    excluded = _today_at(19, 57, 0)
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:admin",
        content="current",
        created_time=_ms(current),
    )
    store.persist_group(
        message_id="included",
        group_id="999",
        sender="user:alice",
        content="included",
        created_time=_ms(included) + 999,
    )
    store.persist_group(
        message_id="excluded",
        group_id="999",
        sender="user:alice",
        content="excluded",
        created_time=_ms(excluded),
    )

    monkeypatch.setattr(tools, "_get_live_adapter", lambda: _adapter_for(store))
    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({
            "target": "group:999",
            "start_time": _time_arg(included),
            "end_time": _time_arg(included),
        }))

    parsed = json.loads(result)
    assert [item["content"].splitlines()[2] for item in parsed] == [
        f"[Message: message_id:'included'; created_time:'{_time_arg(included)}']"
    ]


def test_history_tool_invalid_time_returns_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:admin",
        content="current",
        created_time=_ms(_today_at(9, 30)),
    )

    monkeypatch.setattr(tools, "_get_live_adapter", lambda: _adapter_for(store))
    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({"start_time": "2026.99.99"}))

    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "start_time must use format" in parsed["error"]


def test_history_tool_message_id_ignores_time_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    created = _today_at(9, 30, 1)
    store.persist_group(
        message_id="current",
        group_id="4507088",
        sender="user:bob",
        content="current",
        created_time=_ms(created),
    )

    monkeypatch.setattr(tools, "_get_live_adapter", lambda: _adapter_for(store))
    handler = tools.make_history_handler()
    with recall_inbound_message_id_hint_scope("current"):
        result = asyncio.run(handler({
            "message_id": "current",
            "start_time": "invalid",
            "date": "2026.99.99",
        }))

    parsed = json.loads(result)
    assert len(parsed) == 1
    assert f"[Message: message_id:'current'; created_time:'{_time_arg(created)}']" in parsed[0]["content"]
