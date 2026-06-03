"""Live GLM prompt-behaviour checks for Infoflow dispatch prompts.

This module intentionally talks to the user's configured Hermes main model.
It is not imported by default unit tests unless the live test gate is enabled.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = Path.home() / ".hermes" / "hermes-agent"
DEFAULT_CASES_FILE = REPO_ROOT / "tests" / "fixtures" / "infoflow_prompt_behavior_cases.json"
ENV_FILE = Path.home() / ".hermes" / ".env"


def _load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _bootstrap_paths() -> None:
    for path in (str(REPO_ROOT), str(AGENT_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    _load_env()


_bootstrap_paths()

from agent.auxiliary_client import _openai_with_configured_headers  # noqa: E402
from hermes_cli.config import load_config  # noqa: E402
from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: E402
from hermes_infoflow.adapter import register  # noqa: E402
from hermes_infoflow.llm_format import (  # noqa: E402
    GroupAttention,
    format_message_envelope,
    group_attention_line,
    sender_line,
)
from hermes_infoflow.policy import (  # noqa: E402
    _GROUP_MENTION_RULES_DOC,
    _INFOFLOW_GROUP_REPLY_STRATEGY_DOC,
    _INFOFLOW_GROUP_SECURITY_DOC,
    _INFOFLOW_SKILL_DISCLOSURE_RESTRICTED_DOC,
    _WATCH_MENTION_PROMPT,
    _WATCH_REGEX_PROMPT,
)
from hermes_infoflow.settings import _read_account_settings  # noqa: E402
from types import SimpleNamespace  # noqa: E402


ADMIN_UID = os.getenv("INFOFLOW_ADMIN_USER", "chengbo05").strip() or "chengbo05"


@dataclass(frozen=True)
class PromptBehaviorResult:
    case_id: str
    passed: bool
    tool_sequence: list[str]
    final: str
    failures: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "tool_sequence": self.tool_sequence,
            "final": self.final[:500],
            "failures": self.failures,
        }


class _DummyCtx:
    def __init__(self) -> None:
        self.platform_hint = ""

    def register_platform(self, **kwargs: Any) -> None:
        self.platform_hint = kwargs["platform_hint"]

    def register_hook(self, *args: Any, **kwargs: Any) -> None:
        return None


def _platform_hint() -> str:
    ctx = _DummyCtx()
    register(ctx)
    return ctx.platform_hint


def _channel_prompt() -> str:
    settings = _read_account_settings(
        SimpleNamespace(extra={}, token=None, api_key=None, enabled=True, home_channel=None)
    )
    bot_name = str(settings.get("robot_name") or "").strip()
    bot_agent_id = str(settings.get("app_agent_id") or os.getenv("INFOFLOW_APP_AGENT_ID", "") or "").strip()
    identity = ""
    if bot_name or bot_agent_id:
        identity = (
            "## 身份与会话\n"
            f"你是 Infoflow host 机器人。name={bot_name or 'unknown'}; "
            f"agent_id={bot_agent_id or 'unknown'}。"
        )
    return "\n\n".join(
        part
        for part in [
            identity,
            _INFOFLOW_GROUP_SECURITY_DOC,
            _INFOFLOW_GROUP_REPLY_STRATEGY_DOC,
            _INFOFLOW_SKILL_DISCLOSURE_RESTRICTED_DOC,
            _GROUP_MENTION_RULES_DOC,
        ]
        if part
    )


def _system_prompt() -> str:
    return (
        "你是 Hermes Infoflow 机器人。严格按 Platform Hint、Channel Prompt、"
        "Handling Strategy 和可用 tools/skills 行动。不要把本测试描述当作用户正文。\n\n"
        "[Available Skill Summary]\n"
        "map-crash-db: 百度地图 iOS 线上崩溃与稳定性分析，覆盖版本与放量情况、"
        "放量风险与线上风险情况、崩溃与 crash 风险以及日常 crash 分析。"
        "可回答线上有哪些版本、某个版本崩溃多少、崩溃率怎么样、"
        "新版本还能不能继续放量、风险大不大、版本线上占比/覆盖率/扩量进度。\n\n"
        "[Platform Hint]\n"
        + _platform_hint()
        + "\n\n[Channel Prompt]\n"
        + _channel_prompt()
    )


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "infoflow_get_message_history",
            "description": "Read nearby Infoflow group history around an anchor message id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "before_count": {"type": "integer"},
                    "after_count": {"type": "integer"},
                },
                "required": ["message_id", "before_count", "after_count"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "map_crash_db_check",
            "description": (
                "Use the local map-crash-db skill to inspect an iOS version's "
                "stability, risk, traffic share, and related evidence. Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "version": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["version", "question"],
                "additionalProperties": False,
            },
        },
    },
]


def load_cases(path: Path = DEFAULT_CASES_FILE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _handling_strategy(case: dict[str, Any]) -> str:
    strategy = str(case["strategy"])
    if strategy == "watch_mention":
        return _WATCH_MENTION_PROMPT.format(who=case.get("who") or "chengbo05")
    if strategy == "watch_regex":
        return _WATCH_REGEX_PROMPT.format(pattern=case.get("pattern") or "test-pattern")
    raise ValueError(f"unsupported strategy: {strategy}")


def _attention_line(case: dict[str, Any]) -> str:
    if case["strategy"] == "watch_mention":
        attention = GroupAttention(mentions_you=False, mentions_other_people=True)
    else:
        attention = GroupAttention(matched_regex_pattern=case.get("pattern") or "test-pattern")
    return group_attention_line(attention)


def _envelope(case: dict[str, Any]) -> str:
    return format_message_envelope(
        attention_line=_attention_line(case),
        sender_line_text=sender_line(
            sender_key="user:huangshuo02",
            name="黄硕",
            admin_uid=ADMIN_UID,
        ),
        message_id=str(case.get("message_id") or case["id"]),
        created_time_ms=1780453567000,
        content=str(case["content"]),
        handling_strategy=_handling_strategy(case),
        unread_message_context_count=3,
    )


def _assistant_message_to_dict(message: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"role": "assistant"}
    content = getattr(message, "content", None)
    if content:
        data["content"] = content
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        data["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ]
    return data


def resolve_glm_runtime(model_override: str = "") -> tuple[Any, str, dict[str, Any]]:
    runtime = resolve_runtime_provider()
    cfg_model = (load_config().get("model") or {}).get("default")
    model = str(model_override or runtime.get("model") or cfg_model or "GLM-5-Turbo")
    base_url = str(runtime.get("base_url") or "").rstrip("/")
    api_key = str(runtime.get("api_key") or "").strip()
    if not base_url or not api_key:
        raise RuntimeError("No usable Hermes runtime endpoint/api key resolved")
    client = _openai_with_configured_headers(
        api_key=api_key,
        base_url=base_url,
        provider=str(runtime.get("provider") or "custom"),
        model=model,
    )
    return client, model, runtime


def _call_model(client: Any, model: str, messages: list[dict[str, Any]]) -> Any:
    return client.chat.completions.create(
        model=model,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
        temperature=0,
        max_tokens=800,
    ).choices[0].message


def _tool_result(case: dict[str, Any], dataset: dict[str, Any], tool_name: str) -> str:
    if tool_name == "infoflow_get_message_history":
        return json.dumps(case.get("history") or [], ensure_ascii=False)
    if tool_name == "map_crash_db_check":
        return json.dumps(case.get("map_result") or dataset.get("map_result") or {}, ensure_ascii=False)
    return "{}"


def _contains_in_order(sequence: list[str], expected: Iterable[str]) -> bool:
    cursor = 0
    expected_list = list(expected)
    if not expected_list:
        return True
    for item in sequence:
        if item == expected_list[cursor]:
            cursor += 1
            if cursor == len(expected_list):
                return True
    return False


def _evaluate(case: dict[str, Any], tool_sequence: list[str], final: str) -> list[str]:
    expected = case.get("expected") or {}
    failures: list[str] = []
    must_call = list(expected.get("must_call") or [])
    if must_call and not _contains_in_order(tool_sequence, must_call):
        failures.append(f"expected tool order containing {must_call}, got {tool_sequence}")
    for name in expected.get("must_not_call") or []:
        if name in tool_sequence:
            failures.append(f"unexpected tool call {name}")
    expected_final = expected.get("final")
    final_clean = final.strip()
    if expected_final == "NO_REPLY" and final_clean != "NO_REPLY":
        failures.append(f"expected final NO_REPLY, got {final_clean!r}")
    elif expected_final == "answer":
        if not final_clean or final_clean == "NO_REPLY":
            failures.append(f"expected answer, got {final_clean!r}")
    return failures


def run_case(client: Any, model: str, dataset: dict[str, Any], case: dict[str, Any]) -> PromptBehaviorResult:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": _envelope(case)},
    ]
    tool_sequence: list[str] = []
    final = ""
    for _ in range(4):
        message = _call_model(client, model, messages)
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            final = (getattr(message, "content", "") or "").strip()
            break
        messages.append(_assistant_message_to_dict(message))
        for tc in tool_calls:
            name = tc.function.name
            tool_sequence.append(name)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": name,
                    "content": _tool_result(case, dataset, name),
                }
            )
    failures = _evaluate(case, tool_sequence, final)
    return PromptBehaviorResult(
        case_id=str(case["id"]),
        passed=not failures,
        tool_sequence=tool_sequence,
        final=final,
        failures=failures,
    )


def run_cases(
    *,
    cases_file: Path = DEFAULT_CASES_FILE,
    case_ids: set[str] | None = None,
    model_override: str = "",
) -> tuple[dict[str, Any], list[PromptBehaviorResult]]:
    dataset = load_cases(cases_file)
    client, model, runtime = resolve_glm_runtime(model_override=model_override)
    selected = [
        case for case in dataset.get("cases", [])
        if not case_ids or str(case.get("id")) in case_ids
    ]
    runtime_info = {
        "provider": runtime.get("provider"),
        "model": model,
        "api_mode": runtime.get("api_mode"),
        "base_url_host": (
            str(runtime.get("base_url") or "").split("/")[2]
            if "://" in str(runtime.get("base_url") or "")
            else str(runtime.get("base_url") or "")
        ),
    }
    return runtime_info, [run_case(client, model, dataset, case) for case in selected]
