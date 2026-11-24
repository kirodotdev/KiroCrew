"""Platform dispatch for service install/uninstall/status.

CLI entry points should call functions in this module rather than
importing :mod:`kiro_crew.service.linux` or :mod:`kiro_crew.service.macos`
directly. This keeps the dispatch logic in one place and makes the
``UNSUPPORTED`` path produce consistent error output.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from kiro_crew.service import linux, macos
from kiro_crew.service.common import (
    LAUNCHD_LABEL,
    RESTART_REFUSED,
    SERVICE_NAME,
    Platform,
    RestartReport,
    ScopeRestart,
    current_platform,
    headless_auth_warning,
    system_restart_command_hint,
)
from kiro_crew.service.inherited_env import (
    dropped_host_env_warning,
    missing_launcher_dirs_warning,
)

#: First line of the post-install hints. A service never opens a browser, so this
#: is the only place a new user learns how to reach the dashboard. It names the
#: command, not a URL: ``kirocrew token`` prints the link with the real port and a
#: sign-in token, which a hard-coded ``http://localhost:5476`` would not.
_OPEN_HINT = "   Open:   kirocrew token   (prints a sign-in link to the dashboard)"


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


def _print_dropped_host_env_warning(plat: Platform) -> None:
    """Name the host variables the service will not inherit, after a successful install.

    The AWS container credential endpoint, the AWS region and (under systemd) the
    ssh-agent socket of the installing shell, which MCP servers, cron scripts and
    agent sessions under the service would otherwise go without; then the
    configured MCP launcher dirs the service PATH lacks. Printed and
    non-fatal for the same reasons as :func:`_print_headless_auth_warning`.
    """
    for build in (dropped_host_env_warning, missing_launcher_dirs_warning):
        try:
            warning = build(plat=plat)
        except Exception:  # noqa: BLE001 - diagnostic must never fail the install
            continue
        if warning:
            print(warning)


def installed_unit_path() -> "Path | None":
    """Return the installed service definition's path, or None if not installed.

    Callers outside this module should not have to know which platform stores
    its definition where, nor which of the two modules to import. Presence of
    the file is the signal that a service exists to inherit (or drop) an
    environment: a host running ``kirocrew gateway`` in the foreground has none,
    and inherits the invoking shell instead.

    On Linux both systemd scopes count. The system unit file is checked first
    (a plain stat, no spawn); when it is absent the calling account's own
    manager is asked for the per-user unit the SELinux remedy stands up
    (:func:`linux.user_unit_path`), which bakes the same ``Environment=`` lines
    — so ``kirocrew doctor``'s managed-marker and credential checks read a
    user-scope gateway as the installed service it is, instead of "none".
    """
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        if linux.UNIT_PATH.is_file():
            return linux.UNIT_PATH
        return linux.user_unit_path()
    if plat == Platform.LAUNCHD and macos.PLIST_PATH.is_file():
        return macos.PLIST_PATH
    return None


def unit_is_ours(unit: Path) -> bool:
    """False for a system unit file that is an alias of another unit."""
    if unit == linux.UNIT_PATH:
        return linux.system_unit_is_ours()
    return True


def service_scope(unit: Path) -> str:
    """Scope label for an installed definition: launchd, system or user."""
    if unit == macos.PLIST_PATH:
        return "launchd"
    return "system" if unit == linux.UNIT_PATH else "user"


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
            # The same reading `uninstall` decides ownership by: one line, one
            # rendered form, and an unreadable file makes no claim.
            return linux.unit_file_carries_managed_marker(path)
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


def _plist_argv() -> "list[str] | None":
    """The launchd agent's ``ProgramArguments``, or ``None`` when unreadable."""
    payload = macos._plist_payload(macos.PLIST_PATH)
    argv = payload.get("ProgramArguments") if payload else None
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        return None
    return list(argv) or None


# ``systemctl show`` prints ``{ path=<exe> ; argv[]=<words> ; ignore_errors=... }``.
_SHOWN_COMMAND = re.compile(r"\{ path=.*? ; argv\[\]=(.*) ; ignore_errors=.*\}")


