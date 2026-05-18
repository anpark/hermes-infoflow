"""Configuration reading, env-enablement, requirements checking, and validation.

These functions were extracted from :mod:`hermes_infoflow.adapter` so the
adapter can focus on the webhook / message lifecycle and plugin registration
can delegate config plumbing to a dedicated module.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults / constants
# ---------------------------------------------------------------------------

DEFAULT_PORT = 8646
DEFAULT_HOST = "0.0.0.0"
DEFAULT_WEBHOOK_PATH = "/webhook/infoflow"
MAX_MESSAGE_LENGTH = 2048  # matches OpenClaw textChunkLimit
DEFAULT_BODY_LIMIT_BYTES = 20 * 1024 * 1024
GROUP_TARGET_RE = re.compile(r"^(?:group:)?(\d+)$", re.IGNORECASE)
MAX_PREVIEW_LENGTH = 100  # matches openclaw reply-dispatcher truncatePreview()


# ---------------------------------------------------------------------------
# Settings reader
# ---------------------------------------------------------------------------


def _read_account_settings(config: Any) -> dict[str, Any]:
    """Merge env vars and ``config.extra`` into a flat settings dict.

    Env vars take precedence over config.extra entries — this matches the
    documented contract for other hermes platform plugins.
    """
    extra: dict[str, Any] = {}
    if config is not None:
        extra = dict(getattr(config, "extra", None) or {})

    def pick(env_name: str, key: str, default: Any = None) -> Any:
        env_val = os.getenv(env_name)
        if env_val not in (None, ""):
            return env_val
        if key in extra and extra[key] not in (None, ""):
            return extra[key]
        return default

    settings: dict[str, Any] = {
        "check_token": pick("INFOFLOW_CHECK_TOKEN", "check_token", "") or "",
        "encoding_aes_key": pick("INFOFLOW_ENCODING_AES_KEY", "encoding_aes_key", "") or "",
        "app_key": pick("INFOFLOW_APP_KEY", "app_key", "") or "",
        "app_secret": pick("INFOFLOW_APP_SECRET", "app_secret", "") or "",
        "api_host": pick("INFOFLOW_API_HOST", "api_host", "") or "",
        "robot_name": pick("INFOFLOW_ROBOT_NAME", "robot_name", "") or "",
        # robot_id is auto-discovered from inbound @-mention bodies on first
        # use; users normally don't set this explicitly, but if they do we
        # honour it as a seed value.
        "robot_id": pick("INFOFLOW_ROBOT_ID", "robot_id", "") or "",
        "host": pick("INFOFLOW_HOST", "host", DEFAULT_HOST) or DEFAULT_HOST,
        "webhook_path": pick("INFOFLOW_WEBHOOK_PATH", "webhook_path", DEFAULT_WEBHOOK_PATH)
        or DEFAULT_WEBHOOK_PATH,
        "connection_mode": (pick("INFOFLOW_CONNECTION_MODE", "connection_mode", "webhook") or "webhook").lower(),
        "reply_mode": (pick("INFOFLOW_REPLY_MODE", "reply_mode", "mention-and-watch") or "mention-and-watch"),
        "require_mention_raw": pick("INFOFLOW_REQUIRE_MENTION", "require_mention", "true"),
        "watch_mentions_raw": pick("INFOFLOW_WATCH_MENTIONS", "watch_mentions", ""),
        "watch_regex_raw": pick("INFOFLOW_WATCH_REGEX", "watch_regex", ""),
        "follow_up_raw": pick("INFOFLOW_FOLLOW_UP", "follow_up", "true"),
        "follow_up_window_raw": pick("INFOFLOW_FOLLOW_UP_WINDOW", "follow_up_window", "300"),
        "groups_raw": pick("INFOFLOW_GROUPS", "groups", None),
        "state_dir_raw": pick("HERMES_STATE_DIR", "state_dir", None),
    }

    # Numbers.
    raw_port = pick("INFOFLOW_PORT", "port", DEFAULT_PORT)
    try:
        settings["port"] = int(raw_port) if raw_port not in (None, "") else DEFAULT_PORT
    except ValueError:
        settings["port"] = DEFAULT_PORT
    raw_agent_id = pick("INFOFLOW_APP_AGENT_ID", "app_agent_id", None)
    settings["app_agent_id"] = int(raw_agent_id) if raw_agent_id not in (None, "") else None

    # Booleans.
    def _to_bool(raw: Any, *, default: bool) -> bool:
        if isinstance(raw, bool):
            return raw
        if raw is None or raw == "":
            return default
        return str(raw).strip().lower() not in ("0", "false", "no", "off")

    settings["require_mention"] = _to_bool(settings.pop("require_mention_raw"), default=True)
    settings["follow_up"] = _to_bool(settings.pop("follow_up_raw"), default=True)

    try:
        fuw = settings.pop("follow_up_window_raw")
        settings["follow_up_window"] = int(fuw) if fuw not in (None, "") else 300
    except (TypeError, ValueError):
        settings["follow_up_window"] = 300

    # CSV-ish (mentions).
    watch_raw = settings.pop("watch_mentions_raw") or ""
    if isinstance(watch_raw, list):
        settings["watch_mentions"] = [str(x).strip() for x in watch_raw if str(x).strip()]
    else:
        settings["watch_mentions"] = [s.strip() for s in str(watch_raw).split(",") if s.strip()]

    # CSV-ish (regex) — use a sentinel separator to allow commas inside patterns.
    # Convention: separate patterns with newline OR ``|||`` (3 pipes); single
    # pipes are commonly part of regex alternation so don't split on them.
    regex_raw = settings.pop("watch_regex_raw") or ""
    if isinstance(regex_raw, list):
        settings["watch_regex"] = [str(x).strip() for x in regex_raw if str(x).strip()]
    else:
        normalized = str(regex_raw).replace("|||", "\n")
        settings["watch_regex"] = [s.strip() for s in normalized.split("\n") if s.strip()]

    # Per-group overrides. Accept either an already-decoded dict (config.extra)
    # or a JSON string (env var).
    groups_raw = settings.pop("groups_raw")
    groups_parsed: dict[str, dict[str, Any]] = {}
    if isinstance(groups_raw, dict):
        for k, v in groups_raw.items():
            if isinstance(v, dict):
                groups_parsed[str(k)] = v
    elif isinstance(groups_raw, str) and groups_raw.strip():
        try:
            decoded = json.loads(groups_raw)
            if isinstance(decoded, dict):
                for k, v in decoded.items():
                    if isinstance(v, dict):
                        groups_parsed[str(k)] = v
        except (TypeError, ValueError) as exc:
            logger.warning("Ignoring malformed INFOFLOW_GROUPS JSON: %s", exc)
    settings["groups"] = groups_parsed

    # State dir for the persistent sent-messages SQLite store. We default to
    # ``~/.hermes/state/infoflow`` so cron sub-processes can read what the
    # live adapter wrote.
    state_dir = settings.pop("state_dir_raw")
    if state_dir:
        settings["state_dir"] = str(state_dir)
    else:
        settings["state_dir"] = str(Path.home() / ".hermes" / "state")

    return settings


# ---------------------------------------------------------------------------
# Env-enablement + requirements checking
# ---------------------------------------------------------------------------


def _env_enablement() -> dict | None:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Returning ``None`` skips auto-enable; otherwise the returned dict
    becomes ``PlatformConfig.extra`` (the special key ``home_channel``
    becomes the structured ``HomeChannel`` field).
    """
    api_host = os.getenv("INFOFLOW_API_HOST", "").strip()
    app_key = os.getenv("INFOFLOW_APP_KEY", "").strip()
    app_secret = os.getenv("INFOFLOW_APP_SECRET", "").strip()
    check_token = os.getenv("INFOFLOW_CHECK_TOKEN", "").strip()
    encoding_aes_key = os.getenv("INFOFLOW_ENCODING_AES_KEY", "").strip()
    if not (api_host and app_key and app_secret and check_token and encoding_aes_key):
        return None
    seed: dict[str, Any] = {
        "api_host": api_host,
        "app_key": app_key,
        "app_secret": app_secret,
        "check_token": check_token,
        "encoding_aes_key": encoding_aes_key,
    }
    if os.getenv("INFOFLOW_APP_AGENT_ID", "").strip():
        try:
            seed["app_agent_id"] = int(os.environ["INFOFLOW_APP_AGENT_ID"].strip())
        except ValueError:
            pass
    for env_key, settings_key in (
        ("INFOFLOW_ROBOT_NAME", "robot_name"),
        ("INFOFLOW_ROBOT_ID", "robot_id"),
        ("INFOFLOW_PORT", "port"),
        ("INFOFLOW_HOST", "host"),
        ("INFOFLOW_WEBHOOK_PATH", "webhook_path"),
        ("INFOFLOW_REPLY_MODE", "reply_mode"),
        ("INFOFLOW_REQUIRE_MENTION", "require_mention"),
        ("INFOFLOW_WATCH_MENTIONS", "watch_mentions"),
        ("INFOFLOW_WATCH_REGEX", "watch_regex"),
        ("INFOFLOW_FOLLOW_UP", "follow_up"),
        ("INFOFLOW_FOLLOW_UP_WINDOW", "follow_up_window"),
        ("INFOFLOW_GROUPS", "groups"),
        ("INFOFLOW_CONNECTION_MODE", "connection_mode"),
        ("HERMES_STATE_DIR", "state_dir"),
    ):
        val = os.getenv(env_key, "").strip()
        if val:
            seed[settings_key] = val
    home = os.getenv("INFOFLOW_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("INFOFLOW_HOME_CHANNEL_NAME", "").strip() or home,
        }
    return seed


