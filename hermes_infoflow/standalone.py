"""Standalone (out-of-process) message sender for cron / CLI usage."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import api as _api
from .sent_store import SentMessageStore

logger = logging.getLogger(__name__)


async def standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Send a single message without a live adapter (cron child process).

    The result is also persisted to the shared SQLite ``sent-messages.db`` so
    the LIVE adapter (or a later cron run) can find and recall the message
    by id. Without this, cron-sent messages were "invisible" to the recall
    tool — that was Fix #6.
    """
    # Lazy import to avoid circular dependency — adapter.py imports this module.
    from .adapter import InfoflowAdapter
    from .settings import _read_account_settings

    settings = _read_account_settings(pconfig)
    account = _api.InfoflowAccountAPI(
        api_host=settings["api_host"],
        app_key=settings["app_key"],
        app_secret=settings["app_secret"],
        app_agent_id=settings["app_agent_id"],
    )
    if not (account.api_host and account.app_key and account.app_secret):
        return {"error": "Infoflow standalone send: INFOFLOW_API_HOST/APP_KEY/APP_SECRET are required"}

    kind, group_id, dm_user = InfoflowAdapter._parse_target(chat_id)
    # Build content items from message + metadata (supports @-mentions).
    if kind == "group" and metadata:
        # Reuse the adapter's _build_contents for metadata handling (@-mentions).
        # We instantiate a minimal dummy so the staticmethod can be reached
        # without a full adapter instance.
        contents = InfoflowAdapter._build_contents(message, metadata)
    else:
        is_markdown = InfoflowAdapter._looks_like_markdown(message)
        contents = [_api.ContentItem("markdown" if is_markdown else "text", message)]

    try:
        if kind == "group":
            if group_id is None:
                return {"error": "Infoflow standalone send: invalid group target"}
            res = await _api.send_group_message(
                account,
                group_id=group_id,
                contents=contents,
            )
        else:
            res = await _api.send_private_message(account, to_user=dm_user, contents=contents)
    except Exception as exc:
        return {"error": f"Infoflow standalone send failed: {exc}"}

    if not res.get("ok"):
        return {"error": res.get("error") or "send failed"}
    mid = res.get("messageid") if res.get("messageid") else res.get("msgkey")
    msgseq = res.get("msgseqid") or ""

    # Persist for cross-process recall — same DB the adapter would use.
    # Normalize chat_id so the in-process adapter's lookups still match
    # cron-process inserts even if the caller used an ``infoflow:`` prefix.
    if mid:
        try:
            store = SentMessageStore(
                db_path=Path(settings["state_dir"]) / "infoflow" / "sent-messages.db",
                account_id=settings.get("app_key") or "default",
            )
            store.record(
                chat_id=InfoflowAdapter._normalize_chat_id(chat_id),
                messageid=str(mid),
                msgseqid=str(msgseq) if msgseq else "",
                digest=message[:80],
            )
        except Exception:
            logger.debug("standalone_send: sent-store persist failed", exc_info=True)

    return {"success": True, "message_id": str(mid) if mid else None}
