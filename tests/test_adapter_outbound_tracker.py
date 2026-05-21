"""Outbound tracker events — progress preview on short tool-style sends."""

from __future__ import annotations

from hermes_infoflow.adapter import InfoflowAdapter, _looks_like_progress_line
from hermes_infoflow.dashboard import SessionTracker
from hermes_infoflow.sessiontracker import format_terminal_line


def test_looks_like_progress_line() -> None:
    assert _looks_like_progress_line("┊ 💻 $ ls -la")
    assert not _looks_like_progress_line("hello world")


def test_push_outbound_progress_preview() -> None:
    tracker = SessionTracker(buffer_size=50)
    adapter = InfoflowAdapter.__new__(InfoflowAdapter)
    adapter._tracker = tracker  # noqa: SLF001

    line = "┊ 💻 inspecting repo…"
    adapter._push_infoflow_event(  # noqa: SLF001
        None,
        kind="outbound.infoflow",
        chat_id="group:1",
        extra={
            "type": "text",
            "chars": len(line),
            "preview": line,
            "is_progress_hint": True,
        },
    )

    events = tracker.snapshot("pending:group:1")
    assert len(events) == 1
    ev = events[0]
    assert ev.payload.get("is_progress_hint") is True
    assert ev.payload.get("preview") == line
    block = format_terminal_line(ev)
    assert block is not None
    assert "💻" in block["text"]
