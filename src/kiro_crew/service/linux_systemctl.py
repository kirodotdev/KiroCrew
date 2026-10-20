"""How the Linux service reaches systemd, and what one scope says about its unit.

Privilege escalation (:func:`_privilege_prefix`, :func:`_sudo_run`), ``systemctl``
against the system manager or the calling account's own (:func:`_systemctl`), and
the state one scope reports for ``kirocrew.service`` (:class:`_UnitState`,
:func:`_unit_state`, :func:`_stale_unit_line`). Every ``systemctl`` call and every sudo
argv prefix of the family comes from here; ``linux_install._write_unit_via_sudo`` and
the account queries in :mod:`kiro_crew.service.linux_unit` spawn their own children.
Imports only :mod:`kiro_crew.service.linux_unit`.

Part of the :mod:`kiro_crew.service.linux` family: callers and tests reach every name
through that facade, which keeps the module's patch seams whole
(:mod:`kiro_crew._facade_mirror`). In the docstrings and comments below, "this module"
means that family and "the module docstring" means the facade's, which carries the
systemd 219 commitment and the sudo scope.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from kiro_crew.gateway_lock import LIVE_HOLDER_EXIT_CODE
from kiro_crew.service.common import SERVICE_NAME, systemctl_user_env
from kiro_crew.service.linux_unit import UNIT_PATH, _current_user
from kiro_crew.subprocess_utf8 import UTF8_TEXT


class ServiceInstallError(RuntimeError):
    """Raised when service install can't proceed without manual user action."""


# An exception's dotted name is what a traceback shows: keep the facade's.
ServiceInstallError.__module__ = "kiro_crew.service.linux"


def _privilege_prefix() -> list[str]:
    """Return the argv prefix that runs a command with root privilege.

    Empty when the caller is already root (``euid == 0``) — a minimal
    container or a ``root`` login often has no ``sudo`` binary at all, so
    invoking ``sudo`` there would raise ``FileNotFoundError`` for a privilege
    the process already holds. When not root, ``sudo`` is required to write the
    root-owned unit and drive ``systemctl``; if it is missing we cannot proceed,
    and the caller must surface a clear :class:`ServiceInstallError` rather than
    let a raw ``FileNotFoundError`` escape ``controller.install_service`` (which
    only catches ``ServiceInstallError``). ``_require_privilege`` enforces that.
    """
    # getattr: os.geteuid does not exist on Windows, where this module is
    # imported (via controller) even though these functions never run there.
    # Default 1000 (a non-root euid) keeps the "needs sudo" branch on any
    # platform lacking geteuid, which is the safe assumption.
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        return []
    return ["sudo"]


def _require_privilege() -> None:
    """Raise a clean error if privilege escalation is needed but unavailable.

    Called by every write/control action before it shells out, so a Linux host
    without ``sudo`` (and not already root) fails on the friendly
    ``ServiceInstallError`` path the CLI prints, instead of an uncaught
    ``FileNotFoundError`` traceback.

    Scoped to Linux: this is the systemd module, and in production the
    controller only dispatches here on Linux (macOS uses launchd). The check
    exists specifically for the Linux-host-without-sudo case, so on any other
    platform it is a no-op — which also keeps these functions callable in
    cross-platform unit tests that mock the subprocess layer on a host with no
    ``sudo`` binary.
    """
    if not sys.platform.startswith("linux"):
        return
    # Ask :func:`_privilege_prefix` rather than re-reading ``os.geteuid``: a
    # non-empty prefix IS "this call will shell out through sudo", so the two
    # functions cannot drift into disagreeing about whether escalation is needed.
    if _privilege_prefix() and shutil.which("sudo") is None:
        raise ServiceInstallError(
            "This action needs root to manage the system service at "
            f"{UNIT_PATH}, but 'sudo' was not found. Re-run as root, or install "
            "sudo (e.g. 'yum install sudo' / 'apt-get install sudo')."
        )


