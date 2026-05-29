"""Probe group/private reply target count limits against real Infoflow.

Group messages have a different wire format from private app messages. This
probe keeps the group packet as exact ``msgtype=TEXT`` + body ``TEXT`` so the
only tested variable is the shape/count of ``message.reply``.

Private self-sends do not produce a local webhook echo, so private cases embed
case ids and expected-display text. Ask the recipient to confirm those case ids.
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
    print_json,
    require_group_id,
    require_test_user,
)


async def _raw_group_text_post(
    account: Any,
    *,
    group_id: str,
    content: str,
    reply: Any = None,
) -> dict[str, Any]:
    from hermes_infoflow import api as _api

    token = await _api.get_app_access_token(account)
    payload: dict[str, Any] = {
        "message": {
            "header": {
                "toid": int(group_id),
                "totype": "GROUP",
                "msgtype": "TEXT",
                "clientmsgid": _api._next_clientmsgid(),
                "role": "robot",
            },
            "body": [{"type": "TEXT", "content": content}],
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
    result = _api._parse_send_response(text, kind="group")
    result["raw_http_text"] = text[:500]
    return result


async def _private_payload(account: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from hermes_infoflow import api as _api

    return await _api.send_private_payload(account, payload)


def _result_summary(result: dict[str, Any], *, kind: str) -> dict[str, Any]:
    mid_field = "messageid" if kind == "group" else "msgkey"
    return {
        "success": bool(result.get("ok")),
        "message_id": str(result.get(mid_field) or result.get("messageid") or ""),
        "error": str(result.get("error") or ""),
        "raw": result,
    }


def _parse_counts(raw: str) -> list[int]:
    counts: list[int] = []
    for item in str(raw or "").replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            count = int(item)
        except ValueError as exc:
            raise SystemExit(f"--private-counts must contain integers: {item!r}") from exc
        if count <= 0:
            raise SystemExit("--private-counts values must be positive")
        if count not in counts:
            counts.append(count)
    return counts or [3, 5]


async def _run(args: argparse.Namespace) -> int:
    from hermes_infoflow.serverapi import ServerAPI
    from hermes_infoflow.settings import _read_account_settings

    settings = _read_account_settings(None)
    api = ServerAPI(settings=settings)
    account = api._api_account
    agent_id = str(settings.get("app_agent_id") or "")
    run_marker = marker()
    cases: list[dict[str, Any]] = []

    async def group_case(name: str, content: str, reply: Any = None) -> str:
        result = await _raw_group_text_post(
            account,
            group_id=args.group,
            content=content,
            reply=reply,
        )
        summary = _result_summary(result, kind="group")
        cases.append(
            {
                "scope": "group",
                "name": name,
                "request": {
                    "msgtype": "TEXT",
                    "body": [{"type": "TEXT", "content": content}],
                    "reply": reply,
                },
                "send_result": summary,
            }
        )
        await asyncio.sleep(0.45)
        return summary["message_id"]

    group_base_ids = [
        await group_case(
            f"GBASE{idx}",
            f"【群 TEXT 多 reply 探测 GBASE{idx}｜{run_marker}】基准 {idx}。",
        )
        for idx in range(1, 4)
    ]
    group_replies = [
        {"messageid": mid, "preview": f"GBASE{idx} {run_marker}"}
        for idx, mid in enumerate(group_base_ids, start=1)
        if mid
    ]
    if group_replies:
        await group_case(
            "G_SINGLE_REPLY_OBJECT",
            f"【群 TEXT 多 reply 探测 SINGLE｜{run_marker}】单 reply object，应成功。",
            group_replies[0],
        )
    if len(group_replies) >= 2:
        await group_case(
            "G_REPLY_ARRAY_2",
            f"【群 TEXT 多 reply 探测 ARRAY2｜{run_marker}】reply 数组 2 条，预期服务拒绝。",
            group_replies[:2],
        )
    if len(group_replies) >= 3:
        await group_case(
            "G_REPLY_ARRAY_3",
            f"【群 TEXT 多 reply 探测 ARRAY3｜{run_marker}】reply 数组 3 条，预期服务拒绝。",
            group_replies[:3],
        )

    await attach_group_echoes(cases, wait_seconds=args.wait_seconds)

    private_cases: list[dict[str, Any]] = []
    private_counts = _parse_counts(args.private_counts)
    if args.include_private:
        if not agent_id:
            raise SystemExit("INFOFLOW_APP_AGENT_ID is required for private probes")
        max_count = max(private_counts)

        async def private_case(name: str, payload: dict[str, Any]) -> str:
            result = await _private_payload(account, payload)
            summary = _result_summary(result, kind="private")
            private_cases.append(
                {
                    "scope": "private",
                    "name": name,
                    "request": payload,
                    "send_result": summary,
                    "manual_validation_required": bool(summary["success"]),
                }
            )
            await asyncio.sleep(0.3)
            return summary["message_id"]

        private_base_ids: list[str] = []
        for idx in range(1, max_count + 1):
            mid = await private_case(
                f"PBASE{idx}",
                {
                    "touser": args.private_user,
                    "toparty": "",
                    "totag": "",
                    "agentid": agent_id,
                    "msgtype": "text",
                    "text": {
                        "content": (
                            f"【私聊 reply 数量探测 PBASE{idx}｜{run_marker}】"
                            f"私聊 reply 基准 {idx}。"
                        )
                    },
                },
            )
            private_base_ids.append(mid)

        def reply_payload(count: int) -> list[dict[str, str]]:
            return [
                {
                    "content": f"PBASE{idx + 1} reply count probe {run_marker}",
                    "uid": "0",
                    "msgid": private_base_ids[idx],
                }
                for idx in range(count)
                if idx < len(private_base_ids) and private_base_ids[idx]
            ]

        for count in private_counts:
            label = "P15" if count == 3 else "P16" if count == 5 else f"P-TEXT-{count}"
            await private_case(
                f"{label}_TEXT_{count}_REPLIES",
                {
                    "touser": args.private_user,
                    "toparty": "",
                    "totag": "",
                    "agentid": agent_id,
                    "msgtype": "text",
                    "text": {
                        "content": (
                            f"【私聊 reply 数量探测 {label}｜{run_marker}】"
                            f"text + {count} 个 reply[]。期望：客户端展示 {count} 条引用，正文为本行。"
                        )
                    },
                    "reply": reply_payload(count),
                },
            )

        rich_count = max_count
        rich_label = "P17" if rich_count == 5 else f"P-RICH-{rich_count}"
        await private_case(
            f"{rich_label}_RICHTEXT_LINK_{rich_count}_REPLIES",
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
                            "href": f"https://example.com/private-reply-count-{run_marker}",
                            "label": (
                                f"【私聊 reply 数量探测 {rich_label}｜{run_marker}】"
                                f"纯链接 richtext + {rich_count} 个 reply[]。"
                                f"期望：客户端展示 {rich_count} 条引用，引用后的整行文字为可点击链接。"
                            ),
                        }
                    ]
                },
                "reply": reply_payload(rich_count),
            },
        )

    print_json(
        {
            "marker": run_marker,
            "group": args.group,
            "private_user": args.private_user if args.include_private else None,
            "group_cases": cases,
            "private_cases": private_cases,
            "private_manual_note": (
                "Private self-sends have no local echo. Ask the recipient to "
                "confirm display by P-TEXT/P-RICH case ids."
            )
            if args.include_private
            else "",
        }
    )

    unexpected_group_failures = [
        case
        for case in cases
        if not case["send_result"]["success"]
        and case["name"]
        not in {"G_REPLY_ARRAY_2", "G_REPLY_ARRAY_3"}
    ]
    unexpected_group_successes = [
        case
        for case in cases
        if case["send_result"]["success"]
        and case["name"] in {"G_REPLY_ARRAY_2", "G_REPLY_ARRAY_3"}
    ]
    unexpected_private_failures = [
        case for case in private_cases if not case["send_result"]["success"]
    ]
    return 1 if (
        unexpected_group_failures
        or unexpected_group_successes
        or unexpected_private_failures
    ) else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default=None, help="Override INFOFLOW_OP_GROUP.")
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=12.0,
        help="Seconds to wait before reading local group echo logs.",
    )
    parser.add_argument(
        "--include-private",
        action="store_true",
        help="Also send private multi-reply probes requiring manual validation.",
    )
    parser.add_argument(
        "--private-user",
        default=None,
        help="Private recipient uuapName when --include-private is set.",
    )
    parser.add_argument(
        "--private-counts",
        default="3,5",
        help="Comma-separated private reply counts to probe, default: 3,5.",
    )
    return parser


def main() -> int:
    bootstrap()
    required_env("INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET")
    args = _build_parser().parse_args()
    args.group = require_group_id(args.group)
    if args.include_private:
        args.private_user = require_test_user(args.private_user)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
