"""Send a real group message through the standalone (cron) entry point.

This drives ``hermes_infoflow.standalone.standalone_send`` exactly the
way a Hermes cron child process would: with a ``SimpleNamespace`` config
and only the env vars from ``~/.hermes/.env`` for credentials.

After the refactor, this path uses the shared ``prepare_outbound_message``
helper and the same ``ServerAPI.send_to_group()`` as the live adapter —
so this script verifies the cron path produces identical wire payloads
(including @-mention extraction, which was previously broken).

Usage
-----

    python scripts/sim/test_send_via_standalone.py
    python scripts/sim/test_send_via_standalone.py --text "hello from cron"
    python scripts/sim/test_send_via_standalone.py --mention "@chengbo05"
    python scripts/sim/test_send_via_standalone.py --at-all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from types import SimpleNamespace

from _env import bootstrap, required_env, test_group_id


async def _run(args: argparse.Namespace) -> int:
    from hermes_infoflow.standalone import standalone_send

    metadata: dict[str, object] = {}
    if args.at_all:
        metadata["at_all"] = True
    if args.mention_user:
        metadata["mention_user_ids"] = args.mention_user
    if args.mention_agent:
        metadata["mention_agent_ids"] = args.mention_agent

    pconfig = SimpleNamespace(extra={})
    chat_id = f"infoflow:group:{args.group}"

    result = await standalone_send(
        pconfig,
        chat_id=chat_id,
        message=args.text,
        metadata=metadata or None,
    )
    print("[sim:standalone] result:", json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default=None, help="Override INFOFLOW_TEST_GROUP_ID.")
    parser.add_argument("--text", default=None, help="Message body. Default: timestamped marker.")
    parser.add_argument(
        "--mention", action="append", default=[],
        help="Append @mention(s) to the text. May repeat.",
    )
    parser.add_argument("--mention-user", default="", help="metadata.mention_user_ids CSV.")
    parser.add_argument("--mention-agent", default="", help="metadata.mention_agent_ids CSV.")
    parser.add_argument("--at-all", action="store_true", help="Set metadata.at_all=true.")
    return parser


def main() -> int:
    status = bootstrap()
    print(f"[sim] bootstrap: {json.dumps(status, ensure_ascii=False)}")
    required_env("INFOFLOW_API_HOST", "INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET")

    parser = _build_parser()
    args = parser.parse_args()
    args.group = args.group or test_group_id()
    if not args.text:
        marker = time.strftime("%Y-%m-%d %H:%M:%S")
        args.text = f"[sim:standalone] hermes-infoflow cron smoke @ {marker}"
    if args.mention:
        args.text = " ".join(args.mention) + " " + args.text

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
