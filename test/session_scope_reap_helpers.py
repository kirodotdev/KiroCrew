"""The fake slice tree, ``/proc`` and recorder the agent-scope reaper tests share.

``test_session_scope_reap.py`` and ``test_session_scope_reap_seams.py`` build their scopes
from these: :func:`_make_proc` and :func:`_make_scope` write fake ``/proc`` entries and
cgroup scopes under ``tmp_path``, :class:`_Recorder` stands in for the systemd and signal
seams, and :func:`_reap` drives ``reap_scopes`` over them at ``_NOW``.
:func:`_pin_boot_clock` and :func:`_pin_tick_rate` pin the boot clock and the tick rate,
:func:`_enter_us_for_age` turns an age into an active-enter stamp, and :class:`_ModuleProxy`
overrides attributes of a module the reaper reads. ``_scope_errors_fail_the_test`` is the
autouse fixture that fails a test in which a scope evaluation or reclaim raised; it is
declared with ``name=``, and a test module applies it by importing
``_scope_errors_fail_the_test_fixture``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import session_scope_reap as r
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE

# now_monotonic reference used across tests; ages are derived from active-enter.
_NOW = 1_000_000.0


@pytest.fixture(name="_scope_errors_fail_the_test", autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_session_scope_reap*.py modules calls monkeypatch.undo()
def _scope_errors_fail_the_test_fixture(request, monkeypatch):
    """Fail any test in which a scope evaluation or reclaim raised, unless it opts in.

    The sweep turns an exception into a skip so one bad scope cannot stop the
    others, which would otherwise let every "skips X" test pass over a crash in
    the very rule it means to pin. A test that provokes an error on purpose
    requests ``allow_scope_errors``.
    """
    errors: list[tuple[str, str, BaseException]] = []
    real = r._warn_scope_error

    def record(unit_name, phase, exc, errored):
        errors.append((unit_name, phase, exc))
        real(unit_name, phase, exc, errored)

    monkeypatch.setattr(r, "_warn_scope_error", record)
    monkeypatch.setattr(r, "_SCOPE_ERRORS_WARNED", {})
    yield errors
    if "allow_scope_errors" not in request.fixturenames:
        assert errors == [], f"a scope raised during the sweep: {errors!r}"


def _pin_tick_rate(monkeypatch, hz: int) -> None:
    """Make ``os.sysconf("SC_CLK_TCK")`` answer *hz*, behind the real guards."""
    real = r.platform_compat.os.sysconf
    monkeypatch.setattr(
        r.platform_compat.os, "sysconf", lambda name: hz if name == "SC_CLK_TCK" else real(name)
    )


def _pin_boot_clock(monkeypatch, *, now: float = 100.0) -> None:
    """Pin the boot clock at *now* and the tick rate at 100 Hz."""
    monkeypatch.setattr(r.platform_compat, "boottime_now", lambda: now)
    _pin_tick_rate(monkeypatch, 100)


def _enter_us_for_age(age_secs: float) -> int:
    return int((_NOW - age_secs) * 1_000_000)


def _make_proc(
    proc_root: Path,
    pid: int,
    *,
    pgrp: int,
    marker: bool = True,
    comm: str = "kiro-cli-chat",
    ppid: int = 1,
    cmdline: bytes | None = None,
    raw_comm: bytes | None = None,
    start_ticks: int = 4242,
) -> None:
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    environ = b"PATH=/usr/bin\x00"
    if marker:
        environ += f"{KIROCREW_SPAWNED_ENV}={KIROCREW_SPAWNED_VALUE}".encode() + b"\x00"
    (d / "environ").write_bytes(environ)
    # /proc/<pid>/stat: "pid (comm) state ppid pgrp ...". After the last ')':
    # index 0 state, 1 ppid, 2 pgrp, ... 19 starttime.
    after = ["S", str(ppid), str(pgrp)] + ["0"] * 16 + [str(start_ticks)]
    # ``raw_comm`` writes the name as the kernel stores it: any bytes a process
    # set through prctl(PR_SET_NAME), valid UTF-8 or not.
    name = raw_comm if raw_comm is not None else comm.encode()
    (d / "stat").write_bytes(f"{pid} (".encode() + name + b") " + " ".join(after).encode())
    (d / "cmdline").write_bytes(cmdline if cmdline is not None else comm.encode() + b"\x00")


def _make_scope(slice_dir: Path, unit: str, pids: list[int]) -> Path:
    scope = slice_dir / unit
    scope.mkdir(parents=True, exist_ok=True)
    (scope / "cgroup.procs").write_text("\n".join(str(p) for p in pids) + ("\n" if pids else ""))
    return scope


class _Recorder:
    def __init__(self, *, empty_on_stop: bool = True):
        self.stopped: list[str] = []
        self.killed: list[tuple[int, int]] = []
        self.slept: list[float] = []
        self._empty_on_stop = empty_on_stop
        self._scope_by_unit: dict[str, Path] = {}

    def register(self, unit: str, scope_dir: Path) -> None:
        self._scope_by_unit[unit] = scope_dir

    def stop_unit(self, unit: str) -> bool:
        self.stopped.append(unit)
        if self._empty_on_stop and unit in self._scope_by_unit:
            (self._scope_by_unit[unit] / "cgroup.procs").write_text("")
        return True

    def kill(self, pid: int, sig: int) -> None:
        self.killed.append((pid, sig))

    def signal_owned(
        self,
        pid: int,
        sig: int,
        _members: list[int],
        _scope_dir: Path,
        _proc_root: Path,
        _pinned,
    ) -> tuple[bool, str]:
        self.kill(pid, sig)
        return True, ""

    def sleep(self, secs: float) -> None:
        self.slept.append(secs)


def _reap(
    slice_dir,
    proc_root,
    rec,
    *,
    active=None,
    tracked=None,
    gateway_boot_us=0,
    enter=None,
    min_age=600,
    stop_unit=None,
    signal_owned=None,
):
    enter = enter or {}
    return r.reap_scopes(
        slice_dir,
        active_pids=set(active or set()),
        tracked_pids=set(tracked or set()),
        gateway_boot_us=gateway_boot_us,
        min_age_secs=min_age,
        now_monotonic=_NOW,
        proc_root=proc_root,
        stop_unit=stop_unit or rec.stop_unit,
        signal_owned=signal_owned or rec.signal_owned,
        sleep=rec.sleep,
        active_enter_us=lambda unit: enter.get(unit),
    )


class _ModuleProxy:
    def __init__(self, module, **overrides):
        self._module = module
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._module, name)
