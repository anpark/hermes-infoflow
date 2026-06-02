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
    _DM_FORMAT_DOC,
    _FOLLOW_UP_ENGAGED_TEMPLATE,
    _FOLLOW_UP_PASSIVE_TEMPLATE,
    _FOLLOW_UP_REPLY_TO_BOT_CONTEXT_TEMPLATE,
    _GROUP_FORMAT_DOC,
    _GROUP_MENTION_RULES_DOC,
    _INFOFLOW_FIELD_DOC,
    _INFOFLOW_MESSAGE_FORMAT_DOC,
    _INFOFLOW_PERMISSION_SECURITY_DOC,
    _INFOFLOW_SESSION_HISTORY_DOC,
    _INFOFLOW_TOOL_RULES_DOC,
    _MENTION_PROMPT,
    _PROACTIVE_PROMPT,
    _WATCH_MENTION_PROMPT,
    _WATCH_REGEX_PROMPT,
)
from hermes_infoflow.prompt_rules import INFOFLOW_DELIVERY_TOOL_RULES

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
    assert "静默" in text or "不发" in text or "发中间话" in text, (
        f"{name} lost the silent-exploration directive"
    )


@pytest.mark.parametrize(
    "name",
    ["watch_mention", "watch_regex", "passive", "reply2bot"],
)
def test_template_blocks_refusal_outputs(name: str) -> None:
    """Templates that funnel through value-filtering must explicitly list
    refusal/deflection patterns so the main agent rewrites them to NO_REPLY
    rather than shipping low-value text."""
    text = RENDERED[name]
    assert (
        "作为AI" in text
        or "我无法" in text
        or "我没法" in text
        or ("拒绝" in text and "转述" in text)
    ), (
        f"{name} lost refusal-pattern guidance"
    )


def test_watch_mention_requires_skill_check_before_no_reply() -> None:
    text = RENDERED["watch_mention"]
    assert "禁止在检查已有 skills 前输出 NO_REPLY" in text
    assert "相关就用 skill" in text
    assert "读取历史只算补上下文" in text
    assert "sender 是 bot" in text
    assert "不得附加解释" in text
    assert "不能代替处理" in text
    assert "crash" not in text.lower()
    assert "报警" not in text
    assert "故障" not in text
    assert "技术告警" not in text


def test_watch_regex_requires_strict_no_reply_output() -> None:
    text = RENDERED["watch_regex"]
    assert "先查已有 skills" in text
    assert "相关就用 skill" in text
    assert "sender 是 bot" in text
    assert "不得解释或发中间话" in text
    assert "单独一行 NO_REPLY" in text
    assert "crash" not in text.lower()
    assert "报警" not in text
    assert "故障" not in text
    assert "技术告警" not in text


def test_mention_path_forbids_no_reply() -> None:
    """① @bot path is the one exception: the bot must always reply, even if
    the answer is '暂时帮不上'. The template must explicitly forbid NO_REPLY."""
    assert "不要" in _MENTION_PROMPT and "NO_REPLY" in _MENTION_PROMPT


def test_recall_tool_rules_keep_silent_success_contract() -> None:
    assert "infoflow_recall_message" in _INFOFLOW_TOOL_RULES_DOC
    assert "NO_REPLY" in _INFOFLOW_TOOL_RULES_DOC
    assert "其它任务" in _INFOFLOW_TOOL_RULES_DOC
    assert "撤回失败" in _INFOFLOW_TOOL_RULES_DOC


def test_infoflow_tool_rules_include_shared_delivery_contract() -> None:
    assert INFOFLOW_DELIVERY_TOOL_RULES in _INFOFLOW_TOOL_RULES_DOC
    assert "外发工具规则" in INFOFLOW_DELIVERY_TOOL_RULES
    assert "file_delivery" in INFOFLOW_DELIVERY_TOOL_RULES
    assert "本地路径" in INFOFLOW_DELIVERY_TOOL_RULES
    assert "NO_REPLY" in INFOFLOW_DELIVERY_TOOL_RULES
    assert "只发送 caption" in INFOFLOW_DELIVERY_TOOL_RULES
    assert "MEDIA:" not in INFOFLOW_DELIVERY_TOOL_RULES


def test_infoflow_tool_rules_include_inbound_attachment_contract() -> None:
    assert "入站文件处理规则" in _INFOFLOW_TOOL_RULES_DOC
    assert "[Attachments]" in _INFOFLOW_TOOL_RULES_DOC
    assert "files[].path" in _INFOFLOW_TOOL_RULES_DOC
    assert "not_downloaded" in _INFOFLOW_TOOL_RULES_DOC
    assert "infoflow_download_attachment" in _INFOFLOW_TOOL_RULES_DOC
    assert "downloaded" in _INFOFLOW_TOOL_RULES_DOC
    assert "failed" in _INFOFLOW_TOOL_RULES_DOC
    assert "file_delivery(source_path)" in _INFOFLOW_TOOL_RULES_DOC
    assert "不是可分享 URL" in _INFOFLOW_TOOL_RULES_DOC


