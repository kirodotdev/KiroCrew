"""Is this process running in a remote session, and how did the person get here?

One answer for every surface that changes behaviour off a local desktop: the
``kirocrew doctor`` remote-access hint, the signed-out sign-in screen's browser
open, and the login transport. The SSH variables alone are not enough: an
AWS Systems Manager Session Manager shell sets neither ``SSH_CONNECTION`` nor
``SSH_CLIENT``.

Every function here is cheap, never raises, and answers "not detected" when it
cannot tell.
"""

from __future__ import annotations

import functools
import ipaddress
import os

#: Bound on the ancestry walk. A shell is a handful of hops below the Session
#: Manager worker even after a sudo/su switch; the cap stops a cycle or a long
#: chain from stalling the caller.
SSM_ANCESTRY_MAX_DEPTH = 20

#: The kernel caps a process ``comm`` at 15 visible characters, so the worker
#: ``ssm-session-worker`` shows as its 15-character prefix.
_SSM_WORKER_COMM = "ssm-session-worker"[:15]


def is_ssh_session() -> bool:
    """Return ``True`` when the shell was reached over SSH."""
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))


@functools.cache
def is_ssm_session() -> bool:
    """Return ``True`` when this process descends from a Session Manager shell.

    Computed once per process: the dashboard's auth-status poll reaches this on
    the event loop, and a process's launch shell does not change. Tests reset it
    with ``is_ssm_session.cache_clear()``.

    Session Manager sets no environment variable, and none would survive
    ``sudo -iu <user>``. The reliable signal is process ancestry: an interactive
    Session Manager shell runs under ``ssm-session-worker``. Walking the whole
    ancestor chain keeps the signal after a user switch.

    Linux only (the process helpers answer ``None`` elsewhere).
    """
    from kiro_crew.platform_compat import linux_process_name, parent_pid

    try:
        pid: int | None = os.getpid()
        for _ in range(SSM_ANCESTRY_MAX_DEPTH):
            if pid is None or pid <= 1:
                break
            if linux_process_name(pid) == _SSM_WORKER_COMM:
                return True
            pid = parent_pid(pid)
    except Exception:
        return False
    return False


def is_remote_session() -> bool:
    """Return ``True`` for an SSH or a Session Manager shell."""
    return is_ssh_session() or is_ssm_session()


def ssh_server_address() -> str | None:
    """Return the globally routable address the SSH client connected to, or ``None``.

    ``SSH_CONNECTION`` is ``client_ip client_port server_ip server_port``. The
    third field is the address the client reached this box on. A private value
    (a box behind NAT sees its own private address) is no more reachable from a
    laptop than the box's own hostname, so it answers ``None`` like a missing or
    malformed value.
    """
    parts = os.environ.get("SSH_CONNECTION", "").split()
    if len(parts) < 3:
        return None
    try:
        if not ipaddress.ip_address(parts[2]).is_global:
            return None
    except ValueError:
        return None
    return parts[2]
