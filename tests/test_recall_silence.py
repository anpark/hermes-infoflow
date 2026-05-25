from __future__ import annotations

from hermes_infoflow.recall_silence import RecallSilenceTracker, is_recall_ack_only


def test_recall_ack_detection_is_small_whitelist() -> None:
    assert is_recall_ack_only("已撤回。")
    assert is_recall_ack_only("撤回成功")
    assert is_recall_ack_only("已帮你撤回这条消息")
    assert not is_recall_ack_only("撤回失败，消息可能已过期。")
    assert not is_recall_ack_only("已撤回。另外，另一个任务结果如下")
    assert not is_recall_ack_only("关于“已撤回”是什么意思，可以理解为消息被撤销。")


def test_recall_silence_tracker_requires_same_turn_and_chat() -> None:
    tracker = RecallSilenceTracker(ttl_seconds=10)
    tracker.mark_success(inbound_mid="IN-1", chat_id="infoflow:alice", now=100)

    assert not tracker.consume_if_suppress(
        inbound_mid="IN-2",
        chat_id="alice",
        text="已撤回",
        now=101,
    )
    assert not tracker.consume_if_suppress(
        inbound_mid="IN-1",
        chat_id="group:42",
        text="已撤回",
        now=101,
    )
    assert tracker.consume_if_suppress(
        inbound_mid="IN-1",
        chat_id="alice",
        text="已撤回",
        now=101,
    )
    assert not tracker.consume_if_suppress(
        inbound_mid="IN-1",
        chat_id="alice",
        text="已撤回",
        now=102,
    )


def test_recall_silence_tracker_expires() -> None:
    tracker = RecallSilenceTracker(ttl_seconds=1)
    tracker.mark_success(inbound_mid="IN-1", chat_id="alice", now=100)

    assert not tracker.consume_if_suppress(
        inbound_mid="IN-1",
        chat_id="alice",
        text="已撤回",
        now=102,
    )
