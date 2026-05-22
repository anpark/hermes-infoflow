"""Smoke tests for the ``hermes-infoflow-tools update`` CLI dry-run."""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest
from hermes_infoflow_tools import cli

from hermes_infoflow import config_editor


def _run_dry(args: list[str]) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(args)
    assert rc == 0
    return out.getvalue()


def test_dry_run_extract_mode_prints_pipeline(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    output = _run_dry(["update", "--version", "0.1.0", "--mode", "extract", "--dry-run"])
    # The four major steps must all show up.
    assert "pip download" in output
    assert "rsync" in output
    assert "deploy-common.sh" in output
    assert "--dry-run" in output


def test_dry_run_extract_mode_forwards_port(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    output = _run_dry(
        [
            "update",
            "--version",
            "0.1.0",
            "--mode",
            "extract",
            "--port",
            "9000",
            "--dry-run",
        ]
    )
    assert "--port 9000" in output


def test_invalid_port_rejected_before_dry_run_pipeline(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["update", "--mode", "extract", "--port", "99999", "--dry-run"])
    assert exc.value.code == 2


def test_dry_run_extract_mode_accepts_local_source(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    source = tmp_path / "hermes-infoflow"
    (source / "hermes_infoflow").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / "plugin.yaml").write_text("name: infoflow\n", encoding="utf-8")

    output = _run_dry(
        ["update", "--package-name", str(source), "--mode", "extract", "--dry-run"]
    )

    assert "use local source" in output
    assert "pip download" not in output
    assert "rsync" in output


def test_dry_run_pip_mode_prints_pip_install(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    output = _run_dry(["update", "--version", "0.1.0", "--mode", "pip", "--dry-run"])
    assert "pip install" in output
    assert "hermes-infoflow==0.1.0" in output
    assert "plugins.enabled" in output
    assert "platform_toolsets.infoflow" in output
    assert "pip mode" in output  # the reminder note about plugin.yaml


def test_pip_mode_enables_plugin_in_config(monkeypatch, tmp_path) -> None:
    yaml = pytest.importorskip("yaml")
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(cli._Runner, "__call__", lambda self, cmd, *, cwd=None: None)

    rc = cli.main(["update", "--version", "0.1.0", "--mode", "pip"])

    assert rc == 0
    config_text = (hermes_home / "config.yaml").read_text(encoding="utf-8")
    assert "plugins:" in config_text
    assert "enabled:" in config_text
    assert "- infoflow" in config_text
    config = yaml.safe_load(config_text)
    assert config["platform_toolsets"]["infoflow"] == list(
        config_editor.DEFAULT_INFOFLOW_PLATFORM_TOOLSETS
    )


def test_pip_mode_merges_platform_toolsets(monkeypatch, tmp_path) -> None:
    yaml = pytest.importorskip("yaml")
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        "  - infoflow\n"
        "platform_toolsets:\n"
        "  cli:\n"
        "  - terminal\n"
        "  - web\n"
        "  infoflow:\n"
        "  - custom-mcp\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(cli._Runner, "__call__", lambda self, cmd, *, cwd=None: None)

    rc = cli.main(["update", "--version", "0.1.0", "--mode", "pip"])

    assert rc == 0
    config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    infoflow_toolsets = config["platform_toolsets"]["infoflow"]
    assert infoflow_toolsets[0] == "custom-mcp"
    assert "terminal" in infoflow_toolsets
    assert "web" in infoflow_toolsets
    assert "hermes-infoflow" in infoflow_toolsets


def test_dry_run_pip_mode_forwards_port_to_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    output = _run_dry(
        ["update", "--version", "0.1.0", "--mode", "pip", "--port", "3333", "--dry-run"]
    )
    assert "INFOFLOW_PORT=3333" in output


def test_pip_mode_sets_port_in_env(monkeypatch, tmp_path) -> None:
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(cli._Runner, "__call__", lambda self, cmd, *, cwd=None: None)

    rc = cli.main(["update", "--version", "0.1.0", "--mode", "pip", "--port", "4444"])

    assert rc == 0
    env_text = (hermes_home / ".env").read_text(encoding="utf-8")
    assert "INFOFLOW_PORT=4444" in env_text


def test_pip_mode_preserves_existing_port(monkeypatch, tmp_path) -> None:
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / ".env").write_text("INFOFLOW_PORT=7777\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(cli._Runner, "__call__", lambda self, cmd, *, cwd=None: None)

    rc = cli.main(["update", "--version", "0.1.0", "--mode", "pip"])

    assert rc == 0
    assert (hermes_home / ".env").read_text(encoding="utf-8") == "INFOFLOW_PORT=7777\n"


def test_resolve_pip_version_spec_latest_drops_version() -> None:
    assert cli._resolve_pip_version_spec("hermes-infoflow", "latest") == "hermes-infoflow"
    assert (
        cli._resolve_pip_version_spec("hermes-infoflow", "0.1.0b1")
        == "hermes-infoflow==0.1.0b1"
    )


def test_package_glob_stem_handles_local_paths() -> None:
    assert cli._package_glob_stem("hermes-infoflow") == "hermes_infoflow"
    assert (
        cli._package_glob_stem("/tmp/private/hermes-infoflow")
        == "hermes_infoflow"
    )


def test_looks_like_local_path_only_for_explicit_paths() -> None:
    assert cli._looks_like_local_path("/abs/hermes-infoflow")
    assert cli._looks_like_local_path("./hermes-infoflow")
    assert cli._looks_like_local_path("../hermes-infoflow")
    assert cli._looks_like_local_path("~/hermes-infoflow")
    assert not cli._looks_like_local_path("hermes-infoflow")
    assert not cli._looks_like_local_path("")


def test_extract_mode_ignores_local_lookalike_in_cwd(monkeypatch, tmp_path) -> None:
    """Bare ``--package-name hermes-infoflow`` MUST hit PyPI even when a
    sibling directory of that name exists in cwd."""
    # Build a directory tree that looks like a checkout, then chdir there.
    decoy_parent = tmp_path / "parent"
    decoy = decoy_parent / "hermes-infoflow"
    (decoy / "hermes_infoflow").mkdir(parents=True)
    (decoy / "scripts").mkdir()
    (decoy / "plugin.yaml").write_text("name: infoflow\n", encoding="utf-8")
    monkeypatch.chdir(decoy_parent)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    output = _run_dry(["update", "--mode", "extract", "--dry-run"])

    assert "pip download" in output
    assert "use local source" not in output