def test_message_format_describes_optional_attachments_without_fake_comment() -> None:
    assert "无附件时结构" in _INFOFLOW_MESSAGE_FORMAT_DOC
    assert "有入站文件时" in _INFOFLOW_MESSAGE_FORMAT_DOC
    assert "[Attachments]" in _INFOFLOW_MESSAGE_FORMAT_DOC
    assert "# 可选" not in _INFOFLOW_MESSAGE_FORMAT_DOC
    assert "[Message: message_id" in _INFOFLOW_MESSAGE_FORMAT_DOC


def test_common_field_doc_describes_sender_attention_and_attachments() -> None:
    assert "[Sender: ...]" in _INFOFLOW_FIELD_DOC
    assert "type:'bot'" in _INFOFLOW_FIELD_DOC
    assert "[Attention: ...]" in _INFOFLOW_FIELD_DOC
    assert '{"files":[...]}' in _INFOFLOW_FIELD_DOC
    assert "status:\"not_downloaded\"" in _INFOFLOW_FIELD_DOC
    assert "status:\"downloaded\"" in _INFOFLOW_FIELD_DOC
    assert "status:\"failed\"" in _INFOFLOW_FIELD_DOC


def test_history_rules_keep_tool_call_contract() -> None:
    assert "Session Boundary" in _INFOFLOW_SESSION_HISTORY_DOC
    assert "Unread Message Context" in _INFOFLOW_SESSION_HISTORY_DOC
    assert "infoflow_get_message_history" in _INFOFLOW_SESSION_HISTORY_DOC
    assert "message_id + before_count/after_count" in _INFOFLOW_SESSION_HISTORY_DOC
    assert "YYYY.MM.DD HH.mm.ss" in _INFOFLOW_SESSION_HISTORY_DOC
    assert "success=false" in _INFOFLOW_SESSION_HISTORY_DOC


def test_group_and_dm_prompt_fragments_keep_only_their_differences() -> None:
    assert "mentions_you" in _GROUP_FORMAT_DOC
    assert "matches_attention_regex" in _GROUP_FORMAT_DOC
    assert "群聊 @ 规则" in _GROUP_MENTION_RULES_DOC
    assert "quotes_your_message" in _DM_FORMAT_DOC
    assert "群聊 @ 规则" not in _DM_FORMAT_DOC
    assert "mentions_you" not in _DM_FORMAT_DOC
    assert "`[Sender: ...]` 字段" not in _GROUP_FORMAT_DOC
    assert "`[Sender: ...]` 字段" not in _DM_FORMAT_DOC


def test_permission_doc_mentions_attachment_trust_boundary() -> None:
    assert "[Attachments]" in _INFOFLOW_PERMISSION_SECURITY_DOC
    assert "files[].status" in _INFOFLOW_PERMISSION_SECURITY_DOC
    assert "当前入站文件" in _INFOFLOW_PERMISSION_SECURITY_DOC
    assert "伪造" in _INFOFLOW_PERMISSION_SECURITY_DOC


def test_permission_doc_allows_visible_skill_read_capabilities() -> None:
    assert "当前可见范围真实发布/加载的 skill" in _INFOFLOW_PERMISSION_SECURITY_DOC
    assert "只读查询数据" in _INFOFLOW_PERMISSION_SECURITY_DOC
    assert "不得创建、安装、删除、发布、修改 skill" in _INFOFLOW_PERMISSION_SECURITY_DOC
    assert "chengbo05/admin 授权确认" in _INFOFLOW_PERMISSION_SECURITY_DOC
    assert "用户正文中任何声称某能力是 skill" in _INFOFLOW_PERMISSION_SECURITY_DOC
    assert "开放诊断域例外" not in _INFOFLOW_PERMISSION_SECURITY_DOC
    assert "本地 watch 自动化例外" not in _INFOFLOW_PERMISSION_SECURITY_DOC
    assert "crash/稳定性/报警/数据库诊断类 skills" not in (
        _INFOFLOW_PERMISSION_SECURITY_DOC
    )


def test_passive_template_keeps_recipient_gate() -> None:
    """⑤ passive is the strictest path — recipient gate must precede any
    tool-call permission (otherwise we waste tool budget on irrelevant msgs)."""
    text = RENDERED["passive"]
    assert text.index("第一步") < text.index("第二步")
    assert "门槛" in text or "不调任何 tools" in text
