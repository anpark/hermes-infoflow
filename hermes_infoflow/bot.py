"""Bot message processor — all business logic lives here.

Extracted from ``adapter.py`` so that message-processing logic (dedup,
robot-id discovery, own-message filtering, inbound-context registration,
policy evaluation, message store, and dispatch orchestration) lives in
one focused module.

The adapter remains a thin format-translation layer:
  ``Hermes format ←→ bot format ←→ send service ←→ serverapi format``
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

from .identity import bot_key, private_peer_key, self_key, sender_key, user_key
from .itypes import (
    IncomingMessage,
    ProcessResult,
    RecallResult,
    ReplyInfo,
    SendOptions,
    SentResult,
    coerce_reply_target,
    reply_target_to_dict,
)
from .message_content import render_message_content
from .message_store import MessageStore
from .policy import (
    Action,
    GroupPolicy,
    PolicyDecision,
    _resolve_for_group,
    _watch_regex_match,
    evaluate_inbound,
)
from .reactions import ReactionController, ReactionRunToken
from .recall import (
    _InboundContext,
    _register_inbound_context,
    correct_inbound_confusion,
    format_recall_candidates,
    get_inbound_body,
    get_inbound_sender_imid,
    no_recall_error,
    reply_to_bot_from_current_inbound,
)
from .send_service import InfoflowSendService
from .sent_store import SentMessageStore
from .settings import infoflow_op_channel_from_env, parse_infoflow_admin_users
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

_reaction_promise_cv: contextvars.ContextVar[ReactionRunToken | None] = (
    contextvars.ContextVar("reaction_promise", default=None)
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
_REACTION_FALLBACK_CLEANUP_SECONDS = 10 * 60


def no_reply_sentinel_hits(text: str | None) -> bool:
    """True iff ``text`` should be suppressed by the NO_REPLY sentinel.

    Acceptance rules (kept in sync with the merged-prompt contract):
      1) full text (strip whitespace + ``_NO_REPLY_PUNCT``) == "NO_REPLY";
      2) first non-empty line (same strip) == "NO_REPLY"; or
      3) last non-empty line (same strip) == "NO_REPLY".

    Deliberately does NOT match "NO_REPLY" appearing only in the middle of a
    longer message, or as an inline substring. GLM often appends a trailing
    sentinel after an explanation when it meant to stay silent; suppress that.
    """
    if not text:
        return False
    stripped = text.strip()
    full_clean = stripped.strip(_NO_REPLY_PUNCT)
    if full_clean == "NO_REPLY":
        return True
    lines = [line for line in stripped.splitlines() if line.strip()]
    if not lines:
        return False
    return (
        lines[0].strip().strip(_NO_REPLY_PUNCT) == "NO_REPLY"
        or lines[-1].strip().strip(_NO_REPLY_PUNCT) == "NO_REPLY"
    )


def _no_reply_sentinel_residual(text: str | None) -> str:
    """Return non-sentinel content left after dropping edge NO_REPLY lines."""
    if not no_reply_sentinel_hits(text):
        return str(text or "")
    lines = str(text or "").strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[0].strip().strip(_NO_REPLY_PUNCT) == "NO_REPLY":
        lines.pop(0)
    if lines and lines[-1].strip().strip(_NO_REPLY_PUNCT) == "NO_REPLY":
        lines.pop()
    return "\n".join(lines).strip()


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
        send_service: InfoflowSendService | None = None,
        admin_uid: str = "",
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._serverapi = serverapi
        self._sent_store = sent_store
        self._dedup_set = dedup_set
        self._message_store = message_store
        self._send_service = send_service or self._make_send_service()
        self._admin_users = parse_infoflow_admin_users(admin_uid)
        self._admin_uid = ",".join(self._admin_users)
        self._robot_id: str = str(settings.get("robot_id") or "")
        self._reaction_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._reaction_cleanup_tasks_by_run: dict[str, asyncio.Task[Any]] = {}
        self._reactions = ReactionController(
            add_reaction=self._try_add_reaction,
            delete_reaction=self._try_delete_reaction,
        )
        gw_log().info("[infoflow] Bot init: robot_id=%s admin_uid=%s", self._robot_id, self._admin_uid)
        self._upsert_self_participant()

    # -- robot_id management ------------------------------------------------

    @property
    def robot_id(self) -> str:
        return self._robot_id

    @robot_id.setter
    def robot_id(self, value: str) -> None:
        if value and value != self._robot_id:
            self._robot_id = value
            self._upsert_self_participant()

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

    def _make_send_service(self) -> InfoflowSendService:
        return InfoflowSendService(
            serverapi=self._serverapi,
            message_store=self._message_store,
            inbound_body_lookup=get_inbound_body,
            inbound_sender_imid_lookup=get_inbound_sender_imid,
        )

    def _active_send_service(self) -> InfoflowSendService:
        if (
            getattr(self._send_service, "_serverapi", None) is not self._serverapi
            or getattr(self._send_service, "_message_store", None) is not self._message_store
        ):
            self._send_service = self._make_send_service()
        return self._send_service

    async def _broadcast_mixed_no_reply_to_ops(
        self,
        *,
        text: str,
        group_id: str | None,
        dm_user_id: str | None,
        path: str,
        inbound_mid: str,
        session: Any = None,
    ) -> None:
        residual = _no_reply_sentinel_residual(text)
        if not residual:
            return
        target = infoflow_op_channel_from_env()
        if not target:
            return

        source = f"group:{group_id}" if group_id is not None else (dm_user_id or "")
        if target == source:
            gw_log().warning(
                "[iflow:send] mid=%s path=%s skip mixed NO_REPLY ops forward: "
                "target equals source=%s",
                inbound_mid,
                path,
                target,
            )
            return

        notice = (
            "[Infoflow NO_REPLY suppressed]\n"
            f"mid: {inbound_mid or '-'}\n"
            f"path: {path or '-'}\n"
            f"target: {source or '-'}\n\n"
            f"{text}"
        )
        try:
            send_service = self._active_send_service()
            if target.startswith("group:"):
                await send_service.send_group(
                    target.split(":", 1)[1],
                    message=notice,
                    session=session,
                )
            else:
                await send_service.send_private(
                    target,
                    message=notice,
                    session=session,
                )
            gw_log().info(
                "[iflow:send] mid=%s path=%s mixed NO_REPLY forwarded_to_ops=%s",
                inbound_mid,
                path,
                target,
            )
        except Exception as exc:
            gw_log().warning(
                "[iflow:send] mid=%s path=%s failed to forward mixed NO_REPLY "
                "to ops target=%s error=%s",
                inbound_mid,
                path,
                target,
                exc,
                exc_info=True,
            )

    # ======================================================================
    # INBOUND pipeline
    # ======================================================================

    async def process_inbound(self, msg: IncomingMessage) -> ProcessResult:
        """Run the full inbound processing pipeline.

        Steps (mirrors openclaw bot.ts):
        1. Discover/update robot_id
        2. Persist normalized message facts before policy/echo decisions
        3. Dedup check (plugin-sent echoes)
        4. Own-message echo filter (external channel echoes → RECORD)
        5. Policy evaluation
        6. Return dispatch decision
        """
        _chat = self._chat_label(msg)
        _text_preview = (msg.text or "")[:120]

        # --- Slash command fast path: /new, /stop ---
        # Detection is early, but dispatch is delayed until after message facts
        # have been persisted and dedup/own-echo checks have run.
        raw_text = (msg.text or "").strip()
        slash_command_text = raw_text if raw_text in self._SLASH_COMMANDS else ""
        slash_is_admin_cmd = bool(
            slash_command_text and self._check_slash_command_auth(msg)
        )

        # --- Enrich sender info (group messages only) ---
        if msg.is_group and msg.sender_imid:
            await self._enrich_sender(msg)

        # Step 1: Discover robot_id from @-mention body items, then normalize
        # robot AT targets. Incoming body robot_id is an Infoflow robot_id /
        # imid, never an app_agent_id, so agent IDs are only filled from
        # participants mapping.
        if msg.discovered_robot_id and msg.discovered_robot_id != self._robot_id:
            self._robot_id = msg.discovered_robot_id
            self._serverapi.robot_id = self._robot_id
            gw_log().info(
                "[infoflow] discovered robotId=%s for account %s",
                self._robot_id, self._settings.get("app_agent_id"),
            )
            _persist_robot_id(self._robot_id)
        self._normalize_mention_targets_from_participants(msg)
        self._normalize_reply_targets_from_message_store(msg)

        # --- [iflow:event] — enriched message event fields ---
        try:
            gw_log().info(
                "[iflow:event] mid=%s sender_id=%s sender_name=%s sender_imid=%s "
                "sender_agent_id=%s is_bot=%s mentioned=%s "
                "mention_users=%s mention_robots=%s mention_agents=%s reply_to_bot=%s body=%s",
                msg.message_id, msg.sender_id, msg.sender_name, msg.sender_imid,
                getattr(msg, "sender_agent_id", ""), msg.sender_is_bot,
                msg.bot_was_mentioned,
                msg.mention_user_ids,
                getattr(msg, "mention_robot_ids", []),
                msg.mention_agent_ids,
                msg.is_reply_to_bot, (msg.text or "")[:200],
            )
            for _i, _b in enumerate(msg.body_items or []):
                if hasattr(_b, "type"):
                    gw_log().info(
                        "[iflow:event] body_item[%d] type=%s name=%s user_id=%s robot_id=%s",
                        _i, _b.type, _b.name, _b.user_id, _b.robot_id,
                    )
        except Exception:
            pass

        # Audit log
        gw_log().info(
            "[infoflow] inbound decoded: from=%s chat=%s group=%s mentioned=%s mid=%s text=%r",
            msg.sender_id, _chat, msg.is_group, msg.bot_was_mentioned,
            msg.message_id or "-", _text_preview,
        )

        # Step 2: Persist normalized DB facts before dedup / own-echo / policy.
        dedupe_key = msg.dedupe_key
        already_seen = bool(dedupe_key and self._sent_store.is_duplicate(dedupe_key))
        sent_echo = bool(
            already_seen
            and dedupe_key
            and self._sent_store.find_any(dedupe_key) is not None
        )
        own_echo = self._is_own_echo(msg)
        self._register_context(
            msg,
            is_outgoing_hint=sent_echo or own_echo,
            local_sent_hint=sent_echo,
            register_recall=not (sent_echo or own_echo),
        )

        # Step 3: Dedup check (plugin-sent echoes are already mark_seen).
        # The DB upsert above is idempotent and enriches provisional sent rows
        # with the canonical echo payload without dispatching it to the model.
        if already_seen:
            reason = "own-echo:plugin-sent" if sent_echo else "duplicate"
            gw_log().info(
                "[iflow:decision] mid=%s action=DROP reason=%s text=%r",
                msg.message_id or "-", reason, _text_preview,
            )
            return ProcessResult(
                decision=PolicyDecision(
                    should_dispatch=False,
                    action=Action.RECORD,
                    reason=reason,
                )
            )
        if dedupe_key:
            self._sent_store.mark_seen(dedupe_key)

        # Step 4: Own-message echo filter (ALL_MESSAGE_FORWARD)
        # If dedup didn't catch it but sender_imid==robot_id, this echo came
        # from an external channel (infoflow-cli, another tool, etc.) →
        # persist but don't dispatch.
        if own_echo:
            gw_log().info(
                "[iflow:decision] mid=%s action=RECORD reason=own-echo:external text=%r",
                msg.message_id or "-", _text_preview,
            )
            return ProcessResult(
                decision=PolicyDecision(
                    should_dispatch=False,
                    action=Action.RECORD,
                    reason="own-echo:external",
                )
            )

        if slash_is_admin_cmd:
            gw_log().info(
                "[iflow:decision] mid=%s action=DISPATCH trigger=slash_command "
                "reason=%s sender=%s",
                msg.message_id or "-", slash_command_text, msg.sender_id,
            )
            return ProcessResult(
                should_dispatch=True,
                decision=PolicyDecision(
                    should_dispatch=True,
                    action=Action.DISPATCH,
                    reason=f"slash_command:{slash_command_text}",
                    trigger_reason="slash_command",
                    command_text=slash_command_text,
                ),
            )

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
        if not self._admin_users:
            return False
        if msg.sender_is_bot:
            return False
        sender = (msg.sender_id or "").lower()
        return sender in self._admin_users and msg.bot_was_mentioned

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

    def _is_own_echo(self, msg: IncomingMessage) -> bool:
        """True when this inbound callback is a message from the current bot."""
        me = self_key(self._settings)
        sender = sender_key(msg)
        if me and sender and sender == me:
            return True
        return bool(
            self._robot_id
            and msg.sender_imid
            and msg.sender_imid == self._robot_id
        )

    @staticmethod
    def _msg_time_ms(msg: IncomingMessage) -> int:
        ts = float(getattr(msg, "timestamp", 0.0) or 0.0)
        if ts <= 0:
            return 0
        if ts > 10_000_000_000:
            return int(ts)
        return int(ts * 1000)

    def _upsert_participants_from_message(self, msg: IncomingMessage) -> None:
        """Persist participant facts that are authoritative enough to keep."""
        self._upsert_self_participant()

        if msg.sender_is_bot:
            aid = str(getattr(msg, "sender_agent_id", "") or "").strip()
            if bot_key(aid):
                self._message_store.upsert_participant(
                    participant_type="bot",
                    agent_id=aid,
                    imid=msg.sender_imid or "",
                    name=msg.sender_name or "",
                )
        elif user_key(msg.sender_id):
            # Human sender names from headers / DM FromUserName are not trusted.
            # Human real names are only filled from AT body items below.
            self._message_store.upsert_participant(
                participant_type="user",
                user_id=msg.sender_id,
            )

        for item in msg.body_items or []:
            if (getattr(item, "type", "") or "").upper() != "AT":
                continue
            uid = getattr(item, "user_id", "") or ""
            if user_key(uid):
                self._message_store.upsert_participant(
                    participant_type="user",
                    user_id=uid,
                    name=getattr(item, "name", "") or "",
                )

    def _upsert_self_participant(self) -> None:
        """Persist the current bot participant when its stable agent id is known."""
        me_agent_id = str(self._settings.get("app_agent_id") or "").strip()
        if not me_agent_id:
            return
        try:
            self._message_store.upsert_participant(
                participant_type="bot",
                agent_id=me_agent_id,
                imid=self._robot_id or "",
                name=str(self._settings.get("robot_name") or ""),
            )
        except Exception:
            logger.debug("[bot] self participant upsert failed", exc_info=True)

    def _self_robot_ids(self) -> set[str]:
        """Return known Infoflow robot_id / imid values for the current bot."""
        ids = {str(self._robot_id or "").strip()} - {""}
        me_agent_id = str(self._settings.get("app_agent_id") or "").strip()
        if me_agent_id:
            participant = self._message_store.find_bot_by_agent_id(me_agent_id)
            if participant and participant.imid:
                ids.add(str(participant.imid).strip())
        return ids

    def _normalize_mention_targets_from_participants(self, msg: IncomingMessage) -> None:
        """Map robot AT robot_ids to agent_ids only through participants.

        The serverapi boundary gives robot AT targets as normalized robot_id
        values. Those are IM robot IDs / imids, not app agent IDs; unknown
        robot IDs remain in ``mention_robot_ids`` and are not promoted to
        ``mention_agent_ids``.
        """
        if not msg.is_group:
            return

        self_robot_ids = self._self_robot_ids()
        robot_ids: list[str] = []
        seen_robot_ids: set[str] = set()
        source_robot_ids = [
            str(v or "").strip()
            for v in (getattr(msg, "mention_robot_ids", None) or [])
            if str(v or "").strip()
        ]
        if not source_robot_ids:
            for item in msg.body_items or []:
                if (getattr(item, "type", "") or "").upper() != "AT":
                    continue
                rid = str(getattr(item, "robot_id", "") or "").strip()
                if rid:
                    source_robot_ids.append(rid)

        mapped_agent_ids: list[int] = []
        seen_agent_ids: set[int] = set()
        me_agent_id = str(self._settings.get("app_agent_id") or "").strip()

        def _add_agent_id(value: object) -> None:
            raw = str(value or "").strip()
            if not raw or not raw.isdigit() or raw == me_agent_id:
                return
            aid = int(raw)
            if aid not in seen_agent_ids:
                mapped_agent_ids.append(aid)
                seen_agent_ids.add(aid)

        for existing in getattr(msg, "mention_agent_ids", None) or []:
            _add_agent_id(existing)

        for rid in source_robot_ids:
            if rid in self_robot_ids:
                continue
            if rid not in seen_robot_ids:
                robot_ids.append(rid)
                seen_robot_ids.add(rid)
            participant = self._message_store.find_participant_by_imid(rid)
            if participant and participant.participant_type == "bot":
                _add_agent_id(participant.agent_id)

        msg.mention_robot_ids = robot_ids
        msg.mention_agent_ids = mapped_agent_ids

    def _normalize_reply_targets_from_message_store(self, msg: IncomingMessage) -> None:
        """Mark quoted targets as ours when the persistent store proves it.

        Parser-level detection relies on in-process sent IDs or platform
        reply metadata. System notices and messages quoted after a gateway
        restart can miss both signals, while MessageStore still has the
        durable outbound row for the same chat.
        """
        if not msg.reply_targets:
            return

        reply_targets = [coerce_reply_target(t) for t in msg.reply_targets]
        changed = False
        first_bot_target = None
        for target in reply_targets:
            rec = self._message_record_in_chat(msg, target.message_id)
            if rec is not None and not target.sender_key:
                target.sender_key = str(getattr(rec, "sender", "") or "")
                changed = True
            if (
                not target.is_bot_message
                and self._is_own_outgoing_record(rec)
            ):
                target.is_bot_message = True
                changed = True
            if target.is_bot_message:
                if not target.sender_key:
                    target.sender_key = self_key(self._settings)
                    changed = True
                first_bot_target = first_bot_target or target

        if not changed and first_bot_target is None:
            return

        if changed:
            msg.reply_targets = reply_targets
        if first_bot_target is None:
            return
        msg.is_reply_to_bot = True
        if msg.reply_info is None:
            msg.reply_info = ReplyInfo(
                message_id=first_bot_target.message_id,
                preview=first_bot_target.preview,
                sender_imid=first_bot_target.sender_imid,
            )

    def _message_record_in_chat(
        self,
        msg: IncomingMessage,
        message_id: str,
    ) -> Any | None:
        mid = str(message_id or "").strip()
        if not mid:
            return None

        if msg.is_group and msg.group_id is not None:
            rec = self._message_store.find_group(mid)
            if rec is not None and rec.group_id == str(msg.group_id):
                return rec
            return None

        if msg.is_dm and msg.dm_user_id is not None:
            rec = self._message_store.find_dm(mid)
            peer = private_peer_key(msg.dm_user_id)
            if rec is not None and rec.peer == peer:
                return rec
            return None

        return None

    def _is_own_outgoing_record(self, rec: Any | None) -> bool:
        if rec is None:
            return False
        me = self_key(self._settings)
        return bool(
            getattr(rec, "is_outgoing", False)
            and (
                not me
                or getattr(rec, "sender", "") == me
                or getattr(rec, "self_id", "") == me
            )
        )

    def _sender_key_for_store(self, msg: IncomingMessage) -> str:
        sender = sender_key(msg)
        if sender:
            return sender
        if msg.sender_is_bot and msg.sender_imid:
            participant = self._message_store.find_participant_by_imid(msg.sender_imid)
            if participant and participant.participant_type == "bot":
                return participant.key
        return ""

    def _matched_regex_pattern(self, msg: IncomingMessage) -> str:
        if not msg.group_id:
            return ""
        try:
            eff = _resolve_for_group(self._policy, msg.group_id)
            hit = _watch_regex_match(msg.text or "", eff.get("watch_regex") or ())
            return hit[0] if hit else ""
        except Exception:
            return ""

    def _group_message_flags(self, msg: IncomingMessage) -> dict[str, bool | str]:
        me = self_key(self._settings)
        self_robot_ids = self._self_robot_ids()
        mentions_everyone = False
        mentions_other_people = False
        for item in msg.body_items or []:
            if (getattr(item, "type", "") or "").upper() != "AT":
                continue
            if getattr(item, "at_all", False):
                mentions_everyone = True
                continue
            uid = str(getattr(item, "user_id", "") or "").strip()
            rid = str(getattr(item, "robot_id", "") or "").strip()
            if uid:
                target = user_key(uid)
            elif rid and rid in self_robot_ids:
                target = me
            elif rid:
                participant = self._message_store.find_participant_by_imid(rid)
                if participant and participant.participant_type == "bot":
                    target = participant.key
                else:
                    target = f"robot_id:{rid}"
            else:
                target = ""
            if target and target != me:
                mentions_other_people = True

        reply_targets = [coerce_reply_target(t) for t in msg.reply_targets]
        quotes_your_message = any(t.is_bot_message for t in reply_targets)
        quotes_other = any(not t.is_bot_message for t in reply_targets)
        return {
            "mentions_everyone": mentions_everyone,
            "mentions_other_people": mentions_other_people,
            "quotes_your_message": quotes_your_message,
            "quotes_other_peoples_message": quotes_other,
        }

    def _register_context(
        self,
        msg: IncomingMessage,
        *,
        is_outgoing_hint: bool = False,
        local_sent_hint: bool = False,
        register_recall: bool = True,
    ) -> None:
        """Register inbound context for recall + persist to message store."""
        # message_id is the message-store primary key and the echo reconciliation
        # identity. Do not synthesize fallback IDs; messages without one cannot
        # be safely upserted or matched against later echo callbacks.
        if not msg.message_id:
            return
        target = (
            f"group:{msg.group_id}" if msg.is_group else (msg.dm_user_id or "")
        )

        reply_to_bot_id: str | None = None
        reply_targets = [coerce_reply_target(tgt) for tgt in msg.reply_targets]
        for tgt in reply_targets:
            if tgt.is_bot_message:
                reply_to_bot_id = tgt.message_id or None
                break

        self._upsert_participants_from_message(msg)
        content = self._render_message_content(msg)

        if register_recall:
            _register_inbound_context(
                _InboundContext(
                    account_id=self._settings.get("app_key") or "default",
                    target=target,
                    inbound_message_id=msg.message_id,
                    reply_to_bot_message_id=reply_to_bot_id,
                    reply_targets=[reply_target_to_dict(t) for t in reply_targets],
                    inbound_body=content or msg.text or "",
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
        msg_time = self._msg_time_ms(msg)
        self_id = self_key(self._settings)
        if msg.dm_user_id is not None:
            peer = private_peer_key(msg.dm_user_id)
            sender = self_id if is_outgoing_hint else user_key(msg.sender_id)
            is_outgoing = bool(is_outgoing_hint or (sender and self_id and sender == self_id))
            self._message_store.persist_dm(
                message_id=msg.message_id,
                peer=peer,
                self_id=self_id,
                sender=sender,
                is_outgoing=is_outgoing,
                local_sent=bool(local_sent_hint),
                quotes_your_message=bool(reply_to_bot_id),
                msg_id2=msg.msgid2 or "",
                content=content,
                msg_time=msg_time,
                raw_json=raw_json,
            )
        elif msg.group_id is not None:
            sender = self_id if is_outgoing_hint else self._sender_key_for_store(msg)
            is_outgoing = bool(is_outgoing_hint or (sender and self_id and sender == self_id))
            flags = self._group_message_flags(msg)
            self._message_store.persist_group(
                message_id=msg.message_id,
                group_id=msg.group_id,
                sender=sender,
                self_id=self_id,
                is_outgoing=is_outgoing,
                local_sent=bool(local_sent_hint),
                mentions_you=msg.bot_was_mentioned,
                matched_regex_pattern=self._matched_regex_pattern(msg),
                mentions_everyone=bool(flags["mentions_everyone"]),
                quotes_your_message=bool(flags["quotes_your_message"]),
                mentions_other_people=bool(flags["mentions_other_people"]),
                quotes_other_peoples_message=bool(flags["quotes_other_peoples_message"]),
                msg_id2=msg.msgid2 or "",
                content=content,
                msg_time=msg_time,
                raw_json=raw_json,
            )

    # -- emoji reaction lifecycle (processing indicator) --------------------

    def _build_reaction_handle(
        self,
        msg: IncomingMessage,
        decision: PolicyDecision,
    ) -> dict[str, str] | None:
        """Return reaction API params if this dispatch should show a processing emoji.

        Returns a kwargs dict consumable by ``serverapi.add_message_reaction``
        and ``delete_message_reaction``. Both group (chat_type="group") and DM
        (chat_type="dm") paths are supported.
        """
        if not msg.message_id:
            return None
        if getattr(decision, "command_text", ""):
            return None

        # DM path: always eligible. Policy guarantees DM always dispatches, and
        # the user wants every inbound DM to show a processing indicator until
        # the bot replies (or finalizes silently).
        if msg.is_dm:
            from_uid = msg.dm_user_id or ""
            if not from_uid or from_uid.startswith("IMID:"):
                # Cannot resolve a uuapName for the emoji API.
                return None
            return {
                "chat_type": "dm",
                "group_id": None,
                "base_msg_id": msg.message_id,
                "msgid2": msg.msgid2 or "",
                "from_uid": from_uid,
                "emoji_code": _EMOJI_PROCESSING[0],
                "emoji_desc": _EMOJI_PROCESSING[1],
            }

        # Group path: react when the message is directly addressed to the bot,
        # hits an explicit watch rule, or is part of an engaged follow-up window.
        if not msg.is_group or not msg.group_id or not msg.msgid2:
            return None

        trig = decision.trigger_reason or ""
        eligible = False
        if trig == "bot-mentioned" or trig.startswith("watchMentions:") or trig.startswith("watchRegex#"):
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
            "chat_type": "group",
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

    async def _try_delete_reaction(self, handle: dict[str, str]) -> bool:
        try:
            res = await self._serverapi.delete_message_reaction(**handle)
            if not res.success:
                gw_log().warning("[iflow:reaction] del failed: %s", res.error)
            return res.success
        except Exception:
            gw_log().exception("[iflow:reaction] del raised")
            return False

    @staticmethod
    def _reaction_scope_for_target(
        *,
        group_id: str | None,
        dm_user_id: str | None,
    ) -> str:
        if group_id:
            return f"group:{group_id}"
        if dm_user_id:
            return f"dm:{dm_user_id}"
        return ""

    async def _start_reaction_run(
        self,
        handle: dict[str, str],
    ) -> ReactionRunToken | None:
        """Start a session thinking indicator for this inbound run."""
        return await self._reactions.start(handle)

    def reaction_token_for_context(
        self,
        *,
        group_id: str | None,
        dm_user_id: str | None = None,
        reaction_message_id: str | None = None,
    ) -> ReactionRunToken | None:
        """Return the current processing-reaction token for this inbound event."""
        target_scope = self._reaction_scope_for_target(
            group_id=group_id,
            dm_user_id=dm_user_id,
        )
        return self._reactions.token_by_anchor(
            reaction_message_id,
            expected_scope=target_scope,
        )

    async def _cleanup_reaction_after_timeout(self, token: ReactionRunToken) -> None:
        try:
            await asyncio.sleep(_REACTION_FALLBACK_CLEANUP_SECONDS)
            await self._reactions.finish(token, reason="timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            gw_log().exception("[iflow:reaction] fallback cleanup failed")

    def _schedule_reaction_fallback_cleanup(self, token: ReactionRunToken) -> None:
        existing = self._reaction_cleanup_tasks_by_run.get(token.run_id)
        if existing is not None and not existing.done():
            return
        if existing is not None:
            self._reaction_cleanup_tasks.discard(existing)
            self._reaction_cleanup_tasks_by_run.pop(token.run_id, None)

        task = asyncio.create_task(self._cleanup_reaction_after_timeout(token))
        self._reaction_cleanup_tasks.add(task)
        self._reaction_cleanup_tasks_by_run[token.run_id] = task
        task.add_done_callback(
            lambda done, run_id=token.run_id: self._discard_reaction_cleanup_task(
                run_id,
                done,
            )
        )

    def _discard_reaction_cleanup_task(
        self,
        run_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        self._reaction_cleanup_tasks.discard(task)
        if self._reaction_cleanup_tasks_by_run.get(run_id) is task:
            self._reaction_cleanup_tasks_by_run.pop(run_id, None)

    def _cancel_reaction_fallback_cleanup(
        self,
        token: ReactionRunToken | None,
    ) -> None:
        if token is None:
            return
        task = self._reaction_cleanup_tasks_by_run.pop(token.run_id, None)
        if task is None:
            return
        self._reaction_cleanup_tasks.discard(task)
        if not task.done():
            task.cancel()

    async def _finish_reaction_token(
        self,
        token: ReactionRunToken | None,
        *,
        reason: str,
    ) -> bool:
        finished = await self._reactions.finish(token, reason=reason)
        if finished:
            self._cancel_reaction_fallback_cleanup(token)
        return finished

    async def _finish_reaction_for_send_target(
        self,
        *,
        group_id: str | None,
        dm_user_id: str | None = None,
        reaction_message_id: str | None = None,
        reason: str,
    ) -> None:
        token = _reaction_promise_cv.get(None)
        target_scope = self._reaction_scope_for_target(
            group_id=group_id,
            dm_user_id=dm_user_id,
        )
        if token is not None:
            if not target_scope or token.scope_key == target_scope:
                await self._finish_reaction_token(token, reason=reason)
                # Hermes may queue a newer event while the old background task is
                # still unwinding. The queued task can inherit the old contextvar,
                # so fall through to the message anchor when that token was stale.
                if not token.stale or not reaction_message_id:
                    return
            elif not reaction_message_id:
                return
        if reaction_message_id:
            anchor_token = self._reactions.token_by_anchor(
                reaction_message_id,
                expected_scope=target_scope,
            )
            await self._finish_reaction_token(anchor_token, reason=reason)
            return

    async def finish_processing_reaction(
        self,
        *,
        group_id: str | None,
        dm_user_id: str | None = None,
        reaction_message_id: str | None = None,
        reason: str,
    ) -> None:
        """Finish the processing indicator when the Hermes run actually ends."""
        await self._finish_reaction_for_send_target(
            group_id=group_id,
            dm_user_id=dm_user_id,
            reaction_message_id=reaction_message_id,
            reason=reason,
        )

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

        inbound_mid_token = _inbound_mid.set(msg.message_id or "")
        send_path_token = _send_path_cv.set(decision.trigger_reason or "")
        hint = msg.message_id or None

        reaction_cv_token = None
        reaction_run: ReactionRunToken | None = None
        reaction = self._build_reaction_handle(msg, decision)
        if reaction:
            reaction_run = await self._start_reaction_run(reaction)
            if reaction_run is not None:
                reaction_cv_token = _reaction_promise_cv.set(reaction_run)

        try:
            with recall_inbound_message_id_hint_scope(hint):
                event = await adapter.build_message_event(msg, decision)
                await adapter.handle_message(event)
        except asyncio.CancelledError:
            await self._finish_reaction_token(
                reaction_run,
                reason="dispatch_cancelled",
            )
            raise
        except Exception:
            gw_log().exception("[infoflow] inbound dispatch failed")
            await self._finish_reaction_token(
                reaction_run,
                reason="dispatch_error",
            )
        finally:
            if reaction_run is not None and not reaction_run.finished:
                # adapter.handle_message() may only hand the event to Hermes.
                # The real run can finish later via send()/on_processing_complete().
                self._schedule_reaction_fallback_cleanup(reaction_run)
            if reaction_cv_token is not None:
                _reaction_promise_cv.reset(reaction_cv_token)
            _send_path_cv.reset(send_path_token)
            _inbound_mid.reset(inbound_mid_token)

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
        reply_to: list[dict[str, str]] | None = None,
        reply_to_sender_id: str = "",
        options: SendOptions | None = None,
        session: Any = None,
        reaction_message_id: str | None = None,
    ) -> SentResult:
        """Send a text/markdown message.  Handles NO_REPLY, chunking,
        dedup, message store, and follow-up tracking.
        """
        from .adapter import _inbound_mid as _mid_var
        _path = _send_path_cv.get("")

        # NO_REPLY sentinel — see ``no_reply_sentinel_hits`` for acceptance rules.
        if no_reply_sentinel_hits(text):
            await self._broadcast_mixed_no_reply_to_ops(
                text=text or "",
                group_id=group_id,
                dm_user_id=dm_user_id,
                path=_path,
                inbound_mid=_mid_var.get(""),
                session=session,
            )
            gw_log().info(
                "[iflow:send] mid=%s path=%s NO_REPLY sentinel suppressed",
                _mid_var.get(""), _path,
            )
            await self._finish_reaction_for_send_target(
                group_id=group_id,
                dm_user_id=dm_user_id,
                reaction_message_id=reaction_message_id,
                reason="no_reply",
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
            await self._finish_reaction_for_send_target(
                group_id=group_id,
                dm_user_id=dm_user_id,
                reaction_message_id=reaction_message_id,
                reason="refusal_suppressed",
            )
            return SentResult(success=True)

        # Truncate into chunks (Hermes convention, 2KB per message ceiling)
        from gateway.platforms.base import BasePlatformAdapter  # type: ignore[import-not-found]
        chunks = BasePlatformAdapter.truncate_message(text, 2000)
        if not chunks:
            chunks = [""]

        store_key = self._normalize_store_key(group_id, dm_user_id)
        last_message_id: str = ""
        all_sent_ids: list[tuple[str, str]] = []
        seen_sent_ids: set[str] = set()
        first_error: str = ""
        failed = 0
        succeeded = 0
        send_service = self._active_send_service()

        for idx, chunk in enumerate(chunks):
            # Options (mention metadata) only apply to the first chunk
            opts = options if idx == 0 else None

            if group_id is not None:
                result = await send_service.send_group(
                    group_id,
                    message=chunk,
                    reply_to=reply_to if idx == 0 else None,
                    at_all=opts.at_all if opts is not None else False,
                    mention_user_ids=self._send_option_values(
                        opts.mention_user_ids if opts is not None else ""
                    ),
                    mention_agent_ids=self._send_option_values(
                        opts.mention_agent_ids if opts is not None else ""
                    ),
                    session=session,
                )
            elif dm_user_id is not None:
                result = await send_service.send_private(
                    dm_user_id,
                    message=chunk,
                    reply_to=reply_to if idx == 0 else None,
                    session=session,
                )
            else:
                result = SentResult(success=False, error="no target specified")

            sent_ids = self._sent_result_ids(result)
            if sent_ids:
                for mid, seq in sent_ids:
                    if mid in seen_sent_ids:
                        continue
                    seen_sent_ids.add(mid)
                    all_sent_ids.append((mid, seq))
                    self._sent_store.record(
                        chat_id=store_key,
                        messageid=mid,
                        msgseqid=seq,
                        digest=chunk[:80],
                    )
                    self._record_sent(
                        message_id=mid, text=chunk,
                        group_id=group_id, dm_user_id=dm_user_id,
                    )
                last_message_id = str(result.message_id or sent_ids[-1][0] or last_message_id)

            if result.success:
                succeeded += 1
            else:
                failed += 1
                if not first_error:
                    first_error = result.error

        # Follow-up window: record bot reply timestamp for group messages
        if succeeded and group_id:
            _reply_key = str(reply_to_sender_id or "")
            self._policy.record_bot_reply(
                group_id,
                reply_to_sender=_reply_key,
            )
            gw_log().info("[iflow:send] mid=%s step=record_bot_reply group=%s reply_to=%s", _mid_var.get(""), group_id, _reply_key)

        if first_error:
            await self._finish_reaction_for_send_target(
                group_id=group_id,
                dm_user_id=dm_user_id,
                reaction_message_id=reaction_message_id,
                reason="send_error",
            )
            return SentResult(
                success=False,
                message_id=last_message_id,
                continuation_message_ids=tuple(
                    mid for mid, _seq in all_sent_ids if mid and mid != last_message_id
                ),
                continuation_msgseqids=tuple(
                    seq for mid, seq in all_sent_ids if mid and mid != last_message_id
                ),
                error=(
                    f"{first_error} (sent_messages={len(all_sent_ids)}, "
                    f"succeeded_chunks={succeeded}, failed={failed} of {len(chunks)} chunks)"
                    if all_sent_ids or succeeded else first_error
                ),
            )
        await self._finish_reaction_for_send_target(
            group_id=group_id,
            dm_user_id=dm_user_id,
            reaction_message_id=reaction_message_id,
            reason="send_complete",
        )
        return SentResult(
            success=True,
            message_id=last_message_id,
            continuation_message_ids=tuple(
                mid for mid, _seq in all_sent_ids if mid and mid != last_message_id
            ),
            continuation_msgseqids=tuple(
                seq for mid, seq in all_sent_ids if mid and mid != last_message_id
            ),
        )

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
        reply_to: list[dict[str, str]] | None = None,
        reply_to_sender_id: str = "",
        session: Any = None,
        reaction_message_id: str | None = None,
    ) -> SentResult:
        """Send an image (optionally with caption)."""
        from .adapter import _inbound_mid as _mid_var
        send_service = self._active_send_service()
        if group_id is not None:
            result = await send_service.send_group(
                group_id,
                message=caption or "",
                image_bytes=image_bytes,
                reply_to=reply_to,
                session=session,
            )
        elif dm_user_id is not None:
            result = await send_service.send_private(
                dm_user_id,
                message=caption or "",
                image_bytes=image_bytes,
                reply_to=reply_to,
                session=session,
            )
        else:
            await self._finish_reaction_for_send_target(
                group_id=group_id,
                dm_user_id=dm_user_id,
                reaction_message_id=reaction_message_id,
                reason="image_no_target",
            )
            return SentResult(success=False, error="no target specified")

        sent_ids = self._sent_result_ids(result)
        if sent_ids:
            store_key = self._normalize_store_key(group_id, dm_user_id)
            receipt_by_id = {
                receipt.message_id: receipt
                for receipt in result.sent_messages or ()
                if receipt.message_id
            }
            ambiguous_group_image_ids = (
                not receipt_by_id
                and bool(group_id)
                and bool(caption)
                and len(sent_ids) > 1
            )
            for mid, seq in sent_ids:
                receipt = receipt_by_id.get(mid)
                stored_text = (
                    receipt.preview
                    if receipt and receipt.preview
                    else ("[image]" if ambiguous_group_image_ids else (caption or "[image]"))
                )
                self._sent_store.record(
                    chat_id=store_key, messageid=mid,
                    msgseqid=seq, digest=stored_text[:80],
                )
                self._record_sent(
                    message_id=mid, text=stored_text,
                    group_id=group_id, dm_user_id=dm_user_id,
                )
            if group_id:
                _reply_key = str(reply_to_sender_id or "")
                self._policy.record_bot_reply(
                    group_id,
                    reply_to_sender=_reply_key,
                )
                gw_log().info("[iflow:send] mid=%s step=record_bot_reply group=%s (image) reply_to=%s", _mid_var.get(""), group_id, _reply_key)
        await self._finish_reaction_for_send_target(
            group_id=group_id,
            dm_user_id=dm_user_id,
            reaction_message_id=reaction_message_id,
            reason="image_complete" if result.success else "image_error",
        )
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

    @staticmethod
    def _sent_result_ids(result: SentResult) -> list[tuple[str, str]]:
        receipts = [
            (str(receipt.message_id or ""), str(receipt.msgseqid or ""))
            for receipt in result.sent_messages or ()
            if str(receipt.message_id or "")
        ]
        if receipts:
            return receipts

        ids: list[tuple[str, str]] = []
        continuation_ids = list(getattr(result, "continuation_message_ids", ()) or ())
        continuation_seqs = list(getattr(result, "continuation_msgseqids", ()) or ())
        for idx, mid in enumerate(continuation_ids):
            mid_s = str(mid or "")
            if not mid_s:
                continue
            seq = continuation_seqs[idx] if idx < len(continuation_seqs) else ""
            ids.append((mid_s, str(seq or "")))
        primary = str(result.message_id or "")
        if primary and all(mid != primary for mid, _seq in ids):
            ids.append((primary, str(result.msgseqid or "")))
        return ids

    @staticmethod
    def _send_option_values(value: str | None) -> list[str]:
        return [item.strip() for item in str(value or "").split(",") if item.strip()]

    @staticmethod
    def _image_caption_message_ids(result: SentResult) -> set[str]:
        """Return image caption IDs only when the image path marks them explicitly."""
        ids: set[str] = set()
        if not isinstance(result.raw_response, dict):
            return ids

        # DM images send caption as a separate private message and keep the
        # exact response, so this ID is safe to use for provisional content.
        caption_response = result.raw_response.get("caption_response")
        if isinstance(caption_response, dict):
            caption_mid = str(
                caption_response.get("messageid")
                or caption_response.get("msgkey")
                or ""
            )
            if caption_mid:
                ids.add(caption_mid)

        # Group image captions currently share the generic multi-segment group
        # sender. Until that path returns per-segment roles from real image
        # payloads, only trust an explicit marker supplied by image-specific code.
        for raw_mid in result.raw_response.get("caption_messageids") or []:
            mid = str(raw_mid or "")
            if mid:
                ids.add(mid)
        return ids

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
            me = self_key(self._settings)
            agent_id = str(self._settings.get("app_agent_id") or "").strip()
            if agent_id:
                self._message_store.upsert_participant(
                    participant_type="bot",
                    agent_id=agent_id,
                    imid=self._robot_id or "",
                    name=str(self._settings.get("robot_name") or ""),
                )
            if group_id is not None:
                self._message_store.persist_group(
                    message_id=message_id,
                    group_id=group_id,
                    sender=me,
                    self_id=me,
                    is_outgoing=True,
                    local_sent=True,
                    content=text,
                    created_time=int(time.time() * 1000),
                )
            elif dm_user_id is not None:
                self._message_store.persist_dm(
                    message_id=message_id,
                    peer=private_peer_key(dm_user_id),
                    self_id=me,
                    sender=me,
                    is_outgoing=True,
                    local_sent=True,
                    content=text,
                    created_time=int(time.time() * 1000),
                )
        except Exception:
                logger.debug("[bot] _record_sent failed", exc_info=True)

    def _render_message_content(self, msg: IncomingMessage) -> str:
        return render_message_content(
            msg,
            robot_agent_id_lookup=self._agent_id_for_robot_id,
        )

    def _agent_id_for_robot_id(self, robot_id: str) -> str | None:
        rid = str(robot_id or "").strip()
        if not rid:
            return None
        participant = self._message_store.find_participant_by_imid(rid)
        if participant and participant.participant_type == "bot" and participant.agent_id:
            return participant.agent_id
        if rid in self._self_robot_ids():
            return str(self._settings.get("app_agent_id") or "").strip() or None
        return None

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