def _sudo_run(
    *args: str,
    input_text: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command with root privilege, capturing output.

    Prepends ``sudo`` only when the caller is not already root (see
    :func:`_privilege_prefix`). Sudo prompts for a password on first use;
    subsequent calls within the cached ticket window run silently. All call
    sites (``install``, ``uninstall``, ``stop``) are interactive user commands
    invoked from a TTY, so we always allow the prompt.

    A missing ``sudo`` is turned into a synthetic non-zero result rather than a
    raised ``FileNotFoundError``, so best-effort callers (``restart``, ``stop``)
    degrade to "did not run" instead of crashing. The raising entry points
    (``install`` / ``uninstall``) call :func:`_require_privilege` first, so they
    surface the precise reason before reaching here.
    """
    try:
        return subprocess.run(
            [*_privilege_prefix(), *args],
            input=input_text,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,  # subprocess-encoding: locale -- sudo and coreutils write in the locale
            check=False,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=list(args), returncode=127, stdout="", stderr="sudo: command not found"
        )


def _systemctl(
    *args: str, sudo: bool = True, user: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run ``systemctl`` against one scope: the system manager, or with
    ``user=True`` the calling account's own manager (``systemctl --user``).

    A user-scope call never goes through sudo, whatever ``sudo`` says: under
    ``sudo`` the process is root, and ``systemctl --user`` there addresses ROOT's
    manager, not the account whose unit this module cares about. Callers gate on
    :func:`_user_scope_unreachable_reason` before spawning for exactly that case.

    It runs with :func:`systemctl_user_env` — the one resolver every
    ``systemctl --user`` in this codebase spawns with — so a shell that inherited
    no login-session variables (a gateway started from a system unit, and every
    shell it spawns) still finds the account's bus when its socket exists, and a
    "not reachable" reading names a host condition, not a missing variable.
    """
    if user:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            check=False,
            env=systemctl_user_env(),
            **UTF8_TEXT,
        )
    if sudo:
        return _sudo_run("systemctl", *args)
    return subprocess.run(["systemctl", *args], capture_output=True, check=False, **UTF8_TEXT)


