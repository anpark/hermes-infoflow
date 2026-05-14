#!/usr/bin/env python3
"""Synchronize the install-command version numbers inside README.md.

Mirrors openclaw-infoflow/scripts/sync-readme-install-version.mjs, but
adapted for PyPI's lack of dist-tags. We use **PEP 440 prerelease**
suffixes (e.g. ``0.1.0b1``) to mean "beta", and we ask the PyPI JSON API
which versions actually exist.

The README has three sync markers we maintain:

    <!-- sync:hermes-infoflow-version -->
        ... lines using THE CURRENT pyproject.toml version ...
    <!-- /sync:hermes-infoflow-version -->

    <!-- sync:hermes-infoflow-version:latest -->
        ... lines using the most recent stable PyPI version ...
    <!-- /sync:hermes-infoflow-version:latest -->

    <!-- sync:hermes-infoflow-version:beta -->
        ... lines using the most recent prerelease PyPI version ...
    <!-- /sync:hermes-infoflow-version:beta -->

This script edits in place. If a stream has no version (e.g. no stable
release on PyPI yet), the marker block content is left untouched.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PACKAGE = "hermes-infoflow"
DEFAULT_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
DEFAULT_README = Path(__file__).resolve().parent.parent / "README.md"

MARKER_TEMPLATE = (
    r"<!--\s*sync:{key}\s*-->(.*?)<!--\s*/sync:{key}\s*-->"
)

# Pretty version detection per stream:
#   - "current" pulls from pyproject.toml version field.
#   - "latest" picks the largest PEP 440 *final* (non-prerelease) version
#     present on PyPI.
#   - "beta" picks the largest *prerelease* version on PyPI.
VERSION_RE_IN_LINE = re.compile(r"(==|--version[= ]| )(\d+\.\d+\.\d+(?:[abc]\d+|rc\d+)?)")
HATCH_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def _read_current_version(pyproject: Path) -> str:
    m = HATCH_VERSION_RE.search(pyproject.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"version field not found in {pyproject}")
    return m.group(1)


def _parse_pep440(v: str) -> tuple:
    """Return a comparable tuple for a PEP 440 version string.

    We deliberately use the ``packaging`` library when available, falling
    back to a minimal in-house comparator so this script can run in CI
    runners without ``packaging`` installed.
    """
    try:
        from packaging.version import Version

        return Version(v).release + (Version(v).is_prerelease, Version(v).pre or ())
    except ImportError:
        # naive fallback: split on dot and treat suffix as prerelease
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)([abc]\d+|rc\d+)?$", v)
        if not m:
            return (0, 0, 0, True, ())
        major, minor, patch, pre = m.groups()
        return (int(major), int(minor), int(patch), bool(pre), (pre or "",))


def _is_prerelease(v: str) -> bool:
    try:
        from packaging.version import Version

        return Version(v).is_prerelease
    except ImportError:
        return bool(re.search(r"[abc]\d+$", v) or re.search(r"rc\d+$", v))


def _fetch_pypi_versions(package: str) -> list[str]:
    url = f"https://pypi.org/pypi/{package}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"[sync] failed to fetch {url}: {exc}", file=sys.stderr)
        return []
    return list(data.get("releases", {}).keys())


def _resolve_streams(package: str) -> dict[str, str | None]:
    versions = _fetch_pypi_versions(package)
    stable = [v for v in versions if not _is_prerelease(v)]
    prereleases = [v for v in versions if _is_prerelease(v)]
    stable.sort(key=_parse_pep440)
    prereleases.sort(key=_parse_pep440)
    return {
        "latest": stable[-1] if stable else None,
        "beta": prereleases[-1] if prereleases else None,
    }


def _rewrite_block(text: str, version: str) -> str:
    """Replace ``==X.Y.Z[bN]`` and ``--version X.Y.Z[bN]`` occurrences."""

    def _sub(match: re.Match) -> str:
        return f"{match.group(1)}{version}"

    return VERSION_RE_IN_LINE.sub(_sub, text)


def _patch_marker(content: str, key: str, version: str | None) -> tuple[str, bool]:
    if version is None:
        return content, False
    pattern = re.compile(MARKER_TEMPLATE.format(key=re.escape(key)), re.DOTALL)
    changed = [False]

    def _sub(match: re.Match) -> str:
        body = match.group(1)
        new_body = _rewrite_block(body, version)
        if new_body != body:
            changed[0] = True
        return f"<!-- sync:{key} -->{new_body}<!-- /sync:{key} -->"

    return pattern.sub(_sub, content, count=0), changed[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--readme", default=str(DEFAULT_README))
    parser.add_argument("--pyproject", default=str(DEFAULT_PYPROJECT))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing.",
    )
    args = parser.parse_args(argv)

    readme = Path(args.readme)
    pyproject = Path(args.pyproject)
    if not readme.exists():
        print(f"[sync] no README at {readme}", file=sys.stderr)
        return 1

    current = _read_current_version(pyproject)
    streams = _resolve_streams(args.package)
    streams["hermes-infoflow-version"] = current  # the "current" stream

    content = readme.read_text(encoding="utf-8")
    new_content = content
    touched = False
    for key in ("hermes-infoflow-version", "hermes-infoflow-version:latest", "hermes-infoflow-version:beta"):
        version_key = "hermes-infoflow-version" if key == "hermes-infoflow-version" else key.split(":")[-1]
        version = streams.get(version_key)
        new_content, changed = _patch_marker(new_content, key, version)
        if changed:
            print(f"[sync] updated marker {key} -> {version}")
            touched = True

    if not touched:
        print("[sync] no changes")
        return 0

    if args.dry_run:
        print("[sync] (dry-run) not writing changes")
        return 0

    readme.write_text(new_content, encoding="utf-8")
    print(f"[sync] wrote {readme}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