def _loaded_argv(scope: str) -> "list[str] | None":
    """The start command one systemd scope's manager has loaded for our unit.

    The manager is asked, through ``gateway_restart._systemd_show``: what it has
    loaded is what it starts, drop-ins included, and no unit file is parsed
    here. ``None`` when the manager cannot be asked or does not answer in time,
    when the name is not our own loaded unit, when it reports no start command
    or more than one, and when the command holds a ``$``.
    shortcut: the manager prints the words joined by spaces, so a word that
    holds a space reads as several; read the property over D-Bus if a unit with
    such a word must be told apart.
    """
    # Deferred: this module is imported by the CLI before the gateway's own.
    from kiro_crew import gateway_restart

    user = scope == "user"
    if user and linux._user_scope_unreachable_reason() is not None:
        return None
    try:
        props = gateway_restart._systemd_show(user)
    except (gateway_restart._Inconclusive, OSError, subprocess.SubprocessError):
        return None
    if props.get("Id") != [f"{SERVICE_NAME}.service"] or props.get("LoadState") != ["loaded"]:
        return None
    shown = props.get("ExecStart", [])
    match = _SHOWN_COMMAND.fullmatch(shown[0]) if len(shown) == 1 else None
    # The manager prints a ``$VARIABLE`` as written, and expands it at start.
    if match is None or "$" in match.group(1):
        return None
    return match.group(1).split() or None


def _scope_definition(scope: str) -> "Path | None":
    """The installed definition of one scope: ``system``, ``user`` or ``launchd``."""
    if scope == "system":
        return linux.UNIT_PATH if linux.UNIT_PATH.is_file() else None
    if scope == "user":
        return linux.user_unit_path()
    return macos.PLIST_PATH if macos.PLIST_PATH.is_file() else None


def installed_headless(scope: "str | None" = None) -> "bool | None":
    """Whether the installed service starts the gateway with ``--headless``.

    ``False`` with no service installed. ``None`` when a service is installed
    and its start command cannot be read. The command line is the one record of
    the mode: no setting or environment variable holds it, so nothing in the
    data home can change what the owner installed. On Linux it is the command
    the service manager has loaded (:func:`_loaded_argv`); on macOS it is the
    plist's ``ProgramArguments``.

    Without *scope* the service is the one :func:`installed_unit_path` names,
    which is the one an install rewrites. With it (a :class:`ScopeRestart`
    ``scope``) the service is that scope's own: on Linux a stopped system unit
    can sit beside the running per-user one, and each has its own mode. The
    caller names a scope that has a service, so that scope is always asked.
    """
    if scope is None:
        path = installed_unit_path()
        if path is None:
            return False
        scope = service_scope(path)
    argv = _plist_argv() if scope == "launchd" else _loaded_argv(scope)
    if argv is None:
        return None
    # Deferred: the gateway command's parser lives with the CLI, which imports
    # this module.
    from kiro_crew.cli_restart_headless import argv_headless

    return argv_headless(argv)


#: The line printed when a service is installed and its mode cannot be read.
MODE_UNKNOWN = "unknown (the service's start command could not be read)"


def restarted_headless(report: RestartReport) -> "bool | None":
    """The mode of the service a restart brought back, for the line printed after it.

    Read for each scope the manager restarted, never for one that was not
    started. ``None`` when a start command cannot be read, when two restarted
    scopes disagree, or when the read itself fails: the caller then says the
    mode is unknown, and a restart that worked is never reported as failed
    because of this read.
    """
    try:
        modes = {installed_headless(outcome.scope) for outcome in report.restarted}
    except Exception:  # noqa: BLE001 - a line printed after a restart must not fail it
        return None
    return modes.pop() if len(modes) == 1 else None


#: What a headless service prints where an ordinary one prints :data:`_OPEN_HINT`.
_HEADLESS_HINT = (
    "   Client: kirocrew token --bearer   (credential for a Crew API client; "
    "no dashboard is served)"
)