def _check_requirements() -> bool:
    """Return True iff the minimum env vars are present.

    hermes-agent calls this during gateway start; missing env vars surface
    a clear "platform not configured" message instead of a crash.
    """
    required = (
        "INFOFLOW_API_HOST",
        "INFOFLOW_APP_KEY",
        "INFOFLOW_APP_SECRET",
        "INFOFLOW_CHECK_TOKEN",
        "INFOFLOW_ENCODING_AES_KEY",
    )
    return all(os.getenv(name) for name in required)


def _validate_config(config: Any) -> bool:
    settings = _read_account_settings(config)
    for key in ("api_host", "app_key", "app_secret", "check_token", "encoding_aes_key"):
        if not settings.get(key):
            return False
    return True


def _is_connected(config: Any) -> bool:
    return _validate_config(config)


def _interactive_setup() -> None:  # pragma: no cover - manual flow
    """``hermes gateway setup`` flow stub.

    The real flow lives upstream in ``hermes_cli/setup.py``; this hook just
    prints clear guidance pointing at the env vars to set.
    """
    print(
        "Set these env vars (or hermes config set):\n"
        "  INFOFLOW_API_HOST=https://api.infoflow.example.com\n"
        "  INFOFLOW_APP_KEY=<your appKey>\n"
        "  INFOFLOW_APP_SECRET=<your appSecret>\n"
        "  INFOFLOW_CHECK_TOKEN=<your checkToken>\n"
        "  INFOFLOW_ENCODING_AES_KEY=<your EncodingAESKey>\n"
        "Optional: INFOFLOW_APP_AGENT_ID, INFOFLOW_ROBOT_NAME, INFOFLOW_PORT, "
        "INFOFLOW_HOME_CHANNEL"
    )


