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


# ---------------------------------------------------------------------------
# Reply / Quote
# ---------------------------------------------------------------------------

@dataclass
class ReplyInfo:
    """Unified reply/quote context attached to a message."""

    messageid: str  # ID of the original message being replied to
    preview: str = ""  # Preview snippet of the original message
    replytype: str = "1"  # "1" = reply (引用回复), "2" = quote (仅引用)
    sender_imid: str = ""  # imid of the original message sender (for Reply X: display)


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
    msgid: str  # Infoflow message ID
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

    # Bot-mention context (group only, ignored for DM)
    bot_was_mentioned: bool = False
    mention_user_ids: list[str] = field(default_factory=list)
    mention_agent_ids: list[int] = field(default_factory=list)

    # Reply / quote context
    reply_info: ReplyInfo | None = None  # Extracted bot-layer reply info
    reply_targets: list[dict[str, Any]] = field(default_factory=list)  # Raw targets for Hermes raw_message
    is_reply_to_bot: bool = False  # Whether this message replies to the bot

    # Agent-facing content
    body_for_agent: str = ""  # Full text presented to the LLM (may include quoted content)

    # Media
    image_urls: list[str] = field(default_factory=list)

    # Body items from parser (used by policy for watch matching).
    # Typed as Any to avoid coupling to parser.py internals.
    body_items: list[Any] = field(default_factory=list)

    # Dedup & ordering
    dedupe_key: str = ""
    msgseqid: str = ""  # Infoflow message sequence ID
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


@dataclass
class SentResult:
    """Result from serverapi after sending a message."""

    success: bool
    msgid: str = ""  # Message ID assigned by Infoflow (``messageid`` or ``msgkey``)
    msgseqid: str = ""  # Message sequence ID
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