def _resolve_install_mode(headless: "bool | None") -> bool:
    """The mode this install writes.

    An explicit choice wins. Without one the installed definition's mode is
    kept, so the `service install` that doctor and the updater ask for never
    turns a headless gateway's dashboard on. A definition whose start command
    cannot be read installs the ordinary service, as an install without the
    flag always has, after one warning on stderr.
    """
    if headless is not None:
        return headless
    try:
        kept = installed_headless()
    except Exception:  # noqa: BLE001 - a read that fails must not fail the install
        kept = None
    if kept is None:
        try:
            where = f" ({installed_unit_path()})"
        except Exception:  # noqa: BLE001 - the lookup that failed above fails here too
            where = ""
        print(
            "Warning: could not read whether the installed service"
            f"{where} starts the gateway with --headless, so it is "
            "installed with the dashboard; `kirocrew service install --headless` "
            "installs it without the dashboard.",
            file=sys.stderr,
        )
        return False
    return kept


def _print_install_mode(headless: bool, *, chosen: bool) -> bool:
    """Print the mode line; True when the headless mode is stated as a fact.

    False for an ordinary install, and for a headless one whose start command
    does not read back as headless (a drop-in replaces it, or it cannot be
    read): the mode is then unknown and the closing hint stays the ordinary one.
    """
    if not headless:
        return False
    # The service this install wrote: the plist on macOS, else the system unit.
    scope = "launchd" if current_platform() == Platform.LAUNCHD else "system"
    try:
        stated = installed_headless(scope) is True
    except Exception:  # noqa: BLE001 - a line printed after an install must not fail it
        stated = False
    print(
        "   mode: "
        + (
            "headless (no dashboard; listens on loopback only)"
            if stated
            else "unknown (headless was installed, but the service's start command "
            "does not read back as headless)"
        )
        + ("" if chosen else "; kept from the installed service")
    )
    if not chosen:
        print("         `kirocrew service install --no-headless` serves the dashboard again.")
    return stated


def _unsupported_message() -> None:
    print(
        "Error: kirocrew service management is only supported on Linux (systemd)\n"
        "   and macOS (launchd). On other platforms run `kirocrew gateway`\n"
        "   directly or wrap it in tmux/screen yourself.",
        file=sys.stderr,
    )


def install_service(headless: "bool | None" = None) -> int:
    """Install and start the platform service.

    ``headless`` is the owner's choice on the command line: ``True`` for
    ``--headless``, ``False`` for ``--no-headless``, ``None`` for neither, which
    keeps the mode of the service already installed (ordinary on a first
    install). When that mode cannot be read the ordinary service is installed
    after a warning.

    Returns 0 on success, non-zero otherwise. On Linux the install
    prompts for sudo on first use to write
    ``/etc/systemd/system/kirocrew.service``, to run
    ``systemctl daemon-reload / enable / restart``, and to run
    ``loginctl enable-linger`` for the unit's account. The gateway itself
    runs as ``User=$USER`` once started — kirocrew code is never
    invoked under sudo. On macOS no sudo is required. The CLI is
    expected to surface the sudo prompt to a real terminal.
    """
    plat = current_platform()
    if plat not in (Platform.SYSTEMD, Platform.LAUNCHD):
        _unsupported_message()
        return 2
    mode = _resolve_install_mode(headless)
    if plat == Platform.SYSTEMD:
        try:
            profile = linux.install(headless=mode)
        except linux.ServiceInstallError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print("kirocrew service installed and started.")
        print(f"   unit: {linux.UNIT_PATH}")
        stated = _print_install_mode(mode, chosen=headless is not None)
        # Reported here, but performed inside linux.install() before the unit is
        # started — the directive only applies at service start. Deliberately
        # non-fatal: a failure warns and leaves the service running.
        if profile.message:
            print(f"   {'' if profile.ok else 'Warning: '}{profile.message}")
        # Agent runtimes stop at the account's last logout unless linger is on.
        # Non-fatal: a failure only warns; the install has already succeeded.
        linger = linux.enable_service_account_linger()
        if linger is not None:
            print(f"   {'' if linger[0] else 'Warning: '}{linger[1]}")
        _print_headless_auth_warning()
        _print_dropped_host_env_warning(plat)
        print()
        print(_HEADLESS_HINT if stated else _OPEN_HINT)
        print("   Status: kirocrew service status")
        print("   Logs:   kirocrew logs -f")
        print("   Remove: kirocrew service uninstall")
        return 0
    try:
        macos.install(headless=mode)
    except macos.ServiceInstallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("kirocrew service installed and started.")
    print(f"   plist: {macos.PLIST_PATH}")
    stated = _print_install_mode(mode, chosen=headless is not None)
    _print_headless_auth_warning()
    _print_dropped_host_env_warning(plat)
    print()
    print(_HEADLESS_HINT if stated else _OPEN_HINT)
    print("   Status: kirocrew service status")
    print(f"   Logs:   tail -f {macos.STDOUT_LOG}")
    print("   Remove: kirocrew service uninstall")
    return 0


