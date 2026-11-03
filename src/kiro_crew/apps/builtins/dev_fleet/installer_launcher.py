"""The installer's launchd launcher, recorded so "Restore installed build" can put it back.

On launchd a cutover rewrites the ``live-gateway`` launcher script in place; the plist
never changes (see :mod:`gateway_service`). The launcher ``kirocrew service install``
wrote is a bare pass-through, ``exec '<installed kirocrew>' "$@"``, and nothing else
remembers which binary it named. It cannot be re-derived from inside a gateway that
runs a worktree either: ``kirocrew_bin`` would find the worktree's venv through PATH.
So the FIRST Make Live on a launchd host records that script here, before staging, and
Restore writes it back.

The record is a code-execution input, like the pointer: it names the binary the
gateway runs after a Restore. It lives in ``live_target.staging_path()``, the
directory beside the pointer that is masked in every agent namespace and fenced by
``security.paths`` as a whole directory, written owner-only (``0600``) with a single
link, and the reader refuses anything else: another owner, a wider mode, a second
link, an unexpected shape, a relative path, or a launcher that is not the exact
installer rendering of an executable that still exists.
"""

from __future__ import annotations

import errno
import json
import os
import shlex
import stat
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.service import live_target
from kiro_crew.service.macos import render_live_program

_FILENAME = "installer-launcher.json"
_VERSION = 1
_KEYS = frozenset({"version", "live_program", "content"})


class InvalidRecord(ValueError):
    """The record exists but cannot be trusted; the message says why."""


def record_path() -> Path:
    return live_target.staging_path() / _FILENAME


def installer_bin(content: str) -> str | None:
    """The binary *content* execs when it is exactly the installer's launcher, else None.

    The installer renders ``render_live_program(<bin>)`` with no working directory and
    no PATH; a cutover always adds both. Re-rendering the parsed binary and comparing
    the whole text rejects anything else, a Make Live launcher included.
    """
    lines = content.splitlines()
    if not lines:
        return None
    try:
        parts = shlex.split(lines[-1])
    except ValueError:
        return None
    if len(parts) != 3 or parts[0] != "exec" or parts[2] != "$@":
        return None
    target = parts[1]
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
        return None
    if not os.path.isabs(target):
        return None
    return target if render_live_program(target) == content else None


def is_installers(content: str | None) -> bool:
    """Whether *content* is the installer's launcher: its exact rendering of a binary
    outside the pinned checkout. A worktree's own ``service install`` has the same
    shape but execs the worktree, so shape alone does not establish identity."""
    binary = installer_bin(content) if content is not None else None
    if binary is None:
        return False
    pinned = live_target.pinned_checkout()
    if pinned is None:
        # Nothing pinned is the ordinary case; a pointer that is pinned but names no
        # readable path leaves the identity unknown, so the saved record decides.
        return not live_target.is_pinned()
    return not Path(binary).is_relative_to(pinned)


def record_first_cutover(live_program: Path, current: str | None) -> bool:
    """Record *current* as the installer's launcher. True when it was written.

    Only when *current* is the installer's rendering of a binary outside the pinned
    checkout (a worktree's own ``service install`` is not the installer's). A newer
    installer launcher replaces an older record, so a later ``service install`` from
    another installation is what Restore writes back; a cutover's own launcher is not
    the installer's and never replaces it. An already-pinned pointer does not block
    recording, since a foreground or staged-only cutover pins without rewriting the
    launcher. Raises ``OSError`` when the write or its link check fails, so the
    caller refuses the cutover before staging.
    """
    if current is None or not is_installers(current):
        return False
    path = record_path()
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("content") == current:
            return False
    except (OSError, ValueError, AttributeError):
        pass
    staging = live_target.ensure_staging_dir()
    payload = {"version": _VERSION, "live_program": str(live_program), "content": current}
    dest = staging / _FILENAME
    atomic_write(dest, json.dumps(payload, indent=2) + "\n", mode=0o600, restrict_to_owner=True)
    final = os.lstat(dest)
    if not stat.S_ISREG(final.st_mode) or final.st_nlink != 1:
        dest.unlink(missing_ok=True)
        raise OSError(errno.EMLINK, f"refusing {dest}: it has {final.st_nlink} hard link(s)")
    return True


def _read_owned(path: Path) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise InvalidRecord(f"{path} is a symlink") from exc
        raise
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise InvalidRecord(f"{path} is not a regular file")
        if st.st_nlink != 1:
            raise InvalidRecord(f"{path} has {st.st_nlink} hard links")
        uid = platform_compat.effective_uid()
        if uid is not None:
            if st.st_uid != uid:
                raise InvalidRecord(f"{path} is not owned by this user")
            if st.st_mode & 0o077:
                raise InvalidRecord(f"{path} is not owner-only")
        raw = fh.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidRecord(f"{path} is not UTF-8") from exc


def load_record(live_program: Path) -> str | None:
    """The recorded launcher text, ``None`` when there is no record.

    Raises :class:`InvalidRecord` for a record that fails any check (see the module
    docstring) and ``OSError`` for one that exists but cannot be read.
    """
    path = record_path()
    raw = _read_owned(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidRecord(f"{path} is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != _KEYS or data["version"] != _VERSION:
        raise InvalidRecord(f"{path} does not have the expected shape")
    recorded_program, content = data["live_program"], data["content"]
    if not isinstance(recorded_program, str) or not isinstance(content, str):
        raise InvalidRecord(f"{path} does not have the expected shape")
    if not os.path.isabs(recorded_program) or recorded_program != str(live_program):
        raise InvalidRecord(f"{path} names a different launcher than {live_program}")
    target = installer_bin(content)
    if target is None:
        raise InvalidRecord(f"{path} does not hold an installer launcher")
    if not os.path.isfile(target) or not os.access(target, os.X_OK):
        raise InvalidRecord(f"the recorded installed build {target} is no longer executable")
    return content


def forget_record() -> bool:
    """Delete the record after a successful Restore. False when it could not be removed."""
    try:
        record_path().unlink(missing_ok=True)
        return True
    except OSError:
        return False


__all__ = (
    "InvalidRecord",
    "forget_record",
    "installer_bin",
    "is_installers",
    "load_record",
    "record_first_cutover",
    "record_path",
)
