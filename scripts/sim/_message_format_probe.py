"""Shared helpers for real Infoflow message-format probes.

These helpers are intentionally small and dependency-light. Probe scripts
under ``scripts/sim`` use them to send real messages, redact image payloads,
and correlate group-message webhook echoes from local Hermes logs.
"""

from __future__ import annotations

import base64
import json
import os
import re
import struct
import time
import zlib
from pathlib import Path
from typing import Any


def marker() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def default_group_id() -> str:
    return (
        os.environ.get("INFOFLOW_REAL_TEST_GROUP", "").strip()
        or os.environ.get("INFOFLOW_OP_GROUP", "").strip()
    )


def require_group_id(value: str | None) -> str:
    group_id = str(value or default_group_id()).strip()
    if not group_id:
        raise SystemExit(
            "[sim] group id is required; pass --group or set INFOFLOW_OP_GROUP."
        )
    if not group_id.isdigit():
        raise SystemExit("[sim] group id must be numeric.")
    return group_id


def default_test_user() -> str:
    return (
        os.environ.get("INFOFLOW_TEST_USER", "").strip()
        or os.environ.get("INFOFLOW_ADMIN_USER", "").strip()
    )


def require_test_user(value: str | None) -> str:
    user = str(value or default_test_user()).strip()
    if not user:
        raise SystemExit(
            "[sim] test user is required; pass --user or set INFOFLOW_TEST_USER."
        )
    return user


def parse_int(value: str | None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"[sim] expected integer, got {raw!r}") from exc


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def solid_blue_png_200() -> bytes:
    """Return a 200x200 pure-blue PNG used by image-format probes."""
    width = 200
    height = 200
    raw = b"".join(b"\x00" + (b"\x00\x00\xff" * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, level=9))
        + _png_chunk(b"IEND", b"")
    )


def prepared_blue_png_payload() -> tuple[str, dict[str, Any]]:
    """Prepare the standard blue PNG through the production media pipeline."""
    from hermes_infoflow.media import prepare_infoflow_image_bytes

    prepared = prepare_infoflow_image_bytes(solid_blue_png_200())
    encoded = base64.b64encode(prepared.data).decode("ascii")
    return encoded, {
        "source": "generated solid blue PNG",
        "width": 200,
        "height": 200,
        "prepared_mime": prepared.mime_type,
        "prepared_bytes": len(prepared.data),
        "prepared_base64_length": len(encoded),
    }


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "content" and isinstance(item, str) and len(item) > 300:
                out[key] = f"<base64:{len(item)} chars>"
            else:
                out[key] = redact_payload(item)
        return out
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


def result_summary(result: Any) -> dict[str, Any]:
    return {
        "success": bool(getattr(result, "success", False)),
        "message_id": str(getattr(result, "message_id", "") or ""),
        "msgseqid": str(getattr(result, "msgseqid", "") or ""),
        "error": str(getattr(result, "error", "") or ""),
        "raw": getattr(result, "raw_response", None),
    }


def _extract_raw_payload(line: str) -> dict[str, Any] | None:
    match = re.search(r"payload=(\{.*\})", line) or re.search(
        r"raw_payload=(\{.*\})", line
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except Exception:
        return None


def extract_group_echo(message_id: str, *, log_tail: int = 30000) -> list[dict[str, Any]]:
    """Return webhook echo summaries matching a group message id."""
    mid = str(message_id or "")
    if not mid:
        return []
    out: list[dict[str, Any]] = []
    for path in [
        Path.home() / ".hermes/logs/gateway.log",
        Path.home() / ".hermes/logs/agent.log",
    ]:
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines[-log_tail:]):
            if "[iflow:raw]" not in line:
                continue
            payload = _extract_raw_payload(line)
            if not payload:
                continue
            message = payload.get("message", {})
            header = message.get("header", {})
            if str(header.get("messageid") or "") != mid:
                continue
            body = message.get("body", [])
            out.append(_summarize_echo_payload(path, payload, body, header))
            break
    return out


