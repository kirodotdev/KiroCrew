"""Whether the data home belongs to the account running Kiro Crew.

A run as root (``sudo kirocrew ...``, where sudo keeps ``HOME``) writes root-owned
files into the person's own data home. After that the normal account cannot read
``config.json`` or create a Bot, and every symptom is a generic permission error
far from the cause. Two helpers cover it:

* :func:`root_run_refusal` stops that run before it writes anything.
* :func:`check_data_home` finds the damage an earlier run left, so the gateway
  log, ``kirocrew doctor``, ``/api/status`` and the Bot create route can say
  what is wrong and give the one command that repairs it.

POSIX only. Windows has no root run of this shape, and its ACLs are reported
elsewhere, so both helpers answer "nothing wrong" there.
"""

from __future__ import annotations

import logging
import os
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.platform_compat import IS_POSIX

logger = logging.getLogger(__name__)

#: The ``code`` the Bot create route answers with when this check fails.
DATA_HOME_NOT_OWNED = "data_home_not_owned"

#: Commands that are meant to run as root and handle the invoking person
#: themselves: ``service`` installs the system unit for ``SUDO_USER``, and
#: ``stop`` is the documented remedy for a gateway another account started.
ROOT_ALLOWED_COMMANDS = frozenset({"service", "stop"})


@dataclass(frozen=True)
class DataHomeAccess:
    """What :func:`check_data_home` found."""

    home: str
    #: Paths that exist but are owned by another account or not usable by this one.
    foreign: tuple[str, ...] = ()
    #: One shell line that gives the whole data home back to this account.
    fix_command: str = ""

    @property
    def ok(self) -> bool:
        return not self.foreign

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "home": self.home,
            "foreign": list(self.foreign),
            "fix_command": self.fix_command,
        }


def _account_name(uid: int) -> str:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def fix_command(home: Path) -> str:
    """The repair line for *home*: give it back to this account, owner read/write.

    The privileged part changes only entries another account owns, on the home's
    own filesystem, and never a non-directory with a second hard link: a link
    planted from inside the home to a root-owned file would otherwise hand that
    file to this account. ``chown -h`` changes a symlink itself, never its target.
    """
    quoted = shlex.quote(str(home.resolve()))
    user = shlex.quote(_account_name(os.geteuid()))
    return (
        f"sudo find {quoted} -xdev ! -user {user} \\( -type d -o -links 1 \\) "
        f"-exec chown -h {user} {{}} + && chmod -R u+rwX {quoted}"
    )


def _checked_paths(home: Path) -> tuple[Path, ...]:
    from kiro_crew.members import MEMBERS_DIR_NAME
    from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME

    return (
        home,
        home / "config.json",
        home / MEMBERS_DIR_NAME,
        home / MEMORY_STORES_DIR_NAME,
    )


def _usable(path: Path, uid: int) -> bool:
    """Whether *path* exists in a state this account can use; True when absent.

    A symlink is left alone: its target is outside what the repair may change,
    so reporting it would offer a command that cannot fix it. A file needs only
    to be this account's and readable, since config writes replace it through a
    sibling; a directory needs read, write and search.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    need = os.R_OK | (os.W_OK | os.X_OK if stat.S_ISDIR(st.st_mode) else 0)
    return st.st_uid == uid and os.access(path, need)


def check_data_home(home: Path) -> DataHomeAccess:
    """Report which of the data home's key entries this account cannot use.

    Checks the home itself, ``config.json`` and the Bot (members) and memory
    store directories: the entries every Bot create and every config read go
    through. A missing entry is fine; it is created on first use. Read-only.
    """
    if not IS_POSIX:
        return DataHomeAccess(home=str(home))
    uid = os.geteuid()
    # A linked home is checked, and repaired, at the folder it points to.
    real = home.resolve()
    foreign = tuple(str(p) for p in _checked_paths(real) if not _usable(p, uid))
    return DataHomeAccess(
        home=str(home),
        foreign=foreign,
        fix_command=fix_command(home) if foreign else "",
    )


def warn_if_data_home_not_owned(home: Path) -> None:
    """Log :func:`check_data_home`'s finding once; the gateway calls it after ready."""
    access = check_data_home(home)
    if not access.ok:
        logger.warning(
            "Some Kiro Crew files belong to another user, so Kiro Crew can't read "
            "them: %s. Fix: %s",
            ", ".join(access.foreign),
            access.fix_command,
        )


def _nearest_owner(path: Path) -> int | None:
    """The uid owning *path*, or its closest existing parent."""
    for candidate in (path, *path.parents):
        try:
            return candidate.stat().st_uid
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return None


def root_run_refusal(home: Path, command: str | None, argv: list[str]) -> str:
    """The sentence that stops a root run aimed at a person's data home, or ``""``.

    Refuses only when this process is root AND *home* (or the closest folder
    above it that exists) belongs to another account: that is the run that
    leaves root-owned files the person cannot read. Root working in
    root's own home writes nothing foreign and is left alone, as are the
    :data:`ROOT_ALLOWED_COMMANDS`.
    """
    if not IS_POSIX or os.geteuid() != 0 or command in ROOT_ALLOWED_COMMANDS:
        return ""
    owner = _nearest_owner(home)
    if owner is None or owner == 0:
        return ""
    again = " ".join(["kirocrew", *(shlex.quote(a) for a in argv)])
    return (
        f"Kiro Crew is running as root, but {home} belongs to {_account_name(owner)}. "
        f"Run it without sudo: {again}"
    )
