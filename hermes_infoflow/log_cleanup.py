"""Best-effort cleanup for Hermes log files produced by the Infoflow runtime."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .paths import get_hermes_home

DEFAULT_LOG_RETENTION_DAYS = 14


def _is_rotated_log_file(path: Path) -> bool:
    name = path.name
    return ".log." in name or ".log-" in name


def cleanup_old_logs(
    *,
    settings: Any | None = None,
    log_dir: Path | None = None,
    retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
    now: float | None = None,
) -> list[Path]:
    """Delete log-like files under ``~/.hermes/logs`` older than retention.

    This function is deliberately narrow: it only removes rotated log files,
    never active ``*.log`` files, directories, or arbitrary state.
    """
    days = max(1, int(retention_days or DEFAULT_LOG_RETENTION_DAYS))
    root = (log_dir or (get_hermes_home(settings) / "logs")).expanduser()
    if not root.exists() or not root.is_dir():
        return []

    cutoff = (time.time() if now is None else float(now)) - days * 86400
    removed: list[Path] = []
    for path in root.iterdir():
        try:
            if not path.is_file() or not _is_rotated_log_file(path):
                continue
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            continue
    return removed


__all__ = ["DEFAULT_LOG_RETENTION_DAYS", "cleanup_old_logs"]
