"""Upsert keys in ``~/.hermes/.env`` (bundled for ``hermes-infoflow-tools``).

Logic mirrors ``scripts/lib/edit_hermes_env.py`` in the main repo.
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_INFOFLOW_PORT = 26521
_PORT_KEY = "INFOFLOW_PORT"
_PORT_RE = re.compile(r"^\d{1,5}$")


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
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(raw_line)
        if parsed and parsed[0] == key:
            return parsed[1]
    return None


def validate_port_value(value: str) -> None:
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
    if key == _PORT_KEY:
        validate_port_value(value)
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
    if read_key(path, key) is not None:
        return False
    return upsert_key(path, key, default_value)
