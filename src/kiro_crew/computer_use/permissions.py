"""macOS TCC permission probing — **ADVISORY ONLY, NEVER A GATE**.

This module exists to put a helpful hint in the Settings panel. It must never
decide whether an action proceeds, and no caller may treat a ``missing`` result as
"unavailable". That is not caution, it is a reproduced fact: during live probing
**both** grants reported ``missing`` while a full-fidelity window capture and a
complete accessibility tree came back successfully.

The reason is how macOS attributes a TCC grant. The grant follows the
**responsible parent** of the process tree, not the process that asks. The
gateway spawns kiro-cli, which spawns the MCP sidecar, so the grant belongs to
whatever launched the tree — ``KiroCrew.app`` when packaged, ``Terminal.app`` for a
dev ``kirocrew gateway``, an IDE if launched from one. A probe inspects only its
own process's grant and therefore reports ``missing`` for a tree that is fully
authorized. :attr:`PermissionProbe.responsible_hint` names the process the user
should actually grant, because "grant Accessibility to KiroCrew" is unactionable
advice when the thing holding the grant is their terminal.

Two related hazards this module is careful about:

* ``CGRequestScreenCaptureAccess`` is **never** called and is not even bound in
  :mod:`macos_ffi`. It pops a system dialog from the calling process, which from a
  background sidecar is an unexplained prompt the operator cannot attribute to
  anything they did. Only the ``Preflight`` variant is used.
* Ad-hoc re-signing anything in the process chain **voids an existing grant**
  (observed permanently breaking Accessibility 3/3 times on a re-signed bundle).
  ``packaging/resign-macos-libs.sh`` re-signs native libs, so a user whose
  permissions "worked yesterday" may have been broken by an update rather than by
  a setting. The hint text has to leave room for that.
"""

from __future__ import annotations

import logging
import os
import sys

from kiro_crew import platform_compat
from kiro_crew.computer_use import macos_ffi
from kiro_crew.computer_use.types import (
    PERMISSION_GRANTED,
    PERMISSION_MISSING,
    PERMISSION_UNKNOWN,
    PERMISSION_UNSUPPORTED,
    PermissionProbe,
)

logger = logging.getLogger(__name__)

# Advisory copy for the Settings rows. Deliberately says "not detected" rather
# than "missing" or "denied": the probe genuinely cannot distinguish "not granted"
# from "granted to an ancestor we cannot see", and overclaiming here would send
# users to System Settings to fix something that is not broken.
HINT_RESPONSIBLE = (
    "macOS attributes Accessibility and Screen Recording grants to the process "
    "that launched KiroCrew, not to KiroCrew itself. Grant them to {process} — "
    "and note that 'not detected' does not always mean unavailable."
)
HINT_UNSUPPORTED = "computer use requires macOS; there is nothing to grant on this platform."
HINT_UNKNOWN = (
    "the permission state could not be read. Computer use may still work: grants "
    "follow the launching process, which a probe cannot inspect."
)

# System Settings deep links, for the Settings panel's grant buttons.
SETTINGS_URL_ACCESSIBILITY = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)
SETTINGS_URL_SCREEN_RECORDING = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
)

# Payload keys, so the handler, the CLI and the frontend share one spelling.
KEY_ACCESSIBILITY = "accessibility"
KEY_SCREEN_RECORDING = "screen_recording"
KEY_RESPONSIBLE_HINT = "responsible_hint"

# Fallback label when the responsible process cannot be named.
_UNKNOWN_PROCESS = "the application that launched KiroCrew"


