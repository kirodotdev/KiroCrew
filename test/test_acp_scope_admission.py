"""Scope admission is a startup stage of its own, before ``initialize``.

``systemd-run --user --scope`` execs the agent only after the user systemd manager
has moved its pid into the scope. These tests pin that the runtime waits for that
move under its own bound, reports a slow manager and an early exit as their own
errors with nothing written to the agent, and gives ``initialize`` what is left of
the one cold-start window.

No test reaches systemd. The scope wrap is a stand-in argv that only carries the
markers ``name_scope_unit`` recognises, and time is a :class:`ManualClock` that
moves only when the wait sleeps. The ``/proc`` reader is a fake, except in the
test that runs the real reader on a real spawn, which the rlimit shim starts.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp import runtime_spawn_launch as rt_launch
from kiro_crew.acp import scope_admission
from kiro_crew.acp.harness.base import SpawnPlan
from kiro_crew.acp.runtime import (
    _INITIALIZE_TIMEOUT,
    _REQUEST_TIMEOUT,
    AcpRequestTimeout,
    AcpRuntime,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpScopeAdmissionDied,
    AcpScopeAdmissionTimeout,
)
from kiro_crew.acp.stderr_tail import STDERR_WITHHELD
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import finished

_STAND_IN = "import time; time.sleep(60)"
_UNIT = "kirocrew-rt-0123456789abcdef.scope"
_TRANSPORT_LINE = "Failed to start transient scope unit: Transport endpoint is not connected"
#: The AWS documentation example key the stderr redaction tests use.
_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_REAL_WAIT = scope_admission.wait_for_admission
#: Scripted ``/proc/<pid>/cgroup`` texts: the pid still in the gateway's own
#: service, and the pid in the runtime's scope (``{unit}`` is filled in per read).
_OUT = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/kirocrew.service\n"
_IN = "0::/user.slice/user-1000.slice/user@1000.service/agents.slice/{unit}\n"


def _sleeper(clock: ManualClock) -> Any:
    """The wait's pause: advance the manual clock, then yield to the loop once."""

    async def _sleep(seconds: float) -> None:
        clock.sleep(seconds)
        await asyncio.sleep(0)

    return _sleep


def _reader(
    *answers: str | BaseException,
    then: str | BaseException | None = None,
    unit: Any = lambda: _UNIT,
) -> Any:
    """A fake ``/proc`` reader: each answer in turn, then *then* for every later read.

    An answer that is an exception is raised, and a text has ``{unit}`` replaced by
    what *unit* returns at the read. ``reads`` counts the calls.
    """
    queue = list(answers)

    async def _read(_pid: int) -> str:
        _read.reads += 1  # type: ignore[attr-defined]
        answer = queue.pop(0) if queue else then
        if isinstance(answer, BaseException):
            raise answer
        assert answer is not None, "the reader ran past its scripted answers"
        return answer.format(unit=unit())

    _read.reads = 0  # type: ignore[attr-defined]
    return _read


def _patch_wait(monkeypatch: pytest.MonkeyPatch, reader: Any, clock: ManualClock) -> None:
    """Make the runtime's admission wait read *reader* and run on *clock*."""
    monkeypatch.setattr(
        scope_admission,
        "wait_for_admission",
        functools.partial(
            _REAL_WAIT, read_cgroup=reader, clock=clock.monotonic, sleep=_sleeper(clock)
        ),
    )


def _uninitialized_runtime(**kwargs: Any) -> tuple[AcpRuntime, MagicMock]:
    """A runtime holding a fake, running process wrapped in a named scope."""
    rt = AcpRuntime(work_dir="/tmp", **kwargs)
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = UNALLOCATABLE_PID
    rt._scope_unit = _UNIT
    return rt, proc


