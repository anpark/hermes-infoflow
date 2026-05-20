"""Tests for the strengthened NO_REPLY sentinel in send_message.

After the merge the sentinel is the primary mechanism for suppressing
unwanted outbound messages, so its acceptance rules need to be precise:

- Accept: full text (after stripping punctuation/whitespace) == "NO_REPLY"
- Accept: first line (after stripping punctuation) == "NO_REPLY"
- Reject: "NO_REPLY" appearing only on a middle/trailing line — we'd rather
  ship a real answer followed by an accidental sentinel than swallow the
  entire message.
"""

from __future__ import annotations

from hermes_infoflow.bot import _send_path_cv
from hermes_infoflow.bot import no_reply_sentinel_hits as _sentinel_hits


def test_plain_no_reply_suppresses() -> None:
    assert _sentinel_hits("NO_REPLY")


def test_no_reply_with_trailing_punctuation_suppresses() -> None:
    assert _sentinel_hits("NO_REPLY.")
    assert _sentinel_hits("NO_REPLY。")
    assert _sentinel_hits("NO_REPLY ")


def test_no_reply_first_line_suppresses() -> None:
    assert _sentinel_hits("NO_REPLY\n\n（some explanation）")


def test_no_reply_trailing_does_not_suppress() -> None:
    """Answer first, accidental sentinel trailing: ship the answer."""
    assert not _sentinel_hits("今天是周三\nNO_REPLY")


def test_no_reply_middle_line_does_not_suppress() -> None:
    assert not _sentinel_hits("好的\nNO_REPLY\n（更多说明）")


def test_normal_reply_does_not_suppress() -> None:
    assert not _sentinel_hits("今天是星期三")


def test_empty_text_does_not_suppress() -> None:
    """Empty text suppresses if we naively check first==''=='NO_REPLY', so
    confirm the implementation handles empty input cleanly."""
    assert not _sentinel_hits("")
    assert not _sentinel_hits("   ")
    assert not _sentinel_hits(None)


def test_send_path_cv_module_var_exists() -> None:
    """The sentinel logs include `_send_path_cv.get()`, so the var must
    be importable from bot.py."""
    assert _send_path_cv.get("") == ""
    token = _send_path_cv.set("followUp")
    try:
        assert _send_path_cv.get("") == "followUp"
    finally:
        _send_path_cv.reset(token)
