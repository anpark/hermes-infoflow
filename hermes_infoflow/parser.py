"""Parse Infoflow webhook payloads into platform-neutral inbound events.

Port of openclaw-infoflow/src/infoflow-req-parse.ts (the
``parseAndDispatchInfoflowRequest`` + private/group dispatch helpers) and
the body-item / mention extraction logic from bot.ts. Returns a
``ParsedWebhook`` value that the adapter turns into a hermes
``MessageEvent`` after dedup and policy checks.

Key correctness rules (each maps to an OpenClaw line we audited):

* **Echostr branch first** — form-urlencoded with ``echostr`` is a one-time
  setup probe; we MD5-verify the signature and reply with the literal
  echostr value (status 200, ``text/plain``).
* **AES-ECB** with the account's ``encoding_aes_key`` — same key for both
  ``messageJson.Encrypt`` (private) and the raw ``text/plain`` body (group).
* **Large-integer ID precision** — Python ints have arbitrary precision so
  ``json.loads`` does not lose value, but we still extract every ID-shaped
  field (16+ digits, named ``messageid`` / ``msgid`` / ``MsgId`` / ``msgkey``)
  as a *string* and patch the parsed dict, because we'll later need to
  manually splice those values back into outbound recall payloads as raw
  integers (preserving the wire shape Infoflow expects).
* **Dedup key extraction** — ``message.header.messageid`` > ``msgid`` >
  top-level ``MsgId`` > ``{fromuserid}_{groupid}_{ctime}`` composite.
* **bodyForAgent** — for group messages, rebuild the text with the
  bot's ``@robotName``/robotid preserved so the LLM can see who was
  mentioned (matches OpenClaw types.ts ``bodyForAgent``).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs
from xml.etree import ElementTree as ET

from .crypto import InfoflowCryptoError, decrypt_message, verify_echostr_signature

# Regex matches IDs with 16 or more digits — anything shorter is safe to
# leave as a Python int. The capture groups are (field_name, digits).
_ID_FIELD_RE = re.compile(r'"(messageid|msgid|MsgId|msgkey|msgseqid|fromid|msgid2)"\s*:\s*(\d{16,})')

# Field names we patch through ``patch_precise_ids``. Listed here for
# documentation; the regex is the authoritative source.
ID_FIELDS = ("messageid", "msgid", "MsgId", "msgkey", "msgseqid", "fromid", "msgid2")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountConfig:
    """Subset of the Infoflow account config required to parse a webhook.

    Kept as a flat dataclass (not the live hermes ``PlatformConfig``) so
    parser unit tests can construct it without importing hermes-agent.
    """

    check_token: str
    encoding_aes_key: str
    robot_name: str = ""
    app_agent_id: int | None = None
    robot_id: str = ""


@dataclass
class BodyItem:
    """One item in a group message's ``message.body`` array."""

    type: str = ""
    content: str = ""
    label: str = ""
    name: str = ""
    userid: str = ""
    robotid: str = ""   # stored as str to preserve precision; "" when absent
    atall: bool = False  # True when {"type": "AT", "atall": true}
    downloadurl: str = ""
    messageid: str = ""  # for replyData items, the quoted message id (str)
    preview: str = ""    # for replyData items
    is_bot_message: bool = False  # replyData target sent by us?


