"""Send a real group message through the refactored shared outbound path.

This exercises the exact code that both ``adapter.send()`` and
``standalone_send()`` now converge on:

    prepare_outbound_message(...)   # outbound.py — metadata + @-mention
        ↓
    ServerAPI.send_to_group(...)    # serverapi.py — builds ContentItem[]
        ↓
    api.send_group_message(...)     # api.py — actual HTTP POST

It does **not** depend on hermes-agent / gateway being importable, so
it is the lowest-friction way to verify the refactor against the real
Infoflow backend.

Usage
-----

    python scripts/sim/test_send_via_serverapi.py
    python scripts/sim/test_send_via_serverapi.py --text "hello"
    python scripts/sim/test_send_via_serverapi.py --mention "@chengbo05"
    python scripts/sim/test_send_via_serverapi.py --mention-user chengbo05
    python scripts/sim/test_send_via_serverapi.py --at-all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from _env import bootstrap, required_env, test_group_id


async def _run(args: argparse.Namespace) -> int:
    from hermes_infoflow.outbound import prepare_outbound_message
    from hermes_infoflow.serverapi import ServerAPI
    from hermes_infoflow.settings import _read_account_settings

    settings = _read_account_settings(None)
    serverapi = ServerAPI(settings=settings)

    metadata: dict[str, object] = {}
    if args.at_all:
        metadata["at_all"] = True
    if args.mention_user:
        metadata["mention_user_ids"] = args.mention_user
    if args.mention_agent:
        metadata["mention_agent_ids"] = args.mention_agent

    text, options = await prepare_outbound_message(
        args.text,
        group_id=args.group,
        metadata=metadata or None,
        get_group_members=serverapi.get_group_members,
        bot_agent_id=settings.get("app_agent_id"),
    )

    print(
        f"[sim:serverapi] prepared text={text!r} options="
        f"at_all={options.at_all} users={options.mention_user_ids!r} "
        f"agents={options.mention_agent_ids!r}"
    )

    result = await serverapi.send_to_group(args.group, text, options=options)
    print("[sim:serverapi] result:", json.dumps({
        "success": result.success,
        "message_id": result.message_id,
        "msgseqid": result.msgseqid,
        "error": result.error,
    }, ensure_ascii=False))
    return 0 if result.success else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--group", default=None,
        help="Override INFOFLOW_TEST_GROUP_ID for a one-shot run.",
    )
    parser.add_argument(
        "--text", default=None,
        help="Message body. Default: a timestamped marker.",
    )
    parser.add_argument(
        "--mention", action="append", default=[],
        help="Append @mention(s) to the text (e.g. --mention @chengbo05). May repeat.",
    )
    parser.add_argument(
        "--mention-user", default="",
        help="Comma-separated uuapNames forwarded via metadata.mention_user_ids.",
    )
    parser.add_argument(
        "--mention-agent", default="",
        help="Comma-separated agentIds forwarded via metadata.mention_agent_ids.",
    )
    parser.add_argument("--at-all", action="store_true", help="Set metadata.at_all=true.")
    return parser


def main() -> int:
    status = bootstrap()
    print(f"[sim] bootstrap: {json.dumps(status, ensure_ascii=False)}")
    required_env("INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET")

    parser = _build_parser()
    args = parser.parse_args()
    args.group = args.group or test_group_id()
    if not args.text:
        marker = time.strftime("%Y-%m-%d %H:%M:%S")
        args.text = f"[sim:serverapi] hermes-infoflow refactor smoke test @ {marker}"
    if args.mention:
        args.text = " ".join(args.mention) + " " + args.text

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
