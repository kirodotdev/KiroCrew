"""The driver's environment: an allowlist, with telemetry and update checks forced off.

``cua-driver`` reads its switches from the environment, and two defaults point the
wrong way for Kiro Crew: routine telemetry is ON (an event per MCP tool call to a
hosted analytics endpoint) and a long-lived ``mcp`` process polls GitHub for new
releases. Its own config file and telemetry id live under ``~/.cua-driver`` unless
``CUA_DRIVER_RS_HOME`` points elsewhere.

:func:`driver_env` is the ONE function that builds the child's environment, so
the fence is pinned by a single test. It starts from nothing and copies only the
names in :data:`ALLOWED_NAMES` / :data:`ALLOWED_PREFIXES` (what a desktop client
needs to find the display, the session bus and the user's locale), then forces
:data:`FORCED_ENV` over the top. Cloud credentials, proxy settings, tokens and
every inherited ``CUA_*`` variable are therefore absent by construction: the
driver needs no network at all.

Upstream reads telemetry consent in this order: ``DO_NOT_TRACK``, then
``CUA_DRIVER_RS_TELEMETRY_ENABLED``, ``CUA_TELEMETRY_ENABLED``, ``CUA_TELEMETRY``,
then a config file under ``CUA_HOME`` (default ``~/.cua``), then the persisted
setting under the driver home. All four variables are set, and both homes point
at the private directory, so no user-level file can turn it back on.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

#: Names copied from the gateway's environment when present.
ALLOWED_NAMES: frozenset[str] = frozenset(
    {
        # Process basics.
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        # The X11 / Wayland display and the session bus the driver talks AT-SPI on.
        "DISPLAY",
        "XAUTHORITY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_TYPE",
        "XDG_SESSION_DESKTOP",
        "XDG_CURRENT_DESKTOP",
        "XDG_DATA_DIRS",
        "DESKTOP_SESSION",
        "DBUS_SESSION_BUS_ADDRESS",
        "AT_SPI_BUS_ADDRESS",
        # Toolkit switches that keep an app's accessibility bridge on.
        "GTK_A11Y",
        "NO_AT_BRIDGE",
        "GNOME_ACCESSIBILITY",
        "QT_ACCESSIBILITY",
        "QT_LINUX_ACCESSIBILITY_ALWAYS_ON",
        # Compositor IPC endpoints the Linux driver discovers windows through.
        "SWAYSOCK",
        "HYPRLAND_INSTANCE_SIGNATURE",
        "NIRI_SOCKET",
        # Windows: the directories every Win32 process expects.
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "USERPROFILE",
        "USERNAME",
        "USERDOMAIN",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "TEMP",
        "TMP",
        "COMSPEC",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "HOMEDRIVE",
        "HOMEPATH",
    }
)

#: Prefixes copied from the gateway's environment (locale categories).
ALLOWED_PREFIXES: tuple[str, ...] = ("LC_",)

#: Variable naming the driver's private home (config, ids, caches).
HOME_ENV = "CUA_DRIVER_RS_HOME"
#: Variable naming the home of the shared upstream config file telemetry consults.
SHARED_HOME_ENV = "CUA_HOME"

#: Values forced over anything the base carries.
FORCED_ENV: Mapping[str, str] = {
    "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
    "CUA_TELEMETRY_ENABLED": "0",
    "CUA_TELEMETRY": "0",
    "DO_NOT_TRACK": "1",
    "CUA_DRIVER_RS_UPDATE_CHECK": "false",
    # The promptless default agent-authorization mode. Named so an inherited
    # value can never select the mode that bypasses approvals.
    "CUA_DRIVER_PERMISSION_MODE": "standard",
}


def _allowed(name: str, *, case_insensitive: bool) -> bool:
    key = name.upper() if case_insensitive else name
    return key in ALLOWED_NAMES or key.startswith(ALLOWED_PREFIXES)


def desktop_env(
    base: Mapping[str, str], *, case_insensitive: "bool | None" = None
) -> dict[str, str]:
    """The allowlisted desktop basics alone, for an application Kiro Crew launches.

    Same allowlist as :func:`driver_env` and none of its forced values: a launched
    app gets the display, session bus and locale, never the gateway's secrets.
    """
    if case_insensitive is None:
        case_insensitive = os.name == "nt"
    return {k: v for k, v in base.items() if _allowed(k, case_insensitive=case_insensitive)}


def driver_env(
    base: Mapping[str, str],
    home: "str | os.PathLike[str]",
    *,
    case_insensitive: "bool | None" = None,
) -> dict[str, str]:
    """The environment the driver child is started with.

    *base* is the gateway's own environment; only allowlisted names survive.
    *home* is the private driver home (``<data-home>/computer-use/home``); it
    becomes both ``CUA_DRIVER_RS_HOME`` and ``CUA_HOME``. *case_insensitive*
    defaults to the host's convention (Windows environment names are not
    case-sensitive, so ``Path`` must match ``PATH`` there).
    """
    if case_insensitive is None:
        case_insensitive = os.name == "nt"
    env = {k: v for k, v in base.items() if _allowed(k, case_insensitive=case_insensitive)}
    env.update(FORCED_ENV)
    private = str(Path(home))
    env[HOME_ENV] = private
    env[SHARED_HOME_ENV] = private
    return env


__all__ = [
    "ALLOWED_NAMES",
    "ALLOWED_PREFIXES",
    "FORCED_ENV",
    "HOME_ENV",
    "SHARED_HOME_ENV",
    "driver_env",
]
