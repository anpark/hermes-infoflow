"""Probe reply preview/content omission and invalid message ids.

This script sends raw Infoflow payloads because the production wrappers may
always include preview/content fields. Group cases are checked through local
webhook echoes. Private cases have no local echo, so messages include case ids
and expected display notes for manual validation when API accepts the payload.
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
    result_summary,
)


WRONG_GROUP_MESSAGE_ID = "9999999999999999999"
WRONG_PRIVATE_MSGKEY = "9999999999999999999"


async def _raw_group_text_post(
    account: Any,
    *,
    group_id: str,
    content: str,
    reply: dict[str, Any] | None = None,
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


def _append_raw_case(
    cases: list[dict[str, Any]],
    *,
    scope: str,
    name: str,
    expected: str,
    request: dict[str, Any],
    result: dict[str, Any],
    manual_validation_required: bool = False,
) -> str:
    mid = str(
        result.get("messageid")
        or result.get("msgkey")
        or result.get("message_id")
        or ""
    )
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

    group_cases: list[dict[str, Any]] = []

    async def group_case(
        name: str,
        content: str,
        expected: str,
        *,
        reply: dict[str, Any] | None = None,
    ) -> str:
        result = await _raw_group_text_post(
            account,
            group_id=args.group,
            content=content,
            reply=reply,
        )
        mid = _append_raw_case(
            group_cases,
            scope="group",
            name=name,
            expected=expected,
            request={
                "msgtype": "TEXT",
                "body": [{"type": "TEXT", "content": content}],
                "reply": reply,
            },
            result=result,
        )
        await asyncio.sleep(0.45)
        return mid

    group_base_mid = await group_case(
        "GBASE",
        f"【群 reply preview 边界 GBASE｜{run_marker}】群聊 reply 基准消息。",
        "Base group message for reply preview/content omission probes.",
    )

    if group_base_mid:
        await group_case(
            "G_VALID_REPLY_NO_PREVIEW",
            f"【群 reply preview 边界 G01｜{run_marker}】reply 只传 messageid，不传 preview。期望：若接口支持则显示 replyData。",
            "Valid group reply with messageid only; checks whether preview is required.",
            reply={"messageid": group_base_mid},
        )
        await group_case(
            "G_VALID_REPLY_EMPTY_PREVIEW",
            f"【群 reply preview 边界 G02｜{run_marker}】reply 传 preview 空字符串。期望：若接口支持则显示 replyData。",
            "Valid group reply with empty preview.",
            reply={"messageid": group_base_mid, "preview": ""},
        )

    await group_case(
        "G_WRONG_REPLY_WITH_PREVIEW",
        f"【群 reply preview 边界 G03｜{run_marker}】错误 messageid + preview。期望：验证服务是否拒绝或丢 reply。",
        "Invalid group reply messageid with preview.",
        reply={"messageid": WRONG_GROUP_MESSAGE_ID, "preview": f"wrong group {run_marker}"},
    )
    await group_case(
        "G_WRONG_REPLY_NO_PREVIEW",
        f"【群 reply preview 边界 G04｜{run_marker}】错误 messageid 且不传 preview。期望：验证服务是否拒绝或丢 reply。",
        "Invalid group reply messageid without preview.",
        reply={"messageid": WRONG_GROUP_MESSAGE_ID},
    )

    await attach_group_echoes(group_cases, wait_seconds=args.wait_seconds)

    private_cases: list[dict[str, Any]] = []
    if args.include_private:
        if not agent_id:
            raise SystemExit("INFOFLOW_APP_AGENT_ID is required for private probes")

        async def private_case(
            name: str,
            payload: dict[str, Any],
            expected: str,
        ) -> str:
            result = await _private_payload(account, payload)
            mid = _append_raw_case(
                private_cases,
                scope="private",
                name=name,
                expected=expected,
                request=payload,
                result=result,
                manual_validation_required=bool(result.get("ok")),
            )
            await asyncio.sleep(0.3)
            return mid

        private_base_mid = await private_case(
            "PBASE",
            {
                "touser": args.private_user,
                "toparty": "",
                "totag": "",
                "agentid": agent_id,
                "msgtype": "text",
                "text": {
                    "content": f"【私聊 reply content 边界 PBASE｜{run_marker}】私聊 reply 基准消息。"
                },
            },
            "Base private message for reply content omission probes.",
        )

        if private_base_mid:
            await private_case(
                "P_VALID_REPLY_NO_CONTENT",
                {
                    "touser": args.private_user,
                    "toparty": "",
                    "totag": "",
                    "agentid": agent_id,
                    "msgtype": "text",
                    "text": {
                        "content": (
                            f"【私聊 reply content 边界 P01｜{run_marker}】reply[] item "
                            "只传 uid/msgid，不传 content。期望：若接口支持则展示引用和本行。"
                        )
                    },
                    "reply": [{"uid": "0", "msgid": private_base_mid}],
                },
                "Valid private reply item without content.",
            )
            await private_case(
                "P_VALID_REPLY_EMPTY_CONTENT",
                {
                    "touser": args.private_user,
                    "toparty": "",
                    "totag": "",
                    "agentid": agent_id,
                    "msgtype": "text",
                    "text": {
                        "content": (
                            f"【私聊 reply content 边界 P02｜{run_marker}】reply[] item "
                            "content 为空字符串。期望：若接口支持则展示引用和本行。"
                        )
                    },
                    "reply": [{"content": "", "uid": "0", "msgid": private_base_mid}],
                },
                "Valid private reply item with empty content.",
            )

        await private_case(
            "P_WRONG_REPLY_WITH_CONTENT",
            {
                "touser": args.private_user,
                "toparty": "",
                "totag": "",
                "agentid": agent_id,
                "msgtype": "text",
                "text": {
                    "content": (
                        f"【私聊 reply content 边界 P03｜{run_marker}】错误 msgid + content。"
                        "期望：验证服务是否拒绝或客户端是否不展示引用。"
                    )
                },
                "reply": [
                    {
                        "content": f"wrong private {run_marker}",
                        "uid": "0",
                        "msgid": WRONG_PRIVATE_MSGKEY,
                    }
                ],
            },
            "Invalid private reply msgid with content.",
        )
        await private_case(
            "P_WRONG_REPLY_NO_CONTENT",
            {
                "touser": args.private_user,
                "toparty": "",
                "totag": "",
                "agentid": agent_id,
                "msgtype": "text",
                "text": {
                    "content": (
                        f"【私聊 reply content 边界 P04｜{run_marker}】错误 msgid 且不传 content。"
                        "期望：验证服务是否拒绝或客户端是否不展示引用。"
                    )
                },
                "reply": [{"uid": "0", "msgid": WRONG_PRIVATE_MSGKEY}],
            },
            "Invalid private reply msgid without content.",
        )

    print_json(
        {
            "marker": run_marker,
            "group": args.group,
            "private_user": args.private_user if args.include_private else None,
            "group_cases": group_cases,
            "private_cases": private_cases,
            "private_manual_note": (
                "Private self-sends have no local echo. If private cases return success, "
                "ask the recipient to confirm by Pxx case id whether the quote is visible."
            )
            if args.include_private
            else "",
        }
    )

    return 0


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
        help="Also send private reply omission probes requiring manual validation.",
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
    if args.include_private:
        args.private_user = require_test_user(args.private_user)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
