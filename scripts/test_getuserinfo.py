#!/usr/bin/env python3
"""Test Infoflow getuserinfo API (code -> UserId).

Usage:
  export INFOFLOW_API_HOST=http://apiin.im.baidu.com
  export INFOFLOW_APP_KEY=...
  export INFOFLOW_APP_SECRET=...
  export INFOFLOW_APP_AGENT_ID=6471
  python scripts/test_getuserinfo.py --code 50374f0d197196b535e0a370f49fc131
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid

# Allow running from repo root without install
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_infoflow.api import (  # noqa: E402
    INFOFLOW_GETUSERINFO_PATH,
    InfoflowAccountAPI,
    InfoflowAPIError,
    get_user_info_by_code,
)


def _md5_secret(raw: str) -> str:
    return hashlib.md5(raw.encode("utf-8")).hexdigest().lower()


async def _fetch_raw(account: InfoflowAccountAPI, code: str, token: str) -> tuple[int, str]:
    import aiohttp

    url = account.api_host.rstrip("/") + INFOFLOW_GETUSERINFO_PATH
    headers = {
        "Authorization": f"Bearer-{token}",
        "Content-Type": "application/json; charset=utf-8",
        "LOGID": str(uuid.uuid4()),
    }
    body = {"agentid": int(account.app_agent_id), "code": code}
    print(f"POST {url}")
    print(f"Authorization: Bearer-{token[:24]}...")
    print(f"LOGID: {headers['LOGID']}")
    print("Body:", json.dumps(body, ensure_ascii=False))
    async with aiohttp.ClientSession() as session, session.post(
        url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        text = await resp.text()
        print(f"\nHTTP {resp.status}")
        print(text)
        return resp.status, text


async def main() -> int:
    parser = argparse.ArgumentParser(description="Test Infoflow getuserinfo")
    parser.add_argument("--code", required=True, help="Private-chat code from sessiontracker URL")
    args = parser.parse_args()

    api_host = os.environ.get("INFOFLOW_API_HOST", "").strip()
    app_key = os.environ.get("INFOFLOW_APP_KEY", "").strip()
    app_secret = os.environ.get("INFOFLOW_APP_SECRET", "").strip()
    agent_id_raw = os.environ.get("INFOFLOW_APP_AGENT_ID", "").strip()

    missing = [
        name for name, val in (
            ("INFOFLOW_API_HOST", api_host),
            ("INFOFLOW_APP_KEY", app_key),
            ("INFOFLOW_APP_SECRET", app_secret),
            ("INFOFLOW_APP_AGENT_ID", agent_id_raw),
        )
        if not val
    ]
    if missing:
        print("Missing env:", ", ".join(missing))
        return 1

    account = InfoflowAccountAPI(
        api_host=api_host,
        app_key=app_key,
        app_secret=app_secret,
        app_agent_id=int(agent_id_raw),
    )

    from hermes_infoflow.api import get_app_access_token

    try:
        token = await get_app_access_token(account)
        await _fetch_raw(account, args.code, token)
        user_id = await get_user_info_by_code(account, args.code)
    except InfoflowAPIError as exc:
        print(f"\nget_user_info_by_code failed: {exc}")
        return 1
    except ValueError as exc:
        print(f"\ninvalid config: {exc}")
        return 1

    print(f"\nResolved UserId: {user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
