"""An unreadable cgroup usage file is named in the spawn deferral.

A finite cgroup memory limit whose usage cannot be read gives zero headroom, so
the spawn is still deferred. These tests pin both halves: the guard does not
admit, and the deferral text says the headroom is unknown instead of low.

Every kernel input is fabricated through ``subagent``'s own seams (``open``,
``_read_int_file``, ``_cgroup_memory_roots``), so the tests run the same on
every OS.
"""

from __future__ import annotations

import threading
from contextlib import ExitStack
from io import StringIO
from pathlib import PurePosixPath
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.subagent as sa

GIB = 1024**3
ROOT = PurePosixPath("/sys/fs/cgroup")


@pytest.fixture
def kernel(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A Linux host with one v2 mount at ROOT and a synthetic file table."""
    files: dict[str, object] = {"/proc/meminfo": f"MemAvailable: {32 * 1024 * 1024} kB\n"}

    def fake_open(path, *args, **kwargs):
        value = files.get(str(path))
        if value is None:
            raise FileNotFoundError(path)
        if isinstance(value, Exception):
            raise value
        return StringIO(str(value))

    def fake_int(path: str):
        value = files.get(path)
        return value if isinstance(value, int) else None

    monkeypatch.setattr(sa.platform_compat, "IS_LINUX", True, raising=False)
    monkeypatch.setattr(sa, "open", fake_open, raising=False)
    monkeypatch.setattr(sa, "_read_int_file", fake_int)
    monkeypatch.setattr(sa, "_cgroup_memory_roots", lambda: [(ROOT, ROOT, True)])
    monkeypatch.setattr(sa, "_agents_slice_available_gb", lambda: -1.0)
    monkeypatch.setattr(sa, "_memory_check_cause", threading.local())
    return files


def test_unreadable_usage_defers_and_names_the_cause(kernel) -> None:
    kernel[f"{ROOT}/memory.max"] = 8 * GIB
    kernel[f"{ROOT}/memory.current"] = PermissionError("memory.current")
    assert sa.check_memory_available(min_gb=4.0) == (False, 0.0)
    assert sa.pop_memory_check_cause() == sa.MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE
    assert sa.pop_memory_check_cause() == ""


def test_measured_low_memory_has_no_cause(kernel) -> None:
    kernel[f"{ROOT}/memory.max"] = 8 * GIB
    kernel[f"{ROOT}/memory.current"] = 7 * GIB
    assert sa.check_memory_available(min_gb=4.0) == (False, 1.0)
    assert sa.pop_memory_check_cause() == ""


def test_unlimited_or_absent_limit_uses_host_figure(kernel) -> None:
    assert sa.check_memory_available(min_gb=4.0) == (True, 32.0)
    assert sa.pop_memory_check_cause() == ""
    kernel[f"{ROOT}/memory.max"] = sa._CGROUP_UNLIMITED
    assert sa.check_memory_available(min_gb=4.0) == (True, 32.0)
    assert sa.pop_memory_check_cause() == ""


def test_unreadable_host_and_usage_still_not_admitted(kernel) -> None:
    kernel["/proc/meminfo"] = OSError("meminfo")
    kernel[f"{ROOT}/memory.max"] = 8 * GIB
    assert sa.check_memory_available(min_gb=4.0) == (False, 0.0)
    assert sa.pop_memory_check_cause() == sa.MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE


def test_a_probe_on_another_thread_does_not_set_this_threads_cause(kernel) -> None:
    kernel[f"{ROOT}/memory.max"] = 8 * GIB
    worker = threading.Thread(target=sa.check_memory_available, kwargs={"min_gb": 4.0})
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert sa.pop_memory_check_cause() == ""


def test_finite_ancestor_with_unreadable_usage(kernel, monkeypatch) -> None:
    leaf = ROOT / "kirocrew.service"
    monkeypatch.setattr(sa, "_cgroup_memory_roots", lambda: [(leaf, ROOT, True)])
    kernel[f"{ROOT}/memory.max"] = 8 * GIB  # ancestor limit, usage unreadable
    assert sa.check_memory_available(min_gb=4.0) == (False, 0.0)
    assert sa.pop_memory_check_cause() == sa.MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE


def _mgr():
    sessions = MagicMock()
    sessions.get_agent_selection.return_value = ("template", "")
    return sa.SubagentManager(
        sessions=sessions, ctx_builder=MagicMock(), on_done=MagicMock(), max_concurrent=3
    )


def _spawn(mgr, *, min_gb: float = 4.0, memory=None):
    """Spawn with the memory guard on; ``memory`` mocks the check when given."""
    with ExitStack() as stack:
        cfg = stack.enter_context(patch("kiro_crew.subagent.KiroCrewConfig"))
        sel = stack.enter_context(patch("kiro_crew.subagent.sel"))
        if memory is not None:
            stack.enter_context(
                patch("kiro_crew.subagent.check_memory_available", return_value=memory)
            )
        cfg.load.return_value.agent.spawn_min_memory_gb = min_gb
        cfg.load.return_value.agent.subagent_cost_gb = 0.5
        sel.return_value.log_tool_invocation = MagicMock()
        info = mgr.spawn(task="test task", parent_session_key="sess-1")
    return info, sel.return_value.log_tool_invocation


def _deferral_reason(mgr, info) -> str:
    deferred = [e for e in mgr._taskq.events(info.id) if e.kind == "deferred"]
    return str(deferred[-1].data.get("reason")) if deferred else ""


def test_spawn_deferral_says_usage_is_unreadable(kernel) -> None:
    kernel[f"{ROOT}/memory.max"] = 8 * GIB
    mgr = _mgr()
    info, log = _spawn(mgr)
    assert info is not None and info.queued is True
    reason = _deferral_reason(mgr, info)
    assert "headroom unknown" in reason and "unreadable" in reason
    assert "low memory:" not in reason
    meta = log.call_args[1]["metadata"]
    assert log.call_args[1]["outcome"] == "deferred_low_memory"
    assert meta["cause"] == sa.MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE


def test_spawn_deferral_for_measured_low_memory_is_unchanged(kernel) -> None:
    kernel[f"{ROOT}/memory.max"] = 8 * GIB
    kernel[f"{ROOT}/memory.current"] = 7 * GIB
    mgr = _mgr()
    info, log = _spawn(mgr)
    assert info is not None and info.queued is True
    assert _deferral_reason(mgr, info).startswith("low memory: 1.0 GB available")
    assert "cause" not in log.call_args[1]["metadata"]


def test_stale_cause_does_not_leak_into_a_mocked_check(kernel, monkeypatch) -> None:
    sa._memory_check_cause.value = sa.MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE
    mgr = _mgr()
    info, _log = _spawn(mgr, memory=(False, 3.0))
    assert _deferral_reason(mgr, info).startswith("low memory: 3.0 GB available")


def test_zero_floor_skips_the_guard(kernel) -> None:
    kernel[f"{ROOT}/memory.max"] = 8 * GIB
    mgr = _mgr()
    info, log = _spawn(mgr, min_gb=0.0)
    assert info is not None and info.queued is not True
    outcomes = [c[1].get("outcome") for c in log.call_args_list]
    assert "deferred_low_memory" not in outcomes
