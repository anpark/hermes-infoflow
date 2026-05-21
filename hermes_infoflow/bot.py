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
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .itypes import (
    IncomingMessage,
    ProcessResult,
    RecallResult,
    ReplyInfo,
    SendOptions,
    SentResult,
)
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
from .sent_store import SentMessageStore
from .utils import gw_log

if TYPE_CHECKING:  # pragma: no cover — avoid circular import at runtime
    from .adapter import InfoflowAdapter
    from .serverapi import ServerAPI

logger = logging.getLogger(__name__)


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

# Module-level ContextVar carrying the current dispatch trigger reason
# ("bot-mentioned" / "watchMentions:..." / "watchRegex#..." / "followUp"
# / "proactive" / "direct-message" / ...).  Set in dispatch_inbound entry,
# reset in finally.  Used in send_message to:
#   (a) log which path produced the outbound;
#   (b) gate the static refusal-regex filter to the followUp path only.
_send_path_cv: contextvars.ContextVar[str] = contextvars.ContextVar(
    "send_path", default="",
)

# Static refusal-regex filter — used in send_message as a zero-latency
# fallback against GLM occasionally violating the NO_REPLY contract.
# Matches refusal declarations at the start of a line; only applied on the
# followUp path so that @bot/watch responses like "暂时帮不上" are not killed.
_REFUSAL_RE = re.compile(
    r"(?:^|\n)\s*("
    r"作为(?:一个)?\s*AI[,，]?|"
    r"我无法|我没法|我不能|"
    r"(?:很)?抱歉[,，]?\s*(?:我)?\s*(?:目前|当前)?\s*(?:无法|不能|不太|没有|帮不上)"
    r")",
    re.IGNORECASE,
)

# Characters stripped when matching the NO_REPLY sentinel.  Includes CJK
# punctuation/emoji-adjacent symbols + ASCII whitespace so that variants
# like "NO_REPLY。" / "NO_REPLY ~" still suppress.
_NO_REPLY_PUNCT = "。，,.！!？?~～ \t\n;；:："

# Emoji reaction shown while LLM is processing (敲键盘).
_EMOJI_PROCESSING = ("d135", "(qjp)")


