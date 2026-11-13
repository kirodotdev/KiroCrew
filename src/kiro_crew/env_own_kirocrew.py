"""The crew-home directory that puts this install's ``kirocrew`` ahead of ``~/.local/bin``.

:func:`kiro_crew.env.augmented_path` lists :func:`own_kirocrew_dirs` first on
every agent PATH. The gateway refreshes the one entry inside it on every start
(``agent.refresh_own_kirocrew_link``) and the sandbox seals the directory
read-only (``sandbox_leaves``, ``sandbox_precreate``, ``security.paths_lists``).
The entry is a ``kirocrew`` symlink on POSIX and a ``kirocrew.cmd`` forwarder on
Windows (:func:`windows_forwarder`).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from kiro_crew import platform_compat
from kiro_crew.config.paths import peek_data_home

logger = logging.getLogger(__name__)

#: The crew-home directory holding ONE entry, which runs the install this
#: gateway runs.
OWN_KIROCREW_DIR = "kirocrew-bin"

#: That entry's name on Windows. PATH order outranks PATHEXT order across
#: directories, so this ``.cmd`` wins over a ``kirocrew.exe`` in any later entry.
WINDOWS_FORWARDER_NAME = "kirocrew.cmd"


def own_kirocrew_dirs(validate: Callable[[str], str | None]) -> list[str]:
    """The directory whose only entry is this install's ``kirocrew``, ahead of ``~/.local/bin``.

    An agent shelling out to ``kirocrew`` must reach its own gateway's CLI. A
    stale launcher in ``~/.local/bin`` would otherwise win, and an old CLI saves
    ``config.json`` with a schema lacking member identity, which bricks every
    crewmate. The interpreter dir cannot move forward instead: it also holds
    ``python``/``pip`` (see :func:`kiro_crew.env.augmented_path`).

    *validate* is ``env._validated_bin_dir``. Nothing is created here; a missing
    directory or a dangling link is skipped by every PATH lookup.
    """
    try:
        root = peek_data_home() / OWN_KIROCREW_DIR
    except Exception:
        logger.debug("could not resolve the own-kirocrew directory", exc_info=True)
        return []
    value = validate(str(root))
    # A link or junction at the NAME would put whatever directory it points at
    # ahead of ``~/.local/bin``; only the real, sealed directory is listed.
    if value is None or platform_compat.is_link_or_junction(value):
        return []
    return [value]


#: Characters a forwarder line cannot carry: a quote or line break ends the
#: quoted path (neither is a legal Windows path character), and ``!`` is
#: removed by cmd's delayed expansion when a caller turns it on (``cmd /V:ON``).
_UNQUOTABLE = frozenset('"\r\n\0!')


def windows_forwarder(target: str) -> bytes | None:
    """The ``kirocrew.cmd`` body that runs *target* with the caller's arguments.

    One line, ``@"<target>" %*``: ``%*`` passes every argument through as typed,
    and the batch file's exit code is the target's, including when the target is
    itself a ``.cmd`` (control passes to it and does not return). ``%`` in the
    path is doubled so cmd does not expand it as a variable; ``^``, ``&`` and the
    other metacharacters are inert inside the quotes.

    ASCII only: cmd decodes a batch file in whatever code page the calling
    console has at that moment, and an agent shell may have switched it (``chcp
    65001``), so a non-ASCII byte names a different path from one call to the
    next. Returns ``None`` for a target this cannot write so that every console
    reads it the same way; the caller then writes no forwarder.
    """
    if not target.isascii() or any(ch in _UNQUOTABLE for ch in target):
        return None
    return f'@"{target.replace("%", "%%")}" %*\r\n'.encode("ascii")


def forwarder_target(path: str) -> str | None:
    """The launcher a ``kirocrew.cmd`` written by :func:`windows_forwarder` runs, else ``None``.

    ``os.path.realpath`` follows the POSIX link to the install it names, but not a
    batch file, so a caller asking which install an agent's ``kirocrew`` belongs
    to reads the forwarder's one line instead. Anything that is not a file of
    exactly that shape answers ``None``.
    """
    if os.path.basename(path).lower() != WINDOWS_FORWARDER_NAME:
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4096)
        line = raw.decode("ascii")
    except (OSError, UnicodeDecodeError):
        return None
    if not (line.startswith('@"') and line.endswith('" %*\r\n')) or line.count("\n") != 1:
        return None
    return line[2 : -len('" %*\r\n')].replace("%%", "%")