def _summarize_echo_payload(
    path: Path,
    payload: dict[str, Any],
    body: Any,
    header: dict[str, Any],
) -> dict[str, Any]:
    body_items = body if isinstance(body, list) else []
    return {
        "log": str(path),
        "eventtype": payload.get("eventtype"),
        "header_msgtype": header.get("msgtype"),
        "header_at": header.get("at"),
        "compatible": header.get("compatible"),
        "offlinenotify": header.get("offlinenotify"),
        "body_types": [
            item.get("type") for item in body_items if isinstance(item, dict)
        ],
        "body_at_fields": [
            {
                key: value
                for key, value in item.items()
                if key.startswith("at") or key in ("userid", "robotid", "name")
            }
            for item in body_items
            if isinstance(item, dict) and item.get("type") == "AT"
        ],
        "link_items": [
            dict(item)
            for item in body_items
            if isinstance(item, dict) and str(item.get("type")) == "LINK"
        ],
        "has_replyData": any(
            str(item.get("type")) == "replyData"
            for item in body_items
            if isinstance(item, dict)
        ),
        "has_image": any(
            str(item.get("type")) == "IMAGE"
            for item in body_items
            if isinstance(item, dict)
        ),
        "body_preview": [
            str(
                item.get("content")
                or item.get("label")
                or item.get("href")
                or item.get("name")
                or item.get("downloadurl")
                or ""
            )[:240]
            for item in body_items
            if isinstance(item, dict)
        ],
    }


async def send_group_case(
    api: Any,
    cases: list[dict[str, Any]],
    *,
    group_id: str,
    name: str,
    msgtype: str,
    body: list[dict[str, Any]],
    expected: str,
    reply_to: str = "",
    reply_preview: str = "",
    delay_seconds: float = 0.45,
) -> str:
    reply_target = None
    if reply_to:
        reply_target = {
            "message_id": str(reply_to),
            "preview": reply_preview or f"format probe reply {marker()}",
        }
    result = await api.send_group_structured(
        group_id,
        body=body,
        msgtype=msgtype,
        reply_to=[reply_target] if reply_target else None,
    )
    summary = result_summary(result)
    cases.append(
        {
            "name": name,
            "expected": expected,
            "request": {
                "msgtype": msgtype,
                "body": redact_payload(body),
                "reply": bool(reply_target),
            },
            "send_result": summary,
        }
    )
    if delay_seconds > 0:
        import asyncio

        await asyncio.sleep(delay_seconds)
    return summary["message_id"]


async def attach_group_echoes(
    cases: list[dict[str, Any]],
    *,
    wait_seconds: float,
    log_tail: int = 30000,
) -> None:
    if wait_seconds > 0:
        import asyncio

        await asyncio.sleep(wait_seconds)
    for case in cases:
        mid = case.get("send_result", {}).get("message_id", "")
        case["echo"] = extract_group_echo(str(mid), log_tail=log_tail) if mid else []


async def send_private_case(
    api: Any,
    cases: list[dict[str, Any]],
    *,
    user_id: str,
    name: str,
    expected: str,
    text: str | None = None,
    richtext_content: list[dict[str, str]] | None = None,
    image_bytes: bytes | None = None,
    reply_targets: list[dict[str, str]] | None = None,
    delay_seconds: float = 0.25,
) -> str:
    result = await api.send_private_structured(
        user_id,
        text=text,
        richtext_content=richtext_content,
        image_bytes=image_bytes,
        reply_to=reply_targets or [],
    )
    summary = result_summary(result)
    request: dict[str, Any] = {
        "text": text,
        "richtext_content": richtext_content,
        "has_image_bytes": image_bytes is not None,
        "reply_targets": reply_targets or [],
    }
    cases.append(
        {
            "name": name,
            "expected": expected,
            "request": redact_payload(request),
            "send_result": summary,
            "manual_validation_required": True,
        }
    )
    if delay_seconds > 0:
        import asyncio

        await asyncio.sleep(delay_seconds)
    return summary["message_id"]


async def send_private_payload_case(
    account: Any,
    cases: list[dict[str, Any]],
    *,
    name: str,
    payload: dict[str, Any],
    expected: str,
    delay_seconds: float = 0.25,
) -> str:
    """Send a raw private app-message payload and append a case summary."""
    from hermes_infoflow import api as _api

    result = await _api.send_private_payload(account, payload)
    message_id = str(result.get("messageid") or result.get("msgkey") or "")
    cases.append(
        {
            "name": name,
            "expected": expected,
            "request": redact_payload(payload),
            "send_result": {
                "success": bool(result.get("ok")),
                "message_id": message_id,
                "error": str(result.get("error") or ""),
                "raw": result,
            },
            "manual_validation_required": True,
        }
    )
    if delay_seconds > 0:
        import asyncio

        await asyncio.sleep(delay_seconds)
    return message_id
