"""Fetch and print group member list via ServerAPI (real backend).

Usage::

    python scripts/sim/test_get_group_members.py
    python scripts/sim/test_get_group_members.py --group 4507088
"""

from __future__ import annotations

import argparse
import asyncio
import json

from _env import bootstrap, required_env, test_group_id


async def _run(group_id: str) -> int:
    from hermes_infoflow.serverapi import GroupMembersFetchStatus, ServerAPI
    from hermes_infoflow.settings import _read_account_settings
    from hermes_infoflow.tools import _serialize_group_members_payload

    settings = _read_account_settings(None)
    serverapi = ServerAPI(settings=settings)

    result = await serverapi.fetch_group_members_detailed(
        group_id,
        force_refresh=True,
    )
    if result.status == GroupMembersFetchStatus.FAILED:
        print(json.dumps({
            "error": result.error or "failed to fetch group members",
        }, ensure_ascii=False, indent=2))
        return 1

    payload = _serialize_group_members_payload(
        result.members,
        group_id,
        source=result.status.value,
        stale=result.status == GroupMembersFetchStatus.OK_STALE,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    status = bootstrap()
    print(f"[sim] bootstrap: {json.dumps(status, ensure_ascii=False)}")
    required_env("INFOFLOW_API_HOST", "INFOFLOW_APP_KEY", "INFOFLOW_APP_SECRET")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--group",
        default=None,
        help="Override INFOFLOW_TEST_GROUP_ID for a one-shot run.",
    )
    args = parser.parse_args()
    group_id = args.group or test_group_id()
    return asyncio.run(_run(group_id))


if __name__ == "__main__":
    raise SystemExit(main())
