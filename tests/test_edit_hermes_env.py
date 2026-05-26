"""Tests for scripts/lib/edit_hermes_env.py."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EDIT_SCRIPT = _REPO_ROOT / "scripts" / "lib" / "edit_hermes_env.py"


@pytest.fixture(scope="module")
def edit_env_module():
    spec = importlib.util.spec_from_file_location("edit_hermes_env", _EDIT_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["edit_hermes_env"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_ensure_writes_default_on_empty_file(edit_env_module, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    changed = edit_env_module.ensure_key(env_file, "INFOFLOW_PORT", "26521")
    assert changed
    assert env_file.read_text(encoding="utf-8") == "INFOFLOW_PORT=26521\n"
    assert edit_env_module.read_key(env_file, "INFOFLOW_PORT") == "26521"


def test_ensure_is_noop_when_key_exists(edit_env_module, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("INFOFLOW_PORT=9000\n", encoding="utf-8")
    changed = edit_env_module.ensure_key(env_file, "INFOFLOW_PORT", "26521")
    assert changed is False
    assert edit_env_module.read_key(env_file, "INFOFLOW_PORT") == "9000"


def test_set_overwrites_existing(edit_env_module, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("INFOFLOW_PORT=9000\n", encoding="utf-8")
    changed = edit_env_module.upsert_key(env_file, "INFOFLOW_PORT", "3333")
    assert changed
    assert edit_env_module.read_key(env_file, "INFOFLOW_PORT") == "3333"


def test_copy_key_if_missing_migrates_legacy_value(edit_env_module, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("INFOFLOW_HOME_CHANNEL=legacy-user\n", encoding="utf-8")

    changed = edit_env_module.copy_key_if_missing(
        env_file,
        "INFOFLOW_OP_CHANNEL",
        "INFOFLOW_HOME_CHANNEL",
    )

    assert changed
    assert edit_env_module.read_key(env_file, "INFOFLOW_HOME_CHANNEL") == "legacy-user"
    assert edit_env_module.read_key(env_file, "INFOFLOW_OP_CHANNEL") == "legacy-user"


def test_copy_key_if_missing_preserves_existing_target(
    edit_env_module,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "INFOFLOW_HOME_CHANNEL=legacy-user\nINFOFLOW_OP_CHANNEL=ops-user\n",
        encoding="utf-8",
    )

    changed = edit_env_module.copy_key_if_missing(
        env_file,
        "INFOFLOW_OP_CHANNEL",
        "INFOFLOW_HOME_CHANNEL",
    )

    assert changed is False
    assert edit_env_module.read_key(env_file, "INFOFLOW_OP_CHANNEL") == "ops-user"


def test_upsert_preserves_comments_and_other_keys(edit_env_module, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# webhook\nINFOFLOW_PORT=1\nINFOFLOW_APP_KEY=abc\n",
        encoding="utf-8",
    )
    edit_env_module.upsert_key(env_file, "INFOFLOW_PORT", "26521")
    text = env_file.read_text(encoding="utf-8")
    assert "# webhook" in text
    assert "INFOFLOW_APP_KEY=abc" in text
    assert "INFOFLOW_PORT=26521" in text


def test_read_key_handles_export_prefix(edit_env_module, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('export INFOFLOW_PORT="7777"\n', encoding="utf-8")
    assert edit_env_module.read_key(env_file, "INFOFLOW_PORT") == "7777"


def test_invalid_port_exits_nonzero(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    result = subprocess.run(
        [
            sys.executable,
            str(_EDIT_SCRIPT),
            "--env-file",
            str(env_file),
            "--set",
            "INFOFLOW_PORT=0",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "invalid INFOFLOW_PORT" in result.stderr


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    result = subprocess.run(
        [
            sys.executable,
            str(_EDIT_SCRIPT),
            "--env-file",
            str(env_file),
            "--set",
            "INFOFLOW_PORT=9000",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert not env_file.exists()
