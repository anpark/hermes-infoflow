"""End-to-end smoke tests for the plugin layout produced by ``scripts/deploy.sh``.

hermes-agent's directory loader
(``hermes_cli/plugins.py::_load_directory_module``) requires
``__init__.py`` to live directly at ``<plugin_dir>``.  The repo ships a
root-level ``__init__.py`` that re-exports from ``hermes_infoflow/``, so
``hermes plugins install`` works without flattening.

``scripts/deploy.sh`` (and ``hermes-infoflow-tools update --mode extract``)
*also* flatten the layout for backward compatibility — these tests lock that
contract too.  If a refactor reverts the flattening, hermes-agent silently
fails with ``No __init__.py in <plugin_dir>`` on gateway start — hard to
debug without reading hermes-agent's loader.  We'd rather see a red test
here than a confusing gateway log later.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY_SCRIPT = _REPO_ROOT / "scripts" / "deploy.sh"
_DEPLOY_COMMON_SCRIPT = _REPO_ROOT / "scripts" / "lib" / "deploy-common.sh"
_EDIT_ENV_SCRIPT = _REPO_ROOT / "scripts" / "lib" / "edit_hermes_env.py"

# Every file currently in hermes_infoflow/ should end up at the plugin
# dir root (one level shallower than the repo). Kept as a literal set so
# a renamed/dropped module is caught loudly here too.
_EXPECTED_PACKAGE_FILES = {
    "__init__.py",
    "adapter.py",
    "api.py",
    "config_editor.py",
    "crypto.py",
    "deploy.py",
    "parser.py",
    "policy.py",
    "sent_store.py",
    "py.typed",
}


if shutil.which("rsync") is None or shutil.which("bash") is None:
    pytest.skip(
        "rsync and bash are required for deploy layout tests",
        allow_module_level=True,
    )


def _deploy_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHON"] = sys.executable
    env["PATH"] = "/usr/bin:/bin"
    return env


def _run_deploy(
    home: Path,
    extra_args: list[str] | None = None,
    *,
    pre_config_lines: str | None = None,
    pre_env_lines: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    home.mkdir(parents=True, exist_ok=True)
    if pre_config_lines is not None:
        hermes_dir = home / ".hermes"
        hermes_dir.mkdir(parents=True, exist_ok=True)
        (hermes_dir / "config.yaml").write_text(pre_config_lines, encoding="utf-8")
    if pre_env_lines is not None:
        hermes_dir = home / ".hermes"
        hermes_dir.mkdir(parents=True, exist_ok=True)
        (hermes_dir / ".env").write_text(pre_env_lines, encoding="utf-8")
    cmd = ["bash", str(_DEPLOY_SCRIPT), *(extra_args or [])]
    env = _deploy_env(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _seed_launchd_gateway_plist(home: Path) -> None:
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    (launch_agents / "ai.hermes.gateway.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>ai.hermes.gateway</string>
  <key>ProgramArguments</key><array><string>/ignored/python</string></array>
</dict></plist>
""",
        encoding="utf-8",
    )


def _install_fake_launchd_tools(fakebin: Path) -> tuple[Path, Path]:
    fakebin.mkdir(parents=True, exist_ok=True)
    launchctl_log = fakebin.parent / "launchctl.log"
    hermes_log = fakebin.parent / "hermes.log"
    _write_executable(
        fakebin / "uname",
        "#!/bin/sh\nprintf 'Darwin\\n'\n",
    )
    _write_executable(
        fakebin / "id",
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-u\" ]; then printf '501\\n'; exit 0; fi\n"
        "exec /usr/bin/id \"$@\"\n",
    )
    _write_executable(
        fakebin / "plutil",
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  Label) printf 'ai.hermes.gateway\\n' ;;\n"
        "  ProgramArguments.0) printf '%s\\n' \"$FAKE_HERMES_PYTHON\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    _write_executable(
        fakebin / "launchctl",
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  print) printf 'state = running\\npid = 123\\n' ;;\n"
        "  kickstart) printf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    _write_executable(
        fakebin / "hermes",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_HERMES_LOG\"\n"
        "if [ \"$1\" = \"gateway\" ] && [ \"$2\" = \"status\" ]; then\n"
        "  printf 'PermissionError: Operation not permitted: ps\\n' >&2\n"
        "  exit 1\n"
        "fi\n"
        "if [ \"$1\" = \"gateway\" ] && [ \"$2\" = \"restart\" ]; then exit 9; fi\n"
        "exit 0\n",
    )
    return launchctl_log, hermes_log


