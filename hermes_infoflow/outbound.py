"""Shared outbound message preparation helpers."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from .itypes import SendOptions

logger = logging.getLogger(__name__)

# Match @xxx where xxx is 1-30 chars excluding @, space, newline,
# followed by whitespace or end-of-string.
_AT_RE = re.compile(r"@([^\s@\n]{1,30})(?=[\s]|$)")


def _coerce_csv(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(x) for x in value if x)
    return str(value or "")


def options_from_metadata(metadata: dict[str, Any] | None) -> SendOptions:
    """Translate Hermes send metadata into bot-layer send options."""
    options = SendOptions()
    if not metadata:
        return options

    options.at_all = bool(metadata.get("at_all"))
    options.mention_user_ids = _coerce_csv(metadata.get("mention_user_ids"))
    options.mention_agent_ids = _coerce_csv(metadata.get("mention_agent_ids"))
    return options


def _at_iter(text: str) -> list[tuple[str, int, int]]:
    """Return list of (full_match, start, end) for each @mention in text."""
    results: list[tuple[str, int, int]] = []
    for match in _AT_RE.finditer(text):
        if match.start() > 0 and text[match.start() - 1] not in " \t\r\n":
            continue
        results.append((match.group(0), match.start(), match.end()))
    return results


def extract_mentions(
    text: str,
    members: list[Any] | None,
    *,
    bot_agent_id: int | None = None,
) -> tuple[list[str], list[int], bool, list[str], str]:
    """Extract @ mentions from text and resolve them against group members.

    Returns (user_ids, agent_ids, at_all, unmatched, modified_text).

    When *bot_agent_id* is provided, mentions resolving to the bot itself
    (either by display name or by literal agentId) are dropped silently:
    no rewrite to ``@<agentId>``, no entry in ``agent_ids``, no entry in
    ``unmatched`` — the original ``@<name>`` stays as plain text. This
    avoids Infoflow's server-side "被@机器人不能包含自身" rejection.
    """
    user_ids: list[str] = []
    agent_ids: list[int] = []
    at_all = False
    unmatched: list[str] = []

    human_uids: set[str] | None = None
    bot_aids: set[int] | None = None
    bot_name_map: dict[str, int] | None = None
    seen_users: set[str] = set()
    seen_agents: set[int] = set()

    if members:
        human_uids = {mb.uid for mb in members if not mb.is_bot}
        bot_aids = {mb.agent_id for mb in members if mb.is_bot}
        bot_name_map = {
            mb.name.lower(): mb.agent_id
            for mb in members
            if mb.is_bot and mb.name
        }

    replacements: list[tuple[int, int, str]] = []
    for match_text, start, end in _at_iter(text):
        mention_lower = match_text[1:].lower()
        if mention_lower in ("所有人", "all"):
            at_all = True
            continue

        name_part = match_text[1:]
        if name_part.isdigit():
            agent_id = int(name_part)
            if agent_id in seen_agents:
                continue
            if bot_agent_id is not None and agent_id == bot_agent_id:
                logger.info(
                    "[iflow:send] dropping self @-mention by agentId=%s", agent_id,
                )
                continue
            if bot_aids is not None and agent_id in bot_aids:
                agent_ids.append(agent_id)
                seen_agents.add(agent_id)
            else:
                unmatched.append(name_part)
        else:
            if name_part in seen_users:
                continue
            if human_uids is not None and name_part in human_uids:
                user_ids.append(name_part)
                seen_users.add(name_part)
            elif bot_name_map is not None and mention_lower in bot_name_map:
                agent_id = bot_name_map[mention_lower]
                if bot_agent_id is not None and agent_id == bot_agent_id:
                    logger.info(
                        "[iflow:send] dropping self @-mention by name=%r", name_part,
                    )
                    continue
                if agent_id not in seen_agents:
                    agent_ids.append(agent_id)
                    seen_agents.add(agent_id)
                replacements.append((start, end, f"@{agent_id}"))
            else:
                unmatched.append(name_part)

    for start, end, new_text in sorted(replacements, reverse=True):
        text = text[:start] + new_text + text[end:]

    return user_ids, agent_ids, at_all, unmatched, text


def _merge_options(
    options: SendOptions,
    *,
    user_ids: list[str],
    agent_ids: list[int],
    at_all: bool,
) -> None:
    if at_all:
        options.at_all = True

    existing_users = {
        item.strip() for item in options.mention_user_ids.split(",") if item.strip()
    }
    for user_id in user_ids:
        if user_id not in existing_users:
            existing_users.add(user_id)
            options.mention_user_ids = (
                f"{options.mention_user_ids},{user_id}"
                if options.mention_user_ids
                else user_id
            )

    existing_agents = {
        int(item.strip())
        for item in options.mention_agent_ids.split(",")
        if item.strip() and item.strip().isdigit()
    }
    for agent_id in agent_ids:
        if agent_id not in existing_agents:
            existing_agents.add(agent_id)
            options.mention_agent_ids = (
                f"{options.mention_agent_ids},{agent_id}"
                if options.mention_agent_ids
                else str(agent_id)
            )


GetGroupMembers = Callable[..., Awaitable[list[Any]]]


def _strip_self_from_agent_csv(csv: str, bot_agent_id: int | None) -> str:
    """Remove ``bot_agent_id`` from a comma-separated agentId list (if present)."""
    if not bot_agent_id or not csv:
        return csv
    kept: list[str] = []
    dropped = False
    for raw in csv.split(","):
        item = raw.strip()
        if not item:
            continue
        if item.isdigit() and int(item) == bot_agent_id:
            dropped = True
            continue
        kept.append(item)
    if dropped:
        logger.info(
            "[iflow:send] dropping self @-mention from options (agentId=%s)",
            bot_agent_id,
        )
    return ",".join(kept)


async def prepare_outbound_message(
    text: str,
    *,
    group_id: str | None,
    metadata: dict[str, Any] | None,
    get_group_members: GetGroupMembers | None = None,
    session: Any = None,
    bot_agent_id: int | None = None,
) -> tuple[str, SendOptions]:
    """Build send options and normalize text for all outbound entry points.

    *bot_agent_id* is the running bot's own ``agentId``. When provided, any
    @-mention that resolves to this id (via text or metadata) is dropped:
    the original text is preserved, but no ``at-agent`` ContentItem is built
    for self — Infoflow rejects "bot @ self" with a hard error.
    """
    options = options_from_metadata(metadata)
    options.mention_agent_ids = _strip_self_from_agent_csv(
        options.mention_agent_ids, bot_agent_id,
    )
    if group_id is None or not text or get_group_members is None:
        return text, options

    try:
        members = await get_group_members(str(group_id), session=session)
        user_ids, agent_ids, at_all, unmatched, text = extract_mentions(
            text, members, bot_agent_id=bot_agent_id,
        )

        if unmatched:
            members = await get_group_members(
                str(group_id),
                force_refresh=True,
                session=session,
            )
            for mention in list(unmatched):
                if mention.isdigit():
                    agent_id = int(mention)
                    if bot_agent_id is not None and agent_id == bot_agent_id:
                        unmatched.remove(mention)
                        continue
                    if any(member.is_bot and member.agent_id == agent_id for member in members):
                        if agent_id not in agent_ids:
                            agent_ids.append(agent_id)
                        unmatched.remove(mention)
                else:
                    if any(member.uid == mention for member in members if not member.is_bot):
                        if mention not in user_ids:
                            user_ids.append(mention)
                        unmatched.remove(mention)
            if unmatched:
                logger.info(
                    "[iflow:send] @ mentions discarded (no member match): %s",
                    unmatched,
                )

        _merge_options(
            options,
            user_ids=user_ids,
            agent_ids=agent_ids,
            at_all=at_all,
        )
    except Exception as exc:
        logger.warning("[iflow:send] @ mention extraction failed: %s", exc)

    return text, options
