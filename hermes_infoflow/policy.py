"""Group-message dispatch policy.

Mirrors openclaw-infoflow/src/bot.ts (the per-message branch decision tree)
without pulling in the LLM-side prompt assembly. The adapter consumes a
``PolicyDecision`` and either:

* drops the message (``Action.SKIP``),
* records it into ambient history but doesn't dispatch (``Action.RECORD``),
* dispatches to the agent (``Action.DISPATCH`` with optional
  ``trigger_reason`` + ``group_system_prompt``).

The five upstream ``replyMode`` values are now all faithfully implemented:

* ``ignore``              — drop everything (group only; DMs always dispatch).
* ``record``              — never dispatch, just record into the recent-history
                            map so a later @-mention has context.
* ``mention-only``        — dispatch only when the bot is @-mentioned or
                            quote-replied. Optionally falls back to the
                            "follow-up" path when the bot recently replied.
* ``mention-and-watch``   — ``mention-only`` plus ``watch_mentions`` and
                            ``watch_regex`` matchers.
* ``proactive``           — always dispatch (with a prompt hint telling the
                            agent to use ``NO_REPLY`` when it has nothing
                            useful to add).

DMs always dispatch — ``was_mentioned`` is True by convention in
:func:`hermes_infoflow.parser.build_private_inbound`.

Per-group overrides are resolved via ``per_group_overrides`` — a dict keyed
by ``group_id`` (string), each entry able to override any of:

    reply_mode / watch_mentions / watch_regex /
    follow_up / follow_up_window / system_prompt
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .parser import BodyItem
from .itypes import IncomingMessage


# All five OpenClaw modes are now first-class. Legacy aliases route to the
# closest match, with a warning to encourage a config update.
VALID_REPLY_MODES = (
    "ignore",
    "record",
    "mention-only",
    "mention-and-watch",
    "proactive",
)

DEFAULT_REPLY_MODE = "mention-and-watch"
DEFAULT_FOLLOW_UP = True
DEFAULT_FOLLOW_UP_WINDOW_SECONDS = 300


class Action(str, Enum):
    """What the adapter should do with an inbound message."""

    DISPATCH = "dispatch"      # send to the agent (normal path)
    RECORD = "record"          # add to ambient history but don't dispatch
    SKIP = "skip"              # drop entirely


@dataclass(frozen=True)
class NormalizedMode:
    value: str
    warning: str = ""


def normalize_reply_mode(raw: str | None) -> NormalizedMode:
    """Coerce a raw ``replyMode`` value into one of the supported modes.

    Returns a tuple-like with the canonical value and an optional warning
    explaining a fallback. The caller is responsible for logging the
    warning at construct time.
    """
    if raw is None:
        return NormalizedMode(DEFAULT_REPLY_MODE)
    val = str(raw).strip().lower()
    if not val:
        return NormalizedMode(DEFAULT_REPLY_MODE)
    if val in VALID_REPLY_MODES:
        return NormalizedMode(val)
    return NormalizedMode(
        DEFAULT_REPLY_MODE,
        warning=(
            f"unknown reply_mode={val!r}; falling back to {DEFAULT_REPLY_MODE}. "
            f"Valid values: {', '.join(VALID_REPLY_MODES)}."
        ),
    )


@dataclass(frozen=True)
class GroupConfigOverride:
    """Per-group overrides — mirrors OpenClaw ``InfoflowGroupConfig``.

    Any field left as ``None`` falls back to the account-level setting.
    """

    reply_mode: str | None = None
    watch_mentions: tuple[str, ...] | None = None
    watch_regex: tuple[str, ...] | None = None
    follow_up: bool | None = None
    follow_up_window: int | None = None
    system_prompt: str | None = None


# Mutable on purpose: ``last_reply_at`` is updated by the adapter after each
# successful outbound send (so the follow-up window can kick in). We can't
# use ``frozen=True`` here — Python's auto-generated ``__hash__`` on a frozen
# dataclass would try to hash the ``dict`` fields and crash. The class is
# still treated as configuration data; only ``record_bot_reply`` mutates it.
@dataclass(eq=False)
class GroupPolicy:
    """Configurable policy applied to inbound group messages."""

    reply_mode: str = DEFAULT_REPLY_MODE
    require_mention: bool = True
    watch_mentions: tuple[str, ...] | list[str] = ()
    watch_regex: tuple[str, ...] | list[str] = ()
    follow_up: bool = DEFAULT_FOLLOW_UP
    follow_up_window: int = DEFAULT_FOLLOW_UP_WINDOW_SECONDS
    per_group_overrides: dict[str, GroupConfigOverride] = field(default_factory=dict)
    # Map[group_id_str -> last bot-reply timestamp (seconds)]. The adapter
    # writes to this set after each successful outbound send. Kept here so
    # the policy can read it; the dict is shared by reference.
    last_reply_at: dict[str, float] = field(default_factory=dict)
    # Map[group_id_str -> {sender_id: timestamp}].  Records who @mentioned the
    # bot within each group's follow-up window.  Used by build_follow_up_prompt
    # to select engaged vs passive template.
    sender_mention_at: dict[str, dict[str, float]] = field(default_factory=dict)

    def record_bot_reply(self, group_id: str, *, now: float | None = None) -> None:
        """Mark that the bot has just replied to ``group_id``.

        Used to gate the follow-up window. Callers should hand the same
        dict over so multiple ``evaluate_inbound`` calls share state.
        """
        if not group_id:
            return
        _now = now if now is not None else time.time()
        self.last_reply_at[group_id] = _now
        # Update mention timestamps to the bot-reply time so that
        # sender_mentioned_in_window(mention_time >= window_start) stays True
        # for senders who @mentioned the bot earlier in this conversation.
        # (The @mention always happens BEFORE the bot replies, so T_mention < T_reply;
        #  without this update, `mention_time >= window_start` would always be False.)
        for sid in list(self.sender_mention_at.get(group_id, {})):
            self.sender_mention_at[group_id][sid] = _now

    def record_sender_mention(self, group_id: str, sender_id: str, *, now: float | None = None) -> None:
        """Record that ``sender_id`` @mentioned the bot in ``group_id``."""
        if not group_id or not sender_id:
            return
        _now = now if now is not None else time.time()
        self.sender_mention_at.setdefault(group_id, {})[sender_id] = _now

    def sender_mentioned_in_window(self, group_id: str, sender_id: str, *, now: float | None = None) -> bool:
        """Check whether ``sender_id`` @mentioned the bot within the current follow-up window."""
        if not group_id or not sender_id:
            return False
        window_start = self.last_reply_at.get(group_id)
        if window_start is None:
            return False
        mentions = self.sender_mention_at.get(group_id, {})
        mention_time = mentions.get(sender_id)
        if mention_time is None:
            return False
        return mention_time >= window_start


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of evaluating an inbound message against a ``GroupPolicy``."""

    should_dispatch: bool
    reason: str = ""
    action: Action = Action.DISPATCH
    trigger_reason: str = ""
    # Persistent group-level identity / role prompt → channel_prompt → system prompt.
    group_system_prompt: str = ""
    # Per-message judgement instructions (watch, proactive, etc.) → injected as
    # a PREFIX of the user message text so the LLM sees them in the latest turn
    # rather than buried deep in the system prompt.
    per_message_prompt: str = ""
    # When True, the adapter should asynchronously enrich group_system_prompt
    # with sender context + group member info before dispatching to the LLM.
    needs_sender_context: bool = False

    @property
    def is_record(self) -> bool:
        return self.action == Action.RECORD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    return name.strip().lower()


