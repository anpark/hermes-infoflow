"""Tests for adapter-level helpers that don't require hermes-agent.

These cover the moving parts added in the OpenClaw parity pass:

* Recall-intent regex (``_looks_like_recall_intent`` / ``_looks_like_recall_latest``).
* Inbound-context registry (TTL + eviction).
* Env-driven settings parser (``_read_account_settings``) for the new
  watch_regex / follow_up / per-group / state_dir / robot_id fields.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_infoflow import recall as ad
from hermes_infoflow.adapter import (
    _format_group_status_admin_notice,
    _group_status_redirect_kind,
)
from hermes_infoflow.recall import (
    _InboundContext,
    _looks_like_recall_intent,
    _looks_like_recall_latest,
    _lookup_inbound_context,
    _register_inbound_context,
)
from hermes_infoflow.settings import (
    DEFAULT_API_HOST,
    DEFAULT_PORT,
    _check_requirements,
    _env_enablement,
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


def _clear_watch_regex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key == "INFOFLOW_WATCH_REGEX" or key.startswith("INFOFLOW_WATCH_REGEX_"):
            monkeypatch.delenv(key, raising=False)


def test_read_settings_default_port_without_env(monkeypatch) -> None:
    monkeypatch.delenv("INFOFLOW_PORT", raising=False)
    s = _read_account_settings(_cfg())
    assert s["port"] == DEFAULT_PORT
    assert DEFAULT_PORT == 26521


def test_read_settings_defaults_api_host_without_env(monkeypatch) -> None:
    monkeypatch.delenv("INFOFLOW_API_HOST", raising=False)
    s = _read_account_settings(_cfg())
    assert s["api_host"] == DEFAULT_API_HOST


def test_env_enablement_uses_default_api_host(monkeypatch) -> None:
    monkeypatch.delenv("INFOFLOW_API_HOST", raising=False)
    monkeypatch.setenv("INFOFLOW_APP_KEY", "k")
    monkeypatch.setenv("INFOFLOW_APP_SECRET", "s")
    monkeypatch.setenv("INFOFLOW_CHECK_TOKEN", "tok")
    monkeypatch.setenv("INFOFLOW_ENCODING_AES_KEY", "aes")

    seed = _env_enablement()

    assert seed is not None
    assert seed["api_host"] == DEFAULT_API_HOST


def test_env_enablement_includes_prefixed_watch_regex(monkeypatch) -> None:
    _clear_watch_regex_env(monkeypatch)
    monkeypatch.setenv("INFOFLOW_APP_KEY", "k")
    monkeypatch.setenv("INFOFLOW_APP_SECRET", "s")
    monkeypatch.setenv("INFOFLOW_CHECK_TOKEN", "tok")
    monkeypatch.setenv("INFOFLOW_ENCODING_AES_KEY", "aes")
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX", "\\bdeploy\\b")
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX_001_ios", "iphone|ios|crash")

    seed = _env_enablement()

    assert seed is not None
    assert seed["watch_regex"] == ["\\bdeploy\\b", "iphone|ios|crash"]


def test_requirements_do_not_require_api_host(monkeypatch) -> None:
    monkeypatch.delenv("INFOFLOW_API_HOST", raising=False)
    monkeypatch.setenv("INFOFLOW_APP_KEY", "k")
    monkeypatch.setenv("INFOFLOW_APP_SECRET", "s")
    monkeypatch.setenv("INFOFLOW_CHECK_TOKEN", "tok")
    monkeypatch.setenv("INFOFLOW_ENCODING_AES_KEY", "aes")
    assert _check_requirements() is True


def test_read_settings_parses_single_watch_mention_from_env(monkeypatch) -> None:
    monkeypatch.setenv("INFOFLOW_WATCH_MENTIONS", "chengbo05")
    s = _read_account_settings(_cfg())
    assert s["watch_mentions"] == ["chengbo05"]


def test_read_settings_parses_comma_watch_mentions_from_env(monkeypatch) -> None:
    monkeypatch.setenv("INFOFLOW_WATCH_MENTIONS", "chengbo05, alice, 12345")
    s = _read_account_settings(_cfg())
    assert s["watch_mentions"] == ["chengbo05", "alice", "12345"]


def test_read_settings_parses_watch_regex_from_config_list(monkeypatch) -> None:
    _clear_watch_regex_env(monkeypatch)
    s = _read_account_settings(_cfg({"watch_regex": ["\\bdeploy\\b", "ship\\s+it"]}))
    assert s["watch_regex"] == ["\\bdeploy\\b", "ship\\s+it"]


def test_read_settings_parses_single_watch_regex_from_env(monkeypatch) -> None:
    _clear_watch_regex_env(monkeypatch)
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX", "\\bdeploy\\b")
    s = _read_account_settings(_cfg())
    assert s["watch_regex"] == ["\\bdeploy\\b"]


def test_read_settings_parses_prefixed_watch_regex_env(monkeypatch) -> None:
    _clear_watch_regex_env(monkeypatch)
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX_ios", "iphone|ios|crash")
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX_icode", "^https://console\\.cloud")
    s = _read_account_settings(_cfg())
    assert s["watch_regex"] == ["^https://console\\.cloud", "iphone|ios|crash"]


def test_read_settings_merges_direct_and_prefixed_watch_regex_env(monkeypatch) -> None:
    _clear_watch_regex_env(monkeypatch)
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX", "\\bdeploy\\b")
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX_002_icode", "^https://console\\.cloud")
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX_001_ios", "iphone|ios|crash")
    s = _read_account_settings(_cfg())
    assert s["watch_regex"] == [
        "\\bdeploy\\b",
        "iphone|ios|crash",
        "^https://console\\.cloud",
    ]


def test_read_settings_sorts_numbered_watch_regex_env_naturally(monkeypatch) -> None:
    _clear_watch_regex_env(monkeypatch)
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX_10", "ten")
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX_2", "two")
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX_1", "one")
    s = _read_account_settings(_cfg())
    assert s["watch_regex"] == ["one", "two", "ten"]


def test_read_settings_watch_regex_env_prefix_overrides_config(monkeypatch) -> None:
    _clear_watch_regex_env(monkeypatch)
    monkeypatch.setenv("INFOFLOW_WATCH_REGEX_icode", "^https://console\\.cloud")
    s = _read_account_settings(_cfg({"watch_regex": ["config"]}))
    assert s["watch_regex"] == ["^https://console\\.cloud"]


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


# ---------------------------------------------------------------------------
# Group status suppression helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "⚡ Interrupting current task. I'll respond to your message shortly.",
        "⚠️ Gateway shutting down — Your current task will be interrupted.",
        (
            "⚠️ Gateway restarting — Your current task will be interrupted. "
            "Send any message after restart and I'll try to resume where you left off."
        ),
        "Gateway shutting down — Your current task will be interrupted.",
        "Gateway restarting — Your current task will be interrupted.",
        "  💾 Self-improvement review: Memory updated",
    ],
)
def test_group_status_redirect_kind_matches_hermes_runtime_messages(text: str) -> None:
    assert _group_status_redirect_kind(text)


def test_group_status_redirect_kind_does_not_match_normal_text() -> None:
    assert _group_status_redirect_kind("用户正常问：Memory updated 是什么意思？") == ""


def test_format_group_status_admin_notice_identifies_group() -> None:
    notice = _format_group_status_admin_notice(
        group_id="4507088",
        content="💾 Self-improvement review: Memory updated",
        status_kind="💾 Self-improvement review:",
    )
    assert "group:4507088" in notice
    assert "Memory updated" in notice


def test_parse_infoflow_target_account_id_dm() -> None:
    """accountId-style strings (non-numeric uuapNames) treated as DM."""
    result = _parse_infoflow_target("chengbo297")
    assert result == ("chengbo297", None)
