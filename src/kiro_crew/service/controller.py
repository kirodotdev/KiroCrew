"""Platform dispatch for service install/uninstall/status.

CLI entry points should call functions in this module rather than
importing :mod:`kiro_crew.service.linux` or :mod:`kiro_crew.service.macos`
directly. This keeps the dispatch logic in one place and makes the
``UNSUPPORTED`` path produce consistent error output.
"""

from __future__ import annotations

import sys

from kiro_crew.service import linux, macos
from kiro_crew.service.common import Platform, current_platform


def _unsupported_message() -> None:
    print(
        "❌ kirocrew service management is only supported on Linux (systemd)\n"
        "   and macOS (launchd). On other platforms run `kirocrew gateway`\n"
        "   directly or wrap it in tmux/screen yourself.",
        file=sys.stderr,
    )


def install_service() -> int:
    """Install and start the platform service.

    Returns 0 on success, non-zero otherwise. On Linux the install
    prompts for sudo on first use to write
    ``/etc/systemd/system/kirocrew.service`` and to run
    ``systemctl daemon-reload / enable / restart``. The gateway itself
    runs as ``User=$USER`` once started — kirocrew code is never
    invoked under sudo. On macOS no sudo is required. The CLI is
    expected to surface the sudo prompt to a real terminal.
    """
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        try:
            linux.install()
        except linux.ServiceInstallError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        print("✅ kirocrew service installed and started.")
        print(f"   unit: {linux.UNIT_PATH}")
        print()
        print("   Status: kirocrew service status")
        print("   Logs:   kirocrew logs -f")
        print("   Remove: kirocrew service uninstall")
        return 0
    if plat == Platform.LAUNCHD:
        try:
            macos.install()
        except macos.ServiceInstallError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        print("✅ kirocrew service installed and started.")
        print(f"   plist: {macos.PLIST_PATH}")
        print()
        print("   Status: kirocrew service status")
        print(f"   Logs:   tail -f {macos.STDOUT_LOG}")
        print("   Remove: kirocrew service uninstall")
        return 0
    _unsupported_message()
    return 2


def uninstall_service() -> int:
    """Stop and remove the platform service. Idempotent."""
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        linux.uninstall()
        print("✅ kirocrew service stopped and removed.")
        return 0
    if plat == Platform.LAUNCHD:
        macos.uninstall()
        print("✅ kirocrew service stopped and removed.")
        return 0
    _unsupported_message()
    return 2


def service_status() -> int:
    """Print the platform service status. Returns 0 if active, 1 if inactive, 2 if unsupported."""
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        print(linux.status())
        return 0 if linux.is_active() else 1
    if plat == Platform.LAUNCHD:
        print(macos.status())
        return 0 if macos.is_active() else 1
    _unsupported_message()
    return 2


def is_service_active() -> bool:
    """Return True if a kirocrew service is installed and currently running."""
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        return linux.is_active()
    if plat == Platform.LAUNCHD:
        return macos.is_active()
    return False


def stop_service() -> bool:
    """Stop the platform service if active. Returns True if a service was stopped."""
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        if linux.is_active():
            linux.stop()
            return True
        return False
    if plat == Platform.LAUNCHD:
        if macos.is_active():
            macos.stop()
            return True
        return False
    return False


def restart_service() -> bool:
    """Restart the platform service if installed and active.

    Returns True if a service was restarted. Mirrors :func:`stop_service`
    so callers can branch on "was this handled by the service manager?"
    rather than re-doing platform detection. When False, callers fall
    back to a foreground-gateway path (SIGTERM-by-port + detached spawn).
    """
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        if linux.is_active():
            linux.restart()
            return True
        return False
    if plat == Platform.LAUNCHD:
        if macos.is_active():
            macos.restart()
            return True
        return False
    return False
