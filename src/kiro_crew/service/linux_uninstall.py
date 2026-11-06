"""``kirocrew service uninstall`` on Linux: which unit is Kiro Crew's, and its teardown.

One ownership decision (:func:`_owned_unit`) per scope, and the teardown arms that
act only on what it claims: stop, disable, verify, remove, ``daemon-reload``
(:func:`_teardown_scope`), reported per scope in an :class:`UninstallReport`.
Imports :mod:`kiro_crew.service.linux_unit`,
:mod:`kiro_crew.service.linux_systemctl` and
:mod:`kiro_crew.service.linux_install`.

Part of the :mod:`kiro_crew.service.linux` family: callers and tests reach every name
through that facade, which keeps the module's patch seams whole
(:mod:`kiro_crew._facade_mirror`). In the docstrings and comments below, "this module"
means that family and "the module docstring" means the facade's, which carries the
systemd 219 commitment and the sudo scope.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.service.common import SERVICE_NAME
from kiro_crew.service.linux_install import _env_file_is_untouched_seed
from kiro_crew.service.linux_systemctl import (
    ServiceInstallError,
    _require_privilege,
    _sudo_run,
    _systemctl,
    _unit_state,
    _UnitState,
)
from kiro_crew.service.linux_unit import ENV_DIR, ENV_FILE_PATH, UNIT_PATH, user_unit_file_path


@dataclass(frozen=True)
class UninstallReport:
    """What :func:`uninstall` did, per scope, for the controller to print.

    ``system`` and ``user`` are one line per scope (``removed (…)``, ``not
    installed``, ``left in place (<why>)``, …), so the operator reads which scope
    was touched. The controller reads the two sets, never a line's wording:

    - ``removed``: the unit is gone from that scope's manager (file or link
      unlinked, or a fileless unit stopped and ``daemon-reload``ed away).
    - ``unfinished``: a teardown was attempted and did not finish. That covers a
      refused ``stop``/``disable``; a unit still running, or unreadable, after
      ``stop``; no ``sudo`` for the system scope, or its manager unreachable while
      ``/run/systemd/system`` says one runs; a refusal (alias, unverified ``Id``,
      running non-``loaded`` unit) that leaves a running unit or the file at
      ``UNIT_PATH`` behind; a failed unlink after a stop; and a rejected closing
      ``daemon-reload`` (the manager still holds the old definition).

    A refusal that leaves nothing running and nothing of ours on disk finishes
    the scope. Failures are reported, not raised: by then the other scope may be
    torn down, and an exception would hide that and skip the AppArmor profile
    removal that follows.
    """

    system: str
    user: str
    unfinished: frozenset[str] = frozenset()
    removed: frozenset[str] = frozenset()

    @property
    def incomplete(self) -> bool:
        """At least one attempted teardown did not finish; the exit code says so."""
        return bool(self.unfinished)

    @property
    def removed_any(self) -> bool:
        """A unit is gone from at least one scope's manager — the structured
        outcome, not a line prefix: the fileless system unit that was stopped and
        ``daemon-reload``ed reads ``stopped (…)`` and counts, a ``left in place``
        line never does."""
        return bool(self.removed)


@dataclass(frozen=True)
class _ScopeTeardown:
    """One scope's outcome inside :func:`uninstall`: the report line, whether the
    teardown finished (see :class:`UninstallReport`), and whether the unit is
    gone from that scope's manager."""

    line: str
    finished: bool = True
    removed: bool = False


def _first_line(res: subprocess.CompletedProcess[str]) -> str:
    """The first line of a failed command's diagnostic, for a report line."""
    detail = (res.stderr or res.stdout or "").strip().splitlines()
    return detail[0] if detail else f"exited {res.returncode} without output"


