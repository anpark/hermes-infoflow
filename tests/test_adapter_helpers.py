"""Tests for adapter-level helpers that don't require hermes-agent.

These cover the moving parts added in the OpenClaw parity pass:

* Recall-intent regex (``_looks_like_recall_intent`` / ``_looks_like_recall_latest``).
* Inbound-context registry (TTL + eviction).
* Env-driven settings parser (``_read_account_settings``) for the new
  watch_regex / follow_up / per-group / state_dir / robot_id fields.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_infoflow import recall as ad
from hermes_infoflow.recall import (
    _InboundContext,
    _looks_like_recall_intent,
    _looks_like_recall_latest,
    _lookup_inbound_context,
    _register_inbound_context,
)
from hermes_infoflow.settings import (
    _parse_infoflow_target,
    _read_account_settings,
)


# ---------------------------------------------------------------------------
# Recall intent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "请撤回上一条",
        "把刚才那条收回吧",
        "recall the last message",
        "unsend that please",
        "delete the previous reply",
        "把那条删掉",
    ],
)
def test_recall_intent_triggers(text: str) -> None:
    assert _looks_like_recall_intent(text)


@pytest.mark.parametrize("text", ["", "hello world", "today's release", "撤"])
def test_recall_intent_does_not_overmatch(text: str) -> None:
    assert not _looks_like_recall_intent(text)


@pytest.mark.parametrize(
    "text",
    [
        "撤回上一条",
        "recall the last reply",
        "撤回最近一条",
        "把刚才那条撤回",
    ],
)
def test_recall_latest_requires_both_verb_and_temporal_qualifier(text: str) -> None:
    assert _looks_like_recall_latest(text)


def test_recall_latest_rejects_without_temporal_qualifier() -> None:
    # Has a recall verb but no "上一条/最近一条" → must NOT auto-correct to count=1.
    assert not _looks_like_recall_latest("撤回那条")
    assert not _looks_like_recall_latest("delete that one")


# ---------------------------------------------------------------------------
# Inbound-context registry
# ---------------------------------------------------------------------------


def _make_ctx(mid: str, *, registered_at: float | None = None) -> _InboundContext:
    return _InboundContext(
        account_id="acct",
        target="group:1",
        inbound_message_id=mid,
        reply_to_bot_message_id=None,
        reply_targets=[],
        inbound_body="",
        registered_at=registered_at if registered_at is not None else time.time(),
    )


def test_register_and_lookup_round_trip() -> None:
    ad._inbound_ctx_store.clear()
    _register_inbound_context(_make_ctx("MID-1"))
    found = _lookup_inbound_context("MID-1")
    assert found is not None
    assert found.inbound_message_id == "MID-1"


def test_lookup_returns_none_after_ttl_elapses(monkeypatch) -> None:
    ad._inbound_ctx_store.clear()
    # Insert with a registered_at far in the past.
    stale = _make_ctx("OLD", registered_at=time.time() - ad._INBOUND_CTX_RETENTION_SECONDS - 1)
    ad._inbound_ctx_store["OLD"] = stale
    assert _lookup_inbound_context("OLD") is None
    # And the lookup evicted it.
    assert "OLD" not in ad._inbound_ctx_store


def test_register_evicts_when_over_max() -> None:
    ad._inbound_ctx_store.clear()
    cap = ad._INBOUND_CTX_MAX_ENTRIES
    base_now = 10_000.0
    for i in range(cap):
        ad._inbound_ctx_store[f"M{i}"] = _make_ctx(f"M{i}", registered_at=base_now + i)
    # Adding one more must evict the oldest.
    _register_inbound_context(_make_ctx("NEW", registered_at=base_now + cap))
    assert "NEW" in ad._inbound_ctx_store
    assert "M0" not in ad._inbound_ctx_store
    assert len(ad._inbound_ctx_store) == cap


# ---------------------------------------------------------------------------
# _read_account_settings — new fields
# ---------------------------------------------------------------------------


def _cfg(extra: dict | None = None):
    return SimpleNamespace(extra=extra or {})


def test_read_settings_parses_watch_regex_via_separator(monkeypatch) -> None:
    monkeypatch.delenv("INFOFLOW_WATCH_REGEX", raising=False)
    s = _read_account_settings(_cfg({"watch_regex": ["\\bdeploy\\b", "ship\\s+it"]}))
    assert s["watch_regex"] == ["\\bdeploy\\b", "ship\\s+it"]


def test_read_settings_parses_watch_regex_from_env(monkeypatch) -> None:
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX", "\\bdeploy\\b|||ship\\s+it")
    s = _read_account_settings(_cfg())
    assert s["watch_regex"] == ["\\bdeploy\\b", "ship\\s+it"]


def test_read_settings_parses_follow_up_window(monkeypatch) -> None:
    monkeypatch.setenv("INFOFLOW_FOLLOW_UP", "false")
    monkeypatch.setenv("INFOFLOW_FOLLOW_UP_WINDOW", "120")
    s = _read_account_settings(_cfg())
    assert s["follow_up"] is False
    assert s["follow_up_window"] == 120


def test_read_settings_invalid_follow_up_window_defaults(monkeypatch) -> None:
    monkeypatch.setenv("INFOFLOW_FOLLOW_UP_WINDOW", "not-a-number")
    s = _read_account_settings(_cfg())
    assert s["follow_up_window"] == 300


def test_read_settings_parses_groups_json(monkeypatch) -> None:
    monkeypatch.setenv(
        "INFOFLOW_GROUPS",
        json.dumps({"42": {"reply_mode": "ignore", "watch_regex": ["x"]}}),
    )
    s = _read_account_settings(_cfg())
    assert s["groups"]["42"]["reply_mode"] == "ignore"
    assert s["groups"]["42"]["watch_regex"] == ["x"]


def test_read_settings_ignores_malformed_groups_json(monkeypatch, caplog) -> None:
    monkeypatch.setenv("INFOFLOW_GROUPS", "{not-json")
    s = _read_account_settings(_cfg())
    assert s["groups"] == {}


def test_read_settings_picks_state_dir_from_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_STATE_DIR", str(tmp_path))
    s = _read_account_settings(_cfg())
    assert s["state_dir"] == str(tmp_path)


def test_read_settings_defaults_state_dir(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_STATE_DIR", raising=False)
    s = _read_account_settings(_cfg())
    assert s["state_dir"].endswith(".hermes/state") or s["state_dir"].endswith(".hermes\\state")


def test_read_settings_picks_robot_id_seed(monkeypatch) -> None:
    monkeypatch.setenv("INFOFLOW_ROBOT_ID", "12345")
    s = _read_account_settings(_cfg())
    assert s["robot_id"] == "12345"


# ---------------------------------------------------------------------------
# Target parsing (_parse_infoflow_target)
# ---------------------------------------------------------------------------


def test_parse_infoflow_target_group_prefix() -> None:
    result = _parse_infoflow_target("group:4507088")
    assert result == ("group:4507088", None)


def test_parse_infoflow_target_numeric_as_group() -> None:
    result = _parse_infoflow_target("4507088")
    assert result == ("group:4507088", None)


def test_parse_infoflow_target_uuapname_dm() -> None:
    result = _parse_infoflow_target("chengbo05")
    assert result == ("chengbo05", None)


def test_parse_infoflow_target_empty_string() -> None:
    assert _parse_infoflow_target("") is None


def test_parse_infoflow_target_whitespace_only() -> None:
    assert _parse_infoflow_target("   ") is None


def test_parse_infoflow_target_strips_whitespace() -> None:
    result = _parse_infoflow_target("  group:4507088  ")
    assert result == ("group:4507088", None)


def test_parse_infoflow_target_thread_always_none() -> None:
    """Infoflow does not use threads (unlike Telegram topics)."""
    for ref in ("group:4507088", "chengbo05", "12345"):
        result = _parse_infoflow_target(ref)
        assert result is not None
        assert result[1] is None


def test_parse_infoflow_target_account_id_dm() -> None:
    """accountId-style strings (non-numeric uuapNames) treated as DM."""
    result = _parse_infoflow_target("chengbo297")
    assert result == ("chengbo297", None)
