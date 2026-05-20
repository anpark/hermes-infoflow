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

# Every file currently in hermes_infoflow/ should end up at the plugin
# dir root (one level shallower than the repo). Kept as a literal set so
# a renamed/dropped module is caught loudly here too.
_EXPECTED_PACKAGE_FILES = {
    "__init__.py",
    "adapter.py",
    "api.py",
    "crypto.py",
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


@pytest.fixture
def deployed(tmp_path: Path) -> Path:
    """Run ``scripts/deploy.sh`` against an isolated $HOME and return the plugin dir."""
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    # The test interpreter (pytest) already has cryptography / aiohttp /
    # pyyaml available — its deps are what we're testing against — so
    # force the deploy script to use it instead of the auto-detected
    # hermes-agent Python.
    env["PYTHON"] = sys.executable
    # Sanitize PATH so `command -v hermes` returns nothing and
    # deploy-common.sh takes the "hermes CLI not on PATH; skipping
    # gateway restart" branch. /usr/bin and /bin together cover
    # rsync / mkdir / head / bash on macOS and Linux.
    env["PATH"] = "/usr/bin:/bin"

    result = subprocess.run(
        ["bash", str(_DEPLOY_SCRIPT)],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
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


def test_scripts_synced_for_extract_mode_reruns(deployed: Path) -> None:
    # `hermes-infoflow-tools update --mode extract` falls back to
    # plugin_dir/scripts/lib/deploy-common.sh on re-runs (the sdist
    # tarball it just extracted is already gone). Keep the script tree
    # in the deployed layout so that path keeps working.
    assert (deployed / "scripts" / "lib" / "deploy-common.sh").is_file()
    assert (deployed / "scripts" / "lib" / "edit_hermes_config.py").is_file()


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


def test_flat_layout_loads_like_hermes_agent_does(deployed: Path) -> None:
    """Mimic ``hermes_cli/plugins.py::_load_directory_module`` end-to-end.

    Skipped when hermes-agent isn't importable — same gating as
    test_registration.py — because the package's adapter imports
    ``gateway.platform_registry``. When it IS importable, this is the
    most valuable test: it proves the flattened layout produces a
    module hermes-agent can actually load and call ``register()`` on.
    """
    pytest.importorskip("gateway.platform_registry")

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


def test_plugins_install_layout_loads_like_hermes_agent_does(_REPO_ROOT) -> None:
    """Mimic ``hermes plugins install`` — repo root IS the plugin dir.

    ``hermes plugins install`` does ``git clone --depth 1`` + ``shutil.move``
    into ``~/.hermes/plugins/infoflow/``.  The repo root ships a
    ``__init__.py`` that re-exports from ``hermes_infoflow/``; the directory
    loader sets ``submodule_search_locations=[plugin_dir]`` so the import
    resolves against ``plugin_dir/hermes_infoflow/``.

    This test uses the actual repo root as the plugin dir — no flattening,
    no deploy.sh — to prove that path works.
    """
    pytest.importorskip("gateway.platform_registry")

    repo_root = Path(_REPO_ROOT)
    init_file = repo_root / "__init__.py"
    assert init_file.is_file(), (
        f"root __init__.py missing — hermes plugins install would fail"
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

    import ast

    root_init = ast.parse((repo_root / "__init__.py").read_text())
    pkg_init = ast.parse(
        (repo_root / "hermes_infoflow" / "__init__.py").read_text()
    )

    # Extract __all__ lists
    def _get_all(tree: ast.AST) -> list[str]:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        if isinstance(node.value, ast.List):
                            return [
                                elt.value
                                for elt in node.value.elts
                                if isinstance(elt, ast.Constant)
                                and isinstance(elt.value, str)
                            ]
        return []

    root_all = _get_all(root_init)
    pkg_all = _get_all(pkg_init)
    assert root_all == pkg_all, (
        f"root __init__.py __all__ ({root_all}) != "
        f"hermes_infoflow/__init__.py __all__ ({pkg_all})"
    )

    # Extract __version__
    def _get_version(tree: ast.AST) -> str | None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__version__":
                        if isinstance(node.value, ast.Constant):
                            return node.value.value
        return None

    root_ver = _get_version(root_init)
    pkg_ver = _get_version(pkg_init)
    # Root __init__.py imports __version__ from hermes_infoflow rather than
    # defining it inline, so _get_version returns None for the root.
    # Verify that the import exists and the sub-package version is set.
    assert pkg_ver is not None, "hermes_infoflow/__init__.py has no __version__"
    # Verify root has a `from hermes_infoflow import ... __version__` import.
    has_version_import = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "hermes_infoflow"
        and any(alias.name == "__version__" for alias in node.names)
        for node in ast.walk(root_init)
    )
    assert has_version_import, (
        "root __init__.py must import __version__ from hermes_infoflow"
    )
