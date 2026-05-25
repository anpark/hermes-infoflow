"""Shared prompt and tool-result rules for Infoflow delivery tools."""

from __future__ import annotations

from typing import Any

SUGGESTED_FINAL_RESPONSE_NO_REPLY = "NO_REPLY"

INFOFLOW_DELIVERY_TOOL_RULES = """\
## 外发工具规则

`send_message`、`infoflow_reply` 等会实际向如流发送内容的工具属于外发工具。工具调用结果以工具返回为准。

- `MEDIA:<本地图片绝对路径>` 是传输指令，不是消息正文；绝不能把 `MEDIA:`、`file://` 或本地路径发成普通文本。
- 生成图片/媒体时先保存到 `~/.hermes/cache/images/` 或 `~/.hermes/image_cache/`，再把 `MEDIA:<...>` 交给工具上传字节。
- 外发工具成功并已完成用户要求的对外发送动作时，最终输出单独一行 `NO_REPLY`，不要再补“已发送/来了”等确认文案。
- 如果用户明确还要求你在当前会话报告发送结果，可以简短报告状态，但不要重复发送目标内容或本地路径。
- 外发工具失败时，修正输入后重试；无法修正时只说明失败原因，错误说明中不得包含本地文件路径。不要退化为只发送 caption、`MEDIA:` 文本或路径文本。
"""


def delivery_success_hint() -> dict[str, Any]:
    """Return the soft model hint attached to successful delivery tools."""
    return {
        "delivered": True,
        "suggested_final_response": SUGGESTED_FINAL_RESPONSE_NO_REPLY,
    }
