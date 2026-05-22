"""``hermes-infoflow-tools update`` — hybrid installer for the Infoflow plugin.

Mirrors openclaw-infoflow/tools/infoflow-openclaw-tools/bin/cli.mjs in
purpose and CLI shape, but is implemented in pure Python (stdlib + the
system's ``pip`` / ``tar`` / ``bash``).

Two installation modes:

* ``--mode extract`` (default) — Download the ``hermes-infoflow`` sdist
  from PyPI, untar it, and normalize the contents into
  ``~/.hermes/plugins/infoflow/``. This matches the OpenClaw experience
  (per-user directory plugin, easy to inspect / patch / remove).

* ``--mode pip`` — Backward-compatible alias for the same directory-style
  deployment. Older releases installed an entry-point package into the active
  Python environment, which could shadow ``~/.hermes/plugins/infoflow``. This
  tool now keeps the Hermes plugin source of truth in the directory plugin.

Both update modes finish by running ``scripts/lib/deploy-common.sh`` to ensure
``plugins.enabled`` contains ``infoflow``, seed ``INFOFLOW_PORT``, remove
shadowing entry-point installs from the Hermes runtime when found, and
optionally restart the gateway.

The ``normalize`` subcommand is for installs created by ``hermes plugins
install``: it runs the package's ``hermes_infoflow/deploy.py`` in-place so the
Git-cloned layout is flattened into the same directory-style deployment as
``update`` and ``scripts/deploy.sh``.
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

from .env_editor import (
    DEFAULT_INFOFLOW_PORT,
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


def _parse_channel_id_arg(value: str) -> str:
    if value != DEFAULT_CHANNEL_ID:
        raise argparse.ArgumentTypeError(
            "hermes-infoflow only supports plugin id "
            f"{DEFAULT_CHANNEL_ID!r}; got {value!r}"
        )
    return value


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
        type=_parse_channel_id_arg,
        help=(
            "Plugin id / directory name under ~/.hermes/plugins/. "
            "Only 'infoflow' is supported so all deployment paths overwrite "
            "the same Hermes plugin."
        ),
    )
    upd.add_argument(
        "--mode",
        choices=("extract", "pip"),
        default="extract",
        help=(
            "extract = unpack sdist into ~/.hermes/plugins/<id>/ (default; "
            "mirrors OpenClaw experience). "
            "pip = deprecated alias for extract; it still deploys directory-style "
            "so it can safely overwrite deploy.sh installs."
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

    norm = sub.add_parser(
        "normalize",
        help=(
            "Normalize an existing hermes-infoflow plugin directory into the "
            "canonical ~/.hermes/plugins/infoflow layout."
        ),
    )
    norm.add_argument(
        "--source",
        help=(
            "Source plugin/check-out directory. Defaults to "
            "$HERMES_HOME/plugins/infoflow."
        ),
    )
    norm.add_argument(
        "--channel-id",
        default=DEFAULT_CHANNEL_ID,
        type=_parse_channel_id_arg,
        help="Only 'infoflow' is supported.",
    )
    norm.add_argument(
        "--port",
        type=_parse_port_arg,
        metavar="PORT",
        default=None,
        help=(
            "Webhook listen port (1-65535). Written to ~/.hermes/.env as "
            "INFOFLOW_PORT."
        ),
    )
    norm.add_argument(
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


def _package_glob_stem(package_name: str) -> str:
    """Return the normalized sdist stem for a package name or local path."""
    stem = Path(package_name).name or DEFAULT_PACKAGE
    return re.sub(r"[-_.]+", "_", stem).lower()


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

        # 3) Hand off to the main package normalizer. Keep layout rules in
        # exactly one place so tools/deploy.sh/pip-style installs do not drift.
        if use_local_source or not args.dry_run:
            deploy_script = _find_deploy_script(Path(extracted_label))
        else:
            deploy_script = None
        if deploy_script is None:
            if args.dry_run:
                deploy_script = Path(extracted_label) / "hermes_infoflow" / "deploy.py"
            else:
                raise SystemExit(
                    f"Cannot find hermes-infoflow deploy.py under {extracted_label}"
                )

        deploy_args = [
            sys.executable,
            str(deploy_script),
            "--source",
            extracted_label,
            "--hermes-home",
            str(hermes_home),
            "--config-file",
            str(config_file),
        ]
        if args.port is not None:
            deploy_args.extend(["--port", str(args.port)])
        if args.dry_run:
            deploy_args.append("--dry-run")
        runner(deploy_args)
        return 0
    finally:
        if not args.dry_run:
            shutil.rmtree(tmp_root, ignore_errors=True)


def _find_deploy_script(source: Path) -> Path | None:
    candidates = [
        source / "hermes_infoflow" / "deploy.py",
        source / "deploy.py",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _do_normalize(args, hermes_home: Path) -> int:
    runner = _Runner(dry_run=args.dry_run)
    source = Path(args.source).expanduser() if args.source else hermes_home / "plugins" / args.channel_id
    config_file = Path(
        os.environ.get("HERMES_CONFIG_FILE", str(hermes_home / "config.yaml"))
    )

    deploy_script = _find_deploy_script(source)
    if deploy_script is None and not args.dry_run:
        raise SystemExit(
            f"Cannot find hermes-infoflow deploy.py under {source}. "
            "Install with `hermes-infoflow-tools update` or point --source at "
            "a current hermes-infoflow checkout/plugin directory."
        )

    if deploy_script is not None:
        cmd = [sys.executable, str(deploy_script)]
    else:
        # Dry-run fallback: show the module form a pip install would use.
        cmd = [sys.executable, "-m", "hermes_infoflow.deploy"]

    cmd.extend(
        [
            "--source",
            str(source),
            "--hermes-home",
            str(hermes_home),
            "--config-file",
            str(config_file),
        ]
    )
    if args.port is not None:
        cmd.extend(["--port", str(args.port)])
    if args.dry_run:
        cmd.append("--dry-run")

    runner(cmd)
    return 0


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
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))

    if args.command == "update":
        if args.mode == "extract":
            return _do_extract(args, hermes_home)
        if args.mode == "pip":
            print(
                "[pip mode] deprecated: deploying directory-style to "
                f"{hermes_home / 'plugins' / args.channel_id} so it can safely "
                "overwrite deploy.sh/extract installs."
            )
            return _do_extract(args, hermes_home)
    if args.command == "normalize":
        return _do_normalize(args, hermes_home)
    return 1  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
