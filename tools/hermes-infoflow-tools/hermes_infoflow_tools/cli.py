"""``hermes-infoflow-tools update`` — hybrid installer for the Infoflow plugin.

Mirrors openclaw-infoflow/tools/infoflow-openclaw-tools/bin/cli.mjs in
purpose and CLI shape, but is implemented in pure Python (stdlib + the
system's ``pip`` / ``tar`` / ``rsync`` / ``bash``).

Two installation modes:

* ``--mode extract`` (default) — Download the ``hermes-infoflow`` sdist
  from PyPI, untar it, and rsync the contents to
  ``~/.hermes/plugins/infoflow/``. This matches the OpenClaw experience
  (per-user directory plugin, easy to inspect / patch / remove).

* ``--mode pip`` — ``pip install --upgrade hermes-infoflow==<ver>`` into
  the active Python environment. The plugin is then discovered via the
  ``hermes_agent.plugins`` entry-point. Note that, in this mode, hermes
  does NOT read ``plugin.yaml`` so ``hermes config`` won't list the
  ``INFOFLOW_*`` env vars in its setup wizard. Users must export them
  manually or run ``hermes config set INFOFLOW_*``.

Both modes finish by calling ``edit_hermes_config.py`` to ensure
``plugins.enabled`` contains ``infoflow``, and (in extract mode) by
running ``scripts/lib/deploy-common.sh`` to optionally restart the
gateway.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from .env_editor import (
    DEFAULT_INFOFLOW_PORT,
    ensure_key,
    read_key,
    upsert_key,
    validate_port_value,
)

DEFAULT_PACKAGE = "hermes-infoflow"
DEFAULT_INDEX_URL = "https://pypi.org/simple"
DEFAULT_CHANNEL_ID = "infoflow"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_port_arg(value: str) -> int:
    try:
        validate_port_value(value)
    except SystemExit as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return int(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-infoflow-tools",
        description=(
            "Install or update the hermes-infoflow plugin into a Hermes "
            "Agent home directory."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    upd = sub.add_parser(
        "update",
        help="Install or update the hermes-infoflow plugin.",
    )
    upd.add_argument(
        "--version",
        default="latest",
        help=(
            "Plugin version (PyPI version specifier, e.g. 0.1.0 or 0.1.0b1). "
            "`latest` means the most recent stable release."
        ),
    )
    upd.add_argument(
        "--index-url",
        default=DEFAULT_INDEX_URL,
        help="PyPI index URL (default: https://pypi.org/simple)",
    )
    upd.add_argument(
        "--package-name",
        default=DEFAULT_PACKAGE,
        help=f"Plugin package on PyPI (default: {DEFAULT_PACKAGE})",
    )
    upd.add_argument(
        "--channel-id",
        default=DEFAULT_CHANNEL_ID,
        help=(
            "Plugin id / directory name under ~/.hermes/plugins/. "
            "Must match the plugin's `register(name=...)` value; "
            "do NOT change unless you know what you're doing."
        ),
    )
    upd.add_argument(
        "--mode",
        choices=("extract", "pip"),
        default="extract",
        help=(
            "extract = unpack sdist into ~/.hermes/plugins/<id>/ (default; "
            "mirrors OpenClaw experience). "
            "pip = pip install into site-packages (Pythonic; loads via "
            "entry-point, but setup wizard won't see plugin.yaml)."
        ),
    )
    upd.add_argument(
        "--port",
        type=_parse_port_arg,
        metavar="PORT",
        default=None,
        help=(
            "Webhook listen port (1-65535). Written to ~/.hermes/.env as "
            "INFOFLOW_PORT. Without this flag, an existing value is kept and "
            f"{DEFAULT_INFOFLOW_PORT} is seeded only when missing."
        ),
    )
    upd.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing them.",
    )
    return parser


# ---------------------------------------------------------------------------
# Command runner
# ---------------------------------------------------------------------------


class _Runner:
    """Subprocess runner that respects ``--dry-run``."""

    def __init__(self, *, dry_run: bool):
        self.dry_run = dry_run

    def __call__(self, cmd: list[str], *, cwd: Path | None = None) -> None:
        cwd_label = f"({cwd})" if cwd else ""
        printable = " ".join(cmd)
        print(f"$ {cwd_label} {printable}".strip())
        if self.dry_run:
            return
        result = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
        if result.returncode != 0:
            sys.exit(result.returncode)


def _configure_infoflow_port(
    hermes_home: Path,
    port: int | None,
    *,
    dry_run: bool,
) -> None:
    """Seed or override ``INFOFLOW_PORT`` in ``$HERMES_HOME/.env``."""
    env_file = hermes_home / ".env"
    print(f"==> Configuring INFOFLOW_PORT in {env_file}")
    if port is not None:
        validate_port_value(str(port))
        if dry_run:
            print(
                f"[edit_hermes_env] (dry-run) would set INFOFLOW_PORT={port} in {env_file}"
            )
            return
        changed = upsert_key(env_file, "INFOFLOW_PORT", str(port))
    else:
        if dry_run:
            existing = read_key(env_file, "INFOFLOW_PORT")
            if existing is not None:
                print(
                    f"[edit_hermes_env] (dry-run) would leave "
                    f"INFOFLOW_PORT={existing} in {env_file}"
                )
            else:
                print(
                    f"[edit_hermes_env] (dry-run) would ensure "
                    f"INFOFLOW_PORT={DEFAULT_INFOFLOW_PORT} in {env_file}"
                )
            return
        changed = ensure_key(env_file, "INFOFLOW_PORT", str(DEFAULT_INFOFLOW_PORT))

    if changed:
        current = read_key(env_file, "INFOFLOW_PORT")
        print(f"[edit_hermes_env] updated {env_file} (INFOFLOW_PORT={current})")
    else:
        print(f"[edit_hermes_env] no change needed ({env_file}: INFOFLOW_PORT already set)")


def _resolve_pip_version_spec(package: str, version: str) -> str:
    """Return ``package==version`` (or just ``package`` for ``latest``)."""
    if version in ("", "latest"):
        return package
    return f"{package}=={version}"


def _package_glob_stem(package_name: str) -> str:
    """Return the normalized sdist stem for a package name or local path."""
    stem = Path(package_name).name or DEFAULT_PACKAGE
    return re.sub(r"[-_.]+", "_", stem).lower()


def _ensure_plugin_enabled(config_file: Path, plugin_id: str, *, dry_run: bool) -> bool:
    """Ensure ``plugins.enabled`` contains ``plugin_id``.

    The main package installs PyYAML as a runtime dependency, so pip mode can
    safely use it after the package install step.  Dry-run still works in a
    bare tools environment by printing the intended change.
    """
    if dry_run:
        print(f"$ ensure {plugin_id!r} is present in {config_file}:plugins.enabled")
        print(
            f"$ ensure {config_file}:platform_toolsets.{plugin_id} "
            "includes CLI tool permissions"
        )
        return True

    try:
        import yaml
    except ImportError as exc:
        raise SystemExit(
            "PyYAML is required to edit Hermes config. "
            "Install hermes-infoflow first or run: pip install pyyaml"
        ) from exc

    def _load(path: Path) -> dict[str, Any]:
        if not path.exists() or path.stat().st_size == 0:
            return {}
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise SystemExit(f"refusing to edit {path}: top-level YAML is not a mapping")
        return parsed

    data = _load(config_file)
    changed = _load_config_editor().apply(data, plugin_id)
    if not changed:
        print(f"[pip mode] no config change needed ({plugin_id} already enabled)")
        return False

    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        yaml.safe_dump(
            data,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    print(f"[pip mode] enabled {plugin_id} in {config_file}")
    return True


def _load_config_editor():
    try:
        from hermes_infoflow import config_editor
    except Exception as exc:
        raise SystemExit(
            "hermes-infoflow must be installed before editing Hermes config. "
            "The preceding pip install step should have provided "
            "hermes_infoflow.config_editor."
        ) from exc
    return config_editor


# ---------------------------------------------------------------------------
# Mode: extract
# ---------------------------------------------------------------------------


def _find_sdist(tmp_dir: Path, package_name: str) -> Path:
    """Find the freshly-downloaded sdist tarball in ``tmp_dir``."""
    # pip download names tarballs after PEP 503 normalized form
    # (underscores instead of hyphens), e.g. hermes_infoflow-0.1.0.tar.gz.
    normalized = _package_glob_stem(package_name)
    candidates = sorted(tmp_dir.glob(f"{normalized}-*.tar.gz"))
    if candidates:
        return candidates[-1]
    candidates = sorted(tmp_dir.glob("*.tar.gz"))
    if candidates:
        return candidates[-1]
    raise SystemExit(f"failed to locate sdist tarball under {tmp_dir}")


def _extracted_dir(tmp_dir: Path, package_name: str) -> Path:
    """Return the path the sdist's contents were extracted to."""
    normalized = _package_glob_stem(package_name)
    matches = sorted(p for p in tmp_dir.iterdir() if p.is_dir() and p.name.startswith(normalized + "-"))
    if matches:
        return matches[-1]
    matches = sorted(p for p in tmp_dir.iterdir() if p.is_dir() and "-" in p.name)
    if matches:
        return matches[-1]
    raise SystemExit(f"failed to locate extracted package directory under {tmp_dir}")