def uninstall_service() -> int:
    """Stop and remove the platform service. Idempotent."""
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        # uninstall() needs root to remove the root-owned unit, so it can raise
        # ServiceInstallError on a non-root host without sudo. Catch it and exit
        # non-zero with the reason rather than letting a traceback escape (and
        # leaving the service installed).
        try:
            report = linux.uninstall()
            # Whatever removes the service removes the grant, so a host is left
            # as it was found rather than carrying an orphaned userns permission.
            profile = linux.remove_apparmor_profile()
        except linux.ServiceInstallError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        # Both scopes are always named: a unit can live in the system scope, in
        # the account's user scope (the SELinux remedy), or in neither, and the
        # operator must read which one this actually touched. An unfinished
        # teardown still prints everything (and still drops the profile above);
        # the warning sits on the scope that needs a hand, and only the exit code
        # says so.
        if report.removed_any:
            print("kirocrew service stopped and removed.")
        else:
            print("Note: No kirocrew service was removed.")
        for scope, line in (("system", report.system), ("user", report.user)):
            mark = "Warning: " if scope in report.unfinished else ""
            print(f"   {mark}{scope} scope: {line}")
        if profile.message:
            print(f"   {'' if profile.ok else 'Warning: '}{profile.message}")
        return 1 if report.incomplete else 0
    if plat == Platform.LAUNCHD:
        held_by = macos.uninstall()
        if held_by:
            print(
                f"Error: launchd did not unload {LAUNCHD_LABEL}: it is still loaded in {held_by}.\n"
                f"   Its plist and launcher were removed. Unload it by hand:\n"
                f"   launchctl bootout {held_by}/{LAUNCHD_LABEL}",
                file=sys.stderr,
            )
            return 1
        print("kirocrew service stopped and removed.")
        return 0
    _unsupported_message()
    return 2


def service_status() -> int:
    """Print the platform service status. Returns 0 if the service is up, 1 if
    not, 2 if unsupported.

    On Linux the exit code follows :func:`linux.is_up` — ``ActiveState=active``
    in either scope, what ``systemctl is-active`` exits 0 for — not the wider
    :func:`linux.is_active` that ``stop`` / ``restart`` select scopes by. The two
    part on a crash loop: a unit in ``activating (auto-restart)`` must be
    reachable to ``kirocrew stop``, and must read as DOWN to a script gating on
    this exit code, since a gateway that never started is the failure `status`
    exists to expose. The headline above the exit code prints the real state
    either way.
    """
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        _print_status_mode()
        print(linux.status())
        return 0 if linux.is_up() else 1
    if plat == Platform.LAUNCHD:
        _print_status_mode()
        print(macos.status())
        return 0 if macos.is_up() else 1
    _unsupported_message()
    return 2