def _watch_mentioned(inbound: IncomingMessage, watch_list: tuple[str, ...] | list[str]) -> str | None:
    """Return the first matching watch entry, or ``None``.

    Matching priority (mirrors OpenClaw bot.ts::checkWatchMentioned):
        1. userid (human AT)
        2. robotid (robot AT, when watch list entry parses as a number)
        3. name (case-insensitive fallback)

    Empty entries in ``watch_list`` are filtered out **in lock-step** with
    the normalized form so ``normalized_ids[i]`` always corresponds to
    ``originals[i]`` (avoids the off-by-one when entries like
    ``["", "Alice"]`` are configured).
    """
    if not watch_list:
        return None
    originals: list[str] = []
    normalized_ids: list[str] = []
    for raw in watch_list:
        if not raw:
            continue
        norm = _normalize_name(raw)
        if not norm:
            continue
        originals.append(raw)
        normalized_ids.append(norm)
    if not normalized_ids:
        return None
    numeric_ids: dict[str, str] = {}
    for original in originals:
        s = original.strip()
        if s.isdigit():
            # Keep the first occurrence for stable matching.
            numeric_ids.setdefault(s, original)

    for item in inbound.body_items:
        if item.type != "AT":
            continue
        # Priority 1: userid
        if item.userid:
            uid = _normalize_name(item.userid)
            if uid in normalized_ids:
                return originals[normalized_ids.index(uid)]
        # Priority 2: robotid (numeric)
        if item.robotid:
            rid = item.robotid.strip()
            if rid in numeric_ids:
                return numeric_ids[rid]
        # Priority 3: display name
        if item.name:
            nm = _normalize_name(item.name)
            if nm in normalized_ids:
                return originals[normalized_ids.index(nm)]
    return None


