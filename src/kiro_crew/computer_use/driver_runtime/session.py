"""Which desktop session the gateway is in, and what the driver can honestly do there.

Read from the environment and a few cheap OS queries; nothing here starts the
driver. The driver's own ``check_permissions`` / ``health_report`` (asked by
:mod:`kiro_crew.computer_use.setup` once it runs) are the authority on whether a
session really works; this module decides what to CLAIM before that, and never
claims more than the session can deliver:

* **X11** (or an Xvfb/Xorg display): the full set. Element actions and value
  writes go through AT-SPI with no focus change. Raw background keyboard and
  pointer input (``press_key``, coordinate gestures) need a virtual input
  device, so they are reported unavailable unless ``/dev/uinput`` is writable;
  foreground delivery (named by the model) still works.
* **Wayland**: the driver is run on XWayland only (its native Wayland backend is
  not enabled), so only X11 clients are visible and drivable. A Wayland session
  without an X server refuses. Per compositor notes are reported as limitations.
* **Windows**: an interactive desktop session is required; Session 0 (a service,
  or a process started over SSH) cannot see or drive the desktop.
* **macOS**: an Aqua (logged-in console) session is required.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat

KIND_X11 = "x11"
KIND_WAYLAND = "wayland"
KIND_WINDOWS_INTERACTIVE = "windows_interactive"
KIND_SESSION0 = "session0"
KIND_AQUA = "aqua"
KIND_NONE = "none"

COMPOSITOR_GNOME = "gnome"
COMPOSITOR_KDE = "kde"
COMPOSITOR_SWAY = "sway"
COMPOSITOR_HYPRLAND = "hyprland"
COMPOSITOR_NIRI = "niri"
COMPOSITOR_WESTON = "weston"
COMPOSITOR_OTHER = "other"

ATSPI_REACHABLE = "reachable"
ATSPI_MISSING = "missing"
ATSPI_NOT_APPLICABLE = "not_applicable"

_COMPOSITOR_LIMITS: dict[str, str] = {
    COMPOSITOR_GNOME: (
        "GNOME on Wayland: only apps running under XWayland are visible; native "
        "Wayland windows are not listed or captured"
    ),
    COMPOSITOR_KDE: (
        "KDE Plasma on Wayland: only apps running under XWayland are visible; native "
        "Wayland windows are not listed or captured"
    ),
    COMPOSITOR_SWAY: (
        "Sway / wlroots: only apps running under XWayland are visible; native Wayland "
        "windows are not listed or captured"
    ),
    COMPOSITOR_HYPRLAND: (
        "Hyprland: only apps running under XWayland are visible; native Wayland "
        "windows are not listed or captured"
    ),
    COMPOSITOR_NIRI: (
        "niri: only apps running under XWayland are visible; native Wayland windows "
        "are not listed or captured"
    ),
    COMPOSITOR_WESTON: (
        "Weston: only apps running under XWayland are visible; native Wayland windows "
        "are not listed or captured"
    ),
    COMPOSITOR_OTHER: (
        "this Wayland compositor: only apps running under XWayland are visible; native "
        "Wayland windows are not listed or captured"
    ),
}

LIMIT_NO_UINPUT = (
    "key presses and coordinate clicks or drags in the background need write access to "
    "/dev/uinput; without it they are refused, and only element actions (click by "
    "element_index, type, set value, scroll) run in the background"
)
LIMIT_XWAYLAND_INPUT = (
    "background key presses into XWayland windows are not verified on this compositor"
)


@dataclass(frozen=True)
class SessionInfo:
    kind: str
    detail: str
    supported: bool
    compositor: str = ""
    atspi: str = ATSPI_NOT_APPLICABLE
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail}


def _caps(
    *, tree: bool, shot: bool, background: bool, foreground: bool, window: bool
) -> dict[str, bool]:
    return {
        "observe_tree": tree,
        "screenshot": shot,
        "background_input": background,
        "foreground_input": foreground,
        "window_capture": window,
    }


NO_CAPS = _caps(tree=False, shot=False, background=False, foreground=False, window=False)


def _compositor(env: Mapping[str, str]) -> str:
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return COMPOSITOR_HYPRLAND
    if env.get("SWAYSOCK"):
        return COMPOSITOR_SWAY
    if env.get("NIRI_SOCKET"):
        return COMPOSITOR_NIRI
    desktop = (env.get("XDG_CURRENT_DESKTOP") or env.get("XDG_SESSION_DESKTOP") or "").lower()
    if "gnome" in desktop or "ubuntu" in desktop:
        return COMPOSITOR_GNOME
    if "kde" in desktop or "plasma" in desktop:
        return COMPOSITOR_KDE
    if "sway" in desktop or "wlroots" in desktop:
        return COMPOSITOR_SWAY
    if "hyprland" in desktop:
        return COMPOSITOR_HYPRLAND
    if "niri" in desktop:
        return COMPOSITOR_NIRI
    if "weston" in desktop or (env.get("WAYLAND_DISPLAY") or "").startswith("weston"):
        return COMPOSITOR_WESTON
    return COMPOSITOR_OTHER


def _atspi(env: Mapping[str, str]) -> str:
    if env.get("AT_SPI_BUS_ADDRESS") or env.get("DBUS_SESSION_BUS_ADDRESS"):
        return ATSPI_REACHABLE
    runtime = env.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime, "bus").exists():
        return ATSPI_REACHABLE
    return ATSPI_MISSING


def _uinput_writable() -> bool:
    return os.access("/dev/uinput", os.W_OK)


def detect_linux(
    env: "Mapping[str, str] | None" = None, *, uinput: "bool | None" = None
) -> SessionInfo:
    env = os.environ if env is None else env
    has_uinput = _uinput_writable() if uinput is None else uinput
    display = env.get("DISPLAY", "")
    wayland = env.get("WAYLAND_DISPLAY", "") or (
        env.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    )
    atspi = _atspi(env)
    limits: list[str] = []
    if atspi != ATSPI_REACHABLE:
        limits.append("no session bus was found, so the accessibility tree (AT-SPI) cannot be read")
    if wayland:
        compositor = _compositor(env)
        if not display:
            return SessionInfo(
                kind=KIND_WAYLAND,
                detail=f"Wayland ({compositor}) with no XWayland display",
                supported=False,
                compositor=compositor,
                atspi=atspi,
                capabilities=NO_CAPS,
                limitations=(
                    "this Wayland session has no X server (DISPLAY is unset); the driver "
                    "runs on XWayland only, so nothing can be listed, read or driven",
                ),
            )
        limits.append(_COMPOSITOR_LIMITS.get(compositor, _COMPOSITOR_LIMITS[COMPOSITOR_OTHER]))
        limits.append(LIMIT_XWAYLAND_INPUT)
        if not has_uinput:
            limits.append(LIMIT_NO_UINPUT)
        return SessionInfo(
            kind=KIND_WAYLAND,
            detail=f"Wayland ({compositor}) through XWayland {display}",
            supported=atspi == ATSPI_REACHABLE,
            compositor=compositor,
            atspi=atspi,
            capabilities=_caps(
                tree=atspi == ATSPI_REACHABLE,
                shot=True,
                background=has_uinput,
                foreground=True,
                window=True,
            ),
            limitations=tuple(limits),
        )
    if display:
        if not has_uinput:
            limits.append(LIMIT_NO_UINPUT)
        return SessionInfo(
            kind=KIND_X11,
            detail=f"X11 display {display}",
            supported=atspi == ATSPI_REACHABLE,
            atspi=atspi,
            capabilities=_caps(
                tree=atspi == ATSPI_REACHABLE,
                shot=True,
                background=has_uinput,
                foreground=True,
                window=True,
            ),
            limitations=tuple(limits),
        )
    return SessionInfo(
        kind=KIND_NONE,
        detail="no graphical session (neither DISPLAY nor WAYLAND_DISPLAY is set)",
        supported=False,
        atspi=atspi,
        capabilities=NO_CAPS,
        limitations=("start Kiro Crew from inside a desktop session to use computer use",),
    )


def windows_session_id() -> "int | None":
    """This process's Windows session id, or ``None`` when it cannot be read."""
    if not platform_compat.IS_WINDOWS:
        return None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        session = ctypes.c_ulong(0)
        if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            return None
        return int(session.value)
    except Exception:
        return None


