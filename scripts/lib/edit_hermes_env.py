#!/usr/bin/env python3
"""Upsert keys in ``~/.hermes/.env`` without disturbing other entries.

Used by ``deploy-common.sh`` to seed or override ``INFOFLOW_PORT``. Parsing
matches ``scripts/sim/_env.py`` (``#`` comments, optional ``export`` prefix,
surrounding quotes stripped on read).

Usage::

    python3 edit_hermes_env.py --set INFOFLOW_PORT=9000
    python3 edit_hermes_env.py --ensure INFOFLOW_PORT=26521
    python3 edit_hermes_env.py --env-file ~/.hermes/.env --set FOO=bar --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

DEFAULT_ENV_FILE = Path.home() / ".hermes" / ".env"
DEFAULT_INFOFLOW_PORT = 26521
_PORT_KEY = "INFOFLOW_PORT"
_PORT_RE = re.compile(r"^\d{1,5}$")


def _hermes_env_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home).expanduser() / ".env"
    return DEFAULT_ENV_FILE


def _strip_export(line: str) -> str:
    if line.startswith("export "):
        return line[len("export ") :].lstrip()
    return line


def _unquote(value: str) -> str:
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def _parse_line(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    line = _strip_export(line)
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if not key:
        return None
    return key, _unquote(value.strip())


def read_key(path: Path, key: str) -> str | None:
    """Return the value for *key* in *path*, or ``None`` if absent."""
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(raw_line)
        if parsed and parsed[0] == key:
            return parsed[1]
    return None


def _validate_port(key: str, value: str) -> None:
    if key != _PORT_KEY:
        return
    if not _PORT_RE.match(value):
        raise SystemExit(
            f"invalid {_PORT_KEY}: {value!r} (expected integer 1-65535)"
        )
    port = int(value)
    if port < 1 or port > 65535:
        raise SystemExit(
            f"invalid {_PORT_KEY}: {value!r} (expected integer 1-65535)"
        )


def _format_assignment(key: str, value: str) -> str:
    return f"{key}={value}\n"


def upsert_key(path: Path, key: str, value: str) -> bool:
    """Set *key* to *value*, preserving comments and other lines.

    Returns True if the file content would change.
    """
    _validate_port(key, value)
    assignment = _format_assignment(key, value)

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(assignment, encoding="utf-8")
        return True

    old_text = path.read_text(encoding="utf-8")
    lines = old_text.splitlines(keepends=True)
    if not lines and not old_text.strip():
        path.write_text(assignment, encoding="utf-8")
        return True

    out: list[str] = []
    found = False
    for raw in lines:
        line_no_nl = raw.rstrip("\r\n")
        parsed = _parse_line(line_no_nl)
        if parsed and parsed[0] == key:
            found = True
            suffix = "\n" if raw.endswith("\n") else ""
            out.append(assignment.rstrip("\n") + suffix)
            continue
        out.append(raw)

    if not found:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(assignment)

    new_text = "".join(out)
    if new_text == old_text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def ensure_key(path: Path, key: str, default_value: str) -> bool:
    """Set *key* only when it is missing from *path*."""
    if read_key(path, key) is not None:
        return False
    return upsert_key(path, key, default_value)


def _parse_kv(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise SystemExit(f"expected KEY=VALUE, got {spec!r}")
    key, _, value = spec.partition("=")
    key = key.strip()
    if not key:
        raise SystemExit(f"expected KEY=VALUE, got {spec!r}")
    return key, value.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        help="Path to .env (default: $HERMES_HOME/.env or ~/.hermes/.env)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--set",
        metavar="KEY=VALUE",
        help="Always set (overwrite) the given key",
    )
    group.add_argument(
        "--ensure",
        metavar="KEY=DEFAULT",
        help="Set the key only if it is not already present",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the action without writing the file",
    )
    args = parser.parse_args(argv)

    env_path = _hermes_env_path(args.env_file)
    if args.set:
        key, value = _parse_kv(args.set)
        action = "set"
    else:
        key, value = _parse_kv(args.ensure)
        action = "ensure"

    _validate_port(key, value)

    if args.dry_run:
        existing = read_key(env_path, key)
        if action == "set":
            print(f"[edit_hermes_env] (dry-run) would set {key}={value} in {env_path}")
        elif existing is not None:
            print(
                f"[edit_hermes_env] (dry-run) would leave {key}={existing} in {env_path}"
            )
        else:
            print(
                f"[edit_hermes_env] (dry-run) would ensure {key}={value} in {env_path}"
            )
        return 0

    if action == "set":
        changed = upsert_key(env_path, key, value)
    else:
        changed = ensure_key(env_path, key, value)

    if changed:
        print(f"[edit_hermes_env] updated {env_path} ({key}={read_key(env_path, key)})")
    else:
        print(f"[edit_hermes_env] no change needed ({env_path}: {key} already set)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
