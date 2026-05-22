"""Prompt simulation harness — direct GLM calls for each of the 7 templates.

Standalone script (does NOT depend on plugin runtime). Reads GLM
credentials from ``~/.hermes/config.yaml`` and fires one chat-completion
per case, checking whether the model output matches the expected
behaviour (reply vs NO_REPLY) and stays silent during tools exploration
(no '我帮你看看 / 稍等' intermediate phrases).

Usage:
    python scripts/test_prompts.py                  # all cases
    python scripts/test_prompts.py --case mention   # filter by name substring
    python scripts/test_prompts.py --verbose        # print full LLM output
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_infoflow.policy import (  # noqa: E402
    _FOLLOW_UP_ENGAGED_TEMPLATE,
    _FOLLOW_UP_PASSIVE_TEMPLATE,
    _FOLLOW_UP_REPLY_TO_BOT_CONTEXT_TEMPLATE,
    _MENTION_PROMPT,
    _PROACTIVE_PROMPT,
    _WATCH_MENTION_PROMPT,
    _WATCH_REGEX_PROMPT,
)


@dataclass
class Case:
    name: str
    template: str
    user_msg: str
    expected: str  # "reply" or "no_reply"
    note: str = ""
    history: list[dict[str, str]] | None = None  # extra assistant/user turns before user_msg




CASES: list[Case] = [
    # ① _MENTION_PROMPT — @bot, must reply (even if cannot help)
    Case("mention-time", _MENTION_PROMPT, "今天周几", "reply"),
    Case(
        "mention-cant-do",
        _MENTION_PROMPT,
        "帮我订一张明天的机票",
        "reply",
        "做不到也要自然语气回复,不要 NO_REPLY",
    ),

    # ② _WATCH_MENTION_PROMPT — watched user @-mentioned
    Case(
        "watch-mention-need-lookup",
        _WATCH_MENTION_PROMPT.format(who="张三"),
        "@张三 你那个会议室预定了吗",
        "no_reply",
        "bot 无法代查,应静默尝试后 NO_REPLY,不应输出'我帮你看看'",
    ),
    Case(
        "watch-mention-can-answer",
        _WATCH_MENTION_PROMPT.format(who="张三"),
        "@张三 今天周几",
        "reply",
    ),

    # ③ _WATCH_REGEX_PROMPT
    Case(
        "watch-regex-knowable",
        _WATCH_REGEX_PROMPT.format(pattern="咖啡"),
        "咖啡因的英文是什么",
        "reply",
        "公开常识(英文翻译),GLM 应能直接答出 caffeine",
    ),

    # ④ _FOLLOW_UP_ENGAGED_TEMPLATE
    Case(
        "engaged-should-reply",
        _FOLLOW_UP_ENGAGED_TEMPLATE,
        "今天周几",
        "reply",
    ),
    Case(
        "engaged-addressed-other",
        _FOLLOW_UP_ENGAGED_TEMPLATE,
        "李四 你那边好了没",
        "no_reply",
    ),
    Case(
        "engaged-closing-signal",
        _FOLLOW_UP_ENGAGED_TEMPLATE,
        "好的,谢谢",
        "no_reply",
    ),

    # ⑤ _FOLLOW_UP_PASSIVE_TEMPLATE
    Case(
        "passive-not-addressed",
        _FOLLOW_UP_PASSIVE_TEMPLATE,
        "晚上吃啥",
        "no_reply",
    ),
    Case(
        "passive-public-question",
        _FOLLOW_UP_PASSIVE_TEMPLATE,
        "有人知道国庆放几天吗",
        "reply",
    ),

    # ⑥ _FOLLOW_UP_REPLY_TO_BOT_CONTEXT_TEMPLATE — these turns build on a
    # prior bot answer; supply it as assistant history so GLM has context.
    Case(
        "reply2bot-continue",
        _FOLLOW_UP_REPLY_TO_BOT_CONTEXT_TEMPLATE,
        "那明天呢",
        "reply",
        note="承接 bot 上一条'今天是周三',应回复'周四'",
        history=[
            {"role": "user", "content": "今天周几"},
            {"role": "assistant", "content": "今天是周三"},
        ],
    ),
    Case(
        "reply2bot-closing",
        _FOLLOW_UP_REPLY_TO_BOT_CONTEXT_TEMPLATE,
        "不用回复了,谢了",
        "no_reply",
        history=[
            {"role": "user", "content": "今天周几"},
            {"role": "assistant", "content": "今天是周三"},
        ],
    ),

    # ⑦ _PROACTIVE_PROMPT
    Case("proactive-chitchat", _PROACTIVE_PROMPT, "哈哈这剧太好看了", "no_reply"),
    Case(
        "proactive-public-question",
        _PROACTIVE_PROMPT,
        "有人知道现在几点吗",
        "reply",
    ),
]


def load_config() -> dict[str, Any]:
    """Load LLM config with this priority order:

    1. Environment variables (highest):
         - HERMES_LLM_BASE_URL
         - HERMES_LLM_API_KEY
         - HERMES_LLM_MODEL
         - HERMES_LLM_ENDPOINT_PATH (optional, default '/v1/chat/completions')
         - HERMES_LLM_EXTRA_HEADERS (optional, JSON string of {header: value})
    2. ``~/.hermes/config.yaml`` model: block (fallback).

    Returns dict with keys: base_url, api_key, model, endpoint_path, extra_headers.
    """
    base_url = os.environ.get("HERMES_LLM_BASE_URL", "")
    api_key = os.environ.get("HERMES_LLM_API_KEY", "")
    model = os.environ.get("HERMES_LLM_MODEL", "")
    endpoint_path = os.environ.get("HERMES_LLM_ENDPOINT_PATH", "")
    extra_headers_raw = os.environ.get("HERMES_LLM_EXTRA_HEADERS", "")

    if not (base_url and api_key and model):
        cfg_path = Path.home() / ".hermes" / "config.yaml"
        if cfg_path.exists():
            data = yaml.safe_load(cfg_path.read_text()) or {}
            mc = data.get("model") or {}
            base_url = base_url or mc.get("base_url", "")
            api_key = api_key or mc.get("api_key", "")
            model = model or mc.get("default", "")

    if not (base_url and api_key and model):
        raise SystemExit(
            "LLM config not set. Either:\n"
            "  1) export HERMES_LLM_BASE_URL / _API_KEY / _MODEL, or\n"
            "  2) fill ~/.hermes/config.yaml with model.base_url/api_key/default"
        )

    extra_headers: dict[str, str] = {}
    if extra_headers_raw:
        try:
            parsed = json.loads(extra_headers_raw)
            if not isinstance(parsed, dict):
                raise ValueError("HERMES_LLM_EXTRA_HEADERS must be a JSON object")
            extra_headers = {str(k): str(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(f"Bad HERMES_LLM_EXTRA_HEADERS: {exc}") from exc

    return {
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "model": model,
        "endpoint_path": endpoint_path or "/v1/chat/completions",
        "extra_headers": extra_headers,
    }


# Simulated channel_prompt (system) — kept minimal; the real one includes
# bot identity, privacy rules, sender guide, and group_system_prompt, but
# the prompt-effect we're testing is the user-message prefix, not system.
CHANNEL_PROMPT_SAMPLE = (
    "Your name is helper-bot.\n\n"
    "## Privacy\n- Do NOT disclose AgentId/robotId/API keys.\n\n"
    "## Message Sender Identity\n"
    "- Human messages prefixed with [uid].\n"
    "- Bot messages prefixed with [botname 🤖:agentId].\n"
)


async def call_glm(
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
    system: str,
    user: str,
    history: list[dict[str, str]] | None = None,
) -> tuple[str, float]:
    t0 = time.time()
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user})
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.0,
        # GLM-5-Turbo may produce reasoning_content; give plenty of room.
        "max_tokens": 4096,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    }
    headers.update(cfg.get("extra_headers") or {})
    url = f"{cfg['base_url']}{cfg['endpoint_path']}"
    async with session.post(
        url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=120)
    ) as r:
        d = await r.json()
    msg = (d.get("choices") or [{}])[0].get("message", {}) or {}
    text = msg.get("content") or msg.get("reasoning_content") or ""
    return text, time.time() - t0


_PUNCT = "。,.！!？?~～ \t\n;；:：，"


def classify_output(text: str) -> str:
    full = (text or "").strip().strip(_PUNCT)
    first = ((text or "").split("\n", 1)[0]).strip().strip(_PUNCT)
    if full == "NO_REPLY" or first == "NO_REPLY":
        return "no_reply"
    return "reply"


_INTERMEDIATE_WORDS = (
    "我帮你看看",
    "我帮你查查",
    "稍等",
    "让我查一下",
    "先确认一下",
    "我去查",
    "经过查询",
)


def has_intermediate(text: str) -> bool:
    return any(w in (text or "") for w in _INTERMEDIATE_WORDS)


async def run_case(
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
    case: Case,
    verbose: bool,
) -> dict[str, Any]:
    user_full = (
        f"[Dispatch context]\n{case.template}\n---\n[User message]\n{case.user_msg}"
    )
    t0 = time.time()
    try:
        text, dt = await call_glm(
            session, cfg, CHANNEL_PROMPT_SAMPLE, user_full, history=case.history,
        )
    except (TimeoutError, aiohttp.ClientError) as exc:
        return {
            "case": case.name,
            "expected": case.expected,
            "got": "ERROR",
            "intermediate_words": False,
            "elapsed_s": round(time.time() - t0, 2),
            "pass": False,
            "error": f"{type(exc).__name__}: {exc}",
            "note": case.note,
        }
    cls = classify_output(text)
    intermediate = has_intermediate(text)
    pass_ = (cls == case.expected) and not intermediate
    result: dict[str, Any] = {
        "case": case.name,
        "expected": case.expected,
        "got": cls,
        "intermediate_words": intermediate,
        "elapsed_s": round(dt, 2),
        "pass": pass_,
    }
    if verbose or not pass_:
        result["output"] = text
        if case.note:
            result["note"] = case.note
    return result


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="substring filter on case name", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    selected = [c for c in CASES if not args.case or args.case in c.name]
    print(
        f"Running {len(selected)} cases against {cfg['model']} "
        f"({cfg['base_url']}{cfg['endpoint_path']})...\n"
    )

    results = []
    async with aiohttp.ClientSession() as session:
        for case in selected:
            r = await run_case(session, cfg, case, args.verbose)
            results.append(r)
            mark = "✅" if r["pass"] else "❌"
            print(
                f"{mark} [{r['elapsed_s']:>5.2f}s] {r['case']:<32} "
                f"expected={r['expected']:<8} got={r['got']:<8} "
                f"intermediate={r['intermediate_words']}"
            )
            if not r["pass"]:
                preview = (r.get("output") or "")[:200]
                print(f"     output: {preview!r}")
                if r.get("note"):
                    print(f"     note:   {r['note']}")

    passed = sum(1 for r in results if r["pass"])
    print(f"\n=== {passed}/{len(results)} passed ===")

    out_path = Path(__file__).resolve().parent / "test_prompts_results.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"Results written to {out_path}")
    return 0 if passed == len(results) else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