def _read_env_port(env_file: Path) -> str | None:
    spec = importlib.util.spec_from_file_location("edit_hermes_env", _EDIT_ENV_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.read_key(env_file, "INFOFLOW_PORT")


@pytest.fixture
def deployed(tmp_path: Path) -> Path:
    """Run ``scripts/deploy.sh`` against an isolated $HOME and return the plugin dir."""
    home = tmp_path / "home"
    result = _run_deploy(home)
    if result.returncode != 0:
        pytest.fail(
            f"scripts/deploy.sh failed (rc={result.returncode})\n"
            f"---- stdout ----\n{result.stdout}\n"
            f"---- stderr ----\n{result.stderr}"
        )
    return home / ".hermes" / "plugins" / "infoflow"


def _tree(root: Path) -> str:
    """Pretty-print all files under *root* relative to it, for failure messages."""
    return "\n".join(
        sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    )


def test_init_py_at_plugin_root(deployed: Path) -> None:
    init_py = deployed / "__init__.py"
    assert init_py.is_file(), (
        "hermes-agent's directory loader requires __init__.py at the plugin "
        f"dir root. Deployed layout was:\n{_tree(deployed)}"
    )


def test_deploy_rerun_deletes_stale_root_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run_deploy(home)
    assert result.returncode == 0, result.stderr
    plugin_dir = home / ".hermes" / "plugins" / "infoflow"
    stale = plugin_dir / "stale_from_other_deploy.py"
    stale.write_text("# stale\n", encoding="utf-8")

    result = _run_deploy(home)

    assert result.returncode == 0, result.stderr
    assert not stale.exists()


def test_tools_extract_overwrites_deploy_layout(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    result = _run_deploy(home)
    assert result.returncode == 0, result.stderr
    plugin_dir = home / ".hermes" / "plugins" / "infoflow"
    stale = plugin_dir / "stale_from_deploy_sh.py"
    stale.write_text("# stale\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("PYTHON", sys.executable)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    from hermes_infoflow_tools import cli

    rc = cli.main(
        [
            "update",
            "--package-name",
            str(_REPO_ROOT),
            "--mode",
            "extract",
        ]
    )

    assert rc == 0
    assert not stale.exists()
    assert (plugin_dir / "__init__.py").is_file()
    assert (plugin_dir / "adapter.py").is_file()
    assert not (plugin_dir / "hermes_infoflow").exists()


def _seed_plugins_install_layout(plugin_dir: Path) -> None:
    """Create the layout produced by ``hermes plugins install`` without git."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_REPO_ROOT / "__init__.py", plugin_dir / "__init__.py")
    shutil.copy2(_REPO_ROOT / "plugin.yaml", plugin_dir / "plugin.yaml")
    shutil.copytree(
        _REPO_ROOT / "hermes_infoflow",
        plugin_dir / "hermes_infoflow",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        _REPO_ROOT / "scripts",
        plugin_dir / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (plugin_dir / ".git").mkdir()
    (plugin_dir / "stale_from_git_install.txt").write_text("stale\n", encoding="utf-8")


def test_normalize_overwrites_plugins_install_layout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plugin_dir = home / ".hermes" / "plugins" / "infoflow"
    _seed_plugins_install_layout(plugin_dir)

    env = _deploy_env(home)
    env["HERMES_INFOFLOW_ENTRYPOINT_POLICY"] = "keep"
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "hermes_infoflow" / "deploy.py"),
            "--source",
            str(plugin_dir),
            "--port",
            "4445",
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (plugin_dir / "__init__.py").is_file()
    assert (plugin_dir / "adapter.py").is_file()
    assert (plugin_dir / "deploy.py").is_file()
    assert (plugin_dir / "plugin.yaml").is_file()
    assert (plugin_dir / "scripts" / "lib" / "deploy-common.sh").is_file()
    assert not (plugin_dir / "hermes_infoflow").exists()
    assert not (plugin_dir / ".git").exists()
    assert not (plugin_dir / "stale_from_git_install.txt").exists()
    assert _read_env_port(home / ".hermes" / ".env") == "4445"


def test_pip_style_deploy_command_from_package_source(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = _deploy_env(home)
    env["HERMES_INFOFLOW_ENTRYPOINT_POLICY"] = "keep"
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "hermes_infoflow" / "deploy.py"),
            "--port",
            "4446",
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    plugin_dir = home / ".hermes" / "plugins" / "infoflow"
    assert (plugin_dir / "adapter.py").is_file()
    assert (plugin_dir / "deploy.py").is_file()
    assert not (plugin_dir / "hermes_infoflow").exists()
    assert _read_env_port(home / ".hermes" / ".env") == "4446"


def test_pip_style_deploy_dry_run_works_without_existing_plugin_dir(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    env = _deploy_env(home)
    env["HERMES_INFOFLOW_ENTRYPOINT_POLICY"] = "keep"
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "hermes_infoflow" / "deploy.py"),
            "--port",
            "4448",
            "--dry-run",
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "replace" in result.stdout
    assert "deploy-common.sh" in result.stdout
    assert not (home / ".hermes" / "plugins" / "infoflow").exists()
    assert not (home / ".hermes" / "config.yaml").exists()
    assert not (home / ".hermes" / ".env").exists()


def test_normalize_restores_existing_plugin_when_deploy_common_fails(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plugin_dir = home / ".hermes" / "plugins" / "infoflow"
    plugin_dir.mkdir(parents=True)
    old_marker = plugin_dir / "old-layout-marker.txt"
    old_marker.write_text("keep me\n", encoding="utf-8")

    env = _deploy_env(home)
    env["HERMES_INFOFLOW_ENTRYPOINT_POLICY"] = "invalid"
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "hermes_infoflow" / "deploy.py"),
            "--source",
            str(_REPO_ROOT),
            "--port",
            "4447",
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "HERMES_INFOFLOW_ENTRYPOINT_POLICY" in result.stderr
    assert old_marker.read_text(encoding="utf-8") == "keep me\n"
    assert not (plugin_dir / "adapter.py").exists()
    assert not list(plugin_dir.parent.glob(".infoflow.normalize-backup-*"))


def test_package_files_flattened_at_root(deployed: Path) -> None:
    for name in _EXPECTED_PACKAGE_FILES:
        assert (deployed / name).is_file(), (
            f"expected {name} at the plugin dir root after flattening; got:\n"
            f"{_tree(deployed)}"
        )


def test_plugin_yaml_at_root(deployed: Path) -> None:
    assert (deployed / "plugin.yaml").is_file(), (
        f"plugin.yaml must be re-synced to the plugin dir root; got:\n"
        f"{_tree(deployed)}"
    )


def test_root_and_package_manifests_list_same_env_vars() -> None:
    """The deployed manifest comes from the repo root, so keep both copies in sync."""
    yaml = pytest.importorskip("yaml")
    repo_root = Path(_REPO_ROOT)

    root_manifest = yaml.safe_load((repo_root / "plugin.yaml").read_text(encoding="utf-8"))
    package_manifest = yaml.safe_load(
        (repo_root / "hermes_infoflow" / "plugin.yaml").read_text(encoding="utf-8")
    )

    def _env_names(manifest: dict, key: str) -> list[str]:
        return [item["name"] for item in manifest.get(key, [])]

    assert _env_names(root_manifest, "requires_env") == _env_names(
        package_manifest, "requires_env"
    )
    assert _env_names(root_manifest, "optional_env") == _env_names(
        package_manifest, "optional_env"
    )


def test_scripts_synced_for_extract_mode_reruns(deployed: Path) -> None:
    # `hermes-infoflow-tools update --mode extract` falls back to
    # plugin_dir/scripts/lib/deploy-common.sh on re-runs (the sdist
    # tarball it just extracted is already gone). Keep the script tree
    # in the deployed layout so that path keeps working.
    assert (deployed / "scripts" / "lib" / "deploy-common.sh").is_file()
    assert (deployed / "scripts" / "lib" / "edit_hermes_config.py").is_file()
    assert (deployed / "scripts" / "lib" / "edit_hermes_env.py").is_file()


def test_flat_layout_config_script_loads_shared_editor(deployed: Path) -> None:
    pytest.importorskip("yaml")
    script = deployed / "scripts" / "lib" / "edit_hermes_config.py"
    spec = importlib.util.spec_from_file_location(
        "deployed_edit_hermes_config",
        script,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert "terminal" in module.DEFAULT_INFOFLOW_PLATFORM_TOOLSETS


def test_deploy_seeds_default_port_in_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run_deploy(home)
    assert result.returncode == 0, result.stderr
    env_file = home / ".hermes" / ".env"
    assert env_file.is_file()
    assert _read_env_port(env_file) == "26521"


def test_deploy_preserves_existing_port(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run_deploy(home, pre_env_lines="INFOFLOW_PORT=7777\n")
    assert result.returncode == 0, result.stderr
    assert _read_env_port(home / ".hermes" / ".env") == "7777"


def test_deploy_port_flag_overrides_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run_deploy(
        home,
        ["--port", "3333"],
        pre_env_lines="INFOFLOW_PORT=7777\n",
    )
    assert result.returncode == 0, result.stderr
    assert _read_env_port(home / ".hermes" / ".env") == "3333"


def test_deploy_without_launchd_or_hermes_cli_has_clean_stderr(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"

    result = _run_deploy(home)

    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr
    assert "no launchd gateway plist/label found" in result.stdout
    assert "hermes CLI not on PATH" in result.stdout


def test_deploy_auto_restart_uses_launchctl_when_cli_status_fails(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    fakebin = tmp_path / "fakebin"
    _seed_launchd_gateway_plist(home)
    launchctl_log, hermes_log = _install_fake_launchd_tools(fakebin)

    env = _deploy_env(home)
    env["PATH"] = f"{fakebin}:/usr/bin:/bin"
    env["FAKE_HERMES_PYTHON"] = sys.executable
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    env["FAKE_HERMES_LOG"] = str(hermes_log)
    env["HERMES_INFOFLOW_ENTRYPOINT_POLICY"] = "keep"

    result = subprocess.run(
        ["bash", str(_DEPLOY_SCRIPT)],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "launchctl kickstart -k gui/501/ai.hermes.gateway" in result.stdout
    assert launchctl_log.read_text(encoding="utf-8") == (
        "kickstart -k gui/501/ai.hermes.gateway\n"
    )
    assert not hermes_log.exists()


def test_deploy_common_prefers_launchd_gateway_python_over_python_env(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    fakebin = tmp_path / "fakebin"
    plugin_dir = home / ".hermes" / "plugins" / "infoflow"
    _seed_launchd_gateway_plist(home)
    _install_fake_launchd_tools(fakebin)
    (fakebin / "hermes").unlink()

    env = _deploy_env(home)
    env["PATH"] = f"{fakebin}:/usr/bin:/bin"
    env["PYTHON"] = "/bin/sh"
    env["FAKE_HERMES_PYTHON"] = sys.executable
    env["FAKE_LAUNCHCTL_LOG"] = str(tmp_path / "launchctl.log")
    env["FAKE_HERMES_LOG"] = str(tmp_path / "hermes.log")
    env["HERMES_INFOFLOW_ENTRYPOINT_POLICY"] = "keep"

    result = subprocess.run(
        [
            "bash",
            str(_DEPLOY_COMMON_SCRIPT),
            "--plugin-dir",
            str(plugin_dir),
            "--config-file",
            str(home / ".hermes" / "config.yaml"),
            "--dry-run",
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"candidate interpreters: {sys.executable}" in result.stdout
    assert f"hermes-linked interpreters: {sys.executable}" in result.stdout
    assert "hermes-linked interpreters: /bin/sh" not in result.stdout


def test_deploy_dry_run_rejects_invalid_port(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run_deploy(home, ["--port", "99999", "--dry-run"])
    assert result.returncode != 0
    assert "--port must be an integer 1-65535" in result.stderr


def test_deploy_rejects_noncanonical_plugin_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run_deploy(
        home,
        extra_env={"HERMES_INFOFLOW_PLUGIN_ID": "infoflow-dev"},
    )
    assert result.returncode != 0
    assert "only supports plugin id 'infoflow'" in result.stderr


def test_deploy_common_dry_run_rejects_invalid_port(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [
            "bash",
            str(_DEPLOY_COMMON_SCRIPT),
            "--plugin-dir",
            str(home / ".hermes" / "plugins" / "infoflow"),
            "--config-file",
            str(home / ".hermes" / "config.yaml"),
            "--port",
            "99999",
            "--dry-run",
        ],
        cwd=str(_REPO_ROOT),
        env=_deploy_env(home),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--port must be an integer 1-65535" in result.stderr


def test_no_nested_package_dir(deployed: Path) -> None:
    nested = deployed / "hermes_infoflow"
    assert not nested.exists(), (
        "deploy must flatten hermes_infoflow/ into the plugin dir root; "
        f"found nested package dir at {nested}.\nLayout was:\n{_tree(deployed)}"
    )


def test_config_yaml_enabled_plugin(deployed: Path) -> None:
    """edit_hermes_config.py should have appended 'infoflow' to plugins.enabled."""
    yaml = pytest.importorskip("yaml")
    config_path = deployed.parent.parent / "config.yaml"
    assert config_path.is_file(), f"config.yaml not written at {config_path}"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    enabled = data.get("plugins", {}).get("enabled", [])
    assert "infoflow" in enabled, (
        f"expected 'infoflow' in plugins.enabled, got {enabled!r}"
    )


def test_config_yaml_infoflow_platform_toolsets(deployed: Path) -> None:
    """Deploy should grant infoflow the same baseline tools as CLI sessions."""
    yaml = pytest.importorskip("yaml")
    config_path = deployed.parent.parent / "config.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    infoflow_toolsets = data.get("platform_toolsets", {}).get("infoflow", [])

    expected = {
        "browser",
        "clarify",
        "code_execution",
        "computer_use",
        "cronjob",
        "delegation",
        "file",
        "hermes-infoflow",
        "image_gen",
        "memory",
        "messaging",
        "session_search",
        "skills",
        "terminal",
        "todo",
        "tts",
        "vision",
        "web",
    }
    assert expected.issubset(set(infoflow_toolsets))


def test_deploy_preserves_custom_infoflow_toolsets(tmp_path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    home = tmp_path / "home"
    result = _run_deploy(
        home,
        pre_config_lines=(
            "platform_toolsets:\n"
            "  cli:\n"
            "  - terminal\n"
            "  - web\n"
            "  infoflow:\n"
            "  - custom-mcp\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    data = yaml.safe_load((home / ".hermes" / "config.yaml").read_text(encoding="utf-8"))
    infoflow_toolsets = data["platform_toolsets"]["infoflow"]
    assert infoflow_toolsets[0] == "custom-mcp"
    assert "terminal" in infoflow_toolsets
    assert "web" in infoflow_toolsets
    assert "hermes-infoflow" in infoflow_toolsets


def test_flat_layout_loads_like_hermes_agent_does(deployed: Path) -> None:
    """Mimic ``hermes_cli/plugins.py::_load_directory_module`` end-to-end.

    This proves the flattened layout produces a module hermes-agent can
    import and find ``register()`` on. Calling ``register()`` itself still
    requires hermes-agent runtime symbols.
    """
    init_file = deployed / "__init__.py"
    ns_parent = "hermes_plugins_test_layout"
    module_name = f"{ns_parent}.infoflow"

    if ns_parent not in sys.modules:
        ns_pkg = types.ModuleType(ns_parent)
        ns_pkg.__path__ = []  # type: ignore[attr-defined]
        ns_pkg.__package__ = ns_parent
        sys.modules[ns_parent] = ns_pkg

    spec = importlib.util.spec_from_file_location(
        module_name,
        init_file,
        submodule_search_locations=[str(deployed)],
    )
    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(deployed)]  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        assert callable(getattr(module, "register", None)), (
            "loaded module has no register() entry point"
        )
    finally:
        # Tidy up so other tests aren't surprised by stale modules.
        for name in list(sys.modules):
            if name == ns_parent or name.startswith(ns_parent + "."):
                sys.modules.pop(name, None)


def test_plugins_install_layout_loads_like_hermes_agent_does() -> None:
    """Mimic ``hermes plugins install`` — repo root IS the plugin dir.

    ``hermes plugins install`` does ``git clone --depth 1`` + ``shutil.move``
    into ``~/.hermes/plugins/infoflow/``.  The repo root ships a
    ``__init__.py`` that re-exports from ``hermes_infoflow/``; the directory
    loader sets ``submodule_search_locations=[plugin_dir]`` so the import
    resolves against ``plugin_dir/hermes_infoflow/``.

    This test uses the actual repo root as the plugin dir — no flattening,
    no deploy.sh — to prove that path works.
    """
    repo_root = Path(_REPO_ROOT)
    init_file = repo_root / "__init__.py"
    assert init_file.is_file(), (
        "root __init__.py missing — hermes plugins install would fail"
    )

    ns_parent = "hermes_plugins_test_git_install"
    module_name = f"{ns_parent}.infoflow"

    if ns_parent not in sys.modules:
        ns_pkg = types.ModuleType(ns_parent)
        ns_pkg.__path__ = []  # type: ignore[attr-defined]
        ns_pkg.__package__ = ns_parent
        sys.modules[ns_parent] = ns_pkg

    spec = importlib.util.spec_from_file_location(
        module_name,
        init_file,
        submodule_search_locations=[str(repo_root)],
    )
    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(repo_root)]  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        assert callable(getattr(module, "register", None)), (
            "loaded module has no register() entry point"
        )
    finally:
        for name in list(sys.modules):
            if name == ns_parent or name.startswith(ns_parent + "."):
                sys.modules.pop(name, None)


def test_root_init_py_mirrors_package_exports() -> None:
    """Root ``__init__.py`` and ``hermes_infoflow/__init__.py`` must export
    the same ``__all__`` and ``__version__`` so ``hermes plugins install``
    and pip installs behave identically.
    """
    repo_root = Path(_REPO_ROOT)
    import hermes_infoflow

    ns_parent = "hermes_plugins_test_root_exports"
    module_name = f"{ns_parent}.infoflow"
    ns_pkg = types.ModuleType(ns_parent)
    ns_pkg.__path__ = []  # type: ignore[attr-defined]
    ns_pkg.__package__ = ns_parent
    sys.modules[ns_parent] = ns_pkg

    spec = importlib.util.spec_from_file_location(
        module_name,
        repo_root / "__init__.py",
        submodule_search_locations=[str(repo_root)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(repo_root)]  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        assert module.__all__ == hermes_infoflow.__all__
        assert module.__version__ == hermes_infoflow.__version__
    finally:
        for name in list(sys.modules):
            if name == ns_parent or name.startswith(ns_parent + "."):
                sys.modules.pop(name, None)
