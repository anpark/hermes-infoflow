"""Inbound Infoflow file download and LLM attachment rendering."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from .itypes import InboundFile
from .paths import get_infoflow_inbound_files_root

logger = logging.getLogger(__name__)

DEFAULT_INBOUND_FILE_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_DOWNLOAD_URL_EXP_SECONDS = 180
GROUP_DOWNLOAD_URL_PATH = "/api/v1/open-file-service/file/get/download/url/byFid"
DM_DOWNLOAD_URL_PATH = "/api/v1/open-file-service/file/get/download/url/robot-chat/byFid"

_UNSAFE_FILENAME_RE = re.compile(r'[\x00-\x1f\x7f/\\<>:"|?*#%&]+')
_WHITESPACE_RE = re.compile(r"\s+")


def _gw_log() -> logging.Logger:
    return logging.getLogger("gateway.run")


def _log_file_status(action: str, file: InboundFile) -> None:
    try:
        _gw_log().info(
            "[infoflow:file_inbound] action=%s status=%s source=%s fid=%s "
            "file_msg_id=%s chat_type=%s chat_id=%s name=%r size=%s path=%s error=%s",
            action,
            file.download_status or "-",
            file.download_source or "-",
            file.fid or "-",
            file.file_msg_id or "-",
            file.chat_type or "-",
            file.chat_id or "-",
            file.name or "",
            file.size,
            file.local_path or "-",
            file.error or "-",
        )
    except Exception:
        logger.debug("[infoflow:file_inbound] logging failed", exc_info=True)


def _settings_max_bytes(settings: dict[str, Any] | None) -> int:
    value = (settings or {}).get("inbound_file_max_bytes")
    if value in (None, ""):
        value = os.getenv("HERMES_INFOFLOW_INBOUND_FILE_MAX_BYTES", "")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_INBOUND_FILE_MAX_BYTES
    return max(1, parsed)


def _file_api_host(settings: dict[str, Any] | None) -> str:
    raw = (
        os.getenv("INFOFLOW_FILE_API_HOST", "").strip()
        or str((settings or {}).get("file_api_host") or "").strip()
        or "http://apiin.im.baidu.com"
    )
    return raw.rstrip("/")


def sanitize_inbound_file_name(file_name: str, *, fallback: str = "file") -> str:
    name = Path(str(file_name or "")).name.strip()
    name = name.replace("..", "")
    name = _UNSAFE_FILENAME_RE.sub("_", name)
    name = _WHITESPACE_RE.sub("_", name)
    name = name.strip(" ._")
    if not name:
        name = fallback
    path = Path(name)
    suffix = path.suffix
    stem = path.stem if suffix else name
    if not stem:
        stem = fallback
    max_len = 160
    if len(name) <= max_len:
        return name
    if suffix and len(suffix) < max_len:
        return f"{stem[: max_len - len(suffix)]}{suffix}"
    return name[:max_len]


def _safe_segment(value: str, *, fallback: str) -> str:
    segment = sanitize_inbound_file_name(value, fallback=fallback)
    return segment or fallback


def _short_fid(fid: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", str(fid or ""))
    return cleaned[:64] or "nofid"


def inbound_file_target_path(
    file: InboundFile,
    *,
    settings: dict[str, Any] | None = None,
    now: float | None = None,
) -> Path:
    ts = time.time() if now is None else float(now)
    day = datetime.fromtimestamp(ts).strftime("%Y%m%d")
    root = get_infoflow_inbound_files_root(settings).expanduser()
    if file.chat_type == "group":
        chat_segment = f"group-{file.chat_id or 'unknown'}"
    else:
        chat_segment = f"dm-{file.sender_id or 'unknown'}"
    message_segment = file.file_msg_id or "unknown-message"
    target_dir = (
        root
        / day
        / _safe_segment(chat_segment, fallback="chat")
        / _safe_segment(message_segment, fallback="message")
        / _safe_segment(_short_fid(file.fid), fallback="file")
    )
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    name = sanitize_inbound_file_name(file.name, fallback=f"file-{_short_fid(file.fid)}")
    candidate = target_dir / name
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("inbound file path escapes inbound root") from exc
    return candidate


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _md5_matches(actual: str, expected: str) -> bool:
    expected_clean = str(expected or "").strip().lower()
    if not expected_clean:
        return True
    return actual.lower() == expected_clean


def _cached_file_valid(path: Path, file: InboundFile) -> bool:
    if not path.is_file():
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if file.size and len(data) != int(file.size):
        return False
    return _md5_matches(_md5_hex(data), file.md5)


def build_download_url_request(
    file: InboundFile,
    *,
    exp_seconds: int = DEFAULT_DOWNLOAD_URL_EXP_SECONDS,
) -> tuple[str, dict[str, Any]]:
    if file.chat_type == "group" or int(file.api_chat_type or 0) == 2:
        body: dict[str, Any] = {
            "fid": file.fid,
            "chatId": int(file.chat_id) if str(file.chat_id).isdigit() else file.chat_id,
            "chatType": 2,
            "fileMsgId": file.file_msg_id,
            "expSeconds": exp_seconds,
        }
        return GROUP_DOWNLOAD_URL_PATH, body
    body = {
        "fid": file.fid,
        "chatType": 1,
        "fileMsgId": file.file_msg_id,
        "expSeconds": exp_seconds,
    }
    return DM_DOWNLOAD_URL_PATH, body


def _download_url_from_response(data: dict[str, Any]) -> tuple[str, str]:
    outer_code = data.get("code")
    if outer_code not in (None, "ok", 0, "0"):
        return "", f"download_url_code_{outer_code}"
    top_level_url = str(data.get("url") or "").strip()
    if top_level_url:
        return top_level_url, ""
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    status = payload.get("status")
    if status not in (None, 0, "0"):
        return "", f"download_url_status_{status}"
    direct_url = str(payload.get("url") or "").strip()
    if direct_url:
        return direct_url, ""
    inner = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    url = str(inner.get("url") or "").strip()
    if not url:
        return "", "download_url_empty"
    return url, ""


def _fail(file: InboundFile, error: str) -> InboundFile:
    file.download_status = "failed"
    file.download_source = ""
    file.error = error
    _log_file_status("failed", file)
    return file


async def get_inbound_file_download_url(
    serverapi: Any,
    file: InboundFile,
    *,
    token: str,
    session: aiohttp.ClientSession,
    settings: dict[str, Any] | None = None,
) -> tuple[str, str]:
    path, body = build_download_url_request(file)
    url = f"{_file_api_host(settings)}{path}"
    async with session.post(
        url,
        json=body,
        headers=serverapi.auth_headers(token, content_type="application/json"),
        timeout=aiohttp.ClientTimeout(total=15.0),
    ) as resp:
        text = await resp.text()
        if resp.status >= 400:
            return "", f"download_url_http_{resp.status}"
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return "", "download_url_invalid_json"
    return _download_url_from_response(data)


async def _download_bytes(
    serverapi: Any,
    url: str,
    *,
    token: str,
    session: aiohttp.ClientSession,
    max_bytes: int,
) -> tuple[bytes, str, str]:
    headers = serverapi.openapi_gateway_identity_headers(token)
    async with session.get(
        url,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=60.0),
    ) as resp:
        content_md5 = str(resp.headers.get("Content-Md5") or "")
        if resp.status >= 400:
            return b"", content_md5, f"download_http_{resp.status}"
        buf = bytearray()
        async for chunk in resp.content.iter_chunked(64 * 1024):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                return b"", content_md5, "download_too_large"
        return bytes(buf), content_md5, ""


async def download_inbound_file(
    serverapi: Any,
    file: InboundFile,
    *,
    session: aiohttp.ClientSession | None = None,
    settings: dict[str, Any] | None = None,
) -> InboundFile:
    max_bytes = _settings_max_bytes(settings)
    if not file.fid:
        return _fail(file, "missing_fid")
    if file.size and int(file.size) > max_bytes:
        return _fail(file, "file_too_large")

    target = inbound_file_target_path(file, settings=settings)
    if _cached_file_valid(target, file):
        file.local_path = str(target)
        file.download_status = "downloaded"
        file.download_source = "cache"
        file.error = ""
        _log_file_status("cache", file)
        return file

    async with serverapi._ensure_session(session) as sess:  # noqa: SLF001
        token = await serverapi.get_access_token(session=sess)
        download_url, error = await get_inbound_file_download_url(
            serverapi,
            file,
            token=token,
            session=sess,
            settings=settings,
        )
        if error == "download_url_http_401":
            token = await serverapi.get_access_token(session=sess, force_refresh=True)
            download_url, error = await get_inbound_file_download_url(
                serverapi,
                file,
                token=token,
                session=sess,
                settings=settings,
            )
        if error:
            return _fail(file, error)
        data, content_md5, error = await _download_bytes(
            serverapi,
            download_url,
            token=token,
            session=sess,
            max_bytes=max_bytes,
        )
        if error == "download_http_401":
            token = await serverapi.get_access_token(session=sess, force_refresh=True)
            download_url, error = await get_inbound_file_download_url(
                serverapi,
                file,
                token=token,
                session=sess,
                settings=settings,
            )
            if not error:
                data, content_md5, error = await _download_bytes(
                    serverapi,
                    download_url,
                    token=token,
                    session=sess,
                    max_bytes=max_bytes,
                )
        if error:
            return _fail(file, error)

    if file.size and len(data) != int(file.size):
        return _fail(file, "size_mismatch")
    actual_md5 = _md5_hex(data)
    if content_md5 and not _md5_matches(actual_md5, content_md5):
        return _fail(file, "response_md5_mismatch")
    if not _md5_matches(actual_md5, file.md5):
        return _fail(file, "webhook_md5_mismatch")

    tmp = target.with_name(f"{target.name}.part")
    try:
        tmp.write_bytes(data)
        tmp.replace(target)
    except OSError:
        return _fail(file, "write_failed")
    file.local_path = str(target)
    file.download_status = "downloaded"
    file.download_source = "network"
    file.error = ""
    _log_file_status("downloaded", file)
    return file


def inbound_file_to_raw_dict(file: InboundFile) -> dict[str, Any]:
    status = file.download_status or "not_downloaded"
    if status == "pending":
        status = "not_downloaded"
    return {
        "fid": file.fid,
        "name": file.name,
        "ext": file.ext,
        "size": file.size,
        "md5": file.md5,
        "chat_type": file.chat_type,
        "api_chat_type": file.api_chat_type,
        "chat_id": file.chat_id,
        "file_msg_id": file.file_msg_id,
        "msgid2": file.msgid2,
        "sender_id": file.sender_id,
        "sender_imid": file.sender_imid,
        "local_path": file.local_path,
        "download_status": status,
        "download_source": file.download_source,
        "error": file.error,
    }


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _str_any(*values: Any) -> str:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return ""


def _int_any(*values: Any) -> int:
    raw = _str_any(*values).strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _ext_from_name(name: str) -> str:
    suffix = Path(str(name or "")).suffix
    return suffix[1:].lower() if suffix.startswith(".") else ""


def inbound_file_from_raw_dict(data: dict[str, Any]) -> InboundFile | None:
    """Restore an ``InboundFile`` from framework-persisted attachment metadata."""

    if not isinstance(data, dict):
        return None
    fid = _str_any(data.get("fid"))
    name = _str_any(data.get("name"), f"file-{_short_fid(fid)}")
    if not fid and not name:
        return None
    file = InboundFile(
        fid=fid,
        name=name,
        size=_int_any(data.get("size")),
        ext=_str_any(data.get("ext"), _ext_from_name(name)),
        md5=_str_any(data.get("md5")),
        chat_type=_str_any(data.get("chat_type")),
        api_chat_type=_int_any(data.get("api_chat_type")),
        chat_id=_str_any(data.get("chat_id")),
        file_msg_id=_str_any(data.get("file_msg_id")),
        msgid2=_str_any(data.get("msgid2")),
        sender_id=_str_any(data.get("sender_id")),
        sender_imid=_str_any(data.get("sender_imid")),
        local_path=_str_any(data.get("local_path")),
        download_status=_str_any(
            data.get("download_status"),
            data.get("status"),
            "not_downloaded",
        ),
        download_source=_str_any(data.get("download_source")),
        error=_str_any(data.get("error")),
    )
    if file.download_status == "pending":
        file.download_status = "not_downloaded"
    if file.download_status == "cached":
        file.download_status = "downloaded"
        file.download_source = file.download_source or "cache"
    return file


def inbound_files_from_raw_payload(
    payload: dict[str, Any],
    *,
    chat_type: str,
    chat_id: str = "",
    file_msg_id: str = "",
    msgid2: str = "",
    sender_id: str = "",
    sender_imid: str = "",
) -> list[InboundFile]:
    """Extract file metadata from an Infoflow raw webhook payload for history use."""

    if not isinstance(payload, dict):
        return []

    normalized_chat_type = "group" if str(chat_type or "").lower() == "group" else "dm"
    message = _as_dict(payload.get("message"))
    header = _as_dict(message.get("header"))
    base_file_msg_id = _str_any(
        file_msg_id,
        header.get("messageid"),
        payload.get("MsgId"),
        payload.get("msgid"),
    )
    base_msgid2 = _str_any(msgid2, payload.get("msgid2"), payload.get("MsgId2"))
    base_sender_id = _str_any(
        sender_id,
        header.get("fromuserid"),
        payload.get("FromUserId"),
        payload.get("fromuserid"),
    )
    base_sender_imid = _str_any(
        sender_imid,
        payload.get("fromid"),
        header.get("fromid"),
        payload.get("FromId"),
    )

    if normalized_chat_type == "group":
        body = _as_list(message.get("body"))
        files: list[InboundFile] = []
        group_id = _str_any(chat_id, payload.get("groupid"), header.get("groupid"))
        for item in body:
            raw = _as_dict(item)
            if str(raw.get("type") or raw.get("msgtype") or "").upper() != "FILE":
                continue
            fid = _str_any(raw.get("fid"), raw.get("fileid"), raw.get("FileId"))
            name = _str_any(raw.get("name"), raw.get("filename"), raw.get("fileName"), f"file-{_short_fid(fid)}")
            files.append(InboundFile(
                fid=fid,
                name=name,
                size=_int_any(raw.get("size"), raw.get("FileSize"), raw.get("fsz")),
                ext=_str_any(raw.get("ext"), raw.get("filetype"), _ext_from_name(name)),
                md5=_str_any(raw.get("md5"), raw.get("FileMd5"), raw.get("fmd5")),
                chat_type="group",
                api_chat_type=2,
                chat_id=group_id,
                file_msg_id=base_file_msg_id,
                msgid2=base_msgid2,
                sender_id=base_sender_id,
                sender_imid=base_sender_imid,
            ))
        return files

    msg_type = str(payload.get("MsgType") or payload.get("msgtype") or "").lower()
    fid = _str_any(payload.get("FileId"), payload.get("fid"), payload.get("fileid"))
    if msg_type != "file" and not fid:
        return []
    name = _str_any(payload.get("Name"), payload.get("name"), payload.get("filename"), f"file-{_short_fid(fid)}")
    return [InboundFile(
        fid=fid,
        name=name,
        size=_int_any(payload.get("FileSize"), payload.get("size"), payload.get("fsz")),
        ext=_str_any(payload.get("FileType"), payload.get("ext"), _ext_from_name(name)),
        md5=_str_any(payload.get("FileMd5"), payload.get("md5"), payload.get("fmd5")),
        chat_type="dm",
        api_chat_type=1,
        chat_id="",
        file_msg_id=base_file_msg_id,
        msgid2=base_msgid2,
        sender_id=base_sender_id,
        sender_imid=base_sender_imid,
    )]


def inbound_file_to_attachment_dict(
    file: InboundFile,
    *,
    file_index: int | None = None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "file",
        "name": file.name,
        "ext": file.ext,
        "size": file.size,
    }
    if file.md5:
        base["md5"] = file.md5
    if file.file_msg_id:
        base["message_id"] = file.file_msg_id
    if file_index is not None:
        base["file_index"] = int(file_index)
    if file.download_status == "downloaded" and file.local_path:
        base["status"] = "downloaded"
        base["path"] = file.local_path
    elif file.download_status == "failed":
        base["status"] = "failed"
        base["error"] = file.error or "download_failed"
    else:
        base["status"] = "not_downloaded"
    return base


def render_attachments_block(files: list[InboundFile]) -> str:
    if not files:
        return ""
    payload = {
        "files": [
            inbound_file_to_attachment_dict(file, file_index=index)
            for index, file in enumerate(files)
        ],
    }
    return (
        "[Attachments]\n"
        f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n"
        "[/Attachments]"
    )


__all__ = [
    "DEFAULT_DOWNLOAD_URL_EXP_SECONDS",
    "DEFAULT_INBOUND_FILE_MAX_BYTES",
    "DM_DOWNLOAD_URL_PATH",
    "GROUP_DOWNLOAD_URL_PATH",
    "build_download_url_request",
    "download_inbound_file",
    "get_inbound_file_download_url",
    "inbound_file_target_path",
    "inbound_file_from_raw_dict",
    "inbound_files_from_raw_payload",
    "inbound_file_to_attachment_dict",
    "inbound_file_to_raw_dict",
    "render_attachments_block",
    "sanitize_inbound_file_name",
]