def _looks_like_local_path(value: str) -> bool:
    """Return True iff *value* should be interpreted as a filesystem path.

    A bare PyPI name (``hermes-infoflow``) must NEVER be treated as a path,
    even when the cwd happens to contain a directory of the same name —
    otherwise running ``hermes-infoflow-tools update`` from a parent of a
    checkout would silently install from that checkout. Only treat as a
    path when the value is explicitly written as one: absolute,
    ``./...``, ``../...``, or ``~/...``.
    """
    if not value:
        return False
    if value.startswith(("/", "./", "../", "~")):
        return True
    return os.path.isabs(value)


def _do_extract(args, hermes_home: Path) -> int:
    runner = _Runner(dry_run=args.dry_run)
    plugin_dir = hermes_home / "plugins" / args.channel_id
    config_file = hermes_home / "config.yaml"
    local_source: Path | None = None
    if _looks_like_local_path(args.package_name):
        candidate = Path(args.package_name).expanduser()
        if candidate.is_dir():
            local_source = candidate
    use_local_source = local_source is not None

    tmp_root = Path(tempfile.mkdtemp(prefix="hermes-infoflow-tools-"))
    try:
        # 1) pip download sdist (skipped when --package-name points at a
        # local directory checkout — handy for dev / private mirrors).
        if use_local_source:
            assert local_source is not None  # narrowed above
            print(f"$ use local source {local_source.resolve()}")
            if args.version not in ("", "latest"):
                print(
                    f"  note: --version {args.version!r} is ignored in local-source mode"
                )
        else:
            spec = _resolve_pip_version_spec(args.package_name, args.version)
            runner(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "download",
                    "--no-deps",
                    "--no-binary=:all:",
                    "-d",
                    str(tmp_root),
                    "-i",
                    args.index_url,
                    spec,
                ]
            )

        # 2) Extract
        if use_local_source:
            extracted_label = str(local_source.resolve())
        elif args.dry_run:
            print(f"$ tar -xzf <sdist tarball under {tmp_root}>")
            extracted_label = str(tmp_root / f"<extracted {args.package_name}>")
        else:
            tarball = _find_sdist(tmp_root, args.package_name)
            with tarfile.open(tarball, "r:gz") as tar:
                _safe_extract(tar, tmp_root)
            extracted_label = str(_extracted_dir(tmp_root, args.package_name))

        # 3) rsync into the plugin dir.
        #
        # hermes-agent's directory-plugin loader requires ``__init__.py``
        # to live directly at ``plugin_dir`` (see
        # hermes_cli/plugins.py::_load_directory_module). The sdist
        # tarball keeps the source nested inside ``hermes_infoflow/`` for
        # PyPI / entry-point installs, so we flatten the layout here:
        #
        #   1. ``<extracted>/hermes_infoflow/*``  →  ``plugin_dir/*``
        #   2. ``<extracted>/plugin.yaml``        →  ``plugin_dir/plugin.yaml``
        #   3. ``<extracted>/scripts/``           →  ``plugin_dir/scripts/``
        #
        # Internal imports inside the package are all relative
        # (``from .adapter import register``) and hermes-agent points the
        # loaded module's ``submodule_search_locations`` at ``plugin_dir``,
        # so the relative imports keep resolving after flattening.
        runner(["mkdir", "-p", str(plugin_dir)])
        # Step 1: package contents → plugin_dir/ (flatten + clean stale files)
        runner(
            [
                "rsync",
                "-av",
                "--delete",
                "--exclude",
                "__pycache__",
                "--exclude",
                "*.pyc",
                f"{extracted_label}/hermes_infoflow/",
                f"{plugin_dir}/",
            ]
        )
        # Step 2: plugin.yaml on top (manifest lives at sdist root, not in the
        # package dir).
        runner(
            [
                "rsync",
                "-av",
                f"{extracted_label}/plugin.yaml",
                f"{plugin_dir}/plugin.yaml",
            ]
        )
        # Step 3: scripts/ on top so deploy-common.sh stays available for
        # subsequent re-runs of `hermes-infoflow-tools update`.
        runner(
            [
                "rsync",
                "-av",
                "--delete",
                "--exclude",
                "__pycache__",
                "--exclude",
                "*.pyc",
                f"{extracted_label}/scripts/",
                f"{plugin_dir}/scripts/",
            ]
        )

        # 4) Hand off to deploy-common.sh for config + gateway restart.
        # Prefer the freshly-installed copy; fall back to the in-repo copy
        # if this is a dev checkout (extract mode targeting the same repo).
        common_script = plugin_dir / "scripts" / "lib" / "deploy-common.sh"
        if not common_script.exists():
            here = Path(__file__).resolve().parent
            repo_root = here.parent.parent.parent  # tools/hermes-infoflow-tools/hermes_infoflow_tools/cli.py → repo root
            candidate = repo_root / "scripts" / "lib" / "deploy-common.sh"
            if candidate.exists():
                common_script = candidate
        common_args = [
            "bash",
            str(common_script),
            "--plugin-dir",
            str(plugin_dir),
            "--plugin-id",
            args.channel_id,
            "--config-file",
            str(config_file),
        ]
        if args.port is not None:
            common_args.extend(["--port", str(args.port)])
        if args.dry_run:
            common_args.append("--dry-run")
        runner(common_args)
        return 0
    finally:
        if not args.dry_run:
            shutil.rmtree(tmp_root, ignore_errors=True)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """tarfile.extractall with a guard against path traversal."""
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError as exc:
            raise SystemExit(
                f"refusing to extract tarball member outside of {dest_resolved}: {member.name}"
            ) from exc
    tar.extractall(dest)