def probe() -> PermissionProbe:
    """Probe the two TCC grants. ADVISORY. Never raises.

    Returns :data:`PERMISSION_UNSUPPORTED` off macOS, :data:`PERMISSION_UNKNOWN`
    when the frameworks cannot be loaded or a probe call fails, and otherwise
    ``granted``/``missing`` — with the standing caveat that ``missing`` is not
    authoritative.
    """
    if not platform_compat.IS_MACOS:
        return PermissionProbe(
            accessibility=PERMISSION_UNSUPPORTED,
            screen_recording=PERMISSION_UNSUPPORTED,
            responsible_hint=HINT_UNSUPPORTED,
        )
    hint = HINT_RESPONSIBLE.format(process=responsible_process_name())
    try:
        accessibility = (
            PERMISSION_GRANTED if macos_ffi.ax_is_process_trusted() else PERMISSION_MISSING
        )
    except Exception:
        logger.debug("AXIsProcessTrusted probe failed", exc_info=True)
        accessibility = PERMISSION_UNKNOWN
        hint = HINT_UNKNOWN
    try:
        screen = PERMISSION_GRANTED if macos_ffi.preflight_screen_capture() else PERMISSION_MISSING
    except Exception:
        logger.debug("CGPreflightScreenCaptureAccess probe failed", exc_info=True)
        screen = PERMISSION_UNKNOWN
        hint = HINT_UNKNOWN
    return PermissionProbe(
        accessibility=accessibility, screen_recording=screen, responsible_hint=hint
    )


def probe_dict() -> dict[str, str]:
    """The probe as a plain dict, for the dashboard payload and ``doctor --json``.

    A separate function rather than ``dataclasses.asdict`` so the wire key names
    are stated once, here, and cannot drift when a field is renamed.
    """
    result = probe()
    return {
        KEY_ACCESSIBILITY: result.accessibility,
        KEY_SCREEN_RECORDING: result.screen_recording,
        KEY_RESPONSIBLE_HINT: result.responsible_hint,
    }


def responsible_process_name() -> str:
    """Best-effort name of the process that actually holds the TCC grants.

    Walks up the parent chain (through :mod:`platform_compat`, never a raw
    ``/proc`` read or a ``ps`` shell-out) to the outermost ancestor we can still
    identify, and reports its bundle display name when it has one. That ancestor
    is what macOS considers responsible for the tree, so it is the process the user
    must grant.

    Best-effort by design: the walk is bounded, every step tolerates failure, and
    an unresolvable chain yields a generic phrase. A wrong hint is a cosmetic
    problem; raising here would break the Settings page over a diagnostic.
    """
    # Late import to avoid a module-scope cycle: apps_macos imports macos_ffi,
    # which is fine, but permissions is imported by the driver that apps_macos
    # also feeds, and keeping this local documents that the dependency is
    # diagnostic-only.
    from kiro_crew.computer_use import apps_macos

    pid = os.getpid()
    best = ""
    # Bounded ancestor walk. 8 levels is far more than the real chain
    # (launcher -> gateway -> kiro-cli -> sidecar) and terminates regardless of
    # what a hostile or unusual process tree reports.
    for _ in range(8):
        identity = apps_macos.resolve_identity(pid)
        if identity.display_name:
            best = identity.display_name
        try:
            parent = platform_compat.get_ppid(pid)
        except Exception:
            break
        if not parent or parent <= 1 or parent == pid:
            break
        pid = parent
    if best:
        return best
    # No bundle anywhere in the chain: a bare python/venv launch. Naming the
    # executable is still more useful than nothing.
    return os.path.basename(sys.executable) or _UNKNOWN_PROCESS


def settings_urls() -> dict[str, str]:
    """System Settings deep links for the two grants (macOS only, else empty)."""
    if not platform_compat.IS_MACOS:
        return {}
    return {
        KEY_ACCESSIBILITY: SETTINGS_URL_ACCESSIBILITY,
        KEY_SCREEN_RECORDING: SETTINGS_URL_SCREEN_RECORDING,
    }


__all__ = [
    "HINT_RESPONSIBLE",
    "HINT_UNKNOWN",
    "HINT_UNSUPPORTED",
    "KEY_ACCESSIBILITY",
    "KEY_RESPONSIBLE_HINT",
    "KEY_SCREEN_RECORDING",
    "SETTINGS_URL_ACCESSIBILITY",
    "SETTINGS_URL_SCREEN_RECORDING",
    "probe",
    "probe_dict",
    "responsible_process_name",
    "settings_urls",
]
