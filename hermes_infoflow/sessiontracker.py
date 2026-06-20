"""Session Tracker Web UI — CLI-style live view for one Infoflow chat target."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .api import InfoflowAccountAPI, InfoflowAPIError, get_user_info_by_code
from .dashboard import (
    SessionEvent,
    SessionTracker,
    TRACKER_SESSION_PREFIX,
    normalize_chat_id,
    sessiontracker_enabled,
    sessiontracker_full_user_message_enabled,
)
from .settings import DEFAULT_API_HOST, infoflow_admin_users_from_env
from .sse import (
    SSE_HEARTBEAT,
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    SSE_RESPONSE_HEADERS,
    write_sse,
)
from .sessiontracker_terminal import (
    close_terminal_session,
    create_terminal_session,
    list_terminal_sessions,
    read_terminal_output,
    request_is_localhost,
    resize_terminal_session,
    run_terminal_websocket,
    sessiontracker_terminal_cwd,
    sessiontracker_terminal_enabled,
    sessiontracker_terminal_localhost_only,
    sessiontracker_terminal_max_per_admin,
    sessiontracker_terminal_retention_seconds,
    write_terminal_input,
)

logger = logging.getLogger(__name__)

_SSE_RESPONSE_HEADERS = SSE_RESPONSE_HEADERS
_SESSIONTRACKER_STATIC_ROOT = Path(__file__).resolve().parent / "static" / "sessiontracker"

TERMINAL_EVENT_KINDS = frozenset({
    "display.user",
    "display.tool_line",
    "display.tool_progress",
    "display.hermes",
    "display.hermes_stream",
    "display.thinking_stream",
    "display.status",
    "display.interim",
    "outbound.infoflow",
    "tool.end",
})

GROUP_CHAT_TYPES = frozenset({2, 3, 5, 6})
DM_CHAT_TYPES = frozenset({1, 7})
SUPPORTED_CHAT_TYPES = GROUP_CHAT_TYPES | DM_CHAT_TYPES

_PROGRESS_LINE_RE = re.compile(r"^[┊\s]*[🔍⚙️💻🌐📁📝🧠✨]")

# OAuth code is one-time; cache successful code -> user_id for resolve polling / SSE.
_CODE_USER_CACHE_TTL_SECONDS = int(os.getenv("HERMES_INFOFLOW_CODE_CACHE_TTL", "86400")) if os.getenv("HERMES_INFOFLOW_CODE_CACHE_TTL", "").isdigit() else 86400
_CODE_USER_CACHE_MAX = int(os.getenv("HERMES_INFOFLOW_CODE_CACHE_MAX", "1024")) if os.getenv("HERMES_INFOFLOW_CODE_CACHE_MAX", "").isdigit() else 1024
_code_user_cache: dict[str, tuple[str, float]] = {}
_code_user_cache_lock = asyncio.Lock()


def _code_cache_key(code: str, account: InfoflowAccountAPI | None = None) -> str:
    """Hash OAuth code (and optional account) for in-memory cache lookup."""
    normalized = code.strip()
    parts = [normalized]
    if account is not None:
        parts.append(account.app_key)
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return digest


def _prune_code_user_cache(now: float) -> None:
    expired = [k for k, (_, exp) in _code_user_cache.items() if exp <= now]
    for k in expired:
        del _code_user_cache[k]
    if len(_code_user_cache) <= _CODE_USER_CACHE_MAX:
        return
    by_expiry = sorted(_code_user_cache.items(), key=lambda item: item[1][1])
    for k, _ in by_expiry[: len(_code_user_cache) - _CODE_USER_CACHE_MAX]:
        del _code_user_cache[k]


async def resolve_user_id_by_code_cached(
    account: InfoflowAccountAPI,
    code: str,
    *,
    http_session: Any = None,
) -> str:
    """Resolve OAuth code to uuap, caching successful lookups in-process."""
    stripped = code.strip()
    if not stripped:
        raise ValueError("code is required for private chatType=1/7")

    cache_key = _code_cache_key(stripped, account)
    now = time.monotonic()

    async with _code_user_cache_lock:
        entry = _code_user_cache.get(cache_key)
        if entry is not None:
            user_id, expires_at = entry
            if expires_at > now:
                return user_id
            del _code_user_cache[cache_key]

    user_id = await get_user_info_by_code(
        account, stripped, session=http_session,
    )

    async with _code_user_cache_lock:
        _code_user_cache[cache_key] = (
            user_id,
            now + _CODE_USER_CACHE_TTL_SECONDS,
        )
        _prune_code_user_cache(now)

    return user_id


def format_terminal_line(
    event: SessionEvent,
    *,
    show_full_user_message: bool = False,
) -> dict[str, Any] | None:
    """Map a tracker event to a terminal render unit for the Web UI."""
    kind = event.kind
    payload = event.payload or {}

    if kind == "display.tool_line":
        line = payload.get("line") or ""
        return {"line_kind": "tool", "text": str(line)}

    if kind == "display.user":
        text = (
            payload.get("full_text")
            if show_full_user_message and payload.get("full_text") is not None
            else payload.get("text")
        ) or ""
        return {"line_kind": "user", "text": str(text)}

    if kind == "display.hermes":
        text = payload.get("text") or ""
        return {"line_kind": "hermes", "text": str(text), "final": True}

    if kind == "display.hermes_stream":
        text = payload.get("text") or ""
        stream_id = payload.get("stream_id") or ""
        return {
            "line_kind": "hermes",
            "text": str(text),
            "stream_id": str(stream_id),
            "final": bool(payload.get("final")),
        }

    if kind == "display.thinking_stream":
        text = payload.get("text") or ""
        stream_id = payload.get("stream_id") or ""
        return {
            "line_kind": "thinking",
            "text": str(text),
            "stream_id": str(stream_id),
            "final": bool(payload.get("final")),
        }

    if kind == "display.interim":
        text = payload.get("text") or ""
        return {"line_kind": "interim", "text": str(text)}

    if kind == "display.tool_progress":
        text = payload.get("line") or payload.get("text") or ""
        return {
            "line_kind": "tool_progress",
            "text": str(text),
            "tool_call_id": str(payload.get("tool_call_id") or ""),
            "stage": str(payload.get("stage") or ""),
        }

    if kind == "display.status":
        return {"line_kind": "status", "text": str(payload.get("line") or "")}

    if kind == "outbound.infoflow":
        if payload.get("suppressed_group_status"):
            preview = payload.get("preview") or payload.get("chars") or ""
            return {"line_kind": "status", "text": str(preview)}
        if not payload.get("is_progress_hint"):
            return None
        preview = payload.get("preview") or payload.get("chars")
        return {"line_kind": "tool", "text": f"┊ {preview}" if preview else "┊ …"}

    if kind == "tool.end" and not payload.get("_skip_fallback"):
        name = payload.get("tool_name") or "tool"
        dur = payload.get("duration_ms")
        dur_s = f" {float(dur) / 1000.0:.1f}s" if dur else ""
        return {"line_kind": "tool", "text": f"┊ ⚙️ {name}{dur_s}"}

    return None


def count_terminal_lines(tracker: SessionTracker, session_id: str) -> int:
    """Count events that render as terminal lines (for session pick ranking)."""
    return len(collect_terminal_blocks(tracker, session_id, cursor=0))


def collect_terminal_blocks(
    tracker: SessionTracker,
    session_id: str,
    *,
    cursor: int = 0,
    show_full_user_message: bool = False,
) -> list[dict[str, Any]]:
    """Build renderable terminal blocks for a session snapshot."""
    blocks: list[dict[str, Any]] = []
    for ev in tracker.snapshot(session_id, cursor=cursor):
        if ev.kind not in TERMINAL_EVENT_KINDS:
            continue
        block = event_to_terminal_dict(
            ev,
            show_full_user_message=show_full_user_message,
        )
        if block is not None:
            blocks.append(block)
    return blocks


def event_to_terminal_dict(
    event: SessionEvent,
    *,
    show_full_user_message: bool = False,
) -> dict[str, Any] | None:
    block = format_terminal_line(
        event,
        show_full_user_message=show_full_user_message,
    )
    if block is None:
        return None
    return {
        "seq": event.seq,
        "ts": event.ts,
        "kind": event.kind,
        **block,
    }


async def resolve_target(
    tracker: SessionTracker,
    *,
    chat_type: int,
    chat_id: str,
    code: str,
    account: InfoflowAccountAPI | None = None,
    http_session: Any = None,
) -> dict[str, Any]:
    """Resolve URL query params to canonical chat_id and optional session_id."""
    raw_chat_id = (chat_id or "").strip()
    if chat_type in GROUP_CHAT_TYPES:
        if not raw_chat_id:
            raise ValueError("chatId is required for group chatType=2/3/5/6")
        canonical = f"group:{raw_chat_id}"
        label = f"群 {raw_chat_id}"
    elif chat_type in DM_CHAT_TYPES:
        if not (code or "").strip():
            raise ValueError("code is required for private chatType=1/7")
        if account is None:
            raise ValueError("Infoflow API account is required for private chatType=1/7")
        user_id = await resolve_user_id_by_code_cached(
            account, code, http_session=http_session,
        )
        canonical = user_id
        label = f"私聊 {user_id}"
    else:
        raise ValueError(f"unsupported chatType={chat_type}")

    tracker_session_id = tracker.lookup_tracker_session_id(canonical)
    hermes_session_id = tracker.latest_hermes_session_id(canonical)
    status = "waiting"
    meta = None
    terminal_lines = 0
    if tracker_session_id:
        if tracker_session_id.startswith("pending:"):
            status = "waiting"
        else:
            meta = tracker.get_meta(tracker_session_id)
            hermes_meta = tracker.get_meta(hermes_session_id) if hermes_session_id else None
            if hermes_meta is not None:
                # Pass through hermes_meta.status (active | idle | ended) so the
                # frontend 'ended' branch (e.g. empty-hint for ended sessions) works.
                status = hermes_meta.status or "idle"
            elif not hermes_session_id:
                status = "waiting"
            elif meta is not None:
                status = "idle"
            else:
                status = "waiting"
            terminal_lines = count_terminal_lines(tracker, tracker_session_id)

    return {
        "label": label,
        "canonical_chat_id": canonical,
        "session_id": tracker_session_id or "",
        "tracker_session_id": tracker_session_id or "",
        "hermes_session_id": hermes_session_id,
        "status": status,
        "chat_type": chat_type,
        "user_id": (meta.user_id if meta else "") or "",
        "terminal_lines": terminal_lines,
    }


async def canonical_for_stream_access(
    tracker: SessionTracker,
    *,
    session_id: str,
    chat_type: int,
    chat_id: str,
    code: str,
    account: InfoflowAccountAPI | None = None,
) -> str:
    """Resolve DM/group target for stream/history without re-requiring a fresh OAuth code.

    Infoflow ``code`` in the page URL is the authority for private chats. The
    ``session_id`` may select a tracker bucket, but never proves DM identity.
    """
    if chat_type in GROUP_CHAT_TYPES:
        raw = (chat_id or "").strip()
        if not raw:
            raise ValueError("chatId is required for group chatType=2/3/5/6")
        return f"group:{raw}"

    if chat_type not in DM_CHAT_TYPES:
        raise ValueError(f"unsupported chatType={chat_type}")

    if not (code or "").strip():
        raise ValueError("code is required for private chatType=1/7")
    if account is None:
        raise ValueError("Infoflow API account is required for private chatType=1/7")
    return await resolve_user_id_by_code_cached(account, code)


def session_matches_target(
    tracker: SessionTracker,
    session_id: str,
    canonical_chat_id: str,
) -> bool:
    """Return whether *session_id* belongs to the resolved *canonical_chat_id*."""
    if not session_id or not canonical_chat_id:
        return False
    tracker_sid = tracker.tracker_session_id(canonical_chat_id)
    if session_id == tracker_sid:
        return True
    if session_id.startswith(TRACKER_SESSION_PREFIX):
        return (
            normalize_chat_id(tracker.canonical_from_tracker_session_id(session_id))
            == normalize_chat_id(canonical_chat_id)
        )
    if session_id == f"pending:{canonical_chat_id}":
        return True
    if tracker._chat_to_session.get(canonical_chat_id) == session_id:  # noqa: SLF001
        return True
    meta = tracker.get_meta(session_id)
    if meta is not None and tracker.meta_matches_canonical(meta, canonical_chat_id):
        return True
    for ev in tracker.snapshot(session_id, cursor=0):
        cid = normalize_chat_id((ev.payload or {}).get("chat_id") or "")
        if cid == normalize_chat_id(canonical_chat_id):
            return True
    return False


def _parse_cursor(raw: str) -> int:
    try:
        return max(0, int(raw or "0"))
    except ValueError as exc:
        raise ValueError("cursor must be a non-negative integer") from exc


def _parse_terminal_dimension(raw: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(raw or default)
    except ValueError:
        value = default
    return max(min_value, min(value, max_value))


def _parse_terminal_wait(raw: str) -> float:
    try:
        value = float(raw or "20")
    except ValueError:
        value = 20.0
    return max(0.0, min(value, 25.0))


def _read_infoflow_account() -> InfoflowAccountAPI:
    api_host = os.getenv("INFOFLOW_API_HOST", "").strip() or DEFAULT_API_HOST
    app_key = os.getenv("INFOFLOW_APP_KEY", "").strip()
    app_secret = os.getenv("INFOFLOW_APP_SECRET", "").strip()
    agent_raw = os.getenv("INFOFLOW_APP_AGENT_ID", "").strip()
    if not all((app_key, app_secret, agent_raw)):
        raise ValueError(
            "INFOFLOW_APP_KEY, INFOFLOW_APP_SECRET, INFOFLOW_APP_AGENT_ID are required"
        )
    return InfoflowAccountAPI(
        api_host=api_host,
        app_key=app_key,
        app_secret=app_secret,
        app_agent_id=int(agent_raw),
    )


async def _viewer_can_see_full_user_message(
    *,
    code: str,
    account: InfoflowAccountAPI | None,
) -> bool:
    if not sessiontracker_full_user_message_enabled():
        return False
    return bool(await _viewer_admin_user_id(code=code, account=account))


async def _viewer_admin_user_id(
    *,
    code: str,
    account: InfoflowAccountAPI | None,
) -> str:
    admins = infoflow_admin_users_from_env()
    if not admins or not (code or "").strip() or account is None:
        return ""
    try:
        viewer_user_id = await resolve_user_id_by_code_cached(account, code)
    except (InfoflowAPIError, ValueError):
        return ""
    normalized = viewer_user_id.strip().lower()
    return viewer_user_id if normalized in admins else ""


async def _viewer_is_admin(
    *,
    code: str,
    account: InfoflowAccountAPI | None,
) -> bool:
    return bool(await _viewer_admin_user_id(code=code, account=account))


def _terminal_block_reason(
    request: Any,
    *,
    chat_type: int,
    viewer_is_admin: bool,
) -> str | None:
    if not sessiontracker_terminal_enabled():
        return "disabled"
    if chat_type not in DM_CHAT_TYPES:
        return "not_private_chat"
    if not viewer_is_admin:
        return "not_admin"
    if sessiontracker_terminal_localhost_only() and not request_is_localhost(request):
        return "localhost_only"
    return None


def _terminal_log_context(request: Any, *, chat_type: int) -> dict[str, Any]:
    return {
        "remote": getattr(request, "remote", "") or "",
        "chat_type": chat_type,
        "user_agent": request.headers.get("User-Agent", ""),
    }


def _log_terminal_denied(
    request: Any,
    *,
    action: str,
    chat_type: int,
    reason: str,
) -> None:
    ctx = _terminal_log_context(request, chat_type=chat_type)
    logger.warning(
        "[infoflow] sessiontracker terminal deny action=%s reason=%s remote=%s "
        "chat_type=%s user_agent=%r",
        action,
        reason,
        ctx["remote"],
        ctx["chat_type"],
        ctx["user_agent"],
    )


def _terminal_error_text(reason: str) -> str:
    if reason == "disabled":
        return "terminal disabled"
    if reason == "not_private_chat":
        return "terminal is only available for private Session Tracker pages"
    if reason == "localhost_only":
        return "terminal: localhost only"
    return "terminal requires admin viewer code"


def _account_for_sessiontracker_request(
    chat_type: int,
    code: str,
) -> tuple[InfoflowAccountAPI | None, str | None]:
    if chat_type in DM_CHAT_TYPES:
        try:
            return _read_infoflow_account(), None
        except ValueError as exc:
            return None, str(exc)
    if (
        (code or "").strip()
        and (
            sessiontracker_full_user_message_enabled()
            or sessiontracker_terminal_enabled()
        )
        and infoflow_admin_users_from_env()
    ):
        try:
            return _read_infoflow_account(), None
        except ValueError:
            return None, None
    return None, None


def _parse_query(request: Any) -> tuple[int, str, str]:
    q = request.rel_url.query
    try:
        chat_type = int(q.get("chatType", "") or "0")
    except ValueError as exc:
        raise ValueError("chatType must be an integer") from exc
    chat_id = str(q.get("chatId", "") or "")
    code = str(q.get("code", "") or "")
    return chat_type, chat_id, code


def _require_sessiontracker_params(handler: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapped(request: Any) -> Any:
        from aiohttp import web

        try:
            chat_type, chat_id, code = _parse_query(request)
        except ValueError as exc:
            return web.Response(status=400, text=str(exc))
        if chat_type in DM_CHAT_TYPES and not code.strip():
            return web.Response(status=400, text="code is required for private chatType=1/7")
        if chat_type in GROUP_CHAT_TYPES and not chat_id.strip():
            return web.Response(status=400, text="chatId is required for group chatType=2/3/5/6")
        if chat_type not in SUPPORTED_CHAT_TYPES:
            return web.Response(status=400, text="chatType must be one of 1,2,3,5,6,7")
        return await handler(request, chat_type=chat_type, chat_id=chat_id, code=code)
    return wrapped


def _static_asset_path(rel_path: str) -> Path | None:
    root = _SESSIONTRACKER_STATIC_ROOT.resolve()
    path = (root / rel_path).resolve()
    if path == root or root not in path.parents or not path.is_file():
        return None
    return path


async def _require_terminal_admin_user_id(
    request: Any,
    *,
    chat_type: int,
    code: str,
    account: InfoflowAccountAPI | None,
) -> tuple[str, str | None]:
    viewer_user_id = await _viewer_admin_user_id(code=code, account=account)
    if not viewer_user_id:
        reason = _terminal_block_reason(
            request,
            chat_type=chat_type,
            viewer_is_admin=False,
        )
        return "", _terminal_error_text(reason or "not_admin")
    reason = _terminal_block_reason(
        request,
        chat_type=chat_type,
        viewer_is_admin=True,
    )
    if reason:
        return "", _terminal_error_text(reason)
    return viewer_user_id, None


_SESSIONTRACKER_CSS = """
@font-face { font-family: 'MesloLGM NF'; src: url('/webhook/infoflow/sessiontracker/static/fonts/meslo/meslolgm-nf-regular.woff2') format('woff2'); font-weight: 400; font-style: normal; font-display: swap; }
@font-face { font-family: 'MesloLGM NF'; src: url('/webhook/infoflow/sessiontracker/static/fonts/meslo/meslolgm-nf-bold.woff2') format('woff2'); font-weight: 700; font-style: normal; font-display: swap; }
@font-face { font-family: 'MesloLGM NF'; src: url('/webhook/infoflow/sessiontracker/static/fonts/meslo/meslolgm-nf-italic.woff2') format('woff2'); font-weight: 400; font-style: italic; font-display: swap; }
@font-face { font-family: 'MesloLGM NF'; src: url('/webhook/infoflow/sessiontracker/static/fonts/meslo/meslolgm-nf-bold-italic.woff2') format('woff2'); font-weight: 700; font-style: italic; font-display: swap; }
:root { --bg: #0c0c0c; --text: #d4d4d4; --muted: #6a737d; --accent: #58a6ff;
  --user: #f0b67f; --hermes-border: #3d5a80; --ok: #3dd68c; --interim: #b48ead; }
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { font-family: 'Microsoft YaHei', '微软雅黑', sans-serif; background: var(--bg);
  color: var(--text); font-size: 13px; line-height: 1.55; }
header { padding: 10px 14px; border-bottom: 1px solid #222; background: #111; flex-shrink: 0; }
h1 { margin: 0; font-size: 14px; font-weight: 600; }
#meta-line { color: var(--muted); font-size: 12px; margin-top: 4px; }
.header-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.tabs { display: none; align-items: center; gap: 6px; }
.tabs.visible { display: flex; }
.tab-button { height: 28px; border: 1px solid #30363d; border-radius: 4px; background: #161b22;
  color: #8b949e; padding: 0 10px; font: inherit; cursor: pointer; }
.tab-button.active { border-color: #58a6ff; color: #fff; background: #1f6feb; }
.panel { flex: 1; min-height: 0; display: none; }
.panel.active { display: flex; flex-direction: column; }
#viewport { position: relative; flex: 1; min-height: 0; overflow: hidden; flex-direction: column; }
#terminal-wrap { flex: 1; overflow-y: auto; padding: 12px 14px 48px; }
.user-line { color: var(--user); margin: 14px 0 6px; white-space: pre-wrap; word-break: break-word; }
.user-line .bullet { color: var(--user); font-weight: 600; margin-right: 6px; }
.tool-line { color: #9cdcfe; white-space: pre-wrap; word-break: break-word; margin: 2px 0; }
.tool-progress { color: #9cdcfe; opacity: 0.7; white-space: pre-wrap; word-break: break-word;
  margin: 2px 0; }
.tool-progress.is-done { opacity: 1; }
.hermes-box { border: 1px solid var(--hermes-border); border-radius: 4px; margin: 10px 0;
  padding: 8px 10px; background: #141820; }
.hermes-box.streaming { border-color: #4f7cb0; }
.hermes-title { color: #7eb8ff; font-size: 12px; margin-bottom: 6px; }
.hermes-body { white-space: pre-wrap; word-break: break-word; }
.hermes-body .caret { color: #7eb8ff; opacity: 0.6; animation: blink 1s steps(2, start) infinite; }
.thinking-box { border-left: 2px solid #56616f; border-radius: 4px; margin: 6px 0;
  padding: 6px 10px; background: #101318; color: #8b949e; }
.thinking-box.streaming { border-left-color: #7a8491; }
.thinking-title { color: #8b949e; font-size: 12px; margin-bottom: 4px; }
.thinking-body { white-space: pre-wrap; word-break: break-word; }
.thinking-body .caret { color: #8b949e; opacity: 0.6; animation: blink 1s steps(2, start) infinite; }
@keyframes blink { to { visibility: hidden; } }
.interim-line { color: var(--interim); font-style: italic; margin: 6px 0; white-space: pre-wrap;
  word-break: break-word; }
.status-line { color: var(--muted); margin: 8px 0 4px; font-size: 12px; }
.divider { color: var(--muted); margin: 10px 0; }
#scroll-bottom { display: none; position: fixed; right: 20px; bottom: 20px; width: 44px;
  height: 44px; border-radius: 50%; border: 1px solid #444; background: #1f6feb; color: #fff;
  font-size: 20px; cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,.4); z-index: 10; }
#scroll-bottom.visible { display: block; }
.empty { color: var(--muted); padding: 24px; text-align: center; }
body.layout-col { display: flex; flex-direction: column; height: 100vh; }
#admin-terminal-panel { background: #0a0a0a; }
#terminal-toolbar { display: flex; align-items: center; gap: 8px; min-height: 38px; padding: 6px 10px;
  border-bottom: 1px solid #222; background: #101010; flex-shrink: 0; }
#terminal-session-select { flex: 0 1 170px; min-width: 86px; max-width: 190px; height: 26px;
  border: 1px solid #30363d; border-radius: 4px; background: #161b22; color: #d4d4d4;
  padding: 0 6px; font: inherit; }
#terminal-status { color: var(--muted); font-size: 12px; flex: 1 1 72px; min-width: 24px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.terminal-button { height: 26px; border: 1px solid #30363d; border-radius: 4px; background: #161b22;
  color: #d4d4d4; padding: 0 9px; font: inherit; cursor: pointer; }
.terminal-button.icon { width: 28px; padding: 0; font-size: 14px; line-height: 1; }
.terminal-button:disabled { opacity: 0.45; cursor: default; }
#xterm-host { flex: 1; min-height: 0; padding: 8px; position: relative; }
#terminal-fallback { flex: 1; min-height: 0; margin: 0; padding: 10px 12px; overflow: auto;
  background: #050505; color: #d4d4d4; white-space: pre-wrap; outline: none; }
#terminal-fallback.hidden, #xterm-host.hidden { display: none; }
#terminal-vkeys { display: flex; align-items: center; gap: 4px; min-height: 36px; padding: 5px 8px;
  border-top: 1px solid #222; background: #101010; flex-shrink: 0; flex-wrap: wrap; }
.vkey { height: 30px; min-width: 36px; border: 1px solid #30363d; border-radius: 4px;
  background: #161b22; color: #d4d4d4; padding: 0 6px; font-size: 12px; cursor: pointer;
  -webkit-user-select: none; user-select: none; touch-action: manipulation; }
.vkey:active { background: #264f78; }
.vkey.modifier { border-color: #58a6ff; color: #58a6ff; }
.vkey.modifier.active { background: #1f3a5f; border-color: #79c0ff; color: #79c0ff; }
.vkey.wide { min-width: 48px; }
.xterm { height: 100%; }
/* xterm viewport: smooth touch scrolling on mobile */
#xterm-host .xterm-viewport {
  -webkit-overflow-scrolling: touch;
  overscroll-behavior: contain;
  touch-action: pan-y;
  /* Hide the native scrollbar — we draw a custom one below */
  scrollbar-width: none; /* Firefox */
  -ms-overflow-style: none; /* IE10+ */
}
/* Hide native xterm scrollbar (we use overlay) */
#xterm-host .xterm-viewport::-webkit-scrollbar { width: 0; height: 0; display: none; }
#xterm-host .xterm-viewport { scrollbar-width: none; -ms-overflow-style: none; }
/* Overlay scrollbar — works in both viewport-overflow and tmux modes */
#term-scrollbar {
  position: absolute; top: 0; right: 0; bottom: 0;
  width: 16px; z-index: 20; pointer-events: none;
}
#term-scrollbar-thumb {
  position: absolute; right: 2px; width: 6px;
  border-radius: 3px; min-height: 30px;
  background: rgba(160,170,190,0.45);
  pointer-events: auto; cursor: pointer; touch-action: none;
}
#term-scrollbar-thumb:hover, #term-scrollbar-thumb.active {
  background: rgba(140,155,190,0.8);
}
#terminal-ctx-menu {
  position: fixed; z-index: 10000;
  background: #1e2030; border: 1px solid #444c6a; border-radius: 8px;
  padding: 4px; min-width: 200px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.7);
  -webkit-backdrop-filter: none; backdrop-filter: none;
  opacity: 1;
}
#terminal-ctx-menu button {
  display: flex; align-items: center; gap: 8px; width: 100%;
  background: none; border: none; cursor: pointer; color: #e0e0e0;
  font-family: 'Microsoft YaHei', '微软雅黑', sans-serif; font-size: 13px;
  padding: 9px 12px; border-radius: 4px; text-align: left;
}
#terminal-ctx-menu button:hover, #terminal-ctx-menu button:active { background: #2a3558; }
#terminal-ctx-menu .ctx-kbd { margin-left: auto; color: #888; font-size: 10px; }
#terminal-ctx-menu .ctx-divider { height: 1px; background: #3a3a4a; margin: 3px 6px; }
/* Paste-trap: hidden off-screen */
#terminal-paste-trap {
  position: fixed; top: -9999px; left: -9999px;
  width: 1px; height: 1px; opacity: 0; pointer-events: none;
}
/* Mobile input trap: overlays xterm for keyboard capture on mobile */
.mobile-input-trap {
  display: none; position: absolute; left: 8px; bottom: 48px;
  width: 2px; height: 24px; opacity: 0.01; font-size: 16px;
  resize: none; border: none; outline: none; padding: 0;
  background: transparent; color: transparent; caret-color: transparent;
  z-index: 6; overflow: hidden; -webkit-user-select: text;
}
.mobile-terminal .mobile-input-trap { display: block; }
/* Paste-awaiting mode: enlarge + make visible so user can long-press → system Paste */
.mobile-input-trap.paste-ready {
  width: 80% !important; height: 48px !important;
  left: 10% !important; bottom: 56px !important;
  opacity: 1 !important; color: #d4d4d4 !important;
  caret-color: #58a6ff !important; background: #161b22 !important;
  border: 1px solid #444c6a !important; border-radius: 6px;
  padding: 8px 10px !important; font-size: 15px;
  z-index: 1002 !important;
}
.mobile-input-trap.paste-ready::placeholder { color: #6a7080; }
/* When keyboard is open on mobile, float vkeys above keyboard using visualViewport */
.mobile-terminal.kbd-open #terminal-vkeys {
  position: fixed; left: 0; right: 0;
  z-index: 1001; border-top: 2px solid #333;
  padding-bottom: max(5px, env(safe-area-inset-bottom));
}
/* JS will set bottom/top dynamically based on visualViewport */
/* Mobile copy FAB */
#terminal-mobile-copy-fab {
  position: absolute; right: 10px; top: 48px; z-index: 7;
  height: 32px; padding: 0 10px;
  border: 1px solid #30363d; border-radius: 4px;
  background: #161b22; color: #d4d4d4; font-size: 12px;
  cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,0.3);
}
/* Mobile copy mode overlay */
#terminal-mobile-copy-layer {
  display: none; position: absolute; inset: 0; z-index: 8;
  flex-direction: column; background: #050505; color: #d4d4d4;
  border: 1px solid #30363d;
}
.tmc-toolbar {
  flex: 0 0 auto; display: flex; justify-content: flex-end; gap: 8px;
  padding: 8px; border-bottom: 1px solid #222; background: #101010;
}
.tmc-action {
  height: 32px; padding: 0 10px; border: 1px solid #30363d; border-radius: 4px;
  background: #161b22; color: #d4d4d4; font-size: 12px; cursor: pointer;
}
.tmc-action:active { background: #264f78; }
#terminal-mobile-copy-layer pre {
  flex: 1 1 auto; min-height: 0; margin: 0; padding: 10px;
  font-family: "'MesloLGM NF', Menlo, Consolas, 'Courier New', monospace";
  font-size: 12px; line-height: 1.5; white-space: pre-wrap;
  overflow-wrap: anywhere; overflow: auto;
  user-select: text; -webkit-user-select: text;
  -webkit-touch-callout: default;
  touch-action: auto; cursor: text;
}
"""

_SESSIONTRACKER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session Tracker</title>
<style>""" + _SESSIONTRACKER_CSS + """</style>
</head>
<body class="layout-col">
<header>
  <div class="header-row">
    <div>
      <h1 id="title">Session Tracker</h1>
      <div id="meta-line">Resolving…</div>
    </div>
    <nav id="tabs" class="tabs" aria-label="Session Tracker tabs">
      <button type="button" id="tab-tracker" class="tab-button active">Tracker</button>
      <button type="button" id="tab-terminal" class="tab-button">Terminal</button>
    </nav>
  </div>
</header>
<div id="viewport" class="panel active">
  <div id="terminal-wrap"><p class="empty" id="empty-hint">Waiting for session activity…</p></div>
</div>
<div id="admin-terminal-panel" class="panel">
  <div id="terminal-toolbar">
    <select id="terminal-session-select" aria-label="PTY sessions"></select>
    <span id="terminal-status">Terminal disabled</span>
    <button type="button" id="terminal-new" class="terminal-button">New</button>
    <button type="button" id="terminal-disconnect" class="terminal-button icon" title="Close terminal" aria-label="Close terminal" disabled>⏻</button>
  </div>
  <div id="xterm-host" style="position:relative">
    <div id="term-scrollbar"><div id="term-scrollbar-thumb"></div></div>
    </div>
  </div>
  <pre id="terminal-fallback" class="hidden" tabindex="0"></pre>

  <!-- Mobile input trap: independent textarea for mobile keyboard capture -->
  <textarea id="mobile-input-trap" class="mobile-input-trap"
    autocomplete="off" autocapitalize="none" autocorrect="off"
    spellcheck="false" inputmode="text" enterkeyhint="enter"></textarea>

  <!-- Mobile copy button (FAB, shown when text selected in xterm) -->
  <button type="button" id="terminal-mobile-copy-fab" title="Copy selected text" style="display:none;">📋 Copy</button>

  <!-- Mobile copy mode overlay -->
  <div id="terminal-mobile-copy-layer">
    <div class="tmc-toolbar">
      <span style="font-size:11px;color:#888;flex:1;">长按选择文本</span>
      <button type="button" class="tmc-action" onclick="copyMobileSelection()">📋 Copy</button>
      <button type="button" class="tmc-action" onclick="selectAllMobileCopy()">selectAll</button>
      <button type="button" class="tmc-action" onclick="closeMobileCopyMode()">✕ Close</button>
    </div>
    <pre tabindex="0"></pre>
  </div>

  <div id="terminal-vkeys">
    <button type="button" class="vkey modifier" id="vkey-ctrl" title="Ctrl (toggle)">Ctrl</button>
    <button type="button" class="vkey" id="vkey-tab" title="Tab">Tab</button>
    <button type="button" class="vkey" id="vkey-esc" title="Escape">Esc</button>
    <button type="button" class="vkey" id="vkey-up" title="Arrow Up">↑</button>
    <button type="button" class="vkey" id="vkey-down" title="Arrow Down">↓</button>
    <button type="button" class="vkey" id="vkey-cc" title="Ctrl+C (interrupt)">C-c</button>
    <button type="button" class="vkey" id="vkey-cb" title="Ctrl+B (tmux prefix)">C-b</button>
    <button type="button" class="vkey" id="vkey-pgup" title="Page Up (scroll up)">PgUp</button>
    <button type="button" class="vkey" id="vkey-pgdn" title="Page Down (scroll down)">PgDn</button>
  </div>
</div>
<button type="button" id="scroll-bottom" title="Scroll to bottom">↓</button>

<!-- Terminal context menu (right-click / long-press) -->
<div id="terminal-ctx-menu" style="display:none;">
  <button data-action="copy" onclick="terminalCtxCopy()">📋 Copy <span class="ctx-kbd">Ctrl+Shift+C</span></button>
  <button data-action="paste" onclick="terminalCtxPaste()">📋 Paste <span class="ctx-kbd">Ctrl+Shift+V</span></button>
  <div class="ctx-divider"></div>
  <button onclick="terminalCtxTextSelect()">📋 Text Select (mobile)</button>
  <button onclick="terminalCtxSelectAll()">📋 Select All</button>
  <button onclick="terminalCtxClear()">🧹 Clear</button>
</div>

<!-- Hidden paste-trap input for HTTP fallback -->
<input id="terminal-paste-trap" type="text" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false">

<script>
const params = new URLSearchParams(location.search);
const apiBase = location.pathname.replace(/\\/?$/, '') + '/api';
const staticBase = location.pathname.replace(/\\/?$/, '') + '/static';
const terminal = document.getElementById('terminal-wrap');
const emptyHint = document.getElementById('empty-hint');
const scrollBtn = document.getElementById('scroll-bottom');
const tabs = document.getElementById('tabs');
const trackerPanel = document.getElementById('viewport');
const terminalPanel = document.getElementById('admin-terminal-panel');
const tabTracker = document.getElementById('tab-tracker');
const tabTerminal = document.getElementById('tab-terminal');
const terminalSessionSelect = document.getElementById('terminal-session-select');
const terminalStatus = document.getElementById('terminal-status');
const terminalNew = document.getElementById('terminal-new');
const terminalDisconnect = document.getElementById('terminal-disconnect');
const xtermHost = document.getElementById('xterm-host');
const terminalFallback = document.getElementById('terminal-fallback');
let autoFollow = true;
let sessionId = '';
let lineCursor = 0;
let eventSource = null;
let pollTimer = null;
let gotTerminalLines = false;
let adminTerminalAvailable = false;
const SCROLL_THRESHOLD = 48;

function nearBottom() {
  const el = terminal;
  return el.scrollHeight - el.scrollTop - el.clientHeight <= SCROLL_THRESHOLD;
}

function updateScrollButton() {
  scrollBtn.classList.toggle('visible', !autoFollow && !nearBottom());
}

terminal.addEventListener('scroll', () => {
  if (nearBottom()) {
    autoFollow = true;
  } else {
    autoFollow = false;
  }
  updateScrollButton();
});

scrollBtn.addEventListener('click', () => {
  autoFollow = true;
  terminal.scrollTop = terminal.scrollHeight;
  updateScrollButton();
});

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function selectTab(name) {
  const terminalActive = name === 'terminal' && adminTerminalAvailable;
  trackerPanel.classList.toggle('active', !terminalActive);
  terminalPanel.classList.toggle('active', terminalActive);
  tabTracker.classList.toggle('active', !terminalActive);
  tabTerminal.classList.toggle('active', terminalActive);
  scrollBtn.style.display = terminalActive ? 'none' : '';
  if (terminalActive) {
    openTerminalPanel();
  }
}

tabTracker.addEventListener('click', () => selectTab('tracker'));
tabTerminal.addEventListener('click', () => selectTab('terminal'));

let terminalWs = null;
let xterm = null;
let fitAddon = null;
let xtermAssetsPromise = null;
let terminalSurfaceReady = false;
let usingFallback = false;
let terminalSessions = [];
let activeTerminalId = '';
let maxTerminalSessions = 4;
let terminalWsId = '';
let terminalConnectTimer = null;
let terminalTransport = '';
let terminalHttpPollToken = 0;
let terminalHttpPolling = false;
let _httpInputBuffer = '';
let _httpInputTimer = null;
let terminalOutputCursor = 0;

function setTerminalStatus(text) {
  terminalStatus.textContent = text;
}

function clearTerminalConnectTimer() {
  if (terminalConnectTimer) {
    clearTimeout(terminalConnectTimer);
    terminalConnectTimer = null;
  }
}

function isMobileTerminalClient() {
  const ua = navigator.userAgent || '';
  return /iPhone|iPad|iPod|Android|Mobile|baiduhi_ios/i.test(ua);
}

if (isMobileTerminalClient()) {
  document.body.classList.add('mobile-terminal');
  // Track keyboard open/close via visualViewport and position vkey bar correctly
  const vkeyBar = document.getElementById('terminal-vkeys');
  if (window.visualViewport) {
    const updateKbdState = () => {
      const vk = window.visualViewport;
      const kbdOpen = vk.height < window.innerHeight - 80;
      document.body.classList.toggle('kbd-open', kbdOpen);
      // Position vkey bar at the bottom of the visible viewport (above keyboard).
      // visualViewport.height shrinks when keyboard appears; offsetTop accounts
      // for any scroll offset. Using vk.height + vk.offsetTop gives the visible
      // viewport bottom in CSS pixels relative to the layout viewport.
      if (kbdOpen && vkeyBar) {
        const bottom = Math.max(0, window.innerHeight - (vk.height + vk.offsetTop));
        vkeyBar.style.bottom = bottom + 'px';
      } else if (vkeyBar) {
        vkeyBar.style.bottom = '';
      }
    };
    window.visualViewport.addEventListener('resize', updateKbdState);
    window.visualViewport.addEventListener('scroll', updateKbdState);
    // Initial call
    setTimeout(updateKbdState, 100);
  }
}

function terminalApiUrl(path, extra = {}) {
  const qs = new URLSearchParams(params);
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      qs.set(key, String(value));
    }
  });
  return apiBase + '/admin/terminal' + path + '?' + qs.toString();
}

async function terminalApi(path, { method = 'GET', extra = {}, body = null } = {}) {
  const options = { method };
  if (body !== null) {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body);
  }
  const r = await fetch(terminalApiUrl(path, extra), options);
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}

function loadStyle(url) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector('link[data-sessiontracker-xterm-css]');
    if (existing) {
      resolve();
      return;
    }
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = url;
    link.dataset.sessiontrackerXtermCss = '1';
    link.onload = resolve;
    link.onerror = reject;
    document.head.appendChild(link);
  });
}

function loadScript(url) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = url;
    script.onload = resolve;
    script.onerror = reject;
    document.head.appendChild(script);
  });
}

function ensureXtermAssets() {
  if (!xtermAssetsPromise) {
    xtermAssetsPromise = loadStyle(staticBase + '/xterm/xterm.css')
      .then(() => loadScript(staticBase + '/xterm/xterm.js'))
      .then(() => loadScript(staticBase + '/xterm/addon-fit.js'))
      .then(() => loadScript(staticBase + '/xterm/addon-web-links.js'));
  }
  return xtermAssetsPromise;
}

// ── Terminal copy/paste helpers ─────────────────────────────────────────────
function terminalCopyText(text) {
  if (!text) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => terminalFallbackCopy(text));
  } else {
    terminalFallbackCopy(text);
  }
}

function terminalFallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (_) {}
  document.body.removeChild(ta);
}

function terminalDoPaste() {
  if (!isMobileTerminalClient() && navigator.clipboard && navigator.clipboard.readText) {
    navigator.clipboard.readText()
      .then(text => { if (text) sendTerminalInput(text); })
      .catch(() => {
        awaitingTermPaste = true;
        const trap = document.getElementById('terminal-paste-trap');
        if (trap) { trap.value = ''; trap.focus(); }
      });
    return;
  }
  // Mobile: create a visible paste dialog so the user can use the system
  // keyboard's paste button or long-press paste. The dialog is a semi-transparent
  // overlay with a large textarea that auto-focuses.
  let dialog = document.getElementById('paste-dialog');
  if (dialog) dialog.remove();
  dialog = document.createElement('div');
  dialog.id = 'paste-dialog';
  dialog.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;padding:16px;';
  const box = document.createElement('div');
  box.style.cssText = 'background:#161b22;border:1px solid #444c6a;border-radius:10px;padding:16px;width:100%;max-width:400px;';
  const label = document.createElement('div');
  label.textContent = 'Paste content below (long-press or use keyboard paste):';
  label.style.cssText = 'color:#8b949e;font-size:13px;margin-bottom:10px;';
  const ta = document.createElement('textarea');
  ta.style.cssText = 'width:100%;height:100px;background:#0d1117;color:#d4d4d4;border:1px solid #30363d;border-radius:6px;padding:8px;font-size:16px;resize:none;-webkit-user-select:text;';
  ta.placeholder = 'Long-press here → Paste';
  ta.autocomplete = 'off';
  ta.autocorrect = 'off';
  ta.autocapitalize = 'none';
  ta.spellcheck = false;
  const btnRow = document.createElement('div');
  btnRow.style.cssText = 'display:flex;gap:8px;margin-top:12px;justify-content:flex-end;';
  const cancelBtn = document.createElement('button');
  cancelBtn.textContent = 'Cancel';
  cancelBtn.style.cssText = 'padding:6px 16px;border:1px solid #30363d;border-radius:6px;background:#161b22;color:#8b949e;cursor:pointer;font-size:14px;';
  const sendBtn = document.createElement('button');
  sendBtn.textContent = 'Send';
  sendBtn.style.cssText = 'padding:6px 16px;border:1px solid #1f6feb;border-radius:6px;background:#1f6feb;color:#fff;cursor:pointer;font-size:14px;';
  btnRow.appendChild(cancelBtn);
  btnRow.appendChild(sendBtn);
  box.appendChild(label);
  box.appendChild(ta);
  box.appendChild(btnRow);
  dialog.appendChild(box);
  document.body.appendChild(dialog);
  // Auto-focus the textarea after render
  setTimeout(() => ta.focus(), 50);
  // Send button: send whatever is in the textarea
  sendBtn.addEventListener('click', () => {
    const text = ta.value;
    if (text) sendTerminalInput(text);
    dialog.remove();
    if (xterm) xterm.focus();
  });
  // Cancel button
  cancelBtn.addEventListener('click', () => {
    dialog.remove();
    if (xterm) xterm.focus();
  });
  // Also close on backdrop click
  dialog.addEventListener('click', (e) => {
    if (e.target === dialog) {
      dialog.remove();
      if (xterm) xterm.focus();
    }
  });
  // Catch paste event directly on the textarea (user uses keyboard paste button)
  ta.addEventListener('paste', (e) => {
    const text = e.clipboardData && e.clipboardData.getData('text');
    if (text) {
      // Don't put it in the textarea — send directly and close
      e.preventDefault();
      sendTerminalInput(text);
      dialog.remove();
      if (xterm) xterm.focus();
    }
  });
  // If user types text instead of pasting, allow them to send it
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const text = ta.value;
      if (text) sendTerminalInput(text);
      dialog.remove();
      if (xterm) xterm.focus();
    } else if (e.key === 'Escape') {
      dialog.remove();
      if (xterm) xterm.focus();
    }
  });
}

let awaitingTermPaste = false;

// ── Terminal context menu ───────────────────────────────────────────────────
function openTerminalContextMenu(x, y) {
  let menu = document.getElementById('terminal-ctx-menu');
  if (!menu) return;
  const sel = xterm ? xterm.getSelection() : '';
  const copyBtn = menu.querySelector('[data-action="copy"]');
  if (copyBtn) copyBtn.style.display = sel ? '' : 'none';
  // Show menu near the long-press point, offset down so finger doesn't cover it.
  // When soft keyboard is open, adjust for visualViewport offset so the menu
  // appears in the visible area, not under the keyboard.
  const vk = window.visualViewport;
  const vkOffset = vk ? vk.offsetTop : 0;
  const viewH = vk ? vk.height : window.innerHeight;
  // First render to measure actual size
  menu.style.display = 'block';
  menu.style.left = '-9999px';
  menu.style.top = '-9999px';
  const menuW = menu.offsetWidth || 210;
  const menuH = menu.offsetHeight || 200;
  // x is relative to layout viewport; adjust by visualViewport.offsetTop
  const adjY = y - vkOffset;
  const left = Math.max(8, Math.min(x - menuW / 2, window.innerWidth - menuW - 8));
  const top = Math.min(adjY + 8, viewH - menuH - 8) + vkOffset;
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
}

function hideTerminalContextMenu() {
  const menu = document.getElementById('terminal-ctx-menu');
  if (menu) menu.style.display = 'none';
}

function terminalCtxCopy() {
  hideTerminalContextMenu();
  const sel = xterm ? xterm.getSelection() : '';
  if (sel) terminalCopyText(sel);
}

function terminalCtxPaste() {
  hideTerminalContextMenu();
  terminalDoPaste();
}

function terminalCtxSelectAll() {
  hideTerminalContextMenu();
  if (xterm) xterm.selectAll();
}

function terminalCtxClear() {
  hideTerminalContextMenu();
  if (xterm) xterm.clear();
}

function terminalCtxTextSelect() {
  hideTerminalContextMenu();
  // Open mobile copy mode: dump buffer into a selectable <pre>
  openMobileCopyMode();
}

// ── Mobile copy mode (selectable text overlay) ──────────────────────────────
let mobileCopyModeOpen = false;
let mobileCopyWasNearBottom = true;

function openMobileCopyMode() {
  let layer = document.getElementById('terminal-mobile-copy-layer');
  if (!layer || !xterm) return;
  const buffer = xterm.buffer && xterm.buffer.active;
  let text = '';
  if (buffer) {
    const rows = [];
    for (let i = 0; i < buffer.length; i++) {
      const line = buffer.getLine(i);
      if (!line) continue;
      const t = line.translateToString(true);
      if (line.isWrapped && rows.length > 0) { rows[rows.length - 1] += t; }
      else { rows.push(t); }
    }
    text = rows.join('\\n').replace(/\\n+$/, '');
  }
  const pre = layer.querySelector('pre');
  if (pre) pre.textContent = text;
  const vp = xtermHost.querySelector('.xterm-viewport');
  mobileCopyWasNearBottom = !vp || (vp.scrollHeight - vp.scrollTop - vp.clientHeight < 20);
  layer.style.display = 'flex';
  mobileCopyModeOpen = true;
  if (pre) {
    pre.scrollTop = Math.max(0, pre.scrollHeight - pre.clientHeight);
  }
}

function closeMobileCopyMode() {
  let layer = document.getElementById('terminal-mobile-copy-layer');
  if (layer) layer.style.display = 'none';
  mobileCopyModeOpen = false;
  if (mobileCopyWasNearBottom && xterm) {
    termUserScrolled = false;
    xterm.scrollToBottom();
    flushPendingWrites();
  }
  if (isMobileTerminalClient() && mobileInputTrap) {
    mobileInputTrap.focus({ preventScroll: true });
  } else if (xterm) {
    xterm.focus();
  }
}

function copyMobileSelection() {
  let layer = document.getElementById('terminal-mobile-copy-layer');
  const pre = layer ? layer.querySelector('pre') : null;
  let text = '';
  const sel = window.getSelection && window.getSelection();
  if (sel && sel.toString() && pre && pre.contains(sel.anchorNode)) {
    text = sel.toString();
  } else if (pre) {
    text = pre.textContent || '';
  }
  if (text) terminalCopyText(text);
}

function selectAllMobileCopy() {
  let layer = document.getElementById('terminal-mobile-copy-layer');
  const pre = layer ? layer.querySelector('pre') : null;
  if (!pre) return;
  const range = document.createRange();
  range.selectNodeContents(pre);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

// ── Smart auto-follow for xterm viewport ────────────────────────────────────
let termUserScrolled = false;
let termScrollResumeTimer = null;
let termUserScrollIntentUntil = 0;
let boundXtermVp = null;
const pendingWrites = []; // buffer output while user scrolled up

function isTermNearBottom() {
  if (!xterm) return true;
  const vp = boundXtermVp || xtermHost.querySelector('.xterm-viewport');
  if (!vp) return true;
  return vp.scrollHeight - vp.scrollTop - vp.clientHeight < 20;
}

function onTermViewportScroll() {
  if (isTermNearBottom() && !mobileCopyModeOpen) {
    termUserScrolled = false;
    clearTimeout(termScrollResumeTimer);
    flushPendingWrites();
  } else {
    termUserScrolled = true;
  }
  updateScrollbarThumb();
}

function markTermUserScrollIntent() {
  termUserScrollIntentUntil = Date.now() + (isMobileTerminalClient() ? 4000 : 900);
}

function bindXtermViewport(vp) {
  if (!vp || boundXtermVp === vp) return;
  unbindXtermViewport();
  boundXtermVp = vp;
  vp.addEventListener('scroll', onTermViewportScroll, { passive: true });
  vp.addEventListener('wheel', markTermUserScrollIntent, { passive: true });
  // Touch swipe → PgUp/PgDn when tmux mouse tracking is active
  // (touch scroll is ignored by tmux; only wheel events trigger copy-mode)
  vp.addEventListener('touchstart', (e) => {
    markTermUserScrollIntent();
    if (!xterm || !xterm._core || !xterm._core.coreMouseService) return;
    if (!xterm._core.coreMouseService.areMouseEventsActive) return;
    // Mouse tracking ON (tmux/vim) — track swipe for PgUp/PgDn conversion
    const t = e.touches[0];
    if (t) { _swipeStartY = t.clientY; _swipeStartTime = Date.now(); _swipeFired = false; }
  }, { passive: true });
  vp.addEventListener('touchmove', (e) => {
    markTermUserScrollIntent();
    if (_swipeStartY === null || _swipeFired) return;
    if (!xterm || !xterm._core || !xterm._core.coreMouseService) return;
    if (!xterm._core.coreMouseService.areMouseEventsActive) return;
    const t = e.touches[0];
    if (!t) return;
    const dy = t.clientY - _swipeStartY;
    const elapsed = Date.now() - _swipeStartTime;
    // Threshold: moved >60px within 400ms = swipe
    if (Math.abs(dy) > 60 && elapsed < 400) {
      _swipeFired = true;
      const dir = dy < 0 ? -1 : 1; // swipe up = scroll up, swipe down = scroll down
      tmuxAwarePageKey(dir < 0 ? '\x1b[5~' : '\x1b[6~');
    }
  }, { passive: true });
  vp.addEventListener('touchend', () => { _swipeStartY = null; _swipeFired = false; }, { passive: true });
}

// Swipe state for tmux touch→PgUp/PgDn conversion
let _swipeStartY = null, _swipeStartTime = 0, _swipeFired = false;

function unbindXtermViewport() {
  if (!boundXtermVp) return;
  boundXtermVp.removeEventListener('scroll', onTermViewportScroll);
  boundXtermVp.removeEventListener('wheel', markTermUserScrollIntent);
  // Note: touch listeners on boundXtermVp will be GC'd when vp is replaced
  boundXtermVp = null;
}

// ── Custom scrollbar overlay for xterm viewport ─────────────────────────────
// The native xterm scrollbar is invisible on mobile WebViews. This overlay
// draws a draggable thumb that syncs with the xterm viewport scrollTop, so
// scrolling is discoverable + usable by touch on phones.

async function initAdminTerminal() {
  if (terminalSurfaceReady) return;
  setTerminalStatus('Loading terminal...');
  try {
    await ensureXtermAssets();
    if (!window.Terminal) throw new Error('xterm unavailable');
    xterm = new window.Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: "'MesloLGM NF', 'RemoteCC MesloLGM NF', Menlo, Consolas, 'Courier New', monospace",
      fontSize: 13,
      scrollback: 50000,
      smoothScrollDuration: isMobileTerminalClient() ? 0 : 80,
      allowProposedApi: true,
      theme: {
        background: '#050505',
        foreground: '#d4d4d4',
        cursor: '#58a6ff',
        selectionBackground: '#264f78'
      }
    });
    if (window.FitAddon && window.FitAddon.FitAddon) {
      fitAddon = new window.FitAddon.FitAddon();
      xterm.loadAddon(fitAddon);
    }
    if (window.WebLinksAddon && window.WebLinksAddon.WebLinksAddon) {
      xterm.loadAddon(new window.WebLinksAddon.WebLinksAddon());
    }
    xterm.open(xtermHost);
    // IME composition tracking — guard against mobile IME swallowing characters
    let composing = false;
    const xtermTextarea = xtermHost.querySelector('textarea');
    if (xtermTextarea) {
      xtermTextarea.addEventListener('compositionstart', () => { composing = true; });
      xtermTextarea.addEventListener('compositionend', () => {
        composing = false;
        // Some IMEs (e.g. Sogou on mobile) emit the final text via the
        // textarea value rather than through keydown. Give the browser a
        // tick to update the value, then send whatever is there.
        setTimeout(() => {
          const val = xtermTextarea.value;
          if (val) {
            sendTerminalInput(val.replace(/\\x0d?\\x0a/g, '\\x0d'));
            xtermTextarea.value = '';
          }
        }, 0);
      });
    }
    xterm.onData(data => {
      if (composing) return; // will be handled by compositionend
      sendTerminalInput(data);
    });
    xterm.attachCustomKeyEventHandler(ev => {
      if (ev.type === 'keydown' && ev.key === 'Tab') { ev.preventDefault(); }
      // Ctrl+Shift+C: copy selection
      if (ev.type === 'keydown' && ev.key === 'c' && ev.ctrlKey && ev.shiftKey) {
        ev.preventDefault();
        const sel = xterm.getSelection();
        if (sel) terminalCopyText(sel);
        return false;
      }
      // Ctrl+Shift+V: paste from clipboard
      if (ev.type === 'keydown' && ev.key === 'v' && ev.ctrlKey && ev.shiftKey) {
        ev.preventDefault();
        terminalDoPaste();
        return false;
      }
      // During IME composition, let the browser handle the event natively
      if (ev.type === 'keydown' && composing) { return false; }
      return true;
    });
    // Native paste event (Ctrl+V / mobile long-press paste)
    xtermHost.addEventListener('paste', (e) => {
      e.preventDefault();
      const text = e.clipboardData && e.clipboardData.getData('text');
      if (text) sendTerminalInput(text);
    });
    // Right-click context menu
    xtermHost.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      openTerminalContextMenu(e.clientX, e.clientY);
    });
    // Long-press on mobile → context menu
    let termTouchStart = null;
    let termTouchMoved = false;
    let longPressTimer = null;
    function clearTermLongPress() {
      clearTimeout(longPressTimer); longPressTimer = null; termTouchStart = null; termTouchMoved = false;
    }
    xtermHost.addEventListener('touchstart', (e) => {
      if (!isMobileTerminalClient()) return;
      const t = e.touches && e.touches[0];
      if (!t) return;
      termTouchStart = { x: t.clientX, y: t.clientY };
      termTouchMoved = false;
      clearTimeout(longPressTimer);
      longPressTimer = setTimeout(() => {
        if (!termTouchMoved && termTouchStart) {
          openTerminalContextMenu(termTouchStart.x, termTouchStart.y);
        }
      }, 1000);
    }, { passive: true });
    xtermHost.addEventListener('touchmove', (e) => {
      if (!isMobileTerminalClient()) return;
      const t = e.touches && e.touches[0];
      if (t && termTouchStart) {
        const dx = Math.abs(t.clientX - termTouchStart.x);
        const dy = t.clientY - termTouchStart.y;
        if (Math.abs(dy) > 5 || dx > 5) { termTouchMoved = true; clearTermLongPress(); }
        // Only mark userScrolled when swiping UP (finger moves up = reading history)
        if (dy < -8) { termUserScrolled = true; }
      }
    }, { passive: true });
    xtermHost.addEventListener('touchend', clearTermLongPress, { passive: true });
    xtermHost.addEventListener('touchcancel', clearTermLongPress, { passive: true });
    // xterm selection change → show copy button on mobile
    xterm.onSelectionChange(() => {
      const sel = xterm.getSelection();
      if (isMobileTerminalClient()) {
        const copyFab = document.getElementById('terminal-mobile-copy-fab');
        if (copyFab) { copyFab.style.display = sel ? '' : 'none'; }
      }
    });
    // User scroll detection on xterm viewport for smart auto-follow
    const vpObserver = new MutationObserver(() => {
      const vp = xtermHost.querySelector && xtermHost.querySelector('.xterm-viewport');
    });
    vpObserver.observe(xtermHost, { childList: true, subtree: true });
    const vp0 = xtermHost.querySelector && xtermHost.querySelector('.xterm-viewport');
    if (vp0) { bindXtermViewport(vp0); vpObserver.disconnect(); }
    initTermScrollbar();
    updateScrollbarThumb();
    xterm.writeln('Session Tracker admin terminal');
    terminalFallback.classList.add('hidden');
    xtermHost.classList.remove('hidden');
  } catch (_) {
    usingFallback = true;
    xtermHost.classList.add('hidden');
    terminalFallback.classList.remove('hidden');
    terminalFallback.textContent = 'Session Tracker admin terminal\\r\\n';
    terminalFallback.addEventListener('keydown', handleFallbackKey);
  }
  terminalSurfaceReady = true;
  setTerminalStatus(adminTerminalAvailable ? 'Ready' : 'Terminal disabled');
  resizeAdminTerminal();
}

function resetTerminalSurface() {
  if (xterm) {
    xterm.reset();
  } else {
    terminalFallback.textContent = '';
  }
}

function terminalDimensions() {
  if (xterm) return { cols: xterm.cols || 100, rows: xterm.rows || 30 };
  const rect = terminalFallback.getBoundingClientRect();
  return {
    cols: Math.max(40, Math.floor((rect.width || 800) / 8)),
    rows: Math.max(10, Math.floor((rect.height || 400) / 18))
  };
}

function resizeAdminTerminal() {
  if (!terminalSurfaceReady) return;
  if (fitAddon) {
    try { fitAddon.fit(); } catch (_) {}
  }
  const dims = terminalDimensions();
  if (terminalWs && terminalWs.readyState === WebSocket.OPEN) {
    terminalWs.send(JSON.stringify({ type: 'resize', cols: dims.cols, rows: dims.rows }));
  } else if (terminalTransport === 'http' && activeTerminalId) {
    terminalApi(
      '/sessions/' + encodeURIComponent(activeTerminalId) + '/resize',
      { method: 'POST', body: dims }
    ).catch(() => {});
  }
}

let _scrollToBottomRaf = 0;
function requestScrollToBottom() {
  if (mobileCopyModeOpen || (isMobileTerminalClient() && termUserScrolled)) return;
  if (_scrollToBottomRaf) return;
  _scrollToBottomRaf = requestAnimationFrame(() => {
    _scrollToBottomRaf = 0;
    if (xterm) xterm.scrollToBottom();
  });
}

function writeTerminal(data) {
  data = data || '';
  if (xterm) {
    // Buffer output while user is scrolled up (avoid burning scrollback)
    if (termUserScrolled || mobileCopyModeOpen) {
      pendingWrites.push(data);
      const max = 20000;
      if (pendingWrites.length > max) pendingWrites.splice(0, pendingWrites.length - max);
      // Still update scrollbar in case scrollArea grew (new history)
      return;
    }
    xterm.write(data);
    requestScrollToBottom();
    // Update scrollbar after xterm renders (scrollArea height may change)
    requestAnimationFrame(() => requestAnimationFrame(updateScrollbarThumb));
  } else {
    terminalFallback.textContent += data;
    terminalFallback.scrollTop = terminalFallback.scrollHeight;
  }
}

function flushPendingWrites() {
  if (pendingWrites.length === 0) return;
  const batch = pendingWrites.splice(0);
  xterm.write(batch.join(''));
  xterm.scrollToBottom();
}

function sendTerminalInput(data) {
  // User typed something → resume auto-follow so they see the output
  if (data) {
    termUserScrolled = false;
    flushPendingWrites();
  }
  if (terminalWs && terminalWs.readyState === WebSocket.OPEN) {
    terminalWs.send(JSON.stringify({ type: 'input', data }));
    return;
  }
  if (terminalTransport !== 'http' || !activeTerminalId) return;
  // Buffer rapid input to reduce HTTP request overhead
  _httpInputBuffer += data;
  if (!_httpInputTimer) {
    _httpInputTimer = setTimeout(() => {
      const batch = _httpInputBuffer;
      _httpInputBuffer = '';
      _httpInputTimer = null;
      if (!batch || !activeTerminalId) return;
      terminalApi(
        '/sessions/' + encodeURIComponent(activeTerminalId) + '/input',
        { method: 'POST', body: { data: batch } }
      ).catch(err => {
        setTerminalStatus('Input error: ' + (err && err.message ? err.message : err));
      });
    }, 16); // ~1 frame
  }
}

function handleFallbackKey(ev) {
  if (!usingFallback) return;
  let data = '';
  if (ev.ctrlKey && ev.key.toLowerCase() === 'c') data = '\\x03';
  else if (ev.key === 'Enter') data = '\\r';
  else if (ev.key === 'Backspace') data = '\\u007f';
  else if (ev.key === 'Tab') data = '\\t';
  else if (ev.key === 'Escape') data = '\\x1b';
  else if (ev.key === 'ArrowUp') data = '\\x1b[A';
  else if (ev.key === 'ArrowDown') data = '\\x1b[B';
  else if (ev.key === 'ArrowRight') data = '\\x1b[C';
  else if (ev.key === 'ArrowLeft') data = '\\x1b[D';
  else if (ev.key.length === 1 && !ev.metaKey) data = ev.key;
  if (!data) return;
  ev.preventDefault();
  sendTerminalInput(data);
}

function detachCurrentTerminalWs() {
  stopTerminalHttpPolling();
  clearTerminalConnectTimer();
  if (!terminalWs) return;
  const ws = terminalWs;
  terminalWs = null;
  terminalWsId = '';
  ws.onopen = null;
  ws.onmessage = null;
  ws.onerror = null;
  ws.onclose = null;
  try { ws.close(); } catch (_) {}
}

function stopTerminalHttpPolling() {
  terminalHttpPollToken += 1;
  terminalHttpPolling = false;
  if (terminalTransport === 'http') terminalTransport = '';
}

function waitForNextPoll(delayMs) {
  return new Promise(resolve => setTimeout(resolve, delayMs));
}

async function startTerminalHttpFallback(terminalId) {
  if (!adminTerminalAvailable || !terminalId) return;
  const token = ++terminalHttpPollToken;
  terminalHttpPolling = true;
  terminalTransport = 'http';
  terminalDisconnect.disabled = false;
  setTerminalStatus('Connected (HTTP)');
  try {
    await terminalApi(
      '/sessions/' + encodeURIComponent(terminalId) + '/resize',
      { method: 'POST', body: terminalDimensions() }
    );
  } catch (_) {}
  while (
    terminalHttpPolling &&
    token === terminalHttpPollToken &&
    activeTerminalId === terminalId
  ) {
    try {
      const data = await terminalApi(
        '/sessions/' + encodeURIComponent(terminalId) + '/output',
        { extra: { cursor: terminalOutputCursor, wait: 5 } }
      );
      if (
        !terminalHttpPolling ||
        token !== terminalHttpPollToken ||
        activeTerminalId !== terminalId
      ) {
        break;
      }
      if (data.terminal && data.terminal.id) {
        activeTerminalId = data.terminal.id;
        renderTerminalSessionTabs();
      }
      if (data.overflow) {
        resetTerminalSurface();
      }
      if (data.output) {
        writeTerminal(data.output);
      }
      if (typeof data.cursor === 'number') {
        terminalOutputCursor = data.cursor;
      }
      if (data.exit_code !== null && data.exit_code !== undefined) {
        setTerminalStatus('Closed');
        terminalDisconnect.disabled = true;
        await refreshTerminalSessions({ createIfEmpty: false, connect: false });
        break;
      }
      setTerminalStatus('Connected (HTTP)');
    } catch (err) {
      if (
        !terminalHttpPolling ||
        token !== terminalHttpPollToken ||
        activeTerminalId !== terminalId
      ) {
        break;
      }
      if (err && String(err.message || err).includes('terminal not found')) {
        setTerminalStatus('Closed');
        terminalDisconnect.disabled = true;
        await refreshTerminalSessions({ createIfEmpty: false, connect: false });
        break;
      }
      setTerminalStatus('HTTP reconnecting...');
      await waitForNextPoll(1000);
    }
  }
}

function fallbackTerminalFromWs(ws, terminalId, label) {
  if (terminalWs !== ws) return;
  clearTerminalConnectTimer();
  ws.onopen = null;
  ws.onmessage = null;
  ws.onerror = null;
  ws.onclose = null;
  try { ws.close(); } catch (_) {}
  terminalWs = null;
  terminalWsId = '';
  setTerminalStatus(label || 'Connecting via HTTP...');
  startTerminalHttpFallback(terminalId);
}

function abandonTerminalWs(ws) {
  if (terminalWs !== ws) return;
  clearTerminalConnectTimer();
  ws.onopen = null;
  ws.onmessage = null;
  ws.onerror = null;
  ws.onclose = null;
  try { ws.close(); } catch (_) {}
  terminalWs = null;
  terminalWsId = '';
}

function renderTerminalSessionTabs() {
  terminalSessionSelect.innerHTML = '';
  if (!terminalSessions.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = 'No terminal';
    terminalSessionSelect.appendChild(option);
  }
  terminalSessions.forEach(session => {
    const option = document.createElement('option');
    option.value = session.id;
    option.textContent = session.title || session.id;
    option.title = session.cwd || session.title || session.id;
    terminalSessionSelect.appendChild(option);
  });
  terminalSessionSelect.value = activeTerminalId || '';
  terminalSessionSelect.disabled = !adminTerminalAvailable || !terminalSessions.length;
  terminalNew.disabled = !adminTerminalAvailable || terminalSessions.length >= maxTerminalSessions;
  terminalDisconnect.disabled = !adminTerminalAvailable || !activeTerminalId;
}

function applyTerminalSessionPayload(data) {
  terminalSessions = data.sessions || terminalSessions || [];
  maxTerminalSessions = data.max_sessions || maxTerminalSessions || 4;
  if (data.terminal && data.terminal.id) {
    activeTerminalId = data.terminal.id;
  }
  if (activeTerminalId && !terminalSessions.some(item => item.id === activeTerminalId)) {
    activeTerminalId = '';
  }
  if (!activeTerminalId && terminalSessions.length) {
    activeTerminalId = terminalSessions[0].id;
  }
  renderTerminalSessionTabs();
}

async function refreshTerminalSessions({ createIfEmpty = false, connect = false } = {}) {
  if (!adminTerminalAvailable) return;
  await initAdminTerminal();
  try {
    const data = await terminalApi('/sessions');
    applyTerminalSessionPayload(data);
    if (!terminalSessions.length && createIfEmpty) {
      await createTerminalSession({ connect });
      return;
    }
    if (connect && activeTerminalId) {
      await connectAdminTerminal(activeTerminalId);
    } else if (!terminalSessions.length) {
      setTerminalStatus('No terminal');
      resetTerminalSurface();
    } else {
      setTerminalStatus('Ready');
    }
  } catch (err) {
    setTerminalStatus('Error: ' + (err && err.message ? err.message : err));
  }
}

async function createTerminalSession({ connect = true } = {}) {
  if (!adminTerminalAvailable) return;
  await initAdminTerminal();
  const dims = terminalDimensions();
  try {
    const data = await terminalApi('/sessions', {
      method: 'POST',
      extra: { cols: dims.cols, rows: dims.rows }
    });
    applyTerminalSessionPayload(data);
    if (connect && activeTerminalId) {
      await connectAdminTerminal(activeTerminalId);
    }
  } catch (err) {
    setTerminalStatus('Error: ' + (err && err.message ? err.message : err));
  }
}

async function openTerminalPanel() {
  await refreshTerminalSessions({ createIfEmpty: true, connect: true });
  resizeAdminTerminal();
  if (isMobileTerminalClient() && mobileInputTrap) {
    mobileInputTrap.focus();
  } else if (xterm) {
    xterm.focus();
  }
  if (usingFallback) terminalFallback.focus();
}

async function connectAdminTerminal(terminalId = activeTerminalId) {
  if (!adminTerminalAvailable || !terminalId) return;
  await initAdminTerminal();
  if (
    terminalWs &&
    terminalWsId === terminalId &&
    terminalWs.readyState <= WebSocket.OPEN
  ) {
    return;
  }
  detachCurrentTerminalWs();
  activeTerminalId = terminalId;
  renderTerminalSessionTabs();
  resetTerminalSurface();
  terminalOutputCursor = 0;
  if (isMobileTerminalClient()) {
    startTerminalHttpFallback(terminalId);
    connectTerminalWs(terminalId, { background: true });
    return;
  }
  connectTerminalWs(terminalId, { background: false });
}

function connectTerminalWs(terminalId, { background = false } = {}) {
  const dims = terminalDimensions();
  const qs = new URLSearchParams(params);
  qs.set('cols', String(dims.cols));
  qs.set('rows', String(dims.rows));
  qs.set('terminal_id', terminalId);
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = scheme + '//' + location.host + apiBase + '/admin/terminal/ws?' + qs.toString();
  if (!background) setTerminalStatus('Connecting...');
  let ws = null;
  try {
    ws = new WebSocket(url);
  } catch (_) {
    if (!background) startTerminalHttpFallback(terminalId);
    return;
  }
  terminalWs = ws;
  terminalWsId = terminalId;
  clearTerminalConnectTimer();
  terminalConnectTimer = setTimeout(() => {
    if (terminalWs !== ws || ws.readyState !== WebSocket.CONNECTING) return;
    if (background) abandonTerminalWs(ws);
    else fallbackTerminalFromWs(ws, terminalId, 'Connecting via HTTP...');
  }, 10000);
  ws.onopen = () => {
    if (terminalWs !== ws) return;
    clearTerminalConnectTimer();
    stopTerminalHttpPolling();
    terminalTransport = 'ws';
    setTerminalStatus('Connected');
    terminalDisconnect.disabled = false;
    resizeAdminTerminal();
  };
  ws.onmessage = (ev) => {
    if (terminalWs !== ws) return;
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'session' && msg.terminal) {
        activeTerminalId = msg.terminal.id || activeTerminalId;
        renderTerminalSessionTabs();
      }
      if (msg.type === 'output') {
        const output = msg.data || '';
        if (msg.replay) {
          const base = Number(msg.base_cursor || 0);
          const offset = Math.max(0, terminalOutputCursor - base);
          if (offset < output.length) writeTerminal(output.slice(offset));
        } else {
          writeTerminal(output);
        }
        if (typeof msg.cursor === 'number') {
          terminalOutputCursor = Math.max(terminalOutputCursor, msg.cursor);
        } else {
          terminalOutputCursor += output.length;
        }
      }
      if (msg.type === 'exit') {
        setTerminalStatus(msg.reason ? ('Closed: ' + msg.reason) : 'Closed');
        terminalDisconnect.disabled = true;
        refreshTerminalSessions({ createIfEmpty: false, connect: false });
      }
    } catch (_) {}
  };
  ws.onerror = () => {
    if (terminalWs !== ws) return;
    if (background) abandonTerminalWs(ws);
    else fallbackTerminalFromWs(ws, terminalId, 'Connecting via HTTP...');
  };
  ws.onclose = () => {
    if (terminalWs !== ws) return;
    clearTerminalConnectTimer();
    terminalWs = null;
    terminalWsId = '';
    if (!terminalHttpPolling) terminalDisconnect.disabled = true;
    if (background && terminalHttpPolling) return;
    if (terminalStatus.textContent === 'Connected') {
      setTerminalStatus('Reconnecting via HTTP...');
      startTerminalHttpFallback(terminalId);
    } else if (terminalStatus.textContent === 'Connecting...') {
      startTerminalHttpFallback(terminalId);
    }
  };
}

async function disconnectAdminTerminal() {
  if (!activeTerminalId) return;
  const terminalId = activeTerminalId;
  setTerminalStatus('Closing...');
  try {
    const data = await terminalApi(
      '/sessions/' + encodeURIComponent(terminalId) + '/close',
      { method: 'POST' }
    );
    detachCurrentTerminalWs();
    applyTerminalSessionPayload(data);
    resetTerminalSurface();
    if (activeTerminalId) {
      await connectAdminTerminal(activeTerminalId);
    } else {
      setTerminalStatus('No terminal');
    }
  } catch (err) {
    setTerminalStatus('Error: ' + (err && err.message ? err.message : err));
  }
}

terminalNew.addEventListener('click', () => createTerminalSession({ connect: true }));
terminalSessionSelect.addEventListener('change', () => {
  const terminalId = terminalSessionSelect.value;
  if (!terminalId) return;
  activeTerminalId = terminalId;
  connectAdminTerminal(terminalId);
});
terminalDisconnect.addEventListener('click', disconnectAdminTerminal);

// Mobile copy FAB handler
const mobileCopyFab = document.getElementById('terminal-mobile-copy-fab');
if (mobileCopyFab) {
  mobileCopyFab.addEventListener('click', () => {
    const sel = xterm ? xterm.getSelection() : '';
    if (sel) {
      terminalCopyText(sel);
      setTerminalStatus('Copied ' + sel.length + ' chars');
    }
    mobileCopyFab.style.display = 'none';
    if (xterm) xterm.focus();
  });
}


// Paste-trap input handlers (for HTTP fallback paste)
const pasteTrap = document.getElementById('terminal-paste-trap');
if (pasteTrap) {
  pasteTrap.addEventListener('paste', (e) => {
    if (!awaitingTermPaste) return;
    awaitingTermPaste = false;
    const text = e.clipboardData && e.clipboardData.getData('text');
    if (text) sendTerminalInput(text);
    e.preventDefault();
    if (xterm) xterm.focus();
  });
  pasteTrap.addEventListener('blur', () => {
    awaitingTermPaste = false;
    if (xterm) xterm.focus();
  });
  pasteTrap.addEventListener('keydown', (e) => {
    if (e.key === 'v' && (e.ctrlKey || e.metaKey)) return; // allow Ctrl+V
    awaitingTermPaste = false;
    pasteTrap.value = '';
    e.preventDefault();
    if (e.key.length === 1) sendTerminalInput(e.key);
    if (xterm) xterm.focus();
  });
}

// Click outside context menu to close
document.addEventListener('click', (e) => {
  const menu = document.getElementById('terminal-ctx-menu');
  if (menu && menu.style.display !== 'none') {
    if (!menu.contains(e.target)) hideTerminalContextMenu();
  }
});
document.addEventListener('contextmenu', (e) => {
  const menu = document.getElementById('terminal-ctx-menu');
  if (menu && menu.style.display !== 'none') {
    if (!menu.contains(e.target)) hideTerminalContextMenu();
  }
});


// --- Mobile input trap (independent textarea for mobile keyboard capture) ---
const mobileInputTrap = document.getElementById('mobile-input-trap');
let mobileTrapComposing = false;

if (mobileInputTrap && isMobileTerminalClient()) {
  // On mobile, clicking the xterm area should focus the input trap instead
  // of xterm's internal textarea for better keyboard/IME control.
  xtermHost.addEventListener('click', () => {
    if (mobileCopyModeOpen) return;
    focusMobileTrap();
  });

  function focusMobileTrap() {
    if (!mobileInputTrap) return;
    mobileInputTrap.focus({ preventScroll: true });
  }

  mobileInputTrap.addEventListener('compositionstart', () => {
    mobileTrapComposing = true;
  });

  mobileInputTrap.addEventListener('compositionend', () => {
    mobileTrapComposing = false;
    const value = mobileInputTrap.value;
    if (value) {
      sendTerminalInput(value);
      mobileInputTrap.value = '';
    }
  });

  mobileInputTrap.addEventListener('input', () => {
    if (mobileTrapComposing) return;
    const value = mobileInputTrap.value;
    if (value) {
      sendTerminalInput(value);
      mobileInputTrap.value = '';
    }
  });

  mobileInputTrap.addEventListener('keydown', (e) => {
    if (mobileTrapComposing) return;
    const mapped = {
      Enter: '\\r',
      Backspace: '\\x7f',
      Tab: '\\t',
      Escape: '\\x1b',
      ArrowUp: '\\x1b[A',
      ArrowDown: '\\x1b[B',
      ArrowRight: '\\x1b[C',
      ArrowLeft: '\\x1b[D',
      Home: '\\x1b[H',
      End: '\\x1b[F',
      Delete: '\\x1b[3~',
    }[e.key];
    if (!mapped) return;
    e.preventDefault();
    mobileInputTrap.value = '';
    sendTerminalInput(mapped);
  });

  mobileInputTrap.addEventListener('paste', (e) => {
    const text = e.clipboardData && e.clipboardData.getData('text');
    if (!text) return;
    e.preventDefault();
    mobileInputTrap.value = '';
    // Exit paste-ready visual state
    mobileInputTrap.classList.remove('paste-ready');
    mobileInputTrap.placeholder = '';
    awaitingTermPaste = false;
    sendTerminalInput(text);
    // Return focus to xterm after paste
    if (xterm) setTimeout(() => xterm.focus(), 0);
  });
  mobileInputTrap.addEventListener('blur', () => {
    // Clean up paste-ready state when trap loses focus
    if (mobileInputTrap.classList.contains('paste-ready')) {
      mobileInputTrap.classList.remove('paste-ready');
      mobileInputTrap.placeholder = '';
      awaitingTermPaste = false;
    }
  });

  // When xterm's own textarea gets focus on mobile, redirect to input trap
  const xtermTextarea = xtermHost.querySelector('textarea');
  if (xtermTextarea) {
    xtermTextarea.addEventListener('focus', () => {
      if (isMobileTerminalClient() && !mobileCopyModeOpen) {
        xtermTextarea.blur();
        focusMobileTrap();
      }
    });
  }
}

// --- Virtual key bar for mobile / webview ---
let vkeyCtrlActive = false;
const vkeyCtrlBtn = document.getElementById('vkey-ctrl');

function setVkeyCtrl(active) {
  vkeyCtrlActive = active;
  if (vkeyCtrlBtn) vkeyCtrlBtn.classList.toggle('active', active);
}

// Intercept xterm input: when Ctrl is toggled, convert a-z to Ctrl+A-Z
const _origSendTerminalInput = sendTerminalInput;
sendTerminalInput = function(data) {
  if (vkeyCtrlActive && data.length === 1) {
    const code = data.charCodeAt(0);
    if (code >= 97 && code <= 122) {
      setVkeyCtrl(false);
      _origSendTerminalInput(String.fromCharCode(code - 96));
      return;
    }
    if (code >= 65 && code <= 90) {
      setVkeyCtrl(false);
      _origSendTerminalInput(String.fromCharCode(code - 64));
      return;
    }
  }
  if (vkeyCtrlActive) setVkeyCtrl(false);
  _origSendTerminalInput(data);
};

function vkeySend(seq) {
  sendTerminalInput(seq);
  if (isMobileTerminalClient() && mobileInputTrap) {
    mobileInputTrap.focus({ preventScroll: true });
  } else if (xterm) {
    xterm.focus();
  }
}

if (vkeyCtrlBtn) vkeyCtrlBtn.addEventListener('click', () => {
  setVkeyCtrl(!vkeyCtrlActive);
  if (xterm) xterm.focus();
});
document.getElementById('vkey-tab')?.addEventListener('click', () => vkeySend('\t'));
document.getElementById('vkey-esc')?.addEventListener('click', () => vkeySend('\x1b'));
document.getElementById('vkey-up')?.addEventListener('click', () => vkeySend('\x1b[A'));
document.getElementById('vkey-down')?.addEventListener('click', () => vkeySend('\x1b[B'));
document.getElementById('vkey-cc')?.addEventListener('click', () => vkeySend('\x03'));
document.getElementById('vkey-cb')?.addEventListener('click', () => vkeySend('\x02'));
// ── Overlay scrollbar ──────────────────────────────────────────────────────
let _sbThumb = null, _sbDragging = false, _sbRaf = 0;
let _sbStartY = 0, _sbThumbTop0 = 0, _sbLastPageDir = 0, _sbPageTimer = null;

function initTermScrollbar() {
  _sbThumb = document.getElementById('term-scrollbar-thumb');
  if (!_sbThumb) return;

  function thumbPos() { return parseFloat(_sbThumb.style.top) || 0; }
  function thumbH() { return _sbThumb.clientHeight || 30; }
  function trackH() { return _sbThumb.parentElement.clientHeight || 200; }

  function down(clientY) {
    _sbDragging = true;
    _sbStartY = clientY;
    _sbThumbTop0 = thumbPos();
    _sbLastPageDir = 0;
    _sbThumb.classList.add('active');
  }
  function move(clientY) {
    if (!_sbDragging) return;
    const dy = clientY - _sbStartY;
    const tH = trackH(), hH = thumbH();
    const maxTop = tH - hH;
    let newTop = _sbThumbTop0 + dy;
    newTop = Math.max(0, Math.min(maxTop, newTop));
    _sbThumb.style.top = Math.round(newTop) + 'px';

    if (boundXtermVp && boundXtermVp.scrollHeight > boundXtermVp.clientHeight + 2) {
      // Viewport overflow — scroll directly
      const maxScroll = boundXtermVp.scrollHeight - boundXtermVp.clientHeight;
      const scrollTarget = maxTop > 0 ? (newTop / maxTop) * maxScroll : 0;
      boundXtermVp.scrollTop = scrollTarget;
    } else {
      // No overflow (tmux) — drag past center sends repeated PgUp/PgDn
      const mid = tH / 2;
      const dir = newTop + hH/2 < mid ? -1 : 1;
      if (dir !== _sbLastPageDir) {
        _sbLastPageDir = dir;
        tmuxAwarePageKey(dir < 0 ? '\x1b[5~' : '\x1b[6~');
        clearInterval(_sbPageTimer);
        _sbPageTimer = setInterval(() => {
          if (!_sbDragging) { clearInterval(_sbPageTimer); return; }
          tmuxAwarePageKey(dir < 0 ? '\x1b[5~' : '\x1b[6~');
        }, 400);
      }
    }
  }
  function up() {
    if (!_sbDragging) return;
    _sbDragging = false;
    _sbThumb.classList.remove('active');
    clearInterval(_sbPageTimer);
    // If tmux: snap thumb back to top after drag
    if (!boundXtermVp || boundXtermVp.scrollHeight <= boundXtermVp.clientHeight + 2) {
      _sbThumb.style.top = '0px';
    }
  }

  _sbThumb.addEventListener('mousedown', e => { e.preventDefault(); down(e.clientY); });
  document.addEventListener('mousemove', e => { if (_sbDragging) move(e.clientY); });
  document.addEventListener('mouseup', up);
  _sbThumb.addEventListener('touchstart', e => { e.preventDefault(); const t=e.touches[0]; if(t) down(t.clientY); }, {passive:false});
  _sbThumb.addEventListener('touchmove', e => { if(!_sbDragging) return; e.preventDefault(); const t=e.touches[0]; if(t) move(t.clientY); }, {passive:false});
  _sbThumb.addEventListener('touchend', up);
  _sbThumb.addEventListener('touchcancel', up);
}

function updateScrollbarThumb() {
  if (_sbRaf) return;
  _sbRaf = requestAnimationFrame(() => {
    _sbRaf = 0;
    if (!_sbThumb) return;
    const tH = _sbThumb.parentElement ? _sbThumb.parentElement.clientHeight : 0;
    if (tH === 0) return;
    if (boundXtermVp && boundXtermVp.scrollHeight > boundXtermVp.clientHeight + 2) {
      const cH = boundXtermVp.clientHeight, sH = boundXtermVp.scrollHeight;
      const sT = boundXtermVp.scrollTop;
      const hH = Math.max(30, Math.floor(tH * cH / sH));
      const maxTop = tH - hH;
      const maxScroll = sH - cH;
      const top = maxScroll > 0 ? (sT / maxScroll) * maxTop : 0;
      _sbThumb.style.height = hH + 'px';
      _sbThumb.style.top = Math.round(top) + 'px';
    } else {
      // No overflow (tmux) — small fixed thumb at top
      _sbThumb.style.height = Math.max(30, Math.floor(tH * 0.3)) + 'px';
      _sbThumb.style.top = '0px';
    }
  });
}

// PgUp/PgDn — tmux needs copy-mode first; other apps handle PgUp/PgDn directly.
// When mouse tracking is active (tmux/vim), we send C-b [ to enter copy-mode
// then the page key. Without mouse tracking, just send the page key.
function tmuxAwarePageKey(pageSeq) {
  // pageSeq: '\x1b[5~' for PgUp or '\x1b[6~' for PgDn
  const inTmux = xterm && xterm._core && xterm._core.coreMouseService &&
                 xterm._core.coreMouseService.areMouseEventsActive;
  if (inTmux) {
    // Send C-b [ (tmux prefix + copy-mode), wait briefly, then send page key
    vkeySend('\x02');       // C-b = tmux prefix
    setTimeout(() => {
      vkeySend('[');          // enter copy-mode
      setTimeout(() => {
        vkeySend(pageSeq);    // PgUp/PgDn works in copy-mode
      }, 50);
    }, 50);
  } else {
    vkeySend(pageSeq);
  }
}

document.getElementById('vkey-pgup')?.addEventListener('click', () => {
  tmuxAwarePageKey('\x1b[5~');
});
document.getElementById('vkey-pgdn')?.addEventListener('click', () => {
  tmuxAwarePageKey('\x1b[6~');
});

window.addEventListener('resize', resizeAdminTerminal);

const streamBoxes = new Map();
const thinkingBoxes = new Map();
const progressLines = new Map();

function ensureEmptyHintRemoved() {
  const el = document.getElementById('empty-hint');
  if (el) el.remove();
}

function renderHermesBox(text, { streaming = false, withCaret = false } = {}) {
  const box = document.createElement('div');
  box.className = 'hermes-box' + (streaming ? ' streaming' : '');
  const body = document.createElement('div');
  body.className = 'hermes-body';
  body.textContent = text || '';
  if (withCaret) {
    const caret = document.createElement('span');
    caret.className = 'caret';
    caret.textContent = '▍';
    body.appendChild(caret);
  }
  const head = document.createElement('div');
  head.className = 'hermes-title';
  head.textContent = '╭─ ⚕ Hermes ─────────────────';
  const foot = document.createElement('div');
  foot.className = 'hermes-title';
  foot.textContent = '╰────────────────────────────────';
  box.appendChild(head);
  box.appendChild(body);
  box.appendChild(foot);
  return { box, body };
}

function renderThinkingBox(text, { streaming = false, withCaret = false } = {}) {
  const box = document.createElement('div');
  box.className = 'thinking-box' + (streaming ? ' streaming' : '');
  const head = document.createElement('div');
  head.className = 'thinking-title';
  head.textContent = '╭─ thinking ─────────────────';
  const body = document.createElement('div');
  body.className = 'thinking-body';
  body.textContent = text || '';
  if (withCaret) {
    const caret = document.createElement('span');
    caret.className = 'caret';
    caret.textContent = '▍';
    body.appendChild(caret);
  }
  box.appendChild(head);
  box.appendChild(body);
  return { box, body };
}

function appendBlock(block) {
  gotTerminalLines = true;
  ensureEmptyHintRemoved();
  const kind = block.line_kind;

  if (kind === 'user') {
    const p = document.createElement('div');
    p.className = 'user-line';
    const dot = document.createElement('span');
    dot.className = 'bullet';
    dot.textContent = '●';
    p.appendChild(dot);
    const txt = document.createElement('span');
    txt.textContent = block.text || '';
    p.appendChild(txt);
    terminal.appendChild(p);
  } else if (kind === 'hermes' && block.stream_id) {
    let entry = streamBoxes.get(block.stream_id);
    if (!entry) {
      const made = renderHermesBox(block.text || '', { streaming: !block.final, withCaret: !block.final });
      terminal.appendChild(made.box);
      entry = made;
      streamBoxes.set(block.stream_id, entry);
    } else {
      entry.body.textContent = block.text || '';
      if (block.final) {
        entry.box.classList.remove('streaming');
      } else {
        const caret = document.createElement('span');
        caret.className = 'caret';
        caret.textContent = '▍';
        entry.body.appendChild(caret);
      }
    }
    if (block.final) streamBoxes.delete(block.stream_id);
  } else if (kind === 'hermes') {
    const made = renderHermesBox(block.text || '', { streaming: false, withCaret: false });
    terminal.appendChild(made.box);
  } else if (kind === 'thinking' && block.stream_id) {
    let entry = thinkingBoxes.get(block.stream_id);
    if (!entry) {
      const made = renderThinkingBox(block.text || '', { streaming: !block.final, withCaret: !block.final });
      terminal.appendChild(made.box);
      entry = made;
      thinkingBoxes.set(block.stream_id, entry);
    } else {
      entry.body.textContent = block.text || '';
      if (block.final) {
        entry.box.classList.remove('streaming');
      } else {
        const caret = document.createElement('span');
        caret.className = 'caret';
        caret.textContent = '▍';
        entry.body.appendChild(caret);
      }
    }
    if (block.final) thinkingBoxes.delete(block.stream_id);
  } else if (kind === 'thinking') {
    const made = renderThinkingBox(block.text || '', { streaming: false, withCaret: false });
    terminal.appendChild(made.box);
  } else if (kind === 'interim') {
    const p = document.createElement('div');
    p.className = 'interim-line';
    p.textContent = block.text || '';
    terminal.appendChild(p);
  } else if (kind === 'tool_progress') {
    const key = block.tool_call_id || ('tp:' + (block.seq || 0));
    let p = progressLines.get(key);
    if (!p) {
      p = document.createElement('div');
      p.className = 'tool-progress';
      terminal.appendChild(p);
      progressLines.set(key, p);
    }
    p.textContent = block.text || '';
    if (block.stage === 'end') {
      p.classList.add('is-done');
      progressLines.delete(key);
    }
  } else if (kind === 'status') {
    const p = document.createElement('div');
    p.className = 'status-line';
    p.textContent = block.text || '';
    terminal.appendChild(p);
  } else {
    const p = document.createElement('div');
    p.className = 'tool-line';
    p.textContent = block.text || '';
    terminal.appendChild(p);
  }
  if (autoFollow) {
    terminal.scrollTop = terminal.scrollHeight;
  }
  updateScrollButton();
}

function resetRenderState() {
  streamBoxes.clear();
  thinkingBoxes.clear();
  progressLines.clear();
}

let streamReconnectTimer = null;
let streamReconnectBackoffMs = 1000;
const STREAM_RECONNECT_MAX_MS = 30000;

function scheduleStreamReconnect() {
  if (streamReconnectTimer) return;
  const delay = streamReconnectBackoffMs;
  streamReconnectBackoffMs = Math.min(
    STREAM_RECONNECT_MAX_MS,
    Math.max(1000, streamReconnectBackoffMs * 2)
  );
  streamReconnectTimer = setTimeout(() => {
    streamReconnectTimer = null;
    connectStream();
  }, delay);
}

function connectStream() {
  if (!sessionId) return;
  if (streamReconnectTimer) {
    clearTimeout(streamReconnectTimer);
    streamReconnectTimer = null;
  }
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  const streamQs = params.toString();
  const url = apiBase + '/stream?session_id=' + encodeURIComponent(sessionId)
    + '&cursor=' + lineCursor + (streamQs ? '&' + streamQs : '');
  eventSource = new EventSource(url);
  eventSource.onopen = () => {
    streamReconnectBackoffMs = 1000;
  };
  eventSource.onmessage = (msg) => {
    try {
      const block = JSON.parse(msg.data);
      // Guard against duplicate replays on browser-initiated reconnect:
      // the EventSource may resend buffered events from before `lineCursor`.
      if (typeof block.seq === 'number' && block.seq <= lineCursor) return;
      if (typeof block.seq === 'number') lineCursor = block.seq;
      appendBlock(block);
    } catch (_) {}
  };
  eventSource.onerror = () => {
    // Force the next reconnect to use the most recent lineCursor so we don't
    // re-receive (and re-render) events the client already processed.
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    scheduleStreamReconnect();
  };
}

async function loadHistory() {
  if (!sessionId) return;
  const r = await fetch(
    apiBase + '/history?session_id=' + encodeURIComponent(sessionId)
    + '&cursor=' + lineCursor + '&' + params.toString()
  );
  if (!r.ok) return;
  const data = await r.json();
  const blocks = data.lines || [];
  if (!blocks.length) return;
  blocks.forEach(block => {
    if (block.seq <= lineCursor) return;
    lineCursor = block.seq;
    appendBlock(block);
  });
}

function updateMetaLine(info) {
  const who = info.user_id ? (' | user: ' + info.user_id) : '';
  const lines = info.terminal_lines != null ? (' | lines: ' + info.terminal_lines) : '';
  document.getElementById('meta-line').textContent =
    (info.canonical_chat_id || '') + who + ' | session: ' +
    (info.session_id || '(pending)') + ' | ' +
    (info.status || 'waiting') + lines;
}

function updateEmptyHint(info) {
  if (gotTerminalLines) return;
  const el = document.getElementById('empty-hint');
  if (!el) return;
  if (info.status === 'ended' && (info.terminal_lines || 0) === 0) {
    el.textContent = 'Session ended with no captured activity. Send a new message in 如流 to start a fresh turn.';
  } else if (!info.session_id) {
    el.textContent = 'Waiting for session activity…';
  } else if ((info.terminal_lines || 0) === 0) {
    el.textContent = 'Connected — waiting for agent output…';
  }
}

function updateAdminTerminalAvailability(info) {
  adminTerminalAvailable = !!(
    info &&
    info.viewer_is_admin &&
    info.terminal_enabled &&
    (info.chat_type === 1 || info.chat_type === 7)
  );
  tabs.classList.toggle('visible', adminTerminalAvailable);
  renderTerminalSessionTabs();
  if (!adminTerminalAvailable) {
    setTerminalStatus('Terminal disabled');
    if (terminalPanel.classList.contains('active')) selectTab('tracker');
    detachCurrentTerminalWs();
    terminalSessions = [];
    activeTerminalId = '';
    renderTerminalSessionTabs();
  } else if (!terminalWs && terminalStatus.textContent === 'Terminal disabled') {
    setTerminalStatus('Ready');
  }
}

async function applyResolve(info) {
  const prev = sessionId;
  document.getElementById('title').textContent = info.label || 'Session Tracker';
  updateMetaLine(info);
  updateEmptyHint(info);
  updateAdminTerminalAvailability(info);
  if (!info.session_id) {
    sessionId = '';
    return;
  }
  const changed = info.session_id !== prev;
  if (changed) {
    sessionId = info.session_id;
    lineCursor = 0;
    gotTerminalLines = false;
    resetRenderState();
    document.getElementById('terminal-wrap').innerHTML =
      '<p class="empty" id="empty-hint">Loading…</p>';
    connectStream();
    await loadHistory();
  } else if (!eventSource) {
    sessionId = info.session_id;
    connectStream();
    await loadHistory();
  } else {
    sessionId = info.session_id;
    if (!gotTerminalLines) await loadHistory();
  }
  if (!gotTerminalLines) updateEmptyHint(info);
}

function startResolvePoll(qs) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const r = await fetch(apiBase + '/resolve?' + qs);
    if (!r.ok) return;
    applyResolve(await r.json());
  }, 2000);
}

async function init() {
  const qs = params.toString();
  const r = await fetch(apiBase + '/resolve?' + qs);
  if (!r.ok) {
    document.getElementById('meta-line').textContent = 'Error: ' + (await r.text());
    return;
  }
  applyResolve(await r.json());
  startResolvePoll(qs);
}
init();
</script>
</body>
</html>
"""


def register_sessiontracker_routes(
    app: Any,
    tracker: SessionTracker,
    *,
    base_path: str,
) -> None:
    """Mount Session Tracker routes on the webhook aiohttp app."""
    if not sessiontracker_enabled():
        return

    base = base_path.rstrip("/")
    root = f"{base}/sessiontracker"

    @_require_sessiontracker_params
    async def page(request: Any, **kw: Any) -> Any:
        from aiohttp import web
        return web.Response(text=_SESSIONTRACKER_HTML, content_type="text/html")

    async def static_asset(request: Any) -> Any:
        from aiohttp import web

        rel_path = request.match_info.get("path", "")
        path = _static_asset_path(rel_path)
        if path is None:
            return web.Response(status=404, text="asset not found")
        return web.FileResponse(path)

    @_require_sessiontracker_params
    async def api_resolve(request: Any, **kw: Any) -> Any:
        from aiohttp import web

        chat_type = kw["chat_type"]
        chat_id = kw["chat_id"]
        code = kw["code"]
        account, account_error = _account_for_sessiontracker_request(chat_type, code)
        if account_error:
            return web.Response(status=500, text=account_error)
        try:
            info = await resolve_target(
                tracker,
                chat_type=chat_type,
                chat_id=chat_id,
                code=code,
                account=account,
            )
        except InfoflowAPIError as exc:
            return web.Response(status=403, text=str(exc))
        except ValueError as exc:
            return web.Response(status=400, text=str(exc))
        viewer_user_id = await _viewer_admin_user_id(code=code, account=account)
        viewer_is_admin = bool(viewer_user_id)
        terminal_block_reason = _terminal_block_reason(
            request,
            chat_type=chat_type,
            viewer_is_admin=viewer_is_admin,
        )
        info["viewer_is_admin"] = viewer_is_admin
        info["terminal_enabled"] = terminal_block_reason is None
        info["terminal_block_reason"] = terminal_block_reason
        return web.json_response(info)

    @_require_sessiontracker_params
    async def api_stream(request: Any, **kw: Any) -> Any:
        from aiohttp import web

        sid = request.rel_url.query.get("session_id", "").strip()
        if not sid:
            return web.Response(status=400, text="session_id required")
        if tracker.get_meta(sid) is None and sid not in tracker._events:  # noqa: SLF001
            return web.Response(status=404, text="session not found")

        chat_type = kw["chat_type"]
        chat_id = kw["chat_id"]
        code = kw["code"]
        account, account_error = _account_for_sessiontracker_request(chat_type, code)
        if account_error:
            return web.Response(status=500, text=account_error)
        try:
            canonical = await canonical_for_stream_access(
                tracker,
                session_id=sid,
                chat_type=chat_type,
                chat_id=chat_id,
                code=code,
                account=account,
            )
        except InfoflowAPIError as exc:
            return web.Response(status=403, text=str(exc))
        except ValueError as exc:
            return web.Response(status=400, text=str(exc))

        if not session_matches_target(tracker, sid, canonical):
            return web.Response(status=403, text="session_id does not match target")

        show_full_user_message = await _viewer_can_see_full_user_message(
            code=code,
            account=account,
        )

        try:
            cursor = _parse_cursor(request.rel_url.query.get("cursor", "0"))
        except ValueError as exc:
            return web.Response(status=400, text=str(exc))

        response = web.StreamResponse(status=200, headers=SSE_RESPONSE_HEADERS)
        await response.prepare(request)

        # Subscribe BEFORE backfill so events that arrive between the snapshot
        # iteration and the queue join are not dropped. We dedupe by seq when
        # draining the queue so events covered by the backfill are not resent.
        q = tracker.subscribe(sid)
        try:
            seq_cursor = cursor
            for block in collect_terminal_blocks(
                tracker,
                sid,
                cursor=cursor,
                show_full_user_message=show_full_user_message,
            ):
                seq_cursor = max(seq_cursor, int(block.get("seq", 0)))
                payload = json.dumps(block, ensure_ascii=False, default=str)
                if not await write_sse(
                    response,
                    f"data: {payload}\n\n".encode(),
                    logger=logger,
                    context="sessiontracker backfill",
                ):
                    return response

            while True:
                try:
                    ev = await asyncio.wait_for(
                        q.get(),
                        timeout=SSE_HEARTBEAT_INTERVAL_SECONDS,
                    )
                except TimeoutError:
                    if not await write_sse(
                        response,
                        SSE_HEARTBEAT,
                        logger=logger,
                        context="sessiontracker heartbeat",
                    ):
                        break
                    continue
                if ev is None:
                    break
                if ev.kind not in TERMINAL_EVENT_KINDS:
                    continue
                if ev.seq <= seq_cursor:
                    continue
                block = event_to_terminal_dict(
                    ev,
                    show_full_user_message=show_full_user_message,
                )
                if block is None:
                    continue
                seq_cursor = ev.seq
                payload = json.dumps(block, ensure_ascii=False, default=str)
                if not await write_sse(
                    response,
                    f"data: {payload}\n\n".encode(),
                    logger=logger,
                    context="sessiontracker live",
                ):
                    break
        finally:
            tracker.unsubscribe(sid, q)
        return response

    @_require_sessiontracker_params
    async def api_history(request: Any, **kw: Any) -> Any:
        from aiohttp import web

        sid = request.rel_url.query.get("session_id", "").strip()
        if not sid:
            return web.Response(status=400, text="session_id required")
        if tracker.get_meta(sid) is None and sid not in tracker._events:  # noqa: SLF001
            return web.Response(status=404, text="session not found")

        chat_type = kw["chat_type"]
        chat_id = kw["chat_id"]
        code = kw["code"]
        account, account_error = _account_for_sessiontracker_request(chat_type, code)
        if account_error:
            return web.Response(status=500, text=account_error)
        try:
            canonical = await canonical_for_stream_access(
                tracker,
                session_id=sid,
                chat_type=chat_type,
                chat_id=chat_id,
                code=code,
                account=account,
            )
        except InfoflowAPIError as exc:
            return web.Response(status=403, text=str(exc))
        except ValueError as exc:
            return web.Response(status=400, text=str(exc))

        if not session_matches_target(tracker, sid, canonical):
            return web.Response(status=403, text="session_id does not match target")

        show_full_user_message = await _viewer_can_see_full_user_message(
            code=code,
            account=account,
        )

        try:
            cursor = _parse_cursor(request.rel_url.query.get("cursor", "0"))
        except ValueError as exc:
            return web.Response(status=400, text=str(exc))

        return web.json_response({
            "session_id": sid,
            "lines": collect_terminal_blocks(
                tracker,
                sid,
                cursor=cursor,
                show_full_user_message=show_full_user_message,
            ),
        })

    @_require_sessiontracker_params
    async def api_admin_terminal_sessions(request: Any, **kw: Any) -> Any:
        from aiohttp import web

        chat_type = kw["chat_type"]
        code = kw["code"]
        account, account_error = _account_for_sessiontracker_request(chat_type, code)
        if account_error:
            return web.Response(status=500, text=account_error)
        try:
            viewer_user_id, terminal_error = await _require_terminal_admin_user_id(
                request,
                chat_type=chat_type,
                code=code,
                account=account,
            )
        except InfoflowAPIError as exc:
            return web.Response(status=403, text=str(exc))
        if terminal_error:
            _log_terminal_denied(
                request,
                action="list",
                chat_type=chat_type,
                reason=terminal_error,
            )
            return web.Response(status=403, text=terminal_error)
        sessions = await list_terminal_sessions(viewer_user_id)
        ctx = _terminal_log_context(request, chat_type=chat_type)
        logger.info(
            "[infoflow] sessiontracker terminal list viewer=%s remote=%s "
            "chat_type=%s count=%s user_agent=%r",
            viewer_user_id,
            ctx["remote"],
            ctx["chat_type"],
            len(sessions),
            ctx["user_agent"],
        )
        return web.json_response({
            "sessions": sessions,
            "max_sessions": sessiontracker_terminal_max_per_admin(),
            "retention_seconds": sessiontracker_terminal_retention_seconds(),
        })

    @_require_sessiontracker_params
    async def api_admin_terminal_new(request: Any, **kw: Any) -> Any:
        from aiohttp import web

        chat_type = kw["chat_type"]
        code = kw["code"]
        account, account_error = _account_for_sessiontracker_request(chat_type, code)
        if account_error:
            return web.Response(status=500, text=account_error)
        try:
            viewer_user_id, terminal_error = await _require_terminal_admin_user_id(
                request,
                chat_type=chat_type,
                code=code,
                account=account,
            )
        except InfoflowAPIError as exc:
            return web.Response(status=403, text=str(exc))
        if terminal_error:
            _log_terminal_denied(
                request,
                action="create",
                chat_type=chat_type,
                reason=terminal_error,
            )
            return web.Response(status=403, text=terminal_error)
        rows = _parse_terminal_dimension(
            request.rel_url.query.get("rows", "30"),
            30,
            min_value=1,
            max_value=200,
        )
        cols = _parse_terminal_dimension(
            request.rel_url.query.get("cols", "100"),
            100,
            min_value=2,
            max_value=500,
        )
        try:
            terminal = await create_terminal_session(
                viewer_user_id,
                cwd=sessiontracker_terminal_cwd(),
                rows=rows,
                cols=cols,
            )
        except RuntimeError as exc:
            if str(exc) == "terminal_limit_reached":
                return web.Response(status=409, text="terminal limit reached")
            raise
        except ValueError as exc:
            return web.Response(status=400, text=str(exc))
        sessions = await list_terminal_sessions(viewer_user_id)
        ctx = _terminal_log_context(request, chat_type=chat_type)
        logger.info(
            "[infoflow] sessiontracker terminal create request viewer=%s remote=%s "
            "chat_type=%s id=%s cwd=%s rows=%s cols=%s user_agent=%r",
            viewer_user_id,
            ctx["remote"],
            ctx["chat_type"],
            terminal.get("id"),
            terminal.get("cwd"),
            terminal.get("rows"),
            terminal.get("cols"),
            ctx["user_agent"],
        )
        return web.json_response({
            "terminal": terminal,
            "sessions": sessions,
            "max_sessions": sessiontracker_terminal_max_per_admin(),
            "retention_seconds": sessiontracker_terminal_retention_seconds(),
        })

    @_require_sessiontracker_params
    async def api_admin_terminal_close(request: Any, **kw: Any) -> Any:
        from aiohttp import web

        chat_type = kw["chat_type"]
        code = kw["code"]
        terminal_id = request.match_info.get("terminal_id", "").strip()
        if not terminal_id:
            return web.Response(status=400, text="terminal_id required")
        account, account_error = _account_for_sessiontracker_request(chat_type, code)
        if account_error:
            return web.Response(status=500, text=account_error)
        try:
            viewer_user_id, terminal_error = await _require_terminal_admin_user_id(
                request,
                chat_type=chat_type,
                code=code,
                account=account,
            )
        except InfoflowAPIError as exc:
            return web.Response(status=403, text=str(exc))
        if terminal_error:
            _log_terminal_denied(
                request,
                action="close",
                chat_type=chat_type,
                reason=terminal_error,
            )
            return web.Response(status=403, text=terminal_error)
        closed = await close_terminal_session(viewer_user_id, terminal_id)
        if not closed:
            return web.Response(status=404, text="terminal not found")
        sessions = await list_terminal_sessions(viewer_user_id)
        ctx = _terminal_log_context(request, chat_type=chat_type)
        logger.info(
            "[infoflow] sessiontracker terminal close request viewer=%s remote=%s "
            "chat_type=%s id=%s user_agent=%r",
            viewer_user_id,
            ctx["remote"],
            ctx["chat_type"],
            terminal_id,
            ctx["user_agent"],
        )
        return web.json_response({
            "closed": True,
            "sessions": sessions,
            "max_sessions": sessiontracker_terminal_max_per_admin(),
            "retention_seconds": sessiontracker_terminal_retention_seconds(),
        })

    @_require_sessiontracker_params
    async def api_admin_terminal_output(request: Any, **kw: Any) -> Any:
        from aiohttp import web

        chat_type = kw["chat_type"]
        code = kw["code"]
        terminal_id = request.match_info.get("terminal_id", "").strip()
        if not terminal_id:
            return web.Response(status=400, text="terminal_id required")
        account, account_error = _account_for_sessiontracker_request(chat_type, code)
        if account_error:
            return web.Response(status=500, text=account_error)
        try:
            viewer_user_id, terminal_error = await _require_terminal_admin_user_id(
                request,
                chat_type=chat_type,
                code=code,
                account=account,
            )
        except InfoflowAPIError as exc:
            return web.Response(status=403, text=str(exc))
        if terminal_error:
            _log_terminal_denied(
                request,
                action="output",
                chat_type=chat_type,
                reason=terminal_error,
            )
            return web.Response(status=403, text=terminal_error)
        try:
            cursor = _parse_cursor(request.rel_url.query.get("cursor", "0"))
        except ValueError as exc:
            return web.Response(status=400, text=str(exc))
        wait_seconds = _parse_terminal_wait(request.rel_url.query.get("wait", "20"))
        try:
            result = await read_terminal_output(
                viewer_user_id,
                terminal_id,
                cursor=cursor,
                wait_seconds=wait_seconds,
                retention_seconds=sessiontracker_terminal_retention_seconds(),
            )
        except KeyError:
            return web.Response(status=404, text="terminal not found")
        ctx = _terminal_log_context(request, chat_type=chat_type)
        logger.info(
            "[infoflow] sessiontracker terminal output viewer=%s remote=%s "
            "chat_type=%s id=%s bytes=%s cursor=%s user_agent=%r",
            viewer_user_id,
            ctx["remote"],
            ctx["chat_type"],
            terminal_id,
            len(str(result.get("output") or "")),
            result.get("cursor"),
            ctx["user_agent"],
        )
        return web.json_response(result)

    @_require_sessiontracker_params
    async def api_admin_terminal_input(request: Any, **kw: Any) -> Any:
        from aiohttp import web

        chat_type = kw["chat_type"]
        code = kw["code"]
        terminal_id = request.match_info.get("terminal_id", "").strip()
        if not terminal_id:
            return web.Response(status=400, text="terminal_id required")
        account, account_error = _account_for_sessiontracker_request(chat_type, code)
        if account_error:
            return web.Response(status=500, text=account_error)
        try:
            viewer_user_id, terminal_error = await _require_terminal_admin_user_id(
                request,
                chat_type=chat_type,
                code=code,
                account=account,
            )
        except InfoflowAPIError as exc:
            return web.Response(status=403, text=str(exc))
        if terminal_error:
            _log_terminal_denied(
                request,
                action="input",
                chat_type=chat_type,
                reason=terminal_error,
            )
            return web.Response(status=403, text=terminal_error)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.Response(status=400, text="json body required")
        data = str(payload.get("data") or "")
        if not data:
            return web.Response(status=400, text="data required")
        ok = await write_terminal_input(viewer_user_id, terminal_id, data)
        if not ok:
            return web.Response(status=404, text="terminal not found")
        ctx = _terminal_log_context(request, chat_type=chat_type)
        logger.info(
            "[infoflow] sessiontracker terminal input viewer=%s remote=%s "
            "chat_type=%s id=%s bytes=%s user_agent=%r",
            viewer_user_id,
            ctx["remote"],
            ctx["chat_type"],
            terminal_id,
            len(data),
            ctx["user_agent"],
        )
        return web.json_response({"ok": True})

    @_require_sessiontracker_params
    async def api_admin_terminal_resize(request: Any, **kw: Any) -> Any:
        from aiohttp import web

        chat_type = kw["chat_type"]
        code = kw["code"]
        terminal_id = request.match_info.get("terminal_id", "").strip()
        if not terminal_id:
            return web.Response(status=400, text="terminal_id required")
        account, account_error = _account_for_sessiontracker_request(chat_type, code)
        if account_error:
            return web.Response(status=500, text=account_error)
        try:
            viewer_user_id, terminal_error = await _require_terminal_admin_user_id(
                request,
                chat_type=chat_type,
                code=code,
                account=account,
            )
        except InfoflowAPIError as exc:
            return web.Response(status=403, text=str(exc))
        if terminal_error:
            _log_terminal_denied(
                request,
                action="resize",
                chat_type=chat_type,
                reason=terminal_error,
            )
            return web.Response(status=403, text=terminal_error)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.Response(status=400, text="json body required")
        rows = _parse_terminal_dimension(
            str(payload.get("rows", "30")),
            30,
            min_value=1,
            max_value=200,
        )
        cols = _parse_terminal_dimension(
            str(payload.get("cols", "100")),
            100,
            min_value=2,
            max_value=500,
        )
        ok = await resize_terminal_session(
            viewer_user_id,
            terminal_id,
            rows=rows,
            cols=cols,
        )
        if not ok:
            return web.Response(status=404, text="terminal not found")
        ctx = _terminal_log_context(request, chat_type=chat_type)
        logger.info(
            "[infoflow] sessiontracker terminal resize viewer=%s remote=%s "
            "chat_type=%s id=%s rows=%s cols=%s user_agent=%r",
            viewer_user_id,
            ctx["remote"],
            ctx["chat_type"],
            terminal_id,
            rows,
            cols,
            ctx["user_agent"],
        )
        return web.json_response({"ok": True, "rows": rows, "cols": cols})

    @_require_sessiontracker_params
    async def api_admin_terminal_ws(request: Any, **kw: Any) -> Any:
        from aiohttp import web

        chat_type = kw["chat_type"]
        code = kw["code"]
        terminal_id = request.rel_url.query.get("terminal_id", "").strip()
        if not terminal_id:
            return web.Response(status=400, text="terminal_id required")
        account, account_error = _account_for_sessiontracker_request(chat_type, code)
        if account_error:
            return web.Response(status=500, text=account_error)
        try:
            viewer_user_id, terminal_error = await _require_terminal_admin_user_id(
                request,
                chat_type=chat_type,
                code=code,
                account=account,
            )
        except InfoflowAPIError as exc:
            return web.Response(status=403, text=str(exc))
        if terminal_error:
            _log_terminal_denied(
                request,
                action="ws",
                chat_type=chat_type,
                reason=terminal_error,
            )
            return web.Response(status=403, text=terminal_error)
        ctx = _terminal_log_context(request, chat_type=chat_type)
        logger.info(
            "[infoflow] sessiontracker terminal ws request viewer=%s remote=%s "
            "chat_type=%s id=%s user_agent=%r",
            viewer_user_id,
            ctx["remote"],
            ctx["chat_type"],
            terminal_id,
            ctx["user_agent"],
        )
        return await run_terminal_websocket(
            request,
            viewer_user_id=viewer_user_id,
            terminal_id=terminal_id,
            retention_seconds=sessiontracker_terminal_retention_seconds(),
        )

    app.router.add_get(root, page)
    app.router.add_get(f"{root}/static/{{path:.*}}", static_asset)
    app.router.add_get(f"{root}/api/resolve", api_resolve)
    app.router.add_get(f"{root}/api/history", api_history)
    app.router.add_get(f"{root}/api/stream", api_stream)
    app.router.add_get(f"{root}/api/admin/terminal/sessions", api_admin_terminal_sessions)
    app.router.add_post(f"{root}/api/admin/terminal/sessions", api_admin_terminal_new)
    app.router.add_post(
        f"{root}/api/admin/terminal/sessions/{{terminal_id}}/close",
        api_admin_terminal_close,
    )
    app.router.add_get(
        f"{root}/api/admin/terminal/sessions/{{terminal_id}}/output",
        api_admin_terminal_output,
    )
    app.router.add_post(
        f"{root}/api/admin/terminal/sessions/{{terminal_id}}/input",
        api_admin_terminal_input,
    )
    app.router.add_post(
        f"{root}/api/admin/terminal/sessions/{{terminal_id}}/resize",
        api_admin_terminal_resize,
    )
    app.router.add_get(f"{root}/api/admin/terminal/ws", api_admin_terminal_ws)
    logger.info("[infoflow] Session Tracker at <host>:<port>%s", root)


__all__ = [
    "TERMINAL_EVENT_KINDS",
    "collect_terminal_blocks",
    "count_terminal_lines",
    "format_terminal_line",
    "event_to_terminal_dict",
    "canonical_for_stream_access",
    "resolve_target",
    "session_matches_target",
    "register_sessiontracker_routes",
    "sessiontracker_enabled",
    "sessiontracker_terminal_enabled",
]
