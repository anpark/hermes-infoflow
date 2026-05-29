"""Normalize hermes-infoflow into the canonical Hermes directory plugin.

This module is intentionally importable from both a source checkout and a
pip-installed wheel.  It converges every supported deployment entry point to
the same on-disk result:

    ~/.hermes/plugins/infoflow/

with the package files flattened at the plugin root, ``plugin.yaml`` at the
root, and the shared deploy scripts under ``scripts/``.
"""

from __future__ import annotations

import os
import sys

# When this file is executed directly as ``python hermes_infoflow/deploy.py``,
# Python prepends the package directory to sys.path.  Remove that direct script
# path before importing the rest of the standard library so flattened plugin
# files can never shadow stdlib modules during bootstrap.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path = [
    p for p in sys.path if os.path.abspath(p or os.curdir) != _THIS_DIR
]

import argparse
import fnmatch
import importlib.metadata as metadata
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CANONICAL_PLUGIN_ID = "infoflow"
DEFAULT_INFOFLOW_PORT = 26521
DIST_NAME = "hermes-infoflow"
DEFAULT_HERMES_AGENT_FORK_URL = "https://github.com/chbo297/hermes-agent.git"
DEFAULT_HERMES_AGENT_FORK_REMOTE = "chbo"
DEFAULT_HERMES_AGENT_FORK_BRANCH = "bduse"

_SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    "_deploy_scripts",
    "__pycache__",
    "hermes_infoflow",
    "scripts",
}
_SKIP_FILE_PATTERNS = ("*.pyc", "*.pyo")


@dataclass(frozen=True)
class SourceLayout:
    source_root: Path
    package_dir: Path
    manifest_file: Path
    scripts_dir: Path


def validate_port(value: str) -> str:
    if not value.isdigit():
        raise argparse.ArgumentTypeError(
            f"--port must be an integer 1-65535 (got: {value})"
        )
    port = int(value)
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError(
            f"--port must be an integer 1-65535 (got: {value})"
        )
    return value


def _print_cmd(cmd: list[str]) -> None:
    print("$ " + " ".join(cmd))


def _run_git(
    agent_dir: Path,
    args: list[str],
    *,
    dry_run: bool,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(agent_dir), *args]
    if not capture_output or dry_run:
        _print_cmd(cmd)
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    try:
        result = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=capture_output,
        )
    except FileNotFoundError as exc:
        raise SystemExit("git is required to deploy hermes-infoflow") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SystemExit(
            f"git command failed ({result.returncode}): {' '.join(cmd)}"
            + (f"\n{detail}" if detail else "")
        )
    return result


def _is_chbo297_hermes_agent_url(url: str) -> bool:
    normalized = url.strip().removesuffix(".git")
    return normalized.endswith("github.com/chbo297/hermes-agent") or (
        "github.com:chbo297/hermes-agent" in normalized
    )


def _same_remote_target(existing_url: str, desired_url: str) -> bool:
    if existing_url.strip().removesuffix(".git") == desired_url.strip().removesuffix(".git"):
        return True
    return _is_chbo297_hermes_agent_url(existing_url) and _is_chbo297_hermes_agent_url(
        desired_url
    )


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _git_output(agent_dir: Path, args: list[str], *, check: bool = True) -> str:
    result = _run_git(
        agent_dir,
        args,
        dry_run=False,
        check=check,
        capture_output=True,
    )
    return (result.stdout or "").strip()


def _git_read_for_dry_run(agent_dir: Path, args: list[str], *, check: bool = True) -> str:
    _print_cmd(["git", "-C", str(agent_dir), *args])
    return _git_output(agent_dir, args, check=check)


