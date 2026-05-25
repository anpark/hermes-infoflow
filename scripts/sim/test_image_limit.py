"""Probe Infoflow native image payload limits with real DM/group sends.

This script intentionally bypasses hermes_infoflow.serverapi image compression
and calls the native Infoflow image payload builders directly. It helps answer
whether Infoflow's limit is enforced on image bytes or on base64 JSON payload
size.

Usage:
    python scripts/sim/test_image_limit.py --dm chengbo05 --group 4507088
    python scripts/sim/test_image_limit.py --sizes 700K,760K,900K,1024K,1100K
    python scripts/sim/test_image_limit.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import struct
import time
import zlib

from _env import bootstrap, required_env, test_group_id

PNG_SIG = b"\x89PNG\r\n\x1a\n"
DEFAULT_SIZES = "700K,760K,900K,1024K,1100K"


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _base_png() -> bytes:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw_scanline = b"\x00\xff\x00\x00"
    return (
        PNG_SIG
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw_scanline))
        + _png_chunk(b"IEND", b"")
    )


def _png_with_target_size(target_size: int) -> bytes:
    base = _base_png()
    prefix = b"Comment\x00"
    overhead = 12
    if target_size <= len(base) + overhead + len(prefix):
        return base

    before_iend = base[:-12]
    iend = base[-12:]
    payload_len = target_size - len(base) - overhead
    filler = prefix + (b"x" * (payload_len - len(prefix)))
    return before_iend + _png_chunk(b"tEXt", filler) + iend


def _parse_size(raw: str) -> int:
    text = raw.strip().lower()
    if not text:
        raise ValueError("empty size")
    if text.endswith("kib"):
        return int(float(text[:-3]) * 1024)
    if text.endswith("kb") or text.endswith("k"):
        suffix = 2 if text.endswith("kb") else 1
        return int(float(text[:-suffix]) * 1024)
    if text.endswith("mib"):
        return int(float(text[:-3]) * 1024 * 1024)
    if text.endswith("mb") or text.endswith("m"):
        suffix = 2 if text.endswith("mb") else 1
        return int(float(text[:-suffix]) * 1024 * 1024)
    return int(text)


async def _send_dm(account, user: str, b64: str, session):
    from hermes_infoflow import api as _api

    return await _api.send_private_message(
        account,
        to_user=user,
        contents=[_api.ContentItem("image", b64)],
        session=session,
    )


async def _send_group(account, group_id: str, b64: str, session):
    from hermes_infoflow import api as _api

    return await _api.send_group_message(
        account,
        group_id=int(group_id),
        contents=[_api.ContentItem("image", b64)],
        session=session,
    )


async def _run(args: argparse.Namespace) -> int:
    import aiohttp

    from hermes_infoflow import api as _api
    from hermes_infoflow.settings import _read_account_settings

    settings = _read_account_settings(None)
    account = _api.InfoflowAccountAPI(
        api_host=str(settings["api_host"]),
        app_key=str(settings["app_key"]),
        app_secret=str(settings["app_secret"]),
        app_agent_id=settings.get("app_agent_id"),
    )

    targets: list[tuple[str, str]] = []
    if args.dm:
        targets.append(("dm", args.dm))
    if args.group:
        targets.append(("group", args.group))
    if not targets:
        raise SystemExit("provide --dm and/or --group, or set INFOFLOW_ADMIN_USER/INFOFLOW_TEST_GROUP_ID")

    sizes = [_parse_size(part) for part in args.sizes.split(",") if part.strip()]
    marker = time.strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"[sim:image-limit] marker={marker} sizes={sizes} "
        f"targets={targets} dry_run={args.dry_run}"
    )

    async with aiohttp.ClientSession() as session:
        for size in sizes:
            data = _png_with_target_size(size)
            b64 = base64.b64encode(data).decode("ascii")
            print(
                f"[sim:image-limit] prepared binary_bytes={len(data)} "
                f"base64_chars={len(b64)}"
            )
            if args.dry_run:
                continue

            for kind, target in targets:
                try:
                    if kind == "dm":
                        res = await _send_dm(account, target, b64, session)
                    else:
                        res = await _send_group(account, target, b64, session)
                except Exception as exc:
                    res = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
                print("[sim:image-limit] result:", json.dumps({
                    "target": f"{kind}:{target}",
                    "binary_bytes": len(data),
                    "base64_chars": len(b64),
                    "ok": bool(res.get("ok")),
                    "message_id": res.get("messageid") or res.get("msgkey"),
                    "error": res.get("error"),
                }, ensure_ascii=False))

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dm", default="", help="DM uuapName. Defaults to INFOFLOW_ADMIN_USER if set.")
    parser.add_argument("--group", default="", help="Group id. Defaults to INFOFLOW_TEST_GROUP_ID if set.")
    parser.add_argument("--sizes", default=DEFAULT_SIZES, help="Comma-separated binary sizes, e.g. 760K,1M.")
    parser.add_argument("--dry-run", action="store_true", help="Generate payload sizes without sending.")
    return parser


def main() -> int:
    status = bootstrap()
    print(f"[sim] bootstrap: {json.dumps(status, ensure_ascii=False)}")
    required_env("INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET")

    parser = _build_parser()
    args = parser.parse_args()
    if not args.dm:
        import os
        args.dm = os.environ.get("INFOFLOW_ADMIN_USER", "").strip()
    if not args.group:
        try:
            args.group = test_group_id()
        except SystemExit:
            args.group = ""
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
