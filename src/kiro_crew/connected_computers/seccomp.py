"""Local machine (Linux): the seccomp filter every connector child runs under.

Bubblewrap does not unshare the network for a connector child, so the child
shares the user's network namespace, and with it the user's ABSTRACT unix
sockets (an X11 display, a session bus on some systems): sockets that carry the
user's own authority past the shared folder and need no file the mount fence
hides. This filter refuses the ways to reach one:

* ``socket(AF_UNIX, ...)`` answers ``EPERM`` (``socketpair`` is a different call
  and stays allowed: pipes between a tool's own processes keep working);
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
#: ``struct seccomp_data`` offsets: nr, arch, then args[0] (low word first).
_OFF_NR: Final[int] = 0
_OFF_ARCH: Final[int] = 4
_OFF_ARG0_LOW: Final[int] = 16


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
    denied = tuple(nums[name] for name in DENIED_CALLS)
    # Layout, by index:
    #   0 LD arch; 1 JEQ arch ? 3 : 2; 2 RET KILL
    #   3 LD nr; 4 JGE x32 bit ? DENY
    #   5.. JEQ each denied ? DENY
    #   s  JEQ socket ? s+1 : ALLOW; s+1 LD args[0] low; s+2 JEQ AF_UNIX ? DENY : ALLOW
    #   ALLOW: RET ALLOW; DENY: RET EPERM
    out = [
        _insn(_LD_W_ABS, 0, 0, _OFF_ARCH),
        _insn(_JEQ_K, 1, 0, table.arch),
        _insn(_RET_K, 0, 0, RET_KILL_PROCESS),
        _insn(_LD_W_ABS, 0, 0, _OFF_NR),
    ]
    socket_at = 5 + len(denied)
    allow_at = socket_at + 3
    deny_at = allow_at + 1

    def to(target: int) -> int:
        return target - (len(out) + 1)

    out.append(_insn(_JGE_K, to(deny_at), 0, X32_SYSCALL_BIT))
    for nr in denied:
        out.append(_insn(_JEQ_K, to(deny_at), 0, nr))
    out.append(_insn(_JEQ_K, 0, to(allow_at), nums["socket"]))
    out.append(_insn(_LD_W_ABS, 0, 0, _OFF_ARG0_LOW))
    out.append(_insn(_JEQ_K, to(deny_at), to(allow_at), AF_UNIX))
    out.append(_insn(_RET_K, 0, 0, RET_ALLOW))
    out.append(_insn(_RET_K, 0, 0, RET_EPERM))
    if len(out) != deny_at + 1:
        raise RuntimeError("seccomp program layout drifted")
    return b"".join(out)
