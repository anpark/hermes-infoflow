"""Probe message-contract edge cases that need exact wire payloads.

Unlike ``probe_group_formats.py`` and ``probe_group_links.py``, this script
bypasses the normal structured send helper for selected group cases so it can
test exact outbound casing and protocol-family failures such as
``msgtype=text``, body ``type=text``, ``MD + LINK``, or ``IMAGE + MD``. By
default it only sends group messages; private edge probes are opt-in because
they require recipient-side manual validation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import aiohttp

from _env import bootstrap, required_env
from _message_format_probe import (
    attach_group_echoes,
    marker,
    prepared_blue_png_payload,
    print_json,
    require_group_id,
    require_test_user,
)


async def _raw_group_post(
    account: Any,
    *,
    group_id: str,
    msgtype: str,
    body: list[dict[str, Any]],
    reply: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from hermes_infoflow import api as _api

    token = await _api.get_app_access_token(account)
    payload: dict[str, Any] = {
        "message": {
            "header": {
                "toid": int(group_id),
                "totype": "GROUP",
                "msgtype": msgtype,
                "clientmsgid": _api._next_clientmsgid(),
                "role": "robot",
            },
            "body": body,
        }
    }
    if reply is not None:
        payload["message"]["reply"] = reply

    url = _api._join(account.api_host, _api.INFOFLOW_GROUP_SEND_PATH)
    headers = _api._auth_headers(token, content_type="application/json")
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        ) as resp:
            text = await resp.text()
    parsed = _api._parse_send_response(text, kind="group")
    parsed["raw_http_text"] = text[:500]
    return parsed


async def _private_payload(account: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from hermes_infoflow import api as _api

    return await _api.send_private_payload(account, payload)


def _append_case(
    cases: list[dict[str, Any]],
    *,
    scope: str,
    name: str,
    expected: str,
    request: dict[str, Any],
    result: dict[str, Any],
    manual_validation_required: bool = False,
) -> str:
    mid = str(result.get("messageid") or result.get("msgkey") or "")
    cases.append(
        {
            "scope": scope,
            "name": name,
            "expected": expected,
            "request": request,
            "send_result": {
                "success": bool(result.get("ok")),
                "message_id": mid,
                "error": str(result.get("error") or ""),
                "raw": result,
            },
            "manual_validation_required": manual_validation_required,
        }
    )
    return mid


async def _run(args: argparse.Namespace) -> int:
    from hermes_infoflow.serverapi import ServerAPI
    from hermes_infoflow.settings import _read_account_settings

    settings = _read_account_settings(None)
    api = ServerAPI(settings=settings)
    account = api._api_account
    agent_id = str(settings.get("app_agent_id") or "")
    run_marker = marker()
    url = f"https://example.com/infoflow-contract-edge-{run_marker}"
    image, image_meta = prepared_blue_png_payload()
    cases: list[dict[str, Any]] = []

    async def group_case(
        name: str,
        msgtype: str,
        body: list[dict[str, Any]],
        expected: str,
        *,
        reply: dict[str, Any] | None = None,
    ) -> str:
        result = await _raw_group_post(
            account,
            group_id=args.group,
            msgtype=msgtype,
            body=body,
            reply=reply,
        )
        mid = _append_case(
            cases,
            scope="group",
            name=name,
            expected=expected,
            request={"msgtype": msgtype, "body": body, "reply": reply},
            result=result,
        )
        await asyncio.sleep(0.35)
        return mid

    base_mid = await group_case(
        "edge_base_text",
        "TEXT",
        [{"type": "TEXT", "content": f"【契约边界 BASE｜{run_marker}】群 reply 边界基准消息。"}],
        "Base group message for reply edge probes.",
    )
    await group_case(
        "group_header_lower_text_body_upper",
        "text",
        [{"type": "TEXT", "content": f"【契约边界 G-CASE-01｜{run_marker}】header msgtype=text, body TEXT。"}],
        "Expected failure: group msgtype is case-sensitive.",
    )
    await group_case(
        "group_header_upper_body_lower_text",
        "TEXT",
        [{"type": "text", "content": f"【契约边界 G-CASE-02｜{run_marker}】header TEXT, body type=text。"}],
        "Expected failure: group body type is case-sensitive.",
    )
    await group_case(
        "group_header_mixed_text_body",
        "MIXED",
        [{"type": "TEXT", "content": f"【契约边界 G-CASE-03｜{run_marker}】header MIXED, body TEXT。"}],
        "Expected failure: echo msgtype MIXED is not a valid outbound msgtype.",
    )
    await group_case(
        "group_lower_link_type",
        "TEXT",
        [{"type": "link", "href": f"https://example.com/group-lower-link-{run_marker}"}],
        "Expected failure: group LINK body type is case-sensitive.",
    )
    await group_case(
        "group_md_header_text_body",
        "MD",
        [{"type": "TEXT", "content": f"【契约边界 G-FAMILY-01｜{run_marker}】MD header + TEXT body。"}],
        "Expected failure: MD header cannot carry TEXT-only body.",
    )
    await group_case(
        "group_text_header_md_body",
        "TEXT",
        [{"type": "MD", "content": f"【契约边界 G-FAMILY-02｜{run_marker}】TEXT header + MD body。"}],
        "Expected failure: TEXT header cannot carry MD body.",
    )
    await group_case(
        "group_link_label_only",
        "TEXT",
        [{"type": "LINK", "label": url + "/label-only"}],
        "Expected failure: outbound LINK requires href.",
    )
    await group_case(
        "group_md_link",
        "MD",
        [
            {"type": "MD", "content": f"【契约边界 G-LINK-01｜{run_marker}】MD + LINK。"},
            {"type": "LINK", "href": url + "/md-link"},
        ],
        "Expected failure: LINK is not compatible with MD header.",
    )
    await group_case(
        "group_image_md_body",
        "IMAGE",
        [
            {"type": "MD", "content": f"【契约边界 G-IMAGE-01｜{run_marker}】IMAGE packet 内使用 MD。"},
            {"type": "IMAGE", "content": image},
        ],
        "Expected failure: IMAGE packet text cannot use MD body item.",
    )
    await group_case(
        "group_md_atall_user_separate",
        "MD",
        [
            {"type": "AT", "atall": True},
            {"type": "AT", "atuserids": [args.user]},
            {
                "type": "MD",
                "content": (
                    f"@all @{args.user} "
                    f"【契约边界 G-MD-AT-01｜{run_marker}】MD 下 @all 和具体人拆多个 AT item。"
                ),
            },
        ],
        "Expected failure: MD cannot use separate AT items for atall and specific users.",
    )
    await group_case(
        "group_at_only_user",
        "TEXT",
        [{"type": "AT", "atuserids": [args.user]}],
        "AT-only group message should preserve native AT.",
    )

    if base_mid:
        reply_base = {
            "messageid": base_mid,
            "preview": f"contract edge base {run_marker}",
        }
        await group_case(
            "group_reply_no_imid",
            "TEXT",
            [{"type": "TEXT", "content": f"【契约边界 G-REPLY-01｜{run_marker}】reply block 不带 imid。"}],
            "Reply without imid should still preserve replyData.",
            reply=reply_base,
        )
        await group_case(
            "group_reply_empty_text",
            "TEXT",
            [{"type": "TEXT", "content": ""}],
            "Reply-only group message should preserve replyData with empty TEXT body.",
            reply=reply_base,
        )
        replytype_1 = dict(reply_base)
        replytype_1["replytype"] = "1"
        await group_case(
            "group_replytype_1_text",
            "TEXT",
            [{"type": "TEXT", "content": f"【契约边界 G-REPLY-03｜{run_marker}】replytype=1。"}],
            "replytype=1 should preserve replyData.",
            reply=replytype_1,
        )
        replytype_2 = dict(reply_base)
        replytype_2["replytype"] = "2"
        await group_case(
            "group_replytype_2_text",
            "TEXT",
            [{"type": "TEXT", "content": f"【契约边界 G-REPLY-04｜{run_marker}】replytype=2。"}],
            "replytype=2 should preserve replyData.",
            reply=replytype_2,
        )
        await group_case(
            "group_md_reply_semantic_failure",
            "MD",
            [{"type": "MD", "content": f"【契约边界 G-REPLY-05｜{run_marker}】**MD + reply**。"}],
            "API may return success, but webhook echo should not contain replyData.",
            reply=reply_base,
        )

    await attach_group_echoes(cases, wait_seconds=args.wait_seconds)

    private_cases: list[dict[str, Any]] = []
    if args.include_private:
        async def private_case(
            name: str,
            payload: dict[str, Any],
            expected: str,
        ) -> str:
            result = await _private_payload(account, payload)
            mid = _append_case(
                private_cases,
                scope="private",
                name=name,
                expected=expected,
                request=payload,
                result=result,
                manual_validation_required=bool(result.get("ok")),
            )
            await asyncio.sleep(0.25)
            return mid

        pbase = await private_case(
            "private_edge_base_text",
            {
                "touser": args.private_user,
                "toparty": "",
                "totag": "",
                "agentid": agent_id,
                "msgtype": "text",
                "text": {"content": f"【私聊契约边界 BASE｜{run_marker}】私聊 reply 边界基准消息。"},
            },
            "Base private message for reply edge probes.",
        )
        pbase2 = await private_case(
            "private_edge_base_text_2",
            {
                "touser": args.private_user,
                "toparty": "",
                "totag": "",
                "agentid": agent_id,
                "msgtype": "text",
                "text": {"content": f"【私聊契约边界 BASE2｜{run_marker}】私聊多 reply 的第二条基准消息。"},
            },
            "Second base private message for multiple-reply probes.",
        )
        await private_case(
            "private_upper_msgtype_text",
            {
                "touser": args.private_user,
                "toparty": "",
                "totag": "",
                "agentid": agent_id,
                "msgtype": "TEXT",
                "text": {"content": f"【私聊契约边界 P-CASE-01｜{run_marker}】msgtype=TEXT。"},
            },
            "Expected failure: private msgtype is case-sensitive.",
        )
        await private_case(
            "private_lower_msgtype_upper_object_key",
            {
                "touser": args.private_user,
                "toparty": "",
                "totag": "",
                "agentid": agent_id,
                "msgtype": "text",
                "Text": {"content": f"【私聊契约边界 P-CASE-02｜{run_marker}】msgtype=text, object key Text。"},
            },
            "Expected failure: private content object key is case-sensitive.",
        )
        await private_case(
            "private_richtext_multi_links",
            {
                "touser": args.private_user,
                "toparty": "",
                "totag": "",
                "agentid": agent_id,
                "msgtype": "richtext",
                "richtext": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"【私聊契约边界 P11｜{run_marker}】richtext 多链接。期望：后面两个链接都可点击：",
                        },
                        {
                            "type": "a",
                            "href": f"https://example.com/private-multi-link-{run_marker}/one",
                            "label": "P11 第一个链接",
                        },
                        {
                            "type": "a",
                            "href": f"https://example.com/private-multi-link-{run_marker}/two",
                            "label": "P11 第二个链接",
                        },
                    ]
                },
            },
            "Private richtext with multiple links; manual display validation required.",
        )
        if pbase:
            await private_case(
                "private_reply_empty_text",
                {
                    "touser": args.private_user,
                    "toparty": "",
                    "totag": "",
                    "agentid": agent_id,
                    "msgtype": "text",
                    "text": {"content": ""},
                    "reply": [
                        {
                            "content": f"P12 private edge base {run_marker}",
                            "uid": "0",
                            "msgid": pbase,
                        }
                    ],
                },
                "Private reply-only with empty text; manual display validation required.",
            )
        if pbase and pbase2:
            await private_case(
                "private_reply_two_targets_text",
                {
                    "touser": args.private_user,
                    "toparty": "",
                    "totag": "",
                    "agentid": agent_id,
                    "msgtype": "text",
                    "text": {
                        "content": (
                            f"【私聊契约边界 P13｜{run_marker}】text + 两个 reply[]。"
                            "期望：客户端展示两条引用，正文为本行。"
                        )
                    },
                    "reply": [
                        {
                            "content": f"P13 first private edge base {run_marker}",
                            "uid": "0",
                            "msgid": pbase,
                        },
                        {
                            "content": f"P13 second private edge base {run_marker}",
                            "uid": "0",
                            "msgid": pbase2,
                        },
                    ],
                },
                "Private text with two reply targets; manual display validation required.",
            )
            await private_case(
                "private_richtext_link_two_reply_targets",
                {
                    "touser": args.private_user,
                    "toparty": "",
                    "totag": "",
                    "agentid": agent_id,
                    "msgtype": "richtext",
                    "richtext": {
                        "content": [
                            {
                                "type": "a",
                                "href": f"https://example.com/private-richtext-two-replies-{run_marker}",
                                "label": (
                                    f"【私聊契约边界 P14｜{run_marker}】纯链接 richtext + 两个 reply[]。"
                                    "期望：展示两条引用，引用后的整行文字为可点击链接。"
                                ),
                            }
                        ]
                    },
                    "reply": [
                        {
                            "content": f"P14 first private edge base {run_marker}",
                            "uid": "0",
                            "msgid": pbase,
                        },
                        {
                            "content": f"P14 second private edge base {run_marker}",
                            "uid": "0",
                            "msgid": pbase2,
                        },
                    ],
                },
                "Private link-only richtext with two reply targets; manual display validation required.",
            )

    print_json(
        {
            "marker": run_marker,
            "group": args.group,
            "user": args.user,
            "image": image_meta,
            "private_user": args.private_user if args.include_private else None,
            "group_cases": cases,
            "private_cases": private_cases,
        }
    )
    expected_group_failures = {
        "group_header_lower_text_body_upper",
        "group_header_upper_body_lower_text",
        "group_header_mixed_text_body",
        "group_lower_link_type",
        "group_md_header_text_body",
        "group_text_header_md_body",
        "group_link_label_only",
        "group_md_link",
        "group_image_md_body",
        "group_md_atall_user_separate",
    }
    unexpected_group_failures = [
        case
        for case in cases
        if not case["send_result"]["success"] and case["name"] not in expected_group_failures
    ]
    unexpected_private_failures = [
        case
        for case in private_cases
        if not case["send_result"]["success"]
        and case["name"]
        not in {
            "private_upper_msgtype_text",
            "private_lower_msgtype_upper_object_key",
        }
    ]
    return 1 if unexpected_group_failures or unexpected_private_failures else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default=None, help="Override INFOFLOW_OP_GROUP.")
    parser.add_argument("--user", default=None, help="uuapName used for group AT probes.")
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=12.0,
        help="Seconds to wait before reading local group echo logs.",
    )
    parser.add_argument(
        "--include-private",
        action="store_true",
        help="Also send private edge probes that require manual recipient validation.",
    )
    parser.add_argument(
        "--private-user",
        default=None,
        help="Private recipient uuapName when --include-private is set.",
    )
    return parser


def main() -> int:
    bootstrap()
    required_env("INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET")
    args = _build_parser().parse_args()
    args.group = require_group_id(args.group)
    args.user = require_test_user(args.user)
    if args.include_private:
        args.private_user = require_test_user(args.private_user)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
