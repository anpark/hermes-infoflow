"""Tests for unified group member fetch (debounce, coalescing, status)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, patch

import pytest

from hermes_infoflow.itypes import GroupMember
from hermes_infoflow.serverapi import (
    _MEMBERS_CACHE,
    GroupMembersFetchStatus,
    ServerAPI,
    _guarded_state,
)


@pytest.fixture
def serverapi(monkeypatch) -> ServerAPI:
    monkeypatch.setenv("INFOFLOW_API_HOST", "http://apiin.im.baidu.com")
    monkeypatch.setenv("INFOFLOW_APP_KEY", "test-key")
    monkeypatch.setenv("INFOFLOW_APP_SECRET", "test-secret")
    from hermes_infoflow.settings import _read_account_settings

    return ServerAPI(settings=_read_account_settings(None))


@pytest.fixture(autouse=True)
def _clear_member_caches():
    _MEMBERS_CACHE.clear()
    _guarded_state.clear()
    yield
    _MEMBERS_CACHE.clear()
    _guarded_state.clear()


def test_force_refresh_debounce_single_api_call(serverapi: ServerAPI) -> None:
    api_members = [
        type("M", (), {
            "uid": "u1", "name": "u1", "agent_id": None, "is_bot": False, "imid": "",
        })(),
    ]

    with patch(
        "hermes_infoflow.serverapi._api.get_group_members",
        new_callable=AsyncMock,
        return_value=api_members,
    ) as mock_api:
        async def _run():
            r1 = await serverapi.fetch_group_members_detailed(
                "4507088", force_refresh=True,
            )
            r2 = await serverapi.fetch_group_members_detailed(
                "4507088", force_refresh=True,
            )
            return r1, r2

        r1, r2 = asyncio.run(_run())

    assert mock_api.await_count == 1
    assert r1.status == GroupMembersFetchStatus.OK
    assert r2.status == GroupMembersFetchStatus.OK_DEBOUNCED


def test_ttl_cache_hit_without_api(serverapi: ServerAPI) -> None:
    _MEMBERS_CACHE["99"] = (
        [GroupMember(uid="cached", name="cached")],
        time.time(),
    )

    with patch(
        "hermes_infoflow.serverapi._api.get_group_members",
        new_callable=AsyncMock,
    ) as mock_api:
        result = asyncio.run(
            serverapi.fetch_group_members_detailed("99", force_refresh=False),
        )

    mock_api.assert_not_awaited()
    assert result.status == GroupMembersFetchStatus.OK_CACHED
    assert result.members[0].uid == "cached"


def test_failed_fetch_returns_stale_cache(serverapi: ServerAPI) -> None:
    stale = [GroupMember(uid="stale", name="stale")]
    _MEMBERS_CACHE["77"] = (stale, time.time() - 999)

    with patch(
        "hermes_infoflow.serverapi._api.get_group_members",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        result = asyncio.run(
            serverapi.fetch_group_members_detailed("77", force_refresh=True),
        )

    assert result.status == GroupMembersFetchStatus.OK_STALE
    assert result.members[0].uid == "stale"


def test_failed_fetch_no_cache_is_failed(serverapi: ServerAPI) -> None:
    with patch(
        "hermes_infoflow.serverapi._api.get_group_members",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        result = asyncio.run(
            serverapi.fetch_group_members_detailed("88", force_refresh=True),
        )

    assert result.status == GroupMembersFetchStatus.FAILED
    assert result.members == []
    assert "boom" in (result.error or "")


def test_failure_storm_debounced_within_window(serverapi: ServerAPI) -> None:
    """Repeated failures within 3s must not hammer the remote API."""
    with patch(
        "hermes_infoflow.serverapi._api.get_group_members",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ) as mock_api:
        async def _run():
            r1 = await serverapi.fetch_group_members_detailed(
                "55", force_refresh=True,
            )
            r2 = await serverapi.fetch_group_members_detailed(
                "55", force_refresh=True,
            )
            r3 = await serverapi.fetch_group_members_detailed(
                "55", force_refresh=True,
            )
            return r1, r2, r3

        r1, r2, r3 = asyncio.run(_run())

    assert mock_api.await_count == 1
    for r in (r1, r2, r3):
        assert r.status == GroupMembersFetchStatus.FAILED
        assert r.members == []


def test_stale_storm_debounced_within_window(serverapi: ServerAPI) -> None:
    """After a failure with stale cache, repeats within 3s replay OK_STALE."""
    stale = [GroupMember(uid="stale", name="stale")]
    _MEMBERS_CACHE["66"] = (stale, time.time() - 9999)

    with patch(
        "hermes_infoflow.serverapi._api.get_group_members",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ) as mock_api:
        async def _run():
            r1 = await serverapi.fetch_group_members_detailed(
                "66", force_refresh=True,
            )
            r2 = await serverapi.fetch_group_members_detailed(
                "66", force_refresh=True,
            )
            return r1, r2

        r1, r2 = asyncio.run(_run())

    assert mock_api.await_count == 1
    for r in (r1, r2):
        assert r.status == GroupMembersFetchStatus.OK_STALE
        assert r.members[0].uid == "stale"


def test_concurrent_callers_share_inflight_fetch(serverapi: ServerAPI) -> None:
    """Simultaneous callers must coalesce to a single ``_api.get_group_members`` call."""
    gate = asyncio.Event()

    async def _slow_fetch(*_args, **_kwargs):
        await gate.wait()
        return [
            type("M", (), {
                "uid": "u1", "name": "u1", "agent_id": None,
                "is_bot": False, "imid": "",
            })(),
        ]

    with patch(
        "hermes_infoflow.serverapi._api.get_group_members",
        new=AsyncMock(side_effect=_slow_fetch),
    ) as mock_api:
        async def _run():
            tasks = [
                asyncio.create_task(
                    serverapi.fetch_group_members_detailed(
                        "44", force_refresh=True,
                    ),
                )
                for _ in range(5)
            ]
            # Yield so all tasks reach the await on the shared in-flight task.
            await asyncio.sleep(0)
            gate.set()
            return await asyncio.gather(*tasks)

        results = asyncio.run(_run())

    assert mock_api.await_count == 1
    assert all(r.status == GroupMembersFetchStatus.OK for r in results)


def test_inflight_task_from_other_loop_is_not_awaited(serverapi: ServerAPI) -> None:
    """Tool worker loops must not await a task created on the gateway loop."""

    async def _never_finishes():
        await asyncio.Future()

    old_loop = asyncio.new_event_loop()
    old_task = old_loop.create_task(_never_finishes())
    _guarded_state["99"] = {
        "task": old_task,
        "task_loop": old_loop,
        "last_ts": 0.0,
        "last_result": None,
    }
    api_members = [
        type("M", (), {
            "uid": "fresh", "name": "fresh", "agent_id": None,
            "is_bot": False, "imid": "",
        })(),
    ]

    try:
        with patch(
            "hermes_infoflow.serverapi._api.get_group_members",
            new_callable=AsyncMock,
            return_value=api_members,
        ) as mock_api:
            result = asyncio.run(
                serverapi.fetch_group_members_detailed("99", force_refresh=True),
            )
    finally:
        old_task.cancel()
        with contextlib.suppress(Exception):
            old_loop.run_until_complete(
                asyncio.gather(old_task, return_exceptions=True),
            )
        old_loop.close()

    assert mock_api.await_count == 1
    assert result.status == GroupMembersFetchStatus.OK
    assert result.members[0].uid == "fresh"
