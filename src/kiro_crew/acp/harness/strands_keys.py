"""The one-time file that carries a Strands session's provider keys to its gate module.

A key set in a process's environment when it starts stays readable for that process's
whole life, from ``/proc/<pid>/environ`` on Linux, by any same-user process its own
children run. The Strands CLI is a Node process whose Python worker is its child, and
the agent's shells are children of that worker, so a key in either of their launch
environments is one ``cat /proc/$PPID/environ`` away from the model. So no provider
key Kiro Crew holds for this harness is ever put in a launch environment.

Instead the keys are written to a file only this session can read, and the child is
told its path. The gate module (``agent_sdk/gate_extensions/strands``) reads the file
into the worker's own ``os.environ`` (a runtime change, which ``/proc/<pid>/environ``
does not show), removes it before any tool can run, and refuses to start on a file
that is missing, a link, not a regular file, owned by someone else, or readable by
anyone but its owner. The Node CLI never reads the keys: in project mode the model is
called from the Python worker alone.

The file lives in a fresh owner-only directory inside the session's private scratch
window, the one directory the sandboxed child can both read and remove a file in, and
which no other session sees. A file the gate never loaded (the spawn died first) is
removed by the next spawn of the session before its child starts, and that spawn is
refused when the removal cannot be proven (``strands._sweep_keys_dirs``). The gate
likewise refuses to start when it cannot remove the file it read.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile

#: The variable that names the key file to the gate module. Its value is a path.
KEYS_FILE_ENV = "KIROCREW_STRANDS_KEYS_FILE"

#: The prefix of the one-time directory that holds the file. The gate removes the
#: directory too when its name carries this prefix.
KEYS_DIR_PREFIX = "kirocrew-strands-keys-"
KEYS_FILE_NAME = "keys.json"


def write_keys_file(scratch_dir: str, values: dict[str, str]) -> str:
    """Write *values* to a fresh owner-only file under *scratch_dir*; return its path.

    The directory is created ``0700`` by :func:`tempfile.mkdtemp` and the file with
    ``O_CREAT | O_EXCL`` (and ``O_NOFOLLOW`` where the platform has it) at ``0600``,
    so nothing that existed before this call is opened or followed. Blocking.
    """
    directory = tempfile.mkdtemp(prefix=KEYS_DIR_PREFIX, dir=scratch_dir)
    path = os.path.join(directory, KEYS_FILE_NAME)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(values, handle)
    except BaseException:
        discard_keys_file(path)
        raise
    return path


def discard_keys_file(path: str) -> None:
    """Remove the key file's one-time directory, file included. Blocking."""
    if path:
        remove_keys_dir(os.path.dirname(path))


def remove_keys_dir(directory: str) -> None:
    """Remove a one-time key directory, never anything a link points at. Blocking.

    A directory left in the session's scratch window is the agent's to tamper with:
    an entry with this prefix may be a link to somewhere else. So only a REAL
    directory (``lstat``, not followed) with the prefix is removed, through
    :func:`shutil.rmtree`, which on these platforms walks by descriptor and unlinks
    a link inside rather than following it; a link or any other kind of entry is
    left alone.
    """
    if not os.path.basename(directory).startswith(KEYS_DIR_PREFIX):
        return
    try:
        info = os.lstat(directory)
    except OSError:
        return
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return
    shutil.rmtree(directory, ignore_errors=True)
