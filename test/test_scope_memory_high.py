"""Each agent runtime scope carries its own finite ``MemoryHigh``.

Every runtime runs in its own transient scope under the agents slice. A scope at
``memory.high=max`` is reclaimed only at the SHARED slice ``MemoryHigh``, where
every runtime is throttled together. ``cgroup_scope_argv`` emits ``MemoryHigh`` at
``resource_limits.scope_memory_high_pct`` of the scope's own ``MemoryMax``
(default 90), so a runtime that grows past it reclaims on its own first.

These tests drive the REAL ``cgroup_scope_argv`` and the REAL config parse:
only the host probes (cgroup delegation, the trusted ``systemd-run`` path, the
off-thread slice reconciliation) and the raw config dict are stubbed.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import kiro_crew.sandbox as sb
from kiro_crew.config.loader import _WARNED_RESOURCE_LIMIT_KEYS, ResourceLimitsConfig


@pytest.fixture(autouse=True)
def _reset_state():
    sb._CGROUP_SCOPE_PROBE = None
    sb._CGROUP_WARNED = False
    _WARNED_RESOURCE_LIMIT_KEYS.clear()
    yield
    sb._CGROUP_SCOPE_PROBE = None
    sb._CGROUP_WARNED = False
    _WARNED_RESOURCE_LIMIT_KEYS.clear()


def _wrap(
    raw: dict | None,
    *,
    available: bool = True,
    cpu: bool = False,
    default_mem_mb: int | None = None,
    token: str | None = None,
) -> list[str]:
    """``cgroup_scope_argv(["kiro-cli", "chat"])`` against *raw* ``resource_limits``."""
    extra = []
    if default_mem_mb is not None:
        extra.append(patch("kiro_crew.sandbox._default_max_memory_mb", return_value=default_mem_mb))
    with (
        patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(available, "test")),
        patch("kiro_crew.platform_compat.trusted_system_bin", return_value="/usr/bin/systemd-run"),
        patch("kiro_crew.sandbox._reconcile_slice_memory_high_off_thread"),
        patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=cpu),
        patch(
            "kiro_crew.config.loader._raw_config",
            return_value={} if raw is None else {"resource_limits": raw},
        ),
    ):
        for p in extra:
            p.start()
        try:
            argv = sb.cgroup_scope_argv(["kiro-cli", "chat"])
            return argv if token is None else sb.name_scope_unit(argv, token)
        finally:
            for p in extra:
                p.stop()


def _prop(argv: list[str], name: str) -> int | None:
    """The MB value of ``-p <name>=<N>M`` in *argv*, or None when absent."""
    values = [a for a in argv if a.startswith(f"{name}=")]
    assert len(values) <= 1, values
    if not values:
        return None
    # Every memory property is passed as its own ``-p`` argument.
    assert argv[argv.index(values[0]) - 1] == "-p"
    return int(values[0].split("=", 1)[1].rstrip("M"))


class TestScopeArgv:
    def test_default_is_a_finite_memory_high_below_memory_max(self):
        argv = _wrap({"max_memory_mb": 10000})
        assert _prop(argv, "MemoryMax") == 10000
        assert _prop(argv, "MemoryHigh") == 9000
        # Next to MemoryMax, before the ``--`` that starts the wrapped command.
        assert argv.index("MemoryHigh=9000M") == argv.index("MemoryMax=10000M") + 2
        assert argv.index("MemoryHigh=9000M") < argv.index("--")
        assert argv[argv.index("--") + 1 :] == ["kiro-cli", "chat"]

    def test_the_percent_reads_from_config(self):
        assert (
            _prop(_wrap({"max_memory_mb": 10000, "scope_memory_high_pct": 50}), "MemoryHigh")
            == 5000
        )

    def test_zero_removes_the_property(self):
        """The off switch: the scope stays at ``memory.high=max``."""
        argv = _wrap({"max_memory_mb": 10000, "scope_memory_high_pct": 0})
        assert _prop(argv, "MemoryHigh") is None
        assert _prop(argv, "MemoryMax") == 10000
        assert "MemorySwapMax=0" in argv

    def test_one_hundred_equals_memory_max(self):
        argv = _wrap({"max_memory_mb": 4096, "scope_memory_high_pct": 100})
        assert _prop(argv, "MemoryHigh") == _prop(argv, "MemoryMax") == 4096

    @pytest.mark.parametrize("junk", [-5, 101, 1000, 0.5, True, "90", None, float("nan"), [90]])
    def test_junk_falls_back_to_the_default(self, junk):
        argv = _wrap({"max_memory_mb": 10000, "scope_memory_high_pct": junk})
        assert _prop(argv, "MemoryHigh") == 9000, junk

    def test_follows_the_host_proportional_memory_max_when_none_is_configured(self):
        """A tiny host: MemoryMax is the host-derived default, and MemoryHigh
        tracks it rather than a fixed number."""
        argv = _wrap(None, default_mem_mb=1300)
        assert _prop(argv, "MemoryMax") == 1300
        assert _prop(argv, "MemoryHigh") == 1170

    def test_never_below_one_megabyte(self):
        """``MemoryHigh=0M`` would throttle the scope from its first page."""
        argv = _wrap({"max_memory_mb": 1, "scope_memory_high_pct": 1})
        assert _prop(argv, "MemoryMax") == 1
        assert _prop(argv, "MemoryHigh") == 1

    @pytest.mark.parametrize("cpu", [True, False])
    def test_independent_of_the_cpu_controller(self, cpu):
        argv = _wrap({"max_memory_mb": 10000}, cpu=cpu)
        assert _prop(argv, "MemoryHigh") == 9000
        assert any(a.startswith("CPUWeight=") for a in argv) is cpu

    def test_no_delegation_returns_argv_unchanged(self):
        assert _wrap({"scope_memory_high_pct": 50}, available=False) == ["kiro-cli", "chat"]

    def test_one_scope_start_reads_the_config_once(self):
        """Every limit on the scope comes from one parse of ``config.json``."""
        with (
            patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "test")),
            patch(
                "kiro_crew.platform_compat.trusted_system_bin",
                return_value="/usr/bin/systemd-run",
            ),
            patch("kiro_crew.sandbox._reconcile_slice_memory_high_off_thread"),
            patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=True),
            patch(
                "kiro_crew.config.loader._raw_config",
                return_value={
                    "resource_limits": {
                        "max_memory_mb": 10000,
                        "max_processes": 300,
                        "scope_memory_high_pct": 50,
                    }
                },
            ) as raw,
        ):
            argv = sb.cgroup_scope_argv(["kiro-cli", "chat"])
        assert raw.call_count == 1
        assert "TasksMax=300" in argv
        assert _prop(argv, "MemoryHigh") == 5000

    def test_name_scope_unit_still_names_the_result(self):
        named = _wrap({"max_memory_mb": 10000}, token="deadbeef")
        at = named.index("--unit")
        assert named[at + 1] == "kirocrew-rt-deadbeef.scope"
        assert at < named.index("--")
        assert "MemoryHigh=9000M" in named


class TestParse:
    def test_accepted_range_is_zero_to_one_hundred(self):
        for value in (0, 1, 90, 100):
            parsed = ResourceLimitsConfig.from_raw({"scope_memory_high_pct": value})
            assert parsed.scope_memory_high_pct == value
        for value in (-1, 101):
            parsed = ResourceLimitsConfig.from_raw({"scope_memory_high_pct": value})
            assert parsed.scope_memory_high_pct is None

    def test_inert_on_windows(self):
        assert "scope_memory_high_pct" in sb._WINDOWS_INERT_RESOURCE_LIMIT_KEYS


GIB = 1024**3

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="the slice resolver is Linux-only")


def _slice(tmp_path: Path, *, tree_high: int, own_high: int | None) -> Path:
    """A slice whose subtree counted *tree_high* ``high`` events, *own_high* of
    them its own (``None``: a kernel without ``memory.events.local``).
    Usage stays well under the slice ceiling, so only the counter can trip."""
    d = tmp_path / "kirocrew.slice" / sb._CGROUP_AGENTS_SLICE
    d.mkdir(parents=True, exist_ok=True)
    (d / "memory.high").write_text(f"{20 * GIB}\n", encoding="utf-8")
    (d / "memory.max").write_text(f"{26 * GIB}\n", encoding="utf-8")
    (d / "memory.current").write_text(f"{5 * GIB}\n", encoding="utf-8")
    (d / "memory.events").write_text(f"low 0\nhigh {tree_high}\nmax 0\n", encoding="utf-8")
    if own_high is not None:
        (d / "memory.events.local").write_text(f"low 0\nhigh {own_high}\nmax 0\n", encoding="utf-8")
    return d


@linux_only
class TestSliceProbesCountOnlyTheSliceOwnCeiling:
    """A scope over its OWN MemoryHigh raises the slice's hierarchical
    ``memory.events`` ``high`` count too. The slice probes must not read that as
    the whole agent subtree being throttled."""

    @pytest.fixture(autouse=True)
    def _probe_state(self, tmp_path, _floor_monkeypatch):
        _floor_monkeypatch.setattr(sb, "_USER_MANAGER_CGROUP_BASE", str(tmp_path))
        _floor_monkeypatch.setattr(sb, "_SLICE_THROTTLE_PROBE_SEEN", None)
        _floor_monkeypatch.setattr(sb, "_SLICE_THROTTLE_EDGE_AT", None)
        _floor_monkeypatch.setattr(sb, "_SLICE_MEMHIGH_EVENTS_SEEN", None)
        _floor_monkeypatch.setattr(sb, "_SLICE_MEMHIGH_CLIMB_WARNED", False)
        _floor_monkeypatch.setattr(sb, "time", SimpleNamespace(monotonic=lambda: 5000.0))

    def test_a_scope_event_is_not_slice_throttling(self, tmp_path):
        _slice(tmp_path, tree_high=10, own_high=0)
        assert sb.agents_slice_throttling() is False
        _slice(tmp_path, tree_high=500, own_high=0)
        assert sb.agents_slice_throttling() is False

    def test_a_slice_event_still_is(self, tmp_path):
        _slice(tmp_path, tree_high=10, own_high=0)
        assert sb.agents_slice_throttling() is False
        _slice(tmp_path, tree_high=11, own_high=1)
        assert sb.agents_slice_throttling() is True

    def test_the_climb_warning_ignores_scope_events(self, tmp_path, caplog):
        _slice(tmp_path, tree_high=10, own_high=0)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
            sb._check_slice_memory_pressure()
            _slice(tmp_path, tree_high=500, own_high=0)
            sb._check_slice_memory_pressure()
            assert "high counter climbed" not in caplog.text
            _slice(tmp_path, tree_high=501, own_high=1)
            sb._check_slice_memory_pressure()
        assert "high counter climbed 0 -> 1" in caplog.text

    def test_without_the_local_file_there_is_no_counter_signal(self, tmp_path, caplog):
        """The hierarchical count cannot tell a scope's event from the slice's,
        so a host without ``memory.events.local`` reports no counter climb."""
        _slice(tmp_path, tree_high=10, own_high=None)
        assert sb.agents_slice_throttling() is False
        _slice(tmp_path, tree_high=500, own_high=None)
        assert sb.agents_slice_throttling() is False
        assert sb._slice_memory_events_high() is None
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
            sb._check_slice_memory_pressure()
            sb._check_slice_memory_pressure()
        assert "high counter climbed" not in caplog.text

    def test_without_the_local_file_usage_at_the_ceiling_still_throttles(self, tmp_path):
        d = _slice(tmp_path, tree_high=10, own_high=None)
        (d / "memory.current").write_text(f"{20 * GIB}\n", encoding="utf-8")
        assert sb.agents_slice_throttling() is True

    def test_an_empty_local_file_gives_no_counter_signal(self, tmp_path):
        d = _slice(tmp_path, tree_high=10, own_high=None)
        (d / "memory.events.local").write_text("", encoding="utf-8")
        assert sb.agents_slice_throttling() is False
        _slice(tmp_path, tree_high=500, own_high=None)
        assert sb.agents_slice_throttling() is False
