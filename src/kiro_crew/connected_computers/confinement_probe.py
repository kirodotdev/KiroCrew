"""Standalone confinement witness probe; importing it performs no I/O.

Both the unconfined control and the confined run execute this file with an
explicit witness specification. File opens refuse links at every component.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
from typing import Any


def probe_open(path: str, flags: int) -> int:
    parts = os.path.abspath(path).split(os.sep)
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
    finally:
        os.close(fd)


def read(path: str) -> str:
    try:
        with os.fdopen(probe_open(path, os.O_RDONLY), encoding="ascii") as handle:
            return handle.read(4096)
    except OSError:
        return "denied"


def write(path: str) -> str:
    try:
        path = path + "-" + secrets.token_hex(16)
        with os.fdopen(
            probe_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w", encoding="ascii"
        ) as handle:
            handle.write("x")
    except OSError:
        return "denied"
    return "allowed"


def listing(path: str, name: str) -> str:
    try:
        fd = probe_open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return "allowed" if name in os.listdir(fd) else "denied"
        finally:
            os.close(fd)
    except OSError:
        return "denied"


def connect(family: int, address: Any) -> str:
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return "denied"
    try:
        sock.settimeout(5)
        sock.connect(address)
        return "allowed"
    except OSError:
        return "denied"
    finally:
        sock.close()


def keyring(nr: int) -> str:
    """Read the session keyring id without creating a keyring."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        got = libc.syscall(ctypes.c_long(nr), ctypes.c_long(0), ctypes.c_long(-3), ctypes.c_long(0))
    except Exception:  # noqa: BLE001 -- an unavailable probe cannot prove confinement
        return "error"
    return "allowed" if got >= 0 else "denied"


def run(spec: dict[str, Any]) -> dict[str, str]:
    return {
        "root_read": read(spec["root_file"]),
        "root_write": write(spec["root_new"]),
        "out_read": read(spec["out_file"]),
        "out_write": write(spec["out_new"]),
        "out_list": listing(spec["out_dir"], spec["out_name"]),
        "link_read": read(spec["link"]),
        "net": connect(socket.AF_INET, ("127.0.0.1", spec["tcp_port"])),
        "scratch_write": write(os.path.join(os.environ.get("TMPDIR", ""), spec["scratch_name"])),
        "ipc": connect(socket.AF_UNIX, "\0" + spec["ipc_abstract"]),
        "keyring": keyring(spec["keyctl"]) if spec["keyctl"] else "skip",
    }


if __name__ == "__main__":
    print(json.dumps(run(json.loads(sys.argv[1]))))
