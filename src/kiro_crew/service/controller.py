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
            profile = linux.install()
        except linux.ServiceInstallError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        print("✅ kirocrew service installed and started.")
        print(f"   unit: {linux.UNIT_PATH}")
        # Reported here, but performed inside linux.install() before the unit is
        # started — the directive only applies at service start. Deliberately
        # non-fatal: a failure warns and leaves the service running.
        if profile.message:
            print(f"   {'⚠️ ' if not profile.ok else ''}{profile.message}")
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
        # uninstall() needs root to remove the root-owned unit, so it can raise
        # ServiceInstallError on a non-root host without sudo. Catch it and exit
        # non-zero with the reason rather than letting a traceback escape (and
        # leaving the service installed).
        try:
            linux.uninstall()
            # Whatever removes the service removes the grant, so a host is left
            # as it was found rather than carrying an orphaned userns permission.
            profile = linux.remove_apparmor_profile()
        except linux.ServiceInstallError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        print("✅ kirocrew service stopped and removed.")
        if profile.message:
            print(f"   {'⚠️ ' if not profile.ok else ''}{profile.message}")
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


def install_launcher_profile(exec_path: str | None = None) -> int:
    """Attach the userns AppArmor profile to a directly launched app.

    Linux-only by nature: the whole feature exists for one Ubuntu kernel
    restriction. On macOS and elsewhere this is a clean no-op with an explanation
    rather than an error, because the same desktop app ships everywhere and must
    not present a broken command to users who do not need it.
    """
    if current_platform() != Platform.SYSTEMD:
        print(
            "ℹ️  The AppArmor sandbox profile is Linux-only — this host does not "
            "restrict unprivileged user namespaces, so nothing is needed."
        )
        return 0
    outcome = linux.install_launcher_profile(exec_path)
    if outcome.message:
        print(f"{'⚠️  ' if not outcome.ok else '✅ '}{outcome.message}")
    return 0 if outcome.ok else 1


def remove_launcher_profile() -> int:
    """Unload and delete the launcher profile. Idempotent."""
    if current_platform() != Platform.SYSTEMD:
        print("ℹ️  Nothing to remove — the AppArmor sandbox profile is Linux-only.")
        return 0
    outcome = linux.remove_launcher_profile()
    print(
        f"{'⚠️  ' if not outcome.ok else '✅ '}{outcome.message}"
        if outcome.message
        else "✅ No AppArmor sandbox profile was installed."
    )
    return 0 if outcome.ok else 1


def sandbox_profile_status(exec_path: str | None = None) -> int:
    """Report whether THIS launch is covered by the launcher profile.

    Exit code is the answer, so a script can gate on it: 0 when the sandbox can
    be built (covered, or a host that never needed the profile), 1 when it cannot.
    """
    if current_platform() != Platform.SYSTEMD:
        print("✅ This platform does not restrict unprivileged user namespaces.")
        return 0
    from kiro_crew.service import apparmor

    ok, detail = apparmor.launcher_status(exec_path)
    print(f"{'✅ ' if ok else '❌ '}{detail}")
    return 0 if ok else 1


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
            return linux.restart()
        return False
    if plat == Platform.LAUNCHD:
        if macos.is_active():
            return macos.restart()
        return False
    return False
