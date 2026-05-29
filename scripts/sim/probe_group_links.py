"""Probe supported group LINK body-item behavior against the real backend.

This script posts numbered messages to a test group through
``ServerAPI.send_group_structured()``, then correlates local Hermes webhook
echoes by message id. Exact-wire invalid LINK combinations live in
``probe_contract_edges.py`` so backend failures are not hidden by local
structured validation. It is intended for maintaining the message-format
contract documented in ``docs/infoflow-message-format.md``.
"""

from __future__ import annotations

import argparse
import asyncio

from _env import bootstrap, required_env
from _message_format_probe import (
    attach_group_echoes,
    marker,
    parse_int,
    prepared_blue_png_payload,
    print_json,
    require_group_id,
    require_test_user,
    send_group_case,
)


async def _run(args: argparse.Namespace) -> int:
    from hermes_infoflow.serverapi import ServerAPI
    from hermes_infoflow.settings import _read_account_settings

    api = ServerAPI(settings=_read_account_settings(None))
    run_marker = marker()
    url = f"https://example.com/infoflow-group-link-{run_marker}"
    cases: list[dict] = []

    image = ""
    image_meta = None
    if args.include_image:
        image, image_meta = prepared_blue_png_payload()

    base_mid = await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="link_base_text",
        msgtype="TEXT",
        body=[{"type": "TEXT", "content": f"【群聊 LINK 验证 BASE｜{run_marker}】被回复基准消息。"}],
        expected="Base TEXT message for reply probes.",
    )

    await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="link_href_only",
        msgtype="TEXT",
        body=[{"type": "LINK", "href": url + "/href-only"}],
        expected="TEXT + LINK(href only) should echo a native LINK item.",
    )
    await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="link_href_label",
        msgtype="TEXT",
        body=[{"type": "LINK", "href": url + "/href-label", "label": "G-LINK 自定义展示文本"}],
        expected="LINK(href + label) should send; echo exposes label only.",
    )
    await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="text_multi_links",
        msgtype="TEXT",
        body=[
            {"type": "TEXT", "content": f"【群聊 LINK 验证 G-LINK-MULTI｜{run_marker}】TEXT + 两个 LINK。"},
            {"type": "LINK", "href": url + "/multi-1"},
            {"type": "LINK", "href": url + "/multi-2", "label": "第二个自定义链接"},
        ],
        expected="Multiple LINK items in one TEXT packet should both echo.",
    )
    await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="at_link_only",
        msgtype="TEXT",
        body=[
            {"type": "AT", "atuserids": [args.user]},
            {"type": "LINK", "href": url + "/at-link-only"},
        ],
        expected="AT + LINK without TEXT should preserve native AT and LINK.",
    )
    await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="at_only_user",
        msgtype="TEXT",
        body=[
            {"type": "AT", "atuserids": [args.user]},
        ],
        expected="AT without TEXT or LINK should preserve native AT.",
    )
    await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="at_text_link",
        msgtype="TEXT",
        body=[
            {"type": "AT", "atuserids": [args.user]},
            {"type": "TEXT", "content": f"【群聊 LINK 验证 G-LINK-AT｜{run_marker}】AT + TEXT + LINK。"},
            {"type": "LINK", "href": url + "/at-text-link"},
        ],
        expected="AT + TEXT + LINK should preserve native AT and native LINK.",
    )
    if base_mid:
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="reply_link_only",
            msgtype="TEXT",
            body=[{"type": "LINK", "href": url + "/reply-link-only"}],
            expected="Reply + LINK without TEXT should preserve replyData and LINK.",
            reply_to=base_mid,
            reply_preview=f"codex link base {run_marker}",
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="reply_at_text_link",
            msgtype="TEXT",
            body=[
                {"type": "AT", "atuserids": [args.user]},
                {"type": "TEXT", "content": f"【群聊 LINK 验证 G-LINK-REPLY-AT｜{run_marker}】reply + AT + TEXT + LINK。"},
                {"type": "LINK", "href": url + "/reply-at-text-link"},
            ],
            expected="Reply + AT + TEXT + LINK should preserve replyData, AT, and LINK.",
            reply_to=base_mid,
            reply_preview=f"codex link base {run_marker}",
        )
    if args.include_image and image:
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="at_link_image_only",
            msgtype="IMAGE",
            body=[
                {"type": "AT", "atuserids": [args.user]},
                {"type": "LINK", "href": url + "/at-link-image-only"},
                {"type": "IMAGE", "content": image},
            ],
            expected="AT + LINK + IMAGE without TEXT should preserve native AT, LINK, and IMAGE.",
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="link_image_only",
            msgtype="IMAGE",
            body=[
                {"type": "LINK", "href": url + "/link-image-only"},
                {"type": "IMAGE", "content": image},
            ],
            expected="IMAGE packet can preserve LINK and IMAGE without TEXT.",
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="image_text_link_image",
            msgtype="IMAGE",
            body=[
                {"type": "TEXT", "content": f"【群聊 LINK 验证 G-LINK-IMAGE｜{run_marker}】TEXT + LINK + 200x200 蓝图。"},
                {"type": "LINK", "href": url + "/image-link"},
                {"type": "IMAGE", "content": image},
            ],
            expected="IMAGE packet can preserve TEXT, LINK, and IMAGE.",
        )
        if base_mid:
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="reply_at_text_link_image",
                msgtype="IMAGE",
                body=[
                    {"type": "AT", "atuserids": [args.user]},
                    {"type": "TEXT", "content": f"【群聊 LINK 验证 G-LINK-ALL-IN｜{run_marker}】reply + AT + TEXT + LINK + 200x200 蓝图。"},
                    {"type": "LINK", "href": url + "/reply-at-link-image"},
                    {"type": "IMAGE", "content": image},
                ],
                expected="Reply + AT + LINK + IMAGE should preserve all semantics.",
                reply_to=base_mid,
                reply_preview=f"codex link base {run_marker}",
            )
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="reply_at_link_image_only",
                msgtype="IMAGE",
                body=[
                    {"type": "AT", "atuserids": [args.user]},
                    {"type": "LINK", "href": url + "/reply-at-link-image-only"},
                    {"type": "IMAGE", "content": image},
                ],
                expected="Reply + AT + LINK + IMAGE without TEXT should preserve all semantics.",
                reply_to=base_mid,
                reply_preview=f"codex link base {run_marker}",
            )
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="reply_at_image_only",
                msgtype="IMAGE",
                body=[
                    {"type": "AT", "atuserids": [args.user]},
                    {"type": "IMAGE", "content": image},
                ],
                expected="Reply + AT + IMAGE without TEXT should preserve replyData, AT, and IMAGE.",
                reply_to=base_mid,
                reply_preview=f"codex link base {run_marker}",
            )

    if args.include_at_all:
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="atall_only",
            msgtype="TEXT",
            body=[
                {"type": "AT", "atall": True},
            ],
            expected="@all without TEXT should preserve native atall.",
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="atall_link_only",
            msgtype="TEXT",
            body=[
                {"type": "AT", "atall": True},
                {"type": "LINK", "href": url + "/atall-link-only"},
            ],
            expected="@all + LINK without TEXT should preserve native atall and LINK.",
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="atall_text_link",
            msgtype="TEXT",
            body=[
                {"type": "AT", "atall": True},
                {"type": "TEXT", "content": f"【群聊 LINK 验证 G-LINK-ATALL｜{run_marker}】@all + TEXT + LINK。"},
                {"type": "LINK", "href": url + "/atall-link"},
            ],
            expected="@all + LINK should preserve native atall and native LINK.",
        )
        if base_mid:
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="reply_atall_link_only",
                msgtype="TEXT",
                body=[
                    {"type": "AT", "atall": True},
                    {"type": "LINK", "href": url + "/reply-atall-link-only"},
                ],
                expected="Reply + @all + LINK without TEXT should preserve replyData, atall, and LINK.",
                reply_to=base_mid,
                reply_preview=f"codex link base {run_marker}",
            )
        if args.include_image and image:
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="atall_image_only",
                msgtype="IMAGE",
                body=[
                    {"type": "AT", "atall": True},
                    {"type": "IMAGE", "content": image},
                ],
                expected="@all + IMAGE without TEXT should preserve native atall and IMAGE.",
            )
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="atall_link_image_only",
                msgtype="IMAGE",
                body=[
                    {"type": "AT", "atall": True},
                    {"type": "LINK", "href": url + "/atall-link-image-only"},
                    {"type": "IMAGE", "content": image},
                ],
                expected="@all + LINK + IMAGE without TEXT should preserve all semantics.",
            )
            if base_mid:
                await send_group_case(
                    api,
                    cases,
                    group_id=args.group,
                    name="reply_atall_link_image_only",
                    msgtype="IMAGE",
                    body=[
                        {"type": "AT", "atall": True},
                        {"type": "LINK", "href": url + "/reply-atall-link-image-only"},
                        {"type": "IMAGE", "content": image},
                    ],
                    expected=(
                        "Reply + @all + LINK + IMAGE without TEXT should preserve "
                        "replyData, atall, LINK, and IMAGE."
                    ),
                    reply_to=base_mid,
                    reply_preview=f"codex link base {run_marker}",
                )

    await attach_group_echoes(cases, wait_seconds=args.wait_seconds)
    print_json(
        {
            "group": args.group,
            "user": args.user,
            "marker": run_marker,
            "image": image_meta,
            "cases": cases,
        }
    )
    failed = [case for case in cases if not case["send_result"]["success"]]
    return 1 if failed else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default=None, help="Override INFOFLOW_OP_GROUP.")
    parser.add_argument("--user", default=None, help="uuapName used for AT probes.")
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=12.0,
        help="Seconds to wait before reading local webhook echo logs.",
    )
    parser.add_argument(
        "--include-image",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include 200x200 blue PNG + LINK combination probes.",
    )
    parser.add_argument(
        "--include-at-all",
        action="store_true",
        help="Also send an @all + LINK probe to the target group.",
    )
    return parser


def main() -> int:
    bootstrap()
    required_env("INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET")
    args = _build_parser().parse_args()
    args.group = require_group_id(args.group)
    args.user = require_test_user(args.user)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
