"""Fixtures and fakes shared by ``test_service.py`` and its nine sibling suites.

The siblings are ``test_service_controller.py``, ``test_service_linux_status.py``,
``test_service_linux_uninstall.py``, ``test_service_linux_unit_ownership.py``,
``test_service_macos_control.py``, ``test_service_apparmor.py``,
``test_service_launcher_profile.py``, ``test_service_headless_api_key.py`` and
``test_service_environment.py``. The
other ``test_service_*.py`` files (the SELinux and launchd-domain suites) do not
use this module and keep their own isolation.

A PLAIN module, not a ``conftest.py``: each suite imports what it uses by name.

Each of those ten suites imports the four autouse fixtures, so they apply to every
test in it (their docstrings' "this module" and "here" mean the importing suite).
They are what keeps a service test off the host running it: ``SUDO_USER`` cleared,
the system scope's overrides file and both scopes' unit files pointed under
``tmp_path``, and ``restart()``'s settle window walked on a fake clock.

``durable_dir`` is declared with an explicit ``name=`` so importing it does not
collide with the test parameter of the same name (flake8 F811). ``_fake_systemctl``
and ``_systemctl_stub`` stand in for systemctl; nothing here spawns anything.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew.service.common import SERVICE_NAME


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
def _clear_sudo_user(monkeypatch):
    """Keep ``User=`` resolution deterministic across hosts.

    ``_current_user()`` prefers ``SUDO_USER`` (so ``sudo … service install``
    targets the human, not root). A CI runner that happened to set ``SUDO_USER``
    would otherwise override the ``USER=tester`` these tests set. Clear it once
    for every test in this module; tests that exercise the SUDO_USER path set it
    explicitly themselves.
    """
    monkeypatch.delenv("SUDO_USER", raising=False)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
def _no_host_env_file(monkeypatch, tmp_path):
    """Keep the system scope's overrides file off the host's ``/etc``.

    Uninstall reads ``ENV_FILE_PATH`` directly to decide whether it still holds
    our untouched seed, and issues ``rm``/``rmdir`` if so. On a host where a real
    install left that seed in ``/etc/kirocrew``, every uninstall test here would
    see a file it never wrote. Point both paths at an absent directory; a test
    that needs the file writes its own and re-points them.
    """
    from kiro_crew.service import linux as svc_linux

    env_dir = tmp_path / "absent-etc-kirocrew"
    monkeypatch.setattr(svc_linux, "ENV_DIR", env_dir)
    monkeypatch.setattr(svc_linux, "ENV_FILE_PATH", env_dir / "kirocrew.env")


@pytest.fixture(autouse=True)
def _no_host_unit_files(_floor_monkeypatch, tmp_path, tmp_path_factory):
    """Keep both scopes' unit files off the host running the suite.

    ``install()`` and ``uninstall()`` act on ``UNIT_PATH`` for the system scope and on
    ``user_unit_file_path()`` for the user scope, which resolves against ``Path.home()``.
    A test that re-points only ``UNIT_PATH`` would otherwise reach the user unit of the
    account running the suite, and an uninstall test deletes it. Point both scopes, and
    every ``Path.home()`` lookup, under ``tmp_path``. Anything that still removes or
    replaces a file in the real unit directories fails the test instead of acting. A
    test that needs a unit file writes its own and re-points the path.

    Patched through ``_floor_monkeypatch``, never the shared ``monkeypatch``: a test's
    ``monkeypatch.undo()`` reverts every record on that instance, and would lift the
    home redirection and the guard with it.
    """
    from kiro_crew.service import linux as svc_linux

    monkeypatch = _floor_monkeypatch
    real_dirs = tuple(
        os.path.realpath(d)
        for d in (Path.home() / ".config" / "systemd", svc_linux.UNIT_PATH.parent)
    )
    home = tmp_path_factory.mktemp("service-home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: cls(home)))
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(
        svc_linux, "UNIT_PATH", tmp_path / "absent-etc-systemd" / f"{SERVICE_NAME}.service"
    )

    def _refuse_real_unit_dirs(name):
        act = getattr(os, name)

        def guarded(path, *args, **kwargs):
            for target in (path, *args[:1]):
                resolved = os.path.realpath(os.fspath(target))
                if any(resolved == d or resolved.startswith(d + os.sep) for d in real_dirs):
                    pytest.fail(f"os.{name} would touch a real unit file: {resolved}")
            return act(path, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    for name in ("unlink", "remove", "replace", "rename"):
        _refuse_real_unit_dirs(name)


class _FakeClock:
    """``time`` stand-in for ``linux.restart()``'s settle window.

    ``monotonic()`` returns a clock that advances only through ``sleep()``, so the
    confirm loop walks every poll of its window in order — a stateful fake can
    answer a different unit state to each — and no test waits a real second.
    ``sleeps`` records the waits so a test can assert the window was walked.
    """

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.now += secs


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
def _restart_settle_without_waiting(monkeypatch):
    """Every ``restart()`` here confirms the unit through a fake clock: the
    window (``_RESTART_SETTLE_SECS``) is still walked, poll by poll, but takes
    no wall time. Tests reach the clock as ``svc_linux.time``."""
    from kiro_crew.service import linux as svc_linux

    monkeypatch.setattr(svc_linux, "time", _FakeClock(), raising=False)


_UNIT = f"{SERVICE_NAME}.service"


# A unit file the installer wrote: it carries the managed marker `uninstall`
# decides ownership by, wherever the test puts it.
_OURS_UNIT_TEXT = (
    '[Unit]\nDescription=Kiro Crew gateway\n[Service]\nEnvironment="KIROCREW_SERVICE_MANAGED=1"\n'
)


def _svc_linux():
    from kiro_crew.service import linux as svc_linux

    return svc_linux


_RUNNING = {"ActiveState": "active", "SubState": "running"}


_DEAD = {"ActiveState": "inactive", "SubState": "dead"}


_NO_BUS = "Failed to connect to bus: No medium found"


def _fake_systemctl(
    system=None,
    user=None,
    *,
    user_bus_error=None,
    user_fragment="",
    user_id=_UNIT,
    user_load="loaded",
    system_load="loaded",
    system_id=_UNIT,
    system_fragment=None,
    overrides=None,
    stop_is_ignored=False,
    restart_lands_in=None,
):
    """A ``subprocess.run`` stand-in that answers systemctl per SCOPE and VERB.

    ``system`` / ``user`` describe the unit in that scope: ``None`` is a scope
    with no unit (systemd answers ``LoadState=not-found``), a dict is the
    ``ActiveState`` / ``SubState`` pair of a loaded unit (plus an optional
    ``Result``, ``success`` when absent). ``user_bus_error`` makes
    every ``systemctl --user`` call fail the way an unreachable user manager does
    (exit 1, the diagnostic on stderr, nothing on stdout). ``user_fragment`` is the
    ``FragmentPath`` systemd reports for the user unit, ``user_id`` the canonical
    ``Id`` it resolves the name to (another unit's name when ours is an alias) and
    ``user_load`` / ``system_load`` its ``LoadState`` (``masked`` for a mask, which
    a running unit keeps running under). ``system_fragment`` is the ``FragmentPath``
    reported for the system unit; ``None`` makes it the module's ``UNIT_PATH`` as
    the test left it (the installer's own path), so a test names another location
    only for a unit the installer did not write. ``overrides`` maps a verb to the
    ``CompletedProcess`` it should return instead, for the one-verb-fails cases (a
    restart the manager refuses).

    The fake is stateful the way the manager is: a scope told to ``stop`` (and
    not refused) answers ``inactive (dead)`` to every later ``show`` /
    ``is-active`` / ``status`` -- unless ``stop_is_ignored``, which models a stop
    job that returned 0 while the unit is still up. ``restart_lands_in`` models a
    ``Type=simple`` unit whose ``restart`` exits 0 whatever the forked process
    does next: a dict, or a sequence of dicts answered to successive ``show``
    reads of the restarted scope (the last one sticky), so a unit that reads
    ``active`` once and then ``activating (auto-restart)`` is one entry each.
    A state dict's optional ``Started`` is the ``ExecMainStartTimestampMonotonic``
    the read reports; without it the line is left out, as a manager that does
    not answer the property does.

    Nothing here spawns anything: the whole point of the fixture is that the
    systemd user manager is host state and must never be touched from a test.
    Every argv is recorded on ``run.calls`` so tests can assert scope and sudo.
    """
    verbs = {"show", "status", "is-active", "stop", "disable", "restart", "daemon-reload"}
    landing = (
        list(restart_lands_in)
        if isinstance(restart_lands_in, (list, tuple))
        else ([restart_lands_in] if restart_lands_in is not None else [])
    )

    def run(argv, *_a, **_k):
        tokens = list(argv)
        run.calls.append(tokens)
        user_scope = "--user" in tokens
        verb = next((t for t in tokens if t in verbs), None)
        if user_scope and user_bus_error is not None:
            return subprocess.CompletedProcess(tokens, 1, "", user_bus_error + "\n")
        props = user if user_scope else system
        scope = "user" if user_scope else "system"
        # The landing state describes the unit AFTER the restart attempt, whatever
        # exit the verb is overridden to: a job the manager ran and that failed
        # exits non-zero AND leaves the unit `failed`.
        if verb == "restart" and landing:
            run.restarted.add(scope)
        if overrides and verb in overrides:
            return overrides[verb]
        if verb == "stop" and not stop_is_ignored:
            run.stopped.add(scope)
        if props is not None and scope in run.stopped:
            props = _DEAD
        if props is not None and scope in run.restarted and verb in {"show", "is-active", "status"}:
            props = landing.pop(0) if len(landing) > 1 else landing[0]
        if verb == "show":
            if props is None:
                body = (
                    f"Id={_UNIT}\nLoadState=not-found\nActiveState=inactive\n"
                    "SubState=dead\nFragmentPath=\nResult=success\n"
                )
            else:
                fragment = (
                    user_fragment
                    if user_scope
                    else (system_fragment or str(_svc_linux().UNIT_PATH))
                )
                unit_id = user_id if user_scope else system_id
                load = user_load if user_scope else system_load
                body = (
                    f"Id={unit_id}\nLoadState={load}\nActiveState={props['ActiveState']}\n"
                    f"SubState={props['SubState']}\nFragmentPath={fragment}\n"
                    f"Result={props.get('Result', 'success')}\n"
                )
                if "Started" in props:
                    body += f"ExecMainStartTimestampMonotonic={props['Started']}\n"
            return subprocess.CompletedProcess(tokens, 0, body, "")
        if verb == "is-active":
            state = (props or _DEAD)["ActiveState"]
            return subprocess.CompletedProcess(
                tokens, 0 if state == "active" else 3, state + "\n", ""
            )
        if verb == "status":
            if props is None:
                return subprocess.CompletedProcess(
                    tokens, 4, "", f"Unit {_UNIT} could not be found.\n"
                )
            rc = 0 if props["ActiveState"] == "active" else 3
            block = (
                f"● {_UNIT} - Kiro Crew gateway ({scope} scope block)\n"
                f"     Active: {props['ActiveState']} ({props['SubState']})\n"
            )
            return subprocess.CompletedProcess(tokens, rc, block, "")
        return subprocess.CompletedProcess(tokens, 0, "", "")

    run.calls = []
    run.stopped = set()
    run.restarted = set()
    return run


def _systemctl_stub(unit_path):
    """A ``linux._systemctl`` stand-in for tests that patch the module function
    itself: the system scope holds a LOADED, stopped unit at *unit_path*, the user
    scope holds nothing, and every verb succeeds."""

    def _systemctl(*args, **kwargs):
        if args and args[0] == "show":
            if kwargs.get("user"):
                body = f"Id={_UNIT}\nLoadState=not-found\nActiveState=inactive\nSubState=dead\nFragmentPath=\n"
            else:
                body = (
                    f"Id={_UNIT}\nLoadState=loaded\nActiveState=inactive\nSubState=dead\n"
                    f"FragmentPath={unit_path}\n"
                )
            return subprocess.CompletedProcess(list(args), 0, body, "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    return _systemctl


@pytest.fixture(name="durable_dir")
def durable_dir_fixture(tmp_path, monkeypatch):
    """A ``tmp_path`` that :func:`validate_exec_path` will accept.

    Two checks legitimately refuse a pytest temp directory, and both are refusing
    correctly, so tests exercising the OTHER rules neutralise them rather than
    skipping on the platform this feature ships on:

    * the ``/tmp`` prefix denylist — on Linux ``tmp_path`` lives under ``/tmp``;
    * the mode walk — ``/tmp`` itself is 0o1777, and it is an ancestor.

    ``test_rejects_a_world_writable_location`` and the ``_substitutable_by_others``
    tests deliberately do NOT use this fixture: they assert the refusals.
    """
    from kiro_crew.service import apparmor as aa

    monkeypatch.setattr(aa, "_UNSAFE_EXEC_PARENTS", ())
    monkeypatch.setattr(
        aa, "_substitutable_by_others", lambda _p, expected_uid=None, candidate=None: None
    )
    return tmp_path