def _stop_and_disable(*, user: bool, file_present: bool = True) -> str | None:
    """``stop`` then ``disable`` the unit in one scope, then confirm it stopped;
    the ``left in place (…)`` report line when any step did not take, ``None``
    when the unit is verified stopped.

    Checked, unlike the best-effort ``stop()`` / ``restart()``: the next step
    deletes the unit file, and unlinking a unit whose stop was refused
    (``RefuseManualStop=yes``, a lost bus, a declined sudo) would leave a running
    gateway with no unit to find it by. So any failed step keeps the file. The
    final ``ActiveState`` re-read, not ``stop``'s exit 0, is the proof it stopped.

    ``file_present=False`` is a unit still loaded after its file was deleted:
    ``disable`` needs the file's ``[Install]`` section and systemd refuses it, so
    ``stop`` runs alone.
    """
    unit = f"{SERVICE_NAME}.service"
    spelled = "systemctl --user" if user else "sudo systemctl"
    kept = "the unit file was not removed" if file_present else "the unit is still loaded"
    for verb in ("stop", "disable") if file_present else ("stop",):
        res = _systemctl(verb, unit, user=user)
        if res.returncode != 0:
            return f"left in place (`{spelled} {verb} {unit}` failed: {_first_line(res)}; {kept})"
    after = _unit_state(user=user)
    if not after.reachable:
        # No answer is not "not running": the unlink rests on the manager's
        # word that the unit is stopped, and a manager that went away between
        # `stop` and this read (a session ending, a bus that closed) has given
        # none. The file stays; the operator re-runs from a shell that reaches it.
        return (
            f"left in place (the {after.scope} manager stopped answering after `{spelled} "
            f"stop {unit}`: {after.error}; whether the unit stopped could not be confirmed, "
            f"so {kept})"
        )
    if after.running:
        return (
            f"left in place (still {after.active} ({after.sub}) after `{spelled} stop "
            f"{unit}` returned 0; stop it by hand, then run `kirocrew service uninstall` "
            f"again)"
        )
    return None


def _teardown_refusal(state: _UnitState) -> str | None:
    """Why a scope's unit must not be touched at all — not stopped, not disabled,
    not unlinked — or ``None`` when the teardown may proceed.

    - An ALIAS: every verb on our name would act on the unit it resolves to.
    - No canonical ``Id`` (:attr:`_UnitState.ours` fails closed): nothing verified.
    - RUNNING under a load state other than ``loaded`` (runtime mask, unparseable
      edit, file removed under it): ``FragmentPath`` is the mask or empty, so
      following it deletes the wrong thing or nothing, and ``disable`` fails.

    Each is handed to the operator with the step that makes the unit removable.
    """
    unit = f"{SERVICE_NAME}.service"
    if state.is_alias:
        return f"left in place ({unit} is an alias of {state.unit_id}; manage that unit)"
    if not state.ours:
        # Reachable, yet no canonical `Id` in the answer: the identity every
        # verb below would act on is unverified, and an unverified name is not
        # ours to stop, disable or unlink.
        return (
            f"left in place (the manager did not report {unit}'s canonical Id, so the "
            f"unit's identity could not be verified; inspect it with `systemctl show {unit}`)"
        )
    if state.running and state.load != "loaded":
        how = "unmask and stop it first" if state.load == "masked" else "stop it first"
        return (
            f"left in place (an {state.active} unit whose load state is {state.load}: "
            f"{how}, then run `kirocrew service uninstall` again)"
        )
    return None


# systemd as the init system leaves this directory behind for exactly this
# question (sd_booted(3) checks nothing else). A host without it runs no system
# manager, so no unit can be running there — the one case in which a unit file
# is removed without the manager's word that it is stopped.
_SYSTEMD_BOOTED_DIR = Path("/run/systemd/system")


# The one line every unit this module renders carries (``render_unit``, both
# scopes): the marker ``kirocrew doctor`` reads for managed-service policy, and
# the marker :func:`_owned_unit` reads for ownership. Rendered form, so the
# reading is a line comparison and not a parse of the unit file.
MANAGED_MARKER_LINE = 'Environment="KIROCREW_SERVICE_MANAGED=1"'


