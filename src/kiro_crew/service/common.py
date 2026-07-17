"""Platform detection and shared service constants."""

from __future__ import annotations

import enum
import os
import shutil
import sys

SERVICE_NAME = "kirocrew"  # systemd unit name (without .service)
LAUNCHD_LABEL = "dev.kirocrew.gateway"  # launchd Label


def kirocrew_bin() -> str:
    """Return the resolved kirocrew executable path, or fall back to sys.argv[0].

    Used by both the systemd unit and the launchd plist as ``ExecStart`` /
    ``ProgramArguments``. Falls back to ``sys.argv[0]`` for development
    installs where ``kirocrew`` isn't on the global PATH.
    """
    found = shutil.which("kirocrew")
    if found:
        return found
    return os.path.realpath(sys.argv[0])


def service_path(home: str) -> str:
    """Build the PATH for the gateway's service environment.

    Snapshots the installer's current ``$PATH`` so subprocesses spawned
    by the gateway (git, node, etc.) resolve the same way they did in the
    interactive shell that ran ``kirocrew service install``. Common
    user-local bin dirs (``~/.local/bin``) and POSIX defaults are
    prepended in case the installer's ``$PATH`` is missing them.
    Duplicates are removed while preserving order.
    """
    required = [
        f"{home}/.local/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    env_path = [p for p in os.environ.get("PATH", "").split(":") if p]
    seen: set[str] = set()
    out: list[str] = []
    for entry in required + env_path:
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
    return ":".join(out)


class Platform(enum.Enum):
    """Supported service-management platforms."""

    # System-level systemd. Unit lives at /etc/systemd/system/, write
    # and control commands require sudo. The name reflects the privilege
    # model: a user-level (~/.config/systemd/user/) variant doesn't work
    # on older systemd (e.g. 219), so we don't ship one.
    SYSTEMD = "systemd"
    LAUNCHD = "launchd"
    UNSUPPORTED = "unsupported"


def current_platform() -> Platform:
    """Return the platform whose service manager we should target.

    Linux with systemctl on PATH → SYSTEMD.
    macOS with launchctl on PATH → LAUNCHD.
    Anything else → UNSUPPORTED.
    """
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return Platform.SYSTEMD
    if sys.platform == "darwin" and shutil.which("launchctl"):
        return Platform.LAUNCHD
    return Platform.UNSUPPORTED


def restart_command_hint() -> str:
    """Return the shell command that actually restarts the installed gateway.

    The correct command depends on how the service is installed, and the
    scopes are not interchangeable — printing the wrong one sends the user
    down a dead end (Mesh-2583):

    * ``SYSTEMD`` — the unit is **system-level** at
      ``/etc/systemd/system/kirocrew.service`` (see
      :mod:`kiro_crew.service.linux`). ``systemctl --user`` fails on AL2
      (no per-user systemd manager), so the working command needs sudo:
      ``sudo systemctl restart kirocrew``.
    * ``LAUNCHD`` / ``UNSUPPORTED`` — defer to the service-aware
      ``kirocrew restart`` CLI, which resolves the right mechanism itself.

    Centralised so the update path and the Slack restart-failure hint share
    one source of truth and can never drift back to the broken
    ``systemctl --user`` string.
    """
    if current_platform() is Platform.SYSTEMD:
        return f"sudo systemctl restart {SERVICE_NAME}"
    return "kirocrew restart"
