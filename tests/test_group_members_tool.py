"""Tests for ``infoflow_get_group_members`` tool result serialization."""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from hermes_infoflow.adapter import InfoflowAdapter
from hermes_infoflow.itypes import GroupMember
from hermes_infoflow.serverapi import (
    GroupMembersFetchResult,
    GroupMembersFetchStatus,
)
from hermes_infoflow.tools import (
    make_group_members_handler,
    tool_result_json,
)


def test_group_members_handler_success_returns_json_string() -> None:
    class _Platform:
        def __init__(self, name: str) -> None:
            self.name = name

        def __hash__(self) -> int:
            return hash(self.name)

        def __eq__(self, other: object) -> bool:
            if isinstance(other, _Platform):
                return self.name == other.name
            return self.name == other

    members = [
        GroupMember(uid="chengbo05", name="Untrusted Human Name", is_bot=False),
        GroupMember(
            uid="6471",
            name="chengbo5.1",
            agent_id=6471,
            imid="4105000875",
            is_bot=True,
        ),
    ]
    fetch_result = GroupMembersFetchResult(
        members=members,
        status=GroupMembersFetchStatus.OK,
    )

    serverapi = SimpleNamespace(
        fetch_group_members_detailed=AsyncMock(return_value=fetch_result),
    )
    adapter = InfoflowAdapter.__new__(InfoflowAdapter)
    adapter._serverapi = serverapi  # type: ignore[attr-defined]
    adapter._message_store = SimpleNamespace(
        find_user_by_user_id=lambda uid: (
            SimpleNamespace(name="成博") if uid == "chengbo05" else None
        )
    )

    platform = _Platform("infoflow")
    runner = SimpleNamespace(adapters={platform: adapter})

    gw_run = ModuleType("gateway.run")
    gw_run._gateway_runner_ref = lambda: runner
    gw_config = ModuleType("gateway.config")
    gw_config.Platform = _Platform

    handler = make_group_members_handler()
    with patch.dict(sys.modules, {"gateway.run": gw_run, "gateway.config": gw_config}):
        result = asyncio.run(handler({"group_id": 4507088}))

    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed["success"] is True
    assert parsed["group_id"] == "4507088"
    assert parsed["users"] == [{"user_id": "chengbo05", "name": "成博"}]
    assert parsed["bots"] == [{
        "agent_id": 6471,
        "name": "chengbo5.1",
    }]
    assert "imid" not in json.dumps(parsed, ensure_ascii=False)
    assert parsed["counts"] == {"users": 1, "bots": 1, "total": 2}
    assert parsed["source"] == "ok"

    serverapi.fetch_group_members_detailed.assert_awaited_once_with(
        "4507088",
        force_refresh=True,
    )


def test_group_members_handler_omits_untrusted_human_name() -> None:
    class _Platform:
        def __init__(self, name: str) -> None:
            self.name = name

        def __hash__(self) -> int:
            return hash(self.name)

        def __eq__(self, other: object) -> bool:
            if isinstance(other, _Platform):
                return self.name == other.name
            return self.name == other

    fetch_result = GroupMembersFetchResult(
        members=[GroupMember(uid="alice", name="Do Not Expose", is_bot=False)],
        status=GroupMembersFetchStatus.OK,
    )
    serverapi = SimpleNamespace(
        fetch_group_members_detailed=AsyncMock(return_value=fetch_result),
    )
    adapter = InfoflowAdapter.__new__(InfoflowAdapter)
    adapter._serverapi = serverapi  # type: ignore[attr-defined]
    adapter._message_store = SimpleNamespace(find_user_by_user_id=lambda uid: None)

    platform = _Platform("infoflow")
    runner = SimpleNamespace(adapters={platform: adapter})
    gw_run = ModuleType("gateway.run")
    gw_run._gateway_runner_ref = lambda: runner
    gw_config = ModuleType("gateway.config")
    gw_config.Platform = _Platform

    handler = make_group_members_handler()
    with patch.dict(sys.modules, {"gateway.run": gw_run, "gateway.config": gw_config}):
        result = asyncio.run(handler({"group_id": 4507088}))

    parsed = json.loads(result)
    assert parsed["users"] == [{"user_id": "alice"}]
    assert "Do Not Expose" not in result


def test_group_members_handler_empty_ok_returns_success() -> None:
    fetch_result = GroupMembersFetchResult(
        members=[],
        status=GroupMembersFetchStatus.OK,
    )
    serverapi = SimpleNamespace(
        fetch_group_members_detailed=AsyncMock(return_value=fetch_result),
    )
    adapter = InfoflowAdapter.__new__(InfoflowAdapter)
    adapter._serverapi = serverapi  # type: ignore[attr-defined]

    class _Platform:
        def __init__(self, name: str) -> None:
            self.name = name

        def __hash__(self) -> int:
            return hash(self.name)

        def __eq__(self, other: object) -> bool:
            if isinstance(other, _Platform):
                return self.name == other.name
            return self.name == other

    platform = _Platform("infoflow")
    runner = SimpleNamespace(adapters={platform: adapter})
    gw_run = ModuleType("gateway.run")
    gw_run._gateway_runner_ref = lambda: runner
    gw_config = ModuleType("gateway.config")
    gw_config.Platform = _Platform

    handler = make_group_members_handler()
    with patch.dict(sys.modules, {"gateway.run": gw_run, "gateway.config": gw_config}):
        result = asyncio.run(handler({"group_id": 99}))

    parsed = json.loads(result)
    assert parsed["success"] is True
    assert parsed["counts"] == {"users": 0, "bots": 0, "total": 0}


def test_group_members_handler_failed_fetch_returns_error() -> None:
    fetch_result = GroupMembersFetchResult(
        members=[],
        status=GroupMembersFetchStatus.FAILED,
        error="network down",
    )
    serverapi = SimpleNamespace(
        fetch_group_members_detailed=AsyncMock(return_value=fetch_result),
    )
    adapter = InfoflowAdapter.__new__(InfoflowAdapter)
    adapter._serverapi = serverapi  # type: ignore[attr-defined]

    class _Platform:
        def __init__(self, name: str) -> None:
            self.name = name

        def __hash__(self) -> int:
            return hash(self.name)

        def __eq__(self, other: object) -> bool:
            if isinstance(other, _Platform):
                return self.name == other.name
            return self.name == other

    platform = _Platform("infoflow")
    runner = SimpleNamespace(adapters={platform: adapter})
    gw_run = ModuleType("gateway.run")
    gw_run._gateway_runner_ref = lambda: runner
    gw_config = ModuleType("gateway.config")
    gw_config.Platform = _Platform

    handler = make_group_members_handler()
    with patch.dict(sys.modules, {"gateway.run": gw_run, "gateway.config": gw_config}):
        result = asyncio.run(handler({"group_id": 4507088}))

    parsed = json.loads(result)
    assert parsed == {"error": "network down"}


def test_group_members_handler_missing_group_id() -> None:
    handler = make_group_members_handler()
    result = asyncio.run(handler({}))
    assert json.loads(result) == {"error": "group_id is required"}


def test_group_members_handler_no_adapter() -> None:
    handler = make_group_members_handler()
    result = asyncio.run(handler({"group_id": 1}))
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert "error" in parsed
    assert "not running" in parsed["error"]


def test_tool_result_json_returns_string() -> None:
    raw = tool_result_json({"success": True, "group_id": "1"})
    assert isinstance(raw, str)
    assert json.loads(raw)["group_id"] == "1"
