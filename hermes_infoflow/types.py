"""Unified data structures for the infoflow plugin.

Canonical message format used between layers::

    adapter (Hermes format) ←→ bot (bot format) ←→ serverapi (Infoflow API format)

Each layer converts to/from its external format:
  - **adapter**: Hermes ``MessageEvent`` / ``SendResult`` ←→ bot types
  - **serverapi**: bot types ←→ Infoflow API payloads
  - **bot**: all business logic operates on bot types exclusively

User identity reference
-----------------------
Infoflow has two kinds of users with distinct identifiers:

**Human users:**
  - **uuapName** (= **userid**): Baidu login username, e.g. ``"chengbo05"``.
    This is the primary human identifier used everywhere — inbound AT body
    items carry it in the ``atuserids`` array (and historically a single
    ``userid`` field), outbound AT items also use it, and DM targets are
    addressed by uuapName.  In this codebase the two terms are interchangeable.

**Bot users:**
  - **name**: Display name, e.g. ``"chengbo5.2"``.  Used in the header
    ``fromuser`` field and for inbound name-based mention matching.
  - **agentId** (or **agent_id**): Numeric app-level ID, e.g. ``6533``.
    Used in outbound AT body items as ``atagentids`` and in Hermes
    plugin metadata for routing.

**All users (human & bot):**
  - **imid**: Numeric Infoflow internal ID (stored as a **string** to
    preserve precision).  Assigned by the Infoflow platform.  Rarely needed
    — only required in a few API calls (e.g. reply/quote payloads).
    Not suitable as a general-purpose identifier.
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
    """A member of an Infoflow group.

    See module-level docstring ("User identity reference") for the
    distinction between uuapName/userid, agentId, and imid.
    """

    uid: str           # Human: uuapName (= userid).  Bot: not used as primary ID (see agent_id below).
    name: str = ""     # Display name (used for bots; often empty for humans in this field).
    imid: str = ""     # Numeric Infoflow internal ID (string).  See module docstring — rarely needed.
    agent_id: int = 0  # Agent ID (non-zero for bots only).  Primary bot identifier.
    is_bot: bool = False


# ---------------------------------------------------------------------------
# Bot processing result
# ---------------------------------------------------------------------------

@dataclass
class ProcessResult:
    """Result from :meth:`Bot.process_inbound`."""

    should_dispatch: bool = False
    decision: Any = None  # ``policy.PolicyDecision`` (kept untyped to avoid circular import)
