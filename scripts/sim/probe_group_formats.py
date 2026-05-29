"""Probe supported group message formats against the real Infoflow backend.

The script sends the compact matrix that is valid through
``ServerAPI.send_group_structured()``: MD/TEXT packets, AT placeholders, reply
behavior, and IMAGE packets. Exact-wire invalid combinations live in
``probe_contract_edges.py`` so backend failures are not hidden by local
structured validation. Pure IMAGE is kept as an API-acceptance probe because
local raw echo may be absent.
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
    image, image_meta = prepared_blue_png_payload()
    cases: list[dict] = []

    base_mid = await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="md_plain",
        msgtype="MD",
        body=[{"type": "MD", "content": f"【群聊格式验证 MD｜{run_marker}】**Markdown**\n\n- item"}],
        expected="MD header + MD body should render Markdown and echo body type MD.",
    )
    await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="text_plain",
        msgtype="TEXT",
        body=[{"type": "TEXT", "content": f"【群聊格式验证 TEXT｜{run_marker}】**literal**"}],
        expected="TEXT header + TEXT body should display Markdown syntax literally.",
    )
    await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="text_at_struct",
        msgtype="TEXT",
        body=[
            {"type": "AT", "atuserids": [args.user]},
            {"type": "TEXT", "content": f"【群聊格式验证 TEXT-AT｜{run_marker}】TEXT + AT。"},
        ],
        expected="TEXT + AT should preserve native AT without inline placeholder.",
    )
    await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="md_at_uid_placeholder",
        msgtype="MD",
        body=[
            {"type": "AT", "atuserids": [args.user]},
            {"type": "MD", "content": f"@{args.user} 【群聊格式验证 MD-AT｜{run_marker}】**Markdown + AT**"},
        ],
        expected="MD + AT with @uuap placeholder should preserve native AT.",
    )
    await send_group_case(
        api,
        cases,
        group_id=args.group,
        name="md_at_no_placeholder",
        msgtype="MD",
        body=[
            {"type": "AT", "atuserids": [args.user]},
            {"type": "MD", "content": f"【群聊格式验证 MD-AT-MISS｜{run_marker}】**missing placeholder**"},
        ],
        expected="Semantic failure probe: send succeeds but echo should lose native AT.",
    )
    if args.agent_id is not None:
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="text_at_user_agent_combined",
            msgtype="TEXT",
            body=[
                {"type": "AT", "atuserids": [args.user], "atagentids": [args.agent_id]},
                {"type": "TEXT", "content": f"【群聊格式验证 TEXT-AT-MULTI｜{run_marker}】TEXT + 人类/机器人 AT 合并 item。"},
            ],
            expected="TEXT + one AT item containing atuserids and atagentids should preserve both native ATs.",
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="md_atagent_placeholder",
            msgtype="MD",
            body=[
                {"type": "AT", "atagentids": [args.agent_id]},
                {"type": "MD", "content": f"@{args.agent_id} 【群聊格式验证 MD-agent-AT｜{run_marker}】**agent AT**"},
            ],
            expected="MD + robot AT with @agentId placeholder should preserve native AT.",
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="md_at_user_agent_combined",
            msgtype="MD",
            body=[
                {"type": "AT", "atuserids": [args.user], "atagentids": [args.agent_id]},
                {
                    "type": "MD",
                    "content": (
                        f"@{args.user} @{args.agent_id} "
                        f"【群聊格式验证 MD-AT-MULTI｜{run_marker}】**人类/机器人 AT 合并 item**"
                    ),
                },
            ],
            expected="MD + one AT item containing user and agent ids should preserve both native ATs when both placeholders exist.",
        )
    if args.include_at_all:
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="md_atall_placeholder",
            msgtype="MD",
            body=[
                {"type": "AT", "atall": True},
                {"type": "MD", "content": f"@all 【群聊格式验证 MD-atall｜{run_marker}】**at all**"},
            ],
            expected="MD + atall with @all placeholder should preserve native atall.",
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="text_atall_user_combined",
            msgtype="TEXT",
            body=[
                {"type": "AT", "atall": True, "atuserids": [args.user]},
                {"type": "TEXT", "content": f"【群聊格式验证 TEXT-ATALL-USER｜{run_marker}】@all + 人类 AT 合并 item。"},
            ],
            expected=(
                "Semantic failure probe: one AT item containing atall and "
                "atuserids sends but should lose the specific user AT."
            ),
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="text_atall_user_separate",
            msgtype="TEXT",
            body=[
                {"type": "AT", "atall": True},
                {"type": "AT", "atuserids": [args.user]},
                {"type": "TEXT", "content": f"【群聊格式验证 TEXT-ATALL-USER-SPLIT｜{run_marker}】@all + 人类 AT 分开 item。"},
            ],
            expected="TEXT + separate AT items for atall and atuserids should preserve both native mentions.",
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="md_atall_user_combined",
            msgtype="MD",
            body=[
                {"type": "AT", "atall": True, "atuserids": [args.user]},
                {
                    "type": "MD",
                    "content": (
                        f"@all @{args.user} "
                        f"【群聊格式验证 MD-ATALL-USER｜{run_marker}】**@all + 人类 AT 合并 item**"
                    ),
                },
            ],
            expected=(
                "MD with atall and a specific user in one AT item should keep "
                "Markdown and native atall; the specific user stays visible "
                "as plain MD text, not native AT."
            ),
        )
        if args.agent_id is not None:
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="text_atall_agent_combined",
                msgtype="TEXT",
                body=[
                    {"type": "AT", "atall": True, "atagentids": [args.agent_id]},
                    {
                        "type": "TEXT",
                        "content": f"【群聊格式验证 TEXT-ATALL-AGENT｜{run_marker}】@all + 机器人 AT 合并 item。",
                    },
                ],
                expected=(
                    "Semantic failure probe: one AT item containing atall and "
                    "atagentids sends but should lose the robot AT."
                ),
            )
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="text_atall_agent_separate",
                msgtype="TEXT",
                body=[
                    {"type": "AT", "atall": True},
                    {"type": "AT", "atagentids": [args.agent_id]},
                    {
                        "type": "TEXT",
                        "content": f"【群聊格式验证 TEXT-ATALL-AGENT-SPLIT｜{run_marker}】@all + 机器人 AT 分开 item。",
                    },
                ],
                expected=(
                    "TEXT with separate AT items for atall and atagentids should "
                    "preserve both native mentions."
                ),
            )
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="md_atall_agent_combined",
                msgtype="MD",
                body=[
                    {"type": "AT", "atall": True, "atagentids": [args.agent_id]},
                    {
                        "type": "MD",
                        "content": (
                            f"@all @{args.agent_id} "
                            f"【群聊格式验证 MD-ATALL-AGENT｜{run_marker}】**@all + 机器人 AT 合并 item**"
                        ),
                    },
                ],
                expected=(
                    "MD with atall and a specific robot in one AT item should "
                    "keep Markdown and native atall; the robot token stays "
                    "visible as plain MD text, not native AT."
                ),
            )
    if base_mid:
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="reply_text",
            msgtype="TEXT",
            body=[{"type": "TEXT", "content": f"【群聊格式验证 reply-TEXT｜{run_marker}】reply + TEXT。"}],
            expected="Reply + TEXT should echo replyData.",
            reply_to=base_mid,
            reply_preview=f"group format base {run_marker}",
        )

    if args.include_image:
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="image_text_image",
            msgtype="IMAGE",
            body=[
                {"type": "TEXT", "content": f"【群聊格式验证 IMAGE｜{run_marker}】TEXT + 200x200 蓝图。"},
                {"type": "IMAGE", "content": image},
            ],
            expected="IMAGE packet with TEXT + IMAGE should preserve both.",
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="image_only",
            msgtype="IMAGE",
            body=[
                {"type": "IMAGE", "content": image},
            ],
            expected=(
                "Pure IMAGE packet should be API-accepted; local webhook echo may "
                "be unavailable, so treat display semantics as client/manual validation."
            ),
        )
        await send_group_case(
            api,
            cases,
            group_id=args.group,
            name="at_image_only",
            msgtype="IMAGE",
            body=[
                {"type": "AT", "atuserids": [args.user]},
                {"type": "IMAGE", "content": image},
            ],
            expected="AT + IMAGE without TEXT should preserve native AT and IMAGE.",
        )
        if args.include_at_all:
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="image_atall_user_combined",
                msgtype="IMAGE",
                body=[
                    {"type": "AT", "atall": True, "atuserids": [args.user]},
                    {"type": "IMAGE", "content": image},
                ],
                expected=(
                    "Semantic failure probe: IMAGE with one AT item containing "
                    "atall and atuserids sends but should lose the specific user AT."
                ),
            )
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="image_atall_user_separate",
                msgtype="IMAGE",
                body=[
                    {"type": "AT", "atall": True},
                    {"type": "AT", "atuserids": [args.user]},
                    {"type": "IMAGE", "content": image},
                ],
                expected=(
                    "IMAGE with separate AT items for atall and atuserids should "
                    "preserve both native mentions."
                ),
            )
            if args.agent_id is not None:
                await send_group_case(
                    api,
                    cases,
                    group_id=args.group,
                    name="image_atall_agent_combined",
                    msgtype="IMAGE",
                    body=[
                        {"type": "AT", "atall": True, "atagentids": [args.agent_id]},
                        {"type": "IMAGE", "content": image},
                    ],
                    expected=(
                        "Semantic failure probe: IMAGE with one AT item containing "
                        "atall and atagentids sends but should lose the robot AT."
                    ),
                )
                await send_group_case(
                    api,
                    cases,
                    group_id=args.group,
                    name="image_atall_agent_separate",
                    msgtype="IMAGE",
                    body=[
                        {"type": "AT", "atall": True},
                        {"type": "AT", "atagentids": [args.agent_id]},
                        {"type": "IMAGE", "content": image},
                    ],
                    expected=(
                        "IMAGE with separate AT items for atall and atagentids should "
                        "preserve both native mentions."
                    ),
                )
        if base_mid:
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="reply_image_text_image",
                msgtype="IMAGE",
                body=[
                    {"type": "TEXT", "content": f"【群聊格式验证 reply-IMAGE｜{run_marker}】reply + TEXT + 200x200 蓝图。"},
                    {"type": "IMAGE", "content": image},
                ],
                expected="Reply + IMAGE packet should preserve replyData and IMAGE.",
                reply_to=base_mid,
                reply_preview=f"group format base {run_marker}",
            )
            await send_group_case(
                api,
                cases,
                group_id=args.group,
                name="reply_image_only",
                msgtype="IMAGE",
                body=[
                    {"type": "IMAGE", "content": image},
                ],
                expected="Reply + IMAGE without TEXT should preserve replyData and IMAGE.",
                reply_to=base_mid,
                reply_preview=f"group format base {run_marker}",
            )

    await attach_group_echoes(cases, wait_seconds=args.wait_seconds)
    print_json(
        {
            "group": args.group,
            "user": args.user,
            "agent_id": args.agent_id,
            "marker": run_marker,
            "image": image_meta if args.include_image else None,
            "cases": cases,
        }
    )
    failed = [case for case in cases if not case["send_result"]["success"]]
    return 1 if failed else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default=None, help="Override INFOFLOW_OP_GROUP.")
    parser.add_argument("--user", default=None, help="uuapName used for human AT probes.")
    parser.add_argument("--agent-id", default=None, help="Non-self agentId used for robot AT probes.")
    parser.add_argument(
        "--include-at-all",
        action="store_true",
        help="Also send @all probes to the target group.",
    )
    parser.add_argument(
        "--include-image",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include 200x200 blue PNG image probes.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=12.0,
        help="Seconds to wait before reading local webhook echo logs.",
    )
    return parser


def main() -> int:
    bootstrap()
    required_env("INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET")
    args = _build_parser().parse_args()
    args.group = require_group_id(args.group)
    args.user = require_test_user(args.user)
    args.agent_id = parse_int(args.agent_id)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
