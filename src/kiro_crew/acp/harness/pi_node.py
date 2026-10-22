"""Which Node.js the ``pi`` agent will run under, and whether it is new enough.

pi ships as an npm bin whose first line is ``#!/usr/bin/env node``, so the Node
that runs it is the first ``node`` on the PATH its process inherits. Crew's PATH
augmentation puts directories such as ``/usr/local/bin`` and ``/opt/homebrew/bin``
ahead of the inherited PATH, so an older system Node there runs every pi session:
pi dies at module load and the gate read-back can report only an exit code, which
names no cause. This module answers before any child starts. It resolves the
interpreter pi's shebang names on the child's own PATH, reads that Node's version,
and compares it to the floor pi's ``package.json`` declares in ``engines.node``.

Below the floor it looks for a Node that meets it -- beside the ``pi`` link (the
install that put pi there), in the npm prefix pi's package lives under, then later
on the same PATH -- so the gate launcher can run pi under that Node by path. Only
when none qualifies is the session refused, with the Node found, its version and
the floor in the message.

Left exactly as it was, so the gate read-back still decides: Windows (the
``pi.cmd`` shim picks its own Node), a ``pi`` that is not a Node script, a shebang
this module does not parse, and a Node whose version cannot be read.

Blocking throughout (file reads and short ``node --version`` children); callers
run it off the loop.
"""

from __future__ import annotations

import os
import re
import subprocess as subprocess_mod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.constants import format_node_version, parse_node_version

__all__ = [
    "PiNodeChoice",
    "ProbeWrap",
    "choose_pi_node",
    "node_engine_floor",
]

# Wraps a probe argv for the life of its child: yields the argv to run and cleans
# up whatever the wrapping wrote once the child has exited.
ProbeWrap = Callable[[list[str]], AbstractContextManager[list[str]]]

_NODE = "node"

# A shebang line is short; this bounds the read of a file that may not be a script.
_SHEBANG_READ_BYTES = 256

# ``node --version`` prints and exits; the bound is a backstop for a hung shim.
_NODE_VERSION_TIMEOUT_S = 10.0

# One alternative of an npm range, read for its LOWER bound: ``>=22.19.0``,
# ``^22.19``, ``~22``, ``22.x``. Anything else (``>22``, ``<24``, ``*``) is not
# read as a floor, and the whole range falls back rather than being half-read.
_RANGE_LOWER_RE = re.compile(r"(?:>=|\^|~|=)?\s*v?(\d+)(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?")

# What may follow that lower bound: nothing, an upper bound, or a hyphen range's
# upper half. None of these move the floor.
_RANGE_UPPER_RE = re.compile(r"(?:\s+<=?\s*v?[\d.xX*]+|\s+-\s+v?[\d.xX*]+)?\s*")


@dataclass(frozen=True)
class PiNodeChoice:
    """How to launch pi: ``argv`` is what the launcher execs in place of ``pi``.

    Empty ``argv`` means exec ``pi`` itself and let its shebang choose; a non-empty
    one is ``(node, pi's script)``. A non-empty ``issue`` refuses the session.
    """

    argv: tuple[str, ...] = ()
    issue: str = ""


def node_engine_floor(spec: object) -> tuple[int, int, int] | None:
    """The lowest Node an ``engines.node`` range admits, or ``None`` if unreadable.

    The minimum across ``||`` alternatives; a missing minor or patch, or a
    ``x``/``*`` in it, reads as 0.
    """
    if not isinstance(spec, str) or not spec.strip():
        return None
    floors: list[tuple[int, int, int]] = []
    for alternative in spec.split("||"):
        alternative = alternative.strip()
        match = _RANGE_LOWER_RE.match(alternative)
        if not match or not _RANGE_UPPER_RE.fullmatch(alternative[match.end() :]):
            return None
        major, minor, patch = (int(p) if p and p.isdigit() else 0 for p in match.groups())
        floors.append((major, minor, patch))
    return min(floors)


def _shebang_node(script: str) -> tuple[str, bool] | None:
    """``(interpreter, looked_up)`` when *script* starts with a Node shebang.

    ``("node", True)`` for ``#!/usr/bin/env node`` (the PATH decides), ``(path,
    False)`` for an absolute ``#!/.../node``. ``None`` for anything else,
    including a shebang with interpreter arguments.
    """
    try:
        with open(script, "rb") as fh:
            head = fh.read(_SHEBANG_READ_BYTES)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    parts = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").split()
    if len(parts) == 2 and os.path.basename(parts[0]) == "env" and parts[1] == _NODE:
        return _NODE, True
    if len(parts) == 1 and os.path.isabs(parts[0]) and os.path.basename(parts[0]) == _NODE:
        return parts[0], False
    return None


