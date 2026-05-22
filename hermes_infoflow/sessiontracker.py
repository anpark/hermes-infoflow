"""Session Tracker Web UI — CLI-style live view for a single Hermes session."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from typing import Any

from .api import InfoflowAccountAPI, InfoflowAPIError, get_user_info_by_code
from .dashboard import SessionEvent, SessionTracker, normalize_chat_id, sessiontracker_enabled

logger = logging.getLogger(__name__)

TERMINAL_EVENT_KINDS = frozenset({
    "display.tool_line",
    "display.hermes",
    "display.status",
    "display.interim",
    "outbound.infoflow",
    "llm.response",
    "tool.end",
    "session.start",
    "session.end",
})

_PROGRESS_LINE_RE = re.compile(r"^[┊\s]*[🔍⚙️💻🌐📁📝🧠✨]")


def format_terminal_line(event: SessionEvent) -> dict[str, Any] | None:
    """Map a tracker event to a terminal render unit for the Web UI."""
    kind = event.kind
    payload = event.payload or {}

    if kind == "display.tool_line":
        line = payload.get("line") or ""
        return {"line_kind": "tool", "text": str(line)}

    if kind == "display.hermes":
        text = payload.get("text") or ""
        return {"line_kind": "hermes", "text": str(text)}

    if kind == "display.status":
        return {"line_kind": "status", "text": str(payload.get("line") or "")}

    if kind == "outbound.infoflow":
        if not payload.get("is_progress_hint"):
            return None
        preview = payload.get("preview") or payload.get("chars")
        return {"line_kind": "tool", "text": f"┊ {preview}" if preview else "┊ …"}

    if kind == "llm.response":
        text = payload.get("assistant_response") or ""
        if text:
            return {"line_kind": "hermes", "text": str(text)}

    if kind == "tool.end" and not payload.get("_skip_fallback"):
        name = payload.get("tool_name") or "tool"
        dur = payload.get("duration_ms")
        dur_s = f" {float(dur) / 1000.0:.1f}s" if dur else ""
        return {"line_kind": "tool", "text": f"┊ ⚙️ {name}{dur_s}"}

    return None


def count_terminal_lines(tracker: SessionTracker, session_id: str) -> int:
    """Count events that render as terminal lines (for session pick ranking)."""
    n = 0
    for ev in tracker.snapshot(session_id, cursor=0):
        if ev.kind not in TERMINAL_EVENT_KINDS:
            continue
        if event_to_terminal_dict(ev) is not None:
            n += 1
    return n


def event_to_terminal_dict(event: SessionEvent) -> dict[str, Any] | None:
    block = format_terminal_line(event)
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
    if chat_type == 2:
        if not raw_chat_id:
            raise ValueError("chatId is required for chatType=2")
        canonical = f"group:{raw_chat_id}"
        label = f"群 {raw_chat_id}"
    elif chat_type == 7:
        if not (code or "").strip():
            raise ValueError("code is required for chatType=7")
        if account is None:
            raise ValueError("Infoflow API account is required for chatType=7")
        user_id = await get_user_info_by_code(
            account, code.strip(), session=http_session,
        )
        canonical = user_id
        label = f"私聊 {user_id}"
    else:
        raise ValueError(f"unsupported chatType={chat_type}")

    session_id = tracker.lookup_session_id(canonical)
    status = "waiting"
    meta = None
    terminal_lines = 0
    if session_id:
        if session_id.startswith("pending:"):
            status = "waiting"
        else:
            meta = tracker.get_meta(session_id)
            status = (meta.status if meta is not None else None) or "active"
            terminal_lines = count_terminal_lines(tracker, session_id)

    return {
        "label": label,
        "canonical_chat_id": canonical,
        "session_id": session_id or "",
        "status": status,
        "chat_type": chat_type,
        "user_id": (meta.user_id if meta else "") or "",
        "terminal_lines": terminal_lines,
    }


def session_matches_target(
    tracker: SessionTracker,
    session_id: str,
    canonical_chat_id: str,
) -> bool:
    """Return whether *session_id* belongs to the resolved *canonical_chat_id*."""
    if not session_id or not canonical_chat_id:
        return False
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
    api_host = os.getenv("INFOFLOW_API_HOST", "").strip()
    app_key = os.getenv("INFOFLOW_APP_KEY", "").strip()
    app_secret = os.getenv("INFOFLOW_APP_SECRET", "").strip()
    agent_raw = os.getenv("INFOFLOW_APP_AGENT_ID", "").strip()
    if not all((api_host, app_key, app_secret, agent_raw)):
        raise ValueError(
            "INFOFLOW_API_HOST, INFOFLOW_APP_KEY, INFOFLOW_APP_SECRET, "
            "INFOFLOW_APP_AGENT_ID are required"
        )
    return InfoflowAccountAPI(
        api_host=api_host,
        app_key=app_key,
        app_secret=app_secret,
        app_agent_id=int(agent_raw),
    )


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
        if chat_type == 7 and not code.strip():
            return web.Response(status=400, text="code is required for chatType=7")
        if chat_type == 2 and not chat_id.strip():
            return web.Response(status=400, text="chatId is required for chatType=2")
        if chat_type not in (2, 7):
            return web.Response(status=400, text="chatType must be 2 or 7")
        return await handler(request, chat_type=chat_type, chat_id=chat_id, code=code)
    return wrapped


_SESSIONTRACKER_CSS = """
:root { --bg: #0c0c0c; --text: #d4d4d4; --muted: #6a737d; --accent: #58a6ff;
  --hermes-border: #3d5a80; --ok: #3dd68c; }
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--bg);
  color: var(--text); font-size: 13px; line-height: 1.5; }
