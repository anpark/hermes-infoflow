"""Shared utility functions for the Infoflow adapter.

Contains pure helpers for config parsing, local-path safety, inbound image
downloading, outbound URL safety (SSRF guard), and related exception types.
These are factored out of :mod:`hermes_infoflow.adapter` so they can be
tested independently and to keep the adapter lean.
"""

from __future__ import annotations

import ipaddress as _ipaddress
import json
import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .api import auth_headers

logger = logging.getLogger(__name__)


def gw_log() -> logging.Logger:
    """Return the gateway.run logger so audit lines reach gateway.log."""
    return logging.getLogger("gateway.run")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r (expected int)", name, raw)
        return default


def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


# ---------------------------------------------------------------------------
# Local file path safety
# ---------------------------------------------------------------------------


def _allowed_media_roots() -> list[Path]:
    """Directories we'll accept ``file://`` outbound images from."""
    hermes_home = Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes"))
    roots = [
        hermes_home / "cache" / "images",
        hermes_home / "cache" / "screenshots",
        hermes_home / "image_cache",
        hermes_home / "browser_screenshots",
        Path.home() / ".hermes" / "media",
        Path(tempfile.gettempdir()),
        Path("/tmp"),
    ]
    # De-duplicate, ignore non-existent.
    seen: set[str] = set()
    keep: list[Path] = []
    for r in roots:
        try:
            r_resolved = r.expanduser().resolve()
        except OSError:
            continue
        key = str(r_resolved)
        if key in seen:
            continue
        seen.add(key)
        keep.append(r_resolved)
    return keep


def _resolve_safe_local_path(raw: str) -> Path | None:
    """Resolve ``raw`` and return it iff inside one of the allowed roots.

    Prevents LLM-driven path traversal (e.g. ``file:///etc/passwd``) when
    the agent passes a local image URL to ``send_image``.
    """
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    try:
        candidate = Path(raw).expanduser().resolve()
    except OSError:
        return None
    if not candidate.is_file():
        return None
    for root in _allowed_media_roots():
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Webhook image downloader (with Bearer- fallback)
# ---------------------------------------------------------------------------


async def _download_inbound_image(
    url: str,
    *,
    token_provider,
    session: aiohttp.ClientSession | None = None,
    max_bytes: int = 25 * 1024 * 1024,
) -> tuple[bytes, str] | None:
    """Fetch ``url`` (with a Bearer- fallback) and return ``(bytes, ext)``.

    Infoflow IMAGE bodies' ``downloadurl`` is usually a short-lived signed
    link. Try a bare GET first; if we hit 401/403, retry once with the
    Infoflow app access token as the Authorization header.
    """

    async def _try(s: aiohttp.ClientSession, headers: dict[str, str] | None) -> tuple[bytes, str] | None:
        try:
            async with s.get(
                url,
                headers=headers or {},
                timeout=aiohttp.ClientTimeout(total=30.0),
            ) as resp:
                if resp.status in (401, 403):
                    return None
                if resp.status >= 400:
                    logger.warning("[infoflow:inbound image] HTTP %s for %s", resp.status, url[:100])
                    return None
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        logger.warning("[infoflow:inbound image] payload exceeds %s bytes; aborting", max_bytes)
                        return None
                data = bytes(buf)
                ext = _downloaded_image_ext(content_type, data)
                return data, ext
        except aiohttp.ClientError as exc:
            logger.debug("[infoflow:inbound image] GET failed: %s", exc)
            return None
        except TimeoutError:
            logger.debug("[infoflow:inbound image] GET timed out")
            return None

    own_session = session is None
    sess = session or aiohttp.ClientSession()
    try:
        result = await _try(sess, None)
        if result is None:
            try:
                token = await token_provider()
            except Exception:
                token = None
            if token:
                result = await _try(
                    sess,
                    auth_headers(token, content_type=None, include_logid=False),
                )
        return result
    finally:
        if own_session:
            await sess.close()


def _downloaded_image_ext(content_type: str, data: bytes) -> str:
    """Return a useful image extension for downloaded Infoflow image bytes.

    Infoflow's image proxy commonly returns real JPEG/PNG bytes as
    ``application/octet-stream``.  Prefer content sniffing so downstream media
    caching and MIME labels stay image-shaped even when the HTTP header is
    generic.
    """
    sniffed = _sniff_image_ext(data)
    if sniffed:
        return sniffed
    normalized = (content_type or "").split(";")[0].strip().lower()
    if normalized.startswith("image/"):
        ext = mimetypes.guess_extension(normalized)
        if ext:
            return ".jpg" if ext == ".jpe" else ext
    return ".jpg"


def _sniff_image_ext(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    return ""


def _image_download_urls_from_raw_json(raw_json: str) -> list[str]:
    """Extract downloadable image URLs from a stored Infoflow raw payload.

    Group ``MIXED`` IMAGE messages expose ``downloadurl`` under body items.
    Private image callbacks use the older flat ``MsgType=image`` + ``PicUrl``
    shape.  Both are valid inbound image messages and must be available to the
    deferred image tools by message_id/index.
    """
    raw = str(raw_json or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    urls: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        url = str(value or "").strip()
        if url and url not in seen:
            urls.append(url)
            seen.add(url)

    def _visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                _visit(item)
            return
        if not isinstance(value, dict):
            return
        item_type = str(value.get("type") or value.get("Type") or "").upper()
        if item_type == "IMAGE":
            _add(
                value.get("downloadurl")
                or value.get("download_url")
                or value.get("downloadUrl")
            )
        msg_type = str(
            value.get("MsgType")
            or value.get("msgtype")
            or value.get("msgType")
            or ""
        ).lower()
        if msg_type == "image":
            _add(
                value.get("PicUrl")
                or value.get("picurl")
                or value.get("pic_url")
                or value.get("picUrl")
                or value.get("downloadurl")
                or value.get("download_url")
                or value.get("downloadUrl")
            )
        for child in value.values():
            if isinstance(child, (dict, list)):
                _visit(child)

    _visit(payload)
    return urls


# ---------------------------------------------------------------------------
# Outbound URL safety (SSRF guard for send_image)
# ---------------------------------------------------------------------------

_DENIED_HOSTNAMES = frozenset({
    "localhost",
    "169.254.169.254",   # AWS / GCP metadata
    "metadata.google.internal",
    "metadata",
})


def _is_safe_outbound_url(url: str) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a candidate outbound URL.

    Restricts the agent's ``send_image`` to public http(s) targets. Blocks
    private/loopback/link-local IPs and known cloud metadata hostnames to
    prevent the LLM from being tricked into exfiltrating internal data
    via an "image" send.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"only http/https allowed, got {parsed.scheme!r}"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "URL has no hostname"
    if host in _DENIED_HOSTNAMES:
        return False, f"hostname {host!r} is on the deny list"
    # Reject IP literals that resolve to private / loopback / link-local space.
    try:
        ip = _ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved
    ):
        return False, f"IP {host} is in a non-public range"
    return True, ""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class _ImageLoadError(Exception):
    """Raised when ``send_image`` cannot load its source bytes."""
