"""Admin terminal helpers for the Session Tracker page."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

LOCALHOST_ADDRS = frozenset({"127.0.0.1", "::1"})


def _truthy_env(name: str, default: str) -> bool:
    raw = os.getenv(name, default).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _falsy_env(name: str, default: str) -> bool:
    raw = os.getenv(name, default).strip().lower()
    return raw in ("0", "false", "no", "off")


def sessiontracker_terminal_enabled() -> bool:
    return _truthy_env("INFOFLOW_SESSIONTRACKER_TERMINAL_ENABLED", "false")


def sessiontracker_terminal_localhost_only() -> bool:
    return not _falsy_env("INFOFLOW_SESSIONTRACKER_TERMINAL_LOCALHOST_ONLY", "true")


def sessiontracker_terminal_idle_timeout_seconds() -> int:
    raw = os.getenv("INFOFLOW_SESSIONTRACKER_TERMINAL_IDLE_TIMEOUT", "600").strip()
    try:
        return max(30, int(raw))
    except ValueError:
        return 600


def sessiontracker_terminal_cwd() -> str:
    raw = os.getenv("INFOFLOW_SESSIONTRACKER_TERMINAL_CWD", "").strip()
    if raw:
        return str(Path(raw).expanduser())

    candidates = (
        Path.home() / ".hermes" / "plugins" / "infoflow",
        Path.home() / ".hermes" / "plugin" / "infoflow",
    )
    for path in candidates:
        if path.is_dir():
            return str(path)
    return os.getcwd()


def request_is_localhost(request: Any) -> bool:
    return (getattr(request, "remote", "") or "") in LOCALHOST_ADDRS


def set_pty_window_size(fd: int, *, rows: int, cols: int) -> None:
    rows = max(1, min(int(rows or 24), 200))
    cols = max(2, min(int(cols or 80), 500))
    size = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


def _shell_command() -> list[str]:
    shell = os.getenv("SHELL", "").strip()
    if not shell or not Path(shell).exists():
        shell = "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/sh"
    return [shell, "-l"]


def spawn_terminal_process(
    *,
    cwd: str,
    rows: int,
    cols: int,
) -> tuple[subprocess.Popen[bytes], int]:
    master_fd, slave_fd = pty.openpty()
    set_pty_window_size(master_fd, rows=rows, cols=cols)

    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")

    try:
        proc = subprocess.Popen(
            _shell_command(),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:
        with contextlib.suppress(OSError):
            os.close(master_fd)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(slave_fd)

    return proc, master_fd


async def _send_pty_output(ws: Any, master_fd: int) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            data = await loop.run_in_executor(None, os.read, master_fd, 8192)
        except (OSError, ValueError):
            break
        if not data:
            break
        try:
            await ws.send_json({
                "type": "output",
                "data": data.decode("utf-8", errors="replace"),
            })
        except (ConnectionError, RuntimeError):
            break


async def _send_exit_when_done(ws: Any, proc: subprocess.Popen[bytes]) -> None:
    code = await asyncio.to_thread(proc.wait)
    if getattr(ws, "closed", False):
        return
    with contextlib.suppress(ConnectionError, RuntimeError):
        await ws.send_json({"type": "exit", "code": code})
        await ws.close()


async def _idle_watch(ws: Any, last_activity: Callable[[], float], idle_timeout: int) -> None:
    while True:
        await asyncio.sleep(min(5, max(1, idle_timeout // 4)))
        if time.monotonic() - last_activity() < idle_timeout:
            continue
        with contextlib.suppress(ConnectionError, RuntimeError):
            await ws.send_json({"type": "exit", "reason": "idle_timeout"})
            await ws.close()
        break


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGHUP)


async def _kill_process_group_if_needed(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    await asyncio.sleep(0.4)
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


async def run_terminal_websocket(
    request: Any,
    *,
    viewer_user_id: str,
    cwd: str,
    idle_timeout: int,
) -> Any:
    from aiohttp import WSMsgType, web

    rows = int(request.rel_url.query.get("rows", "30") or "30")
    cols = int(request.rel_url.query.get("cols", "100") or "100")
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=262144)
    await ws.prepare(request)

    resolved_cwd = str(Path(cwd).expanduser())
    if not Path(resolved_cwd).is_dir():
        await ws.send_json({"type": "exit", "reason": "cwd_not_found"})
        await ws.close()
        return ws

    proc, master_fd = spawn_terminal_process(cwd=resolved_cwd, rows=rows, cols=cols)
    last_activity_at = time.monotonic()

    def _last_activity() -> float:
        return last_activity_at

    logger.info(
        "[infoflow] sessiontracker terminal start viewer=%s remote=%s cwd=%s pid=%s",
        viewer_user_id,
        getattr(request, "remote", "") or "unknown",
        resolved_cwd,
        proc.pid,
    )

    output_task = asyncio.create_task(_send_pty_output(ws, master_fd))
    exit_task = asyncio.create_task(_send_exit_when_done(ws, proc))
    idle_task = asyncio.create_task(_idle_watch(ws, _last_activity, idle_timeout))

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
                continue
            try:
                payload = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            mtype = str(payload.get("type") or "")
            if mtype == "input":
                data = str(payload.get("data") or "")
                if not data:
                    continue
                last_activity_at = time.monotonic()
                with contextlib.suppress(OSError):
                    os.write(master_fd, data.encode())
            elif mtype == "resize":
                last_activity_at = time.monotonic()
                with contextlib.suppress(OSError, ValueError):
                    set_pty_window_size(
                        master_fd,
                        rows=int(payload.get("rows") or rows),
                        cols=int(payload.get("cols") or cols),
                    )
    finally:
        for task in (idle_task, output_task, exit_task):
            task.cancel()
        _terminate_process_group(proc)
        with contextlib.suppress(OSError):
            os.close(master_fd)
        await _kill_process_group_if_needed(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            await asyncio.to_thread(proc.wait, timeout=1)
        await asyncio.gather(idle_task, output_task, exit_task, return_exceptions=True)
        logger.info(
            "[infoflow] sessiontracker terminal stop viewer=%s pid=%s code=%s",
            viewer_user_id,
            proc.pid,
            proc.poll(),
        )
    return ws


__all__ = [
    "LOCALHOST_ADDRS",
    "request_is_localhost",
    "run_terminal_websocket",
    "sessiontracker_terminal_cwd",
    "sessiontracker_terminal_enabled",
    "sessiontracker_terminal_idle_timeout_seconds",
    "sessiontracker_terminal_localhost_only",
    "set_pty_window_size",
    "spawn_terminal_process",
]
