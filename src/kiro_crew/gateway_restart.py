"""Gateway restart target selection through the composed lifecycle provider.

Also the readers of the service manager's loaded unit (``_systemd_show``) and of the
launchd launcher (``_launcher_target``) that :mod:`kiro_crew.service.gateway_identity`
and the doctor share."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.platform.context import current_context
from kiro_crew.subprocess_utf8 import UTF8_TEXT


def resolve_restart_launcher() -> str | None:
    """Validate the edition launcher before draining any live sessions.

    Imported by restart consumers before an update can retire their import tree.
    None alone opts into the core's existing Python/managed-venv resolver. A bad
    explicit target or a provider error refuses restart, never falls back to A.
    This is an availability check, not a new authorization boundary: the trusted
    composition root supplies the provider, not request/config/environment data.
    """
    launcher = current_context().gateway_lifecycle.restart_launcher()
    if launcher is None:
        return None
    if not isinstance(launcher, str) or not launcher or "\0" in launcher:
        raise ValueError("Cannot restart: invalid gateway launcher path")
    path = Path(launcher)
    if not path.is_absolute() or not path.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError("Cannot restart: gateway launcher must be an absolute executable file")
    if platform_compat.IS_WINDOWS and path.suffix.lower() != ".exe":
        raise ValueError("Cannot restart: gateway launcher must be a native Windows executable")
    # Do not resolve symlinks: dispatchers can select the app from this basename.
    return launcher


#: Budget of one ``systemctl show`` query.
_SYSTEMCTL_TIMEOUT_SECS = 3.0


class _Inconclusive(Exception):
    """The supervisor's command could not be established; nothing is proven."""


def _systemd_show(user: bool) -> dict[str, list[str]]:
    """The unit's properties as one scope's manager has them loaded.

    ``systemctl`` comes from the fixed system directories only: this runs inside
    the gateway, whose ``PATH`` can lead with a directory a planted shim could
    sit in.
    """
    from kiro_crew.service import common

    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        raise _Inconclusive("systemctl is not in a trusted system directory")
    argv = [systemctl]
    if user:
        argv.append("--user")
    argv.append("show")
    for prop in (
        "Id",
        "LoadState",
        "MainPID",
        "NeedDaemonReload",
        "ExecStart",
        "Environment",
        "EnvironmentFiles",
    ):
        argv += ["-p", prop]
    argv.append(f"{common.SERVICE_NAME}.service")
    res = subprocess.run(
        argv,
        capture_output=True,
        check=False,
        env=common.systemctl_user_env() if user else None,
        timeout=_SYSTEMCTL_TIMEOUT_SECS,
        **UTF8_TEXT,
    )
    props: dict[str, list[str]] = {}
    for line in (res.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props.setdefault(key.strip(), []).append(value.strip())
    return props


def _launcher_target(launcher: Path) -> str:
    """What the launchd launcher script finally ``exec``s; the launcher if unparsable."""
    if not launcher.is_file():
        return str(launcher)
    try:
        text = launcher.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _Inconclusive(f"its launcher {launcher} could not be read ({exc})") from exc
    for line in reversed(text.splitlines()):
        if line.startswith("exec "):
            words = shlex.split(line, posix=True)
            return words[1] if len(words) > 1 else str(launcher)
    return str(launcher)
