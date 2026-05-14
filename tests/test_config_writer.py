"""Tests for scripts/lib/edit_hermes_config.py.

Hermes reads enabled plugins from a *list* at ``plugins.enabled`` — these
tests pin the writer's behavior so we never regress to the OpenClaw
``plugins.entries.<id>.enabled = true`` shape.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EDIT_SCRIPT = _REPO_ROOT / "scripts" / "lib" / "edit_hermes_config.py"


@pytest.fixture(scope="module")
def edit_module():
    spec = importlib.util.spec_from_file_location("edit_hermes_config", _EDIT_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["edit_hermes_config"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_apply_creates_plugins_block_in_empty_config(edit_module) -> None:
    data: dict = {}
    changed = edit_module.apply(data, "infoflow")
    assert changed
    assert data == {"plugins": {"enabled": ["infoflow"]}}


def test_apply_appends_to_existing_list(edit_module) -> None:
    data = {"plugins": {"enabled": ["other"]}}
    changed = edit_module.apply(data, "infoflow")
    assert changed
    assert data["plugins"]["enabled"] == ["other", "infoflow"]


def test_apply_is_idempotent(edit_module) -> None:
    data = {"plugins": {"enabled": ["infoflow"]}}
    changed = edit_module.apply(data, "infoflow")
    assert changed is False
    assert data["plugins"]["enabled"] == ["infoflow"]


def test_apply_preserves_other_keys(edit_module) -> None:
    data = {
        "core": {"x": 1},
        "plugins": {"enabled": ["a"], "disabled": ["b"]},
        "other": "kept",
    }
    edit_module.apply(data, "infoflow")
    assert data["core"] == {"x": 1}
    assert data["plugins"]["disabled"] == ["b"]
    assert data["other"] == "kept"
    assert data["plugins"]["enabled"] == ["a", "infoflow"]


def test_apply_remove_drops_id(edit_module) -> None:
    data = {"plugins": {"enabled": ["infoflow", "other"]}}
    changed = edit_module.apply(data, "infoflow", remove=True)
    assert changed
    assert data["plugins"]["enabled"] == ["other"]


def test_apply_rejects_mapping_enabled(edit_module) -> None:
    data = {"plugins": {"enabled": {"infoflow": True}}}
    with pytest.raises(SystemExit):
        edit_module.apply(data, "infoflow")


def test_main_writes_yaml_round_trip(edit_module, tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("core:\n  x: 1\n", encoding="utf-8")
    rc = edit_module.main(["--config-file", str(cfg), "--plugin-id", "infoflow"])
    assert rc == 0
    loaded = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert loaded == {"core": {"x": 1}, "plugins": {"enabled": ["infoflow"]}}


def test_main_dry_run_does_not_write(edit_module, tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("core:\n  x: 1\n", encoding="utf-8")
    rc = edit_module.main(
        ["--config-file", str(cfg), "--plugin-id", "infoflow", "--dry-run"]
    )
    assert rc == 0
    assert "infoflow" not in cfg.read_text(encoding="utf-8")