def _require_agent_git_checkout(
    agent_dir: Path,
    *,
    fork_url: str,
    fork_branch: str,
) -> None:
    if not agent_dir.is_dir():
        raise SystemExit(
            "hermes-infoflow requires a patched hermes-agent checkout at "
            f"{agent_dir}.\n"
            "Clone it first:\n"
            f"  git clone -b {fork_branch} {fork_url} {agent_dir}"
        )

    result = _run_git(
        agent_dir,
        ["rev-parse", "--is-inside-work-tree"],
        dry_run=False,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 or (result.stdout or "").strip() != "true":
        detail = (result.stderr or result.stdout or "").strip()
        raise SystemExit(
            "hermes-infoflow requires a git checkout for patched hermes-agent "
            f"at {agent_dir}.\n"
            "Clone it first:\n"
            f"  git clone -b {fork_branch} {fork_url} {agent_dir}"
            + (f"\n{detail}" if detail else "")
        )


def _sync_hermes_agent_fork(
    *,
    hermes_home: Path,
    dry_run: bool,
) -> None:
    agent_dir = Path(
        os.environ.get("HERMES_AGENT_DIR", str(hermes_home / "hermes-agent"))
    ).expanduser()
    fork_url = os.environ.get("HERMES_AGENT_FORK_URL", DEFAULT_HERMES_AGENT_FORK_URL)
    fork_remote = os.environ.get(
        "HERMES_AGENT_FORK_REMOTE", DEFAULT_HERMES_AGENT_FORK_REMOTE
    )
    fork_branch = os.environ.get(
        "HERMES_AGENT_FORK_BRANCH", DEFAULT_HERMES_AGENT_FORK_BRANCH
    )

    _require_agent_git_checkout(
        agent_dir,
        fork_url=fork_url,
        fork_branch=fork_branch,
    )

    print("==> Aligning required hermes-agent checkout with fork branch")
    print(f"    repo:   {agent_dir}")
    print(f"    remote: {fork_remote} -> {fork_url}")
    print(f"    branch: {fork_branch}")

    stamp = _timestamp()
    if dry_run:
        dirty_status = _git_read_for_dry_run(agent_dir, ["status", "--porcelain"])
        if dirty_status:
            _print_cmd(
                [
                    "git",
                    "-C",
                    str(agent_dir),
                    "stash",
                    "push",
                    "-u",
                    "-m",
                    f"hermes-infoflow deploy {stamp}",
                ]
            )
    else:
        dirty_status = _git_output(agent_dir, ["status", "--porcelain"])
        if dirty_status:
            stash_message = f"hermes-infoflow deploy {stamp}"
            _run_git(
                agent_dir,
                ["stash", "push", "-u", "-m", stash_message],
                dry_run=False,
            )
            stash_name = _git_output(
                agent_dir,
                ["stash", "list", "-n", "1", "--format=%gd"],
                check=False,
            )
            print(
                "  saved dirty hermes-agent worktree to stash: "
                f"{stash_name or '<unknown stash>'} ({stash_message})"
            )

    if dry_run:
        prior_head = (
            _git_read_for_dry_run(
                agent_dir,
                ["rev-parse", "--short", "HEAD"],
                check=False,
            )
            or "unknown"
        )
        existing_url = _git_read_for_dry_run(
            agent_dir,
            ["remote", "get-url", fork_remote],
            check=False,
        )
    else:
        prior = _run_git(
            agent_dir,
            ["rev-parse", "--short", "HEAD"],
            dry_run=False,
            check=False,
            capture_output=True,
        )
        prior_head = (prior.stdout or "").strip() or "unknown"
        existing = _run_git(
            agent_dir,
            ["remote", "get-url", fork_remote],
            dry_run=False,
            check=False,
            capture_output=True,
        )
        existing_url = (existing.stdout or "").strip()
    if not existing_url:
        _run_git(
            agent_dir,
            ["remote", "add", fork_remote, fork_url],
            dry_run=dry_run,
        )
    elif not _same_remote_target(existing_url, fork_url):
        print(f"  - remote {fork_remote} was {existing_url}; updating to {fork_url}")
        _run_git(
            agent_dir,
            ["remote", "set-url", fork_remote, fork_url],
            dry_run=dry_run,
        )

    remote_ref = f"refs/remotes/{fork_remote}/{fork_branch}"
    remote_branch = f"{fork_remote}/{fork_branch}"
    fetch_refspec = f"+refs/heads/{fork_branch}:{remote_ref}"
    _run_git(
        agent_dir,
        ["fetch", fork_remote, fetch_refspec],
        dry_run=dry_run,
    )

    if dry_run:
        _print_cmd(["git", "-C", str(agent_dir), "rev-parse", remote_ref])
        _print_cmd(["git", "-C", str(agent_dir), "switch", "--detach", remote_ref])
        _print_cmd(["git", "-C", str(agent_dir), "branch", "-f", fork_branch, remote_ref])
        _print_cmd(
            [
                "git",
                "-C",
                str(agent_dir),
                "branch",
                "--set-upstream-to",
                remote_branch,
                fork_branch,
            ]
        )
        _print_cmd(["git", "-C", str(agent_dir), "switch", fork_branch])
    else:
        current_head = _git_output(agent_dir, ["rev-parse", "HEAD"])
        remote_head = _git_output(agent_dir, ["rev-parse", remote_ref])
        current_branch = _git_output(
            agent_dir,
            ["branch", "--show-current"],
            check=False,
        )
        if current_head != remote_head or current_branch != fork_branch:
            backup_branch = f"hermes-infoflow/backup/{stamp}"
            for suffix in ["", "-1", "-2", "-3", "-4", "-5"]:
                candidate = f"{backup_branch}{suffix}"
                created = _run_git(
                    agent_dir,
                    ["branch", candidate, "HEAD"],
                    dry_run=False,
                    check=False,
                    capture_output=True,
                )
                if created.returncode == 0:
                    print(f"  saved previous hermes-agent HEAD to branch: {candidate}")
                    break
            else:
                raise SystemExit(
                    "failed to create hermes-agent backup branch before switching"
                )

        if current_branch == fork_branch:
            fast_forward = _run_git(
                agent_dir,
                ["merge", "--ff-only", remote_ref],
                dry_run=False,
                check=False,
                capture_output=True,
            )
            if fast_forward.returncode != 0:
                print(
                    "  - local branch cannot fast-forward cleanly; "
                    "using saved backup branch and aligning to remote"
                )

        final_head = _git_output(agent_dir, ["rev-parse", "HEAD"])
        final_branch = _git_output(
            agent_dir,
            ["branch", "--show-current"],
            check=False,
        )
        if final_head != remote_head or final_branch != fork_branch:
            print("  - aligning local branch to fetched remote ref")
            _run_git(agent_dir, ["switch", "--detach", remote_ref], dry_run=False)
            _run_git(
                agent_dir,
                ["branch", "-f", fork_branch, remote_ref],
                dry_run=False,
            )
            _run_git(
                agent_dir,
                ["branch", "--set-upstream-to", remote_branch, fork_branch],
                dry_run=False,
                check=False,
            )
            _run_git(agent_dir, ["switch", fork_branch], dry_run=False)

    if not dry_run:
        final_head = _git_output(agent_dir, ["rev-parse", "HEAD"])
        remote_head = _git_output(agent_dir, ["rev-parse", remote_ref])
        final_branch = _git_output(
            agent_dir,
            ["branch", "--show-current"],
            check=False,
        )
        if final_head != remote_head or final_branch != fork_branch:
            raise SystemExit(
                "hermes-agent did not align to the required fork branch: "
                f"branch={final_branch or '<detached>'} HEAD={final_head} "
                f"{remote_ref}={remote_head}"
            )
        print(f"  hermes-agent HEAD is now {final_head[:12]} ({remote_branch})")

    print(f"  prior HEAD was {prior_head} (recoverable via 'git -C {agent_dir} reflog')")


def _dist_scripts_dir() -> Path | None:
    try:
        dist = metadata.distribution(DIST_NAME)
    except metadata.PackageNotFoundError:
        return None

    files = dist.files or ()
    for file in files:
        parts = tuple(file.parts)
        if len(parts) >= 4 and parts[:4] == (
            "hermes_infoflow",
            "_deploy_scripts",
            "lib",
            "deploy-common.sh",
        ):
            scripts_dir = Path(dist.locate_file("hermes_infoflow/_deploy_scripts"))
            if scripts_dir.is_dir():
                return scripts_dir
    return None


def _candidate_scripts_dir(source_root: Path, package_dir: Path | None = None) -> Path | None:
    scripts_dir = source_root / "scripts"
    if scripts_dir.is_dir():
        return scripts_dir
    if package_dir is not None:
        package_scripts = package_dir / "_deploy_scripts"
        if (package_scripts / "lib" / "deploy-common.sh").is_file():
            return package_scripts
    return _dist_scripts_dir()


def _resolve_source(source: Path) -> SourceLayout:
    source = source.expanduser().resolve()

    if (source / "hermes_infoflow" / "__init__.py").is_file():
        package_dir = source / "hermes_infoflow"
        manifest_file = source / "plugin.yaml"
        if not manifest_file.is_file():
            manifest_file = package_dir / "plugin.yaml"
        scripts_dir = _candidate_scripts_dir(source, package_dir)
    elif source.name == "hermes_infoflow" and (source / "__init__.py").is_file():
        package_dir = source
        source_root = source.parent
        manifest_file = source_root / "plugin.yaml"
        if not manifest_file.is_file():
            manifest_file = package_dir / "plugin.yaml"
        scripts_dir = _candidate_scripts_dir(source_root, package_dir)
        source = source_root
    elif (source / "__init__.py").is_file() and (
        (source / "adapter.py").is_file() or (source / "deploy.py").is_file()
    ):
        package_dir = source
        manifest_file = source / "plugin.yaml"
        scripts_dir = _candidate_scripts_dir(source, package_dir)
    else:
        raise SystemExit(
            "Cannot locate hermes-infoflow source layout. Expected a repo root "
            "with hermes_infoflow/, a flattened plugin dir, or the "
            "hermes_infoflow package directory."
        )

    if not package_dir.is_dir():
        raise SystemExit(f"Cannot find package directory: {package_dir}")
    if not manifest_file.is_file():
        raise SystemExit(f"Cannot find plugin manifest: {manifest_file}")
    if scripts_dir is None or not (scripts_dir / "lib" / "deploy-common.sh").is_file():
        raise SystemExit(
            "Cannot find scripts/lib/deploy-common.sh. Reinstall from a source "
            "checkout, sdist, or wheel that includes deployment scripts."
        )

    return SourceLayout(
        source_root=source,
        package_dir=package_dir,
        manifest_file=manifest_file,
        scripts_dir=scripts_dir,
    )


def _default_source() -> Path:
    package_dir = Path(__file__).resolve().parent
    repo_root = package_dir.parent
    if (repo_root / "plugin.yaml").is_file() and (repo_root / "scripts").is_dir():
        return repo_root
    return package_dir


def _skip_child(path: Path) -> bool:
    if path.is_dir() and path.name in _SKIP_DIR_NAMES:
        return True
    return path.is_file() and any(
        fnmatch.fnmatch(path.name, pat) for pat in _SKIP_FILE_PATTERNS
    )


def _copy_filtered(src: Path, dst: Path) -> None:
    for child in sorted(src.iterdir(), key=lambda p: p.name):
        if _skip_child(child):
            continue
        target = dst / child.name
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_filtered(child, target)
        elif child.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def _copy_docs(layout: SourceLayout, staging: Path) -> None:
    source_docs = layout.source_root / "docs"
    package_docs = layout.package_dir / "docs"
    docs_dir = source_docs if source_docs.is_dir() else package_docs
    if not docs_dir.is_dir():
        return
    docs_target = staging / "docs"
    docs_target.mkdir(parents=True, exist_ok=True)
    _copy_filtered(docs_dir, docs_target)


def _build_staging(layout: SourceLayout) -> tuple[Path, Path]:
    tmp_root = Path(tempfile.mkdtemp(prefix="hermes-infoflow-normalize-"))
    try:
        staging = tmp_root / CANONICAL_PLUGIN_ID
        staging.mkdir(parents=True)

        _copy_filtered(layout.package_dir, staging)
        shutil.copy2(layout.manifest_file, staging / "plugin.yaml")

        scripts_target = staging / "scripts"
        scripts_target.mkdir(parents=True, exist_ok=True)
        _copy_filtered(layout.scripts_dir, scripts_target)
        _copy_docs(layout, staging)

        if not (staging / "__init__.py").is_file():
            raise SystemExit(f"staged plugin has no __init__.py: {staging}")
        if not (staging / "plugin.yaml").is_file():
            raise SystemExit(f"staged plugin has no plugin.yaml: {staging}")
        required_scripts = (
            "deploy-common.sh",
            "edit_hermes_config.py",
            "edit_hermes_env.py",
        )
        for script_name in required_scripts:
            if not (staging / "scripts" / "lib" / script_name).is_file():
                raise SystemExit(f"staged plugin has no {script_name}: {staging}")
        return tmp_root, staging
    except BaseException:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise


def _replace_plugin_dir(
    staging: Path,
    plugin_dir: Path,
    *,
    dry_run: bool,
) -> Path | None:
    if dry_run:
        print(f"$ replace {plugin_dir} with staged canonical layout from {staging}")
        return None

    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    if not plugin_dir.exists():
        shutil.move(str(staging), str(plugin_dir))
        return None

    backup = plugin_dir.with_name(f".{plugin_dir.name}.normalize-backup-{os.getpid()}")
    if backup.exists():
        shutil.rmtree(backup)

    plugin_dir.rename(backup)
    try:
        shutil.move(str(staging), str(plugin_dir))
    except Exception:
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir)
        backup.rename(plugin_dir)
        raise
    return backup


