"""Structured JSONL API logger for Infoflow interactions.

Logs all inbound webhooks and outbound API calls to daily JSONL files
under ~/.hermes/logs/infoflow-api/ (configurable via environment variables).
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["InfoflowAPILogger", "get_logger", "default_logger"]

logger = logging.getLogger(__name__)

# Constants
_DEFAULT_LOG_DIR = "~/.hermes/logs/infoflow-api"
_TRUNCATE_THRESHOLD = 10 * 1024  # 10 KB
_SENSITIVE_FIELDS = {"appSecret", "encodingAesKey"}
_MAX_REDACTED_DEPTH = 20  # prevent infinite recursion on self-referential structures


def _utc_offset_hours() -> float:
    """Return the local timezone offset as hours (e.g. +8.0 for CST)."""
    offset = datetime.now().astimezone().utcoffset()
    return offset.total_seconds() / 3600 if offset else 0.0


def _iso_now() -> str:
    """Return current timestamp in ISO 8601 format with local offset."""
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _redact_value(value: Any, depth: int = 0) -> Any:
    """Recursively redact sensitive fields and truncate oversized strings."""
    if depth > _MAX_REDACTED_DEPTH:
        return "[REDACTED: max depth exceeded]"

    if isinstance(value, dict):
        return {k: _redact_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, depth + 1) for item in value]
    if isinstance(value, str) and len(value) > _TRUNCATE_THRESHOLD:
        return value[:_TRUNCATE_THRESHOLD] + f"[TRUNCATED:{len(value)}B]"
    return value


def _redact_sensitive(obj: Any) -> Any:
    """Redact sensitive fields (appSecret, encodingAesKey) to [REDACTED]."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k in _SENSITIVE_FIELDS:
                result[k] = "[REDACTED]"
            elif isinstance(v, (dict, list)):
                result[k] = _redact_sensitive(v)
            else:
                result[k] = v
        return result
    if isinstance(obj, list):
        return [_redact_sensitive(item) for item in obj]
    return obj


def _sanitize(obj: Any) -> Any:
    """Apply both truncation and sensitive-field redaction."""
    obj = _redact_sensitive(obj)
    obj = _redact_value(obj)
    return obj


def _mask_authorization(headers: dict[str, Any]) -> dict[str, Any]:
    """Mask Authorization header, keeping only first 8 chars."""
    if not isinstance(headers, dict):
        return headers
    masked = dict(headers)
    auth = masked.get("Authorization") or masked.get("authorization")
    if auth and isinstance(auth, str) and len(auth) > 8:
        masked["Authorization"] = auth[:8] + "..."
        masked.pop("authorization", None)
    return masked


class InfoflowAPILogger:
    """Structured JSONL logger for Infoflow API interactions.

    Writes one JSON object per line (JSONL) to daily-rotating log files.
    Sensitive data is redacted and oversized values are truncated automatically.
    All errors are swallowed (logged as warnings) to never block callers.
    """

    def __init__(
        self,
        log_dir: str | None = None,
        retention_days: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        env_enabled = os.environ.get("INFOFLOW_API_LOG_ENABLED", "").strip().lower()
        self._enabled = (
            enabled
            if enabled is not None
            else (env_enabled not in ("false", "0", ""))
        )
        self._log_dir = Path(
            os.environ.get("INFOFLOW_API_LOG_DIR", log_dir or _DEFAULT_LOG_DIR)
        ).expanduser()
        self._retention_days = int(
            os.environ.get("INFOFLOW_API_LOG_RETENTION_DAYS", retention_days or 7)
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_inbound(
        self,
        chat_id: str,
        raw_body: str,
        parsed: dict[str, Any],
        duration_ms: float | int,
    ) -> None:
        """Log an incoming webhook payload."""
        if not self._enabled:
            return
        entry = {
            "ts": _iso_now(),
            "direction": "inbound",
            "api": "webhook",
            "chat_id": str(chat_id),
            "duration_ms": round(float(duration_ms), 2),
            "raw_body_preview": self._truncate_str(raw_body),
            "parsed": _sanitize(parsed),
        }
        self._write(entry)

    def log_outbound(
        self,
        api: str,
        chat_id: str,
        request: dict[str, Any],
        response: dict[str, Any] | str | None,
        status: int,
        duration_ms: float | int,
    ) -> None:
        """Log an outgoing Infoflow API call."""
        if not self._enabled:
            return

        sanitized_request = _sanitize(request)

        # Sanitize response
        if isinstance(response, dict):
            sanitized_response = _sanitize(response)
        elif isinstance(response, str):
            try:
                sanitized_response = _sanitize(json.loads(response))
            except (json.JSONDecodeError, ValueError):
                sanitized_response = self._truncate_str(response)
        else:
            sanitized_response = response

        entry = {
            "ts": _iso_now(),
            "direction": "outbound",
            "api": str(api),
            "chat_id": str(chat_id),
            "status": int(status),
            "duration_ms": round(float(duration_ms), 2),
            "request_body": sanitized_request,
            "response_body": sanitized_response,
        }
        self._write(entry)

    def close(self) -> None:
        """Flush / close any resources (no-op for sync write-per-call)."""
        pass  # Each _write opens/appends/closes, so nothing to flush

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, entry: dict[str, Any]) -> None:
        """Serialize *entry* to JSON and append it to today's log file."""
        try:
            self._ensure_dir()
            self._rotate()
            today_path = self._today_path()
            line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
            with open(today_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            logger.warning("Failed to write Infoflow API log entry", exc_info=True)

    def _ensure_dir(self) -> None:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.warning(
                "Failed to create log directory %s", self._log_dir, exc_info=True
            )

    def _rotate(self) -> None:
        """Delete log files older than the retention period."""
        try:
            now = datetime.now()
            for p in self._log_dir.glob("*.jsonl"):
                # Extract date from filename: YYYY-MM-DD.jsonl
                stem = p.stem  # e.g. "2026-05-17"
                try:
                    file_date = datetime.strptime(stem, "%Y-%m-%d")
                    age = (now - file_date).days
                    if age > self._retention_days:
                        p.unlink()
                except ValueError:
                    continue  # skip files with unexpected names
        except Exception:
            logger.warning("Failed to rotate log files", exc_info=True)

    def _today_path(self) -> Path:
        return self._log_dir / f"{datetime.now():%Y-%m-%d}.jsonl"

    @staticmethod
    def _truncate_str(value: str, limit: int = _TRUNCATE_THRESHOLD) -> str:
        if len(value) > limit:
            return value[:limit] + f"[TRUNCATED:{len(value)}B]"
        return value


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

default_logger = InfoflowAPILogger()


def get_logger() -> InfoflowAPILogger:
    """Return the module-level default :class:`InfoflowAPILogger`."""
    return default_logger
