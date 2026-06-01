"""Smoke-test file_delivery/file_to_url and send-format routing against real Infoflow.

This script is for validating changes like:

* Non-image local files are published to BOS and sent as clickable links.
* ``auto/markdown + image_paths/image_bytes`` publishes the image to BOS and
  sends Markdown image syntax.
* ``format=text + image_paths`` keeps the native IMAGE packet.
* plain ``auto + image_paths`` keeps the native IMAGE packet when no Markdown
  is needed.
* ``reply_to + Markdown + image_paths`` keeps reply and Markdown by sending
  the wire-compatible packet sequence.

It sends real messages to Infoflow and prints the exact high-level payload
families that ``ServerAPI`` chose. By default it imports the in-tree source.
Pass ``--runtime-plugin`` to test the deployed plugin under ``~/.hermes/plugins``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from _env import HERMES_HOME, bootstrap, required_env, test_group_id
from _message_format_probe import solid_blue_png_200


RUNTIME_PLUGINS = HERMES_HOME / "plugins"


def _payload_preview(body: list[dict[str, Any]]) -> str:
    pieces: list[str] = []
    for item in body:
        kind = str(item.get("type") or "")
        if kind in {"TEXT", "MD"}:
            pieces.append(str(item.get("content") or "")[:180])
        elif kind == "IMAGE":
            pieces.append("[IMAGE]")
        elif kind == "LINK":
            pieces.append(f"[LINK:{item.get('label') or item.get('href')}]")
        elif kind == "AT":
            pieces.append("[AT]")
    return " ".join(pieces)


def _summary(result: Any) -> dict[str, Any]:
    return {
        "success": bool(getattr(result, "success", False)),
        "message_id": str(getattr(result, "message_id", "") or ""),
        "msgseqid": str(getattr(result, "msgseqid", "") or ""),
        "continuation_message_ids": list(
            getattr(result, "continuation_message_ids", ()) or ()
        ),
        "sent_messages": [
            {
                "message_id": receipt.message_id,
                "kind": receipt.kind,
                "preview": receipt.preview,
            }
            for receipt in getattr(result, "sent_messages", ()) or ()
        ],
        "warnings": list(getattr(result, "warnings", ()) or ()),
        "error_code": str(getattr(result, "error_code", "") or ""),
        "error": str(getattr(result, "error", "") or ""),
    }


def _import_modules(*, runtime_plugin: bool):
    if runtime_plugin:
        sys.path.insert(0, str(RUNTIME_PLUGINS))
        package = "infoflow"
    else:
        package = "hermes_infoflow"
    return (
        importlib.import_module(f"{package}.api"),
        importlib.import_module(f"{package}.file_delivery"),
        importlib.import_module(f"{package}.serverapi"),
        importlib.import_module(f"{package}.settings"),
    )


async def _run(args: argparse.Namespace) -> int:
    info_api, file_delivery_mod, serverapi_mod, settings_mod = _import_modules(
        runtime_plugin=args.runtime_plugin
    )
    settings = settings_mod._read_account_settings(None)
    group_id = args.group or test_group_id()
    private_user = str(args.private_user or "").strip()
    marker = args.marker or time.strftime("%Y%m%d-%H%M%S")

    workdir = Path(args.workdir).expanduser() / marker
    workdir.mkdir(parents=True, exist_ok=True)
    image_path = workdir / f"file-to-url-{marker}.png"
    image_bytes = solid_blue_png_200()
    image_path.write_bytes(image_bytes)
    text_path = workdir / f"file-to-url-{marker}.txt"
    text_path.write_text(
        f"FILETOURL|{marker}|group-file-link\n"
        "This text file should be published by file_delivery and sent as a link.\n",
        encoding="utf-8",
    )

    original_group_send = info_api.send_group_payload
    original_private_send = info_api.send_private_payload
    original_upload = info_api.im_bos_upload
    original_get_url = info_api.im_bos_get_url
    original_head = info_api.im_bos_head_url

    observed: list[dict[str, Any]] = []

    async def wrapped_group_send(account, group_id, **kwargs):
        body = kwargs.get("body") or []
        item = {
            "payload": "group",
            "group_id": str(group_id),
            "msgtype": kwargs.get("msgtype"),
            "body_types": [entry.get("type") for entry in body],
            "has_reply": kwargs.get("reply_to") is not None,
            "preview": _payload_preview(body),
        }
        observed.append(item)
        print(json.dumps(item, ensure_ascii=False))
        return await original_group_send(account, group_id, **kwargs)

    async def wrapped_private_send(account, payload, session=None, timeout=info_api.DEFAULT_TIMEOUT_SECONDS):
        preview = ""
        if payload.get("msgtype") == "md":
            preview = str((payload.get("md") or {}).get("content") or "")[:180]
        elif payload.get("msgtype") == "text":
            preview = str((payload.get("text") or {}).get("content") or "")[:180]
        elif payload.get("msgtype") == "image":
            preview = "[IMAGE]"
        item = {
            "payload": "private",
            "user_id": payload.get("touser"),
            "msgtype": payload.get("msgtype"),
            "has_reply": bool(payload.get("reply")),
            "preview": preview,
        }
        observed.append(item)
        print(json.dumps(item, ensure_ascii=False))
        return await original_private_send(
            account,
            payload,
            session=session,
            timeout=timeout,
        )

    async def wrapped_upload(account, *, file_content, file_name, object_key=None, session=None, timeout=info_api.BOS_UPLOAD_TIMEOUT_SECONDS):
        item = {
            "payload": "bos_upload",
            "file_name": file_name,
            "size": len(file_content or b""),
            "object_key": object_key,
        }
        observed.append(item)
        print(json.dumps(item, ensure_ascii=False))
        return await original_upload(
            account,
            file_content=file_content,
            file_name=file_name,
            object_key=object_key,
            session=session,
            timeout=timeout,
        )

    async def wrapped_get_url(account, *, object_key, expiration_seconds=info_api.BOS_GET_URL_DEFAULT_EXPIRATION_SECONDS, session=None, timeout=info_api.BOS_GET_URL_TIMEOUT_SECONDS):
        item = {
            "payload": "bos_get_url",
            "object_key": object_key,
            "expiration_seconds": expiration_seconds,
        }
        observed.append(item)
        print(json.dumps(item, ensure_ascii=False))
        return await original_get_url(
            account,
            object_key=object_key,
            expiration_seconds=expiration_seconds,
            session=session,
            timeout=timeout,
        )

    async def wrapped_head(url, *, session=None, timeout=15.0):
        result = await original_head(url, session=session, timeout=timeout)
        item = {
            "payload": "bos_head",
            "ok": bool(getattr(result, "ok", False)),
            "status": int(getattr(result, "status", 0) or 0),
        }
        observed.append(item)
        print(json.dumps(item, ensure_ascii=False))
        return result

    info_api.send_group_payload = wrapped_group_send
    info_api.send_private_payload = wrapped_private_send
    info_api.im_bos_upload = wrapped_upload
    info_api.im_bos_get_url = wrapped_get_url
    info_api.im_bos_head_url = wrapped_head

    serverapi = serverapi_mod.ServerAPI(
        settings=settings,
        image_loader=lambda path: Path(path).expanduser().read_bytes(),
    )

    async def run_case(name: str, awaitable):
        result = await awaitable
        summary = {"case": name, **_summary(result)}
        print(json.dumps(summary, ensure_ascii=False))
        return summary

    cases: list[dict[str, Any]] = []
    if not args.skip_file_link:
        published = await file_delivery_mod.publish_file(serverapi, text_path)
        item = {
            "payload": "file_delivery",
            "source": str(text_path),
            "object_key": published.object_key,
            "size_bytes": published.size_bytes,
            "url_prefix": published.url[:120],
        }
        observed.append(item)
        print(json.dumps(item, ensure_ascii=False))
        cases.append(await run_case(
            "group-file-link",
            serverapi.send_group_message_intent(
                group_id,
                message=(
                    f"【FILETOURL|{marker}|group-file-link】 "
                    "非图片文件应发布为 URL，并显示为可点击文件链接。"
                ),
                links=[{"href": published.url, "label": text_path.name}],
            ),
        ))

    first = await run_case(
        "group-auto-markdown-image-path",
        serverapi.send_group_message_intent(
            group_id,
            message=(
                f"【FILETOURL|{marker}|group-auto-markdown-image-path】\n\n"
                "**Markdown 图文**\n\n- image_paths 应转成 Markdown 图片 URL"
            ),
            image_paths=[str(image_path)],
        ),
    )
    cases.append(first)

    cases.append(await run_case(
        "group-reply-markdown-image-path",
        serverapi.send_group_message_intent(
            group_id,
            message=(
                f"【FILETOURL|{marker}|group-reply-markdown-image-path】\n\n"
                "**Reply + 图文**\n\n- 引用挂第一条，正文仍 Markdown"
            ),
            image_paths=[str(image_path)],
            reply_to=[{"message_id": first["message_id"]}] if first["message_id"] else None,
        ),
    ))

    cases.append(await run_case(
        "group-text-native-image-path",
        serverapi.send_group_message_intent(
            group_id,
            message=(
                f"【FILETOURL|{marker}|group-text-native-image-path】 "
                "**这里应按纯文本展示**"
            ),
            format="text",
            image_paths=[str(image_path)],
        ),
    ))

    cases.append(await run_case(
        "group-auto-plain-native-image-path",
        serverapi.send_group_message_intent(
            group_id,
            message=(
                f"【FILETOURL|{marker}|group-auto-plain-native-image-path】 "
                "普通文字 + 本地图片应保持原生图片"
            ),
            image_paths=[str(image_path)],
        ),
    ))

    if private_user:
        cases.append(await run_case(
            "private-auto-markdown-image-bytes",
            serverapi.send_private_message_intent(
                private_user,
                message=(
                    f"【FILETOURL|{marker}|private-auto-markdown-image-bytes】\n\n"
                    "**私聊图文**\n\n- image_bytes 应转 Markdown 图片 URL"
                ),
                image_bytes=image_bytes,
            ),
        ))

    ok = all(case["success"] for case in cases)
    print(json.dumps({
        "ok": ok,
        "marker": marker,
        "runtime_plugin": bool(args.runtime_plugin),
        "group": group_id,
        "private_user": private_user,
        "image_path": str(image_path),
        "text_path": str(text_path),
        "cases": cases,
        "observed_count": len(observed),
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--group",
        default="",
        help=(
            "Override the configured test group. Defaults to "
            "INFOFLOW_REAL_TEST_GROUP, INFOFLOW_OP_GROUP, or numeric/group "
            "INFOFLOW_OP_CHANNEL."
        ),
    )
    parser.add_argument(
        "--private-user",
        default="",
        help="Optional uuapName for the private image_bytes case.",
    )
    parser.add_argument("--marker", default="", help="Marker shown in every message.")
    parser.add_argument(
        "--workdir",
        default="/private/tmp/infoflow-file-to-url-send-matrix",
        help="Directory for generated local image fixtures.",
    )
    parser.add_argument(
        "--runtime-plugin",
        action="store_true",
        help="Import the deployed plugin from ~/.hermes/plugins/infoflow.",
    )
    parser.add_argument(
        "--skip-file-link",
        action="store_true",
        help="Skip the non-image file_delivery + clickable link case.",
    )
    return parser


def main() -> int:
    bootstrap()
    required_env("INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET", "INFOFLOW_API_HOST")
    return asyncio.run(_run(_build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
