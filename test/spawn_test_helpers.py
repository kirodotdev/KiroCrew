"""Helpers for tests that assert on a spawn's argv.

Resource limits are applied AFTER ``exec`` by ``kiro_crew._spawn_exec_shim``, so
every spawn routed through ``sandbox.create_subprocess_limited`` carries an argv
prefix (``<python> -I -S -c <shim source> [options] --``) ahead of the real
command. Tests that care about the command itself use :func:`strip_spawn_shim`
rather than hard-coding the prefix, which varies with the resource profile.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

_SHIM_FLAGS = ("-I", "-S", "-c")
_ARGV_SEPARATOR = "--"


def strip_spawn_shim(args: Sequence[str]) -> tuple[str, ...]:
    """Return *args* without the post-exec shim prefix, if one is present.

    Returns *args* unchanged when no shim was prepended -- on Windows, for a
    policy-free profile, or when the spawn was not routed through the wrapper --
    so a test can use this unconditionally.

    The scan for the ``--`` terminator starts after the shim's source argument,
    so a ``--`` inside the command itself is never mistaken for the separator.
    """
    argv = tuple(args)
    if len(argv) < 6 or argv[0] != sys.executable or argv[1:4] != _SHIM_FLAGS:
        return argv
    index = 4  # argv[4] is the shim source string
    while index < len(argv) and argv[index] != _ARGV_SEPARATOR:
        index += 1
    return argv[index + 1 :]