def _watch_regex_match(mes: str, patterns: tuple[str, ...] | list[str]) -> tuple[str, int] | None:
    """Return ``(pattern, index)`` of the first matching pattern, or ``None``.

    Uses dotAll + ignorecase.  The index is 0-based so callers can build
    ``watchRegex#3`` style trigger reasons.
    """
    if not mes or not patterns:
        return None
    for idx, raw in enumerate(patterns):
        if not raw:
            continue
        try:
            if re.search(raw, mes, flags=re.DOTALL | re.IGNORECASE):
                return raw, idx
        except re.error:
            continue
    return None


def _within_follow_up_window(
    policy: GroupPolicy,
    group_id: str,
    window_seconds: int,
    *,
    now: float | None = None,
) -> bool:
    """True iff the bot replied to ``group_id`` within ``window_seconds``."""
    if not group_id or window_seconds <= 0:
        return False
    last = policy.last_reply_at.get(group_id)
    if last is None:
        return False
    ts = now if now is not None else time.time()
    return (ts - last) <= window_seconds


def _resolve_for_group(policy: GroupPolicy, group_id: str | None) -> dict[str, Any]:
    """Merge per-group overrides on top of the account-level policy."""
    override = policy.per_group_overrides.get(group_id or "")
    base = {
        "reply_mode": policy.reply_mode,
        "watch_mentions": tuple(policy.watch_mentions or ()),
        "watch_regex": tuple(policy.watch_regex or ()),
        "follow_up": policy.follow_up,
        "follow_up_window": policy.follow_up_window,
        "system_prompt": "",
    }
    if override is not None:
        if override.reply_mode is not None:
            base["reply_mode"] = override.reply_mode
        if override.watch_mentions is not None:
            base["watch_mentions"] = tuple(override.watch_mentions)
        if override.watch_regex is not None:
            base["watch_regex"] = tuple(override.watch_regex)
        if override.follow_up is not None:
            base["follow_up"] = override.follow_up
        if override.follow_up_window is not None:
            base["follow_up_window"] = override.follow_up_window
        if override.system_prompt:
            base["system_prompt"] = override.system_prompt
    return base


# ---------------------------------------------------------------------------
# Prompt fragments — kept terse, used by the adapter when forwarding to
# hermes-agent so the agent knows whether to NO_REPLY.
# ---------------------------------------------------------------------------


_WATCH_MENTION_PROMPT = """\
[Dispatch] 群里有人 @ 了 {who}(你是 {who} 的助理,在旁听)。

**快通道 — 简单事实即代答**:如果消息问的是无需调 tools 就能定论的常识(时间、日期、星期、基础算术、公开知识、英文翻译、单位换算等),即使消息明确 @ 的是 {who} 而不是你,你也以"{who} 的助理"身份**直接代答**,不要 NO_REPLY、不要 "我帮你看看 / 让我查一下"等开场白。{who} 不一定立刻在线,你代答能让群继续运转。

否则,走下面三步:

第一步 · 静默探索(**不输出任何消息**):
- 允许并鼓励你**多轮**调用任何 tools / skills 去尝试解决该消息背后的问题:查资料、查日历、看历史记录、调内部接口、链式调用都可以。
- 全过程**只在内部进行**——绝不要发"我帮你看看 / 让我查一下 / 稍等 / 先确认一下"等任何中间消息。
- 不要因为"先确认一下"就提前回复半句话;要么静默继续探索,要么走第二步定结论。

第二步 · 价值判定(拿到最终结论后才做):
- 你已得到**有信息量的结果**(具体事实 / 完整答案 / 可执行建议)→ 进入第三步。
- 结论是拒绝声明("我没法 / 我无法 / 作为AI")、客套、与原消息脱节的发散、或仍只是"我去查一下"这类过程话 → 输出单独一行 `NO_REPLY`,**不输出其它任何字**。

第三步 · 直接给答(仅价值通过时):
- 作为 {who} 的助理,把最终结论直接发出去;不加开场白、不复述问题、不汇报过程("经过查询..."、"根据我的搜索..." 一律不要)。

**Output:要么是已查得的最终答案正文,要么单独一行 NO_REPLY。**"""

