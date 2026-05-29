"""Smoke-test ServerAPI intent sends against the real Infoflow service.

This script verifies the high-level path used by tool/Bot/adapter after the
send-message refactor:

    ServerAPI.send_group_message_intent(...)
    ServerAPI.send_private_message_intent(...)

It sends numbered Chinese messages with explicit expectations so private
message display can be checked manually when there is no echo event.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from _env import bootstrap, required_env, test_group_id
from _message_format_probe import solid_blue_png_200


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


async def _run(args: argparse.Namespace) -> int:
    from hermes_infoflow.serverapi import ServerAPI
    from hermes_infoflow.settings import _read_account_settings

    settings = _read_account_settings(None)
    api = ServerAPI(settings=settings)
    group_id = args.group or test_group_id()
    private_user = args.private_user
    marker = args.marker or time.strftime("%Y%m%d-%H%M%S")
    blue_png = solid_blue_png_200()
    url = "https://example.com/infoflow-intent-smoke"

    cases: list[dict[str, Any]] = []

    g01 = await api.send_group_message_intent(
        group_id,
        message=(
            f"【intent smoke G01｜{marker}】普通群聊 Markdown。"
            "期望：粗体 Markdown 渲染，msgtype=MD。**MD OK**"
        ),
    )
    cases.append({"name": "G01_group_markdown", "result": _summary(g01)})

    g02 = await api.send_group_message_intent(
        group_id,
        message=(
            f"【intent smoke G02｜{marker}】群聊 reply + link + @all + @chengbo05 + 200x200 蓝图。"
            "期望：展示 G01 引用、可点击链接、@全员和 chengbo05 原生提醒、图片为 200x200 纯蓝。"
        ),
        reply_to=[{"message_id": g01.message_id}] if g01.message_id else None,
        links=[{"href": url + "/g02", "label": "G02 示例链接"}],
        image_bytes=blue_png,
        at_all=True,
        mention_user_ids=["chengbo05"],
    )
    cases.append({"name": "G02_group_reply_link_at_image", "result": _summary(g02)})

    if private_user:
        p01 = await api.send_private_message_intent(
            private_user,
            message=(
                f"【intent smoke P01｜{marker}】私聊普通 Markdown。"
                "期望：粗体 Markdown 渲染。**MD OK**"
            ),
        )
        cases.append({"name": "P01_private_markdown", "result": _summary(p01)})

        p02 = await api.send_private_message_intent(
            private_user,
            message=(
                f"【intent smoke P02｜{marker}】私聊 richtext + reply + image。"
                "期望：第一条显示 P01 引用和可点击链接；第二条显示 200x200 纯蓝图片。"
            ),
            reply_to=[{"message_id": p01.message_id}] if p01.message_id else None,
            links=[{"href": url + "/p02", "label": "P02 示例链接"}],
            image_bytes=blue_png,
        )
        cases.append({"name": "P02_private_reply_link_image", "result": _summary(p02)})

    print(json.dumps({"marker": marker, "group": group_id, "cases": cases}, ensure_ascii=False, indent=2))
    return 0 if all(case["result"]["success"] for case in cases) else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="", help="Override INFOFLOW_OP_GROUP.")
    parser.add_argument("--private-user", default="", help="Optional uuapName for private smoke.")
    parser.add_argument("--marker", default="", help="Marker shown in every message.")
    return parser


def main() -> int:
    bootstrap()
    required_env("INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET", "INFOFLOW_API_HOST")
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
