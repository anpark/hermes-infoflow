"""Tests for ``infoflow_create_group`` tool argument handling."""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from hermes_infoflow.adapter import InfoflowAdapter
from hermes_infoflow.bot import recall_inbound_message_id_hint_scope
from hermes_infoflow.tools import make_create_group_handler


class _Platform:
    def __init__(self, name: str) -> None:
        self.name = name

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _Platform):
            return self.name == other.name
        return self.name == other


def _run_with_adapter(adapter, args: dict) -> dict:
    platform = _Platform("infoflow")
    runner = SimpleNamespace(adapters={platform: adapter})

    gw_run = ModuleType("gateway.run")
    gw_run._gateway_runner_ref = lambda: runner
    gw_config = ModuleType("gateway.config")
    gw_config.Platform = _Platform

    handler = make_create_group_handler()
    with patch.dict(sys.modules, {"gateway.run": gw_run, "gateway.config": gw_config}):
        result = asyncio.run(handler(args))
    return json.loads(result)


def test_create_group_handler_normalizes_userids_and_robot_ids() -> None:
    serverapi = SimpleNamespace(
        create_group=AsyncMock(return_value={
            "ok": True,
            "groupid": "123456",
            "failMembers": [],
            "failRobotIds": [999],
            "failManager": [],
            "failRobotManager": [],
        }),
    )
    adapter = InfoflowAdapter.__new__(InfoflowAdapter)
    adapter._serverapi = serverapi  # type: ignore[attr-defined]
    adapter._settings = {"app_agent_id": 6471}  # type: ignore[attr-defined]
    adapter._admin_uid = "chengbo05"  # type: ignore[attr-defined]
    adapter._message_store = SimpleNamespace(find_any=lambda mid: None)

    parsed = _run_with_adapter(adapter, {
        "group_name": "测试群",
        "group_owner": "chengbo05",
        "member_users": ["alice", "bob@baidu.com", "alice"],
        "robot_ids": ["15072", 6471, "15072"],
        "friendly_level": 3,
        "search_ability": 0,
        "managers": ["alice"],
        "robot_managers": ["15072"],
    })

    assert parsed["success"] is True
    assert parsed["group_id"] == "123456"
    assert parsed["group_owner"] == "chengbo05@baidu.com"
    assert parsed["requested"]["member_users"] == [
        "alice@baidu.com",
        "bob@baidu.com",
    ]
    assert parsed["requested"]["robot_ids"] == [15072, 6471]
    assert parsed["requested"]["managers"] == ["alice@baidu.com"]
    assert parsed["requested"]["robot_managers"] == [15072, 6471]
    assert parsed["partial_failure"] is True
    serverapi.create_group.assert_awaited_once_with(
        group_name="测试群",
        group_owner="chengbo05@baidu.com",
        member_list=["alice@baidu.com", "bob@baidu.com"],
        robot_list=[15072, 6471],
        friendly_level=3,
        search_ability=0,
        managers=["alice@baidu.com"],
        robot_managers=[15072, 6471],
        group_sidebar=None,
    )


def test_create_group_handler_defaults_to_open_group_and_self_robot_manager() -> None:
    serverapi = SimpleNamespace(
        create_group=AsyncMock(return_value={
            "ok": True,
            "groupid": "123456",
            "failMembers": [],
            "failRobotIds": [],
            "failManager": [],
            "failRobotManager": [],
        }),
    )
    adapter = InfoflowAdapter.__new__(InfoflowAdapter)
    adapter._serverapi = serverapi  # type: ignore[attr-defined]
    adapter._settings = {"app_agent_id": 6471}  # type: ignore[attr-defined]
    adapter._admin_uid = "chengbo05"  # type: ignore[attr-defined]
    adapter._message_store = SimpleNamespace(find_any=lambda mid: None)

    parsed = _run_with_adapter(adapter, {
        "group_name": "测试群",
        "group_owner": "chengbo05",
        "member_users": ["chengbo05"],
    })

    assert parsed["success"] is True
    assert parsed["requested"]["friendly_level"] == 3
    assert parsed["requested"]["robot_ids"] == [6471]
    assert parsed["requested"]["robot_managers"] == [6471]
    serverapi.create_group.assert_awaited_once_with(
        group_name="测试群",
        group_owner="chengbo05@baidu.com",
        member_list=["chengbo05@baidu.com"],
        robot_list=[6471],
        friendly_level=3,
        search_ability=1,
        managers=None,
        robot_managers=[6471],
        group_sidebar=None,
    )


def test_create_group_handler_fails_when_self_robot_manager_assignment_fails() -> None:
    serverapi = SimpleNamespace(
        create_group=AsyncMock(return_value={
            "ok": True,
            "groupid": "123456",
            "failMembers": [],
            "failRobotIds": [],
            "failManager": [],
            "failRobotManager": [6471],
        }),
    )
    adapter = InfoflowAdapter.__new__(InfoflowAdapter)
    adapter._serverapi = serverapi  # type: ignore[attr-defined]
    adapter._settings = {"app_agent_id": 6471}  # type: ignore[attr-defined]
    adapter._admin_uid = "chengbo05"  # type: ignore[attr-defined]
    adapter._message_store = SimpleNamespace(find_any=lambda mid: None)

    parsed = _run_with_adapter(adapter, {
        "group_name": "测试群",
        "group_owner": "chengbo05",
    })

    assert parsed["success"] is False
    assert parsed["group_id"] == "123456"
    assert 6471 in parsed["failed"]["robot_managers"]
    assert "bot itself" in parsed["error"]


def test_create_group_handler_rejects_robot_name_without_agent_id() -> None:
    handler = make_create_group_handler()
    result = asyncio.run(handler({
        "group_name": "测试群",
        "group_owner": "chengbo05",
        "robot_ids": ["chengbo5.2"],
    }))

    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "agentIds" in parsed["error"]


def test_create_group_handler_requires_manager_in_member_users() -> None:
    handler = make_create_group_handler()
    result = asyncio.run(handler({
        "group_name": "测试群",
        "group_owner": "chengbo05",
        "member_users": ["alice"],
        "managers": ["bob"],
    }))

    parsed = json.loads(result)
    assert parsed["success"] is False
    assert "managers must also be included" in parsed["error"]


def test_create_group_handler_denies_non_admin_current_sender() -> None:
    serverapi = SimpleNamespace(create_group=AsyncMock())
    adapter = InfoflowAdapter.__new__(InfoflowAdapter)
    adapter._serverapi = serverapi  # type: ignore[attr-defined]
    adapter._admin_uid = "chengbo05"  # type: ignore[attr-defined]
    adapter._message_store = SimpleNamespace(
        find_any=lambda mid: SimpleNamespace(sender="user:bob")
    )

    with recall_inbound_message_id_hint_scope("MID"):
        parsed = _run_with_adapter(adapter, {
            "group_name": "测试群",
            "group_owner": "chengbo05",
        })

    assert parsed == {
        "success": False,
        "error": "Only Infoflow admin users can create groups.",
    }
    serverapi.create_group.assert_not_awaited()
