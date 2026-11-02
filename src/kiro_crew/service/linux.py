"""systemd system-service generation and control for Linux.

The unit lives at ``/etc/systemd/system/kirocrew.service`` and is
enabled+started via ``sudo systemctl enable --now``. The service runs
as the invoking user (via ``User=`` in the unit) — only the install,
uninstall, and start/stop actions need sudo.

Why system-level instead of user-level (``systemctl --user``):
some older distros (notably systemd 219) do not have a working
per-user systemd manager — ``systemctl --user`` fails with
``Failed to get D-Bus connection``. System-level units work
uniformly across any distro shipping systemd >= 219, which is
everything since 2015.

One host class this choice does NOT work on, and cannot be made to work by
anything the installer writes: an SELinux-enforcing host whose kirocrew lives
under ``$HOME`` (the default on Bazzite, Fedora Silverblue/Kinoite and other
atomic desktops). PID 1's domain is denied ``execute`` on a home-labelled file,
so the unit fails every start with ``203/EXEC``. :mod:`kiro_crew.service
.selinux` detects exactly that case by querying the loaded policy, and
:func:`install` refuses up front with a rendered user-scope unit as the remedy
rather than writing a unit that provably cannot start. A per-user INSTALL mode
is deliberately NOT implemented here — it is an install-model change (where the
AppArmor profile and the root-owned overrides file live) rather than a
mechanical one. The unit that remedy stands up is nevertheless first-class to
every other verb: :func:`status`, :func:`is_active`, :func:`stop`,
:func:`restart` and :func:`uninstall` look at BOTH scopes and name the one they
report on, because a health command that sees only the system unit reports a
running user-scope gateway as ``inactive (dead)``.

Sudo scope: this file escalates ``systemctl``, ``install``, ``mkdir``,
``rm``, ``rmdir`` and ``test`` directly, and lends its privileged helpers
to ``service/apparmor.py``, which adds ``apparmor_parser``, ``aa-exec``,
and — inside ``aa-exec`` — ``setpriv`` plus a trusted system ``python3``.

What each mechanism here buys is narrower than it reads, and for ``setpriv`` it
depends on which install path is running — that gap is where an audit of this
module goes wrong.

Trusted resolution buys one thing, on both paths: the interpreter is root-owned,
resolved from a fixed list of trusted system directories, and never
``sys.executable`` — which rules out escalating the venv python, the one that is
user-writable. It says nothing about what that interpreter then loads, so the
probe also runs it ISOLATED: ``python3 -I`` (no working directory on
``sys.path``, no user site, no ``PYTHON*`` variables), from the fixed directory
``/``, with every ``PYTHON*`` variable dropped from the environment it inherits.
Without that, CPython prepends the caller's working directory to ``sys.path``
and imports ``site``, so code from that working directory, ``PYTHONPATH``, a
user-site ``.pth`` line, ``sitecustomize`` or ``usercustomize`` would run before
or during the payload's own first import — ``ctypes``, which a planted module on
any of those paths shadows. ``cd /tmp && sudo kirocrew service install`` is the
case that needs no privilege at all.

WHOSE privileges that loaded code gets is what ``setpriv`` decides, and both
install paths are live. It reuids to the account the INSTALLER was invoked as
(``os.getuid()``): started as an ordinary user — the default, where this module
escalates individual commands through ``sudo`` — it reuids from sudo's root back
to that user, so the probe stays unprivileged; started as
``sudo kirocrew service install`` it reuids to 0, which is a no-op, so on that
path the probe runs as root and the isolation above is what keeps planted code
out of it.

What IS bounded on both paths is the PAYLOAD: a constant stdlib snippet importing
no ``kiro_crew``, so no MCP / LLM / agent code is reached deliberately.

``docs/system-specs/modules/security.md`` carries the reasoning behind the
AppArmor step's four tools. The actual gateway runs as ``User=$USER`` once
started.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror
from kiro_crew.service.common import (
    RESTART_NOT_UP,
    RESTART_REFUSED,
    RESTART_UNCONFIRMED,
    SERVICE_NAME,
    RestartReport,
    ScopeRestart,
    system_restart_command_hint,
    user_restart_command_hint,
)
from kiro_crew.service.linux_systemctl import _stale_unit_line, _systemctl, _unit_state
from kiro_crew.service.linux_uninstall import _first_line


def user_unit_installed() -> bool:
    """Whether the calling account's own manager has a ``kirocrew.service`` installed:
    the manager answers a load state other than ``not-found`` for a unit whose
    canonical ``Id`` is ours (``loaded``, ``masked``, ``bad-setting`` and
    ``error`` all count; only ``not-found`` does not).

    The one scope question a caller outside this module needs: ``kirocrew logs``
    reads the USER journal when the gateway is the per-user unit, and that
    journal exists only if this answers True. An unreachable user scope (root
    shell, no session bus) is False — there is nothing to read from here.
    """
    state = _unit_state(user=True)
    return state.installed and state.ours


def system_unit_is_ours() -> bool:
    """Our system unit is loaded and its canonical ``Id`` is ours (fail-closed)."""
    state = _unit_state(user=False)
    return state.load == "loaded" and state.ours


def user_unit_active() -> bool:
    """Whether the per-user unit is running right now — active, or crash-looping
    through ``activating (auto-restart)``, which is the unit whose journal holds
    the failure worth reading.

    ``kirocrew logs`` asks this when BOTH scopes hold a unit — a stopped system
    unit left by an earlier install beside the per-user one — so the journal it
    shows is the running gateway's, not the dead unit's.
    """
    return _unit_state(user=True).acts_on


def user_unit_path() -> Path | None:
    """The per-user unit's file, when the calling account's own manager has one
    loaded and the file exists — the user-scope answer to
    :func:`controller.installed_unit_path`'s "is a service definition installed".

    The manager is asked, not a directory guessed: the SELinux remedy names
    ``~/.config/systemd/user``, but a unit an operator placed in any other
    user-unit directory is just as much the user-scope unit, and the file
    systemd reports as ``FragmentPath`` is the one it loaded. Only a LOADED unit
    whose canonical ``Id`` is ours (:attr:`_UnitState.ours`, fail-closed) answers:
    a unit the manager does not have loaded (a file dropped without
    ``daemon-reload``), a mask (``FragmentPath=/dev/null`` — no definition to
    read), a unit that failed to parse (``bad-setting`` / ``error`` — a file the
    doctor would read as the definition while the manager runs nothing from
    it), a name that is an alias of another unit, an answer with no ``Id`` or a
    blank ``LoadState`` (nothing verified), and an unreachable user scope all
    answer ``None``: nothing this account can point ``kirocrew doctor`` at.
    """
    state = _unit_state(user=True)
    if state.load != "loaded" or not state.ours or not state.fragment:
        return None
    path = Path(state.fragment)
    return path if path.is_file() else None


def _running_scopes() -> list[bool]:
    """The ``user`` flags of every scope whose unit is running or mid-transition
    AND is canonically ours (:attr:`_UnitState.acts_on`).

    The one scope question ``is_active()``, ``stop()`` and ``restart()`` share,
    asked with the same ``systemctl show`` the status verbs use rather than a
    second ``is-active`` probe: ``is-active`` answers non-zero for ``activating``,
    so a crash-looping unit in its auto-restart backoff would select no scope and
    ``kirocrew stop`` would issue nothing while the loop kept flapping. An alias
    is never selected: ``show kirocrew.service`` answered for the unit the alias
    points at, and every verb issued on our name would act on that unit — the
    same guard :func:`uninstall` applies before it touches a file.
    """
    return [user for user in (False, True) if _unit_state(user=user).acts_on]


def is_active() -> bool:
    """Return True if OUR unit is running in EITHER systemd scope — the REACH
    predicate.

    "Running" is any ``ActiveState`` but ``inactive`` / ``failed`` — a unit
    crash-looping through ``activating (auto-restart)`` counts, because a caller
    asking this is about to stop or restart it, and a unit the manager is still
    trying to run is one it must be able to reach. It is NOT the answer to "is
    the gateway up": that is :func:`is_up`, which the `service status` exit code
    follows. A name that is an alias of another unit is not counted in either:
    that unit is not this gateway, and the verbs gated on this answer would act
    on it. ``show`` needs no sudo, so both scopes use the unprivileged path.
    The user scope counts because the SELinux remedy runs the gateway there; a
    caller must not be told no while a gateway runs.
    """
    return bool(_running_scopes())


def is_up() -> bool:
    """Return True if OUR gateway is UP in either scope — the HEALTH predicate.

    ``ActiveState=active`` (or ``reloading``), which is what ``systemctl
    is-active`` exits 0 for. A crash-looping unit in ``activating
    (auto-restart)`` and a ``failed`` one are not up, however reachable they
    are: the `service status` exit code follows this predicate so that a script
    or monitor gating on it reads a gateway that never started as down — the
    ``203/EXEC`` loop is the failure `status` exists to surface, and its
    headline says ``activating (auto-restart)`` while this says 1. An alias of
    another unit is not up either: whatever runs under that name is not this
    gateway (the headline names the alias).
    """
    return any((s := _unit_state(user=user)).up and s.ours for user in (False, True))


@dataclass(frozen=True)
class AliasHolder:
    """A scope whose ``kirocrew.service`` name resolves to a DIFFERENT unit.

    ``scope`` is ``system`` or ``user``; ``unit_id`` is the canonical ``Id`` the
    manager resolved our name to — the unit an operator pointed at our name with
    ``Alias=kirocrew.service`` in their own unit's ``[Install]`` section, or a
    ``kirocrew.service -> other.service`` symlink in a unit directory.
    """

    scope: str
    unit_id: str


def alias_holder() -> AliasHolder | None:
    """Return the scope that holds ``kirocrew.service`` as an alias of another
    unit, or ``None`` when no scope does.

    The question :func:`is_active` / :func:`is_up` / :func:`stop` / :func:`restart`
    cannot answer and callers that fall back from them need: those predicates
    fail CLOSED on an alias (:attr:`_UnitState.ours` is False, so the unit is
    never selected or counted), which is correct for "do not act on it" but
    leaves a caller unable to tell "no managed unit here" apart from "the name is
    an alias of a unit a manager supervises". ``kirocrew stop`` / ``kirocrew
    restart`` ask this before their foreground-gateway fallback so they refuse —
    exit 1, nothing signalled, nothing spawned — rather than SIGTERM the alias
    target's process (which its manager restarts at once) and spawn a competitor
    for the port and the ``KIROCREW_HOME`` lock. :func:`uninstall` already refuses
    an alias whole and :func:`status` already names it; this is the same fact
    offered to the two verbs that fall through to signalling.

    System scope is checked first — the same order :func:`status` and
    :func:`uninstall` report in — and the first scope that is an alias is
    returned, since a refusal needs only one holder to name. ``show`` needs no
    sudo, so both scopes use the unprivileged path.

    An alias whose target is stopped (:attr:`_UnitState.running` is False —
    ``ActiveState`` is ``inactive`` or ``failed``) is NOT reported: there is no
    process the alias's manager is supervising, so the caller's SIGTERM would not
    land on another unit's process, and refusing would misdirect — the printed
    ``systemctl`` remedy points at a unit that is already down while the real
    foreground gateway the operator means to stop keeps running. ``running``
    fails CLOSED on an indeterminate ``ActiveState`` (empty, or a value a newer
    systemd adds), so an alias we cannot confirm is stopped still refuses.
    """
    for user in (False, True):
        state = _unit_state(user=user)
        if state.is_alias and state.running:
            return AliasHolder(state.scope, state.unit_id)
    return None


def stop() -> None:
    """Stop the service in every scope where it runs, without disabling it."""
    for user in _running_scopes():
        _systemctl("stop", f"{SERVICE_NAME}.service", user=user)


# How long a unit the manager just restarted must stay up before the restart is
# reported as having taken, and how often its state is re-read meanwhile. The
# unit this module renders is ``Type=simple``, whose start job completes the
# moment the process is forked: ``systemctl restart`` exits 0 for a gateway that
# dies on its first instruction — a ``203/EXEC`` crash loop included — and only
# the state read AFTER the fork tells the two apart. A failed one is in
# ``activating (auto-restart)`` (or ``failed``, once the start limit is hit)
# within milliseconds of the fork and stays there for ``RestartSec=10``; a live
# one is still ``active (running)`` at the end of the window. Two seconds cover
# an exec failure and a Python import error with margin; a gateway that dies
# later than that is the supervisor's to report, and `kirocrew service status`
# / `kirocrew logs` are where it shows.
#
# A unit whose ``RestartSec`` is shorter than the read interval can die and be
# ``active`` again between two reads, so the state alone can step over the
# death. Each read therefore also carries ``ExecMainStartTimestampMonotonic``:
# the manager stamps it at every exec of the main process, so a value that
# changes inside the window is a restart the reads never saw in progress.
_RESTART_SETTLE_SECS = 2.0
_RESTART_POLL_SECS = 0.25


def _journal_hint(*, user: bool) -> str:
    """The journal read that shows why THIS scope's unit exits — the remedy for a
    unit that does not stay up. Scope-specific on purpose: with both scopes
    running, `kirocrew logs` picks the user unit's journal whenever that unit is
    running, which is the healthy sibling's when the system unit is the one
    flapping."""
    unit = f"{SERVICE_NAME}.service"
    if user:
        return f"journalctl --user -u {unit} -n 50 --no-pager"
    return f"sudo journalctl -u {unit} -n 50 --no-pager"


def _confirm_up(*, user: bool) -> tuple[str, str] | None:
    """After a ``restart`` the manager ran: ``None`` once the unit is up at the
    end of the settle window, else ``(kind, reason)`` — the unit's real
    ``ActiveState (SubState)`` and ``Result`` — the moment that is known.

    A unit seen in ``auto-restart``, ``failed``, ``inactive`` or ``deactivating``
    inside the window has already died: there is nothing to wait for
    (:data:`RESTART_NOT_UP`). Any other ``activating`` sub-state (``start``,
    ``start-pre``, …) is a unit still coming up, so the loop keeps reading until
    the deadline and reports what it finds there. A manager that stops answering
    mid-window leaves the unit's health UNKNOWN — not "exiting", not "restarted"
    — and is reported as :data:`RESTART_UNCONFIRMED`.

    A main-process start stamp that changes between two reads is a death and a
    re-exec the state reads fell between (a short ``RestartSec``), reported as
    :data:`RESTART_NOT_UP` whatever state the later read shows. A manager that
    answers no stamp (``0`` or absent) leaves this check off.
    """
    unit = f"{SERVICE_NAME}.service"
    deadline = time.monotonic() + _RESTART_SETTLE_SECS
    first_start = ""
    while True:
        state = _unit_state(user=user)
        if not state.reachable:
            return (
                RESTART_UNCONFIRMED,
                f"the {state.scope} manager stopped answering after the restart "
                f"({state.error}); whether {unit} is up could not be read",
            )
        last = f" (last result: {state.result})" if state.result else ""
        if not state.running or state.sub == "auto-restart" or state.active == "deactivating":
            return (
                RESTART_NOT_UP,
                f"{unit} is {state.active} ({state.sub}){last} after the restart — "
                f"the gateway exits as soon as it starts",
            )
        started = state.started if state.started not in ("", "0") else ""
        if started and not first_start:
            first_start = started
        elif started and started != first_start:
            return (
                RESTART_NOT_UP,
                f"{unit} exited and was started again by the manager after the "
                f"restart (now {state.active} ({state.sub}){last}) — the gateway "
                f"exits as soon as it starts",
            )
        if time.monotonic() >= deadline:
            if state.up:
                return None
            return (
                RESTART_NOT_UP,
                f"{unit} is still {state.active} ({state.sub}){last} "
                f"{_RESTART_SETTLE_SECS:g}s after the restart",
            )
        time.sleep(_RESTART_POLL_SECS)


def _restart_scope(*, user: bool) -> ScopeRestart:
    """``systemctl restart`` the unit in one scope and confirm it came back up.

    A non-zero exit is classified by the unit's state right after it, never by
    the exit code alone: ``systemctl restart`` exits non-zero both when the
    manager did not run the job (an unprivileged caller and a system unit —
    "Interactive authentication required" — or a bus this shell cannot reach)
    and when it ran the job and the job FAILED (a start limit already hit, an
    ``ExecStartPre=`` that exits non-zero, a ``Type=notify`` unit that never
    signalled). The first leaves the unit as it was — still up, or unreadable —
    and is a REFUSAL, remedied by the same restart with the right privilege from
    the right shell: ``sudo systemctl restart kirocrew`` for the system unit,
    ``systemctl --user restart kirocrew`` for the user unit, never the other
    one (under ``sudo`` the user command addresses root's manager and fails
    with ``Unit kirocrew.service not found``). The second leaves the unit
    ``failed``, ``inactive`` or in ``activating (auto-restart)``: the gateway is
    NOT UP, a hand-run restart fails the same way, and the remedy is its
    journal. A zero exit is only the manager's word that the restart job ran;
    :func:`_confirm_up` decides whether the gateway is up.
    """
    unit = f"{SERVICE_NAME}.service"
    scope = "user" if user else "system"
    spelled = "systemctl --user" if user else "sudo systemctl"
    restart_hint = user_restart_command_hint() if user else system_restart_command_hint()
    res = _systemctl("restart", unit, user=user)
    if res.returncode != 0:
        diag = _first_line(res)
        after = _unit_state(user=user)
        if not after.reachable or after.up:
            return ScopeRestart(
                scope,
                False,
                reason=f"the {scope} manager refused the restart: {diag}",
                kind=RESTART_REFUSED,
                hint=restart_hint,
            )
        last = f" (last result: {after.result})" if after.result else ""
        return ScopeRestart(
            scope,
            False,
            reason=(
                f"`{spelled} restart {unit}` exited {res.returncode} ({diag}); "
                f"{unit} is {after.active} ({after.sub}){last}"
            ),
            kind=RESTART_NOT_UP,
            hint=_journal_hint(user=user),
        )
    outcome = _confirm_up(user=user)
    if outcome is None:
        return ScopeRestart(scope, True)
    kind, reason = outcome
    hint = _journal_hint(user=user) if kind == RESTART_NOT_UP else "kirocrew service status"
    return ScopeRestart(scope, False, reason=reason, kind=kind, hint=hint)


def restart() -> RestartReport:
    """Restart the service in every scope where it runs and confirm each came up.

    Single ``systemctl restart`` call per scope rather than ``stop`` + ``start``
    — smaller down-window, and the supervisor stays in charge of the lifecycle
    the whole time. ``Restart=always`` semantics in the unit are unaffected:
    ``systemctl restart`` is an explicit operator action, so the manager honors
    it regardless of restart policy.

    The report is per scope (:class:`RestartReport`), and ``ok`` only when every
    restarted unit is UP — ``active`` or ``reloading``, what ``systemctl
    is-active`` exits 0 for — at the end of the settle window, not when ``systemctl``
    exited 0: for the ``Type=simple`` unit this module renders that exit says
    the process was forked, not that it lived, so a crash-looping gateway
    restarted into ``activating (auto-restart)`` would otherwise read as
    restarted. Each failure carries its kind: REFUSED (the manager did not run
    the job — unit unchanged; remedy: that scope's own restart command), NOT UP
    (the job ran, the gateway is not up; remedy: that scope's journal), or
    UNCONFIRMED (the manager stopped answering; remedy: `service status`). A
    unit running in no scope at all is an empty report (``attempted`` False):
    nothing was restarted — and a stopped, still-installed unit in the other
    scope is never started on the side, which selecting on "installed" instead
    of "running" would do.
    """
    scopes = _running_scopes()
    # Every scope gets its restart before the results are combined: stopping at
    # the first refusal would leave the other scope's gateway untouched while
    # reporting a failed restart.
    return RestartReport(tuple(_restart_scope(user=user) for user in scopes))


def status() -> str:
    """Return a human-readable status report covering BOTH systemd scopes.

    One headline per scope — ``system scope: …`` then ``user scope: …`` — stating
    ``not installed``, ``not reachable from this shell (…)``, or the unit's
    ``ActiveState (SubState)``, followed by the ``systemctl status`` block for
    each scope that actually has a unit. A scope with no unit never shows
    systemd's ``inactive (dead)`` for it: that line, printed for the system
    scope alone, is what makes a running user-scope gateway read as dead. A
    loaded unit of ours whose manager-loaded ``RestartPreventExitStatus`` does
    not name :data:`LIVE_HOLDER_EXIT_CODE` gets one more line under its headline
    naming the directive and the command that adds it (:func:`_stale_unit_line`);
    nothing is rewritten.

    Status is queryable without sudo. We avoid sudo here so
    ``kirocrew service status`` doesn't prompt for a password just to
    show whether the service is up.
    """
    sections: list[str] = []
    for user in (False, True):
        state = _unit_state(user=user)
        lines = [state.headline()]
        stale = _stale_unit_line(state)
        if stale is not None:
            lines.append(stale)
        if state.installed:
            res = _systemctl(
                "status", f"{SERVICE_NAME}.service", "--no-pager", sudo=False, user=user
            )
            block = (res.stdout or res.stderr).rstrip("\n")
            if block:
                lines.append(block)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


# This module is the facade of the Linux service owners, and every name is reached through
# it as ``kiro_crew.service.linux.<name>``: a read is served by the owner that binds the
# name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner holding
# it (:mod:`kiro_crew._facade_mirror`). Owners never import this module, and each imports
# only the owners listed before it in ``_OWNERS``. New work goes to the owner of its
# responsibility:
#
#   ``linux_unit`` -- The unit and overrides paths, the service account, and render_unit.
#   ``linux_systemctl`` -- Escalation, systemctl in either scope, one scope's unit state.
#   ``linux_install`` -- service install: privileged writes, the SELinux refusal, AppArmor.
#   ``linux_uninstall`` -- service uninstall: which unit is ours, and its scope teardown.
#
# This module keeps the verbs that read and drive an installed unit: the user-scope
# queries, is_active, is_up, alias_holder, stop, restart and status. Its docstring
# describes the whole family: "this file" there, and its sudo scope, cover what the
# owners escalate.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.service.linux_unit",
    "kiro_crew.service.linux_systemctl",
    "kiro_crew.service.linux_install",
    "kiro_crew.service.linux_uninstall",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.service.linux_install import (  # noqa: F401
        _PROBE_CWD,
        _env_file_is_untouched_seed,
        _install_file_via_sudo,
        _seed_env_file,
        _sudo_capture,
        _sudo_run_checked,
        _user_scope_remedy,
        _write_unit_via_sudo,
        install,
        install_apparmor_profile,
        install_launcher_profile,
        log,
        remove_apparmor_profile,
        remove_launcher_profile,
        selinux_refusal,
        selinux_start_failure_hint,
    )
    from kiro_crew.service.linux_systemctl import (  # noqa: F401
        _SHOW_PROPERTIES,
        _STOPPED_STATES,
        _UP_STATES,
        ServiceInstallError,
        _loaded_restart_prevent,
        _privilege_prefix,
        _require_privilege,
        _sudo_run,
        _UnitState,
        _user_scope_unreachable_reason,
    )
    from kiro_crew.service.linux_uninstall import (  # noqa: F401
        _SYSTEMD_BOOTED_DIR,
        MANAGED_MARKER_LINE,
        OwnedUnit,
        UninstallReport,
        _finish_scope,
        _installer_unit_path,
        _is_mask_of_our_name,
        _not_ours,
        _owned_unit,
        _privilege_refusal,
        _remove_owned,
        _remove_stale_owned,
        _scope_finished,
        _ScopeTeardown,
        _stop_and_disable,
        _teardown_owned,
        _teardown_refusal,
        _teardown_scope,
        uninstall,
        unit_file_carries_managed_marker,
    )
    from kiro_crew.service.linux_unit import (  # noqa: F401
        _ENV_FILE_TEMPLATE,
        ENV_DIR,
        ENV_FILE_PATH,
        UNIT_PATH,
        USER_UNIT_SUBDIR,
        _current_group,
        _current_uid,
        _current_user,
        _home_for_user,
        _linger_enabled,
        _sd_quote,
        linger_warning_for_service_account,
        render_unit,
        user_unit_file_path,
    )
if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    import logging  # noqa: F401
    import os  # noqa: F401
    import shlex  # noqa: F401
    import shutil  # noqa: F401
    import subprocess  # noqa: F401
    import sys  # noqa: F401
    import tempfile  # noqa: F401

    from kiro_crew.gateway_lock import (  # noqa: F401
        LIVE_HOLDER_EXIT_CODE,
    )
    from kiro_crew.gateway_shutdown_budget import (  # noqa: F401
        TOTAL_SHUTDOWN_BUDGET_SECS,
    )
    from kiro_crew.platform_compat import (  # noqa: F401
        trusted_system_bin,
    )
    from kiro_crew.service import (  # noqa: F401
        apparmor,
        selinux,
    )
    from kiro_crew.service.common import (  # noqa: F401
        kirocrew_bin,
        service_environment,
        systemctl_user_env,
        systemd_quote,
    )
    from kiro_crew.subprocess_utf8 import (  # noqa: F401
        UTF8_TEXT,
    )
