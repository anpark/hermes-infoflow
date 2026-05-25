"""Shared utility functions for the Infoflow adapter.

Contains pure helpers for config parsing, local-path safety, inbound image
downloading, outbound URL safety (SSRF guard), and related exception types.
These are factored out of :mod:`hermes_infoflow.adapter` so they can be
tested independently and to keep the adapter lean.
"""

from __future__ import annotations

import ipaddress as _ipaddress
import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

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
                ext = mimetypes.guess_extension(content_type) or ".jpg"
                return bytes(buf), ext
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
                result = await _try(sess, {"Authorization": f"Bearer-{token}"})
        return result
    finally:
        if own_session:
            await sess.close()


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
