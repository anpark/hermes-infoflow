"""Standalone (out-of-process) message sender for cron / CLI usage."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

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
    from .outbound import prepare_outbound_message
    from .serverapi import ServerAPI
    from .settings import _read_account_settings

    settings = _read_account_settings(pconfig)
    if not (settings["api_host"] and settings["app_key"] and settings["app_secret"]):
        return {"error": "Infoflow standalone send: INFOFLOW_API_HOST/APP_KEY/APP_SECRET are required"}

    serverapi = ServerAPI(settings=settings)
    kind, group_id, dm_user = InfoflowAdapter._parse_target(chat_id)
    prepared_message, options = await prepare_outbound_message(
        message,
        group_id=str(group_id) if kind == "group" and group_id is not None else None,
        metadata=metadata,
        get_group_members=serverapi.get_group_members,
        bot_agent_id=settings.get("app_agent_id"),
    )

    try:
        if kind == "group":
            if group_id is None:
                return {"error": "Infoflow standalone send: invalid group target"}
            result = await serverapi.send_to_group(
                str(group_id),
                prepared_message,
                options=options,
            )
        else:
            result = await serverapi.send_to_dm(dm_user, prepared_message, options=options)
    except Exception as exc:
        return {"error": f"Infoflow standalone send failed: {exc}"}

    if not result.success:
        return {"error": result.error or "send failed"}
    mid = result.message_id
    msgseq = result.msgseqid

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
                digest=prepared_message[:80],
            )
        except Exception:
            logger.debug("standalone_send: sent-store persist failed", exc_info=True)

    return {"success": True, "message_id": str(mid) if mid else None}