_WATCH_REGEX_PROMPT = """\
[Dispatch] 消息命中关注的正则 ({pattern})。

第一步 · 静默探索:允许并鼓励**多轮**调用 tools / skills;全过程不发任何中间消息(不要"我帮你看看 / 稍等")。
第二步 · 价值判定(拿到结论后):
- 有信息量的事实 / 答案 / 可执行建议 → 直接发出去,不带开场白。
- 拒绝声明("我没法 / 我无法 / 作为AI")、客套、脱题发散、或仍是过程话 → 单独一行 NO_REPLY。

**Output:要么回复正文,要么单独一行 NO_REPLY。**"""

_MENTION_PROMPT = """\
[Dispatch] 用户在群里直接 @ 了你,必须回复。

允许并鼓励多轮静默调用 tools / skills 去解决问题;过程中不发"我帮你看看 / 让我查一下 / 稍等"等任何中间消息。
拿到结论后直接给最终答案;即使确实做不到,也用自然语气如实说(如"这个我暂时帮不上"),**不要**输出 NO_REPLY。
不要输出 JSON、不要解释自己在做什么。"""

_PROACTIVE_PROMPT = """\
[Dispatch] 你在被动观察群消息,没人 @ 你。默认 NO_REPLY。

仅在下列**全部** YES 时允许回复:
1) 消息没点名另一个人(无"李四 …"、"@张三 …"、"老王:")
2) 你能给出确定有用的答案(不是拒绝、客套、水货、发散)
3) 主动插话不会显得打扰(消息明显是公开提问,如"有人知道X吗")

可以静默调 tools / skills;不要发"我帮你看看 / 让我查一下"等任何中间消息。
其余一律单行 NO_REPLY。"""

# --- Follow-up prompt templates (injected into channel_prompt at dispatch time) ---

# Template A: sender @mentioned bot earlier in the current follow-up window.
# Default: RESPOND.  Only NO_REPLY for clear conversation-closing signals.
_FOLLOW_UP_ENGAGED_TEMPLATE = """\
**Follow-up — 对方在窗口内 @过你,默认回复。** Sender: {sender_label}

仅在以下任一条件命中时输出 NO_REPLY:
1. 收件人显然是别人:"李四 …"、"张三,…"、"@老王 …"、"老刘:"、"帮我问问王哥"
2. 独立成句的结束 / 反应词:"没事了 / 不用回复了 / 谢谢 / 好的 / 收到 / 👍 / 哈哈 / 666 / ..."
3. 你将要发出的内容是拒绝声明("我没法 / 我无法 / 作为AI")、空礼貌、与原消息脱钩的发散

其余一律静默调 tools / skills 直至拿到结论,再**简洁直接**地回答——今天周几、日期、计算、追问、对你上一句的延展都属于"该回"。调用过程不输出任何中间消息("我帮你看看 / 稍等 / 先确认一下" 一律不要)。
拿不准时:能答就答;要发的像拒绝就 NO_REPLY。

**Output:要么回复正文,要么单独一行 NO_REPLY。**"""

# Template B: sender has NOT @mentioned bot in the current follow-up window.
# Default: DO NOT respond.  Only reply when clearly directed at bot.
_FOLLOW_UP_PASSIVE_TEMPLATE = """\
**⚠️ STRICT MODE — 对方在窗口内**没有** @过你,默认 NO_REPLY。** Sender: {sender_label}

第一步 · 收件人门槛(不过则直接 NO_REPLY,**不进入下一步、不调任何 tools**):
仅当下列**全部** YES 才能继续:
1) 消息**显式**指向你(含你的名字 / @你 / 是回复你的消息 / 自然延续了你上一轮且没出现别的收件人);
2) 消息里**没有任何其他人**作为收件人("另一个人名 + 内容" 一律视作不指向你)。

任一 NO → 立刻输出 NO_REPLY,不要解释、不要调任何工具。

**罕见可回的例外**:群里有人问 "有人知道X吗 / 谁会X" 这类公开提问,且看起来你可能答得了 → 视为通过第一步,可以进第二步。

第二步 · 静默探索(仅当第一步全过时):
- 允许并鼓励**多轮**调用 tools / skills 去尝试解决该消息背后的问题。
- 全过程**静默**——绝不发"我帮你看看 / 让我查一下 / 稍等 / 先确认一下"等任何中间消息。

第三步 · 价值判定(拿到最终结论后才做):
- 结论是**确定且有价值**的答案(具体事实 / 可执行结果)→ 简洁直接地发出去,不带开场白、不复述问题、不汇报过程。
- 结论是 "我不知道 / 我无法 / 作为AI"、空礼貌、发散、属于"现实世界你查不到的事"(菜单、实时排期、当下位置)、或仍只是"我去查一下" → 改为单独一行 NO_REPLY。

**绝不要回复**:其它 bot 发的消息(除非它明确求助你)。

**Output:要么回复正文,要么单独一行 NO_REPLY。**"""

