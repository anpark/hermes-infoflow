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
class ImageRef:
    """LLM-visible reference to an inbound Infoflow IMAGE body."""

    message_id: str = ""
    image_index: int = 0
    source: str = "current_message"


@dataclass
class ReplyTarget:
    """Normalized quoted/replied message target used below serverapi."""

    message_id: str = ""
    preview: str = ""
    sender_key: str = ""
    is_bot_message: bool = False
    platform_is_bot_message: bool = False
    sender_imid: str = ""
    image_refs: list[ImageRef] = field(default_factory=list)


def coerce_image_ref(value: Any) -> ImageRef:
    """Accept transitional raw image refs and return ``ImageRef``."""
    if isinstance(value, ImageRef):
        return value
    if isinstance(value, dict):
        raw_index = first_present(value, "image_index", "imageIndex", "index")
        try:
            index = int(raw_index if raw_index not in (None, "") else 0)
        except (TypeError, ValueError):
            index = 0
        return ImageRef(
            message_id=str(value.get("message_id") or value.get("messageid") or ""),
            image_index=max(0, index),
            source=str(value.get("source") or "current_message"),
        )
    raw_index = (
        getattr(value, "image_index", None)
        if getattr(value, "image_index", None) not in (None, "")
        else getattr(value, "index", 0)
    )
    try:
        index = int(raw_index if raw_index not in (None, "") else 0)
    except (TypeError, ValueError):
        index = 0
    return ImageRef(
        message_id=str(getattr(value, "message_id", "") or ""),
        image_index=max(0, index),
        source=str(getattr(value, "source", "") or "current_message"),
    )


def image_ref_to_dict(value: Any) -> dict[str, Any]:
    ref = coerce_image_ref(value)
    return {
        "message_id": ref.message_id,
        "image_index": ref.image_index,
        "source": ref.source,
    }


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
    face_cid: str = ""
    face_name: str = ""
    message_id: str = ""
    preview: str = ""
    sender_imid: str = ""
    is_bot_message: bool = False
    fid: str = ""
    size: int = 0
    md5: str = ""


@dataclass
class InboundFile:
    """Normalized file attachment received from Infoflow."""

    fid: str
    name: str
    size: int = 0
    ext: str = ""
    md5: str = ""
    chat_type: str = ""       # "group" | "dm"
    api_chat_type: int = 0    # group=2, dm=1 for file-download API
    chat_id: str = ""         # groupid; empty for DM file-download API
    file_msg_id: str = ""
    msgid2: str = ""
    sender_id: str = ""
    sender_imid: str = ""
    local_path: str = ""
    download_status: str = "not_downloaded"  # not_downloaded | downloaded | failed
    download_source: str = ""         # network | cache | empty
    error: str = ""


def coerce_reply_target(value: Any) -> ReplyTarget:
    """Accept transitional raw/parser reply targets and return ReplyTarget."""
    if isinstance(value, ReplyTarget):
        return value
    if isinstance(value, dict):
        image_refs_raw = (
            value.get("image_refs")
            or value.get("imageRefs")
            or value.get("images")
            or []
        )
        return ReplyTarget(
            message_id=str(value.get("message_id") or value.get("messageid") or ""),
            preview=str(value.get("preview") or ""),
            sender_key=str(
                value.get("sender_key")
                or value.get("sender")
                or value.get("senderKey")
                or ""
            ),
            is_bot_message=coerce_bool(
                first_present(value, "is_bot_message", "isBotMessage")
            ),
            platform_is_bot_message=coerce_bool(
                first_present(value, "platform_is_bot_message", "platformIsBotMessage")
            ),
            sender_imid=str(value.get("sender_imid") or ""),
            image_refs=[
                coerce_image_ref(ref)
                for ref in image_refs_raw
                if ref is not None
            ],
        )
    return ReplyTarget(
        message_id=str(getattr(value, "message_id", "") or ""),
        preview=str(getattr(value, "preview", "") or ""),
        sender_key=str(
            getattr(value, "sender_key", "")
            or getattr(value, "sender", "")
            or ""
        ),
        is_bot_message=coerce_bool(getattr(value, "is_bot_message", False)),
        platform_is_bot_message=coerce_bool(
            getattr(value, "platform_is_bot_message", False)
        ),
        sender_imid=str(getattr(value, "sender_imid", "") or ""),
        image_refs=[
            coerce_image_ref(ref)
            for ref in list(getattr(value, "image_refs", None) or [])
            if ref is not None
        ],
    )


def reply_target_to_dict(value: Any) -> dict[str, Any]:
    """Return a JSON-serializable normalized reply target dict."""
    target = coerce_reply_target(value)
    return {
        "message_id": target.message_id,
        "preview": target.preview,
        "sender_key": target.sender_key,
        "is_bot_message": target.is_bot_message,
        "platform_is_bot_message": target.platform_is_bot_message,
        "sender_imid": target.sender_imid,
        "image_refs": [image_ref_to_dict(ref) for ref in target.image_refs],
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
    files: list[InboundFile] = field(default_factory=list)

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
class SentMessageReceipt:
    """One concrete Infoflow message emitted by a send operation."""

    message_id: str
    msgseqid: str = ""
    kind: str = "text"
    preview: str = ""


@dataclass
class SentResult:
    """Result from serverapi after sending a message."""

    success: bool
    message_id: str = ""  # Message ID assigned by Infoflow (unified from messageid/msgkey)
    msgseqid: str = ""  # Message sequence ID
    continuation_message_ids: tuple[str, ...] = ()
    continuation_msgseqids: tuple[str, ...] = ()
    sent_messages: tuple[SentMessageReceipt, ...] = ()
    warnings: tuple[dict[str, str], ...] = ()
    error_code: str = ""
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
