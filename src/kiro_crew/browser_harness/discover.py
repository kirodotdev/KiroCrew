"""Which browser the agent drives on this host.

The order is fixed and the reasons are the person's: their own Chrome carries their
logins and is the one they can watch and take over; Edge is the same browser under
another name on a Windows machine without Chrome; Kiro's own Chromium, headless, is
for a host with no display, a gateway reached from another machine, or a Chrome whose
``RemoteDebuggingAllowed`` policy is off. A snap-packaged Chromium is skipped because
snap confinement breaks the debugging port.

Every probe is a ``stat``, a directory listing or a policy-file read, never a launch.
A probe that cannot run answers "not found" rather than raising: the caller is a
status line, and a wrong guess there costs one Settings click while an exception
costs the page.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

BrowserKind = Literal["user_chrome", "edge", "kiro_chromium"]

#: ``browser.choice`` values (the Settings contract). ``auto`` is the order above.
CHOICE_AUTO = "auto"
CHOICE_USER_CHROME = "user_chrome"
CHOICE_KIRO_CHROMIUM = "kiro_chromium"
CHOICE_HEADLESS = "headless"
CHOICES: tuple[str, ...] = (CHOICE_AUTO, CHOICE_USER_CHROME, CHOICE_KIRO_CHROMIUM, CHOICE_HEADLESS)

#: Chrome's user-data directories, relative to the home (or ``%LOCALAPPDATA%`` on
#: Windows), per platform key. The debugging port file lives in the first one that
#: exists.
_CHROME_PROFILES: Mapping[str, tuple[str, ...]] = {
    "darwin": ("Library/Application Support/Google/Chrome",),
    "linux": (".config/google-chrome", ".config/chromium"),
    "win32": ("Google/Chrome/User Data",),
}
_EDGE_PROFILES: Mapping[str, tuple[str, ...]] = {
    "darwin": ("Library/Application Support/Microsoft Edge",),
    "linux": (".config/microsoft-edge",),
    "win32": ("Microsoft/Edge/User Data",),
}

#: Where a branded Chrome binary is looked for when ``PATH`` does not name one.
_CHROME_MAC_APP = "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_EDGE_MAC_APP = "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
_CHROME_LINUX_BINS: tuple[str, ...] = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)
#: Under :data:`_SYSTEM_ROOT`, so a test can point the lookups at a fake root.
_CHROME_LINUX_PATHS: tuple[Path, ...] = (Path("opt", "google", "chrome", "chrome"),)
_EDGE_LINUX_BINS: tuple[str, ...] = ("microsoft-edge", "microsoft-edge-stable")
_CHROME_WIN_TAIL = Path("Google") / "Chrome" / "Application" / "chrome.exe"
_EDGE_WIN_TAIL = Path("Microsoft") / "Edge" / "Application" / "msedge.exe"
_WIN_INSTALL_ROOTS: tuple[str, ...] = ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")

#: Where system-wide installs are looked for. A module seam so a test can point it at
#: a temporary tree instead of reading whatever the test host has installed.
_SYSTEM_ROOT = Path("/")

#: Snap's binary prefix: a Chromium resolved from here is skipped.
_SNAP_PREFIX = "/snap/"

#: Chrome's enterprise policy that turns the debugging port off.
_POLICY_KEY = "RemoteDebuggingAllowed"
_LINUX_POLICY_DIRS: tuple[Path, ...] = (
    Path("etc", "opt", "chrome", "policies", "managed"),
    Path("etc", "chromium", "policies", "managed"),
)
_MAC_MANAGED_PLIST = "/Library/Managed Preferences/com.google.Chrome.plist"
_WIN_POLICY_KEY = r"SOFTWARE\Policies\Google\Chrome"

#: The display variables a Linux desktop session carries. Absent both, there is no
#: window to show a headed browser in.
_DISPLAY_ENVS: tuple[str, ...] = ("DISPLAY", "WAYLAND_DISPLAY")


@dataclass(frozen=True)
class BrowserChoice:
    """The browser the supervisor will connect to or launch."""

    kind: BrowserKind
    executable: Path | None
    headless: bool
    user_data_dir: Path | None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "headless": self.headless}


@dataclass(frozen=True)
class HostFacts:
    """What the order needs to know about this host; probed once, injectable in tests."""

    chrome: Path | None = None
    chrome_profile: Path | None = None
    edge: Path | None = None
    edge_profile: Path | None = None
    has_display: bool = True
    policy_off: bool = False
    chrome_is_snap: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def platform_key(system: str | None = None) -> str:
    """``darwin`` / ``linux`` / ``win32`` for *system* (``sys.platform`` by default)."""
    system = system or sys.platform
    if system.startswith("linux"):
        return "linux"
    return system


def probe_host(
    platform: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> HostFacts:
    """Read the host facts the order needs. Never raises."""
    key = platform_key(platform)
    home = home if home is not None else Path.home()
    env = env if env is not None else os.environ
    chrome = _find_chrome(key, home, env)
    return HostFacts(
        chrome=chrome,
        chrome_profile=_first_existing(_profile_roots(_CHROME_PROFILES, key, home, env)),
        edge=_find_edge(key, home, env),
        edge_profile=_first_existing(_profile_roots(_EDGE_PROFILES, key, home, env)),
        has_display=_has_display(key, env),
        policy_off=_policy_off(key, env),
        chrome_is_snap=chrome is not None and str(chrome).startswith(_SNAP_PREFIX),
    )


def choose_browser(
    cfg: Mapping[str, Any] | None,
    platform: str | None = None,
    *,
    facts: HostFacts | None = None,
    kiro_chromium: Path | None = None,
    kiro_profile: Path | None = None,
) -> BrowserChoice:
    """Pick the browser for this host.

    *cfg* is the ``browser`` config section (only ``choice`` is read; absent means
    ``auto``). *kiro_chromium* and *kiro_profile* are the fallback's executable and
    user-data directory, passed in so this module never knows the data home.
    """
    key = platform_key(platform)
    facts = facts if facts is not None else probe_host(platform)
    choice = str((cfg or {}).get("choice") or CHOICE_AUTO)
    if choice not in CHOICES:
        choice = CHOICE_AUTO
    kiro_headless = BrowserChoice("kiro_chromium", kiro_chromium, True, kiro_profile)
    if choice == CHOICE_HEADLESS:
        return kiro_headless
    if choice == CHOICE_KIRO_CHROMIUM:
        return BrowserChoice("kiro_chromium", kiro_chromium, not facts.has_display, kiro_profile)
    if not facts.has_display or facts.policy_off:
        return kiro_headless
    if facts.chrome is not None and not facts.chrome_is_snap:
        return BrowserChoice("user_chrome", facts.chrome, False, facts.chrome_profile)
    if choice == CHOICE_USER_CHROME:
        # Asked for Chrome, none found: the fallback is the only thing that works.
        return kiro_headless
    if key == "win32" and facts.edge is not None:
        return BrowserChoice("edge", facts.edge, False, facts.edge_profile)
    return kiro_headless


# ── probes ──


def _profile_roots(
    table: Mapping[str, tuple[str, ...]], key: str, home: Path, env: Mapping[str, str]
) -> list[Path]:
    if key == "win32":
        local = env.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else home / "AppData" / "Local"
    else:
        base = home
    return [base / rel for rel in table.get(key, ())]


def _first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        try:
            if path.is_dir():
                return path
        except OSError:
            continue
    return None


def _find_chrome(key: str, home: Path, env: Mapping[str, str]) -> Path | None:
    if key == "darwin":
        return _first_file([_SYSTEM_ROOT / _CHROME_MAC_APP, home / _CHROME_MAC_APP])
    if key == "win32":
        return _first_file(_windows_candidates(_CHROME_WIN_TAIL, env))
    if key == "linux":
        for name in _CHROME_LINUX_BINS:
            found = shutil.which(name)
            if found:
                return Path(found)
        return _first_file([_SYSTEM_ROOT / p for p in _CHROME_LINUX_PATHS])
    return None


def _find_edge(key: str, home: Path, env: Mapping[str, str]) -> Path | None:
    if key == "darwin":
        return _first_file([_SYSTEM_ROOT / _EDGE_MAC_APP, home / _EDGE_MAC_APP])
    if key == "win32":
        return _first_file(_windows_candidates(_EDGE_WIN_TAIL, env))
    if key == "linux":
        for name in _EDGE_LINUX_BINS:
            found = shutil.which(name)
            if found:
                return Path(found)
    return None


def _windows_candidates(tail: Path, env: Mapping[str, str]) -> list[Path]:
    out: list[Path] = []
    for root_var in _WIN_INSTALL_ROOTS:
        root = env.get(root_var, "").strip()
        if root:
            out.append(Path(root) / tail)
    return out


def _first_file(paths: list[Path]) -> Path | None:
    for path in paths:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _has_display(key: str, env: Mapping[str, str]) -> bool:
    if key != "linux":
        return True
    return any(env.get(name, "").strip() for name in _DISPLAY_ENVS)


def _policy_off(key: str, env: Mapping[str, str]) -> bool:
    """Whether Chrome's ``RemoteDebuggingAllowed`` policy is set to false here."""
    try:
        if key == "linux":
            return _linux_policy_off()
        if key == "darwin":
            return _mac_policy_off()
        if key == "win32":
            return _windows_policy_off()
    except Exception:  # noqa: BLE001 - a policy read that fails is "not off"
        return False
    return False


def _linux_policy_off() -> bool:
    for policy_dir in _LINUX_POLICY_DIRS:
        root = _SYSTEM_ROOT / policy_dir
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get(_POLICY_KEY) is False:
                return True
    return False


def _mac_policy_off() -> bool:
    import plistlib

    path = Path(_MAC_MANAGED_PLIST)
    if not path.is_file():
        return False
    with path.open("rb") as fh:
        data = plistlib.load(fh)
    return isinstance(data, dict) and data.get(_POLICY_KEY) is False


def _windows_policy_off() -> bool:
    # Typed ``Any``: mypy runs with ``--platform linux``, where ``winreg``'s attributes
    # are genuinely absent (the same shape ``computer_use.launch_windows._winreg`` uses).
    import winreg

    reg: Any = winreg
    for hive in (reg.HKEY_LOCAL_MACHINE, reg.HKEY_CURRENT_USER):
        try:
            with reg.OpenKey(hive, _WIN_POLICY_KEY) as handle:
                value, _kind = reg.QueryValueEx(handle, _POLICY_KEY)
        except OSError:
            continue
        if value == 0:
            return True
    return False
