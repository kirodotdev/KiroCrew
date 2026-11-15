"""``kirocrew agent validate``: check agent specs offline, one line per problem.

``kirocrew agent validate [path]`` reads one spec file, or every spec in a
directory (the user-level agents directory when no path is given), and reports
what :func:`kiro_crew.agent_spec_schema.check_spec` finds: unknown fields, a
field of the wrong shape and a malformed ``tools`` / ``allowedTools`` entry.
Whether an ``@server`` ref resolves is ``kirocrew doctor``'s check, which judges
it per backend. It exits 1 when any problem is found, so a script or an editor
task can gate on it. Nothing is written and no session, gateway or kiro-cli is
needed.

``--print-schema`` prints the JSON Schema built from the same field table, to
save beside a spec and name in its ``$schema`` field.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from kiro_crew import agent_spec_schema
from kiro_crew.hooks import is_unc_shape, unc_probe_allowed
from kiro_crew.terminal_safe import safe_terminal_line

_WINDOWS = os.name == "nt"


def _untrusted_share(target: Path) -> bool:
    """Whether *target* is a Windows UNC path outside the trusted roots.

    On Windows, probing such a path opens an SMB connection to the host it names,
    so it is refused before any probe, as the agent spec readers refuse it.
    """
    raw = str(target)
    return _WINDOWS and is_unc_shape(raw) and not unc_probe_allowed(raw)


def _without_path(message: str, path: Path) -> str:
    """*message* with the leading path the reader puts in it removed."""
    for prefix in (f"{path}: ", f"{path} "):
        if message.startswith(prefix):
            return message[len(prefix) :]
    return message


def _load_spec(path: Path) -> tuple[Any, str]:
    """``(parsed spec, "")`` for *path*, or ``(None, why it cannot be read)``.

    Read through :func:`kiro_crew.agent_discovery.read_agent_spec_strict`, the agent
    readers' own gate: an untrusted network share, a sensitive target, an oversized
    file and an AppleDouble sidecar are refused there, before the file is opened.
    """
    from kiro_crew.agent_discovery import SensitiveAgentSpecPathError, read_agent_spec_strict

    try:
        return read_agent_spec_strict(path, operation="agent_validate", source="cli"), ""
    except OSError as exc:
        return None, f"cannot be read ({exc.strerror or type(exc).__name__})"
    except RecursionError:
        return None, "cannot be parsed: it is nested too deeply"
    except UnicodeDecodeError:
        return None, "is not UTF-8 text"
    except SensitiveAgentSpecPathError as exc:
        return None, f"is refused: {_without_path(str(exc), path)}"
    except ValueError as exc:
        # A sidecar or an oversized file says what it is; anything else did not parse.
        text = _without_path(str(exc), path)
        return None, text if text.startswith("is ") else f"cannot be parsed: {text}"


def _spec_files(target: Path) -> list[Path]:
    """The files to check: *target* itself, or the specs of a directory.

    In a directory, a macOS AppleDouble sidecar and a markdown file with no opening
    frontmatter fence (a README, a shared prompt fragment) are not specs, so they are
    skipped, as the agent readers skip them. A file named on the command line is
    always checked.
    """
    if not target.is_dir():
        return [target]
    from kiro_crew.agent_discovery import apple_double_sidecar, plain_markdown_document
    from kiro_crew.agent_spec_format import is_markdown_spec, iter_agent_spec_files_strict

    return [
        path
        for path in iter_agent_spec_files_strict(target)
        if not apple_double_sidecar(path)
        and not (is_markdown_spec(path) and plain_markdown_document(path))
    ]


def _problems_for(path: Path, *, allow_unknown: bool) -> list[tuple[str, str]]:
    spec, unreadable = _load_spec(path)
    if unreadable:
        return [("", unreadable)]
    return agent_spec_schema.check_spec(spec, allow_unknown=allow_unknown)


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
    if _untrusted_share(target):
        print(
            f"Error: {safe_terminal_line(str(target))} names a network share outside the "
            "trusted roots; copy the spec to a local folder to check it",
            file=sys.stderr,
        )
        sys.exit(1)
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
