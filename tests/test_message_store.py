"""Tests for hermes_infoflow.message_store (msgid2 persistence)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_infoflow import message_store as ms
from hermes_infoflow.message_store import MessageStore


def test_persist_group_stores_msgid2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store = MessageStore(account_id="test-acct")
    rec = store.persist_group(
        message_id="mid-1",
        group_id="4507088",
        sender_id="bob",
        is_inbound=True,
        msgid2="300014580",
        text="hello",
    )
    assert rec is not None
    assert rec.msgid2 == "300014580"

    found = store.find_group("mid-1")
    assert found is not None
    assert found.msgid2 == "300014580"


def test_persist_group_alter_table_migration_on_old_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing DB without msgid2 column gets migrated via ALTER TABLE."""
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)
    store1 = MessageStore(account_id="test-acct")
    store1.persist_group(
        message_id="mid-old",
        group_id="1",
        text="before migration",
    )
    store2 = MessageStore(account_id="test-acct")
    rec = store2.persist_group(
        message_id="mid-new",
        group_id="1",
        msgid2="999",
        text="after migration",
    )
    assert rec is not None
    assert rec.msgid2 == "999"
    found = store2.find_group("mid-new")
    assert found is not None
    assert found.msgid2 == "999"


def test_pre_migration_schema_then_open_adds_msgid2_at_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate an existing pre-msgid2 DB: ALTER adds column at end,
    SELECT must still return msgid2 in the expected position via explicit
    column list.
    """
    monkeypatch.setattr(ms, "_STATE_BASE_DIR", tmp_path)

    db_dir = tmp_path / "test-acct"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "messages.db"

    # Hand-create the OLD schema (without msgid2).
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE group_messages (
            message_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL DEFAULT '',
            sender_id TEXT NOT NULL DEFAULT '',
            sender_name TEXT NOT NULL DEFAULT '',
            sender_imid TEXT NOT NULL DEFAULT '',
            sender_is_bot INTEGER NOT NULL DEFAULT 0,
            is_inbound INTEGER NOT NULL DEFAULT 1,
            bot_was_mentioned INTEGER NOT NULL DEFAULT 0,
            text TEXT NOT NULL DEFAULT '',
            digest TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            raw_json TEXT
        )
        """
    )
    now = time.time()
    conn.execute(
        "INSERT INTO group_messages (message_id, group_id, text, digest, created_at) "
        "VALUES ('legacy-1', '4507088', 'before migration', '', ?)",
        (now,),
    )
    conn.commit()
    conn.close()

    # First open triggers ALTER TABLE migration (msgid2 added at end).
    store = MessageStore(account_id="test-acct")

    # Legacy row should still be readable, with msgid2 defaulting to "".
    legacy = store.find_group("legacy-1")
    assert legacy is not None
    assert legacy.text == "before migration"
    assert legacy.msgid2 == ""

    # New writes correctly persist msgid2 too.
    new_rec = store.persist_group(
        message_id="new-1",
        group_id="4507088",
        msgid2="300014580",
        text="after migration",
    )
    assert new_rec is not None
    found = store.find_group("new-1")
    assert found is not None
    assert found.msgid2 == "300014580"
    assert found.text == "after migration"

    # list also returns correct fields.
    recent = store.recent_group("4507088", limit=10)
    by_id = {r.message_id: r for r in recent}
    assert by_id["legacy-1"].msgid2 == ""
    assert by_id["legacy-1"].text == "before migration"
    assert by_id["new-1"].msgid2 == "300014580"
    assert by_id["new-1"].text == "after migration"