def detect_windows(session_id: "int | None" = None) -> SessionInfo:
    sid = windows_session_id() if session_id is None else session_id
    if sid == 0:
        return SessionInfo(
            kind=KIND_SESSION0,
            detail="Windows Session 0 (a service or a remote shell)",
            supported=False,
            capabilities=NO_CAPS,
            limitations=(
                "Kiro Crew is running in Windows Session 0, which has no desktop; start it "
                "from a signed-in desktop session (not as a service or over SSH)",
            ),
        )
    return SessionInfo(
        kind=KIND_WINDOWS_INTERACTIVE,
        detail=f"interactive Windows session {sid if sid is not None else '?'}",
        supported=True,
        capabilities=_caps(tree=True, shot=True, background=True, foreground=True, window=True),
        limitations=(
            "background clicks on some surfaces (browsers, GTK, WPF text) are refused; "
            "the model must name the foreground method to bring the window forward",
        ),
    )


def detect_macos(console_uid: "int | None" = None) -> SessionInfo:
    try:
        owner = os.stat("/dev/console").st_uid if console_uid is None else console_uid
    except OSError:
        owner = -1
    getuid = getattr(os, "getuid", None)
    # root owns /dev/console only at the login window: no user is signed in.
    mine = owner > 0 and owner == (getuid() if getuid is not None else -2)
    if not mine:
        return SessionInfo(
            kind=KIND_NONE,
            detail="no logged-in macOS desktop session for this user",
            supported=False,
            capabilities=NO_CAPS,
            limitations=("sign in at the Mac's login window, then start Kiro Crew there",),
        )
    return SessionInfo(
        kind=KIND_AQUA,
        detail="macOS desktop session",
        supported=True,
        capabilities=_caps(tree=True, shot=True, background=True, foreground=True, window=True),
    )


def detect() -> SessionInfo:
    """The session for THIS process."""
    if platform_compat.IS_WINDOWS:
        return detect_windows()
    if platform_compat.IS_MACOS:
        return detect_macos()
    if platform_compat.IS_LINUX:
        return detect_linux()
    return SessionInfo(kind=KIND_NONE, detail="unsupported operating system", supported=False)


__all__ = [
    "ATSPI_MISSING",
    "ATSPI_NOT_APPLICABLE",
    "ATSPI_REACHABLE",
    "KIND_AQUA",
    "KIND_NONE",
    "KIND_SESSION0",
    "KIND_WAYLAND",
    "KIND_WINDOWS_INTERACTIVE",
    "KIND_X11",
    "SessionInfo",
    "detect",
    "detect_linux",
    "detect_macos",
    "detect_windows",
    "windows_session_id",
]
