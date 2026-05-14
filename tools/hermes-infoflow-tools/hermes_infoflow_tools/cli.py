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
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

DEFAULT_PACKAGE = "hermes-infoflow"
DEFAULT_INDEX_URL = "https://pypi.org/simple"
DEFAULT_CHANNEL_ID = "infoflow"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


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


def _resolve_pip_version_spec(package: str, version: str) -> str:
    """Return ``package==version`` (or just ``package`` for ``latest``)."""
    if version in ("", "latest"):
        return package
    return f"{package}=={version}"


# ---------------------------------------------------------------------------
# Mode: extract
# ---------------------------------------------------------------------------


def _find_sdist(tmp_dir: Path, package_name: str) -> Path:
    """Find the freshly-downloaded sdist tarball in ``tmp_dir``."""
    # pip download names tarballs after PEP 503 normalized form
    # (underscores instead of hyphens), e.g. hermes_infoflow-0.1.0.tar.gz.
    normalized = package_name.replace("-", "_")
    candidates = sorted(tmp_dir.glob(f"{normalized}-*.tar.gz"))
    if candidates:
        return candidates[-1]
    candidates = sorted(tmp_dir.glob("*.tar.gz"))
    if candidates:
        return candidates[-1]
    raise SystemExit(f"failed to locate sdist tarball under {tmp_dir}")


def _extracted_dir(tmp_dir: Path, package_name: str) -> Path:
    """Return the path the sdist's contents were extracted to."""
    normalized = package_name.replace("-", "_")
    matches = sorted(p for p in tmp_dir.iterdir() if p.is_dir() and p.name.startswith(normalized + "-"))
    if matches:
        return matches[-1]
    matches = sorted(p for p in tmp_dir.iterdir() if p.is_dir() and "-" in p.name)
    if matches:
        return matches[-1]
    raise SystemExit(f"failed to locate extracted package directory under {tmp_dir}")


def _do_extract(args, hermes_home: Path) -> int:
    runner = _Runner(dry_run=args.dry_run)
    plugin_dir = hermes_home / "plugins" / args.channel_id
    config_file = hermes_home / "config.yaml"

    tmp_root = Path(tempfile.mkdtemp(prefix="hermes-infoflow-tools-"))
    try:
        # 1) pip download sdist
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
        if args.dry_run:
            print(f"$ tar -xzf <sdist tarball under {tmp_root}>")
        else:
            tarball = _find_sdist(tmp_root, args.package_name)
            with tarfile.open(tarball, "r:gz") as tar:
                _safe_extract(tar, tmp_root)

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
        if args.dry_run:
            extracted_label = str(tmp_root / f"<extracted {args.package_name}>")
        else:
            extracted_label = str(_extracted_dir(tmp_root, args.package_name))

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
    # will see it, so we *only* update plugins.enabled here. The user is
    # responsible for setting INFOFLOW_* env vars themselves.
    here = Path(__file__).resolve().parent
    repo_root = here.parent.parent.parent
    edit_script = repo_root / "scripts" / "lib" / "edit_hermes_config.py"
    if not edit_script.exists():
        # Fall back to whatever hermes plugin dir we may have installed
        # earlier (dev convenience).
        edit_script = hermes_home / "plugins" / args.channel_id / "scripts" / "lib" / "edit_hermes_config.py"
    if not edit_script.exists():
        print(
            "[pip mode] note: edit_hermes_config.py not on disk; "
            f"please add `{args.channel_id}` to plugins.enabled in "
            f"{config_file} manually.",
        )
        return 0

    edit_cmd = [
        sys.executable,
        str(edit_script),
        "--config-file",
        str(config_file),
        "--plugin-id",
        args.channel_id,
    ]
    if args.dry_run:
        edit_cmd.append("--dry-run")
    runner(edit_cmd)

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
