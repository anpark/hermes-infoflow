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
    request_is_localhost,
    run_terminal_websocket,
    sessiontracker_terminal_cwd,
    sessiontracker_terminal_enabled,
    sessiontracker_terminal_idle_timeout_seconds,
    sessiontracker_terminal_localhost_only,
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
_CODE_USER_CACHE_TTL_SECONDS = int(os.getenv("HERMES_INFOFLOW_CODE_CACHE_TTL", "86400"))
_CODE_USER_CACHE_MAX = int(os.getenv("HERMES_INFOFLOW_CODE_CACHE_MAX", "1024"))
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
            if hermes_meta is not None and hermes_meta.status == "active":
                status = "active"
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
    if not sessiontracker_terminal_enabled():
        return "", "terminal disabled"
    if chat_type not in DM_CHAT_TYPES:
        return "", "terminal is only available for private Session Tracker pages"
    if sessiontracker_terminal_localhost_only() and not request_is_localhost(request):
        return "", "terminal: localhost only"
    viewer_user_id = await _viewer_admin_user_id(code=code, account=account)
    if not viewer_user_id:
        return "", "terminal requires admin viewer code"
    return viewer_user_id, None


_SESSIONTRACKER_CSS = """
:root { --bg: #0c0c0c; --text: #d4d4d4; --muted: #6a737d; --accent: #58a6ff;
  --user: #f0b67f; --hermes-border: #3d5a80; --ok: #3dd68c; --interim: #b48ead; }
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--bg);
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
#terminal-status { color: var(--muted); font-size: 12px; margin-right: auto; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.terminal-button { height: 26px; border: 1px solid #30363d; border-radius: 4px; background: #161b22;
  color: #d4d4d4; padding: 0 9px; font: inherit; cursor: pointer; }
.terminal-button:disabled { opacity: 0.45; cursor: default; }
#xterm-host { flex: 1; min-height: 0; padding: 8px; }
#terminal-fallback { flex: 1; min-height: 0; margin: 0; padding: 10px 12px; overflow: auto;
  background: #050505; color: #d4d4d4; white-space: pre-wrap; outline: none; }
#terminal-fallback.hidden, #xterm-host.hidden { display: none; }
.xterm { height: 100%; }
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
    <span id="terminal-status">Terminal disabled</span>
    <button type="button" id="terminal-connect" class="terminal-button">Connect</button>
    <button type="button" id="terminal-disconnect" class="terminal-button" disabled>Disconnect</button>
  </div>
  <div id="xterm-host"></div>
  <pre id="terminal-fallback" class="hidden" tabindex="0"></pre>
</div>
<button type="button" id="scroll-bottom" title="Scroll to bottom">↓</button>
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
const terminalStatus = document.getElementById('terminal-status');
const terminalConnect = document.getElementById('terminal-connect');
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
    initAdminTerminal().then(() => {
      if (!terminalWs) connectAdminTerminal();
      resizeAdminTerminal();
      if (xterm) xterm.focus();
      if (usingFallback) terminalFallback.focus();
    });
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

function setTerminalStatus(text) {
  terminalStatus.textContent = text;
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
      .then(() => loadScript(staticBase + '/xterm/addon-fit.js'));
  }
  return xtermAssetsPromise;
}

async function initAdminTerminal() {
  if (terminalSurfaceReady) return;
  setTerminalStatus('Loading terminal...');
  try {
    await ensureXtermAssets();
    if (!window.Terminal) throw new Error('xterm unavailable');
    xterm = new window.Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      fontSize: 13,
      scrollback: 8000,
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
    xterm.open(xtermHost);
    xterm.onData(data => sendTerminalInput(data));
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
  }
}

function writeTerminal(data) {
  if (xterm) {
    xterm.write(data || '');
  } else {
    terminalFallback.textContent += data || '';
    terminalFallback.scrollTop = terminalFallback.scrollHeight;
  }
}

function sendTerminalInput(data) {
  if (!terminalWs || terminalWs.readyState !== WebSocket.OPEN) return;
  terminalWs.send(JSON.stringify({ type: 'input', data }));
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

async function connectAdminTerminal() {
  if (!adminTerminalAvailable) return;
  await initAdminTerminal();
  if (terminalWs && terminalWs.readyState <= WebSocket.OPEN) return;
  const dims = terminalDimensions();
  const qs = new URLSearchParams(params);
  qs.set('cols', String(dims.cols));
  qs.set('rows', String(dims.rows));
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = scheme + '//' + location.host + apiBase + '/admin/terminal/ws?' + qs.toString();
  setTerminalStatus('Connecting...');
  terminalConnect.disabled = true;
  terminalWs = new WebSocket(url);
  terminalWs.onopen = () => {
    setTerminalStatus('Connected');
    terminalConnect.disabled = true;
    terminalDisconnect.disabled = false;
    resizeAdminTerminal();
  };
  terminalWs.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'output') writeTerminal(msg.data || '');
      if (msg.type === 'exit') {
        setTerminalStatus(msg.reason ? ('Closed: ' + msg.reason) : 'Closed');
        terminalConnect.disabled = false;
        terminalDisconnect.disabled = true;
      }
    } catch (_) {}
  };
  terminalWs.onerror = () => {
    setTerminalStatus('Connection error');
  };
  terminalWs.onclose = () => {
    terminalWs = null;
    terminalConnect.disabled = false;
    terminalDisconnect.disabled = true;
    if (terminalStatus.textContent === 'Connected') setTerminalStatus('Disconnected');
  };
}

function disconnectAdminTerminal() {
  if (terminalWs) terminalWs.close();
}

terminalConnect.addEventListener('click', connectAdminTerminal);
terminalDisconnect.addEventListener('click', disconnectAdminTerminal);
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
  terminalConnect.disabled = !adminTerminalAvailable || !!terminalWs;
  if (!adminTerminalAvailable) {
    setTerminalStatus('Terminal disabled');
    if (terminalPanel.classList.contains('active')) selectTab('tracker');
    if (terminalWs) terminalWs.close();
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
        viewer_is_admin = await _viewer_is_admin(code=code, account=account)
        info["viewer_is_admin"] = viewer_is_admin
        info["terminal_enabled"] = bool(
            chat_type in DM_CHAT_TYPES
            and viewer_is_admin
            and sessiontracker_terminal_enabled()
        )
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
    async def api_admin_terminal_ws(request: Any, **kw: Any) -> Any:
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
            return web.Response(status=403, text=terminal_error)
        return await run_terminal_websocket(
            request,
            viewer_user_id=viewer_user_id,
            cwd=sessiontracker_terminal_cwd(),
            idle_timeout=sessiontracker_terminal_idle_timeout_seconds(),
        )

    app.router.add_get(root, page)
    app.router.add_get(f"{root}/static/{{path:.*}}", static_asset)
    app.router.add_get(f"{root}/api/resolve", api_resolve)
    app.router.add_get(f"{root}/api/history", api_history)
    app.router.add_get(f"{root}/api/stream", api_stream)
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
