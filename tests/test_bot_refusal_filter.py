"""Tests for the zero-latency static refusal-regex filter in send_message.

This filter is the static, free fallback against the main agent
occasionally violating the NO_REPLY contract on the follow-up path.

Critical constraints (mirrored from the implementation):
- only applied when ``_send_path_cv == "followUp"``
- only applied for group messages (group_id is not None)
- only matches refusal phrases at the start of a line
- only checks the first 200 chars
"""

from __future__ import annotations

from hermes_infoflow.bot import _REFUSAL_RE


def _refusal_hits(text: str) -> bool:
    return _REFUSAL_RE.search((text or "")[:200]) is not None


# --- positive cases (should suppress on follow-up group path) ----------

def test_as_ai_refusal_matches() -> None:
    assert _refusal_hits("作为AI，我无法回答这个问题")
    assert _refusal_hits("作为一个AI，我没法帮你")


def test_short_refusal_matches() -> None:
    assert _refusal_hits("我无法处理这个请求")
    assert _refusal_hits("我没法帮你查到")
    assert _refusal_hits("我不能告诉你这个")


def test_apology_refusal_matches() -> None:
    assert _refusal_hits("抱歉，我目前无法回答这个")
    assert _refusal_hits("很抱歉，我无法处理")
    assert _refusal_hits("抱歉我目前帮不上")


def test_refusal_at_second_line_matches() -> None:
    """Sometimes GLM emits a one-line preamble then refuses — still catch it."""
    assert _refusal_hits("好的，让我看看\n我无法找到相关信息")


# --- negative cases (should NOT suppress) ------------------------------

def test_normal_answer_does_not_match() -> None:
    assert not _refusal_hits("今天是星期三")
    assert not _refusal_hits("会议室预定在三楼")
    assert not _refusal_hits("根据日历，下周三 10 点")


def test_refusal_phrase_mid_sentence_does_not_match() -> None:
    """Anchored to line-start, so 'X 无法' in the middle of a sentence is safe."""
    assert not _refusal_hits("这个方案无法在周末执行，但周一可以")
    assert not _refusal_hits("我看了一下他无法到场")


def test_refusal_past_200_chars_does_not_match() -> None:
    """Only the first 200 chars are checked, so a long detailed answer
    that happens to end with '我无法...' should still ship."""
    long_answer = "周三日历已确认。" * 30 + "我无法继续"
    assert len(long_answer) > 200
    assert not _refusal_hits(long_answer)


def test_helpful_response_with_unable_keyword_safe() -> None:
    """'暂时帮不上' starts the message but is a polite, helpful response on
    the @bot path. The regex would catch it, BUT in production the @bot
    path is gated by `_send_path_cv != "followUp"` and skips this filter
    entirely. Verify the path-gating logic exists in the source."""
    import inspect

    from hermes_infoflow import bot as bot_mod

    src = inspect.getsource(bot_mod)
    # Path gate: only followUp path triggers the refusal regex
    assert '_path == "followUp"' in src or 'path == "followUp"' in src
