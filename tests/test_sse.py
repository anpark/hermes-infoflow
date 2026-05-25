"""Tests for shared SSE helpers."""

from __future__ import annotations

import pytest

from hermes_infoflow.sse import write_sse


class _Response:
    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc
        self.writes: list[bytes] = []

    async def write(self, data: bytes) -> None:
        if self.exc is not None:
            raise self.exc
        self.writes.append(data)


async def test_write_sse_returns_false_for_disconnected_client() -> None:
    response = _Response(ConnectionResetError("closing transport"))

    ok = await write_sse(response, b"data: hi\n\n")

    assert ok is False


async def test_write_sse_returns_true_for_successful_write() -> None:
    response = _Response()

    ok = await write_sse(response, b"data: hi\n\n")

    assert ok is True
    assert response.writes == [b"data: hi\n\n"]


async def test_write_sse_does_not_swallow_unexpected_errors() -> None:
    response = _Response(ValueError("bad payload"))

    with pytest.raises(ValueError, match="bad payload"):
        await write_sse(response, b"data: hi\n\n")
