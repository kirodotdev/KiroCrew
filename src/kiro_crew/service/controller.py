"""Platform dispatch for service install/uninstall/status.

CLI entry points should call functions in this module rather than
importing :mod:`kiro_crew.service.linux` or :mod:`kiro_crew.service.macos`
directly. This keeps the dispatch logic in one place and makes the
``UNSUPPORTED`` path produce consistent error output.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from kiro_crew.service import linux, macos, windows
from kiro_crew.service.common import (
    LAUNCHD_LABEL,
    Platform,
    current_platform,
    headless_auth_warning,
    restart_command_hint,
)

logger = logging.getLogger(__name__)


def _print_headless_auth_warning() -> None:
    """Surface a dropped API-key credential right after a successful install.

    Printed on stdout beside the other post-install lines rather than raised:
    the service IS installed and running at this point, and the gateway will
    still serve — it just cannot see the operator's credential. Silent on a
    login-based or already-configured install.

    Non-fatal by construction, like the AppArmor profile message above it. The
    check resolves the crew home to locate ``.env``, and by the time it runs the
    unit is written and started — so an exception here would print a traceback
    over a successful install and return non-zero for a machine state that is
    actually fine. A diagnostic that cannot fire is strictly better than an
    install that reports failure.
    """
    try:
        warning = headless_auth_warning()
    except Exception:  # noqa: BLE001 - diagnostic must never fail the install
        return
    if warning:
        print(warning)


def installed_unit_path() -> "Path | None":
    """Return the installed service definition's path, or None if not installed.

    Callers outside this module should not have to know which platform stores
    its definition where, nor which of the two modules to import. Presence of
    the file is the signal that a service exists to inherit (or drop) an
    environment: a host running ``kirocrew gateway`` in the foreground has none,
    and inherits the invoking shell instead.
    """
    plat = current_platform()
    if plat == Platform.SYSTEMD and linux.UNIT_PATH.is_file():
        return linux.UNIT_PATH
    if plat == Platform.LAUNCHD and macos.PLIST_PATH.is_file():
        return macos.PLIST_PATH
    return None


def installed_service_has_managed_marker() -> "bool | None":
    """Report whether an installed definition selects managed-service policy.

    ``None`` means no service definition is installed on this platform. ``False``
    includes an unreadable or malformed definition: doctor must tell the operator
    to regenerate it rather than silently claim the wider watchdog budget applies.
    """
    path = installed_unit_path()
    if path is None:
        return None
    plat = current_platform()
    try:
        if plat == Platform.SYSTEMD:
            expected = 'Environment="KIROCREW_SERVICE_MANAGED=1"'
            lines = path.read_text(encoding="utf-8").splitlines()
            return any(line.strip() == expected for line in lines)
        if plat == Platform.LAUNCHD:
            # Reuse the launchd reader so malformed XML (which plistlib exposes
            # as an ExpatError) fails closed just like every other service
            # inspection path instead of crashing `kirocrew doctor`.
            payload = macos._plist_payload(path)
            if payload is None:
                return False
            environment = payload.get("EnvironmentVariables")
            return (
                isinstance(environment, dict) and environment.get("KIROCREW_SERVICE_MANAGED") == "1"
            )
    except (OSError, ValueError):
        return False
    return None


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
            print(f"   {'' if profile.ok else '⚠️ '}{profile.message}")
        _print_headless_auth_warning()
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
        _print_headless_auth_warning()
        print()
        print("   Status: kirocrew service status")
        print(f"   Logs:   tail -f {macos.STDOUT_LOG}")
        print("   Remove: kirocrew service uninstall")
        return 0
    if plat == Platform.SCHTASKS:
        try:
            definition = windows.install()
            # Registering a logon-triggered task does not run it, so a fresh
            # install would otherwise leave the operator without a gateway
            # until they next signed in.
            windows.start()
        except windows.ServiceInstallError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        print("✅ kirocrew service installed and started.")
        print(f"   task: {windows.TASK_NAME}")
        print(f"   definition: {definition}")
        _print_headless_auth_warning()
        print()
        print("   Status: kirocrew service status")
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
            print(f"   {'' if profile.ok else '⚠️ '}{profile.message}")
        return 0
    if plat == Platform.LAUNCHD:
        macos.uninstall()
        print("✅ kirocrew service stopped and removed.")
        return 0
    if plat == Platform.SCHTASKS:
        try:
            # Stop first: /Delete removes the registration but does not end a
            # running instance, which would leave an unsupervised gateway.
            # Only when there is a task to stop: stop() refuses a failed
            # /Change /DISABLE, and /DISABLE on an absent task is one.
            if windows.is_installed():
                windows.stop()
            windows.uninstall()
        except windows.ServiceInstallError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
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
    if plat == Platform.SCHTASKS:
        # status() asks the scheduler, which can refuse or not answer. A
        # status verb is the one an operator runs precisely when something is
        # wrong, so it reports the problem and exits non-zero rather than
        # handing them a traceback.
        try:
            print(windows.status())
        except windows.ServiceInstallError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        return 0 if windows.is_active() else 1
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
        print(f"{'✅ ' if outcome.ok else '⚠️  '}{outcome.message}")
    return 0 if outcome.ok else 1


def remove_launcher_profile() -> int:
    """Unload and delete the launcher profile. Idempotent."""
    if current_platform() != Platform.SYSTEMD:
        print("ℹ️  Nothing to remove — the AppArmor sandbox profile is Linux-only.")
        return 0
    outcome = linux.remove_launcher_profile()
    if outcome.message:
        print(f"{'✅ ' if outcome.ok else '⚠️  '}{outcome.message}")
    else:
        print("✅ No AppArmor sandbox profile was installed.")
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
    if plat == Platform.SCHTASKS:
        try:
            installed = windows.is_installed()
        except windows.ServiceInstallError:
            # Could not ask the scheduler. The port still answers the question
            # the caller is actually asking -- is a gateway serving -- and
            # answering it without the scheduler is better than reporting no
            # service and starting a second one beside the first.
            return windows.is_active()
        return installed and windows.is_active()
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
    if plat == Platform.SCHTASKS:
        # INSTALLATION, not liveness. On systemd and launchd is_active() asks
        # the service manager, so False means stopped. Here the trigger repeats
        # every minute, so False can simply mean "between runs" -- and a stop
        # that skipped the disable would be undone within the minute while the
        # operator believes the gateway is down. is_active() is also a port
        # probe, which answers for a gateway this task never started.
        #
        # False, not a traceback, when Task Scheduler refuses or does not
        # answer. linux.stop() and macos.stop() cannot raise, so this caller
        # has no handler: the exception would escape `kirocrew stop` instead of
        # falling through to its foreground fallback, which is the one path
        # that still works when the service manager is the thing that is broken.
        try:
            if windows.is_installed():
                windows.stop()
                # Registration is not ownership. An installed-but-disabled
                # task and a gateway the operator started in the foreground
                # coexist happily, and /End refuses silently in that case, so
                # claiming the stop here would report a gateway down while it
                # keeps serving. If the port still answers, this stop did not
                # reach it and the caller's foreground fallback is what will.
                return not windows.is_active()
        except windows.ServiceInstallError as exc:
            logger.warning("Task Scheduler could not stop the gateway: %s", exc)
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
    if plat == Platform.SCHTASKS:
        # Same gate as stop_service, and for the same reason: a task caught
        # between runs is not a task that needs no restart. Returning False
        # here sends the caller to its foreground fallback, which is also
        # where a refusal or a timeout belongs -- see stop_service.
        try:
            if windows.is_installed():
                return windows.restart()
        except windows.ServiceInstallError as exc:
            logger.warning("Task Scheduler could not restart the gateway: %s", exc)
        return False
    return False


def manual_restart_hint() -> str:
    """Command an operator can run BY HAND to restart the installed service.

    Printed when :func:`restart_service` was refused by the service manager —
    a system-scope unit needs root/polkit privileges the calling process may
    not have. Unlike :func:`kiro_crew.service.common.restart_command_hint`,
    this must never answer ``kirocrew restart``: that is the command that just
    failed, so a circular hint would send the operator straight back into the
    same refusal.
    """
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        # "sudo systemctl restart kirocrew" — shared with the update path and
        # the Slack restart-failure hint so the string cannot drift.
        return restart_command_hint()
    if plat == Platform.LAUNCHD:
        # NOT `launchctl kickstart` — that is the exact call macos.restart()
        # just ran and got refused. Tearing the job down and bootstrapping it
        # from the installed plist is the outside-process recovery; `;` rather
        # than `&&` so a bootout refused because the job is not loaded still
        # proceeds to the bootstrap.
        uid = getattr(os, "getuid", lambda: -1)()
        return (
            f"launchctl bootout gui/{uid}/{LAUNCHD_LABEL}; "
            f'launchctl bootstrap gui/{uid} "{macos.PLIST_PATH}"'
        )
    if plat == Platform.SCHTASKS:
        # NOT a bare `/Run`: that is the call windows.restart() just made and
        # got refused. Ending the instance first is the outside-process
        # recovery, and `&` rather than `&&` so an `/End` refused because
        # nothing is running still proceeds to the `/Run`.
        #
        # The ABSOLUTE path, quoted. This string is printed for an operator to
        # paste into cmd.exe, which resolves a bare name from the current
        # directory first -- so a `schtasks.exe` the agent dropped in whatever
        # directory the operator happens to be in would run as them, on our
        # own instruction. schtasks_bin() resolves through the trusted-system
        # path table instead.
        exe = windows.schtasks_bin()
        if exe is None:
            return "kirocrew gateway"
        return f'"{exe}" /End /TN "{windows.TASK_NAME}" & "{exe}" /Run /TN "{windows.TASK_NAME}"'
    # No platform service manager exists here, so there is no service to have
    # refused the restart; kept total for safety rather than reachability.
    return "kirocrew gateway"
