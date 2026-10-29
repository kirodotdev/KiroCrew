"""Laptop detection and keep-awake for a shared Crew.

Teammates lose a Crew the moment its host sleeps, which on a laptop is often.
Two things follow:

* :func:`detect_laptop` says whether this host runs on a battery, so the
  first-invite warning can say so. Linux reads ``/sys/class/power_supply``
  (a ``Battery`` supply that is not a peripheral's), macOS asks
  ``/usr/bin/pmset -g batt`` for an ``InternalBattery``, Windows reads
  ``GetSystemPowerStatus``. Anything else is "unknown", never "laptop".
* :func:`shared_use_wants_awake` is the gateway's third keep-awake reason
  (``server_runtime.prevent_sleep``): while a collaborator has a dashboard
  socket open or work charged to a collaborator is queued or running, the host
  is kept awake through :class:`kiro_crew.power.SleepInhibitor`
  (``caffeinate -i -w <gateway pid>`` on macOS, ``systemd-inhibit`` on Linux,
  ``SetThreadExecutionState`` on Windows) and released when that stops.
  :func:`keep_awake_capability` reports whether that backend exists here.

Nothing is installed. Every helper is resolved from a fixed system path, never
from ``PATH``, and every failure reads as "not a laptop" or "unavailable".
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

POWER_SUPPLY_ROOT = Path("/sys/class/power_supply")
#: macOS-only; ``_macos_battery`` reads it.
PMSET_PATH = str(PurePosixPath("/", "usr", "bin", "pmset"))

REASON_BATTERY = "battery_present"
REASON_NO_BATTERY = "no_battery"
REASON_UNKNOWN = "unknown"

KEEP_AWAKE_AVAILABLE = "available"
KEEP_AWAKE_UNAVAILABLE = "unavailable"
REASON_NO_BACKEND = "no_backend"
REASON_UNSUPPORTED = "unsupported_platform"


@dataclass(frozen=True)
class LaptopInfo:
    laptop: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"laptop": self.laptop, "reason": self.reason}


def _linux_battery(root: Path) -> Optional[bool]:
    try:
        supplies = list(root.iterdir())
    except OSError:
        return None
    for supply in supplies:
        try:
            kind = (supply / "type").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if kind != "Battery":
            continue
        try:
            scope = (supply / "scope").read_text(encoding="utf-8").strip()
        except OSError:
            scope = ""
        if scope == "Device":
            # A wireless mouse or keyboard reports its own battery here.
            continue
        return True
    return False


def _macos_battery(run: Callable[..., Any]) -> Optional[bool]:
    if not (os.path.isfile(PMSET_PATH) and os.access(PMSET_PATH, os.X_OK)):
        return None
    try:
        done = run(
            [PMSET_PATH, "-g", "batt"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = getattr(done, "stdout", "") or ""
    return "InternalBattery" in out


def _windows_battery() -> Optional[bool]:
    try:
        import ctypes

        class _Status(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]

        status = _Status()
        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
    except Exception:  # noqa: BLE001 -- an unreadable status is unknown
        return None
    if status.BatteryFlag == 255:
        return None
    return not bool(status.BatteryFlag & 128)


def detect_laptop(
    *,
    system: Optional[str] = None,
    power_supply_root: Path = POWER_SUPPLY_ROOT,
    run: Callable[..., Any] = subprocess.run,
    windows_probe: Callable[[], Optional[bool]] = _windows_battery,
) -> LaptopInfo:
    """Whether this host runs on a battery. Blocking; never raises."""
    name = system or _system()
    try:
        if name == "linux":
            found = _linux_battery(power_supply_root)
        elif name == "darwin":
            found = _macos_battery(run)
        elif name == "windows":
            found = windows_probe()
        else:
            found = None
    except Exception:  # noqa: BLE001 -- detection is a hint, never a failure
        logger.debug("laptop detection failed", exc_info=True)
        found = None
    if found is None:
        return LaptopInfo(False, REASON_UNKNOWN)
    return LaptopInfo(found, REASON_BATTERY if found else REASON_NO_BATTERY)


def _system() -> str:
    if platform_compat.IS_MACOS:
        return "darwin"
    if platform_compat.IS_WINDOWS:
        return "windows"
    if platform_compat.IS_LINUX:
        return "linux"
    return "other"


def keep_awake_capability(*, system: Optional[str] = None) -> dict[str, str]:
    """``{state, reason, backend}``: whether this host can be kept awake. Blocking."""
    from kiro_crew import power

    name = system or _system()
    if name == "darwin":
        found = power._resolve_trusted(power._CAFFEINATE_PATHS)
        backend = "caffeinate"
    elif name == "linux":
        found = power._resolve_trusted(power._SYSTEMD_INHIBIT_PATHS) and power._resolve_trusted(
            power._SLEEP_PATHS
        )
        backend = "systemd-inhibit"
    elif name == "windows":
        return {"state": KEEP_AWAKE_AVAILABLE, "reason": "", "backend": "SetThreadExecutionState"}
    else:
        return {"state": KEEP_AWAKE_UNAVAILABLE, "reason": REASON_UNSUPPORTED, "backend": "none"}
    if not found:
        return {"state": KEEP_AWAKE_UNAVAILABLE, "reason": REASON_NO_BACKEND, "backend": backend}
    return {"state": KEEP_AWAKE_AVAILABLE, "reason": "", "backend": backend}


# ── what the gateway found at start ──

_REPORTED: dict[str, Any] = {}


def note_host() -> dict[str, Any]:
    """Detect once (at gateway start, off the loop) and keep the answer. Blocking."""
    laptop = detect_laptop()
    report = {**keep_awake_capability(), "laptop": laptop.laptop, "laptop_reason": laptop.reason}
    _REPORTED.clear()
    _REPORTED.update(report)
    return dict(report)


async def probe_after_start() -> None:
    """Run the informational host probes once the dashboard already accepts requests.

    The proxy-proof scan and the laptop and keep-awake detection only fill
    capability rows, so they run off the boot path; the rows say
    ``not_checked`` until this has answered.
    """
    from kiro_crew.dashboard.tailnet_proxy_proof import proof_support
    from kiro_crew.shared_crew import access

    try:
        access.note_proof_gap(await asyncio.to_thread(proof_support))
    except Exception:  # noqa: BLE001 -- the row keeps saying not_checked
        logger.warning("shared crew: the proxy proof probe failed", exc_info=True)
    try:
        await asyncio.to_thread(note_host)
    except Exception:  # noqa: BLE001 -- the row keeps saying not_checked
        logger.warning("shared crew: the host power probe failed", exc_info=True)


def reported() -> dict[str, Any]:
    """The capabilities' ``keep_awake`` row: no I/O, ``unavailable`` before :func:`note_host`."""
    if not _REPORTED:
        return {
            "state": KEEP_AWAKE_UNAVAILABLE,
            "reason": "not_checked",
            "backend": "none",
            "laptop": False,
            "laptop_reason": REASON_UNKNOWN,
        }
    return dict(_REPORTED)


