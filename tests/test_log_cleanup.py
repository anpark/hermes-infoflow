from __future__ import annotations

import os

from hermes_infoflow.log_cleanup import cleanup_old_logs


def test_cleanup_old_logs_removes_only_expired_log_files(tmp_path) -> None:
    now = 1_800_000_000.0
    old = now - 15 * 86400
    recent = now - 2 * 86400

    old_gateway = tmp_path / "gateway.log.1"
    old_agent = tmp_path / "agent.log-20260501"
    old_active_gateway = tmp_path / "gateway.log"
    old_active_error = tmp_path / "gateway.error.log"
    old_active_agent = tmp_path / "agent.log"
    recent_gateway = tmp_path / "gateway.log.2"
    unrelated = tmp_path / "state.db"
    for path in (
        old_gateway,
        old_agent,
        old_active_gateway,
        old_active_error,
        old_active_agent,
        recent_gateway,
        unrelated,
    ):
        path.write_text("x", encoding="utf-8")
    for path in (
        old_gateway,
        old_agent,
        old_active_gateway,
        old_active_error,
        old_active_agent,
    ):
        os.utime(path, (old, old))
    for path in (recent_gateway, unrelated):
        os.utime(path, (recent, recent))

    removed = cleanup_old_logs(log_dir=tmp_path, now=now)

    assert set(removed) == {old_gateway, old_agent}
    assert not old_gateway.exists()
    assert not old_agent.exists()
    assert old_active_gateway.exists()
    assert old_active_error.exists()
    assert old_active_agent.exists()
    assert recent_gateway.exists()
    assert unrelated.exists()


def test_cleanup_old_logs_ignores_missing_directory(tmp_path) -> None:
    assert cleanup_old_logs(log_dir=tmp_path / "missing") == []
