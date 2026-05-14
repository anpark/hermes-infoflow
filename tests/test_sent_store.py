"""Tests for hermes_infoflow.sent_store.SentMessageStore."""

from __future__ import annotations

from hermes_infoflow.sent_store import SentMessageStore


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
