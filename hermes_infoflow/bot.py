"""Bot message processor — all business logic lives here.

Extracted from ``adapter.py`` so that message-processing logic (dedup,
robot-id discovery, own-message filtering, inbound-context registration,
policy evaluation, message store, and dispatch orchestration) lives in
one focused module.

The adapter remains a thin format-translation layer:
  ``Hermes format ←→ bot format ←→ serverapi format``
"""

from __future__ import annotations

import asyncio
import contextvars
import aiohttp
import json
import logging
import os
import time
from typing import Any, TYPE_CHECKING
from contextlib import contextmanager
from pathlib import Path

from .message_store import MessageStore
from .policy import (
    Action,
    GroupPolicy,
    PolicyDecision,
    evaluate_inbound,
)
from .recall import (
    _InboundContext,
    _register_inbound_context,
    correct_inbound_confusion,
    format_recall_candidates,
    no_recall_error,
    reply_to_bot_from_current_inbound,
)
from .sent_store import SentMessageStore, SentMessage
from .itypes import (
    IncomingMessage,
    ProcessResult,
    RecallResult,
    ReplyInfo,
    SendOptions,
    SentResult,
)

if TYPE_CHECKING:  # pragma: no cover — avoid circular import at runtime
    from .serverapi import ServerAPI
    from .adapter import InfoflowAdapter

logger = logging.getLogger(__name__)


def gw_log() -> logging.Logger:
    """Return the gateway.run logger so audit lines reach gateway.log."""
    return logging.getLogger("gateway.run")


# ---------------------------------------------------------------------------
# robot_id persistence (mirrors openclaw-infoflow/src/bot.ts:738-755)
# ---------------------------------------------------------------------------

_ROBOT_ID_PATH: str | None = None


def _get_robot_id_path() -> str:
    global _ROBOT_ID_PATH
    if _ROBOT_ID_PATH is not None:
        return _ROBOT_ID_PATH
    state_dir = os.environ.get("HERMES_STATE_DIR") or str(
        Path.home() / ".hermes" / "state" / "infoflow"
    )
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    _ROBOT_ID_PATH = os.path.join(state_dir, "robot_id.json")
    return _ROBOT_ID_PATH


def _persist_robot_id(robot_id: str) -> None:
    """Write the discovered robot_id to disk."""
    try:
        path = _get_robot_id_path()
        with open(path, "w") as f:
            json.dump({"robot_id": robot_id}, f)
        logger.info("[infoflow] persisted robot_id=%s to %s", robot_id, path)
    except Exception:
        logger.warning("[infoflow] failed to persist robot_id", exc_info=True)


def load_persisted_robot_id() -> str | None:
    """Load a previously persisted robot_id from disk (or None)."""
    try:
        path = _get_robot_id_path()
        if not os.path.isfile(path):
            return None
        with open(path) as f:
            data = json.load(f)
        return str(data.get("robot_id") or "")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Inbound message-id hint (thread-local scope for recall)
# ---------------------------------------------------------------------------

_recall_hint: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_recall_hint", default=None
)


@contextmanager
def recall_inbound_message_id_hint_scope(message_id: str | None):
    """Set the current inbound message_id hint for recall resolution."""
    token = _recall_hint.set(message_id)
    try:
        yield
    finally:
        token.var.reset(token)


def get_recall_inbound_message_id_hint() -> str | None:
    """Return the current inbound message_id hint (for recall handlers)."""
    return _recall_hint.get(None)


# ---------------------------------------------------------------------------
# Bot — stateful message processor
# ---------------------------------------------------------------------------

# Module-level ContextVars for follow-up dispatch state.  Must be module-level
# (not instance-level) so that asyncio.create_task() context copies work
# correctly across concurrent dispatch tasks.
_dispatch_followup_cv: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "dispatch_is_followup", default=False,
)
_dispatch_followup_passive_cv: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "dispatch_is_passive_followup", default=False,
)
_dispatch_inbound_text_cv: contextvars.ContextVar[str] = contextvars.ContextVar(
    "dispatch_inbound_text", default="",
)


