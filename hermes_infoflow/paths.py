"""Local filesystem paths for the Infoflow plugin.

The plugin source may run from a deployed copy under ``~/.hermes/plugins``,
but user-facing files should live under Hermes home so gateway, cron, and
tool worker processes can share one stable location.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

INFOFLOW_DIR_NAME = "infoflow"
INFOFLOW_SHARED_FILES_DIR_NAME = "shared_files"
INFOFLOW_PRIVATE_DIR_NAME = "private"
INFOFLOW_INBOUND_FILES_DIR_NAME = "inbound_files"
INFOFLOW_SHARED_FILES_DB_NAME = "sql_shared_files.db"


def get_hermes_home(settings: Any | None = None) -> Path:
    """Return Hermes home, defaulting to ``$HERMES_HOME`` or ``~/.hermes``."""
    configured = ""
    if isinstance(settings, dict):
        configured = str(settings.get("hermes_home") or "").strip()
    raw = configured or os.getenv("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(raw).expanduser()


def get_infoflow_home(settings: Any | None = None) -> Path:
    """Return the root directory for Infoflow-owned local files."""
    configured = ""
    if isinstance(settings, dict):
        configured = str(settings.get("infoflow_home") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return get_hermes_home(settings) / INFOFLOW_DIR_NAME


def get_infoflow_shared_files_root(settings: Any | None = None) -> Path:
    """Return the root directory whose descendants can be published outward."""
    return get_infoflow_home(settings) / INFOFLOW_SHARED_FILES_DIR_NAME


def get_infoflow_shared_files_db_path(settings: Any | None = None) -> Path:
    """Return the SQLite DB path used by file-delivery URL cache metadata."""
    return get_infoflow_home(settings) / INFOFLOW_SHARED_FILES_DB_NAME


def get_infoflow_private_root(settings: Any | None = None) -> Path:
    """Return the directory reserved for Infoflow-private metadata/material."""
    return get_infoflow_home(settings) / INFOFLOW_PRIVATE_DIR_NAME


def get_infoflow_inbound_files_root(settings: Any | None = None) -> Path:
    """Return the root directory for files received from Infoflow users."""
    configured = ""
    if isinstance(settings, dict):
        configured = str(settings.get("inbound_file_dir") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return get_infoflow_home(settings) / INFOFLOW_INBOUND_FILES_DIR_NAME


def ensure_infoflow_dirs(settings: Any | None = None) -> None:
    """Create the standard Infoflow local directory skeleton."""
    home = get_infoflow_home(settings)
    shared = get_infoflow_shared_files_root(settings)
    private = get_infoflow_private_root(settings)
    inbound = get_infoflow_inbound_files_root(settings)
    for path in (
        home,
        shared,
        shared / "temp",
        shared / "permanent",
        private,
        inbound,
    ):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)


__all__ = [
    "INFOFLOW_DIR_NAME",
    "INFOFLOW_INBOUND_FILES_DIR_NAME",
    "INFOFLOW_PRIVATE_DIR_NAME",
    "INFOFLOW_SHARED_FILES_DB_NAME",
    "INFOFLOW_SHARED_FILES_DIR_NAME",
    "ensure_infoflow_dirs",
    "get_hermes_home",
    "get_infoflow_inbound_files_root",
    "get_infoflow_home",
    "get_infoflow_private_root",
    "get_infoflow_shared_files_db_path",
    "get_infoflow_shared_files_root",
]
