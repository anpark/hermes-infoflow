"""Tests for unified group member fetch (debounce, coalescing, status)."""

from __future__ import annotations

import asyncio
import threading
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


def test_cross_loop_callers_share_inflight_fetch(serverapi: ServerAPI) -> None:
    """Gateway and tool worker loops should share one remote member fetch."""
    entered_remote = threading.Event()
    release_remote = threading.Event()
    api_members = [
        type("M", (), {
            "uid": "fresh", "name": "fresh", "agent_id": None,
            "is_bot": False, "imid": "",
        })(),
    ]

    async def _slow_fetch(*_args, **_kwargs):
        entered_remote.set()
        await asyncio.to_thread(release_remote.wait)
        return api_members

    thread_result: dict[str, object] = {}
    thread_errors: list[BaseException] = []

    def _thread_run() -> None:
        try:
            thread_result["first"] = asyncio.run(
                serverapi.fetch_group_members_detailed("99", force_refresh=True),
            )
        except BaseException as exc:  # pragma: no cover - test diagnostics
            thread_errors.append(exc)

    with patch(
        "hermes_infoflow.serverapi._api.get_group_members",
        new=AsyncMock(side_effect=_slow_fetch),
    ) as mock_api:
        thread = threading.Thread(target=_thread_run)
        thread.start()
        assert entered_remote.wait(timeout=2)

        async def _run_second_loop():
            task = asyncio.create_task(
                serverapi.fetch_group_members_detailed("99", force_refresh=True),
            )
            await asyncio.sleep(0.05)
            assert not task.done()
            release_remote.set()
            return await task

        second = asyncio.run(_run_second_loop())
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert thread_errors == []
    assert mock_api.await_count == 1
    first = thread_result["first"]
    assert isinstance(first, type(second))
    assert first.status == GroupMembersFetchStatus.OK
    assert second.status == GroupMembersFetchStatus.OK
    assert first.members[0].uid == "fresh"
    assert second.members[0].uid == "fresh"


def test_inflight_task_cancellation_completes_shared_future(
    serverapi: ServerAPI,
) -> None:
    async def _never_finishes(*_args, **_kwargs):
        await asyncio.Future()

    async def _wait_for_inflight_task() -> asyncio.Task:
        for _ in range(100):
            state = _guarded_state.get("99") or {}
            task = state.get("task")
            if isinstance(task, asyncio.Task):
                return task
            await asyncio.sleep(0.01)
        raise AssertionError("group member fetch task was not registered")

    async def _run() -> None:
        first = asyncio.create_task(
            serverapi.fetch_group_members_detailed("99", force_refresh=True),
        )
        inflight = await _wait_for_inflight_task()
        second = asyncio.create_task(
            serverapi.fetch_group_members_detailed("99", force_refresh=True),
        )
        await asyncio.sleep(0)

        inflight.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(second, timeout=1)
        assert _guarded_state["99"]["future"] is None
        assert _guarded_state["99"]["task"] is None

    with patch(
        "hermes_infoflow.serverapi._api.get_group_members",
        new=AsyncMock(side_effect=_never_finishes),
    ):
        asyncio.run(_run())
