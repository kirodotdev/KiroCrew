"""Local machine (Linux): the seccomp filter every connector child runs under.

Bubblewrap does not unshare the network for a connector child, so the child
shares the user's network namespace, and with it the user's ABSTRACT unix
sockets (an X11 display, a session bus on some systems): sockets that carry the
user's own authority past the shared folder and need no file the mount fence
hides. This filter refuses the ways to reach one:

* ``socket(AF_UNIX, ...)`` answers ``EPERM``;
* ``socketpair(AF_UNIX, ...)`` is allowed only for ``SOCK_STREAM`` and
  ``SOCK_SEQPACKET``, so pipes between a tool's own processes keep working. A
  datagram pair (``SOCK_DGRAM``, or ``SOCK_RAW``, which the kernel turns into
  one) answers ``EPERM``: ``sendto`` with an explicit address on it reaches any
  unconnected abstract datagram listener of the user's session;
* ``io_uring_setup``/``_enter``/``_register`` answer ``EPERM``, since a ring can
  create and connect a socket without the ``socket`` call seccomp sees;
* ``keyctl``, ``add_key`` and ``request_key`` answer ``EPERM``: bubblewrap gives
  the child no session keyring of its own, so it would inherit the user's, which
  can hold Kerberos tickets and other logins (Flatpak refuses the same three);
* the x32 number space answers ``EPERM`` and any other architecture is killed,
  so no second syscall table bypasses the rule.

IP sockets are left alone: whether a command may use the network is the plan's
network policy, which is separate from protecting the user's credentials. A
filesystem unix socket inside the shared folder is refused too; that is the
cost of a rule seccomp can express (it cannot read the address being dialled).

The program is classic BPF in the ``struct sock_filter`` layout bubblewrap's
``--seccomp FD`` reads. It is built here, not shared with the agent sandbox's
launcher filter, which guards a different fence.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final, Mapping

AF_UNIX: Final[int] = 1
SOCK_STREAM: Final[int] = 1
SOCK_SEQPACKET: Final[int] = 5
#: The low bits of a socket type; the rest are ``SOCK_CLOEXEC``/``SOCK_NONBLOCK``.
SOCK_TYPE_MASK: Final[int] = 0xF
EPERM: Final[int] = 1
X32_SYSCALL_BIT: Final[int] = 0x40000000

RET_KILL_PROCESS: Final[int] = 0x80000000
RET_ERRNO: Final[int] = 0x00050000
RET_ALLOW: Final[int] = 0x7FFF0000
RET_EPERM: Final[int] = RET_ERRNO | EPERM

_LD_W_ABS: Final[int] = 0x20
_JEQ_K: Final[int] = 0x15
_JGE_K: Final[int] = 0x35
_RET_K: Final[int] = 0x06
_AND_K: Final[int] = 0x54
#: ``struct seccomp_data`` offsets: nr, arch, then args[0] (low word first).
_OFF_NR: Final[int] = 0
_OFF_ARCH: Final[int] = 4
_OFF_ARG0_LOW: Final[int] = 16
_OFF_ARG1_LOW: Final[int] = 24


@dataclass(frozen=True)
class SyscallTable:
    arch: int
    numbers: Mapping[str, int]


TABLES: Final[Mapping[str, SyscallTable]] = {
    "x86_64": SyscallTable(
        arch=0xC000003E,
        numbers={
            "read": 0,
            "socket": 41,
            "socketpair": 53,
            "add_key": 248,
            "request_key": 249,
            "keyctl": 250,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
        },
    ),
    "aarch64": SyscallTable(
        arch=0xC00000B7,
        numbers={
            "read": 63,
            "socket": 198,
            "socketpair": 199,
            "add_key": 217,
            "request_key": 218,
            "keyctl": 219,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
        },
    ),
}
MACHINES: Final[tuple[str, ...]] = tuple(TABLES)
#: Calls refused outright, whatever their arguments.
DENIED_CALLS: Final[tuple[str, ...]] = (
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "keyctl",
    "add_key",
    "request_key",
)


def _insn(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k)


def program(machine: str) -> bytes:
    """The filter for ``machine`` (``platform.machine()``); ``ValueError`` if none."""
    table = TABLES.get(machine)
    if table is None:
        raise ValueError(f"no seccomp table for {machine!r}")
    nums = table.numbers
    # Each row is (label, code, jump-if-true, jump-if-false, k); a jump names a
    # label, or None for the next row.
    rows: list[tuple[str | None, int, str | None, str | None, int]] = [
        (None, _LD_W_ABS, None, None, _OFF_ARCH),
        (None, _JEQ_K, "nr", "kill", table.arch),
        ("kill", _RET_K, None, None, RET_KILL_PROCESS),
        ("nr", _LD_W_ABS, None, None, _OFF_NR),
        (None, _JGE_K, "deny", None, X32_SYSCALL_BIT),
        *((None, _JEQ_K, "deny", None, nums[name]) for name in DENIED_CALLS),
        (None, _JEQ_K, "socket", None, nums["socket"]),
        (None, _JEQ_K, "pair", "allow", nums["socketpair"]),
        ("socket", _LD_W_ABS, None, None, _OFF_ARG0_LOW),
        (None, _JEQ_K, "deny", "allow", AF_UNIX),
        ("pair", _LD_W_ABS, None, None, _OFF_ARG0_LOW),
        (None, _JEQ_K, None, "allow", AF_UNIX),
        (None, _LD_W_ABS, None, None, _OFF_ARG1_LOW),
        (None, _AND_K, None, None, SOCK_TYPE_MASK),
        (None, _JEQ_K, "allow", None, SOCK_STREAM),
        (None, _JEQ_K, "allow", "deny", SOCK_SEQPACKET),
        ("allow", _RET_K, None, None, RET_ALLOW),
        ("deny", _RET_K, None, None, RET_EPERM),
    ]
    at = {label: i for i, (label, *_rest) in enumerate(rows) if label}

    def jump(row: int, label: str | None) -> int:
        return 0 if label is None else at[label] - row - 1

    return b"".join(
        _insn(code, jump(i, jt), jump(i, jf), k) for i, (_l, code, jt, jf, k) in enumerate(rows)
    )
