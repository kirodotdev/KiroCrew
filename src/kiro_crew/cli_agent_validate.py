"""``kirocrew agent validate``: check agent specs offline, one line per problem.

``kirocrew agent validate [path]`` reads one spec file, or every spec in a
directory (the user-level agents directory when no path is given), and reports
what :func:`kiro_crew.agent_spec_schema.check_spec` finds: unknown fields, a
field of the wrong shape, a malformed ``tools`` / ``allowedTools`` entry and an
``@server`` reference that nothing the spec can reach defines. It exits 1 when any
problem is found, so a script or an editor task can gate on it. Nothing is
written and no session, gateway or kiro-cli is needed.

``--print-schema`` prints the JSON Schema built from the same field table, to
save beside a spec and name in its ``$schema`` field.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kiro_crew import agent_spec_schema
from kiro_crew.terminal_safe import safe_terminal_line

#: Largest spec file read. A spec is a few kilobytes; a bigger file is refused whole
#: so a stray large file in an agents directory cannot hold the command.
_MAX_SPEC_BYTES = 8 * 1024 * 1024


def _load_spec(path: Path) -> tuple[Any, str]:
    """``(parsed spec, "")`` for *path*, or ``(None, why it cannot be read)``."""
    from kiro_crew.agent_spec_format import parse_agent_spec_bytes

    try:
        if not path.is_file():
            return None, "is not a regular file"
        if path.stat().st_size > _MAX_SPEC_BYTES:
            return None, f"is larger than {_MAX_SPEC_BYTES // (1024 * 1024)} MiB"
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"cannot be read ({exc.strerror or type(exc).__name__})"
    try:
        return parse_agent_spec_bytes(raw, path), ""
    except UnicodeDecodeError:
        return None, "is not UTF-8 text"
    except ValueError as exc:
        return None, f"cannot be parsed: {exc}"


def _spec_files(target: Path) -> list[Path]:
    """The files to check: *target* itself, or the live specs of a directory."""
    if not target.is_dir():
        return [target]
    from kiro_crew.agent_spec_format import iter_agent_spec_files_strict

    return iter_agent_spec_files_strict(target)


def _problems_for(path: Path, *, allow_unknown: bool) -> list[tuple[str, str]]:
    spec, unreadable = _load_spec(path)
    if unreadable:
        return [("", unreadable)]
    from kiro_crew.agent_sdk.drivers.acp import configured_server_names

    declared = spec.get("name") if isinstance(spec, dict) else None
    agent = declared if isinstance(declared, str) and declared else path.stem
    servers = configured_server_names(agent, spec) if isinstance(spec, dict) else None
    return agent_spec_schema.check_spec(
        spec, configured_servers=servers, allow_unknown=allow_unknown
    )


def agent_validate(args: argparse.Namespace) -> None:
    """Run ``kirocrew agent validate``; exits 1 on a problem or an unreadable path."""
    if getattr(args, "print_schema", False):
        print(json.dumps(agent_spec_schema.json_schema(), indent=2))
        return
    if getattr(args, "path", None):
        target = Path(args.path).expanduser()
    else:
        from kiro_crew.agent import kiro_agents_dir_path

        target = kiro_agents_dir_path()
    if not target.exists():
        print(f"Error: {safe_terminal_line(str(target))} does not exist", file=sys.stderr)
        sys.exit(1)
    try:
        files = _spec_files(target)
    except OSError as exc:
        print(
            f"Error: cannot list {safe_terminal_line(str(target))}: {exc.strerror}", file=sys.stderr
        )
        sys.exit(1)
    allow_unknown = bool(getattr(args, "allow_unknown_fields", False))
    total = 0
    for path in files:
        label = safe_terminal_line(str(path))
        for field, message in _problems_for(path, allow_unknown=allow_unknown):
            where = f"{label}: {safe_terminal_line(field)}: " if field else f"{label}: "
            print(f"{where}{safe_terminal_line(message)}", file=sys.stderr)
            total += 1
    print(f"Checked {len(files)} agent spec(s): {total} problem(s).")
    if total:
        sys.exit(1)