def _nodes_on_path(search_path: str) -> list[str]:
    """Every executable ``node`` on *search_path*, in the order ``env`` tries them."""
    found: list[str] = []
    for entry in search_path.split(os.pathsep):
        candidate = os.path.join(entry, _NODE) if entry else ""
        if candidate and platform_compat.is_executable_file(candidate):
            found.append(candidate)
    return found


def _nodes_beside_pi(pi_bin: str) -> Iterator[str]:
    """The Node that installed *pi_bin*: beside its link, then in its npm prefix."""
    yield os.path.join(os.path.dirname(os.path.abspath(pi_bin)), _NODE)
    for parent in Path(os.path.realpath(pi_bin)).parents:
        if parent.name == "node_modules" and parent.parent.name == "lib":
            yield str(parent.parent.parent / "bin" / _NODE)
            return


def _node_version(
    node: str, search_path: str, cwd: str, wrap: ProbeWrap = nullcontext
) -> tuple[int, int, int] | None:
    """What ``node --version`` reports for *node*, or ``None`` when it cannot say.

    Run by the path given, not its realpath: a version-manager shim answers for
    the Node it selects, and does so by ``cwd``, so the session's directory is
    used. That directory is the agent's, and a shim reads its config there, so the
    caller passes *wrap*: the session's own sandbox and credential mask, applied to
    the argv for the life of the child (see ``acp/harness/pi.py``). The environment
    is only ``PATH`` and ``HOME``: nothing of the gateway's own reaches the child,
    and a ``NODE_OPTIONS`` cannot break the probe.
    """
    env = {"PATH": search_path}
    if os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    try:
        with wrap([node, "--version"]) as argv:
            completed = subprocess_mod.run(
                argv,
                cwd=cwd or None,
                env=env,
                stdin=subprocess_mod.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_NODE_VERSION_TIMEOUT_S,
            )
    except (OSError, ValueError, subprocess_mod.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_node_version(completed.stdout)


def _too_old_issue(
    found: str, version: tuple[int, int, int] | None, floor: tuple[int, int, int]
) -> str:
    floor_text = format_node_version(floor)
    if found and version is not None:
        what = f"the Node.js it would run under is {found}, v{format_node_version(version)}"
        where = f"put it ahead of {os.path.dirname(found)} on PATH"
    else:
        what = "no 'node' is on the session's PATH"
        where = "put it on PATH"
    return (
        f"pi needs Node.js {floor_text} or newer (the 'engines' floor in its package.json), but {what}, and no Node.js "
        f"that new was found beside pi or later on that PATH. Install Node.js "
        f"{floor_text} or newer and {where}, then start a new chat."
    )


def choose_pi_node(
    pi_bin: str,
    floor: tuple[int, int, int],
    search_path: str,
    *,
    cwd: str = "",
    wrap: ProbeWrap = nullcontext,
) -> PiNodeChoice:
    """How to run *pi_bin* so the Node under it meets *floor*.

    *search_path* is the PATH the pi child inherits, which is what its shebang's
    ``env`` searches; *floor* is the one pi's manifest declares. *wrap* confines each ``node --version`` child
    (:func:`_node_version`).
    """
    if platform_compat.IS_WINDOWS:
        return PiNodeChoice()
    script = os.path.realpath(pi_bin)
    interpreter = _shebang_node(script)
    if interpreter is None:
        return PiNodeChoice()
    name, looked_up = interpreter
    path_nodes = _nodes_on_path(search_path)
    if looked_up:
        current, later = (path_nodes[0], path_nodes[1:]) if path_nodes else ("", [])
    else:
        current, later = name, path_nodes
    version = _node_version(current, search_path, cwd, wrap) if current else None
    if current and version is None:
        return PiNodeChoice()
    if version is not None and version >= floor:
        return PiNodeChoice()
    tried = {os.path.realpath(current)} if current else set()
    for candidate in [*_nodes_beside_pi(pi_bin), *later]:
        real = os.path.realpath(candidate)
        if real in tried or not platform_compat.is_executable_file(candidate):
            continue
        tried.add(real)
        candidate_version = _node_version(candidate, search_path, cwd, wrap)
        if candidate_version is not None and candidate_version >= floor:
            return PiNodeChoice(argv=(candidate, script))
    return PiNodeChoice(issue=_too_old_issue(current, version, floor))