def _restore_plugin_dir(plugin_dir: Path, backup: Path | None) -> None:
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    if backup is not None and backup.exists():
        backup.rename(plugin_dir)


def _run_deploy_common(
    plugin_dir: Path,
    config_file: Path,
    *,
    hermes_home: Path,
    port: str | None,
    dry_run: bool,
    phase: str,
    script_plugin_dir: Path | None = None,
) -> None:
    script_plugin_dir = script_plugin_dir or plugin_dir
    common_script = script_plugin_dir / "scripts" / "lib" / "deploy-common.sh"
    if not common_script.is_file():
        common_script = script_plugin_dir / "lib" / "deploy-common.sh"
    if not common_script.is_file():
        raise SystemExit(f"Cannot find deploy-common.sh after normalize: {common_script}")

    cmd = [
        "bash",
        str(common_script),
        "--plugin-dir",
        str(plugin_dir),
        "--plugin-id",
        CANONICAL_PLUGIN_ID,
        "--config-file",
        str(config_file),
        "--phase",
        phase,
    ]
    if port is not None:
        cmd.extend(["--port", port])
    if dry_run:
        cmd.append("--dry-run")

    _print_cmd(cmd)
    if dry_run:
        return

    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def normalize(
    *,
    source: Path | None = None,
    hermes_home: Path | None = None,
    config_file: Path | None = None,
    port: str | None = None,
    dry_run: bool = False,
) -> Path:
    hermes_home = hermes_home or Path(
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    )
    hermes_home = hermes_home.expanduser()
    config_file = config_file or Path(
        os.environ.get("HERMES_CONFIG_FILE", str(hermes_home / "config.yaml"))
    )
    config_file = config_file.expanduser()
    plugin_dir = hermes_home / "plugins" / CANONICAL_PLUGIN_ID

    layout = _resolve_source(source or _default_source())
    print(f"==> Normalizing hermes-infoflow from {layout.source_root}")
    print(f"==> Target plugin directory: {plugin_dir}")
    tmp_root, staging = _build_staging(layout)
    _sync_hermes_agent_fork(hermes_home=hermes_home, dry_run=dry_run)
    _run_deploy_common(
        plugin_dir,
        config_file,
        hermes_home=hermes_home,
        port=port,
        dry_run=dry_run,
        phase="preflight",
        script_plugin_dir=layout.scripts_dir,
    )

    backup: Path | None = None
    try:
        backup = _replace_plugin_dir(staging, plugin_dir, dry_run=dry_run)
        try:
            _run_deploy_common(
                plugin_dir,
                config_file,
                hermes_home=hermes_home,
                port=port,
                dry_run=dry_run,
                phase="apply",
                script_plugin_dir=staging if dry_run else plugin_dir,
            )
        except BaseException:
            if not dry_run:
                _restore_plugin_dir(plugin_dir, backup)
            raise
        else:
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
    finally:
        # If staging was moved successfully, tmp_root is already empty.
        shutil.rmtree(tmp_root, ignore_errors=True)
    return plugin_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-infoflow-deploy",
        description=(
            "Deploy or normalize hermes-infoflow into "
            "~/.hermes/plugins/infoflow with the canonical directory layout."
        ),
    )
    parser.add_argument(
        "--source",
        help=(
            "Source checkout/package/flattened plugin dir. Defaults to the "
            "current hermes_infoflow package or its source checkout."
        ),
    )
    parser.add_argument(
        "--hermes-home",
        help="Hermes home directory (default: $HERMES_HOME or ~/.hermes).",
    )
    parser.add_argument(
        "--config-file",
        help="Hermes config file (default: $HERMES_CONFIG_FILE or <home>/config.yaml).",
    )
    parser.add_argument(
        "--port",
        type=validate_port,
        help=(
            "Webhook listen port (1-65535). Written to ~/.hermes/.env as "
            "INFOFLOW_PORT; without it an existing value is kept or "
            f"{DEFAULT_INFOFLOW_PORT} is seeded."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the operations without changing files.",
    )
    args = parser.parse_args(argv)

    normalize(
        source=Path(args.source).expanduser() if args.source else None,
        hermes_home=Path(args.hermes_home).expanduser() if args.hermes_home else None,
        config_file=Path(args.config_file).expanduser() if args.config_file else None,
        port=args.port,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
