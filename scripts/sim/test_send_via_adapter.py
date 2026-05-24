"""Send a real group message through the live ``InfoflowAdapter.send()``.

This is the highest-fidelity simulation: it instantiates the full
``InfoflowAdapter`` exactly like the gateway does, then invokes its
``send()`` method directly. The whole call chain is exercised:

    InfoflowAdapter.send()
        ↓
    prepare_outbound_message()      # metadata + @-mention extraction
        ↓
    Bot.send_message()              # NO_REPLY filter, chunking, dedup,
                                    # sent_store / message_store / policy
        ↓
    ServerAPI.send_to_group()       # ContentItem builder + reply context
        ↓
    api.send_group_message()        # HTTP POST to Infoflow

Requires ``~/.hermes/hermes-agent`` to be present so the ``gateway``
package is importable.

Usage
-----

    python scripts/sim/test_send_via_adapter.py
    python scripts/sim/test_send_via_adapter.py --text "hello via adapter"
    python scripts/sim/test_send_via_adapter.py --mention "@chengbo05"
    python scripts/sim/test_send_via_adapter.py --at-all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from _env import bootstrap, required_env, test_group_id


def _make_platform_config():
    from gateway.config import PlatformConfig
    return PlatformConfig(enabled=True, extra={})


def _ensure_infoflow_in_platform_enum() -> None:
    """Backfill ``Platform.INFOFLOW`` for older hermes-agent checkouts.

    Current hermes-agent builds support plugin platform pseudo-members via
    ``Platform._missing_()`` and this function becomes a no-op. Keeping the
    fallback lets the sim run against older local checkouts as well.
    """
    from gateway.config import Platform
    if "infoflow" in Platform._value2member_map_:
        return
    member = object.__new__(Platform)
    member._name_ = "INFOFLOW"
    member._value_ = "infoflow"
    Platform._member_map_["INFOFLOW"] = member
    Platform._value2member_map_["infoflow"] = member
    if "INFOFLOW" not in Platform._member_names_:
        Platform._member_names_.append("INFOFLOW")


async def _run(args: argparse.Namespace) -> int:
    _ensure_infoflow_in_platform_enum()
    from hermes_infoflow.adapter import InfoflowAdapter

    adapter = InfoflowAdapter(_make_platform_config())

    metadata: dict[str, object] = {}
    if args.at_all:
        metadata["at_all"] = True
    if args.mention_user:
        metadata["mention_user_ids"] = args.mention_user
    if args.mention_agent:
        metadata["mention_agent_ids"] = args.mention_agent

    chat_id = f"infoflow:group:{args.group}"
    print(f"[sim:adapter] sending to {chat_id!r} text={args.text!r} metadata={metadata}")

    result = await adapter.send(
        chat_id=chat_id,
        content=args.text,
        reply_to=None,
        metadata=metadata or None,
    )

    payload = {
        "success": getattr(result, "success", None),
        "message_id": getattr(result, "message_id", None),
        "error_message": getattr(result, "error_message", None),
    }
    print("[sim:adapter] result:", json.dumps(payload, ensure_ascii=False, default=str))
    return 0 if payload["success"] else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default=None, help="Override INFOFLOW_TEST_GROUP_ID.")
    parser.add_argument("--text", default=None, help="Message body.")
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
    if not status["hermes_agent_available"]:
        raise SystemExit(
            "[sim:adapter] hermes-agent checkout not found at "
            f"{status['hermes_agent_path']}; this script needs it on sys.path."
        )
    required_env("INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET")

    parser = _build_parser()
    args = parser.parse_args()
    args.group = args.group or test_group_id()
    if not args.text:
        marker = time.strftime("%Y-%m-%d %H:%M:%S")
        args.text = f"[sim:adapter] hermes-infoflow adapter smoke @ {marker}"
    if args.mention:
        args.text = " ".join(args.mention) + " " + args.text

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