@dataclass
class InboundMessage:
    """Parsed, decrypted, dedup-pending inbound event.

    The adapter wraps this in a hermes ``MessageEvent`` (text + metadata).
    """

    chat_type: str                    # "dm" | "group"
    from_user: str                    # uuapName (sender)
    text: str                         # raw message text (no @-mention prefix)
    body_for_agent: str               # text with @robotName preserved (for LLM)
    sender_name: str = ""
    message_id: str | None = None
    group_id: str | None = None       # numeric string, "" when DM
    msgseqid: str | None = None
    msgid2: str = ""                  # top-level webhook msgid2 (group only; emoji API)
    timestamp_ms: int | None = None
    raw_msgdata: dict[str, Any] = field(default_factory=dict)
    body_items: list[BodyItem] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    mention_user_ids: list[str] = field(default_factory=list)
    mention_agent_ids: list[str] = field(default_factory=list)
    reply_targets: list[dict[str, Any]] = field(default_factory=list)
    is_reply_to_bot: bool = False
    was_mentioned: bool = False
    # Robot ID discovered from the @-mention AT item. The adapter uses this to
    # persist the bot's actual robotid (Infoflow doesn't tell us upfront) so we
    # can later ignore our own messages echoed back as ALL_MESSAGE_FORWARD
    # events. Empty string when nothing was discovered. Mirrors OpenClaw
    # bot.ts::getBotRobotidFromBody.
    discovered_robot_id: str = ""
    is_at_only: bool = False  # True when body has only AT items, no TEXT/MD
    # The raw root-level ``fromid`` from the inbound payload (group events).
    # Used by the adapter to compare against the persisted robotId for the
    # "ignore own bot message" guard (OpenClaw bot.ts:766-775).
    fromid: str = ""
    # ``eventtype`` from the inbound payload — surfaced so the adapter / policy
    # can branch on ``MESSAGE_RECEIVE`` vs ``ALL_MESSAGE_FORWARD`` etc.
    event_type: str = ""
    # Whether the sender is a bot (agent).  Detected by matching ``fromid``
    # against body-item ``robotid`` values, or by checking if ``fromid`` is a
    # pure numeric string (robotId is always numeric; human uuapName is not).
    is_bot_sender: bool = False

    def dedupe_key(self) -> str | None:
        """Compute the dedup key (priority: message_id > composite)."""
        if self.message_id:
            return str(self.message_id)
        fu = self.from_user or "?"
        gid = self.group_id or "dm"
        ts = self.timestamp_ms or int(time.time() * 1000)
        return f"{fu}_{gid}_{ts}"


@dataclass
class ParsedWebhook:
    """Result of parsing a single inbound HTTP request.

    The adapter inspects ``kind`` and reacts:

    * ``"echostr_ok"``   → return ``status_code`` + ``body`` (the echostr) verbatim.
    * ``"echostr_bad"``  → return 403.
    * ``"http_error"``   → return ``status_code`` + ``body`` (some 4xx).
    * ``"message"``      → ``inbound`` is set; check dedup and dispatch.
    * ``"ignored"``      → respond 200/"OK" but do nothing.
    """

    kind: str
    status_code: int = 200
    body: str = "OK"
    inbound: InboundMessage | None = None


# ---------------------------------------------------------------------------
# Large-integer ID precision protection
# ---------------------------------------------------------------------------


def _find_precise_ids(raw_text: str) -> dict[str, list[str]]:
    """Group ID strings by field name in the order they appear in ``raw_text``."""
    by_field: dict[str, list[str]] = {f: [] for f in ID_FIELDS}
    for match in _ID_FIELD_RE.finditer(raw_text):
        field_name, digits = match.group(1), match.group(2)
        by_field.setdefault(field_name, []).append(digits)
    return {k: v for k, v in by_field.items() if v}


def _patch_field_recursive(
    obj: Any,
    field_name: str,
    values: list[str],
    idx: int,
) -> int:
    """Replace numeric ``field_name`` entries with the next precise string."""
    if isinstance(obj, list):
        for item in obj:
            idx = _patch_field_recursive(item, field_name, values, idx)
        return idx
    if isinstance(obj, dict):
        if field_name in obj and idx < len(values):
            current = obj[field_name]
            if isinstance(current, int) or (
                isinstance(current, str) and current.isdigit() and len(current) >= 16
            ):
                obj[field_name] = values[idx]
                idx += 1
        for v in obj.values():
            if isinstance(v, (dict, list)):
                idx = _patch_field_recursive(v, field_name, values, idx)
    return idx


def patch_precise_ids(raw_text: str, parsed: Any) -> None:
    """In-place replace large numeric IDs in ``parsed`` with their string form.

    Mirrors openclaw-infoflow/src/infoflow-req-parse.ts::patchPreciseIds.
    Python int has arbitrary precision so this is mostly belt-and-braces,
    but downstream code (recall payloads) joins ``str(id)`` into a manual
    JSON literal; having them already be strings avoids accidental
    re-stringification through ``json.dumps`` (which would quote them).
    """
    if not isinstance(parsed, (dict, list)):
        return
    precise = _find_precise_ids(raw_text)
    for field_name, values in precise.items():
        _patch_field_recursive(parsed, field_name, values, 0)


