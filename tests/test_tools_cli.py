"""Smoke tests for the ``hermes-infoflow-tools update`` CLI dry-run."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

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
    assert "rsync" in output
    assert "deploy-common.sh" in output
    assert "--dry-run" in output


def test_dry_run_pip_mode_prints_pip_install(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    output = _run_dry(["update", "--version", "0.1.0", "--mode", "pip", "--dry-run"])
    assert "pip install" in output
    assert "hermes-infoflow==0.1.0" in output
    assert "pip mode" in output  # the reminder note about plugin.yaml


def test_resolve_pip_version_spec_latest_drops_version() -> None:
    assert cli._resolve_pip_version_spec("hermes-infoflow", "latest") == "hermes-infoflow"
    assert (
        cli._resolve_pip_version_spec("hermes-infoflow", "0.1.0b1")
        == "hermes-infoflow==0.1.0b1"
    )