# ---------------------------------------------------------------------------
# Mode: pip
# ---------------------------------------------------------------------------


def _do_pip(args, hermes_home: Path) -> int:
    runner = _Runner(dry_run=args.dry_run)
    spec = _resolve_pip_version_spec(args.package_name, args.version)
    config_file = hermes_home / "config.yaml"

    runner(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "-i",
            args.index_url,
            spec,
        ]
    )

    # The entry-point installer doesn't unpack plugin.yaml anywhere hermes
    # will see it, so we only update plugins.enabled here. Users still need
    # to set INFOFLOW_* env vars themselves.
    _ensure_plugin_enabled(config_file, args.channel_id, dry_run=args.dry_run)
    _configure_infoflow_port(hermes_home, args.port, dry_run=args.dry_run)

    print(
        "[pip mode] plugin installed via entry-point. Reminder: hermes "
        "does NOT read plugin.yaml in this mode, so the setup wizard "
        "will not list INFOFLOW_* env vars. Export them manually or "
        "run `hermes config set INFOFLOW_*` before starting the gateway.",
    )
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command != "update":  # pragma: no cover - argparse already guards
        parser.print_help()
        return 1

    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))

    if args.mode == "extract":
        return _do_extract(args, hermes_home)
    if args.mode == "pip":
        return _do_pip(args, hermes_home)
    return 1  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
