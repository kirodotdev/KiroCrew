"""The ssh-agent socket a gateway offers to the shared MCP backends an operator names.

Every pooled MCP backend runs from the gateway daemon with ``SSH_AUTH_SOCK``
scrubbed: once before the daemon is spawned and again for each backend. A backend
whose credential client reaches the operator's identity only through an ssh-agent
(an SSO client that forwards a laptop session over the agent, for example) then
cannot authenticate at all when it is shared. The operator can name such backends
in ``ssh_auth_sock_consent.json`` (``mcp_gateway_backends``); this module holds
the socket they are offered.

Which socket, and why the BOOT value
------------------------------------
The value offered is the ``SSH_AUTH_SOCK`` this gateway was started with,
recorded by :func:`record_boot_agent_socket` from ``cli.main`` before the
platform boots. Code that runs later may repoint the live variable at a socket
it discovered (a companion that falls back to a forwarded agent does exactly
that), and such a socket is typically one SSH connection's own path, which
vanishes when that connection ends. A pooled backend outlives connections, so
it is offered only the socket its operator gave the gateway: usually a stable
path (a service drop-in that points at a link the forwarder keeps current).

An in-app restart (``platform_compat.reexec_*``) keeps the live environment,
including a socket something repointed at. The recorded value therefore rides
across the exec in :data:`BOOT_MARKER_ENV` (``platform_compat.keep_for_reexec``),
and the successor takes that instead of its inherited ``SSH_AUTH_SOCK``. An empty
marker means the lineage started with no socket, so none is offered. One gap: a
build that predates the marker carries none, so the first in-app restart INTO
this build records the live value; a cold restart of the gateway records the
launch value again.

The gateway hands the value to gatewayd in :data:`DAEMON_ENV`, set after the
daemon's env scrub and taken out again before gatewayd launches anything
(:func:`take_daemon_socket`). It is not an argv flag because a command line is
readable by every local process, an environment only by its owner. Both names
start with ``SSH_AUTH_SOCK`` on purpose: every credential scrub matches that
prefix, so neither can reach a backend or an agent child even if it is left
behind.

Path vetting
------------
:func:`agent_socket_refusal` runs before every offer. It refuses a socket whose
directory another user owns or can write to, a socket or link another user
owns, and a link whose target directory does not exist: after an SSH connection
ends its per-connection directory is gone, and another local user could
recreate it. A path that does not exist yet inside a private directory is
offered, because only its owner can create it there, and a client that finds no
socket falls back exactly as it would without the variable. A link whose target
directory exists and is private, but whose target socket does not exist yet, is
offered for the same reason.

The check is made at each launch and covers the immediate directory of the path
and of a link's target, not their ancestors. It cannot cover what happens after
the launch: a backend keeps the path it was given, so a link the forwarder later
points elsewhere, or a target directory that later disappears, is followed at
connect time. The decision is also kept for the backend's life: one launched
while the link dangles (no forwarded connection is live) gets no socket until it
exits, by idle eviction or a gateway restart. Point the variable at a stable link
the forwarder keeps current, in a private directory.
"""

from __future__ import annotations

import os
import stat
import sys
from typing import MutableMapping, Optional

from kiro_crew import platform_compat

#: Carries the boot value to this gateway's own exec successor, and only to it.
BOOT_MARKER_ENV = "SSH_AUTH_SOCK_KIROCREW_BOOT"

#: Carries the boot value from the gateway to the gatewayd it spawns.
DAEMON_ENV = "SSH_AUTH_SOCK_KIROCREW_GATEWAYD"

#: ``sizeof(sockaddr_un.sun_path)``: 108 on Linux, 104 on macOS and the BSDs. A
#: longer path cannot be connected to, so it is not a socket path to offer.
_SUN_PATH_MAX = 108 if sys.platform.startswith("linux") else 104

_recorded = False
_boot_socket: Optional[str] = None


def plausible_socket_path(value: str) -> bool:
    """Whether *value* could name a unix socket: absolute, no NUL, short enough."""
    if not value or "\x00" in value or not os.path.isabs(value):
        return False
    return len(os.fsencode(value)) < _SUN_PATH_MAX


def record_boot_agent_socket(environ: Optional[MutableMapping[str, str]] = None) -> None:
    """Record the agent socket this gateway lineage was started with.

    Called once from ``cli.main`` for the ``gateway`` command, before the platform
    boots. The first call in a process wins. The exec marker is always taken out
    of the environment, so no descendant inherits it; only this process's own
    exec successor gets it back.
    """
    global _recorded, _boot_socket
    source = os.environ if environ is None else environ
    carried = source.pop(BOOT_MARKER_ENV, None)
    if _recorded:
        return
    _recorded = True
    value = carried if carried is not None else source.get("SSH_AUTH_SOCK", "")
    _boot_socket = value if plausible_socket_path(value) else None
    if environ is None:
        platform_compat.keep_for_reexec(BOOT_MARKER_ENV, _boot_socket or "")


def boot_agent_socket() -> Optional[str]:
    """The socket :func:`record_boot_agent_socket` recorded, or ``None``."""
    return _boot_socket


def take_daemon_socket(environ: Optional[MutableMapping[str, str]] = None) -> Optional[str]:
    """Remove :data:`DAEMON_ENV` from gatewayd's environment and return its value.

    ``None`` when it is absent or not a plausible socket path.
    """
    source = os.environ if environ is None else environ
    value = source.pop(DAEMON_ENV, "")
    return value if plausible_socket_path(value) else None


def _reset_for_tests() -> None:
    global _recorded, _boot_socket
    _recorded = False
    _boot_socket = None


def _private_dir_refusal(directory: str, euid: int) -> Optional[str]:
    try:
        entry = os.lstat(directory)
        st = os.stat(directory)
    except OSError:
        return "its directory does not exist"
    if stat.S_ISLNK(entry.st_mode) and entry.st_uid != euid:
        return "its directory is a link another user owns"
    if not stat.S_ISDIR(st.st_mode):
        return "its directory is not a directory"
    if st.st_uid != euid:
        return "its directory belongs to another user"
    if st.st_mode & 0o022:
        return "its directory is writable by other users"
    return None


def agent_socket_refusal(path: str) -> Optional[str]:
    """Why *path* must not be offered to a backend, or ``None`` to offer it.

    BLOCKING (a few ``stat`` calls): call it off the event loop.
    """
    if platform_compat.IS_WINDOWS:
        return "not offered on Windows"
    if not plausible_socket_path(path):
        return "not an absolute socket path"
    euid = os.geteuid()
    refusal = _private_dir_refusal(os.path.dirname(path), euid)
    if refusal is not None:
        return refusal
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot be read ({exc.strerror})"
    if st.st_uid != euid:
        return "it belongs to another user"
    if stat.S_ISSOCK(st.st_mode):
        return None
    if not stat.S_ISLNK(st.st_mode):
        return "it is not a socket or a link to one"
    target = os.path.join(os.path.dirname(path), os.readlink(path))
    refusal = _private_dir_refusal(os.path.dirname(target), euid)
    if refusal is not None:
        return f"its link target: {refusal}"
    try:
        tst = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"its link target cannot be read ({exc.strerror})"
    if not stat.S_ISSOCK(tst.st_mode):
        return "its link target is not a socket"
    if tst.st_uid != euid:
        return "its link target belongs to another user"
    return None