def no_reply_sentinel_hits(text: str | None) -> bool:
    """True iff ``text`` should be suppressed by the NO_REPLY sentinel.

    Acceptance rules (kept in sync with the merged-prompt contract):
      1) full text (strip whitespace + ``_NO_REPLY_PUNCT``) == "NO_REPLY", or
      2) first line (same strip) == "NO_REPLY".

    Deliberately does NOT match "NO_REPLY" appearing only on a middle/last
    line — we'd rather ship a real answer followed by an accidental
    sentinel than swallow the entire message.
    """
    if not text:
        return False
    stripped = text.strip()
    full_clean = stripped.strip(_NO_REPLY_PUNCT)
    if full_clean == "NO_REPLY":
        return True
    first_line = stripped.split("\n", 1)[0].strip().strip(_NO_REPLY_PUNCT)
    return first_line == "NO_REPLY"


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
        admin_uid: str = "",
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._serverapi = serverapi
        self._sent_store = sent_store
        self._dedup_set = dedup_set
        self._message_store = message_store
        self._admin_uid = admin_uid.strip().lower()
        self._robot_id: str = str(settings.get("robot_id") or "")
        gw_log().info("[infoflow] Bot init: robot_id=%s admin_uid=%s", self._robot_id, self._admin_uid)

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

        # --- Slash command fast path: /new, /stop ---
        raw_text = (msg.body_for_agent or msg.text or "").strip()
        if raw_text in ("/new", "/stop"):
            is_admin_cmd = self._check_slash_command_auth(msg)
            if is_admin_cmd:
                gw_log().info(
                    "[iflow:decision] mid=%s action=DISPATCH trigger=slash_command "
                    "reason=%s sender=%s",
                    msg.message_id or "-", raw_text, msg.sender_id,
                )
                return ProcessResult(
                    should_dispatch=True,
                    decision=PolicyDecision(
                        should_dispatch=True,
                        action=Action.DISPATCH,
                        reason=f"slash_command:{raw_text}",
                        trigger_reason="slash_command",
                        command_text=raw_text,
                    ),
                )

        # --- Enrich sender info (group messages only) ---
        if msg.is_group and msg.sender_imid:
            await self._enrich_sender(msg)

        # --- [iflow:event] — enriched message event fields ---
        try:
            gw_log().info(
                "[iflow:event] mid=%s sender_id=%s sender_name=%s sender_imid=%s "
                "sender_agent_id=%s is_bot=%s mentioned=%s "
                "mention_users=%s mention_agents=%s reply_to_bot=%s body=%s",
                msg.message_id, msg.sender_id, msg.sender_name, msg.sender_imid,
                getattr(msg, "sender_agent_id", ""), msg.sender_is_bot,
                msg.bot_was_mentioned,
                msg.mention_user_ids, msg.mention_agent_ids,
                msg.is_reply_to_bot, (msg.body_for_agent or "")[:200],
            )
            for _i, _b in enumerate(msg.body_items or []):
                if hasattr(_b, "type"):
                    gw_log().info(
                        "[iflow:event] body_item[%d] type=%s name=%s userid=%s robotid=%s",
                        _i, _b.type, _b.name, _b.userid, _b.robotid,
                    )
        except Exception:
            pass

        # Audit log
        gw_log().info(
            "[infoflow] inbound decoded: from=%s chat=%s group=%s mentioned=%s mid=%s text=%r",
            msg.sender_id, _chat, msg.is_group, msg.bot_was_mentioned,
            msg.message_id or "-", _text_preview,
        )

        # Step 1: Discover robot_id from @-mention body items
        if msg.discovered_robot_id and msg.discovered_robot_id != self._robot_id:
            self._robot_id = msg.discovered_robot_id
            self._serverapi.robot_id = self._robot_id
            gw_log().info(
                "[infoflow] discovered robotId=%s for account %s",
                self._robot_id, self._settings.get("app_agent_id"),
            )
            _persist_robot_id(self._robot_id)

        # Step 2: Dedup check (before echo filter — plugin-sent echoes are already mark_seen)
        dedupe_key = msg.dedupe_key
        if dedupe_key and self._sent_store.is_duplicate(dedupe_key):
            gw_log().info(
                "[iflow:decision] mid=%s action=DROP reason=own-echo:plugin-sent text=%r",
                msg.message_id or "-", _text_preview,
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
                msg.message_id or "-", _text_preview,
            )
            self._register_context(msg)
            return ProcessResult(decision=PolicyDecision(action=Action.RECORD, reason="own-echo:external"))

        # Step 4: Register inbound context + record to message store
        self._register_context(msg)

        # Step 4b: If this message @mentioned the bot, record sender mention
        # for follow-up engaged/passive template selection.
        if getattr(msg, "bot_was_mentioned", False) and msg.group_id:
            _mention_key = ""
            if msg.sender_is_bot:
                _aid = getattr(msg, "sender_agent_id", "") or ""
                if _aid and not _aid.startswith("IMID:"):
                    _mention_key = str(_aid)
            else:
                _mention_key = msg.sender_id or ""
            if _mention_key:
                self._policy.record_sender_mention(msg.group_id, _mention_key)
                gw_log().info(
                    "[iflow:decision] mid=%s step=record_mention sender=%s group=%s",
                    msg.message_id or "-", _mention_key, msg.group_id,
                )

        # Step 5: Policy evaluation
        decision = evaluate_inbound(msg, self._policy)
        if not decision.should_dispatch:
            gw_log().info(
                "[iflow:decision] mid=%s action=%s reason=%s sender=%s text=%r",
                msg.message_id or "-", decision.action.value, decision.reason,
                msg.sender_name or msg.sender_id, _text_preview,
            )
            return ProcessResult(decision=decision)

        # Step 6: Dispatch
        # (Layer 1 intent classification was removed — the merged prompt
        # templates in policy.py let the main agent self-classify and output
        # NO_REPLY directly when the message isn't for it.)
        gw_log().info(
            "[iflow:decision] mid=%s action=DISPATCH trigger=%s reason=%s sender=%s text=%r",
            msg.message_id or "-", decision.trigger_reason, decision.reason,
            msg.sender_name or msg.sender_id, _text_preview,
        )
        return ProcessResult(should_dispatch=True, decision=decision)

    # -- slash command auth -------------------------------------------------

    _SLASH_COMMANDS = frozenset({"/new", "/stop"})

    def _check_slash_command_auth(self, msg: IncomingMessage) -> bool:
        """Check if a slash command should be dispatched.

        DM: any sender's /new /stop is allowed.
        Group: only admin (admin_uid match) + bot was mentioned.
        """
        if not msg.is_group:
            return True
        if not self._admin_uid:
            return False
        sender = (msg.sender_id or "").lower()
        if msg.sender_is_bot:
            sender = (getattr(msg, "sender_agent_id", "") or "").lower()
        return sender == self._admin_uid and msg.bot_was_mentioned

    # -- enrich sender (moved from adapter.py) -----------------------------

    async def _enrich_sender(self, msg: IncomingMessage) -> None:
        """Populate sender_name / sender_agent_id from group member cache or API.

        Called for ALL group messages in the inbound pipeline (before dispatch
        decision), so even non-dispatched messages get enriched for logging
        and potential future use.

        Degradation:
          - Bot without agent_id  → ``IMID:{imid}`` (agent-level ops may fail)
          - Human without userId  → ``IMID:{imid}``
        """
        from .serverapi import CacheRetrievalPolicy, resolve_member_identity

        if not msg.group_id:
            return

        sender_info = await resolve_member_identity(
            msg.group_id,
            imid=msg.sender_imid,
            cache_policy=CacheRetrievalPolicy.RETRIEVE_FROM_CACHE_THEN_REMOTE,
            serverapi=self._serverapi,
        )

        if sender_info:
            if sender_info["is_bot"]:
                if not msg.sender_name or msg.sender_name == msg.sender_imid:
                    msg.sender_name = sender_info["name"] or msg.sender_name
                if sender_info["agent_id"]:
                    msg.sender_agent_id = str(sender_info["agent_id"])
            else:
                if sender_info["uid"] and (not msg.sender_id or msg.sender_id == msg.sender_imid):
                    msg.sender_id = sender_info["uid"]
                if not msg.sender_name or msg.sender_name == msg.sender_imid:
                    msg.sender_name = sender_info["name"] or msg.sender_id

        # Degradation: ensure mandatory fields exist
        _degraded = False
        if msg.sender_is_bot and not getattr(msg, "sender_agent_id", ""):
            msg.sender_agent_id = f"IMID:{msg.sender_imid}"
            _degraded = True
        if not msg.sender_is_bot and not msg.sender_id:
            msg.sender_id = f"IMID:{msg.sender_imid}"
            _degraded = True

        gw_log().info(
            "[infoflow-enrich] mid=%s sender=%s(%s) name=%s agent_id=%s is_bot=%s degraded=%s",
            msg.message_id, msg.sender_id, msg.sender_imid, msg.sender_name,
            getattr(msg, "sender_agent_id", ""), msg.sender_is_bot, _degraded,
        )

    # -- context registration + message store -------------------------------

    def _register_context(self, msg: IncomingMessage) -> None:
        """Register inbound context for recall + persist to message store."""
        if not msg.message_id:
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
                inbound_message_id=msg.message_id,
                reply_to_bot_message_id=reply_to_bot_id,
                reply_targets=list(msg.reply_targets),
                inbound_body=msg.text or "",
                sender_imid=msg.sender_imid or "",
                sender_id=msg.sender_id if not msg.sender_is_bot else "",
                sender_agent_id=str(getattr(msg, "sender_agent_id", "") or ""),
                registered_at=time.time(),
                msgseqid=msg.msgseqid,
                msgid2=msg.msgid2 or None,
            )
        )

        # Persist to unified message store.
        raw_json = json.dumps(msg.raw_data, ensure_ascii=False) if msg.raw_data else ""
        if msg.dm_user_id is not None:
            self._message_store.persist_dm(
                message_id=msg.message_id,
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
                message_id=msg.message_id,
                group_id=msg.group_id,
                sender_id=msg.sender_id or msg.sender_imid or "",
                sender_name=msg.sender_name or "",
                sender_imid=msg.sender_imid or "",
                sender_is_bot=msg.sender_is_bot,
                is_inbound=True,
                bot_was_mentioned=msg.bot_was_mentioned,
                msgid2=msg.msgid2 or "",
                text=msg.text or "",
                raw_json=raw_json,
            )

    # -- emoji reaction lifecycle (processing indicator) --------------------

    def _build_reaction_handle(
        self,
        msg: IncomingMessage,
        decision: PolicyDecision,
    ) -> dict[str, str] | None:
        """Return reaction API params if this dispatch should show a processing emoji."""
        if not msg.is_group or not msg.group_id or not msg.msgid2:
            return None
        if not msg.message_id:
            return None
        if getattr(decision, "command_text", ""):
            return None

        trig = decision.trigger_reason or ""
        eligible = False
        if trig == "bot-mentioned":
            eligible = True
        elif trig == "followUp":
            if msg.is_reply_to_bot:
                eligible = True
            else:
                key = self._engaged_key(msg)
                if key and self._policy.sender_engaged_recently(msg.group_id, key):
                    eligible = True
        if not eligible:
            return None

        from_uid = self._reaction_from_uid(msg)
        if not from_uid:
            return None
        return {
            "group_id": msg.group_id,
            "base_msg_id": msg.message_id,
            "msgid2": msg.msgid2,
            "from_uid": from_uid,
            "emoji_code": _EMOJI_PROCESSING[0],
            "emoji_desc": _EMOJI_PROCESSING[1],
        }

    @staticmethod
    def _reaction_from_uid(msg: IncomingMessage) -> str:
        """fromUid for emoji API: sender uuapName or bot agentId.

        Returns "" when identity is degraded (``IMID:xxx``) — the Ruliu API
        expects either a uuapName or numeric agentId, not the IMID fallback.
        """
        if msg.sender_is_bot:
            aid = (getattr(msg, "sender_agent_id", "") or "").strip()
            if aid and not aid.startswith("IMID:"):
                return aid
            return ""
        sid = msg.sender_id or ""
        if sid.startswith("IMID:"):
            return ""
        return sid

    @staticmethod
    def _engaged_key(msg: IncomingMessage) -> str:
        """Policy key for sender_engaged_recently (same as adapter follow-up)."""
        if msg.sender_is_bot:
            aid = (getattr(msg, "sender_agent_id", "") or "").strip()
            if aid and not aid.startswith("IMID:"):
                return aid
            return ""
        return msg.sender_id or ""

    async def _try_add_reaction(self, handle: dict[str, str]) -> bool:
        try:
            res = await self._serverapi.add_message_reaction(**handle)
            if not res.success:
                gw_log().warning("[iflow:reaction] add failed: %s", res.error)
            return res.success
        except Exception:
            gw_log().exception("[iflow:reaction] add raised")
            return False

    async def _try_delete_reaction(self, handle: dict[str, str]) -> None:
        try:
            res = await self._serverapi.delete_message_reaction(**handle)
            if not res.success:
                gw_log().warning("[iflow:reaction] del failed: %s", res.error)
        except Exception:
            gw_log().exception("[iflow:reaction] del raised")

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
        _inbound_mid.set(msg.message_id or "")
        _send_path_cv.set(decision.trigger_reason or "")
        hint = msg.message_id or None

        reaction = self._build_reaction_handle(msg, decision)
        if reaction:
            ok = await self._try_add_reaction(reaction)
            if not ok:
                reaction = None

        try:
            with recall_inbound_message_id_hint_scope(hint):
                event = await adapter.build_message_event(msg, decision)
                await adapter.handle_message(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            gw_log().exception("[infoflow] inbound dispatch failed")
        finally:
            if reaction:
                await self._try_delete_reaction(reaction)
            _send_path_cv.set("")

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
        _path = _send_path_cv.get("")

        # NO_REPLY sentinel — see ``no_reply_sentinel_hits`` for acceptance rules.
        if no_reply_sentinel_hits(text):
            gw_log().info(
                "[iflow:send] mid=%s path=%s NO_REPLY sentinel suppressed",
                _mid_var.get(""), _path,
            )
            return SentResult(success=True)

        # Static refusal-regex filter (zero-latency Layer-3 fallback).
        # Only applied on the followUp path so that @bot/watch responses
        # like "暂时帮不上" are not killed.
        if (
            _path == "followUp"
            and group_id is not None
            and _REFUSAL_RE.search((text or "")[:200])
        ):
            gw_log().info(
                "[iflow:send] mid=%s path=%s refusal-regex SUPPRESS preview=%r",
                _mid_var.get(""), _path, (text or "")[:60],
            )
            return SentResult(success=True)

        # Truncate into chunks (Hermes convention, 2KB per message ceiling)
        from gateway.platforms.base import BasePlatformAdapter  # type: ignore[import-not-found]
        chunks = BasePlatformAdapter.truncate_message(text, 2000)
        if not chunks:
            chunks = [""]

        store_key = self._normalize_store_key(group_id, dm_user_id)
        last_message_id: str = ""
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
                mid = result.message_id
                if mid:
                    self._sent_store.record(
                        chat_id=store_key,
                        messageid=mid,
                        msgseqid=result.msgseqid,
                        digest=chunk[:80],
                    )
                    self._record_sent(
                        message_id=mid, text=chunk,
                        group_id=group_id, dm_user_id=dm_user_id,
                    )
                    last_message_id = mid
            else:
                failed += 1
                if not first_error:
                    first_error = result.error

        # Follow-up window: record bot reply timestamp for group messages
        if succeeded and group_id:
            _reply_key = getattr(reply_info, 'sender_id', '') or ''
            self._policy.record_bot_reply(
                group_id,
                reply_to_sender=_reply_key,
            )
            gw_log().info("[iflow:send] mid=%s step=record_bot_reply group=%s reply_to=%s", _mid_var.get(""), group_id, _reply_key)

        if first_error:
            return SentResult(
                success=False,
                message_id=last_message_id,
                error=(
                    f"{first_error} (succeeded={succeeded}, failed={failed} of {len(chunks)} chunks)"
                    if succeeded else first_error
                ),
            )
        return SentResult(success=True, message_id=last_message_id)

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
        from .adapter import _inbound_mid as _mid_var
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
            mid = result.message_id
            if mid:
                self._sent_store.record(
                    chat_id=store_key, messageid=mid,
                    msgseqid=result.msgseqid, digest="[image]",
                )
                self._record_sent(
                    message_id=mid, text="[image]",
                    group_id=group_id, dm_user_id=dm_user_id,
                )
            if group_id:
                _reply_key = getattr(reply_info, 'sender_id', '') or ''
                self._policy.record_bot_reply(
                    group_id,
                    reply_to_sender=_reply_key,
                )
                gw_log().info("[iflow:send] mid=%s step=record_bot_reply group=%s (image) reply_to=%s", _mid_var.get(""), group_id, _reply_key)
        return result

    # ======================================================================
    # OUTBOUND — recall
    # ======================================================================

    async def recall_message(
        self,
        *,
        group_id: str | None = None,
        dm_user_id: str | None = None,
        message_id: str | None = None,
        msgseqid: str = "",
        count: int = 1,
        current_inbound_message_id: str | None = None,
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
        if current_inbound_message_id is None:
            current_inbound_message_id = get_recall_inbound_message_id_hint()

        # Aggressive guard: when a hint is present, only treat message_id
        # as the inbound id if it matches the hint.
        if message_id:
            inbound_key_for_aggressive: str | None = None
            if current_inbound_message_id:
                if message_id == current_inbound_message_id:
                    inbound_key_for_aggressive = current_inbound_message_id
            else:
                inbound_key_for_aggressive = message_id

            corrected = None
            if inbound_key_for_aggressive:
                corrected = correct_inbound_confusion(
                    inbound_message_id=inbound_key_for_aggressive,
                    store_key=store_key,
                    account_id=account_id,
                    sent_store=self._sent_store,
                )
            if corrected is not None and corrected.get("kind") == "swap":
                gw_log().info(
                    "[bot:recall] auto-swap inbound id=%s -> bot msg id=%s",
                    message_id, corrected.get("message_id"),
                )
                message_id = str(corrected["message_id"])
            elif corrected is not None and corrected.get("kind") == "drop_to_count":
                gw_log().info("[bot:recall] auto-correct: drop to count=1")
                message_id = None
                count = 1

        targets: list[tuple[str, str]] = []  # (message_id, msgseqid)

        if message_id:
            entry = self._sent_store.find(store_key, message_id)
            need_reply_fallback = (
                entry is None or not (entry.msgseqid or "").strip()
            ) if group_id else entry is None

            if need_reply_fallback and current_inbound_message_id:
                fb_entry = reply_to_bot_from_current_inbound(
                    current_inbound_message_id=current_inbound_message_id,
                    store_key=store_key,
                    account_id=account_id,
                    sent_store=self._sent_store,
                )
                if fb_entry is not None:
                    ok_use = (group_id is None) or bool((fb_entry.msgseqid or "").strip())
                    if ok_use:
                        gw_log().info(
                            "[bot:recall] fallback: message_id=%s -> bot id=%s",
                            message_id, fb_entry.messageid,
                        )
                        message_id = fb_entry.messageid
                        entry = fb_entry

            seq = (entry.msgseqid if entry else "") or ""
            targets.append((message_id, seq))
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
        message_id: str,
        text: str,
        group_id: str | None = None,
        dm_user_id: str | None = None,
    ) -> None:
        """Persist a bot-sent message to the unified message store."""
        try:
            if group_id is not None:
                self._message_store.persist_group(
                    message_id=message_id,
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
                    message_id=message_id,
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
