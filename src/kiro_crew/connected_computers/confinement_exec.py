"""Local machine (macOS): start one confined child with its own environment.

``sandbox-exec`` runs this file inside the fence (``python -I -S``), so
``sandbox-exec`` and this interpreter see only the bare base environment. The
child's whole environment arrives on an inherited descriptor, as NUL-separated
name and value pairs, and this replaces itself with the child. A loader
variable such as ``DYLD_INSERT_LIBRARIES`` pointing into the shared root
therefore never reaches a process that runs before the fence exists, and no
value sits in an argv another local user can read.

Usage: ``confinement_exec.py FD -- PROGRAM [ARG ...]``. Standalone: importing
it performs no I/O, and it uses the standard library only.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional

#: The most environment this reads: a larger one is refused, never cut.
MAX_ENV_BYTES = 1 << 20
_CHUNK = 65536


class EnvRefused(ValueError):
    """The descriptor did not hold a well-formed environment."""


def read_env(fd: int) -> dict[str, str]:
    """The environment on ``fd``, which is closed whatever happens."""
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            data = os.read(fd, _CHUNK)
            if not data:
                break
            total += len(data)
            if total > MAX_ENV_BYTES:
                raise EnvRefused("the environment is too large")
            chunks.append(data)
    finally:
        os.close(fd)
    parts = b"".join(chunks).split(b"\0")
    if parts[-1] != b"":
        raise EnvRefused("the environment does not end with a separator")
    parts = parts[:-1]
    if len(parts) % 2:
        raise EnvRefused("a name has no value")
    env: dict[str, str] = {}
    for at in range(0, len(parts), 2):
        name, value = os.fsdecode(parts[at]), os.fsdecode(parts[at + 1])
        if not name or "=" in name:
            raise EnvRefused("a name cannot be empty or hold '='")
        env[name] = value
    return env


def main(
    argv: list[str], *, execvpe: Optional[Callable[[str, list[str], dict[str, str]], None]] = None
) -> int:
    """Read the environment and become the child; return only on a failure."""
    run = execvpe or os.execvpe
    if len(argv) < 4 or argv[2] != "--":
        sys.stderr.write("usage: confinement_exec.py FD -- PROGRAM [ARG ...]\n")
        return 2
    try:
        env = read_env(int(argv[1]))
    except (ValueError, OSError):
        sys.stderr.write("the tool's settings could not be read\n")
        return 2
    program = argv[3:]
    try:
        # Searches the child's own PATH, not this process's.
        run(program[0], program, env)
    except OSError as exc:
        sys.stderr.write(f"could not start {program[0]}: {exc.strerror or 'error'}\n")
        return 127
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