class _Spawn:
    """A real stand-in process spawned through ``AcpRuntime.spawn``, its handshake recorded.

    *scoped* wraps the argv so ``name_scope_unit`` names a scope, the shape a host
    with cgroup delegation produces, without ``systemd-run``. ``initialize`` never
    reaches the stand-in: ``_send_and_await`` records the call and fails it, so the
    spawn's failed-start guard reaps the process like any failed start.
    """

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, scoped: bool) -> None:
        self.rt = AcpRuntime(
            work_dir=tmp_path / "wd",
            agent="kirocrew",
            sandbox_mode="off",
            native_skill_projection=False,
        )
        self.rt._KILL_TERM_TIMEOUT = 1.0
        self.rt._KILL_REAP_TIMEOUT = 1.0
        self.sent: list[tuple[str, float | None, int]] = []
        self.reader: Any = None
        # Read while the wait runs: the failed-start cleanup clears ``_scope_unit``
        # and lets go of the process.
        self.seen: tuple[int, str] | None = None
        self.process: Any = None

        async def _plan() -> SpawnPlan:
            return SpawnPlan(argv=[sys.executable, "-c", _STAND_IN, "--agent", "kirocrew"])

        async def _send(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
            reads = self.reader.reads if self.reader is not None else 0
            self.sent.append((method, timeout, reads))
            raise AcpRuntimeError("stopped after initialize")

        monkeypatch.setattr(self.rt, "_resolve_spawn_plan", _plan)
        monkeypatch.setattr(self.rt, "_send_and_await", _send)
        if scoped:
            # The flags are trailing arguments to ``python -c``, which ignores them.
            monkeypatch.setattr(
                runtime_mod, "cgroup_scope_argv", lambda argv: [*argv, "--scope", "--"]
            )

    def use(self, monkeypatch: pytest.MonkeyPatch, reader: Any, clock: ManualClock) -> None:
        self.reader = reader

        async def _read(pid: int) -> str:
            self.seen = (pid, self.rt._scope_unit)
            self.process = self.rt._process
            return await reader(pid)

        _patch_wait(monkeypatch, _read, clock)

    async def spawn(self) -> BaseException:
        """Run the spawn to its failure and return what it raised."""
        with pytest.raises(AcpRuntimeError) as raised:
            await finished(self.rt.spawn(), "the scoped spawn")
        return raised.value

    def reaped(self) -> bool:
        """Whether the process the wait saw has exited and been reaped."""
        return self.process is not None and self.process.returncode is not None

    def unit(self) -> str:
        """The scope unit the spawn named, for a reader's ``{unit}``."""
        return self.rt._scope_unit


# --------------------------------------------------------------------------- #
# The spawn path: admission, then initialize, in one window.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_initialize_is_sent_after_a_delayed_admission_with_the_rest_of_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _Spawn(tmp_path, monkeypatch, scoped=True)
    clock = ManualClock(monotonic_start=1000.0)
    reader = _reader(_OUT, _OUT, _OUT, then=_IN, unit=spawn.unit)
    spawn.use(monkeypatch, reader, clock)

    raised = await spawn.spawn()

    assert str(raised) == "stopped after initialize"
    assert spawn.seen is not None
    assert spawn.seen[0] == spawn.rt._pid
    assert spawn.seen[1].startswith("kirocrew-rt-")
    assert clock.sleeps == (scope_admission.POLL_INTERVAL_S,) * 3
    admission_s = 3 * scope_admission.POLL_INTERVAL_S
    [(method, budget, reads_before_send)] = spawn.sent
    assert method == "initialize"
    # All four reads, the last one finding the pid in its scope, came before the request.
    assert reads_before_send == reader.reads == 4
    assert budget == pytest.approx(_INITIALIZE_TIMEOUT - admission_s)
    assert spawn.reaped()


@pytest.mark.asyncio
async def test_an_admission_timeout_writes_no_initialize_and_reaps_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spawn = _Spawn(tmp_path, monkeypatch, scoped=True)
    clock = ManualClock(monotonic_start=1000.0)
    spawn.use(monkeypatch, _reader(then=_OUT), clock)

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        raised = await spawn.spawn()

    assert isinstance(raised, AcpScopeAdmissionTimeout)
    assert spawn.seen is not None
    pid, unit = spawn.seen
    assert unit.startswith("kirocrew-rt-")
    assert (raised.pid, raised.scope_unit) == (pid, unit)
    assert raised.waited_s >= rt_launch._SCOPE_ADMISSION_TIMEOUT
    text = str(raised)
    assert f"agent process (PID {pid}) into its scope {unit}" in text
    assert f"within {rt_launch._SCOPE_ADMISSION_TIMEOUT:g}s" in text
    assert "systemctl --user list-units --state=failed" in text
    assert spawn.sent == []
    assert spawn.reaped()
    records = [r for r in caplog.records if "stage=scope_admission" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "outcome=timeout" in records[0].getMessage()
    assert f"scope={unit}" in records[0].getMessage()


@pytest.mark.asyncio
async def test_an_unreadable_proc_goes_straight_to_initialize_with_the_whole_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _Spawn(tmp_path, monkeypatch, scoped=True)
    clock = ManualClock(monotonic_start=1000.0)
    reader = _reader(PermissionError("denied"))
    spawn.use(monkeypatch, reader, clock)

    await spawn.spawn()

    assert clock.sleeps == ()
    assert spawn.sent == [("initialize", _INITIALIZE_TIMEOUT, 1)]


@pytest.mark.asyncio
async def test_an_unwrapped_spawn_takes_no_admission_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _Spawn(tmp_path, monkeypatch, scoped=False)
    clock = ManualClock(monotonic_start=1000.0)
    reader = _reader()
    spawn.use(monkeypatch, reader, clock)

    await spawn.spawn()

    assert spawn.rt._scope_unit == ""
    assert reader.reads == 0
    assert spawn.sent == [("initialize", _INITIALIZE_TIMEOUT, 0)]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="procfs is Linux-only")
async def test_a_real_spawn_outside_its_scope_is_not_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real reader on a real spawn, which the rlimit shim starts as Python.

    No systemd moves the stand-in, so it is never in its scope. The wait must run
    to its bound and send nothing: the shim's and the stand-in's names are not
    systemd-run, and a check on the name would admit the spawn at its first read.
    """
    from kiro_crew.sandbox_rlimit import RLIMIT_PROFILE_SESSION_HOST, spawn_shim_argv

    assert spawn_shim_argv(RLIMIT_PROFILE_SESSION_HOST), "the spawn must start in the shim"
    monkeypatch.setattr(rt_launch, "_SCOPE_ADMISSION_TIMEOUT", 0.3)
    spawn = _Spawn(tmp_path, monkeypatch, scoped=True)

    raised = await spawn.spawn()

    assert isinstance(raised, AcpScopeAdmissionTimeout)
    assert raised.scope_unit.startswith("kirocrew-rt-")
    assert spawn.sent == []


# --------------------------------------------------------------------------- #
# An exit during the admission wait is a death that names the stage, and only
# what the reads saw.
# --------------------------------------------------------------------------- #


def _reaped_with(proc: MagicMock, code: int) -> Any:
    """A reader whose pid is gone: the reap set *code* just before the read."""

    async def _read(_pid: int) -> str:
        proc.returncode = code
        raise FileNotFoundError("/proc/<pid>/stat")

    return _read


def _exits_after_a_read_outside_the_scope(proc: MagicMock, code: int) -> Any:
    """A reader that finds the pid outside its scope once, and the process then exits.

    The shape of a manager that moves the pid just after a read, where systemd-run
    execs the next layer and that layer exits within one poll interval.
    """

    async def _read(_pid: int) -> str:
        _read.reads += 1  # type: ignore[attr-defined]
        proc.returncode = code
        return _OUT

    _read.reads = 0  # type: ignore[attr-defined]
    return _read


def _assert_claims_no_admission_state(text: str) -> None:
    """The text must not say whether systemd admitted the scope: no read measured it."""
    assert "during the scope admission wait" in text
    assert "admitted its scope" not in text
    assert "never started" not in text


@pytest.mark.asyncio
async def test_an_exit_during_admission_is_a_death_naming_the_stage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rt, proc = _uninitialized_runtime()
    rt._stderr_lines.append(_TRANSPORT_LINE)
    _patch_wait(monkeypatch, _reaped_with(proc, 1), ManualClock(monotonic_start=1000.0))

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        with pytest.raises(AcpRuntimeDead) as raised:
            await finished(rt_launch._admit_scope(rt), "the scope admission")

    exc = raised.value
    assert isinstance(exc, AcpScopeAdmissionDied)
    assert (exc.pid, exc.scope_unit, exc.returncode) == (UNALLOCATABLE_PID, _UNIT, "1")
    text = str(exc)
    _assert_claims_no_admission_state(text)
    assert "stage=scope_admission" in text
    assert "reads=0" in text
    assert f"pid={UNALLOCATABLE_PID}" in text
    assert f"scope={_UNIT}" in text
    assert "returncode=1" in text
    assert f"stderr_tail: {_TRANSPORT_LINE}" in text
    [record] = [r for r in caplog.records if "stage=scope_admission" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "outcome=exited" in record.getMessage()
    assert "reads=0" in record.getMessage()
    assert "returncode=1" in record.getMessage()


@pytest.mark.asyncio
async def test_an_exit_between_two_reads_counts_the_read_and_does_not_blame_the_manager(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rt, proc = _uninitialized_runtime()
    rt._stderr_lines.append("bwrap: setting up uid map: Permission denied")
    reader = _exits_after_a_read_outside_the_scope(proc, 1)
    _patch_wait(monkeypatch, reader, ManualClock(monotonic_start=1000.0))

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        with pytest.raises(AcpScopeAdmissionDied) as raised:
            await finished(rt_launch._admit_scope(rt), "the scope admission")

    assert reader.reads == 1
    text = str(raised.value)
    _assert_claims_no_admission_state(text)
    assert "reads=1" in text
    assert "stderr_tail: bwrap: setting up uid map: Permission denied" in text
    [record] = [r for r in caplog.records if "stage=scope_admission" in r.getMessage()]
    assert "outcome=exited" in record.getMessage()
    assert "reads=1" in record.getMessage()


@pytest.mark.asyncio
async def test_an_exit_during_admission_withholds_a_secret_in_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, proc = _uninitialized_runtime()
    rt._stderr_lines.extend([_TRANSPORT_LINE, f"token {_KEY} rejected"])
    _patch_wait(monkeypatch, _reaped_with(proc, 1), ManualClock(monotonic_start=1000.0))

    with pytest.raises(AcpScopeAdmissionDied) as raised:
        await finished(rt_launch._admit_scope(rt), "the scope admission")

    text = str(raised.value)
    assert f"stderr_tail: {STDERR_WITHHELD}" in text
    assert _KEY not in text
    assert "returncode=1" in text


@pytest.mark.asyncio
async def test_a_restricted_session_keeps_the_code_but_not_the_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, proc = _uninitialized_runtime(memory_mode="incognito")
    rt._stderr_lines.append(_TRANSPORT_LINE)
    _patch_wait(monkeypatch, _reaped_with(proc, 1), ManualClock(monotonic_start=1000.0))

    with pytest.raises(AcpScopeAdmissionDied) as raised:
        await finished(rt_launch._admit_scope(rt), "the scope admission")

    text = str(raised.value)
    assert "returncode=1" in text
    assert "stderr_tail: <not retained>" in text
    assert "Transport endpoint" not in text


@pytest.mark.asyncio
async def test_a_runtime_marked_dead_ends_the_wait_as_an_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, proc = _uninitialized_runtime()
    rt._dead = True
    proc.wait = AsyncMock(return_value=1)
    reader = _reader()
    _patch_wait(monkeypatch, reader, ManualClock(monotonic_start=1000.0))

    with pytest.raises(AcpScopeAdmissionDied) as raised:
        await finished(rt_launch._admit_scope(rt), "the scope admission")

    assert reader.reads == 0
    assert "reads=0" in str(raised.value)


@pytest.mark.asyncio
async def test_a_spawn_with_no_scope_returns_no_admission_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, _ = _uninitialized_runtime()
    rt._scope_unit = ""
    reader = _reader()
    _patch_wait(monkeypatch, reader, ManualClock(monotonic_start=1000.0))

    assert await finished(rt_launch._admit_scope(rt), "the scope admission") is None
    assert reader.reads == 0


# --------------------------------------------------------------------------- #
# The wait itself.
# --------------------------------------------------------------------------- #


async def _wait(reader: Any, clock: ManualClock, *, exited: Any = lambda: False, bound_s=1.0):
    return await finished(
        _REAL_WAIT(
            UNALLOCATABLE_PID,
            _UNIT,
            exited=exited,
            bound_s=bound_s,
            read_cgroup=reader,
            clock=clock.monotonic,
            sleep=_sleeper(clock),
        ),
        "the admission wait",
    )


@pytest.mark.asyncio
async def test_the_wait_returns_once_the_pid_is_in_its_scope() -> None:
    clock = ManualClock(monotonic_start=1000.0)
    got = await _wait(_reader(_OUT, _OUT, then=_IN), clock)
    assert (got.outcome, got.reads) == (scope_admission.ADMITTED, 2)
    assert got.waited_s == pytest.approx(2 * scope_admission.POLL_INTERVAL_S)


@pytest.mark.asyncio
async def test_the_wait_times_out_at_its_bound_with_the_pid_outside_its_scope() -> None:
    clock = ManualClock(monotonic_start=1000.0)
    reader = _reader(then=_OUT)
    got = await _wait(reader, clock, bound_s=1.0)
    assert got.outcome == scope_admission.TIMEOUT
    assert got.waited_s >= 1.0
    assert reader.reads == got.reads == 21


@pytest.mark.asyncio
async def test_an_absent_pid_with_no_exit_counts_as_unchecked_after_the_grace() -> None:
    clock = ManualClock(monotonic_start=1000.0)
    got = await _wait(_reader(then=FileNotFoundError("gone")), clock, bound_s=60.0)
    assert (got.outcome, got.detail) == (scope_admission.UNCHECKED, "pid_not_listed")
    assert got.waited_s == pytest.approx(scope_admission.MISSING_PID_GRACE_S)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [PermissionError("denied"), OSError("io"), "no hierarchy line here\n"],
    ids=["PermissionError", "OSError", "unparsable"],
)
async def test_a_read_that_cannot_run_counts_as_unchecked(answer: str | BaseException) -> None:
    clock = ManualClock(monotonic_start=1000.0)
    got = await _wait(_reader(answer), clock)
    assert (got.outcome, got.waited_s) == (scope_admission.UNCHECKED, 0.0)
    assert got.detail == ("ValueError" if isinstance(answer, str) else type(answer).__name__)


@pytest.mark.asyncio
async def test_a_read_still_queued_at_the_bound_counts_as_unchecked() -> None:
    clock = ManualClock(monotonic_start=1000.0)
    never = asyncio.Event()

    async def _queued(_pid: int) -> str:
        await never.wait()
        return _IN.format(unit=_UNIT)

    got = await _wait(_queued, clock, bound_s=scope_admission.POLL_INTERVAL_S)
    assert (got.outcome, got.detail) == (scope_admission.UNCHECKED, "read_timeout")


@pytest.mark.asyncio
async def test_an_exit_is_seen_before_the_unreaped_process_is_read() -> None:
    clock = ManualClock(monotonic_start=1000.0)
    reader = _reader(then=_OUT)
    got = await _wait(reader, clock, exited=lambda: reader.reads >= 2)
    assert (got.outcome, got.reads) == (scope_admission.EXITED, 2)
    assert reader.reads == 2


@pytest.mark.parametrize(
    ("text", "inside"),
    [
        (_IN.format(unit=_UNIT), True),
        (f"0::/user.slice/agents.slice/{_UNIT}/agent\n", True),
        (f"0::/../../{_UNIT}\n", True),
        (f"12:pids:/user.slice/{_UNIT}\n0::/user.slice/kirocrew.service\n", True),
        (_OUT, False),
        ("0::/user.slice/agents.slice/kirocrew-rt-fedcba9876543210.scope\n", False),
        (f"0::/user.slice/agents.slice/{_UNIT}.d\n", False),
        ("0::/\n", False),
    ],
    ids=[
        "scope",
        "child-cgroup",
        "outside-the-namespace-root",
        "v1-lines",
        "service",
        "other-scope",
        "longer-name",
        "root",
    ],
)
def test_the_scope_is_one_component_of_a_cgroup_path(text: str, inside: bool) -> None:
    assert scope_admission.in_scope(text, _UNIT) is inside


def test_a_cgroup_text_with_no_hierarchy_line_does_not_parse() -> None:
    with pytest.raises(ValueError):
        scope_admission.in_scope("", _UNIT)


@pytest.mark.skipif(sys.platform != "linux", reason="procfs is Linux-only")
def test_the_production_reader_finds_this_process_outside_a_runtime_scope() -> None:
    import os

    assert scope_admission.in_scope(scope_admission.read_proc_cgroup(os.getpid()), _UNIT) is False


# --------------------------------------------------------------------------- #
# Budgets, logs and error types.
# --------------------------------------------------------------------------- #


def test_admission_takes_the_window_less_one_request_budget() -> None:
    assert rt_launch._SCOPE_ADMISSION_RESERVE == _REQUEST_TIMEOUT
    assert rt_launch._SCOPE_ADMISSION_TIMEOUT == _INITIALIZE_TIMEOUT - _REQUEST_TIMEOUT
    assert rt_launch._SCOPE_ADMISSION_TIMEOUT > 0


@pytest.mark.parametrize(
    ("admission_s", "expected"),
    [
        (None, 90.0),
        (0.0, 90.0),
        (0.002, 89.998),
        (56.0, 34.0),
        (60.0, 30.0),
        (60.04, 30.0),
        (85.0, 30.0),
    ],
)
def test_initialize_gets_the_rest_of_the_window_and_never_less_than_the_reserve(
    admission_s: float | None, expected: float
) -> None:
    budget = scope_admission.initialize_budget(90.0, 30.0, admission_s)
    assert budget == pytest.approx(expected)
    assert budget >= 30.0


@pytest.mark.parametrize(
    ("waited_s", "level"),
    [(0.004, logging.INFO), (scope_admission.SLOW_ADMISSION_S + 0.1, logging.WARNING)],
    ids=["healthy", "slow"],
)
def test_an_admission_logs_at_info_and_a_slow_one_at_warning(
    waited_s: float, level: int, caplog: pytest.LogCaptureFixture
) -> None:
    admission = scope_admission.Admission(scope_admission.ADMITTED, waited_s)
    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        scope_admission.log_stage(admission, bound_s=60.0, pid=4242, unit=_UNIT)
    [record] = caplog.records
    assert record.levelno == level
    assert record.getMessage() == (
        "acp_startup_stage stage=scope_admission outcome=admitted "
        f"duration_ms={waited_s * 1000.0:.1f} budget_s=60 pid=4242 scope={_UNIT}"
    )


def test_an_unchecked_admission_logs_as_admitted_with_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    admission = scope_admission.Admission(scope_admission.UNCHECKED, 0.0, "PermissionError")
    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        scope_admission.log_stage(admission, bound_s=60.0, pid=4242, unit=_UNIT)
    [record] = caplog.records
    assert record.levelno == logging.INFO
    assert "outcome=admitted" in record.getMessage()
    assert record.getMessage().endswith("proc_check=unavailable reason=PermissionError")


@pytest.mark.asyncio
async def test_an_initialize_timeout_after_admission_names_the_admission_time(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rt, _ = _uninitialized_runtime()
    monkeypatch.setattr(runtime_mod, "agents_slice_throttling", lambda: False)

    async def _stall(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        raise AcpRequestTimeout(f"Request {method} timed out after {timeout:g}s")

    monkeypatch.setattr(rt, "_send_and_await", _stall)
    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        with pytest.raises(AcpRequestTimeout, match="timed out after 30s"):
            await finished(
                rt._initialize_handshake({}, 30.0, admission_s=60.0), "the initialize handshake"
            )
    [record] = [r for r in caplog.records if "timeout_after_scope_admission" in r.getMessage()]
    assert "admission_ms=60000.0" in record.getMessage()
    assert "timeout_budget_s=30" in record.getMessage()
    assert f"scope={_UNIT}" in record.getMessage()


def test_the_admission_errors_carry_their_retry_verdicts() -> None:
    from kiro_crew.llm_helpers import acp_error_is_transient

    timeout = AcpScopeAdmissionTimeout("slow", pid=1, scope_unit=_UNIT, waited_s=60.0)
    assert AcpScopeAdmissionTimeout.transient is False
    assert acp_error_is_transient(timeout) is False
    assert isinstance(timeout, AcpRuntimeError)
    assert not isinstance(timeout, AcpRequestTimeout)
    died = AcpScopeAdmissionDied("exited", pid=1, scope_unit=_UNIT, returncode="1")
    assert isinstance(died, AcpRuntimeDead)


def test_the_admission_errors_are_exported_from_the_runtime_module() -> None:
    from kiro_crew.acp import session_handle

    for name in ("AcpScopeAdmissionTimeout", "AcpScopeAdmissionDied"):
        assert name in runtime_mod.__all__
        assert getattr(runtime_mod, name) is getattr(session_handle, name)
        assert getattr(session_handle, name).__module__ == "kiro_crew.acp.session_handle"
