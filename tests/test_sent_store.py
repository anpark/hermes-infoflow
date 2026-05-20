"""Tests for hermes_infoflow.sent_store.SentMessageStore."""

from __future__ import annotations

from pathlib import Path

from hermes_infoflow.sent_store import (
    SentMessageStore,
)


def test_record_populates_shared_dedup_set() -> None:
    """``record`` and inbound dedup MUST share one ``set`` instance."""
    shared: set[str] = set()
    store = SentMessageStore(dedup_set=shared)
    store.record("group:1", "mid-1")
    assert "mid-1" in shared
    assert store.is_duplicate("mid-1") is True


def test_mark_seen_marks_foreign_id() -> None:
    shared: set[str] = set()
    store = SentMessageStore(dedup_set=shared)
    store.mark_seen("inbound-42")
    assert "inbound-42" in shared


def test_recent_returns_newest_first() -> None:
    store = SentMessageStore()
    store.record("group:1", "a")
    store.record("group:1", "b")
    store.record("group:1", "c")
    ids = [m.messageid for m in store.recent("group:1", count=2)]
    assert ids == ["c", "b"]


def test_recent_count_zero_returns_empty() -> None:
    store = SentMessageStore()
    store.record("group:1", "a")
    assert store.recent("group:1", count=0) == []


def test_ttl_expiry_evicts_old_dedup_entries() -> None:
    shared: set[str] = set()
    store = SentMessageStore(dedup_set=shared, ttl_seconds=10)
    store.record("g", "m1", now=1_000.0)
    assert "m1" in shared
    # Advance well past TTL.
    store.mark_seen("m2", now=1_100.0)
    assert "m1" not in shared
    assert "m2" in shared


def test_find_returns_matching_entry() -> None:
    store = SentMessageStore()
    store.record("group:1", "a")
    store.record("group:1", "b", msgseqid="seq-b")
    found = store.find("group:1", "b")
    assert found is not None
    assert found.msgseqid == "seq-b"
    assert store.find("group:1", "missing") is None


# ---------------------------------------------------------------------------
# Bounded dedup set (Fix #5)
# ---------------------------------------------------------------------------


def test_dedup_set_respects_max_size_cap() -> None:
    shared: set[str] = set()
    store = SentMessageStore(dedup_set=shared, max_dedup_entries=3)
    for i in range(10):
        store.record("g", f"m{i}", now=1_000.0 + i)
    # Only the most recent 3 must remain in the dedup set.
    assert len(shared) == 3
    assert shared == {"m7", "m8", "m9"}


# ---------------------------------------------------------------------------
# SQLite persistence (Fix #6)
# ---------------------------------------------------------------------------


def test_sqlite_persists_across_store_instances(tmp_path: Path) -> None:
    db = tmp_path / "infoflow" / "sent.db"
    a = SentMessageStore(db_path=db, account_id="acct-A")
    a.record("group:1", "MID-1", msgseqid="SEQ-1", digest="hello")

    # Fresh in-memory store sharing the same DB sees the persisted row.
    b = SentMessageStore(db_path=db, account_id="acct-A")
    found = b.find("group:1", "MID-1")
    assert found is not None
    assert found.msgseqid == "SEQ-1"
    assert found.digest == "hello"

    recent = b.recent("group:1", count=5)
    assert any(r.messageid == "MID-1" for r in recent)


def test_sqlite_account_isolation(tmp_path: Path) -> None:
    db = tmp_path / "sent.db"
    a = SentMessageStore(db_path=db, account_id="acct-A")
    a.record("group:1", "MID-A")
    b = SentMessageStore(db_path=db, account_id="acct-B")
    assert b.find("group:1", "MID-A") is None
    b.record("group:1", "MID-B")
    assert a.find("group:1", "MID-A") is not None


def test_sqlite_remove_deletes_from_db(tmp_path: Path) -> None:
    db = tmp_path / "sent.db"
    s = SentMessageStore(db_path=db, account_id="acct")
    s.record("group:1", "MID-1")
    s.remove("group:1", "MID-1")

    fresh = SentMessageStore(db_path=db, account_id="acct")
    assert fresh.find("group:1", "MID-1") is None


def test_recent_merges_in_memory_and_db(tmp_path: Path) -> None:
    db = tmp_path / "sent.db"
    a = SentMessageStore(db_path=db, account_id="acct")
    a.record("group:1", "OLD-1", now=1_000.0)

    b = SentMessageStore(db_path=db, account_id="acct")
    b.record("group:1", "NEW-1", now=2_000.0)

    # ``b`` only has NEW-1 in-memory but should see OLD-1 via the DB layer.
    ids = [r.messageid for r in b.recent("group:1", count=5)]
    assert "NEW-1" in ids and "OLD-1" in ids
    # Newest-first.
    assert ids.index("NEW-1") < ids.index("OLD-1")


def test_concurrent_writers_dont_lose_records(tmp_path: Path) -> None:
    """Multiple threads using their own ``SentMessageStore`` against the same
    DB file must not lose records under WAL contention. Regression for the
    "sqlite init failed: database is locked" silent loss observed during
    the OpenClaw parity audit.
    """
    import threading

    db = tmp_path / "sent.db"
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        s = SentMessageStore(db_path=db, account_id="acct")
        try:
            for j in range(20):
                s.record(f"c{i}", f"m{i}-{j}")
        except BaseException as exc:  # noqa: BLE001 — collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    fresh = SentMessageStore(db_path=db, account_id="acct")
    for i in range(5):
        assert len(fresh.recent(f"c{i}", count=50)) == 20, (
            f"chat c{i} lost records under concurrent writers"
        )