# ── when to keep awake ──


def _owner_keys() -> tuple[str, str]:
    from kiro_crew.dashboard import tailnet_live_allowlist

    snap = tailnet_live_allowlist.snapshot()
    if snap is None:
        return "", ""
    return snap.owner_login.strip().lower(), snap.owner_user_id


def _is_collaborator(principal: Any, owner_login: str, owner_id: str) -> bool:
    from kiro_crew.shared_crew.contracts import PrincipalKind
    from kiro_crew.shared_crew.identity import PROVIDER_TAILSCALE

    if getattr(principal, "provider", "") != PROVIDER_TAILSCALE:
        return False
    if getattr(principal, "kind", None) not in (PrincipalKind.HUMAN, PrincipalKind.MACHINE):
        return False
    subject = str(getattr(principal, "subject", "") or "")
    login = str(getattr(getattr(principal, "display", None), "login", "") or "").lower()
    return not (owner_login and (subject == owner_login or login == owner_login))


def collaborator_connected(state: Any) -> bool:
    """Whether a collaborator (not the owner) has a dashboard socket open."""
    from kiro_crew.shared_crew.routes_invites import WS_ACTOR_KEY

    owner_login, owner_id = _owner_keys()
    for ws in list(getattr(state, "_ws_clients", None) or ()):
        try:
            if getattr(ws, "closed", False):
                continue
            actor = ws.get(WS_ACTOR_KEY)
            if _is_collaborator(getattr(actor, "principal", None), owner_login, owner_id):
                return True
        except Exception:  # noqa: BLE001 -- one odd socket never decides
            continue
    return False


def collaborator_work_active() -> bool:
    """Whether work charged to a collaborator is queued or running."""
    from kiro_crew.dashboard import tailnet_live_allowlist
    from kiro_crew.shared_crew.access_set import principal_ids_for
    from kiro_crew.shared_crew.admission import TERMINAL
    from kiro_crew.shared_crew.admission_gate import installed_service
    from kiro_crew.shared_crew.turn_gate import unadmitted_slots

    snap = tailnet_live_allowlist.snapshot()
    if snap is None:
        return False
    owner = snap.owner_login.strip().lower()
    ids: set[str] = set()
    for login in (snap.member_logins | snap.approved_logins) - {owner}:
        ids |= principal_ids_for(login, frozenset())
    for uid in snap.approved_user_ids - {snap.owner_user_id}:
        ids |= principal_ids_for("", frozenset({uid}))
    if snap.network_trust:
        # Tailscale alone decides: anyone whois named on the direct listener.
        for login in tailnet_live_allowlist.seen_logins() - {owner}:
            ids |= principal_ids_for(login, frozenset())
    if not ids:
        return False
    # A turn that runs outside fair admission (admission off) is not in the service.
    if unadmitted_slots(ids):
        return True
    service = installed_service()
    if service is None:
        return False
    return any(
        live.work.budget_key in ids and live.work.state not in TERMINAL
        for live in list(service._live.values())
    )


def shared_use_wants_awake(state: Any) -> bool:
    """The prevent-sleep poll's Shared Crew reason. Never raises."""
    try:
        return collaborator_connected(state) or collaborator_work_active()
    except Exception:  # noqa: BLE001 -- fail toward normal sleep
        logger.debug("shared crew keep-awake check failed", exc_info=True)
        return False


__all__ = [
    "LaptopInfo",
    "collaborator_connected",
    "collaborator_work_active",
    "detect_laptop",
    "keep_awake_capability",
    "note_host",
    "probe_after_start",
    "reported",
    "shared_use_wants_awake",
]
