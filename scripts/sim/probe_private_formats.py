"""Probe private app-message formats against the real Infoflow backend.

Private messages do not produce a local webhook echo for bot self-sends, so
this script embeds Chinese case numbers and expected display text in each
message. The recipient should confirm the visible client behavior by case id.
"""

from __future__ import annotations

import argparse
import asyncio

from _env import bootstrap, required_env
from _message_format_probe import (
    marker,
    print_json,
    require_test_user,
    send_private_case,
    send_private_payload_case,
    solid_blue_png_200,
)


async def _run(args: argparse.Namespace) -> int:
    from hermes_infoflow.serverapi import ServerAPI
    from hermes_infoflow.settings import _read_account_settings

    settings = _read_account_settings(None)
    api = ServerAPI(settings=settings)
    api_account = api._api_account
    agent_id = str(settings.get("app_agent_id") or "")
    run_marker = marker()
    url = f"https://example.com/infoflow-private-format-{run_marker}"
    image_bytes = solid_blue_png_200()
    cases: list[dict] = []

    p01 = await send_private_case(
        api,
        cases,
        user_id=args.user,
        name="P01_text_plain",
        text=(
            f"【私聊格式验收 P01｜{run_marker}】纯 text。"
            "期望：Markdown 标记 **P01 粗体标记** 按普通文本展示。"
        ),
        expected="Plain text should not render Markdown.",
    )
    await send_private_payload_case(
        api_account,
        cases,
        name="P02_md_plain",
        payload={
            "touser": args.user,
            "toparty": "",
            "totag": "",
            "agentid": agent_id,
            "msgtype": "md",
            "md": {
                "content": (
                    f"【私聊格式验收 P02｜{run_marker}】Markdown。"
                    "\n\n**期望：这一段加粗**\n\n- 列表项正常渲染"
                )
            },
        },
        expected="Private md payload should render Markdown.",
    )
    await send_private_case(
        api,
        cases,
        user_id=args.user,
        name="P03_richtext_text_link",
        richtext_content=[
            {"type": "text", "text": f"【私聊格式验收 P03｜{run_marker}】richtext text + link。期望：后面的链接可点击："},
            {"type": "a", "href": url + "/p03", "label": "P03 示例链接"},
        ],
        expected="Richtext text + link should show text and a clickable link.",
    )
    await send_private_case(
        api,
        cases,
        user_id=args.user,
        name="P04_DESC_image_plain",
        text=(
            f"【私聊格式验收 P04-DESC｜{run_marker}】下一条 P04-IMG 是 200x200 纯蓝 PNG。"
            "期望：不是 1x1，不是破图。"
        ),
        expected="Description for the following image-only message.",
    )
    if args.include_image:
        await send_private_case(
            api,
            cases,
            user_id=args.user,
            name="P04_IMG_image_plain",
            image_bytes=image_bytes,
            expected="200x200 pure-blue PNG should display.",
        )

    reply_target = (
        [{"message_id": p01, "preview": f"private P01 base {run_marker}"}]
        if p01
        else []
    )
    await send_private_case(
        api,
        cases,
        user_id=args.user,
        name="P05_text_reply",
        text=f"【私聊格式验收 P05｜{run_marker}】text + reply。期望：显示 P01 引用/回复结构。",
        reply_targets=reply_target,
        expected="Text + reply should show a quote/reply card.",
    )
    p06_payload = {
        "touser": args.user,
        "toparty": "",
        "totag": "",
        "agentid": agent_id,
        "msgtype": "md",
        "md": {
            "content": (
                f"【私聊格式验收 P06｜{run_marker}】md + reply 语义探测。"
                "\n\n**期望：Markdown 会渲染，但客户端不展示 reply 引用。**"
            )
        },
    }
    if p01:
        p06_payload["reply"] = [
            {
                "content": f"private P01 base {run_marker}",
                "uid": "0",
                "msgid": p01,
            }
        ]
    await send_private_payload_case(
        api_account,
        cases,
        name="P06_md_reply_semantic_probe",
        payload=p06_payload,
        expected="Known semantic failure: API accepts md + reply, Markdown renders, but reply is not shown.",
    )
    if args.include_image:
        await send_private_case(
            api,
            cases,
            user_id=args.user,
            name="P07_DESC_image_reply",
            text=(
                f"【私聊格式验收 P07-DESC｜{run_marker}】下一条 P07-IMG 是 200x200 纯蓝 PNG + reply。"
                "期望：图片和 P01 引用/回复结构都展示。"
            ),
            expected="Description for the following image + reply message.",
        )
        await send_private_case(
            api,
            cases,
            user_id=args.user,
            name="P07_IMG_image_reply",
            image_bytes=image_bytes,
            reply_targets=reply_target,
            expected="Image + reply should display both the quote/reply card and image.",
        )
    p08_base = await send_private_case(
        api,
        cases,
        user_id=args.user,
        name="P08_BASE_richtext_reply",
        text=f"【私聊格式验收 P08-BASE｜{run_marker}】richtext + reply 的被回复基准消息。",
        expected="Base text message for P08 richtext reply.",
    )
    await send_private_case(
        api,
        cases,
        user_id=args.user,
        name="P08_RICH_richtext_reply",
        richtext_content=[
            {"type": "text", "text": f"【私聊格式验收 P08-RICH｜{run_marker}】richtext + reply。期望：显示 P08-BASE 引用，且链接可点击："},
            {"type": "a", "href": url + "/p08-richtext-reply", "label": "P08 示例链接"},
        ],
        reply_targets=(
            [{"message_id": p08_base, "preview": f"P08 base private {run_marker}"}]
            if p08_base
            else []
        ),
        expected="Richtext + reply should show quote/reply card and clickable link.",
    )
    p09_label = f"【私聊格式验收 P09｜{run_marker}】纯 links 无 text。期望：整条消息是一条可点击超链。"
    await send_private_case(
        api,
        cases,
        user_id=args.user,
        name="P09_RICH_link_only",
        richtext_content=[
            {"type": "a", "href": url + "/p09-link-only", "label": p09_label}
        ],
        expected="Confirmed on 2026-05-28: the whole message displays as one clickable link.",
    )
    p10_label = f"【私聊格式验收 P10｜{run_marker}】纯 links + reply。期望：显示引用，引用后的整行文字都是可点击超链。"
    await send_private_case(
        api,
        cases,
        user_id=args.user,
        name="P10_RICH_link_only_reply",
        richtext_content=[
            {"type": "a", "href": url + "/p10-link-only-reply", "label": p10_label}
        ],
        reply_targets=(
            [{"message_id": p08_base, "preview": f"P08 base private {run_marker}"}]
            if p08_base
            else reply_target
        ),
        expected="Confirmed on 2026-05-28: quote/reply is shown and the following row is a clickable link.",
    )

    print_json(
        {
            "user": args.user,
            "marker": run_marker,
            "image": {
                "source": "generated solid blue PNG",
                "width": 200,
                "height": 200,
                "raw_bytes": len(image_bytes),
            },
            "manual_validation_note": (
                "Private self-sends have no local echo. Ask the recipient to confirm "
                "client display by Pxx case id."
            ),
            "cases": cases,
        }
    )
    return 0 if all(case["send_result"]["success"] for case in cases) else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=None, help="Private recipient uuapName.")
    parser.add_argument(
        "--include-image",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include 200x200 blue PNG image probes.",
    )
    return parser


def main() -> int:
    bootstrap()
    required_env("INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET")
    args = _build_parser().parse_args()
    args.user = require_test_user(args.user)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
