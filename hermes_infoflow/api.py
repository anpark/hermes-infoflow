"""Infoflow REST API client.

Port of openclaw-infoflow/src/send.ts. Covers:

* App access token acquisition + caching (keyed by appKey, 7200s − 5min buffer).
* Private (DM) message send: text / markdown / richtext (link).
* Group message send: TEXT / MD / AT / LINK / IMAGE body items.
* Group message recall.
* Private message recall.

Non-obvious wire contract bits (do NOT change without an upstream notice):

* Auth header is ``Authorization: Bearer-<token>`` — with a **hyphen**, not
  a space. (openclaw-infoflow/src/send.ts:487-488)
* ``app_secret`` is MD5'd (lowercase hex) before being POSTed to the token
  endpoint. (send.ts:183)
* Recall endpoints' ``messageid`` / ``msgseqid`` / ``groupId`` must be
  serialised as **raw JSON integers** to preserve precision. We hand-build
  the body string instead of going through ``json.dumps`` so 19-digit IDs
  survive intact.
* LINK and IMAGE body items must be sent as their own group messages
  (one body item per request). TEXT / MD / AT items batch into a single
  request. (send.ts:472-475)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_SECONDS = 30.0
TOKEN_TTL_BUFFER_SECONDS = 300
TOKEN_DEFAULT_LIFETIME_SECONDS = 7200

INFOFLOW_AUTH_PATH = "/api/v1/auth/app_access_token"
INFOFLOW_PRIVATE_SEND_PATH = "/api/v1/app/message/send"
INFOFLOW_GROUP_SEND_PATH = "/api/v1/robot/msg/groupmsgsend"
INFOFLOW_GROUP_RECALL_PATH = "/api/v1/robot/group/msgRecall"
INFOFLOW_PRIVATE_RECALL_PATH = "/api/v1/app/message/revoke"

# In-memory token cache, keyed by appKey. Survives across InfoflowClient
# instances within the same process — matches OpenClaw's module-level
# Map<string, {token, expiresAt}>.
_token_cache: dict[str, tuple[str, float]] = {}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class InfoflowAccountAPI:
    """Minimum credentials + endpoint root required to talk to Infoflow."""

    api_host: str
    app_key: str
    app_secret: str
    app_agent_id: int | None = None


@dataclass
class ContentItem:
    """A single piece of outbound content.

    ``type`` ∈ {``text``, ``markdown``, ``link``, ``image``, ``at``,
    ``at-agent``}. ``content`` is the payload (URL, base64 image,
    user-id CSV, etc).
    """

    type: str
    content: str


@dataclass
class ReplyContext:
    """Reply / quote context for group messages.

    Per the Infoflow API docs, reply sits at the same level as header
    and body inside the ``message`` object.
    """

    messageid: str
    preview: str = ""
    replytype: str = "1"  # "1" = reply (default), "2" = quote
    imid: str = ""        # robot imid (required by the API)


@dataclass
class GroupMember:
    """A member of an Infoflow group (user or bot)."""

    uid: str              # userId (humans) or str(agentId) (bots)
    name: str             # display name
    role: str             # "owner" | "manager" | "member"
    is_bot: bool          # True for agent-type members
    agent_id: int | None  # agentId for bots, None for humans
    imid: str = ""        # Infoflow numeric imId / robotId


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def ensure_https(api_host: str) -> str:
    """Force https:// for non-loopback hosts so secrets aren't sent in clear."""
    if api_host.startswith("http://"):
        from urllib.parse import urlparse

        try:
            parsed = urlparse(api_host)
        except Exception:
            return api_host
        host = (parsed.hostname or "").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            return "https://" + api_host[len("http://"):]
    return api_host


def _join(api_host: str, path: str) -> str:
    return ensure_https(api_host).rstrip("/") + path


# ---------------------------------------------------------------------------
# Link content parsing
# ---------------------------------------------------------------------------


def _parse_link_content(content: str) -> tuple[str, str]:
    """Parse ``[label]href`` or bare ``href``. Returns ``(href, label)``."""
    if content.startswith("[") and "]" in content:
        idx = content.index("]")
        if idx > 1:
            label = content[1:idx]
            href = content[idx + 1:]
            return href, label
    return content, content


