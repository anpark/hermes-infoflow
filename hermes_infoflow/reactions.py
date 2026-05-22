"""Processing-reaction state for Infoflow sessions.

The reaction is a session-level thinking indicator: one visible marker per
scope, owned by the current agent run for that scope. Older runs may finish
later, but they are never allowed to delete a newer run's marker.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .utils import gw_log

ReactionCall = Callable[[dict[str, Any]], Awaitable[bool]]


@dataclass
class ReactionRunToken:
    """Owner token for one agent run's processing reaction."""

    scope_key: str
    run_id: str
    generation: int
    anchor_message_id: str
    handle: dict[str, Any] | None = None
    visible: bool = False
    stale: bool = False
    finished: bool = False


@dataclass
class ThinkingState:
    """Current thinking indicator state for a scope."""

    scope_key: str
    run_id: str
    generation: int
    anchor_message_id: str
    token: ReactionRunToken
    handle: dict[str, Any] | None = None
    visible: bool = False


class ReactionController:
    """Manage one processing reaction per Infoflow conversation scope."""

    def __init__(
        self,
        *,
        add_reaction: ReactionCall,
        delete_reaction: ReactionCall,
    ) -> None:
        self._add_reaction = add_reaction
        self._delete_reaction = delete_reaction
        self._lock = threading.RLock()
        self._states: dict[str, ThinkingState] = {}
        self._generations: dict[str, int] = {}
        self._anchor_to_scope: dict[str, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    @staticmethod
    def scope_key_for_handle(handle: dict[str, Any] | None) -> str:
        if not isinstance(handle, dict):
            return ""
        if handle.get("chat_type") == "group" and handle.get("group_id"):
            return f"group:{handle['group_id']}"
        if handle.get("chat_type") == "dm" and handle.get("from_uid"):
            return f"dm:{handle['from_uid']}"
        return ""

    def active_state(self, scope_key: str) -> ThinkingState | None:
        with self._lock:
            return self._states.get(scope_key)

    def token_by_anchor(
        self,
        anchor_message_id: str | None,
        *,
        expected_scope: str = "",
    ) -> ReactionRunToken | None:
        """Return the current token anchored to *anchor_message_id*, if any."""
        if not anchor_message_id:
            return None
        anchor = str(anchor_message_id)
        with self._lock:
            if expected_scope:
                state = self._states.get(expected_scope)
                if state is None or state.anchor_message_id != anchor:
                    return None
            else:
                scope_key = self._anchor_to_scope.get(anchor)
                state = self._states.get(scope_key or "") if scope_key else None
            return state.token if state is not None else None

    async def start(self, handle: dict[str, Any]) -> ReactionRunToken | None:
        """Start a new thinking run and replace any visible marker in scope."""
        scope_key = self.scope_key_for_handle(handle)
        if not scope_key:
            return None

        anchor_message_id = str(handle.get("base_msg_id") or "")
        previous_handle: dict[str, Any] | None = None
        with self._lock:
            generation = self._generations.get(scope_key, 0) + 1
            self._generations[scope_key] = generation
            run_id = f"{scope_key}:{generation}:{anchor_message_id}"
            token = ReactionRunToken(
                scope_key=scope_key,
                run_id=run_id,
                generation=generation,
                anchor_message_id=anchor_message_id,
                handle=handle,
            )

            previous = self._states.pop(scope_key, None)
            if previous is not None:
                previous.token.stale = True
                previous.token.visible = False
                if (
                    previous.anchor_message_id
                    and self._anchor_to_scope.get(previous.anchor_message_id)
                    == previous.scope_key
                ):
                    self._anchor_to_scope.pop(previous.anchor_message_id, None)
                if previous.visible and previous.handle:
                    previous_handle = previous.handle

            self._states[scope_key] = ThinkingState(
                scope_key=scope_key,
                run_id=run_id,
                generation=generation,
                anchor_message_id=anchor_message_id,
                token=token,
            )
            if anchor_message_id:
                self._anchor_to_scope[anchor_message_id] = scope_key

        if previous_handle is not None:
            self._schedule(
                self._delete_handle(previous_handle, reason="superseded"),
            )

        self._schedule(self._add_for_token(token, handle))
        return token

    async def _add_for_token(
        self,
        token: ReactionRunToken,
        handle: dict[str, Any],
    ) -> None:
        added = await self._add_handle(handle)
        delete_added = False
        with self._lock:
            state = self._states.get(token.scope_key)
            if (
                state is not None
                and state.run_id == token.run_id
                and not token.finished
            ):
                if added:
                    state.handle = handle
                    state.visible = True
                    token.visible = True
                else:
                    state.handle = None
                    state.visible = False
            else:
                token.stale = True
                token.visible = False
                delete_added = added

        if delete_added:
            await self._delete_handle(handle, reason="superseded_race")

    async def finish(
        self,
        token: ReactionRunToken | None,
        *,
        reason: str,
    ) -> bool:
        """Finish one run; only the current owner may delete the marker."""
        if token is None:
            return False
        handle_to_delete: dict[str, Any] | None = None
        with self._lock:
            if token.finished:
                return True
            token.finished = True
            state = self._states.get(token.scope_key)
            if state is None or state.run_id != token.run_id:
                token.stale = True
                token.visible = False
                return True
            self._states.pop(token.scope_key, None)
            if (
                state.anchor_message_id
                and self._anchor_to_scope.get(state.anchor_message_id)
                == state.scope_key
            ):
                self._anchor_to_scope.pop(state.anchor_message_id, None)
            if state.visible and state.handle:
                handle_to_delete = state.handle
            state.visible = False
            token.visible = False

        if handle_to_delete is not None:
            self._schedule(self._delete_handle(handle_to_delete, reason=reason))
        return True

    async def finish_by_anchor(
        self,
        anchor_message_id: str | None,
        *,
        expected_scope: str = "",
        reason: str,
    ) -> bool:
        """Finish the current run anchored to *anchor_message_id*, if any."""
        if not anchor_message_id:
            return False
        with self._lock:
            if expected_scope:
                state = self._states.get(expected_scope)
                if (
                    state is None
                    or state.anchor_message_id != anchor_message_id
                ):
                    return False
            else:
                scope_key = self._anchor_to_scope.get(anchor_message_id)
                state = self._states.get(scope_key or "") if scope_key else None
            token = state.token if state is not None else None
        if token is None:
            return False
        return await self.finish(token, reason=reason)

    def _schedule(self, awaitable: Awaitable[Any]) -> None:
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_task_exception)

    @staticmethod
    def _log_task_exception(task: asyncio.Task[Any]) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            gw_log().error(
                "[iflow:reaction] background task failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _add_handle(self, handle: dict[str, Any]) -> bool:
        try:
            ok = await self._add_reaction(handle)
            return bool(ok)
        except Exception:
            gw_log().exception("[iflow:reaction] add raised")
            return False

    async def _delete_handle(self, handle: dict[str, Any], *, reason: str) -> bool:
        gw_log().info(
            "[iflow:reaction] delete reason=%s mid=%s",
            reason,
            handle.get("base_msg_id", "-"),
        )
        try:
            ok = await self._delete_reaction(handle)
            return bool(ok)
        except Exception:
            gw_log().exception("[iflow:reaction] del raised")
            return False
