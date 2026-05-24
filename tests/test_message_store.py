"""Tests for the Infoflow message fact store."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_infoflow import message_store as ms
from hermes_infoflow.itypes import GroupMember
from hermes_infoflow.message_store import MessageStore


def test_old_schema_is_dropped_and_new_schema_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    db_dir = tmp_path / "test-acct"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "messages.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE dm_messages (message_id TEXT PRIMARY KEY, text TEXT)")
    conn.execute("INSERT INTO dm_messages VALUES ('legacy-dm', 'old')")
    conn.execute(
        """
        CREATE TABLE group_messages (
            message_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("INSERT INTO group_messages VALUES ('legacy-group', '1', 'old')")
    conn.commit()
    conn.close()

    store = MessageStore(account_id="test-acct")
    rec = store.persist_group(
        message_id="new-1",
        group_id="4507088",
        sender="user:alice",
        content="hello",
    )

    assert rec is not None
    assert store.find_group("legacy-group") is None
    assert store.find_dm("legacy-dm") is None
    found = store.find_group("new-1")
    assert found is not None
    assert found.content == "hello"


def test_group_upsert_preserves_first_seen_created_time_and_fills_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    created_time = int(time.time() * 1000)

    store.persist_group(
        message_id="mid-1",
        group_id="4507088",
        sender="bot:6471",
        self_id="bot:6471",
        is_outgoing=True,
        content="provisional",
        created_time=created_time,
    )
    store.persist_group(
        message_id="mid-1",
        group_id="4507088",
        sender="bot:6471",
        self_id="bot:6471",
        is_outgoing=False,
        mentions_you=True,
        msg_id2="300014580",
        content="echo body",
        msg_time=2000,
        raw_json='{"fromid":"999","body":[]}',
        created_time=9999,
    )

    found = store.find_group("mid-1")
    assert found is not None
    assert found.created_time == created_time
    assert found.is_outgoing is True
    assert found.mentions_you is True
    assert found.msg_id2 == "300014580"
    assert found.content == "echo body"
    assert found.msg_time == 2000
    assert found.raw_json == '{"fromid":"999","body":[]}'


def test_group_echo_first_content_wins_over_late_send_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    first_seen = int(time.time() * 1000)
    late_seen = first_seen + 1

    store.persist_group(
        message_id="mid-echo-first",
        group_id="4507088",
        sender="bot:6471",
        self_id="bot:6471",
        is_outgoing=True,
        content="canonical echo body",
        msg_id2="300014580",
        msg_time=2000,
        raw_json='{"fromid":999,"body":[{"type":"TEXT","content":"canonical echo body"}]}',
        created_time=first_seen,
    )
    store.persist_group(
        message_id="mid-echo-first",
        group_id="4507088",
        sender="bot:6471",
        self_id="bot:6471",
        is_outgoing=True,
        content="provisional send body",
        created_time=late_seen,
    )

    found = store.find_group("mid-echo-first")
    assert found is not None
    assert found.created_time == first_seen
    assert found.content == "canonical echo body"
    assert found.msg_id2 == "300014580"
    assert found.msg_time == 2000
    assert found.raw_json == '{"fromid":999,"body":[{"type":"TEXT","content":"canonical echo body"}]}'


def test_private_echo_first_content_wins_over_late_send_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    first_seen = int(time.time() * 1000)
    late_seen = first_seen + 1

    store.persist_dm(
        message_id="dm-echo-first",
        peer="user:alice",
        self_id="bot:6471",
        sender="bot:6471",
        is_outgoing=True,
        content="canonical dm echo",
        msg_id2="300014581",
        msg_time=2000,
        raw_json='{"FromId":999,"Content":"canonical dm echo"}',
        created_time=first_seen,
    )
    store.persist_dm(
        message_id="dm-echo-first",
        peer="user:alice",
        self_id="bot:6471",
        sender="bot:6471",
        is_outgoing=True,
        content="provisional dm body",
        created_time=late_seen,
    )

    found = store.find_dm("dm-echo-first")
    assert found is not None
    assert found.created_time == first_seen
    assert found.content == "canonical dm echo"
    assert found.msg_id2 == "300014581"
    assert found.msg_time == 2000
    assert found.raw_json == '{"FromId":999,"Content":"canonical dm echo"}'


def test_private_raw_json_keeps_from_user_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")

    store.persist_dm(
        message_id="dm-1",
        peer="user:alice",
        self_id="bot:6471",
        sender="user:alice",
        content="hi",
        raw_json='{"FromUserId":"alice","FromUserName":"临时昵称"}',
    )

    found = store.find_dm("dm-1")
    assert found is not None
    assert found.raw_json == '{"FromUserId":"alice","FromUserName":"临时昵称"}'
    assert found.sender == "user:alice"


def test_participant_upsert_backfills_group_sender_by_imid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")

    store.persist_group(
        message_id="mid-1",
        group_id="4507088",
        sender="",
        content="bot echo before member cache",
        raw_json='{"fromid":999}',
    )
    rec = store.upsert_participant(
        participant_type="bot",
        agent_id="6471",
        imid="999",
        name="helper",
    )

    assert rec is not None
    assert rec.key == "bot:6471"
    found = store.find_group("mid-1")
    assert found is not None
    assert found.sender == "bot:6471"


def test_group_members_update_participants_without_human_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")

    store.upsert_group_members(
        "4507088",
        [
            GroupMember(uid="6471", name="helper", imid="999", agent_id=6471, is_bot=True),
            GroupMember(uid="alice", name="Should Not Store", is_bot=False),
        ],
    )

    bot = store.find_bot_by_agent_id("6471")
    user = store.find_user_by_user_id("alice")
    assert bot is not None
    assert bot.imid == "999"
    assert bot.name == "helper"
    assert user is not None
    assert user.name == ""