header { padding: 10px 14px; border-bottom: 1px solid #222; background: #111; flex-shrink: 0; }
h1 { margin: 0; font-size: 14px; font-weight: 600; }
#meta-line { color: var(--muted); font-size: 12px; margin-top: 4px; }
#viewport { position: relative; flex: 1; min-height: 0; overflow: hidden; display: flex;
  flex-direction: column; }
#terminal-wrap { flex: 1; overflow-y: auto; padding: 12px 14px 48px; }
.tool-line { color: #9cdcfe; white-space: pre-wrap; word-break: break-word; margin: 2px 0; }
.hermes-box { border: 1px solid var(--hermes-border); border-radius: 4px; margin: 12px 0;
  padding: 8px 10px; background: #141820; }
.hermes-title { color: #7eb8ff; font-size: 12px; margin-bottom: 6px; }
.hermes-body { white-space: pre-wrap; word-break: break-word; }
.status-line { color: var(--muted); margin: 8px 0 4px; font-size: 12px; }
.divider { color: var(--muted); margin: 10px 0; }
#scroll-bottom { display: none; position: fixed; right: 20px; bottom: 20px; width: 44px;
  height: 44px; border-radius: 50%; border: 1px solid #444; background: #1f6feb; color: #fff;
  font-size: 20px; cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,.4); z-index: 10; }
#scroll-bottom.visible { display: block; }
.empty { color: var(--muted); padding: 24px; text-align: center; }
body.layout-col { display: flex; flex-direction: column; height: 100vh; }
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
  <h1 id="title">Session Tracker</h1>
  <div id="meta-line">Resolving…</div>
</header>
<div id="viewport">
  <div id="terminal-wrap"><p class="empty" id="empty-hint">Waiting for session activity…</p></div>
</div>
<button type="button" id="scroll-bottom" title="Scroll to bottom">↓</button>
<script>
const params = new URLSearchParams(location.search);
const apiBase = location.pathname.replace(/\\/?$/, '') + '/api';
const terminal = document.getElementById('terminal-wrap');
const emptyHint = document.getElementById('empty-hint');
const scrollBtn = document.getElementById('scroll-bottom');
let autoFollow = true;
let sessionId = '';
let lineCursor = 0;
let eventSource = null;
let pollTimer = null;
let gotTerminalLines = false;
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

function appendBlock(block) {
  gotTerminalLines = true;
  if (emptyHint) emptyHint.remove();
  if (block.line_kind === 'hermes') {
    const box = document.createElement('div');
    box.className = 'hermes-box';
    box.innerHTML = '<div class="hermes-title">╭─ ⚕ Hermes ─────────────────</div>'
      + '<div class="hermes-body">' + esc(block.text || '') + '</div>'
      + '<div class="hermes-title">╰────────────────────────────────</div>';
    terminal.appendChild(box);
  } else if (block.line_kind === 'status') {
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

function connectStream() {
  if (!sessionId) return;
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  const streamQs = params.toString();
  const url = apiBase + '/stream?session_id=' + encodeURIComponent(sessionId)
    + '&cursor=' + lineCursor + (streamQs ? '&' + streamQs : '');
  eventSource = new EventSource(url);
  eventSource.onmessage = (msg) => {
    try {
      const block = JSON.parse(msg.data);
      if (block.seq > lineCursor) lineCursor = block.seq;
      appendBlock(block);
    } catch (_) {}
  };
  eventSource.addEventListener('snapshot', (msg) => {
    try {
      const data = JSON.parse(msg.data);
      (data.lines || []).forEach(block => {
        if (block.seq > lineCursor) lineCursor = block.seq;
        appendBlock(block);
      });
    } catch (_) {}
  });
  eventSource.onerror = () => { /* browser will retry */ };
}

function updateMetaLine(info) {
  const who = info.user_id ? (' | user: ' + info.user_id) : '';
  const lines = info.terminal_lines != null ? (' | lines: ' + info.terminal_lines) : '';
  document.getElementById('meta-line').textContent =
    (info.canonical_chat_id || '') + who + ' | session: ' +
    (info.session_id || '(pending)') + ' | ' + (info.status || 'waiting') + lines;
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

function applyResolve(info) {
  const prev = sessionId;
  document.getElementById('title').textContent = info.label || 'Session Tracker';
  updateMetaLine(info);
  updateEmptyHint(info);
  if (!info.session_id) {
    sessionId = '';
    return;
  }
  if (info.session_id !== prev) {
    sessionId = info.session_id;
    lineCursor = 0;
    gotTerminalLines = false;
    connectStream();
  } else if (!eventSource) {
    sessionId = info.session_id;
    connectStream();
  }
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

    @_require_sessiontracker_params
    async def api_resolve(request: Any, **kw: Any) -> Any:
        from aiohttp import web

        chat_type = kw["chat_type"]
        chat_id = kw["chat_id"]
        code = kw["code"]
        account = None
        if chat_type == 7:
            try:
                account = _read_infoflow_account()
            except ValueError as exc:
                return web.Response(status=500, text=str(exc))
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
        account = None
        if chat_type == 7:
            try:
                account = _read_infoflow_account()
            except ValueError as exc:
                return web.Response(status=500, text=str(exc))
        try:
            target = await resolve_target(
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

        canonical = target["canonical_chat_id"]
        if not session_matches_target(tracker, sid, canonical):
            return web.Response(status=403, text="session_id does not match target")

        try:
            cursor = _parse_cursor(request.rel_url.query.get("cursor", "0"))
        except ValueError as exc:
            return web.Response(status=400, text=str(exc))

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)

        lines: list[dict[str, Any]] = []
        for ev in tracker.snapshot(sid, cursor=cursor):
            if ev.kind not in TERMINAL_EVENT_KINDS:
                continue
            block = event_to_terminal_dict(ev)
            if block:
                lines.append(block)
        if lines:
            snap = json.dumps({"lines": lines}, ensure_ascii=False, default=str)
            await response.write(f"event: snapshot\ndata: {snap}\n\n".encode())

        q = tracker.subscribe(sid)
        try:
            seq_cursor = cursor
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25.0)
                except TimeoutError:
                    await response.write(b": heartbeat\n\n")
                    continue
                if ev is None:
                    break
                if ev.kind not in TERMINAL_EVENT_KINDS:
                    continue
                if ev.seq <= seq_cursor:
                    continue
                block = event_to_terminal_dict(ev)
                if block is None:
                    continue
                seq_cursor = ev.seq
                payload = json.dumps(block, ensure_ascii=False, default=str)
                await response.write(f"data: {payload}\n\n".encode())
        finally:
            tracker.unsubscribe(sid, q)
        return response

    app.router.add_get(root, page)
    app.router.add_get(f"{root}/api/resolve", api_resolve)
    app.router.add_get(f"{root}/api/stream", api_stream)
    logger.info("[infoflow] Session Tracker at <host>:<port>%s", root)


__all__ = [
    "TERMINAL_EVENT_KINDS",
    "count_terminal_lines",
    "format_terminal_line",
    "event_to_terminal_dict",
    "resolve_target",
    "session_matches_target",
    "register_sessiontracker_routes",
    "sessiontracker_enabled",
]