def _user_scope_unreachable_reason() -> str | None:
    """Why this process cannot see the service account's user manager, or ``None``.

    Decided from the process's own identity, never from a spawned command's
    output: a root process whose ``SUDO_USER`` names a human is a ``sudo`` shell,
    and ``systemctl --user`` from it would answer for root's (usually absent)
    manager while the human's unit runs on unseen — the one answer worse than
    "unreachable". ``service install`` is documented to run under sudo, so this is
    the shell an operator is most likely to type ``service status`` into.

    Every other failure to reach the user bus — no session, a stripped
    environment, a sandbox — is left to ``systemctl`` itself, whose exit status
    and diagnostic :func:`_unit_state` reports verbatim as "not reachable".
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return None
    account = _current_user()
    if not account or account == "root":
        return None
    return (
        f"this is a root shell, and the user scope belongs to {account}'s own "
        f"session; run `systemctl --user status {SERVICE_NAME}.service` as {account}"
    )


# ``ActiveState`` values under which the unit has no process. Everything else
# (``active``, ``activating``, ``deactivating``, ``reloading``, and any value a
# newer systemd adds, or an empty one) is treated as "may still be running".
_STOPPED_STATES = frozenset({"inactive", "failed"})

# ``ActiveState`` values under which the unit is UP — the process is running and
# the manager is not between a crash and the next attempt. Exactly the states
# ``systemctl is-active`` exits 0 for, so the `service status` exit code keeps
# the meaning the base gave it while reading both scopes.
_UP_STATES = frozenset({"active", "reloading"})


@dataclass(frozen=True)
class _UnitState:
    """What one systemd scope says about ``kirocrew.service``.

    ``load`` is systemd's ``LoadState`` (``loaded``, ``not-found``, ``masked``,
    ...) or ``None`` when the scope could not be queried at all, in which case
    ``error`` carries the reason. ``unit_id`` is the canonical ``Id`` the manager
    resolved the name to — another unit's name when ours is an alias — and
    ``fragment`` the unit file it loaded for THAT unit — the path
    :func:`_owned_unit` decides ownership from. ``result`` is
    the unit's ``Result`` — ``success``, or why its last run ended (``exit-code``,
    ``signal``, ``start-limit-hit``, …) — which is what tells a crash loop's
    ``activating (auto-restart)`` apart from a unit that is merely slow to start.

    Two questions are asked of this state and they are deliberately not the
    same predicate: :attr:`running` — is there a process a ``stop`` or
    ``restart`` must reach, which includes a unit the manager is still trying
    to run — and :attr:`up` — is the gateway actually running right now, which
    excludes it. A ``203/EXEC`` crash loop is the state the two disagree on:
    ``kirocrew stop`` must reach it, ``kirocrew service status`` must not exit
    0 for it.
    """

    scope: str
    load: str | None
    active: str = ""
    sub: str = ""
    fragment: str = ""
    unit_id: str = ""
    error: str = ""
    result: str = ""
    started: str = ""

    @property
    def reachable(self) -> bool:
        return self.load is not None

    @property
    def installed(self) -> bool:
        """The manager knows a unit by this name (any load state but not-found)."""
        return self.load is not None and self.load != "not-found"

    @property
    def running(self) -> bool:
        """The unit has, or may still have, a process: any ``ActiveState`` but
        ``inactive`` / ``failed``. ``activating`` covers a crash-looping unit
        sitting in its auto-restart backoff — the state the reporter's ``203/EXEC``
        loop spends nearly all its time in, which ``is-active`` answers non-zero
        for — and ``deactivating`` / ``reloading`` a unit mid-transition. Load
        state says nothing here: a unit masked or made unparseable at runtime
        keeps running until it is stopped. This is the REACH predicate; see
        :attr:`up` for health.
        """
        return self.load is not None and self.active not in _STOPPED_STATES

    @property
    def up(self) -> bool:
        """The gateway is running right now: ``ActiveState`` is ``active`` (or
        ``reloading``), the states ``systemctl is-active`` exits 0 for. A unit in
        ``activating`` — crash-looping through ``auto-restart``, or still
        starting — is not up, and neither is ``failed``. This is the HEALTH
        predicate the `service status` exit code follows; :attr:`running` is
        the wider one ``stop`` / ``restart`` select scopes by.
        """
        return self.load is not None and self.active in _UP_STATES

    @property
    def is_alias(self) -> bool:
        """The name resolves to a different canonical unit."""
        return bool(self.unit_id) and self.unit_id != f"{SERVICE_NAME}.service"

    @property
    def ours(self) -> bool:
        """The unit the name resolves to IS ``kirocrew.service`` — the one guard
        every verb that acts on the name shares, and it fails CLOSED: the manager
        must have reported that exact canonical ``Id``. ``systemctl show <name>``
        on an alias answers for the unit the alias points at, and so would
        ``stop``, ``restart``, ``disable`` and the unlink: an operator who declared
        ``Alias=kirocrew.service`` on their own unit has not made that unit this
        gateway; and an answer with no ``Id`` at all has verified nothing, so it
        is not acted on either. :func:`uninstall` refuses such a unit whole;
        :func:`stop`, :func:`restart`, :func:`is_active` and :func:`is_up` do not
        select or count it, so nothing this module runs ever lands on another
        unit.
        """
        return self.load is not None and self.unit_id == f"{SERVICE_NAME}.service"

    @property
    def acts_on(self) -> bool:
        """Selected by ``stop`` / ``restart`` and counted by ``is_active``: the
        unit is running (:attr:`running`) and it is ours (:attr:`ours`)."""
        return self.running and self.ours

    def headline(self) -> str:
        """One line naming the scope and its state — never a bare ``inactive``
        standing in for "no unit here"."""
        if not self.reachable:
            return f"{self.scope} scope: not reachable from this shell ({self.error})"
        if not self.installed:
            return f"{self.scope} scope: not installed"
        alias = f", an alias of {self.unit_id}" if self.is_alias else ""
        return f"{self.scope} scope: {self.active} ({self.sub}){alias}"


_SHOW_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "FragmentPath",
    "Result",
    "ExecMainStartTimestampMonotonic",
)


def _unit_state(*, user: bool) -> _UnitState:
    """Query one scope for the unit's load / active state and unit file.

    ``systemctl show`` rather than ``is-active`` or ``status`` because it is the
    one query that separates "no unit in this scope" (``LoadState=not-found``)
    from "a unit that is stopped" (``loaded`` + ``inactive``): ``is-active``
    answers ``inactive`` to both, which is what turns a running user-scope
    gateway into a dead system unit in a report that asks only one scope.
    Properties are parsed as ``Key=value`` lines rather than read with
    ``--value``, which the systemd 219 the module docstring commits to does not
    have.

    A scope is unreachable when ``systemctl`` exits non-zero without printing a
    ``LoadState`` — no bus, no session, a sandbox — and the diagnostic is carried
    as the reason; nothing here classifies stderr text.
    """
    scope = "user" if user else "system"
    if user:
        reason = _user_scope_unreachable_reason()
        if reason is not None:
            return _UnitState(scope, None, error=reason)
    argv: list[str] = ["show"]
    for prop in _SHOW_PROPERTIES:
        argv += ["-p", prop]
    res = _systemctl(*argv, f"{SERVICE_NAME}.service", sudo=False, user=user)
    props: dict[str, str] = {}
    for line in (res.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    load = props.get("LoadState")
    if load is None:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        return _UnitState(
            scope,
            None,
            error=detail[0] if detail else f"systemctl exited {res.returncode} without output",
        )
    return _UnitState(
        scope,
        load,
        active=props.get("ActiveState", ""),
        sub=props.get("SubState", ""),
        fragment=props.get("FragmentPath", ""),
        unit_id=props.get("Id", ""),
        result=props.get("Result", ""),
        started=props.get("ExecMainStartTimestampMonotonic", ""),
    )


def _loaded_restart_prevent(*, user: bool) -> str | None:
    """The ``RestartPreventExitStatus`` value the scope's manager has LOADED for
    our unit -- the merged unit, drop-ins included, as of the last daemon-reload
    -- or ``None`` when ``systemctl`` answered with no such property line.

    ``--all`` so an unset property still prints as ``RestartPreventExitStatus=``
    (empty) rather than being omitted, which keeps "not set" apart from "this
    systemctl did not answer".
    """
    res = _systemctl(
        "show",
        "--all",
        "-p",
        "RestartPreventExitStatus",
        f"{SERVICE_NAME}.service",
        sudo=False,
        user=user,
    )
    for line in (res.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "RestartPreventExitStatus":
            return value.strip()
    return None


def _stale_unit_line(state: _UnitState) -> str | None:
    """One line saying the LOADED unit does not exempt
    :data:`LIVE_HOLDER_EXIT_CODE` from ``Restart=``, with the command that fixes
    it, or ``None`` when there is nothing to say.

    ``service install`` writes the unit once and no upgrade re-renders it, so a
    unit written before the exemption existed keeps relaunching into the
    live-holder refusal. The manager's loaded value is what is asked, not the
    file on disk: it is what systemd acts on, it includes drop-ins, and a file
    edited without a daemon-reload still reads as missing. Read-only on purpose:
    no sudo, no rewrite, no reload. Only a loaded unit that is ours
    (:attr:`_UnitState.ours`) is asked; a property ``systemctl`` did not report
    makes no claim.
    """
    if state.load != "loaded" or not state.ours:
        return None
    value = _loaded_restart_prevent(user=state.scope == "user")
    if value is None or str(LIVE_HOLDER_EXIT_CODE) in value.split():
        return None
    directive = f"RestartPreventExitStatus={LIVE_HOLDER_EXIT_CODE}"
    if state.scope == "system":
        remedy = "re-run `sudo kirocrew service install` to re-render it"
    else:
        where = f" in {state.fragment}" if state.fragment else ""
        remedy = f"add it under [Service]{where}, then run `systemctl --user daemon-reload`"
    return (
        f"  ⚠️  the loaded unit lacks `{directive}`, which this build's unit carries "
        f"(no upgrade re-renders an installed unit): {remedy}."
    )