# Template C: message is a direct reply/quote to bot's previous message.
# Always respond unless it's an explicit conversation closer.
_FOLLOW_UP_REPLY_TO_BOT_CONTEXT_TEMPLATE = """\
**Follow-up — 这条消息直接回复/引用了你的上一条,目标就是你。** Sender: {sender_label}

NO_REPLY 仅限两种:
- 对方明确结束("不用回复了 / 没事了 / 谢谢 / 好的 / 收到 / 👍 / 哈哈 / 666" 独立成句)
- 你本次回复会是 "我没法 / 我无法 / 我不知道 / 作为AI / 纯礼貌空话" 这类无价值内容

其余 → 静默调 tools / skills(**不发**"我帮你看看 / 稍等"等中间消息)拿到结论后,直接给出有信息量的回复。

**Output:要么回复正文,要么单独一行 NO_REPLY。**"""


def build_follow_up_prompt(
    *,
    fromid: str = "",
    sender_name: str = "",
    is_bot: bool = False,
    agent_id: str = "",
    is_reply_to_bot: bool = False,
    sender_engaged: bool = False,
) -> str:
    """Build a context-aware follow-up prompt with sender info.

    ``sender_engaged`` is True when the sender @mentioned the bot earlier in
    the current follow-up window (template A), False otherwise (template B).
    Template C is used for direct reply-to-bot regardless of engaged state.
    """
    if is_bot:
        sender_label = f"{sender_name} (bot agentId={agent_id})"
    else:
        sender_label = f"{sender_name} ({fromid}, human)"
    if is_reply_to_bot:
        tmpl = _FOLLOW_UP_REPLY_TO_BOT_CONTEXT_TEMPLATE
    elif sender_engaged:
        tmpl = _FOLLOW_UP_ENGAGED_TEMPLATE
    else:
        tmpl = _FOLLOW_UP_PASSIVE_TEMPLATE
    return tmpl.format(
        sender_label=sender_label,
    )