def _print_status_mode() -> None:
    """One line above the manager's report for each installed headless definition.

    Read for each scope on its own; an ordinary service prints nothing, so its
    output is unchanged. With a definition in both systemd scopes each line
    names its scope, because the two can differ. Never fails the status it
    sits above.
    """
    scopes = ("launchd",) if current_platform() == Platform.LAUNCHD else ("system", "user")
    modes: list[tuple[str, "bool | None"]] = []
    for s in scopes:
        try:
            if _scope_definition(s) is None:
                continue
            modes.append((s, installed_headless(s)))
        except Exception:  # noqa: BLE001 - a status line must not fail the status
            # A read that failed is reported as unknown, never left out.
            modes.append((s, None))
    for scope, headless in modes:
        label = "mode" if len(modes) == 1 else f"mode ({scope} unit)"
        if headless is None:
            print(f"{label}: {MODE_UNKNOWN}")
        elif headless:
            print(f"{label}: headless (no dashboard; the gateway listens on loopback only)")


def install_launcher_profile(exec_path: str | None = None) -> int:
    """Attach the userns AppArmor profile to a directly launched app.

    Linux-only by nature: the whole feature exists for one Ubuntu kernel
    restriction. On macOS and elsewhere this is a clean no-op with an explanation
    rather than an error, because the same desktop app ships everywhere and must
    not present a broken command to users who do not need it.
    """
    if current_platform() != Platform.SYSTEMD:
        print(
            "Note: The AppArmor sandbox profile is Linux-only — this host does not "
            "restrict unprivileged user namespaces, so nothing is needed."
        )
        return 0
    outcome = linux.install_launcher_profile(exec_path)
    if outcome.message:
        print(f"{'' if outcome.ok else 'Warning: '}{outcome.message}")
    return 0 if outcome.ok else 1


def remove_launcher_profile() -> int:
    """Unload and delete the launcher profile. Idempotent."""
    if current_platform() != Platform.SYSTEMD:
        print("Note: Nothing to remove — the AppArmor sandbox profile is Linux-only.")
        return 0
    outcome = linux.remove_launcher_profile()
    if outcome.message:
        print(f"{'' if outcome.ok else 'Warning: '}{outcome.message}")
    else:
        print("No AppArmor sandbox profile was installed.")
    return 0 if outcome.ok else 1


def sandbox_profile_status(exec_path: str | None = None) -> int:
    """Report whether THIS launch is covered by the launcher profile.

    Exit code is the answer, so a script can gate on it: 0 when the sandbox can
    be built (covered, or a host that never needed the profile), 1 when it cannot.
    """
    if current_platform() != Platform.SYSTEMD:
        print("This platform does not restrict unprivileged user namespaces.")
        return 0
    from kiro_crew.service import apparmor

    ok, detail = apparmor.launcher_status(exec_path)
    print(f"{'' if ok else 'Error: '}{detail}")
    return 0 if ok else 1


def is_service_active() -> bool:
    """Return True if a kirocrew service is installed and currently running."""
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        return linux.is_active()
    if plat == Platform.LAUNCHD:
        return macos.is_active()
    return False


def service_alias_holder() -> "linux.AliasHolder | None":
    """Return the scope whose ``kirocrew.service`` name is an alias of another
    unit, or ``None`` when none is.

    systemd-only: ``launchd`` has no alias concept for labels, so macOS and the
    unsupported platforms always answer ``None``. :func:`is_service_active`,
    :func:`stop_service` and :func:`restart_service` all fail closed on an alias
    (they never select or count it), which is right — the aliased unit is not
    this gateway — but leaves a CLI caller that falls back from them unable to
    tell "no managed unit here" from "the name resolves to a unit a manager
    supervises". ``kirocrew stop`` / ``kirocrew restart`` ask this before their
    foreground-gateway fallback so they refuse rather than signal the alias
    target's process and spawn a competitor beside it.
    """
    if current_platform() == Platform.SYSTEMD:
        return linux.alias_holder()
    return None