# ---------------------------------------------------------------------------
# Token acquisition + caching
# ---------------------------------------------------------------------------


class InfoflowAPIError(Exception):
    """Raised when an Infoflow REST call returns a non-ok response."""


async def get_app_access_token(
    account: InfoflowAccountAPI,
    *,
    session: aiohttp.ClientSession | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Fetch (or reuse a cached) app access token.

    Cache key is ``account.app_key``. The token's announced ``expires_in`` is
    honored minus a 5-minute safety buffer.
    """
    cached = _token_cache.get(account.app_key)
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]

    md5_secret = hashlib.md5(account.app_secret.encode("utf-8")).hexdigest().lower()
    url = _join(account.api_host, INFOFLOW_AUTH_PATH)
    payload = {"app_key": account.app_key, "app_secret": md5_secret}

    async with _ensure_session(session) as sess:
        async with sess.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"Content-Type": "application/json"},
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise InfoflowAPIError(
                    f"token endpoint HTTP {resp.status}: {text[:200]}"
                )
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise InfoflowAPIError(f"token response is not JSON: {exc}") from exc

    errcode = data.get("errcode")
    if errcode not in (None, 0):
        raise InfoflowAPIError(
            f"token endpoint errcode={errcode} errmsg={data.get('errmsg')}"
        )

    inner = data.get("data") or {}
    token = inner.get("app_access_token") if isinstance(inner, dict) else None
    if not token:
        raise InfoflowAPIError(f"token response missing app_access_token: {text[:200]}")
    expires_in = TOKEN_DEFAULT_LIFETIME_SECONDS
    if isinstance(inner, dict) and isinstance(inner.get("expires_in"), (int, float)):
        expires_in = int(inner["expires_in"])

    _token_cache[account.app_key] = (
        token,
        now + max(60, expires_in - TOKEN_TTL_BUFFER_SECONDS),
    )
    return token


def clear_token_cache() -> None:
    """Test-only: drop all cached tokens."""
    _token_cache.clear()


def _ensure_session(session: aiohttp.ClientSession | None):
    """Yield ``session`` if supplied, else create+close a one-shot session."""

    class _Wrap:
        def __init__(self, s):
            self.s = s
            self._owned = False

        async def __aenter__(self):
            if self.s is None:
                self.s = aiohttp.ClientSession()
                self._owned = True
            return self.s

        async def __aexit__(self, *exc):
            if self._owned and self.s is not None:
                await self.s.close()

    return _Wrap(session)


def _auth_headers(token: str, *, content_type: str = "application/json; charset=utf-8") -> dict[str, str]:
    """Build the Infoflow auth headers.

    Note: ``Bearer-<token>`` (hyphen, no space). This is the Infoflow
    service's non-standard wire format and matches OpenClaw send.ts:487.
    """
    return {
        "Authorization": f"Bearer-{token}",
        "Content-Type": content_type,
        "LOGID": str(uuid.uuid4()),
    }


# ---------------------------------------------------------------------------
# Private (DM) send
# ---------------------------------------------------------------------------


def _build_private_payload(to_user: str, contents: list[ContentItem]) -> dict[str, Any] | None:
    """Translate generic ``ContentItem`` list into the private send payload."""
    # Special case: if any image item is present, send it as a native private
    # image message (msgtype="image") instead of a text/markdown/richtext
    # payload. OpenClaw's sendInfoflowPrivateImage uses the same endpoint with
    # this shape (media.ts).
    image_items = [item for item in contents if item.type.lower() == "image"]
    if image_items:
        # If more than one image was passed, only the first is sent in this call;
        # callers (the adapter) loop and call us once per image.
        return {
            "touser": to_user,
            "msgtype": "image",
            "image": {"content": image_items[0].content},
        }

    has_link = any(item.type.lower() == "link" for item in contents)

    if has_link:
        richtext: list[dict[str, str]] = []
        for item in contents:
            t = item.type.lower()
            if t == "text" or t in ("md", "markdown"):
                if item.content:
                    richtext.append({"type": "text", "text": item.content})
            elif t == "link" and item.content:
                href, label = _parse_link_content(item.content)
                richtext.append({"type": "a", "href": href, "label": label})
        if not richtext:
            return None
        return {"touser": to_user, "msgtype": "richtext", "richtext": {"content": richtext}}

    text_parts: list[str] = []
    for item in contents:
        t = item.type.lower()
        if t in ("text", "md", "markdown"):
            if item.content:
                text_parts.append(item.content)
    if not text_parts:
        return None
    merged = "\n".join(text_parts)
    return {"touser": to_user, "msgtype": "md", "md": {"content": merged}}


# Pattern for extracting ID fields from raw JSON strings.
# Infoflow uses 16+ digit integers that exceed JavaScript's Number.MAX_SAFE_INTEGER.
# Using regex on raw JSON avoids precision loss from json.loads().
_ID_EXTRACT_RE = re.compile(r'"(%s)"\s*:\s*"?(\d{10,})"?')


def _extract_id(raw_json: str, *fields: str) -> str | None:
    """Extract an ID from a raw JSON string, preserving full integer precision.

    Args:
        raw_json: The raw JSON response string.
        *fields: Field names to try, in priority order (e.g. "messageid", "msgid").

    Returns:
        The ID as a string, or None if not found.
    """
    for field in fields:
        pattern = _ID_EXTRACT_RE.pattern % re.escape(field)
        m = re.search(pattern, raw_json)
        if m:
            return m.group(2)
    return None


async def send_private_message(
    account: InfoflowAccountAPI,
    to_user: str,
    contents: list[ContentItem],
    *,
    session: aiohttp.ClientSession | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Send a private (DM) Infoflow message.

    Returns ``{"ok": True, "msgkey": str | None}`` on success, or
    ``{"ok": False, "error": str, "invaliduser": ...}`` on failure.
    """
    if not account.app_key or not account.app_secret:
        return {"ok": False, "error": "Infoflow appKey/appSecret not configured"}

    payload = _build_private_payload(to_user, contents)
    if payload is None:
        return {"ok": False, "error": "no valid content for private message"}

    try:
        token = await get_app_access_token(account, session=session, timeout=timeout)
    except InfoflowAPIError as exc:
        logger.error("[infoflow:sendPrivate] token error: %s", exc)
        return {"ok": False, "error": str(exc)}

    url = _join(account.api_host, INFOFLOW_PRIVATE_SEND_PATH)
    body_str = json.dumps(payload, ensure_ascii=False)
    headers = _auth_headers(token)

    async with _ensure_session(session) as sess:
        async with sess.post(
            url,
            data=body_str.encode("utf-8"),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            text = await resp.text()
    return _parse_send_response(text, kind="private")


def _parse_send_response(response_text: str, *, kind: str) -> dict[str, Any]:
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"non-JSON response: {response_text[:200]}"}

    code = data.get("code")
    if code != "ok":
        err = data.get("message") or data.get("errmsg") or f"code={code}"
        logger.error("[infoflow:send%s] failed: %s", kind.title(), err)
        return {"ok": False, "error": str(err)}

    inner = data.get("data") if isinstance(data.get("data"), dict) else None
    if inner is not None:
        errcode = inner.get("errcode")
        if errcode not in (None, 0):
            err = inner.get("errmsg") or f"errcode {errcode}"
            logger.error("[infoflow:send%s] failed: %s", kind.title(), err)
            return {
                "ok": False,
                "error": str(err),
                "invaliduser": inner.get("invaliduser"),
            }

    if kind == "private":
        msgkey = _extract_id(response_text, "msgkey", "messageid", "msgid")
        return {"ok": True, "msgkey": msgkey, "invaliduser": (inner or {}).get("invaliduser")}
    if kind == "group":
        messageid = _extract_id(response_text, "messageid", "msgid")
        msgseqid = _extract_id(response_text, "msgseqid")
        return {"ok": True, "messageid": messageid, "msgseqid": msgseqid}
    return {"ok": True}


# ---------------------------------------------------------------------------
# Group send
# ---------------------------------------------------------------------------


def _build_group_body_items(contents: list[ContentItem]) -> tuple[list[dict[str, Any]], bool]:
    """Translate generic ``ContentItem`` into Infoflow's group body item shape.

    All text content is emitted as MD body items.  AT items are prepended
    before the first MD item.
    """
    body: list[dict[str, Any]] = []

    # ── Pass 1: collect AT items ──
    at_all = False
    at_items: list[dict[str, Any]] = []

    for item in contents:
        t = item.type.lower()
        if t == "at":
            if item.content == "all":
                at_all = True
            else:
                ids = [s.strip() for s in item.content.split(",") if s.strip()]
                if ids:
                    for uid in ids:
                        at_items.append({"type": "AT", "atuserids": [uid]})
        elif t == "at-agent":
            ids_int: list[int] = []
            for raw in item.content.split(","):
                s = raw.strip()
                if not s:
                    continue
                try:
                    ids_int.append(int(s))
                except ValueError:
                    continue
            if ids_int:
                at_items.append({"type": "AT", "atagentids": ids_int})

    # ── Pass 2: build body items ──
    md_items: list[dict[str, Any]] = []
    for item in contents:
        t = item.type.lower()
        if t in ("md", "markdown"):
            md_content = item.content or ""
            # Normalize @all variants to lowercase for proper MD rendering
            if at_all:
                md_content = md_content.replace("@所有人", "@all")
                md_content = md_content.replace("@All", "@all")
                md_content = md_content.replace("@ALL", "@all")
            md_items.append({"type": "MD", "content": md_content})
        elif t == "link":
            if item.content:
                href, _ = _parse_link_content(item.content)
                body.append({"type": "LINK", "href": href})
        elif t == "image":
            if item.content:
                body.append({"type": "IMAGE", "content": item.content})
        # "text", "at", "at-agent" are skipped (text always sent as MD now)

    # Prepend AT items before MD content
    at_prefix: list[dict[str, Any]] = []
    if at_all:
        at_prefix.append({"type": "AT", "atall": True, "atuserids": []})
    for d in at_items:
        at_prefix.append(d)

    body = at_prefix + md_items + body
    return body, True  # has_markdown always True


async def send_group_message(
    account: InfoflowAccountAPI,
    group_id: int,
    contents: list[ContentItem],
    *,
    reply_to: ReplyContext | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Send a group Infoflow message (splits LINK/IMAGE into separate messages)."""
    if not account.app_key or not account.app_secret:
        return {"ok": False, "error": "Infoflow appKey/appSecret not configured"}
    if not contents:
        return {"ok": False, "error": "contents array is empty"}

    body_items, has_markdown = _build_group_body_items(contents)
    if not body_items:
        return {"ok": False, "error": "no valid content for group message"}

    text_items = [b for b in body_items if b["type"] not in ("LINK", "IMAGE")]
    link_items = [b for b in body_items if b["type"] == "LINK"]
    image_items = [b for b in body_items if b["type"] == "IMAGE"]

    try:
        token = await get_app_access_token(account, session=session, timeout=timeout)
    except InfoflowAPIError as exc:
        logger.error("[infoflow:sendGroup] token error: %s", exc)
        return {"ok": False, "error": str(exc)}

    url = _join(account.api_host, INFOFLOW_GROUP_SEND_PATH)
    headers = _auth_headers(token, content_type="application/json")
    msg_seq = 0

    async def _post(body: list[dict[str, Any]], msgtype: str, reply: ReplyContext | None):
        nonlocal msg_seq
        payload: dict[str, Any] = {
            "message": {
                "header": {
                    "toid": group_id,
                    "totype": "GROUP",
                    "msgtype": msgtype,
                    "clientmsgid": int(time.time() * 1000) + msg_seq,
                    "role": "robot",
                },
                "body": body,
            }
        }
        msg_seq += 1
        if reply is not None:
            # Per Infoflow docs: reply sits at the same level as header
            # and body inside the message object.
            reply_block: dict[str, Any] = {
                "messageid": reply.messageid,
                "preview": reply.preview,
                "replytype": reply.replytype,
            }
            if reply.imid:
                reply_block["imid"] = reply.imid
            payload["message"]["reply"] = reply_block
        _log = logging.getLogger("hermes_plugins.infoflow.api")
        _log.debug(
            "[infoflow] reply payload: messageid=%s preview=%r replytype=%s",
            reply.messageid if reply else None,
            reply.preview[:80] if reply else None,
            reply.replytype if reply else None,
        )
        body_str = json.dumps(payload, ensure_ascii=False)
        logger.info("[infoflow:send_payload] %s", body_str)
        async with _ensure_session(session) as sess:
            async with sess.post(
                url,
                data=body_str.encode("utf-8"),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
        return _parse_send_response(text, kind="group")

    last_messageid: str | None = None
    last_msgseqid: str | None = None
    first_error: str | None = None
    reply_applied = False

    if text_items:
        msgtype = "MD"
        res = await _post(
            text_items,
            msgtype,
            reply_to if not reply_applied else None,
        )
        reply_applied = True
        if res.get("ok"):
            last_messageid = res.get("messageid") or last_messageid
            last_msgseqid = res.get("msgseqid") or last_msgseqid
        else:
            first_error = first_error or res.get("error")

    for link in link_items:
        res = await _post([link], "TEXT", reply_to if not reply_applied else None)
        reply_applied = True
        if res.get("ok"):
            last_messageid = res.get("messageid") or last_messageid
            last_msgseqid = res.get("msgseqid") or last_msgseqid
        else:
            first_error = first_error or res.get("error")

    for img in image_items:
        res = await _post([img], "IMAGE", reply_to if not reply_applied else None)
        reply_applied = True
        if res.get("ok"):
            last_messageid = res.get("messageid") or last_messageid
            last_msgseqid = res.get("msgseqid") or last_msgseqid
        else:
            first_error = first_error or res.get("error")

    if first_error:
        return {
            "ok": False,
            "error": first_error,
            "messageid": last_messageid,
            "msgseqid": last_msgseqid,
        }
    return {"ok": True, "messageid": last_messageid, "msgseqid": last_msgseqid}


# ---------------------------------------------------------------------------
# Group members
# ---------------------------------------------------------------------------


async def get_group_members(
    account: InfoflowAccountAPI,
    *,
    group_id: int | str,
    session: aiohttp.ClientSession | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[GroupMember]:
    """Fetch the member list of a group.

    Returns a list of :class:`GroupMember` objects (both humans and bots).
    """
    _log = logging.getLogger("hermes_plugins.infoflow.api")
    api_host = ensure_https(account.api_host)
    token = await get_app_access_token(account, session=session)
    url = f"{api_host}/api/v1/robot/group/memberList"
    headers = _auth_headers(token, content_type="application/json")
    body = json.dumps({"groupId": int(group_id), "recallType": 0})

    async with _ensure_session(session) as sess:
        async with sess.post(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            text = await resp.text()

    try:
        data = json.loads(text)
    except Exception:
        _log.warning("[infoflow] get_group_members: non-JSON response: %s", text[:200])
        return []

    inner = data.get("data", data)
    # Handle nested {"data":{"data":{...}}} structure
    if isinstance(inner, dict) and "data" in inner and isinstance(inner["data"], dict):
        inner = inner["data"]
    members: list[GroupMember] = []

    for u in inner.get("userInfoList") or []:
        members.append(GroupMember(
            uid=u.get("userId", ""),
            name=u.get("name", u.get("userId", "")),
            role=u.get("role", "member"),
            is_bot=False,
            agent_id=None,
            imid="",  # humans don't have imId in this API
        ))

    for a in inner.get("agentInfoList") or []:
        aid = a.get("agentId")
        members.append(GroupMember(
            uid=str(aid) if aid is not None else "",
            name=a.get("name", f"机器人{aid}"),
            role=a.get("role", "member"),
            is_bot=True,
            agent_id=int(aid) if aid is not None else None,
            imid=str(a.get("imId", "")),  # robot imId = webhook fromid
        ))

    _log.debug(
        "[infoflow] get_group_members(%s): %d members (%d humans, %d bots)",
        group_id, len(members),
        sum(1 for m in members if not m.is_bot),
        sum(1 for m in members if m.is_bot),
    )
    return members


# ---------------------------------------------------------------------------
# Group recall
# ---------------------------------------------------------------------------


async def recall_group_message(
    account: InfoflowAccountAPI,
    *,
    group_id: int,
    messageid: str,
    msgseqid: str,
    session: aiohttp.ClientSession | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Recall a previously-sent group message (撤回).

    ``messageid`` / ``msgseqid`` arrive as strings (preserving large-int
    precision); we splice them into the JSON body as raw integers using an
    f-string so json.dumps' string-quoting can't damage them.
    """
    if not account.app_key or not account.app_secret:
        return {"ok": False, "error": "Infoflow appKey/appSecret not configured"}
    try:
        gid = int(group_id)
        mid = int(messageid)
        seq = int(msgseqid)
    except (TypeError, ValueError):
        return {"ok": False, "error": "groupId/messageid/msgseqid must be integers"}

    try:
        token = await get_app_access_token(account, session=session, timeout=timeout)
    except InfoflowAPIError as exc:
        return {"ok": False, "error": str(exc)}

    url = _join(account.api_host, INFOFLOW_GROUP_RECALL_PATH)
    body = f'{{"groupId":{gid},"messageid":{mid},"msgseqid":{seq}}}'
    headers = _auth_headers(token)

    async with _ensure_session(session) as sess:
        async with sess.post(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            text = await resp.text()
    return _parse_recall_response(text, kind="group")


async def recall_private_message(
    account: InfoflowAccountAPI,
    *,
    msgkey: str,
    session: aiohttp.ClientSession | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Recall a previously-sent private message.

    Requires ``account.app_agent_id``.
    """
    if not account.app_key or not account.app_secret:
        return {"ok": False, "error": "Infoflow appKey/appSecret not configured"}
    if account.app_agent_id is None:
        return {
            "ok": False,
            "error": "Infoflow appAgentId is required for private message recall",
        }

    try:
        token = await get_app_access_token(account, session=session, timeout=timeout)
    except InfoflowAPIError as exc:
        return {"ok": False, "error": str(exc)}

    url = _join(account.api_host, INFOFLOW_PRIVATE_RECALL_PATH)
    # ``msgkey`` is a string (the messaging API returns it that way), so json.dumps
    # correctly escapes embedded quotes/backslashes. ``agentid`` is a normal-sized
    # int. We deliberately do NOT manually splice msgkey because an attacker-controlled
    # msgkey could otherwise break out of the JSON literal.
    body = json.dumps({"msgkey": str(msgkey), "agentid": int(account.app_agent_id)})
    headers = _auth_headers(token)

    async with _ensure_session(session) as sess:
        async with sess.post(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            text = await resp.text()
    return _parse_recall_response(text, kind="private")


def _parse_recall_response(response_text: str, *, kind: str) -> dict[str, Any]:
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"non-JSON response: {response_text[:200]}"}
    code = data.get("code")
    if code != "ok":
        err = data.get("message") or data.get("errmsg") or f"code={code}"
        logger.error("[infoflow:recall%s] failed: %s", kind.title(), err)
        return {"ok": False, "error": str(err)}
    inner = data.get("data") if isinstance(data.get("data"), dict) else None
    if inner is not None and inner.get("errcode") not in (None, 0):
        err = inner.get("errmsg") or f"errcode {inner.get('errcode')}"
        logger.error("[infoflow:recall%s] failed: %s", kind.title(), err)
        return {"ok": False, "error": str(err)}
    return {"ok": True}


__all__ = [
    "ContentItem",
    "InfoflowAPIError",
    "GroupMember",
    "InfoflowAccountAPI",
    "ReplyContext",
    "clear_token_cache",
    "ensure_https",
    "get_app_access_token",
    "get_group_members",
    "recall_group_message",
    "recall_private_message",
    "send_group_message",
    "send_private_message",
]


# ---------------------------------------------------------------------------
# Public test helpers (for tests/test_api.py)
# ---------------------------------------------------------------------------


def _sync_run(coro):  # pragma: no cover - convenience helper
    return asyncio.get_event_loop().run_until_complete(coro)
