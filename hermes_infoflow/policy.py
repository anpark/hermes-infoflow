"""Group-message dispatch policy.

OpenClaw exposes five ``replyMode`` values (ignore / record / mention-only /
mention-and-watch / proactive). The Hermes adapter implements:

* ``ignore``              — never dispatch group messages.
* ``mention-only``        — only when the bot is @-mentioned.
* ``mention-and-watch``   — when @-mentioned OR a name in ``watch_mentions``
                            is @-mentioned by someone else.
* ``record`` / ``proactive`` — fall back to ``mention-and-watch`` with a
                              warning at construct time (no silent
                              configuration drift when porting from OpenClaw).

DMs always dispatch (they're 1-on-1; ``was_mentioned`` is True by
convention in :func:`hermes_infoflow.parser.build_private_inbound`).
"""

from __future__ import annotations

from dataclasses import dataclass

from .parser import InboundMessage


VALID_REPLY_MODES = ("ignore", "mention-only", "mention-and-watch")
FALLBACK_REPLY_MODES = {"record", "proactive"}


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
        return NormalizedMode("mention-and-watch")
    val = str(raw).strip().lower()
    if not val:
        return NormalizedMode("mention-and-watch")
    if val in VALID_REPLY_MODES:
        return NormalizedMode(val)
    if val in FALLBACK_REPLY_MODES:
        return NormalizedMode(
            "mention-and-watch",
            warning=(
                f"reply_mode={val!r} is not implemented in hermes-infoflow yet; "
                "falling back to mention-and-watch. Set INFOFLOW_REPLY_MODE to one of "
                f"{', '.join(VALID_REPLY_MODES)} to silence this warning."
            ),
        )
    return NormalizedMode(
        "mention-and-watch",
        warning=(
            f"unknown reply_mode={val!r}; falling back to mention-and-watch. "
            f"Valid values: {', '.join(VALID_REPLY_MODES)}."
        ),
    )


@dataclass(frozen=True)
class GroupPolicy:
    """Configurable policy applied to inbound group messages."""

    reply_mode: str = "mention-and-watch"
    require_mention: bool = True
    watch_mentions: tuple[str, ...] | list[str] = ()


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of evaluating an inbound message against a ``GroupPolicy``."""

    should_dispatch: bool
    reason: str = ""


def _normalize_name(name: str) -> str:
    return name.strip().lower()


def _watch_mentioned(inbound: InboundMessage, watch_list: list[str] | tuple[str, ...]) -> bool:
    """True if any watch-listed name appears as @-mentioned in the body."""
    if not watch_list:
        return False
    norm_watch = {_normalize_name(w) for w in watch_list if w}
    if not norm_watch:
        return False
    for item in inbound.body_items:
        if item.type != "AT":
            continue
        if _normalize_name(item.name or "") in norm_watch:
            return True
        if _normalize_name(item.userid or "") in norm_watch:
            return True
    return False


def evaluate_inbound(inbound: InboundMessage, policy: GroupPolicy) -> PolicyDecision:
    """Decide whether to dispatch ``inbound`` to the agent."""
    if inbound.chat_type == "dm":
        return PolicyDecision(True, reason="dm")

    if policy.reply_mode == "ignore":
        return PolicyDecision(False, reason="reply_mode=ignore")

    bot_mentioned = bool(inbound.was_mentioned)
    watch_hit = _watch_mentioned(inbound, list(policy.watch_mentions))
    reply_to_bot = bool(inbound.is_reply_to_bot)

    if policy.reply_mode == "mention-only":
        if bot_mentioned or reply_to_bot:
            return PolicyDecision(True, reason="mention-only: bot mentioned")
        if policy.require_mention:
            return PolicyDecision(False, reason="mention-only: bot not mentioned")
        return PolicyDecision(True, reason="mention-only: require_mention=false")

    # mention-and-watch
    if bot_mentioned or reply_to_bot:
        return PolicyDecision(True, reason="mention-and-watch: bot mentioned")
    if watch_hit:
        return PolicyDecision(True, reason="mention-and-watch: watch list hit")
    if not policy.require_mention:
        return PolicyDecision(True, reason="mention-and-watch: require_mention=false")
    return PolicyDecision(False, reason="mention-and-watch: no mention / watch hit")


__all__ = [
    "FALLBACK_REPLY_MODES",
    "GroupPolicy",
    "NormalizedMode",
    "PolicyDecision",
    "VALID_REPLY_MODES",
    "evaluate_inbound",
    "normalize_reply_mode",
]