class Bot:
    """All business logic for the infoflow plugin.

    Owns runtime state (robot_id, dedup, policy, inbound context, stores)
    and exposes:
    - ``process_inbound()`` — full inbound pipeline
    - ``send_message()`` — send with logging + dedup + follow-up tracking
    - ``send_image()`` — send image with logging
    - ``recall_message()`` — recall with confusion correction + logging
    """

    def __init__(
        self,
        *,
        settings: dict[str, Any],
        policy: GroupPolicy,
        serverapi: ServerAPI,
        sent_store: SentMessageStore,
        dedup_set: set[str],
        message_store: MessageStore,
        llm_config: dict[str, str] | None = None,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._serverapi = serverapi
        self._sent_store = sent_store
        self._dedup_set = dedup_set
        self._message_store = message_store
        self._robot_id: str = str(settings.get("robot_id") or "")
        self._llm_config = llm_config or {}
        self._http_session = http_session  # shared with adapter
        gw_log().info(
            "[infoflow] Bot init: llm_config=%s, has_session=%s, robot_id=%s",
            bool(self._llm_config), self._http_session is not None, self._robot_id,
        )

    # -- helpers -------------------------------------------------------------

    def _get_http_session(self) -> aiohttp.ClientSession:
        """Return the shared aiohttp session (owned by adapter)."""
        if self._http_session is None:
            raise RuntimeError("Bot._http_session not set — adapter must pass its session")
        return self._http_session

    # -- robot_id management ------------------------------------------------

    @property
    def robot_id(self) -> str:
        return self._robot_id

    @robot_id.setter
    def robot_id(self, value: str) -> None:
        if value and value != self._robot_id:
            self._robot_id = value

    # -- dedup set (shared with adapter for outbound recording) -------------

    @property
    def dedup_set(self) -> set[str]:
        return self._dedup_set

    # -- policy (shared with adapter for record_bot_reply) ------------------

    @property
    def policy(self) -> GroupPolicy:
        return self._policy

    # -- serverapi reference (adapter calls this for group members) ---------

    @property
    def serverapi(self) -> ServerAPI:
        return self._serverapi

    # ======================================================================
    # INBOUND pipeline
    # ======================================================================

    async def process_inbound(self, msg: IncomingMessage) -> ProcessResult:
        """Run the full inbound processing pipeline.

        Steps (mirrors openclaw bot.ts):
        1. Discover/update robot_id
        2. Dedup check (plugin-sent echoes)
        3. Own-message echo filter (external channel echoes → RECORD)
        4. Register inbound context (for recall correction)
        5. Policy evaluation
        6. Return dispatch decision
        """
        _chat = self._chat_label(msg)
        _text_preview = (msg.text or "")[:120]

        # Audit log
        logger.info(
            "[infoflow] inbound decoded: from=%s chat=%s group=%s mentioned=%s mid=%s text=%r",
            msg.sender_id, _chat, msg.is_group, msg.bot_was_mentioned,
            msg.msgid or "-", _text_preview,
        )

        # Step 1: Discover robot_id from @-mention body items
        if msg.discovered_robot_id and msg.discovered_robot_id != self._robot_id:
            self._robot_id = msg.discovered_robot_id
            self._serverapi.robot_id = self._robot_id
            logger.info(
                "[infoflow] discovered robotId=%s for account %s",
                self._robot_id, self._settings.get("app_agent_id"),
            )
            gw_log().info("[infoflow] discovered robotId=%s", self._robot_id)
            _persist_robot_id(self._robot_id)

        # Step 2: Dedup check (before echo filter — plugin-sent echoes are already mark_seen)
        dedupe_key = msg.dedupe_key
        if dedupe_key and self._sent_store.is_duplicate(dedupe_key):
            gw_log().info(
                "[iflow:decision] mid=%s action=DROP reason=own-echo:plugin-sent text=%r",
                msg.msgid or "-", _text_preview,
            )
            return ProcessResult()
        if dedupe_key:
            self._sent_store.mark_seen(dedupe_key)

        # Step 3: Own-message echo filter (ALL_MESSAGE_FORWARD)
        # If dedup didn't catch it but fromid==robot_id, this echo came from
        # an external channel (infoflow-cli, another tool, etc.) → persist but don't dispatch.
        if (
            self._robot_id
            and msg.is_group
            and msg.sender_imid
            and msg.sender_imid == self._robot_id
        ):
            gw_log().info(
                "[iflow:decision] mid=%s action=RECORD reason=own-echo:external text=%r",
                msg.msgid or "-", _text_preview,
            )
            self._register_context(msg)
            return ProcessResult(decision=PolicyDecision(action=Action.RECORD, reason="own-echo:external"))

        # Step 4: Register inbound context + record to message store
        self._register_context(msg)

        # Step 4b: If this message @mentioned the bot, record sender mention
        # for follow-up engaged/passive template selection.
        if getattr(msg, "bot_was_mentioned", False) and msg.group_id and msg.sender_id:
            self._policy.record_sender_mention(msg.group_id, msg.sender_id)
            gw_log().info(
                "[iflow:decision] mid=%s step=record_mention sender=%s group=%s",
                msg.msgid or "-", msg.sender_id, msg.group_id,
            )

        # Step 5: Policy evaluation
        decision = evaluate_inbound(msg, self._policy)
        if not decision.should_dispatch:
            gw_log().info(
                "[iflow:decision] mid=%s action=%s reason=%s sender=%s text=%r",
                msg.msgid or "-", decision.action.value, decision.reason,
                msg.sender_name or msg.sender_id, _text_preview,
            )
            return ProcessResult(decision=decision)

        # Step 5b: LLM intent classification for follow-up messages
        # Only triggered for follow-up window hits — @mention / watch hits skip this.
        gw_log().info(
            "[iflow:decision] mid=%s step5b trigger=%s llm_config=%s",
            msg.msgid or "-", decision.trigger_reason, bool(self._llm_config),
        )
        if decision.trigger_reason == "followUp" and self._llm_config:
            _intent = await self._classify_followup_intent(msg)
            if _intent == "other":
                gw_log().info(
                    "[iflow:decision] mid=%s action=RECORD reason=followUp-intent:other sender=%s text=%r",
                    msg.msgid or "-", msg.sender_name or msg.sender_id, _text_preview,
                )
                return ProcessResult(decision=PolicyDecision(action=Action.RECORD, reason="followUp-intent:other"))
            if _intent == "bot":
                gw_log().info(
                    "[iflow:decision] mid=%s action=DISPATCH reason=followUp-intent:bot sender=%s text=%r",
                    msg.msgid or "-", msg.sender_name or msg.sender_id, _text_preview,
                )
            elif _intent is None:
                # LLM call failed → safe fallback: dispatch (with prompt guard)
                gw_log().info(
                    "[iflow:decision] mid=%s step=intent_classify result=FAILED action=dispatch_fallback",
                    msg.msgid or "-",
                )
            # _intent == "none" → fall through to dispatch (Layer 2 + 3)
            elif _intent == "none":
                gw_log().info(
                    "[iflow:decision] mid=%s step=intent_classify result=none action=dispatch_with_guard",
                    msg.msgid or "-",
                )

        # Step 6: Dispatch
        gw_log().info(
            "[iflow:decision] mid=%s action=DISPATCH trigger=%s reason=%s sender=%s text=%r",
            msg.msgid or "-", decision.trigger_reason, decision.reason,
            msg.sender_name or msg.sender_id, _text_preview,
        )
        return ProcessResult(should_dispatch=True, decision=decision)

    # -- context registration + message store -------------------------------

    def _register_context(self, msg: IncomingMessage) -> None:
        """Register inbound context for recall + persist to message store."""
        if not msg.msgid:
            return
        target = (
            f"group:{msg.group_id}" if msg.is_group else (msg.dm_user_id or "")
        )

        reply_to_bot_id: str | None = None
        for tgt in msg.reply_targets:
            if tgt.get("isBotMessage"):
                reply_to_bot_id = str(tgt.get("messageid") or "") or None
                break

        _register_inbound_context(
            _InboundContext(
                account_id=self._settings.get("app_key") or "default",
                target=target,
                inbound_message_id=msg.msgid,
                reply_to_bot_message_id=reply_to_bot_id,
                reply_targets=list(msg.reply_targets),
                inbound_body=msg.text or "",
                sender_imid=msg.sender_imid or "",
                registered_at=time.time(),
                msgseqid=msg.msgseqid,
            )
        )

        # Persist to unified message store.
        raw_json = json.dumps(msg.raw_data, ensure_ascii=False) if msg.raw_data else ""
        if msg.dm_user_id is not None:
            self._message_store.persist_dm(
                message_id=msg.msgid,
                dm_user_id=msg.dm_user_id,
                sender_id=msg.sender_id or msg.sender_imid or "",
                sender_name=msg.sender_name or "",
                sender_imid=msg.sender_imid or "",
                sender_is_bot=msg.sender_is_bot,
                is_inbound=True,
                text=msg.text or "",
                raw_json=raw_json,
            )
        elif msg.group_id is not None:
            self._message_store.persist_group(
                message_id=msg.msgid,
                group_id=msg.group_id,
                sender_id=msg.sender_id or msg.sender_imid or "",
                sender_name=msg.sender_name or "",
                sender_imid=msg.sender_imid or "",
                sender_is_bot=msg.sender_is_bot,
                is_inbound=True,
                bot_was_mentioned=msg.bot_was_mentioned,
                text=msg.text or "",
                raw_json=raw_json,
            )

    # -- LLM judge helpers ---------------------------------------------------

    async def _classify_followup_intent(self, msg: IncomingMessage) -> str | None:
        """Layer 1: classify whether a follow-up message targets the bot."""
        from .llm_judge import classify_followup_intent

        try:
            return await classify_followup_intent(
                self._get_http_session(),
                text=msg.text or "",
                sender_name=msg.sender_name or msg.sender_id or "",
                is_bot=msg.sender_is_bot,
                bot_name=self._settings.get("robot_name") or "bot",
                config=self._llm_config,
            )
        except Exception:
            logger.debug("[llm_judge] intent classification error", exc_info=True)
            return None

    async def _evaluate_reply_value(self, reply_text: str) -> bool | None:
        """Layer 3: evaluate whether a generated reply is worth sending."""
        from .llm_judge import evaluate_reply_value

        try:
            return await evaluate_reply_value(
                self._get_http_session(),
                original_text=_dispatch_inbound_text_cv.get(""),
                reply_text=reply_text,
                config=self._llm_config,
            )
        except Exception:
            logger.debug("[llm_judge] reply value evaluation error", exc_info=True)
            return None  # safe fallback: send

    # -- dispatch orchestration ---------------------------------------------

    async def dispatch_inbound(
        self,
        msg: IncomingMessage,
        decision: PolicyDecision,
        adapter: InfoflowAdapter,
    ) -> None:
        """Build event and dispatch to agent via adapter."""
        # Propagate mid via contextvar so send() can trace it
        from .adapter import _inbound_mid
        _inbound_mid.set(msg.msgid or "")
        # Mark follow-up dispatch context for Layer 3 (reply value check)
        _dispatch_followup_cv.set(decision.trigger_reason == "followUp")
        _dispatch_inbound_text_cv.set(msg.text or "")
        hint = msg.msgid or None
        try:
            with recall_inbound_message_id_hint_scope(hint):
                event = await adapter.build_message_event(msg, decision)
                # Read passive flag set by adapter during build_message_event
                _dispatch_followup_passive_cv.set(
                    getattr(adapter, "_last_followup_is_passive", False)
                )
                await adapter.handle_message(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[infoflow] inbound dispatch failed")
        finally:
            _dispatch_followup_cv.set(False)
            _dispatch_followup_passive_cv.set(False)
            _dispatch_inbound_text_cv.set("")

    def spawn_dispatch(
        self,
        msg: IncomingMessage,
        decision: PolicyDecision,
        adapter: InfoflowAdapter,
        background_tasks: set[asyncio.Task[Any]],
    ) -> None:
        """Schedule dispatch_inbound as a background task (fire-and-forget)."""
        task = asyncio.create_task(
            self.dispatch_inbound(msg, decision, adapter)
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    # ======================================================================
    # OUTBOUND — send
    # ======================================================================

    async def send_message(
        self,
        *,
        group_id: str | None = None,
        dm_user_id: str | None = None,
        text: str,
        reply_info: ReplyInfo | None = None,
        options: SendOptions | None = None,
        session: Any = None,
    ) -> SentResult:
        """Send a text/markdown message.  Handles NO_REPLY, chunking,
        dedup, message store, and follow-up tracking.
        """
        from .adapter import _inbound_mid as _mid_var

        # NO_REPLY sentinel — suppress outbound message
        stripped = (text or "").strip()
        # Normalize: collapse whitespace, strip punctuation/emoji after NO_REPLY
        _nr_first_line = stripped.split("\n")[0].strip()
        _nr_clean = _nr_first_line.strip("。，,.！!？?~～ \t;；:：")
        if _nr_clean == "NO_REPLY":
            gw_log().info("[iflow:send] mid=%s NO_REPLY sentinel suppressed", _mid_var.get(""))
            return SentResult(success=True)

        # Layer 3: Reply value evaluation (follow-up group messages only)
        if (
            _dispatch_followup_cv.get(False)
            and group_id is not None
            and self._llm_config
        ):
            _should_send = await self._evaluate_reply_value(stripped)
            if _should_send is False:
                gw_log().info(
                    "[iflow:send] mid=%s target=group:%s chars=%d action=SUPPRESSED (Layer 3: low value)",
                    _mid_var.get(""),
                    group_id,
                    len(stripped),
                )
                return SentResult(success=True)
            elif _should_send is None:
                # Layer 3 failed (timeout/error) → graded fallback
                if _dispatch_followup_passive_cv.get(False):
                    # Passive template: default to NOT sending on failure
                    gw_log().info(
                        "[iflow:send] mid=%s Layer3=None passive_followup → SUPPRESS",
                        _mid_var.get(""),
                    )
                    return SentResult(success=True)
                # Engaged / reply-to-bot: allow through
                gw_log().info(
                    "[iflow:send] mid=%s Layer3=None engaged → ALLOW",
                    _mid_var.get(""),
                )
            else:
                gw_log().info(
                    "[iflow:send] mid=%s step=layer3_eval result=%s action=SEND",
                    _mid_var.get(""), _should_send,
                )

        # Truncate into chunks (Hermes convention, 2KB per message ceiling)
        from gateway.platforms.base import BasePlatformAdapter  # type: ignore[import-not-found]
        chunks = BasePlatformAdapter.truncate_message(text, 2000)
        if not chunks:
            chunks = [""]

        store_key = self._normalize_store_key(group_id, dm_user_id)
        last_msgid: str = ""
        first_error: str = ""
        failed = 0
        succeeded = 0

        for idx, chunk in enumerate(chunks):
            # Options (mention metadata) only apply to the first chunk
            opts = options if idx == 0 else None

            if group_id is not None:
                result = await self._serverapi.send_to_group(
                    group_id, chunk,
                    reply_info=reply_info if idx == 0 else None,
                    options=opts, session=session,
                )
            elif dm_user_id is not None:
                result = await self._serverapi.send_to_dm(
                    dm_user_id, chunk, options=opts, session=session,
                )
            else:
                result = SentResult(success=False, error="no target specified")

            if result.success:
                succeeded += 1
                mid = result.msgid
                if mid:
                    self._sent_store.record(
                        chat_id=store_key,
                        messageid=mid,
                        msgseqid=result.msgseqid,
                        digest=chunk[:80],
                    )
                    self._record_sent(
                        msgid=mid, text=chunk,
                        group_id=group_id, dm_user_id=dm_user_id,
                    )
                    last_msgid = mid
            else:
                failed += 1
                if not first_error:
                    first_error = result.error

        # Follow-up window: record bot reply timestamp for group messages
        if succeeded and group_id:
            self._policy.record_bot_reply(group_id)
            gw_log().info("[iflow:send] mid=%s step=record_bot_reply group=%s", _mid_var.get(""), group_id)

        if first_error:
            return SentResult(
                success=False,
                msgid=last_msgid,
                error=(
                    f"{first_error} (succeeded={succeeded}, failed={failed} of {len(chunks)} chunks)"
                    if succeeded else first_error
                ),
            )
        return SentResult(success=True, msgid=last_msgid)

    # ======================================================================
    # OUTBOUND — send image
    # ======================================================================

    async def send_image(
        self,
        *,
        group_id: str | None = None,
        dm_user_id: str | None = None,
        image_bytes: bytes,
        caption: str | None = None,
        reply_info: ReplyInfo | None = None,
        session: Any = None,
    ) -> SentResult:
        """Send an image (optionally with caption)."""
        if group_id is not None:
            result = await self._serverapi.send_image_to_group(
                group_id, image_bytes,
                caption=caption, reply_info=reply_info, session=session,
            )
        elif dm_user_id is not None:
            result = await self._serverapi.send_image_to_dm(
                dm_user_id, image_bytes, caption=caption, session=session,
            )
        else:
            return SentResult(success=False, error="no target specified")

        if result.success:
            store_key = self._normalize_store_key(group_id, dm_user_id)
            mid = result.msgid
            if mid:
                self._sent_store.record(
                    chat_id=store_key, messageid=mid,
                    msgseqid=result.msgseqid, digest="[image]",
                )
                self._record_sent(
                    msgid=mid, text="[image]",
                    group_id=group_id, dm_user_id=dm_user_id,
                )
            if group_id:
                self._policy.record_bot_reply(group_id)
                gw_log().info("[iflow:send] mid=%s step=record_bot_reply group=%s (image)", _mid_var.get(""), group_id)
        return result

    # ======================================================================
    # OUTBOUND — recall
    # ======================================================================

    async def recall_message(
        self,
        *,
        group_id: str | None = None,
        dm_user_id: str | None = None,
        msgid: str | None = None,
        msgseqid: str = "",
        count: int = 1,
        current_inbound_msgid: str | None = None,
        session: Any = None,
    ) -> RecallResult:
        """Recall one or more bot-sent messages.

        Mirrors the full recall logic from the old adapter:
        - Inbound confusion correction
        - Reply-to-bot fallback
        - Count-based recent message recall
        """
        store_key = self._normalize_store_key(group_id, dm_user_id)
        account_id = self._settings.get("app_key") or "default"

        # Resolve current inbound hint
        if current_inbound_msgid is None:
            current_inbound_msgid = get_recall_inbound_message_id_hint()

        # Aggressive guard: when a hint is present, only treat msgid
        # as the inbound id if it matches the hint.
        if msgid:
            inbound_key_for_aggressive: str | None = None
            if current_inbound_msgid:
                if msgid == current_inbound_msgid:
                    inbound_key_for_aggressive = current_inbound_msgid
            else:
                inbound_key_for_aggressive = msgid

            corrected = None
            if inbound_key_for_aggressive:
                corrected = correct_inbound_confusion(
                    inbound_message_id=inbound_key_for_aggressive,
                    store_key=store_key,
                    account_id=account_id,
                    sent_store=self._sent_store,
                )
            if corrected is not None and corrected.get("kind") == "swap":
                logger.info(
                    "[bot:recall] auto-swap inbound id=%s -> bot msg id=%s",
                    msgid, corrected.get("message_id"),
                )
                msgid = str(corrected["message_id"])
            elif corrected is not None and corrected.get("kind") == "drop_to_count":
                logger.info("[bot:recall] auto-correct: drop to count=1")
                msgid = None
                count = 1

        targets: list[tuple[str, str]] = []  # (msgid, msgseqid)

        if msgid:
            entry = self._sent_store.find(store_key, msgid)
            need_reply_fallback = (
                entry is None or not (entry.msgseqid or "").strip()
            ) if group_id else entry is None

            if need_reply_fallback and current_inbound_msgid:
                fb_entry = reply_to_bot_from_current_inbound(
                    current_inbound_message_id=current_inbound_msgid,
                    store_key=store_key,
                    account_id=account_id,
                    sent_store=self._sent_store,
                )
                if fb_entry is not None:
                    ok_use = (group_id is None) or bool((fb_entry.msgseqid or "").strip())
                    if ok_use:
                        logger.info(
                            "[bot:recall] fallback: msgid=%s -> bot id=%s",
                            msgid, fb_entry.messageid,
                        )
                        msgid = fb_entry.messageid
                        entry = fb_entry

            seq = (entry.msgseqid if entry else "") or ""
            targets.append((msgid, seq))
        else:
            for entry in self._sent_store.recent(store_key, max(1, count)):
                targets.append((entry.messageid, entry.msgseqid))

        if not targets:
            return RecallResult(
                success=False,
                error=no_recall_error(self._sent_store, store_key),
            )

        first_error: str = ""
        recalled_ids: list[str] = []

        for mid, seq in targets:
            if group_id is not None:
                if not seq:
                    if not first_error:
                        candidates = format_recall_candidates(self._sent_store, store_key)
                        first_error = (
                            f"messageId={mid} is not a known bot-sent group message "
                            "(msgseqid unavailable)."
                            + (f" Recent bot messages here: {candidates}." if candidates else "")
                        )
                    continue
                result = await self._serverapi.recall_group_message(
                    group_id, mid, seq, session=session,
                )
            else:
                result = await self._serverapi.recall_private_message(
                    mid, session=session,
                )
            if result.success:
                recalled_ids.append(mid)
                try:
                    self._sent_store.remove(store_key, mid)
                except Exception:
                    logger.debug("sent_store.remove failed", exc_info=True)
            elif not first_error:
                first_error = result.error

        if not recalled_ids:
            return RecallResult(success=False, error=first_error or "recall failed")
        return RecallResult(success=True)

    # ======================================================================
    # Internal helpers
    # ======================================================================

    def _record_sent(
        self,
        *,
        msgid: str,
        text: str,
        group_id: str | None = None,
        dm_user_id: str | None = None,
    ) -> None:
        """Persist a bot-sent message to the unified message store."""
        try:
            if group_id is not None:
                self._message_store.persist_group(
                    message_id=msgid,
                    group_id=group_id,
                    sender_id=self._robot_id or "",
                    sender_name=self._settings.get("robot_name") or "",
                    sender_imid=self._robot_id or "",
                    is_inbound=False,
                    text=text,
                    digest=text[:80],
                )
            elif dm_user_id is not None:
                self._message_store.persist_dm(
                    message_id=msgid,
                    dm_user_id=dm_user_id,
                    sender_id=self._robot_id or "",
                    sender_name=self._settings.get("robot_name") or "",
                    sender_imid=self._robot_id or "",
                    is_inbound=False,
                    text=text,
                    digest=text[:80],
                )
        except Exception:
            logger.debug("[bot] _record_sent failed", exc_info=True)

    @staticmethod
    def _normalize_store_key(
        group_id: str | None, dm_user_id: str | None
    ) -> str:
        """Return canonical store key for sent_store lookups."""
        if group_id is not None:
            return f"group:{group_id}"
        return dm_user_id or ""

    @staticmethod
    def _chat_label(msg: IncomingMessage) -> str:
        if msg.is_group and msg.group_id:
            return f"group:{msg.group_id}"
        return msg.sender_id or "unknown"


# Backward-compatible aliases for tools.py
BotProcessor = Bot
