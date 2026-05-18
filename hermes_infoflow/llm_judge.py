"""Lightweight LLM judge for follow-up message classification and reply evaluation.

Used by bot.py to add two extra judgment layers around the main agent loop:

1. **Intent classification** (before dispatch): determine whether a follow-up
   message is addressed to the bot, to someone else, or to nobody.
2. **Reply value evaluation** (before send): determine whether the LLM's
   generated reply is worth sending.

Reads LLM config from ``~/.hermes/config.yaml`` to reuse the same model/endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config reader
# ---------------------------------------------------------------------------

def _load_llm_config() -> dict[str, str]:
    """Read the main LLM config from ``~/.hermes/config.yaml``.

    Returns dict with keys: ``base_url``, ``api_key``, ``model``, ``api_mode``.
    Falls back to env vars if config file is missing or incomplete.
    """
    import os

    config_path = Path.home() / ".hermes" / "config.yaml"
    cfg: dict[str, str] = {
        "base_url": os.environ.get("LLM_BASE_URL", ""),
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "model": os.environ.get("LLM_MODEL", ""),
        "api_mode": os.environ.get("LLM_API_MODE", "chat_completions"),
    }
    if config_path.exists():
        try:
            import yaml

            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
            mc = data.get("model") or {}
            if mc.get("base_url"):
                cfg["base_url"] = mc["base_url"]
            if mc.get("api_key"):
                cfg["api_key"] = mc["api_key"]
            if mc.get("default"):
                cfg["model"] = mc["default"]
            if data.get("api_mode"):
                cfg["api_mode"] = data["api_mode"]
        except Exception:
            logger.debug("[llm_judge] failed to read config.yaml, using env vars")
    return cfg


# ---------------------------------------------------------------------------
# Core LLM call
# ---------------------------------------------------------------------------

async def _llm_call(
    session: aiohttp.ClientSession,
    prompt: str,
    config: dict[str, str],
    max_tokens: int = 8192,
) -> str:
    """Single-round LLM call with no tools.  Returns raw text response."""
    base_url = config.get("base_url", "").rstrip("/")
    api_key = config.get("api_key", "")
    model = config.get("model", "")
    if not base_url or not api_key or not model:
        return ""

    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a classifier. Output ONLY JSON, nothing else."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning("[llm_judge] HTTP %s from %s", resp.status, url)
                return ""
            data = await resp.json()
            msg = (data.get("choices") or [{}])[0].get("message", {})
            # GLM-5-Turbo may put output in reasoning_content instead of content
            return msg.get("content") or msg.get("reasoning_content") or ""
    except Exception:
        logger.warning("[llm_judge] request failed", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Layer 1: Intent classification (before dispatch)
# ---------------------------------------------------------------------------

_INTENT_CLASSIFY_PROMPT = """\
Classify who this group chat message is addressed to. You are "__BOT_NAME__".

Message from "__SENDER_NAME__" (__SENDER_TYPE__): "__MSG_TEXT__"

Output JSON: {"target": "bot" | "other" | "none"}

Rules:
- "other": A specific person (not you) is being addressed. Patterns:
  • Name at the start: "李四 你吃了吗", "老王 帮我看看"
  • Name with punctuation: "张三，明天吃什么", "小刘：那个文件呢"
  • Name anywhere as addressee: "帮我问问李四", "跟老王说一下"
  • Even if the question is general, if a person's name appears as the target recipient → "other"
- "bot": The message addresses YOU (__BOT_NAME__). Patterns:
  • Uses your name or @mentions you
  • Asks for help that an AI assistant would provide, with no other addressee
  • Directly continues a conversation with you
- "none": No specific addressee. General comments, reactions, status updates.

IMPORTANT: If the message contains a human name as the recipient (even without punctuation between name and content), choose "other" — not "bot" or "none"."""


async def classify_followup_intent(
    session: aiohttp.ClientSession,
    *,
    text: str,
    sender_name: str,
    is_bot: bool,
    bot_name: str,
    config: dict[str, str],
) -> str | None:
    """Classify a follow-up message intent.

    Returns "bot", "other", or "none".  Returns None on failure (safe fallback: dispatch).
    """
    sender_type = "bot" if is_bot else "human"
    prompt = (
        _INTENT_CLASSIFY_PROMPT
        .replace("__BOT_NAME__", bot_name)
        .replace("__SENDER_NAME__", sender_name)
        .replace("__SENDER_TYPE__", sender_type)
        .replace("__MSG_TEXT__", text)
    )
    raw = await _llm_call(session, prompt, config)
    if not raw:
        return None

    # Extract JSON from response (tolerate markdown fences)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        obj = json.loads(cleaned)
        result = str(obj.get("target", "")).lower().strip()
        if result in ("bot", "other", "none"):
            return result
    except json.JSONDecodeError:
        pass

    # Fallback: search for the target keyword in raw text
    for kw in ("bot", "other", "none"):
        if f'"target": "{kw}"' in raw or f'"target":"{kw}"' in raw:
            return kw

    return None  # failed to parse → safe fallback


# ---------------------------------------------------------------------------
# Layer 3: Reply value evaluation (before send)
# ---------------------------------------------------------------------------

_REPLY_EVAL_TEMPLATE = """\
Evaluate whether this bot reply is worth sending in a group chat.

Original message: "__ORIG_TEXT__"
Bot reply: "__REPLY_TEXT__"

Reply with JSON only: {{"should_send": true | false, "reason": "brief reason"}}

Return false if:
- The reply is a refusal or inability statement ("我没法", "我无法", "我不知道", "我不确定", "我没法帮", "作为AI")
- The reply adds no information or value (just polite filler without substance)
- The reply is tangential to the original message
- The reply would be annoying or unwanted in a group chat context

Return true if the reply provides useful information, answers a question, or performs a helpful action.
"""  # noqa: E501


async def evaluate_reply_value(
    session: aiohttp.ClientSession,
    *,
    original_text: str,
    reply_text: str,
    config: dict[str, str],
) -> bool | None:
    """Evaluate whether a bot reply should be sent.

    Returns True (send), False (suppress), or None on failure (safe fallback: send).
    """
    # Use unique placeholders to avoid cross-replacement pollution
    prompt = (
        _REPLY_EVAL_TEMPLATE
        .replace("__ORIG_TEXT__", original_text[:500])
        .replace("__REPLY_TEXT__", reply_text[:500])
    )
    raw = await _llm_call(session, prompt, config)
    if not raw:
        return None  # failed → safe fallback: send

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        obj = json.loads(cleaned)
        return bool(obj.get("should_send", True))
    except json.JSONDecodeError:
        pass

    return None  # parse failed → safe fallback: send