# ---------------------------------------------------------------------------
# XML fallback (private chat sometimes arrives in WeChat-style XML)
# ---------------------------------------------------------------------------


_TAG_RE = re.compile(
    r"<(\w+)>(?:<!\[CDATA\[(?P<cdata>[\s\S]*?)\]\]>|(?P<plain>[^<]*))</\1>",
)


def parse_xml_message(xml_string: str) -> dict[str, str] | None:
    """Lightweight XML parser for the private-chat fallback format.

    Mirrors openclaw-infoflow/src/infoflow-req-parse.ts::parseXmlMessage.
    Returns ``None`` if no tags are found.
    """
    if not xml_string:
        return None
    try:
        # Try a strict parse first for well-formed payloads.
        root = ET.fromstring(xml_string)
        out = {}
        for child in root:
            out[child.tag] = (child.text or "").strip()
        if out:
            return out
    except ET.ParseError:
        pass

    out: dict[str, str] = {}
    for match in _TAG_RE.finditer(xml_string):
        tag = match.group(1)
        cdata = match.group("cdata")
        plain = match.group("plain")
        out[tag] = (cdata if cdata is not None else (plain or "")).strip()
    return out or None


# ---------------------------------------------------------------------------
# Body item / mention extraction (group messages)
# ---------------------------------------------------------------------------


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _coerce_body_item(raw: dict[str, Any]) -> BodyItem:
    return BodyItem(
        type=_stringify(raw.get("type")),
        content=_stringify(raw.get("content")),
        label=_stringify(raw.get("label")),
        name=_stringify(raw.get("name")),
        userid=_stringify(raw.get("userid")),
        robotid=_stringify(raw.get("robotid")),
        atall=bool(raw.get("atall")),
        downloadurl=_stringify(raw.get("downloadurl")),
        messageid=_stringify(raw.get("messageid") or raw.get("sBasemsgId")),
        preview=_stringify(raw.get("preview")),
        is_bot_message=bool(raw.get("isBotMessage") or raw.get("is_bot_message")),
    )


def _normalize_robot_name(name: str) -> str:
    return (name or "").strip().lower()


def _check_bot_mentioned(
    body_items: list[BodyItem],
    *,
    robot_name: str,
    app_agent_id: int | None,
    robot_id: str,
) -> tuple[bool, str]:
    """Return ``(was_mentioned, discovered_robot_id)``.

    Matches against (in order) ``app_agent_id``, ``robot_name``, ``robot_id``
    — same priority as openclaw-infoflow/src/bot.ts::checkBotMentioned.
    """
    norm_name = _normalize_robot_name(robot_name)
    norm_id = (robot_id or "").strip()
    app_agent_id_str = str(app_agent_id) if app_agent_id is not None else ""
    discovered = ""
    for item in body_items:
        if item.type != "AT":
            continue
        if app_agent_id_str and item.robotid and item.robotid == app_agent_id_str:
            return True, item.robotid or discovered
        if norm_name and item.name and item.name.lower() == norm_name:
            if item.robotid:
                discovered = item.robotid
            return True, discovered
        if norm_id and item.robotid and item.robotid == norm_id:
            return True, norm_id
    return False, discovered


def _extract_mention_ids(
    body_items: list[BodyItem],
    *,
    bot_app_agent_id: int | None,
    bot_robot_id: str,
) -> tuple[list[str], list[str]]:
    """Return ``(user_ids, agent_ids)`` of @-mentions excluding the bot itself."""
    bot_id_str = str(bot_app_agent_id) if bot_app_agent_id is not None else ""
    bot_robot = (bot_robot_id or "").strip()
    user_ids: list[str] = []
    agent_ids: list[str] = []
    for item in body_items:
        if item.type != "AT":
            continue
        if item.userid:
            user_ids.append(item.userid)
        elif item.robotid:
            if (bot_id_str and item.robotid == bot_id_str) or (
                bot_robot and item.robotid == bot_robot
            ):
                continue
            agent_ids.append(item.robotid)
    return user_ids, agent_ids