def unit_file_carries_managed_marker(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` carries :data:`MANAGED_MARKER_LINE`. An absent, unreadable
    or undecodable file is ``False``: the marker is a claim the file makes, and a
    file that cannot make it is not taken to have made it."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return False
    return any(line.strip() == MANAGED_MARKER_LINE for line in lines)


def _installer_unit_path(*, user: bool) -> str:
    """The one path the installer writes a unit file to in each scope: the
    system unit at :data:`UNIT_PATH`, the per-user unit the SELinux remedy
    stands up at :func:`user_unit_file_path`."""
    return str(user_unit_file_path()) if user else str(UNIT_PATH)


@dataclass(frozen=True)
class OwnedUnit:
    """A unit Kiro Crew owns in one scope, and what its teardown may touch.

    ``fragment`` is the path the decision was made from — the manager-reported
    ``FragmentPath``, or the installer's own path when the manager has nothing
    loaded there. ``definition`` is the file that holds the unit: ``fragment``
    itself for a DIRECT unit, the link's resolved source for a LINKED one.
    ``links`` are the link entries a linked unit is removed by — the fragment
    when it is itself a symlink, the installer's own path when that entry is a
    symlink resolving to the fragment — and a linked unit's ``definition`` is
    never touched: removing the link is what uninstalls it, and the source
    lives where the operator put it. ``mask`` is a MASK of our unit name — the
    symlink to ``/dev/null`` that ``systemctl mask`` writes at exactly the
    installer's own path: it holds no definition, ``definition`` is the mask
    entry itself, and removing that entry is what unmasks the name.
    """

    scope: str
    fragment: str
    definition: str
    links: tuple[str, ...] = ()
    mask: bool = False

    @property
    def linked(self) -> bool:
        return bool(self.links)


def _owned_unit(fragment: str, *, user: bool) -> OwnedUnit | None:
    """The ONE ownership decision every teardown arm rests on: the unit whose
    definition sits at ``fragment`` is Kiro Crew's, or ``None`` and nothing —
    not ``stop``, not ``disable``, not an unlink — is done to it.

    Ours when any of these holds:

    - ``fragment`` is a symlink: a LINKED unit whose definition is its source.
    - The installer's own path is a symlink resolving to ``fragment`` (the loader
      reports a link from a higher-priority directory by its target), or a plain
      file there: the unit the installer wrote, marker or not (older installs).
    - The definition carries :data:`MANAGED_MARKER_LINE`, so a copy an operator
      placed or linked elsewhere is still ours.
    - A mask of our name: ``systemctl mask`` writes a ``/dev/null`` symlink at
      exactly the installer's path; unlinking it unmasks the name. A mask
      elsewhere (``--runtime`` under ``/run``) is not claimed.

    Anything else (a distribution unit under ``/usr/lib``, an operator's unit
    linked under our name, a file with another unit's name, an unreadable file)
    is not ours. Nothing downstream
    re-decides ownership.
    """
    unit = f"{SERVICE_NAME}.service"
    if not fragment or os.path.basename(fragment) != unit:
        return None
    own_path = _installer_unit_path(user=user)
    scope = "user" if user else "system"
    if _is_mask_of_our_name(fragment, own_path):
        return OwnedUnit(scope, fragment, fragment, (), mask=True)
    links: list[str] = []
    definition = fragment
    if os.path.islink(fragment):
        links.append(fragment)
        definition = os.path.realpath(fragment)
    elif os.path.islink(own_path) and os.path.realpath(own_path) == os.path.realpath(fragment):
        links.append(own_path)
    by_path = not links and os.path.normpath(definition) == os.path.normpath(own_path)
    if not by_path and not unit_file_carries_managed_marker(definition):
        return None
    return OwnedUnit(scope, fragment, definition, tuple(links))


def _is_mask_of_our_name(path: str, own_path: str) -> bool:
    """Whether ``path`` is the mask ``systemctl mask`` leaves for our unit name:
    the installer's own path (``own_path``) itself, a symlink, resolving to
    ``/dev/null``. Exact path, not the directory — a mask is placed by name at
    the top-priority path, which is the one the installer writes."""
    return (
        os.path.normpath(path) == os.path.normpath(own_path)
        and os.path.islink(path)
        and os.path.realpath(path) == os.path.realpath(os.devnull)
    )


def _not_ours(fragment: str, *, user: bool) -> _ScopeTeardown:
    """The report line for a unit under our name that :func:`_owned_unit` did
    not claim: a finished scope, nothing removed, nothing stopped."""
    own_path = _installer_unit_path(user=user)
    return _ScopeTeardown(
        f"left untouched (not installed by Kiro Crew: {fragment} is not the unit file written "
        f"at {own_path} and carries no {MANAGED_MARKER_LINE} line; manage it with systemctl)"
    )


def _remove_owned(owned: OwnedUnit, *, after_verbs: bool) -> str | None:
    """Remove what :class:`OwnedUnit` says may go — its link entries, or its
    direct file — as the calling user in the user scope and under sudo in the
    system scope; the report line when a removal failed, ``None`` when
    everything is gone. ``rm -f`` and ``os.unlink`` on a symlink remove the
    entry alone, so a linked unit's definition is never reached. A path already
    gone (``disable`` removes a link entry it created) is not a failure.
    """
    user = owned.scope == "user"
    reload_cmd = "systemctl --user daemon-reload" if user else "sudo systemctl daemon-reload"
    prefix = "stopped and disabled, but " if after_verbs else ""
    for path in owned.links if owned.linked else (owned.definition,):
        if user:
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                failure = str(exc)
            else:
                continue
        else:
            res = _sudo_run("rm", "-f", path)
            if res.returncode == 0:
                continue
            failure = _first_line(res)
        if owned.linked:
            return (
                f"{prefix}the link {path} to its unit file {owned.definition} could not be "
                f"removed ({failure}); remove the link by hand — not the file it points at — "
                f"then run `{reload_cmd}`"
            )
        if owned.mask:
            unmask_cmd = "systemctl --user unmask" if user else "sudo systemctl unmask"
            return (
                f"{prefix}the mask {path} could not be removed ({failure}); run "
                f"`{unmask_cmd} {SERVICE_NAME}.service` by hand"
            )
        return (
            f"{prefix}its unit file {path} could not be removed ({failure}); remove it by "
            f"hand, then run `{reload_cmd}`"
        )
    return None


def _privilege_refusal(owned: OwnedUnit) -> _ScopeTeardown | None:
    """The system scope needs sudo for its verbs and its ``rm``; reported, not
    raised, so the user scope — the one this account CAN tear down — is still
    reached and both lines are printed."""
    if owned.scope == "user":
        return None
    try:
        _require_privilege()
    except ServiceInstallError as exc:
        return _ScopeTeardown(f"left in place (privilege unavailable: {exc})", finished=False)
    return None


def _teardown_owned(owned: OwnedUnit, state: _UnitState) -> _ScopeTeardown:
    """Tear down a LOADED unit :func:`_owned_unit` claimed: stop → disable →
    verify inactive (:func:`_stop_and_disable`) → remove (:func:`_remove_owned`)
    → ``daemon-reload``. A step the manager refuses ends the scope with the
    file in place and the scope unfinished; a unit whose file was already
    deleted under it is stopped alone (nothing to disable or unlink) and the
    ``daemon-reload`` makes it ``not-found``, which counts as removed.
    """
    refused = _privilege_refusal(owned)
    if refused is not None:
        return refused
    user = owned.scope == "user"
    present = os.path.lexists(owned.fragment)
    line = _stop_and_disable(user=user, file_present=present)
    if line is not None:
        return _ScopeTeardown(line, finished=False)
    if present:
        failure = _remove_owned(owned, after_verbs=True)
        if failure is not None:
            return _ScopeTeardown(failure, finished=False)
    reload_failure = _finish_scope(owned)
    if owned.linked:
        line = (
            f"removed (the link to {owned.definition}; the linked unit file {owned.definition} "
            f"itself was kept)"
        )
    elif not present:
        reloaded = "" if reload_failure is not None else "; daemon-reload run"
        line = (
            f"stopped (its unit file {owned.definition} was already gone, so nothing was disabled "
            f"or unlinked{reloaded})"
        )
    else:
        line = f"removed ({owned.definition})"
    return _scope_finished(line, reload_failure)


def _remove_stale_owned(owned: OwnedUnit) -> _ScopeTeardown:
    """Remove a unit :func:`_owned_unit` claimed that the manager does not have
    LOADED and that runs nothing — a file dropped without a ``daemon-reload``,
    an edit that fails to parse, the file left behind on a host with no
    manager, the mask of our name — without the verbs: there is no loaded unit
    to stop or disable, and the ``daemon-reload`` afterwards is what makes the
    manager forget it (or, for a mask, see the name unmasked).
    """
    refused = _privilege_refusal(owned)
    if refused is not None:
        return refused
    failure = _remove_owned(owned, after_verbs=False)
    if failure is not None:
        return _ScopeTeardown(failure, finished=False)
    reload_failure = _finish_scope(owned)
    if owned.linked:
        line = (
            f"removed (the link to {owned.definition}; the linked unit file {owned.definition} "
            f"itself was kept)"
        )
    elif owned.mask:
        line = f"removed (the mask {owned.definition}; {SERVICE_NAME}.service is unmasked)"
    else:
        line = f"removed ({owned.definition})"
    return _scope_finished(line, reload_failure)


def _scope_finished(line: str, reload_failure: str | None) -> _ScopeTeardown:
    """A scope whose unit file is gone: removed when the closing
    ``daemon-reload`` took; unfinished — and NOT removed — when the manager
    refused it, since the manager still holds the old definition until someone
    reloads it by hand, so the headline must not read ``stopped and removed``."""
    if reload_failure is not None:
        return _ScopeTeardown(f"{line}, but {reload_failure}", finished=False)
    return _ScopeTeardown(line, removed=True)


def _finish_scope(owned: OwnedUnit) -> str | None:
    """The steps after a removal: the system scope's untouched overrides seed
    goes with the unit, and the scope's manager is told to reload.

    Returns ``None`` on success, or a diagnostic string when the
    ``daemon-reload`` exits non-zero — the manager still holds the old
    definition until someone reloads it by hand. On a host not booted with
    systemd (no ``/run/systemd/system``) there is no system manager, so a
    failed system-scope reload is not a failure.
    """
    if owned.scope == "system" and _env_file_is_untouched_seed():
        # Only when it still holds our untouched seed — proving both that we
        # wrote it and that the operator never edited it. An operator-authored
        # or -edited /etc/kirocrew/kirocrew.env (including one pre-provisioned
        # before install, which _seed_env_file preserves) is their config. rmdir
        # is best-effort and only clears an empty dir.
        _sudo_run("rm", "-f", str(ENV_FILE_PATH))
        _sudo_run("rmdir", str(ENV_DIR))
    user = owned.scope == "user"
    res = _systemctl("daemon-reload", user=user)
    if res.returncode != 0 and (user or _SYSTEMD_BOOTED_DIR.is_dir()):
        # A host not booted with systemd has no system manager to hold the old
        # definition, so the reload failing there leaves nothing behind.
        reload_cmd = "systemctl --user daemon-reload" if user else "sudo systemctl daemon-reload"
        return f"`{reload_cmd}` failed: {_first_line(res)}; run it by hand"
    return None


def _teardown_scope(*, user: bool) -> _ScopeTeardown:
    """One scope of :func:`uninstall`, decided in this order: can the manager
    be reached; does it refuse the name whole (:func:`_teardown_refusal`); is
    the unit ours (:func:`_owned_unit`) — the only question the verbs then rest
    on; and finally the teardown itself. The same order in both scopes; the
    system scope adds sudo for its verbs and the one case in which a file goes
    without the manager's word — a host not booted with systemd, where no unit
    can be running.
    """
    own_path = _installer_unit_path(user=user)
    entry = os.path.lexists(own_path)
    state = _unit_state(user=user)
    if not state.reachable:
        if user:
            return _ScopeTeardown(f"not reachable from this shell ({state.error})")
        if not _SYSTEMD_BOOTED_DIR.is_dir():
            # No manager can exist here (sd_booted(3)'s test), so nothing runs
            # under any unit; a stale file the installer left behind — its own
            # daemon-reload failed in a container where systemd is not PID 1 —
            # is removed on ownership alone.
            if not entry:
                return _ScopeTeardown("not installed")
            owned = _owned_unit(own_path, user=False)
            return (
                _remove_stale_owned(owned) if owned is not None else _not_ours(own_path, user=False)
            )
        if entry:
            # A manager runs that this shell cannot see, and it may well run
            # the unit: the file stays, unfinished, for a shell that reaches it.
            return _ScopeTeardown(
                f"left in place (the system manager is not reachable from this shell: "
                f"{state.error}; whether the unit is running cannot be confirmed from "
                f"here, so its file was not removed — run this from a host shell)",
                finished=False,
            )
        return _ScopeTeardown(f"not reachable from this shell ({state.error})")
    refusal = _teardown_refusal(state)
    if refusal is not None:
        # Unfinished when something is left behind for the operator: a unit
        # that keeps running, or the entry at the path this module writes to.
        return _ScopeTeardown(refusal, finished=not (state.running or (not user and entry)))
    if state.load == "loaded":
        owned = _owned_unit(state.fragment, user=user)
        if owned is None:
            return _not_ours(state.fragment, user=user)
        return _teardown_owned(owned, state)
    # Not loaded, and not running (a running one was refused above): `not-found`,
    # a mask, a unit that failed to parse. An entry at the installer's own path
    # is decided on ownership — a file dropped without a daemon-reload or an
    # unparseable edit of ours is removed, and so is a mask of our name there
    # (`systemctl mask` writes its /dev/null symlink at exactly this path;
    # unlinking it unmasks the name) — and with no entry there is nothing of
    # ours on disk to remove: a mask elsewhere (`--runtime`, under /run) is
    # reported, not touched.
    if entry:
        owned = _owned_unit(own_path, user=user)
        if owned is not None:
            return _remove_stale_owned(owned)
        return _ScopeTeardown(f"left in place (load state {state.load}, unit file {own_path})")
    if not state.installed:
        return _ScopeTeardown("not installed")
    return _ScopeTeardown(
        f"left in place (load state {state.load}, unit file {state.fragment or 'unknown'})"
    )


def uninstall() -> UninstallReport:
    """Stop, disable, and remove the unit from every scope that has one of OURS.
    Idempotent.

    The system unit (under sudo) and the SELinux remedy's per-user unit
    (``systemctl --user``, never sudo) both go through :func:`_teardown_scope`,
    and every verb rests on :func:`_owned_unit`; a unit that is not ours is left
    untouched (exit 0). A linked unit loses only its link entries, a mask of our
    name is unlinked (unmasking it). Order: stop → disable → verify inactive →
    remove → ``daemon-reload``; a refused step keeps the file, and
    :func:`_teardown_refusal` cases are refused before any verb. An unreachable
    manager is reported, never acted on: nothing is deleted on a query that did
    not run.
    """
    system = _teardown_scope(user=False)
    user = _teardown_scope(user=True)
    outcomes = {"system": system, "user": user}
    return UninstallReport(
        system.line,
        user.line,
        unfinished=frozenset(scope for scope, outcome in outcomes.items() if not outcome.finished),
        removed=frozenset(scope for scope, outcome in outcomes.items() if outcome.removed),
    )
