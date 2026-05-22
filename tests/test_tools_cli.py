"""Smoke tests for the ``hermes-infoflow-tools update`` CLI dry-run."""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest
from hermes_infoflow_tools import cli


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
    assert "deploy.py" in output
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


def test_noncanonical_channel_id_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["update", "--channel-id", "infoflow-dev", "--dry-run"])
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
    assert "deploy.py" in output


def test_dry_run_pip_mode_aliases_directory_deploy(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    output = _run_dry(["update", "--version", "0.1.0", "--mode", "pip", "--dry-run"])
    assert "pip mode" in output
    assert "deprecated" in output
    assert "pip download" in output
    assert "hermes-infoflow==0.1.0" in output
    assert "deploy.py" in output
    assert "pip install" not in output


def test_dry_run_pip_mode_forwards_port_to_deploy_common(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    output = _run_dry(
        ["update", "--version", "0.1.0", "--mode", "pip", "--port", "3333", "--dry-run"]
    )
    assert "--port 3333" in output


def test_dry_run_normalize_defaults_to_infoflow_plugin_dir(monkeypatch, tmp_path) -> None:
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    output = _run_dry(["normalize", "--dry-run"])

    assert "-m hermes_infoflow.deploy" in output
    assert f"--source {hermes_home / 'plugins' / 'infoflow'}" in output
    assert "--hermes-home" in output
    assert "--config-file" in output


def test_dry_run_normalize_uses_source_deploy_script(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    source = tmp_path / "infoflow"
    deploy_script = source / "hermes_infoflow" / "deploy.py"
    deploy_script.parent.mkdir(parents=True)
    deploy_script.write_text("# deploy\n", encoding="utf-8")

    output = _run_dry(["normalize", "--source", str(source), "--port", "4444", "--dry-run"])

    assert str(deploy_script) in output
    assert f"--source {source}" in output
    assert "--port 4444" in output


def test_normalize_rejects_noncanonical_channel_id(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["normalize", "--channel-id", "infoflow-dev", "--dry-run"])
    assert exc.value.code == 2


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
