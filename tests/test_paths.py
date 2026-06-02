from __future__ import annotations

from pathlib import Path

from hermes_infoflow.paths import (
    ensure_infoflow_dirs,
    get_hermes_home,
    get_infoflow_home,
    get_infoflow_inbound_files_root,
    get_infoflow_private_root,
    get_infoflow_shared_files_db_path,
    get_infoflow_shared_files_root,
)


def test_infoflow_paths_default_under_hermes_home(monkeypatch, tmp_path: Path) -> None:
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert get_hermes_home() == hermes_home
    assert get_infoflow_home() == hermes_home / "infoflow"
    assert get_infoflow_shared_files_root() == hermes_home / "infoflow" / "shared_files"
    assert get_infoflow_private_root() == hermes_home / "infoflow" / "private"
    assert get_infoflow_inbound_files_root() == (
        hermes_home / "infoflow" / "inbound_files"
    )
    assert get_infoflow_shared_files_db_path() == (
        hermes_home / "infoflow" / "sql_shared_files.db"
    )


def test_ensure_infoflow_dirs_creates_standard_skeleton(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    ensure_infoflow_dirs()

    root = get_infoflow_shared_files_root()
    assert root.is_dir()
    assert (root / "temp").is_dir()
    assert (root / "permanent").is_dir()
    assert get_infoflow_private_root().is_dir()
    assert get_infoflow_inbound_files_root().is_dir()
