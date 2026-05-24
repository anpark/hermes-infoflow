"""Tests that ``register(ctx)`` registers everything we expect with hermes.

Skipped automatically when hermes-agent isn't importable.
"""

from __future__ import annotations

import pytest

pytest.importorskip("gateway.platform_registry")

from hermes_infoflow.adapter import register  # noqa: E402


class _Capture:
    """Minimal stand-in for ``hermes_cli.plugins.PluginContext``."""

    def __init__(self):
        self.platforms: list[dict] = []
        self.tools: list[dict] = []

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)


def test_register_registers_platform_and_tool() -> None:
    ctx = _Capture()
    register(ctx)
    assert len(ctx.platforms) == 1
    platform = ctx.platforms[0]
    assert platform["name"] == "infoflow"
    assert "Infoflow" in platform["label"]
    assert platform["cron_deliver_env_var"] == "INFOFLOW_HOME_CHANNEL"
    assert platform["max_message_length"] == 2048
    assert "infoflow" in (platform.get("install_hint") or "").lower() or platform.get("install_hint")
    # Required env names align with the plugin.yaml manifest.
    required = set(platform["required_env"])
    assert required == {
        "INFOFLOW_CHECK_TOKEN",
        "INFOFLOW_ENCODING_AES_KEY",
        "INFOFLOW_APP_KEY",
        "INFOFLOW_APP_SECRET",
    }

    # The recall tool must be registered too.
    assert any(t["name"] == "infoflow_recall_message" for t in ctx.tools)
    tool = next(t for t in ctx.tools if t["name"] == "infoflow_recall_message")
    assert tool["toolset"] == "hermes-infoflow"
    assert tool["is_async"] is True
    assert tool["schema"]["parameters"]["required"] == ["target"]

    assert any(t["name"] == "infoflow_get_group_members" for t in ctx.tools)
    members_tool = next(
        t for t in ctx.tools if t["name"] == "infoflow_get_group_members"
    )
    assert members_tool["toolset"] == "hermes-infoflow"
    assert members_tool["is_async"] is True
    assert members_tool["schema"]["parameters"]["required"] == ["group_id"]

    assert any(t["name"] == "infoflow_get_message_history" for t in ctx.tools)
    history_tool = next(
        t for t in ctx.tools if t["name"] == "infoflow_get_message_history"
    )
    assert history_tool["toolset"] == "hermes-infoflow"
    assert history_tool["is_async"] is True
    history_props = history_tool["schema"]["parameters"]["properties"]
    assert "message_id" in history_props
    assert "start_time" in history_props
    assert "end_time" in history_props
    assert "date" not in history_props


def test_plugin_name_consistency() -> None:
    """plugin.yaml.name, register(name=...), and entry-point key must align."""
    import pathlib

    import yaml

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    manifest = yaml.safe_load((repo_root / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "infoflow"

    pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    # Entry-point line: `infoflow = "hermes_infoflow"`
    assert 'infoflow = "hermes_infoflow"' in pyproject

    ctx = _Capture()
    register(ctx)
    assert ctx.platforms[0]["name"] == "infoflow"