def _join_prompt(*parts: str) -> str:
    return "\n\n---\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def evaluate_inbound(
    inbound: "IncomingMessage",
    policy: GroupPolicy,
    *,
    now: float | None = None,
) -> PolicyDecision:
    """Decide whether to dispatch ``inbound`` to the agent."""
    is_dm = inbound.dm_user_id is not None
    if is_dm:
        return PolicyDecision(
            should_dispatch=True,
            reason="dm",
            action=Action.DISPATCH,
            trigger_reason="direct-message",
        )

    eff = _resolve_for_group(policy, inbound.group_id)
    reply_mode = eff["reply_mode"]

    if reply_mode == "ignore":
        return PolicyDecision(
            should_dispatch=False, reason="reply_mode=ignore", action=Action.SKIP
        )

    if reply_mode == "record":
        return PolicyDecision(
            should_dispatch=False, reason="reply_mode=record", action=Action.RECORD
        )

    bot_mentioned = bool(inbound.bot_was_mentioned)
    reply_to_bot = bool(inbound.is_reply_to_bot)
    direct_signal = bot_mentioned or reply_to_bot
    group_id = inbound.group_id or ""

    if reply_mode == "proactive":
        # Always dispatch, but tell the agent to NO_REPLY if nothing useful.
        trigger = "bot-mentioned" if direct_signal else "proactive"
        per_msg = _MENTION_PROMPT if direct_signal else _PROACTIVE_PROMPT
        return PolicyDecision(
            should_dispatch=True,
            reason="proactive",
            action=Action.DISPATCH,
            trigger_reason=trigger,
            group_system_prompt=eff["system_prompt"],
            per_message_prompt=per_msg,
        )

    if reply_mode == "mention-only":
        if direct_signal:
            return PolicyDecision(
                should_dispatch=True,
                reason="mention-only: bot mentioned",
                action=Action.DISPATCH,
                trigger_reason="bot-mentioned",
                group_system_prompt=eff["system_prompt"],
                per_message_prompt=_MENTION_PROMPT,
            )
        if (
            eff["follow_up"]
            and group_id
            and _within_follow_up_window(policy, group_id, eff["follow_up_window"], now=now)
        ):
            return PolicyDecision(
                should_dispatch=True,
                reason="mention-only: follow-up window",
                action=Action.DISPATCH,
                trigger_reason="followUp",
                group_system_prompt="",  # adapter will enrich with sender context
                needs_sender_context=True,
            )
        if not policy.require_mention:
            return PolicyDecision(
                should_dispatch=True,
                reason="mention-only: require_mention=false",
                action=Action.DISPATCH,
                trigger_reason="require_mention=false",
                group_system_prompt=eff["system_prompt"],
            )
        # Otherwise record (matches OpenClaw's "pending" behavior — accumulate
        # context for future @-mentions).
        return PolicyDecision(
            should_dispatch=False,
            reason="mention-only: bot not mentioned",
            action=Action.RECORD,
        )

    # mention-and-watch
    if direct_signal:
        return PolicyDecision(
            should_dispatch=True,
            reason="mention-and-watch: bot mentioned",
            action=Action.DISPATCH,
            trigger_reason="bot-mentioned",
            group_system_prompt=eff["system_prompt"],
            per_message_prompt=_MENTION_PROMPT,
        )
    watch_hit = _watch_mentioned(inbound, eff["watch_mentions"])
    if watch_hit:
        return PolicyDecision(
            should_dispatch=True,
            reason=f"mention-and-watch: watch list hit ({watch_hit})",
            action=Action.DISPATCH,
            trigger_reason=f"watchMentions:{watch_hit}",
            group_system_prompt=eff["system_prompt"],
            per_message_prompt=_WATCH_MENTION_PROMPT.format(who=watch_hit),
        )
    regex_hit = _watch_regex_match(inbound.text, eff["watch_regex"])
    if regex_hit:
        pattern, idx = regex_hit
        return PolicyDecision(
            should_dispatch=True,
            reason=f"mention-and-watch: regex hit ({pattern})",
            action=Action.DISPATCH,
            trigger_reason=f"watchRegex#{idx}({pattern})",
            group_system_prompt=eff["system_prompt"],
            per_message_prompt=_WATCH_REGEX_PROMPT.format(pattern=pattern),
        )
    if (
        eff["follow_up"]
        and group_id
        and _within_follow_up_window(policy, group_id, eff["follow_up_window"], now=now)
    ):
        return PolicyDecision(
            should_dispatch=True,
            reason="mention-and-watch: follow-up window",
            action=Action.DISPATCH,
            trigger_reason="followUp",
            group_system_prompt="",  # adapter will enrich with sender context
            needs_sender_context=True,
        )
    if not policy.require_mention:
        return PolicyDecision(
            should_dispatch=True,
            reason="mention-and-watch: require_mention=false",
            action=Action.DISPATCH,
            trigger_reason="require_mention=false",
            group_system_prompt=eff["system_prompt"],
        )
    return PolicyDecision(
        should_dispatch=False,
        reason="mention-and-watch: no mention / watch hit",
        action=Action.RECORD,
    )


# ---------------------------------------------------------------------------
# Compatibility shim — legacy callers expected a flat set of fallback modes.
# Retained for any external code still importing the name.
# ---------------------------------------------------------------------------

FALLBACK_REPLY_MODES: frozenset[str] = frozenset()


__all__ = [
    "Action",
    "DEFAULT_FOLLOW_UP",
    "DEFAULT_FOLLOW_UP_WINDOW_SECONDS",
    "DEFAULT_REPLY_MODE",
    "FALLBACK_REPLY_MODES",
    "GroupConfigOverride",
    "GroupPolicy",
    "NormalizedMode",
    "PolicyDecision",
    "VALID_REPLY_MODES",
    "build_follow_up_prompt",
    "evaluate_inbound",
    "normalize_reply_mode",
]
