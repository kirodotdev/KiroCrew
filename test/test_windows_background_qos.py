"""Windows background QoS for sub-agent runtimes (``agent.windows_background_qos``).

The Win32 calls are driven through an injected fake ``kernel32`` so the decision
logic and the exact arguments are checked on every runner; the Windows-gated
test at the end reads a real child's priority class back from the kernel.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from typing import Any

import pytest

from kiro_crew import platform_compat, sandbox_slice
from kiro_crew import windows_background_qos as qos
from kiro_crew.start_priority import StartPriority


class _FakeKernel32:
    """Records each call; every call succeeds unless its name is in *fail*."""

    def __init__(self, *, fail: tuple[str, ...] = ()) -> None:
        self.fail = fail
        self.calls: list[tuple[Any, ...]] = []

    def _result(self, name: str, ok: Any) -> Any:
        return 0 if name in self.fail else ok

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:  # noqa: N802
        self.calls.append(("OpenProcess", access, inherit, pid))
        return self._result("OpenProcess", 77)

    def SetPriorityClass(self, handle: int, cls: int) -> int:  # noqa: N802
        self.calls.append(("SetPriorityClass", handle, cls))
        return self._result("SetPriorityClass", 1)

    def SetProcessInformation(
        self, handle: int, cls: int, ref: Any, size: int
    ) -> int:  # noqa: N802
        state = ctypes.cast(ref, ctypes.POINTER(qos._ProcessPowerThrottlingState)).contents
        fields = (state.Version, state.ControlMask, state.StateMask)
        self.calls.append(("SetProcessInformation", handle, cls, fields, size))
        return self._result("SetProcessInformation", 1)

    def SetInformationJobObject(self, job: int, cls: int, ref: Any, size: int) -> int:  # noqa: N802
        info = ctypes.cast(ref, ctypes.POINTER(qos._JobCpuRateControlInformation)).contents
        self.calls.append(
            ("SetInformationJobObject", job, cls, info.ControlFlags, info.Weight, size)
        )
        return self._result("SetInformationJobObject", 1)

    def CloseHandle(self, handle: int) -> int:  # noqa: N802
        self.calls.append(("CloseHandle", handle))
        return 1

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture(autouse=True)
def _reset_ecoqos_note(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(qos, "_ecoqos_unavailable_logged", False)


class TestApplyBackgroundQos:
    def test_sets_below_normal_and_ecoqos_then_closes(self) -> None:
        k32 = _FakeKernel32()
        assert qos.apply_background_qos(4242, kernel32=k32) is True
        assert k32.calls == [
            ("OpenProcess", 0x0200, False, 4242),
            ("SetPriorityClass", 77, 0x4000),
            ("SetProcessInformation", 77, 4, (1, 1, 1), 12),
            ("CloseHandle", 77),
        ]

    def test_ecoqos_refused_keeps_priority_and_logs_once(self, caplog) -> None:
        k32 = _FakeKernel32(fail=("SetProcessInformation",))
        with caplog.at_level(logging.INFO, logger=qos.logger.name):
            assert qos.apply_background_qos(1, kernel32=k32) is True
            assert qos.apply_background_qos(2, kernel32=k32) is True
        notes = [r for r in caplog.records if "EcoQoS unavailable" in r.getMessage()]
        assert len(notes) == 1

    def test_priority_refused_reports_false_and_still_closes(self) -> None:
        k32 = _FakeKernel32(fail=("SetPriorityClass",))
        assert qos.apply_background_qos(5, kernel32=k32) is False
        assert k32.names()[-1] == "CloseHandle"

    def test_unopenable_pid_touches_nothing(self) -> None:
        k32 = _FakeKernel32(fail=("OpenProcess",))
        assert qos.apply_background_qos(5, kernel32=k32) is False
        assert k32.names() == ["OpenProcess"]

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX inertness")
    def test_inert_off_windows(self) -> None:
        assert qos.apply_background_qos(os.getpid()) is False


class TestSetJobCpuWeight:
    def test_weight_based_rate_control(self) -> None:
        k32 = _FakeKernel32()
        assert qos.set_job_cpu_weight(k32, 9, qos.BACKGROUND_JOB_CPU_WEIGHT) is True
        assert k32.calls == [("SetInformationJobObject", 9, 15, 0x3, 3, 8)]

    @pytest.mark.parametrize("weight", [0, 10])
    def test_out_of_range_weight_is_not_sent(self, weight: int) -> None:
        k32 = _FakeKernel32()
        assert qos.set_job_cpu_weight(k32, 9, weight) is False
        assert k32.calls == []

    def test_refusal_reports_false(self) -> None:
        k32 = _FakeKernel32(fail=("SetInformationJobObject",))
        assert qos.set_job_cpu_weight(k32, 9, 3) is False


class TestResourceCeilingDecision:
    """``apply_windows_resource_ceiling`` marks only an opted-in background spawn."""

    @pytest.fixture
    def recorded(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
        seen: dict[str, list[Any]] = {"job": [], "qos": []}
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(sandbox_slice, "_warn_windows_inert_resource_limits_once", lambda: None)
        monkeypatch.setattr(
            platform_compat,
            "apply_job_limits",
            lambda pid, **kw: seen["job"].append(kw["cpu_weight"]) or True,
        )
        monkeypatch.setattr(
            qos, "apply_background_qos", lambda pid: seen["qos"].append(pid) or True
        )
        return seen

    @staticmethod
    def _config(monkeypatch: pytest.MonkeyPatch, agent: object, local: object = None) -> None:
        import json

        from kiro_crew.config.loader import config_local_path, config_path

        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps({"agent": agent}), encoding="utf-8")
        if local is not None:
            config_local_path().write_text(json.dumps({"agent": local}), encoding="utf-8")
        else:
            config_local_path().unlink(missing_ok=True)

    @pytest.mark.parametrize(
        ("base", "local", "marked"),
        [
            ({"windows_background_qos": True}, {"windows_background_qos": False}, False),
            ({}, {"windows_background_qos": True}, True),
        ],
    )
    def test_the_local_override_wins(self, monkeypatch, recorded, base, local, marked) -> None:
        self._config(monkeypatch, base, local)
        sandbox_slice.apply_windows_resource_ceiling(31, background=True)
        assert recorded["qos"] == ([31] if marked else [])

    def test_opted_in_background_spawn_is_marked(self, monkeypatch, recorded) -> None:
        self._config(monkeypatch, {"windows_background_qos": True})
        assert sandbox_slice.apply_windows_resource_ceiling(31, background=True) is True
        assert recorded == {"job": [qos.BACKGROUND_JOB_CPU_WEIGHT], "qos": [31]}

    @pytest.mark.parametrize(
        ("agent", "background"),
        [
            ({"windows_background_qos": True}, False),
            ({"windows_background_qos": False}, True),
            ({"windows_background_qos": "true"}, True),
            ({}, True),
            (None, True),
        ],
    )
    def test_anything_else_keeps_todays_ceiling(
        self, monkeypatch, recorded, agent: object, background: bool
    ) -> None:
        self._config(monkeypatch, agent)
        assert sandbox_slice.apply_windows_resource_ceiling(31, background=background) is True
        assert recorded == {"job": [0], "qos": []}


def test_finish_suspended_spawn_hands_the_background_role_to_the_ceiling(monkeypatch) -> None:
    from kiro_crew.acp import client as acp_client

    calls: list[tuple[int, dict[str, Any]]] = []
    monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(
        acp_client,
        "apply_windows_resource_ceiling",
        lambda pid, **kw: calls.append((pid, kw)) or False,
    )
    acp_client.finish_suspended_spawn(object(), 7, label="kiro-cli acp", background=True)  # type: ignore[arg-type]
    acp_client.finish_suspended_spawn(object(), 8, label="kiro-cli acp")  # type: ignore[arg-type]
    assert calls == [(7, {"background": True}), (8, {})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("priority", "marked"),
    [(StartPriority.BACKGROUND, True), (StartPriority.FOREGROUND, False)],
)
async def test_companion_runtime_is_background_unless_a_person_started_it(
    monkeypatch, priority: StartPriority, marked: bool
) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session import SessionManager

    built: list[bool] = []

    class _Runtime:
        def __init__(self, agent=None, background_qos=False):
            built.append(background_qos)

        async def spawn(self, start_priority=None):
            return None

        def is_alive(self):
            return True

    monkeypatch.setattr("kiro_crew.acp.runtime.AcpRuntime", _Runtime)
    sm = SessionManager(KiroCrewConfig.load())
    sm._get_session_agent = lambda k: "kirocrew"  # type: ignore[assignment]
    await sm.get_subagent_runtime("dashboard:slot1", start_priority=priority)
    assert built == [marked]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("priority", "marked"),
    [(StartPriority.BACKGROUND, True), (StartPriority.FOREGROUND, False)],
)
async def test_task_run_runtime_is_background_unless_a_person_opened_it(
    priority: StartPriority, marked: bool
) -> None:
    from unittest.mock import AsyncMock

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session import SessionManager

    seen: list[bool] = []

    class _Provider:
        background_qos = False

        async def start(self) -> None:
            seen.append(self.background_qos)

        def disown_work_dir(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    sm = SessionManager(KiroCrewConfig.load(), provider_factory=lambda *a, **kw: _Provider())
    sm.get_subagent_runtime = AsyncMock(return_value=object())  # type: ignore[method-assign]
    await sm._get_or_bootstrap_run_runtime("taskrunner:run", start_priority=priority)
    assert seen == [marked]


def test_acp_runtime_defaults_to_foreground() -> None:
    from kiro_crew.acp.runtime import AcpRuntime

    assert AcpRuntime()._background_qos is False
    assert AcpRuntime(background_qos=True)._background_qos is True


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="reads a real Windows priority class")
def test_a_real_child_reads_back_below_normal() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.readline()"],
        stdin=subprocess.PIPE,
    )
    try:
        assert qos.apply_background_qos(child.pid) is True
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = k32.OpenProcess(0x1000, False, child.pid)  # QUERY_LIMITED_INFORMATION
        try:
            k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
            assert k32.GetPriorityClass(handle) == 0x4000
        finally:
            k32.CloseHandle(handle)
    finally:
        child.communicate(b"\n", timeout=30)  # flake-ok: bound on reaping a child we just released
