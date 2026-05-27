"""Create an Infoflow group via ServerAPI (real backend).

Usage::

    python scripts/sim/test_create_group.py --name "测试群" --owner chengbo05
    python scripts/sim/test_create_group.py --name "测试群" --owner chengbo05 \
      --members chengbo05 --robots 15072,6471
"""

from __future__ import annotations

import argparse
import asyncio
import json

from _env import bootstrap, required_env


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _email(value: str) -> str:
    raw = value.strip().lower()
    if not raw:
        return raw
    return raw if "@" in raw else f"{raw}@baidu.com"


def _int_csv(raw: str) -> list[int]:
    return [int(item) for item in _split_csv(raw)]


async def _run(args: argparse.Namespace) -> int:
    from hermes_infoflow.serverapi import ServerAPI
    from hermes_infoflow.settings import _read_account_settings

    serverapi = ServerAPI(settings=_read_account_settings(None))
    result = await serverapi.create_group(
        group_name=args.name,
        group_owner=_email(args.owner),
        member_list=[_email(item) for item in _split_csv(args.members)] or None,
        robot_list=_int_csv(args.robots) or None,
        friendly_level=args.friendly_level,
        search_ability=args.search_ability,
        managers=[_email(item) for item in _split_csv(args.managers)] or None,
        robot_managers=_int_csv(args.robot_managers) or None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def main() -> int:
    status = bootstrap()
    print(f"[sim] bootstrap: {json.dumps(status, ensure_ascii=False)}")
    required_env("INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Group name")
    parser.add_argument("--owner", required=True, help="Owner uuapName or email")
    parser.add_argument("--members", default="", help="Human members, comma-separated")
    parser.add_argument("--robots", default="", help="Robot agentIds, comma-separated")
    parser.add_argument("--friendly-level", type=int, default=3, choices=[1, 2, 3])
    parser.add_argument("--search-ability", type=int, default=1, choices=[0, 1])
    parser.add_argument("--managers", default="", help="Human managers, comma-separated")
    parser.add_argument("--robot-managers", default="", help="Robot manager agentIds, comma-separated")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
