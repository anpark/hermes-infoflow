"""Shared Server-Sent Events helpers."""

from __future__ import annotations

import logging
from typing import Any

# Headers for nginx (and other reverse proxies) to stream SSE without buffering.
SSE_RESPONSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

SSE_HEARTBEAT = b": heartbeat\n\n"
SSE_HEARTBEAT_INTERVAL_SECONDS = 25.0


async def write_sse(
    response: Any,
    data: bytes,
    *,
    logger: logging.Logger | None = None,
    context: str = "",
) -> bool:
    """Write an SSE chunk, returning False when the client has gone away."""
    try:
        await response.write(data)
    except (ConnectionResetError, BrokenPipeError) as exc:
        if logger is not None:
            suffix = f" ({context})" if context else ""
            logger.debug("SSE client disconnected%s: %s", suffix, exc)
        return False
    return True
