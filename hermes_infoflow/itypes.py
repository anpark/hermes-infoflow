"""Unified data structures for the infoflow plugin.

Canonical message format used between layers::

    adapter (Hermes format) ←→ bot (bot format) ←→ serverapi (Infoflow API format)

Each layer converts to/from its external format:
  - **adapter**: Hermes ``MessageEvent`` / ``SendResult`` ←→ bot types
  - **serverapi**: bot types ←→ Infoflow API payloads
  - **bot**: all business logic operates on bot types exclusively
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .coerce import coerce_bool, first_present

# ---------------------------------------------------------------------------
# Reply / Quote
# ---------------------------------------------------------------------------

@dataclass
class ReplyInfo:
    """Unified reply/quote context attached to a message."""

    message_id: str  # ID of the original message being replied to
    preview: str = ""  # Preview snippet of the original message
    replytype: str = "1"  # "1" = reply (引用回复), "2" = quote (仅引用)
    sender_imid: str = ""  # imid of the original message sender (for Reply X: display)
    sender_id: str = ""  # uuapName for humans, "" for bots
    sender_agent_id: str = ""  # agentId (int-as-str) for bots, "" for humans


@dataclass
class ReplyTarget:
    """Normalized quoted/replied message target used below serverapi."""

    message_id: str = ""
    preview: str = ""
    is_bot_message: bool = False
    platform_is_bot_message: bool = False
    sender_imid: str = ""


@dataclass
class BodyItem:
    """Normalized group message body item used below the serverapi boundary."""

    type: str = ""
    content: str = ""
    label: str = ""
    name: str = ""
    user_id: str = ""
    robot_id: str = ""
    at_all: bool = False
    download_url: str = ""
    message_id: str = ""
    preview: str = ""
    sender_imid: str = ""
    is_bot_message: bool = False


def coerce_reply_target(value: Any) -> ReplyTarget:
    """Accept transitional raw/parser reply targets and return ReplyTarget."""
    if isinstance(value, ReplyTarget):
        return value
    if isinstance(value, dict):
        return ReplyTarget(
            message_id=str(value.get("message_id") or value.get("messageid") or ""),
            preview=str(value.get("preview") or ""),
            is_bot_message=coerce_bool(
                first_present(value, "is_bot_message", "isBotMessage")
            ),
            platform_is_bot_message=coerce_bool(
                first_present(value, "platform_is_bot_message", "platformIsBotMessage")
            ),
            sender_imid=str(value.get("sender_imid") or ""),
        )
    return ReplyTarget(
        message_id=str(getattr(value, "message_id", "") or ""),
        preview=str(getattr(value, "preview", "") or ""),
        is_bot_message=coerce_bool(getattr(value, "is_bot_message", False)),
        platform_is_bot_message=coerce_bool(
            getattr(value, "platform_is_bot_message", False)
        ),
        sender_imid=str(getattr(value, "sender_imid", "") or ""),
    )


def reply_target_to_dict(value: Any) -> dict[str, Any]:
    """Return a JSON-serializable normalized reply target dict."""
    target = coerce_reply_target(value)
    return {
        "message_id": target.message_id,
        "preview": target.preview,
        "is_bot_message": target.is_bot_message,
        "platform_is_bot_message": target.platform_is_bot_message,
        "sender_imid": target.sender_imid,
    }


# ---------------------------------------------------------------------------
# Incoming
# ---------------------------------------------------------------------------

@dataclass
class IncomingMessage:
    """Unified incoming message from Infoflow.

    Use **group_id** vs **dm_user_id** to determine the chat context —
    no separate ``chat_type`` discriminator needed.
    """

    # Identity
    message_id: str  # Infoflow message ID (unified from messageid/msgkey)
    text: str  # Plain text of the message

    # Target (exactly one is set)
    group_id: str | None = None  # Set for group messages
    dm_user_id: str | None = None  # Set for DM messages (uuapName)

    # Sender
    sender_id: str = ""  # Sender's uuapName
    sender_name: str = ""  # Sender's display name
    sender_imid: str = ""  # Sender's numeric Infoflow imid
    sender_is_bot: bool = False  # Whether the sender is a bot
    sender_agent_id: str = ""  # Sender's agent ID (bots only, enriched from group members)

    # Direct bot-mention context (group only, ignored for DM). ``@all`` is
    # tracked separately by the message store as mentions_everyone.
    bot_was_mentioned: bool = False
    mention_user_ids: list[str] = field(default_factory=list)
    # Robot AT targets as Infoflow robot_id / imid values. These are distinct
    # from app agent IDs and are only mapped to mention_agent_ids through the
    # participants table when that relationship is known.
    mention_robot_ids: list[str] = field(default_factory=list)
    mention_agent_ids: list[int] = field(default_factory=list)

    # Reply / quote context
    reply_info: ReplyInfo | None = None  # Extracted bot-layer reply info
    reply_targets: list[ReplyTarget] = field(default_factory=list)
    is_reply_to_bot: bool = False  # Whether this message replies to the bot

    # Deprecated compatibility field. New code renders DB/LLM text from
    # body_items/reply_targets in message_content.py so service-only ids such as
    # robot_id / imid do not leak into [Message].
    body_for_agent: str = ""

    # Media
    image_urls: list[str] = field(default_factory=list)

    # Normalized body items from serverapi (used by bot/policy/content logic).
    body_items: list[BodyItem] = field(default_factory=list)

    # Dedup & ordering
    dedupe_key: str = ""
    msgseqid: str = ""  # Infoflow message sequence ID
    msgid2: str = ""  # Top-level webhook msgid2 (group only; emoji API, not for LLM)
    timestamp: float = 0.0  # Unix timestamp

    # Robot discovery (populated when the message reveals the bot's own robotId)
    discovered_robot_id: str | None = None

    # True when the message body contains only AT items with no TEXT/MD content.
    # The bot was pinged but no question or instruction was typed.
    is_at_only: bool = False

    # Raw payload (retained for debugging / forward-compat)
    raw_data: dict[str, Any] = field(default_factory=dict)
    event_type: str = ""  # Original event type string from Infoflow

    # -- Derived helpers ------------------------------------------------

    @property
    def is_group(self) -> bool:
        return self.group_id is not None

    @property
    def is_dm(self) -> bool:
        return self.dm_user_id is not None


# ---------------------------------------------------------------------------
# Outbound — send options & results
# ---------------------------------------------------------------------------

@dataclass
class SendOptions:
    """Unified options for sending a message."""

    at_all: bool = False
    mention_user_ids: str = ""  # Comma-separated uuapNames (human users)
    mention_agent_ids: str = ""  # Comma-separated numeric agentIds (bots)
    markdown: bool | None = None  # ``None`` = auto-detect from content

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> SendOptions:
        """Translate Hermes send metadata into bot-layer send options."""
        options = cls()
        if not metadata:
            return options

        def _coerce_csv(value: Any) -> str:
            if isinstance(value, list):
                return ",".join(str(item) for item in value if item)
            return str(value or "")

        options.at_all = coerce_bool(metadata.get("at_all"))
        options.mention_user_ids = _coerce_csv(metadata.get("mention_user_ids"))
        options.mention_agent_ids = _coerce_csv(metadata.get("mention_agent_ids"))
        return options


@dataclass
class SentResult:
    """Result from serverapi after sending a message."""

    success: bool
    message_id: str = ""  # Message ID assigned by Infoflow (unified from messageid/msgkey)
    msgseqid: str = ""  # Message sequence ID
    continuation_message_ids: tuple[str, ...] = ()
    continuation_msgseqids: tuple[str, ...] = ()
    raw_response: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class RecallResult:
    """Result from serverapi after recalling (withdrawing) a message."""

    success: bool
    raw_response: dict[str, Any] = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Group member
# ---------------------------------------------------------------------------

@dataclass
class GroupMember:
    """A member of an Infoflow group."""

    uid: str  # User ID (uuapName for humans, agent ID for bots)
    name: str = ""  # Display name
    imid: str = ""  # Infoflow numeric imid
    agent_id: int = 0  # Agent ID (non-zero for bots)
    is_bot: bool = False


# ---------------------------------------------------------------------------
# Bot processing result
# ---------------------------------------------------------------------------

@dataclass
class ProcessResult:
    """Result from :meth:`Bot.process_inbound`."""

    should_dispatch: bool = False
    decision: Any = None  # ``policy.PolicyDecision`` (kept untyped to avoid circular import)
