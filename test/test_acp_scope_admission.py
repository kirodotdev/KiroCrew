"""Scope admission is a startup stage of its own, before ``initialize``.

``systemd-run --user --scope`` execs the agent only after the user systemd manager
has moved its pid into the scope. These tests pin that the runtime waits for that
move under its own bound, reports a slow manager and an early exit as their own
errors with nothing written to the agent, and gives ``initialize`` what is left of
the one cold-start window.

No test reaches systemd. Time is a :class:`ManualClock` that moves only when the
wait sleeps, and the ``/proc`` reader is a fake. The stage runs on a runtime that
holds a fake process (``_uninitialized_runtime``), so those tests run on every
platform and start no process.

One test runs the real reader on a real spawn, which the rlimit shim starts. Its
scope wrap is a stand-in argv that only carries the markers ``name_scope_unit``
recognises. It is the module's one real interpreter child (testing-conventions D8).
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
    AcpRequestTimeout,
    AcpRuntime,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpScopeAdmissionTimeout,
)
from kiro_crew.acp.stderr_tail import STDERR_WITHHELD
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import finished

_STAND_IN = "import time; time.sleep(60)"
_UNIT = "kirocrew-rt-0123456789abcdef.scope"
_TRANSPORT_LINE = "Failed to start transient scope unit: Transport endpoint is not connected"
#: A synthetic GitHub token with a valid checksum. The redaction rule table
#: reads it by format, so the stderr tail that carries it is withheld.
_KEY = github_classic(seed=52)
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


def _reader(*answers: str | BaseException, then: str | BaseException | None = None) -> Any:
    """A fake ``/proc`` reader: each answer in turn, then *then* for every later read.

    An answer that is an exception is raised, and a text has ``{unit}`` replaced by
    :data:`_UNIT`. ``reads`` counts the calls.
    """
    queue = list(answers)

    async def _read(_pid: int) -> str:
        _read.reads += 1  # type: ignore[attr-defined]
        answer = queue.pop(0) if queue else then
        if isinstance(answer, BaseException):
            raise answer
        assert answer is not None, "the reader ran past its scripted answers"
        return answer.format(unit=_UNIT)

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


# --------------------------------------------------------------------------- #
# The stage's budget for initialize, and the spawn that runs it.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_delayed_admission_leaves_initialize_the_rest_of_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, proc = _uninitialized_runtime()
    clock = ManualClock(monotonic_start=1000.0)
    reader = _reader(_OUT, _OUT, _OUT, then=_IN)
    _patch_wait(monkeypatch, reader, clock)

    budget = await finished(rt_launch._admit_scope(rt), "the scope admission")

    assert clock.sleeps == (scope_admission.POLL_INTERVAL_S,) * 3
    # The fourth read found the pid in its scope.
    assert reader.reads == 4
    assert budget == pytest.approx(_INITIALIZE_TIMEOUT - 3 * scope_admission.POLL_INTERVAL_S)
    proc.stdin.write.assert_not_called()


@pytest.mark.asyncio
async def test_an_admission_timeout_names_the_unit_and_the_pid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rt, proc = _uninitialized_runtime()
    _patch_wait(monkeypatch, _reader(then=_OUT), ManualClock(monotonic_start=1000.0))

    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        with pytest.raises(AcpScopeAdmissionTimeout) as raised:
            await finished(rt_launch._admit_scope(rt), "the scope admission")

    text = str(raised.value)
    assert f"agent process (PID {UNALLOCATABLE_PID}) into its scope {_UNIT}" in text
    assert f"within {rt_launch._SCOPE_ADMISSION_TIMEOUT:g}s" in text
    assert "systemctl --user list-units --state=failed" in text
    proc.stdin.write.assert_not_called()
    records = [r for r in caplog.records if "stage=scope_admission" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "outcome=timeout" in records[0].getMessage()
    assert f"scope={_UNIT}" in records[0].getMessage()


@pytest.mark.asyncio
async def test_an_unreadable_proc_goes_straight_to_initialize_with_the_whole_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, _ = _uninitialized_runtime()
    clock = ManualClock(monotonic_start=1000.0)
    reader = _reader(PermissionError("denied"))
    _patch_wait(monkeypatch, reader, clock)

    budget = await finished(rt_launch._admit_scope(rt), "the scope admission")

    assert budget == _INITIALIZE_TIMEOUT
    assert clock.sleeps == ()
    assert reader.reads == 1


@pytest.mark.asyncio
async def test_a_spawn_with_no_scope_keeps_the_whole_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, _ = _uninitialized_runtime()
    rt._scope_unit = ""
    reader = _reader()
    _patch_wait(monkeypatch, reader, ManualClock(monotonic_start=1000.0))

    budget = await finished(rt_launch._admit_scope(rt), "the scope admission")

    assert budget == _INITIALIZE_TIMEOUT
    assert reader.reads == 0


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="procfs is Linux-only")
# The module's one real interpreter child, serialised with the suite's other real
# spawns so its cold start never lands beside theirs on another worker (D8).
@pytest.mark.xdist_group(name="subprocess_spawn")
async def test_a_real_spawn_outside_its_scope_is_not_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real reader on a real spawn, which the rlimit shim starts as Python.

    No systemd moves the stand-in, so it is never in its scope. The wait must run
    to its bound and send nothing: the shim's and the stand-in's names are not
    systemd-run, and a check on the name would admit the spawn at its first read.
    The spawn's failed-start guard must then reap the process.
    """
    from kiro_crew.sandbox_rlimit import RLIMIT_PROFILE_SESSION_HOST, spawn_shim_argv

    assert spawn_shim_argv(RLIMIT_PROFILE_SESSION_HOST), "the spawn must start in the shim"
    monkeypatch.setattr(rt_launch, "_SCOPE_ADMISSION_TIMEOUT", 0.3)
    rt = AcpRuntime(
        work_dir=tmp_path / "wd",
        agent="kirocrew",
        sandbox_mode="off",
        native_skill_projection=False,
    )
    rt._KILL_TERM_TIMEOUT = 1.0
    rt._KILL_REAP_TIMEOUT = 1.0
    sent: list[str] = []
    admitted: list[Any] = []
    real_admit = rt_launch._admit_scope

    async def _plan() -> SpawnPlan:
        return SpawnPlan(argv=[sys.executable, "-c", _STAND_IN, "--agent", "kirocrew"])

    async def _send(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        sent.append(method)
        raise AcpRuntimeError("stopped after initialize")

    async def _admit(runtime: AcpRuntime) -> float:
        # Read before the wait: the failed-start cleanup lets go of the process.
        admitted.append(runtime._process)
        return await real_admit(runtime)

    monkeypatch.setattr(rt, "_resolve_spawn_plan", _plan)
    monkeypatch.setattr(rt, "_send_and_await", _send)
    # The flags are trailing arguments to ``python -c``, which ignores them.
    monkeypatch.setattr(runtime_mod, "cgroup_scope_argv", lambda argv: [*argv, "--scope", "--"])
    monkeypatch.setattr(rt_launch, "_admit_scope", _admit)

    with pytest.raises(AcpScopeAdmissionTimeout) as raised:
        await finished(rt.spawn(), "the scoped spawn")

    assert "into its scope kirocrew-rt-" in str(raised.value)
    assert sent == []
    [process] = admitted
    assert process is not None and process.returncode is not None, "the process was not reaped"


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
    assert type(exc) is AcpRuntimeDead
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
        with pytest.raises(AcpRuntimeDead, match="stage=scope_admission") as raised:
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

    with pytest.raises(AcpRuntimeDead, match="stage=scope_admission") as raised:
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

    with pytest.raises(AcpRuntimeDead, match="stage=scope_admission") as raised:
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

    with pytest.raises(AcpRuntimeDead, match="stage=scope_admission") as raised:
        await finished(rt_launch._admit_scope(rt), "the scope admission")

    assert reader.reads == 0
    assert "reads=0" in str(raised.value)


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


def test_admission_leaves_initialize_its_reserve() -> None:
    assert rt_launch._SCOPE_ADMISSION_RESERVE == 30.0
    assert (
        rt_launch._SCOPE_ADMISSION_TIMEOUT
        == _INITIALIZE_TIMEOUT - rt_launch._SCOPE_ADMISSION_RESERVE
    )
    assert rt_launch._SCOPE_ADMISSION_TIMEOUT > 0


@pytest.mark.parametrize(
    ("waited_s", "expected"),
    [
        (0.0, 90.0),
        (0.002, 89.998),
        (56.0, 34.0),
        (60.0, 30.0),
        (60.04, 30.0),
        (85.0, 30.0),
    ],
)
def test_initialize_gets_the_rest_of_the_window_and_never_less_than_the_reserve(
    waited_s: float, expected: float
) -> None:
    budget = scope_admission.initialize_budget(90.0, 30.0, waited_s)
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


def test_the_admission_errors_carry_their_retry_verdicts() -> None:
    from kiro_crew.llm_helpers import acp_error_is_transient

    timeout = AcpScopeAdmissionTimeout("slow")
    assert AcpScopeAdmissionTimeout.transient is False
    assert acp_error_is_transient(timeout) is False
    assert isinstance(timeout, AcpRuntimeError)
    assert not isinstance(timeout, AcpRequestTimeout)


def test_the_admission_errors_are_exported_from_the_runtime_module() -> None:
    from kiro_crew.acp import session_handle

    name = "AcpScopeAdmissionTimeout"
    assert name in runtime_mod.__all__
    assert getattr(runtime_mod, name) is getattr(session_handle, name)
    assert getattr(session_handle, name).__module__ == "kiro_crew.acp.session_handle"
