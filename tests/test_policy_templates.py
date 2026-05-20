"""Contract tests for the 7 merged dispatch/follow-up prompt templates.

After collapsing the legacy 3-LLM pipeline (intent classification + main
agent + reply value evaluation) into a single main-agent call, all
behavioural rules now live inside the per-path prompt templates. These
tests guard against silent regressions: each template must keep its
key NO_REPLY contract and silent-tools-exploration directives.
"""

from __future__ import annotations

import pytest

from hermes_infoflow.policy import (
    _FOLLOW_UP_ENGAGED_TEMPLATE,
    _FOLLOW_UP_PASSIVE_TEMPLATE,
    _FOLLOW_UP_REPLY_TO_BOT_CONTEXT_TEMPLATE,
    _MENTION_PROMPT,
    _PROACTIVE_PROMPT,
    _WATCH_MENTION_PROMPT,
    _WATCH_REGEX_PROMPT,
)

# Render templates with placeholder values so {var} substitution doesn't
# pollute the assertions below.
RENDERED = {
    "mention": _MENTION_PROMPT,
    "watch_mention": _WATCH_MENTION_PROMPT.format(who="张三"),
    "watch_regex": _WATCH_REGEX_PROMPT.format(pattern="咖啡"),
    "proactive": _PROACTIVE_PROMPT,
    "engaged": _FOLLOW_UP_ENGAGED_TEMPLATE.format(sender_label="x (uid, human)"),
    "passive": _FOLLOW_UP_PASSIVE_TEMPLATE.format(sender_label="x (uid, human)"),
    "reply2bot": _FOLLOW_UP_REPLY_TO_BOT_CONTEXT_TEMPLATE.format(
        sender_label="x (uid, human)"
    ),
}


@pytest.mark.parametrize("name,text", list(RENDERED.items()))
def test_template_mentions_no_reply_token(name: str, text: str) -> None:
    """Every template must reference the NO_REPLY sentinel — that's the
    only way the main agent can suppress an outbound message."""
    assert "NO_REPLY" in text, f"{name} template lost NO_REPLY"


@pytest.mark.parametrize(
    "name",
    ["watch_mention", "watch_regex", "engaged", "passive", "reply2bot", "proactive"],
)
def test_template_requests_silent_tools(name: str) -> None:
    """Paths that may want to call tools must say so explicitly AND demand
    silence (no '我帮你看看 / 稍等' intermediate messages)."""
    text = RENDERED[name]
    assert "tools" in text, f"{name} lost the tools-allowed directive"
    assert "静默" in text or "不发" in text, (
        f"{name} lost the silent-exploration directive"
    )


@pytest.mark.parametrize(
    "name",
    ["watch_mention", "watch_regex", "passive", "reply2bot"],
)
def test_template_blocks_refusal_outputs(name: str) -> None:
    """Templates that funnel through value-filtering must explicitly list
    refusal patterns ('我没法 / 我无法 / 作为AI') so the main agent rewrites
    them to NO_REPLY rather than shipping low-value text."""
    text = RENDERED[name]
    assert "作为AI" in text or "我无法" in text or "我没法" in text, (
        f"{name} lost refusal-pattern guidance"
    )


def test_mention_path_forbids_no_reply() -> None:
    """① @bot path is the one exception: the bot must always reply, even if
    the answer is '暂时帮不上'. The template must explicitly forbid NO_REPLY."""
    assert "不要" in _MENTION_PROMPT and "NO_REPLY" in _MENTION_PROMPT


def test_passive_template_keeps_recipient_gate() -> None:
    """⑤ passive is the strictest path — recipient gate must precede any
    tool-call permission (otherwise we waste tool budget on irrelevant msgs)."""
    text = RENDERED["passive"]
    assert text.index("第一步") < text.index("第二步")
    assert "门槛" in text or "不调任何 tools" in text