def _extract_reply_targets(
    body_items: list[BodyItem],
    *,
    sent_message_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(targets, is_reply_to_bot)`` extracted from replyData items."""
    targets: list[dict[str, Any]] = []
    is_reply_to_bot = False
    for item in body_items:
        if item.type not in ("replyData", "REPLYDATA", "reply"):
            continue
        if not item.messageid:
            continue
        is_bot = item.is_bot_message
        if not is_bot and sent_message_ids is not None:
            is_bot = item.messageid in sent_message_ids
        targets.append(
            {
                "messageid": item.messageid,
                "preview": item.preview or item.content,
                "isBotMessage": is_bot,
            }
        )
        if is_bot:
            is_reply_to_bot = True
    return targets, is_reply_to_bot


def _build_body_for_agent(body_items: list[BodyItem]) -> tuple[str, str, list[str]]:
    """Reconstruct two strings + the image URL list from the body items.

    Mirrors openclaw-infoflow/src/bot.ts (handleGroupChatMessage body loop):

    * ``raw_text`` — TEXT/MD content only, used for ``CommandBody`` (no @-prefix).
    * ``body_for_agent`` — what the LLM sees: TEXT/MD plus each AT rendered as
      ``@<name> (robotid:<N>) `` (or ``@<name> `` for human ATs). LINK items
      contribute their ``label`` (matches OpenClaw's LINK handling for the
      agent-visible text).
    """
    raw_parts: list[str] = []
    agent_parts: list[str] = []
    image_urls: list[str] = []
    for item in body_items:
        t = (item.type or "").upper()
        if t in ("TEXT", "MD"):
            raw_parts.append(item.content)
            agent_parts.append(item.content)
        elif t == "AT":
            name = item.name or item.userid or item.robotid or "?"
            if item.robotid:
                agent_parts.append(f"@{name} (robotid:{item.robotid}) ")
            else:
                agent_parts.append(f"@{name} ")
        elif t == "LINK":
            label = item.label or item.content or ""
            if label:
                raw_parts.append(f" {label} ")
                agent_parts.append(f" {label} ")
        elif t in ("REPLYDATA", "REPLY"):
            if item.messageid:
                agent_parts.append(f"<引用 message_id:{item.messageid}>{item.content}</引用>")
        elif t == "IMAGE":
            if item.downloadurl:
                image_urls.append(item.downloadurl)
    return ("".join(raw_parts).strip(), "".join(agent_parts).strip(), image_urls)


# ---------------------------------------------------------------------------
# Decryption helpers
# ---------------------------------------------------------------------------


def _try_decrypt_and_parse(
    ciphertext: str,
    encoding_aes_key: str,
    *,
    fallback_xml: bool = False,
) -> dict[str, Any] | None:
    """Decrypt + parse JSON (with XML fallback). Returns None on failure."""
    if not ciphertext.strip() or not encoding_aes_key:
        return None
    try:
        plain = decrypt_message(ciphertext, encoding_aes_key)
    except InfoflowCryptoError:
        return None

    try:
        parsed = json.loads(plain)
        if isinstance(parsed, dict):
            patch_precise_ids(plain, parsed)
            return parsed
    except json.JSONDecodeError:
        pass

    if fallback_xml:
        xml_parsed = parse_xml_message(plain)
        if xml_parsed:
            return dict(xml_parsed)
    return None


# ---------------------------------------------------------------------------
# Private / group conversion
# ---------------------------------------------------------------------------


def build_private_inbound(
    msg_data: dict[str, Any],
    *,
    sent_message_ids: set[str] | None = None,
) -> InboundMessage | None:
    """Translate a decrypted private-chat ``msgData`` to ``InboundMessage``."""
    from_user = _stringify(
        msg_data.get("FromUserId")
        or msg_data.get("fromuserid")
        or msg_data.get("from")
    )
    if not from_user:
        return None

    text = _stringify(
        msg_data.get("Content")
        or msg_data.get("content")
        or msg_data.get("text")
        or msg_data.get("mes")
    ).strip()

    sender_name = _stringify(
        msg_data.get("FromUserName")
        or msg_data.get("username")
        or from_user
    )

    raw_msg_id = (
        msg_data.get("MsgId")
        or msg_data.get("msgid")
        or msg_data.get("messageid")
    )
    message_id = _stringify(raw_msg_id) if raw_msg_id is not None else None

    raw_create_time = msg_data.get("CreateTime", msg_data.get("createtime"))
    if raw_create_time is not None:
        try:
            timestamp_ms = int(raw_create_time) * 1000
        except (TypeError, ValueError):
            timestamp_ms = int(time.time() * 1000)
    else:
        timestamp_ms = int(time.time() * 1000)

    msg_type = _stringify(msg_data.get("MsgType") or msg_data.get("msgtype"))
    pic_url = _stringify(msg_data.get("PicUrl") or msg_data.get("picurl")).strip()
    image_urls: list[str] = []
    if msg_type == "image" and pic_url:
        image_urls.append(pic_url)

    # --- Extract reply (引用) targets from the DM-specific Reply field ---
    reply_targets: list[dict[str, Any]] = []
    is_reply_to_bot = False
    reply_raw = msg_data.get("Reply") or msg_data.get("reply")
    if isinstance(reply_raw, list) and reply_raw:
        for item in reply_raw:
            reply_msg_id = _stringify(item.get("ReplyMsgId") or item.get("replyMsgId"))
            if not reply_msg_id:
                continue
            preview = _stringify(
                item.get("ReplyContent") or item.get("replyContent") or ""
            )
            is_bot = False
            if sent_message_ids is not None:
                is_bot = reply_msg_id in sent_message_ids
            reply_targets.append(
                {
                    "messageid": reply_msg_id,
                    "preview": preview,
                    "isBotMessage": is_bot,
                }
            )
            if is_bot:
                is_reply_to_bot = True

    # Build body_for_agent with reply tag prefix (same format as group chat)
    reply_prefix = ""
    if reply_targets:
        rt = reply_targets[0]
        reply_prefix = f"<引用 message_id:{rt['messageid']}>{rt['preview']}</引用>\n"

    if not text and not image_urls and not reply_targets:
        return None

    if not text and image_urls:
        text = "<media:image>"
    elif not text and reply_targets:
        text = "(引用回复)"

    body_for_agent = reply_prefix + text

    return InboundMessage(
        chat_type="dm",
        from_user=from_user,
        text=text,
        body_for_agent=body_for_agent,
        sender_name=sender_name,
        message_id=message_id,
        timestamp_ms=timestamp_ms,
        raw_msgdata=msg_data,
        image_urls=image_urls,
        was_mentioned=True,  # private chat is always "directly addressed"
        reply_targets=reply_targets,
        is_reply_to_bot=is_reply_to_bot,
    )


def build_group_inbound(
    msg_data: dict[str, Any],
    *,
    account: AccountConfig,
    sent_message_ids: set[str] | None = None,
) -> InboundMessage | None:
    """Translate a decrypted group-chat ``msgData`` to ``InboundMessage``."""
    message = msg_data.get("message") if isinstance(msg_data.get("message"), dict) else None
    header = (message or {}).get("header") if isinstance(message, dict) else None
    if not isinstance(header, dict):
        header = None

    from_user = _stringify(
        (header or {}).get("fromuserid")
        or msg_data.get("fromuserid")
        or msg_data.get("from")
        or msg_data.get("fromid")
    )
    if not from_user:
        return None

    raw_msg_id = (
        (header or {}).get("messageid")
        or (header or {}).get("msgid")
        or msg_data.get("MsgId")
    )
    message_id = _stringify(raw_msg_id) if raw_msg_id is not None else None

    raw_msgid2 = msg_data.get("msgid2")
    msgid2 = _stringify(raw_msgid2) if raw_msgid2 not in (None, "") else ""

    msgseqid_raw = (header or {}).get("msgseqid") or msg_data.get("msgseqid")
    msgseqid = _stringify(msgseqid_raw) if msgseqid_raw is not None else None

    raw_group_id = msg_data.get("groupid") or (header or {}).get("groupid")
    group_id_str: str | None
    group_id_str = None if raw_group_id is None or raw_group_id == "" else _stringify(raw_group_id)

    raw_time = msg_data.get("time") or (header or {}).get("servertime")
    try:
        timestamp_ms = int(raw_time) if raw_time is not None else int(time.time() * 1000)
    except (TypeError, ValueError):
        timestamp_ms = int(time.time() * 1000)

    raw_body = (message or {}).get("body") if isinstance(message, dict) else None
    if not isinstance(raw_body, list):
        raw_body = msg_data.get("body") if isinstance(msg_data.get("body"), list) else []

    body_items: list[BodyItem] = [
        _coerce_body_item(item) for item in raw_body if isinstance(item, dict)
    ]

    raw_text, body_for_agent, image_urls = _build_body_for_agent(body_items)

    was_mentioned_flag, discovered_robot_id = _check_bot_mentioned(
        body_items,
        robot_name=account.robot_name,
        app_agent_id=account.app_agent_id,
        robot_id=account.robot_id,
    )
    event_type = _stringify(msg_data.get("eventtype"))
    if msg_data.get("wasMentioned") is True or event_type == "MESSAGE_RECEIVE":
        was_mentioned = True
    else:
        was_mentioned = was_mentioned_flag
    fromid_str = _stringify(msg_data.get("fromid"))

    mention_user_ids, mention_agent_ids = _extract_mention_ids(
        body_items,
        bot_app_agent_id=account.app_agent_id,
        bot_robot_id=account.robot_id,
    )
    reply_targets, is_reply_to_bot = _extract_reply_targets(
        body_items,
        sent_message_ids=sent_message_ids,
    )

    if not raw_text.strip() and not image_urls and not reply_targets and not body_for_agent:
        return None

    _is_at_only = False  # will be set True if AT-only message

    # Strip raw_text early so whitespace-only content is treated as empty
    _raw_stripped = raw_text.strip()

    # Mirror OpenClaw bot.ts:838-844: when there's no text but media / reply
    # context exists, fall back to a placeholder so the message isn't dropped.
    if not _raw_stripped and image_urls:
        if len(image_urls) > 1:
            text_out = f"<media:image> ({len(image_urls)} images)"
        else:
            text_out = "<media:image>"
    elif not _raw_stripped and reply_targets:
        text_out = "(引用回复)"
    elif not _raw_stripped and body_for_agent:
        # AT-only message (no TEXT/MD body, e.g. user just @'s the bot)
        text_out = body_for_agent
        _is_at_only = True
    else:
        text_out = raw_text
        _is_at_only = False

    # Sender display name: prefer header.username / nickname (OpenClaw bot.ts:849).
    sender_display = _stringify(
        (header or {}).get("username")
        or (header or {}).get("nickname")
        or msg_data.get("username")
        or from_user
    )

    # Detect if the sender is a bot (agent).
    # Strategy 1 (reliable): header.fromuserid is absent for bot senders
    #   and present for human senders.  When fromuserid is missing, from_user
    #   falls back to msg_data.fromid (a numeric robotId).
    # Strategy 2 (supplementary): fromid matches a body-item robotid.
    _header_fromuserid = (header or {}).get("fromuserid") or ""
    _bot_sender = False
    if not _header_fromuserid:
        # No fromuserid → bot sender (human messages always have it)
        _bot_sender = True
    elif fromid_str:
        for bi in body_items:
            if bi.type == "replyData" and bi.robotid:
                continue
            rid = bi.robotid
            if rid and str(rid) == fromid_str:
                _bot_sender = True
                break

    return InboundMessage(
        chat_type="group",
        from_user=from_user,
        text=text_out,
        body_for_agent=body_for_agent or text_out,
        sender_name=sender_display,
        message_id=message_id,
        group_id=group_id_str,
        msgseqid=msgseqid,
        msgid2=msgid2,
        timestamp_ms=timestamp_ms,
        raw_msgdata=msg_data,
        body_items=body_items,
        image_urls=image_urls,
        mention_user_ids=mention_user_ids,
        mention_agent_ids=mention_agent_ids,
        reply_targets=reply_targets,
        is_reply_to_bot=is_reply_to_bot,
        was_mentioned=was_mentioned,
        discovered_robot_id=discovered_robot_id,
        is_at_only=_is_at_only,
        fromid=fromid_str,
        event_type=event_type,
        is_bot_sender=_bot_sender,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_webhook(
    *,
    content_type: str,
    raw_body: str,
    account: AccountConfig,
    sent_message_ids: set[str] | None = None,
) -> ParsedWebhook:
    """Parse a single inbound webhook request.

    ``content_type`` should be the raw header value (case-insensitive
    matching is performed). ``raw_body`` is the decoded request body.

    Returns a ``ParsedWebhook`` indicating what the HTTP handler should
    do next.
    """
    ct = (content_type or "").lower()

    # form-urlencoded: echostr (probe) or messageJson (private)
    if ct.startswith("application/x-www-form-urlencoded"):
        form = parse_qs(raw_body, keep_blank_values=True)

        echostr_list = form.get("echostr")
        if echostr_list:
            echostr = echostr_list[0]
            signature = (form.get("signature") or [""])[0]
            timestamp = (form.get("timestamp") or [""])[0]
            rn = (form.get("rn") or [""])[0]
            ok = verify_echostr_signature(
                rn=rn,
                timestamp=timestamp,
                check_token=account.check_token,
                signature=signature,
            )
            if ok:
                return ParsedWebhook(kind="echostr_ok", status_code=200, body=echostr)
            return ParsedWebhook(kind="echostr_bad", status_code=403, body="Invalid signature")

        message_json_list = form.get("messageJson")
        if message_json_list:
            message_json_str = message_json_list[0]
            try:
                message_json = json.loads(message_json_str)
            except json.JSONDecodeError:
                return ParsedWebhook(
                    kind="http_error", status_code=400, body="invalid messageJson"
                )
            if not isinstance(message_json, dict):
                return ParsedWebhook(
                    kind="http_error", status_code=400, body="invalid messageJson"
                )
            patch_precise_ids(message_json_str, message_json)
            encrypt = message_json.get("Encrypt")
            if not isinstance(encrypt, str) or not encrypt:
                return ParsedWebhook(
                    kind="http_error",
                    status_code=400,
                    body="missing Encrypt field in messageJson",
                )
            decoded = _try_decrypt_and_parse(
                encrypt, account.encoding_aes_key, fallback_xml=True
            )
            if not decoded:
                return ParsedWebhook(
                    kind="http_error",
                    status_code=500,
                    body="decryption failed",
                )
            inbound = build_private_inbound(decoded, sent_message_ids=sent_message_ids)
            if not inbound:
                return ParsedWebhook(kind="ignored")
            return ParsedWebhook(kind="message", inbound=inbound)

        return ParsedWebhook(
            kind="http_error", status_code=400, body="missing echostr or messageJson"
        )

    # text/plain: group chat
    if ct.startswith("text/plain"):
        # OpenClaw infoflow-req-parse.ts::tryDecryptAndDispatch returns 400
        # "empty content" before attempting to decrypt — match that so Infoflow's
        # retry-on-5xx behavior doesn't snowball on empty replays.
        if not raw_body or not raw_body.strip():
            return ParsedWebhook(
                kind="http_error", status_code=400, body="empty content"
            )
        decoded = _try_decrypt_and_parse(raw_body, account.encoding_aes_key)
        if not decoded:
            return ParsedWebhook(
                kind="http_error", status_code=500, body="decryption failed"
            )
        inbound = build_group_inbound(
            decoded,
            account=account,
            sent_message_ids=sent_message_ids,
        )
        if not inbound:
            return ParsedWebhook(kind="ignored")
        return ParsedWebhook(kind="message", inbound=inbound)

    return ParsedWebhook(kind="http_error", status_code=400, body="unsupported content type")


__all__ = [
    "AccountConfig",
    "BodyItem",
    "InboundMessage",
    "ParsedWebhook",
    "build_group_inbound",
    "build_private_inbound",
    "parse_webhook",
    "parse_xml_message",
    "patch_precise_ids",
]