def stale_unit_notes() -> list[str]:
    """One line per way an installed systemd unit lags this build's unit, with
    its remedy (:func:`linux.stale_unit_notes`); empty off systemd."""
    if current_platform() == Platform.SYSTEMD:
        return linux.stale_unit_notes()
    return []


def restart_policy_notes() -> list[str]:
    """One advisory line per scope whose installed systemd unit restarts on
    anything other than ``always`` (:func:`linux.restart_policy_notes`); empty
    off systemd."""
    if current_platform() == Platform.SYSTEMD:
        return linux.restart_policy_notes()
    return []


class ServiceStopRefused(RuntimeError):
    """The service manager refused to stop a running unit.

    ``hints`` holds the command to run by hand for each refused scope. Raised
    rather than answered ``False``: ``False`` sends the CLI to its foreground
    path, which would signal a process the manager restarts (``Restart=always``).
    """

    def __init__(self, hints: list[str]) -> None:
        super().__init__("; ".join(hints))
        self.hints = list(hints)


def stop_service() -> bool:
    """Stop the platform service if active. Returns True if a service was stopped.

    Raises :class:`ServiceStopRefused` when a systemd unit is running and its
    manager refused the stop.
    """
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        if linux.is_active():
            refused = linux.stop()
            if refused:
                raise ServiceStopRefused(refused)
            return True
        return False
    if plat == Platform.LAUNCHD:
        # No is_active() gate: its ``list`` probe reads the CALLER's domain, so
        # from SSH it misses a gui/<uid> job. stop() searches both domains and
        # returns False when nothing is loaded or launchd refused the bootout.
        return macos.stop()
    return False


def restart_service() -> RestartReport:
    """Restart the platform service if installed and active.

    Returns a :class:`RestartReport`: truthy when a service was restarted and
    came back up, so callers still branch on "was this handled by the service
    manager?" rather than re-doing platform detection, and read its
    ``failures`` when it was handled and did not take — which scope, whether
    the manager REFUSED the restart (retry by hand with that scope's command,
    ``hint``) or the unit did not stay up (read its journal). An empty report
    (nothing running under a manager) sends callers to the foreground-gateway
    path (SIGTERM-by-port + detached spawn). Mirrors :func:`stop_service`.
    """
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        if linux.is_active():
            return linux.restart()
        return RestartReport()
    if plat == Platform.LAUNCHD:
        # Gated on is_loaded(), not is_active(): the latter's ``list`` probe
        # reads the CALLER's domain and from SSH misses a gui/<uid> job, which
        # would send `kirocrew restart` to the foreground path and spawn an
        # unmanaged gateway beside the KeepAlive one.
        if macos.is_loaded():
            ok = macos.restart()
            return RestartReport(
                (
                    ScopeRestart(
                        "launchd",
                        ok,
                        reason="" if ok else "launchctl refused the restart",
                        kind="" if ok else RESTART_REFUSED,
                        hint="" if ok else manual_restart_hint(),
                    ),
                )
            )
        return RestartReport()
    return RestartReport()


def manual_restart_hint() -> str:
    """Command an operator can run BY HAND to restart the installed service.

    Printed when :func:`restart_service` was refused by the service manager —
    a system-scope unit needs root/polkit privileges the calling process may
    not have. Unlike :func:`kiro_crew.service.common.restart_command_hint`,
    this must never answer ``kirocrew restart``: that is the command that just
    failed, so a circular hint would send the operator straight back into the
    same refusal. On Linux the per-scope report (:class:`ScopeRestart.hint`)
    carries the command for the scope that refused; this is the launchd path's
    and the system scope's answer.
    """
    plat = current_platform()
    if plat == Platform.SYSTEMD:
        # "sudo systemctl restart kirocrew" — the same spelling the update path
        # and the Slack restart-failure hint use for a system unit, so the
        # string cannot drift; unconditional, because a systemd host with no
        # unit file at all would otherwise be answered the circular CLI.
        return system_restart_command_hint()
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
    # No platform service manager exists here, so there is no service to have
    # refused the restart; kept total for safety rather than reachability.
    return "kirocrew gateway"
