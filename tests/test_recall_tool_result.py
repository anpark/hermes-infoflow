"""Tests for ``infoflow_recall_message`` tool result serialization.

These run without hermes-agent on PYTHONPATH (unlike ``test_adapter.py``).
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from hermes_infoflow.adapter import InfoflowAdapter
from hermes_infoflow.tools import make_recall_handler, tool_result_json


def test_tool_result_json_returns_string() -> None:
    raw = tool_result_json({"success": True, "message_id": "1865187797205374754"})
    assert isinstance(raw, str)
    assert json.loads(raw) == {
        "success": True,
        "message_id": "1865187797205374754",
    }


def test_recall_handler_success_returns_json_string() -> None:
    class _Platform:
        def __init__(self, name: str) -> None:
            self.name = name

        def __hash__(self) -> int:
            return hash(self.name)

        def __eq__(self, other: object) -> bool:
            if isinstance(other, _Platform):
                return self.name == other.name
            return self.name == other

    adapter = InfoflowAdapter.__new__(InfoflowAdapter)
    adapter._http_session = None

    async def _fake_delete(
        target,
        message_id=None,
        *,
        count=1,
    ):
        return SimpleNamespace(
            success=True,
            message_id="1865187797205374754",
            error=None,
        )

    adapter.delete_message = _fake_delete  # type: ignore[method-assign]

    platform = _Platform("infoflow")
    runner = SimpleNamespace(adapters={platform: adapter})

    gw_run = ModuleType("gateway.run")
    gw_run._gateway_runner_ref = lambda: runner
    gw_config = ModuleType("gateway.config")
    gw_config.Platform = _Platform

    handler = make_recall_handler()
    with (
        patch.dict(sys.modules, {"gateway.run": gw_run, "gateway.config": gw_config}),
    ):
        result = asyncio.run(handler({"target": "alice", "count": 1}))

    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed["success"] is True
    assert parsed["action"] == "recall_message"
    assert parsed["status"] == "recalled"
    assert parsed["target"] == "alice"
    assert parsed["count"] == 1
    assert "error" not in parsed
    assert parsed["final_response"] == {
        "mode": "silent_if_only_task",
        "content": "NO_REPLY",
        "if_other_tasks": "answer_only_other_tasks_without_recall_confirmation",
    }


def test_recall_handler_error_returns_json_string() -> None:
    handler = make_recall_handler()
    result = asyncio.run(handler({"count": 1}))
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed["success"] is False
    assert parsed["action"] == "recall_message"
    assert parsed["status"] == "failed"
    assert parsed["error"] == "target is required"
    assert parsed["final_response"] == {
        "mode": "report_failure",
        "content": "撤回失败，消息可能已过期。",
    }