# ---------------------------------------------------------------------------
# Target parsing for send_message tool integration
# ---------------------------------------------------------------------------


def _parse_infoflow_target(
    target_ref: str,
) -> tuple[str, str | None] | None:
    """Parse an infoflow target reference into ``(chat_id, thread_id)``.

    This is registered as ``PlatformEntry.target_parse_fn`` so that
    ``tools/send_message_tool._parse_target_ref`` can recognise infoflow
    targets on the first pass (without consulting channel_directory).

    Recognised formats::

        group:<id>        → ("group:<id>", None)
        <id> (numeric)    → ("group:<id>", None)
        <uuapName>        → ("<uuapName>", None)

    Returns ``None`` for empty/whitespace-only strings to decline parsing.
    Infoflow does not use threads (unlike Telegram topics).
    """
    target_ref = target_ref.strip()
    if not target_ref:
        return None
    # Already in canonical form: group:4507088
    if target_ref.startswith("group:"):
        return (target_ref, None)
    # Pure numeric → treat as group ID (matches _normalize_chat_id logic)
    if target_ref.isdigit():
        return (f"group:{target_ref}", None)
    # Anything else → uuapName (DM)
    return (target_ref, None)


__all__ = [
    "DEFAULT_BODY_LIMIT_BYTES",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_WEBHOOK_PATH",
    "GROUP_TARGET_RE",
    "MAX_MESSAGE_LENGTH",
    "MAX_PREVIEW_LENGTH",
    "_check_requirements",
    "_env_enablement",
    "_interactive_setup",
    "_is_connected",
    "_parse_infoflow_target",
    "_read_account_settings",
    "_validate_config",
]
