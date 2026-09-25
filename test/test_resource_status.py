"""Tests for the advisory resource probe and its two surfaces.

Covers :mod:`kiro_crew.resource_status` (posture classification, context line,
disable switch, fail-open) and the ``resource_status`` pull tool wired into
``mcp_core`` (advertised + dispatchable).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import resource_status as rs


def _cfg(pressure: float, critical: float) -> SimpleNamespace:
    """Minimal stand-in for KiroCrewConfig exposing the two thresholds."""
    return SimpleNamespace(
        agent=SimpleNamespace(
            resource_pressure_gb=pressure,
            resource_critical_gb=critical,
        )
    )


@pytest.fixture(autouse=True)
def _unmeasurable_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every case here to a slice whose task count cannot be read.

    ``probe`` reads the real host's agent slice, so without this default the
    memory assertions would turn on whatever the machine running them happens to
    hold: a host sitting above the tight ratio makes an "ample memory is silent"
    case emit a task line and fail. The cases that exercise the task figure patch
    it themselves, and those that exercise the READ call
    :data:`_REAL_SLICE_PROBE`, captured past this default.
    """
    monkeypatch.setattr(rs, "_read_agent_slice_tasks", lambda: (-1, -1, -1))


#: The genuine probe, bound at import before the autouse default can replace it.
_REAL_SLICE_PROBE = rs._read_agent_slice_tasks


# ── _classify ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "avail,expected",
    [
        (16.0, rs.POSTURE_AMPLE),
        (4.01, rs.POSTURE_AMPLE),
        (4.0, rs.POSTURE_TIGHT),   # boundary: <= pressure
        (2.5, rs.POSTURE_TIGHT),
        (2.0, rs.POSTURE_CRITICAL),  # boundary: <= critical
        (0.5, rs.POSTURE_CRITICAL),
        (-1.0, rs.POSTURE_UNKNOWN),  # unreadable probe → fail open
    ],
)
def test_classify_buckets(avail: float, expected: str) -> None:
    assert rs._classify(avail, pressure_gb=4.0, critical_gb=2.0) == expected


def test_zero_thresholds_disable_buckets() -> None:
    # pressure=0 → never tight; critical=0 → never critical, for any positive avail.
    assert rs._classify(0.1, pressure_gb=0.0, critical_gb=0.0) == rs.POSTURE_AMPLE
    assert rs._classify(0.1, pressure_gb=4.0, critical_gb=0.0) == rs.POSTURE_TIGHT


# ── probe ──────────────────────────────────────────────────────────────────────


def test_probe_tight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 3.0)
    status = rs.probe(_cfg(4.0, 2.0))
    assert status.posture == rs.POSTURE_TIGHT
    assert status.under_pressure is True
    assert status.available_gb == 3.0
    assert status.cpu_count >= 1


def test_probe_ample_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 32.0)
    status = rs.probe(_cfg(4.0, 2.0))
    assert status.posture == rs.POSTURE_AMPLE
    assert status.under_pressure is False
    assert status.context_line() == ""


def test_probe_unknown_when_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: -1.0)
    status = rs.probe(_cfg(4.0, 2.0))
    assert status.posture == rs.POSTURE_UNKNOWN
    assert status.under_pressure is False
    assert status.context_line() == ""


def test_probe_never_raises_on_bad_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 3.0)
    # Garbage thresholds fall back to defaults rather than raising.
    status = rs.probe(_cfg("nan", None))  # type: ignore[arg-type]
    assert status.pressure_gb == rs._DEFAULT_PRESSURE_GB
    assert status.critical_gb == rs._DEFAULT_CRITICAL_GB


# ── context_line ────────────────────────────────────────────────────────────────


def test_context_line_tight_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 3.0)
    line = rs.probe(_cfg(4.0, 2.0)).context_line()
    assert line.startswith("[RESOURCES]")
    assert "tight" in line
    assert "resource_status" in line  # points the model at the pull tool


def test_context_line_critical_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 1.0)
    line = rs.probe(_cfg(4.0, 2.0)).context_line()
    assert line.startswith("[RESOURCES]")
    assert "CRITICALLY" in line


def test_load_suffix_omitted_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 3.0)
    monkeypatch.setattr(rs, "_read_load_per_cpu", lambda cpu: None)
    line = rs.probe(_cfg(4.0, 2.0)).context_line()
    assert "load" not in line


def test_read_load_per_cpu_handles_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> tuple[float, float, float]:
        raise OSError("no loadavg")

    monkeypatch.setattr(rs.os, "getloadavg", _boom, raising=False)
    assert rs._read_load_per_cpu(4) is None
    assert rs._read_load_per_cpu(0) is None


# ── summary_lines ────────────────────────────────────────────────────────────────


def test_summary_lines_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 3.0)
    lines = rs.probe(_cfg(4.0, 2.0)).summary_lines()
    joined = "\n".join(lines)
    assert "Available memory: 3.0 GB" in joined
    assert "Posture: TIGHT" in joined


def test_summary_lines_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: -1.0)
    joined = "\n".join(rs.probe(_cfg(4.0, 2.0)).summary_lines())
    assert "unknown" in joined.lower()


# ── mcp_core pull tool ──────────────────────────────────────────────────────────


def test_tool_is_advertised() -> None:
    from kiro_crew import mcp_core

    names = {t["name"] for t in mcp_core._list_tools()}
    assert "resource_status" in names


def test_tool_dispatch_returns_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import mcp_core

    fake = rs.ResourceStatus(
        available_gb=3.0,
        cpu_count=8,
        load_per_cpu=0.5,
        posture=rs.POSTURE_TIGHT,
        pressure_gb=4.0,
        critical_gb=2.0,
    )
    monkeypatch.setattr(rs, "probe", lambda cfg=None: fake)
    out = mcp_core._call_tool_inner("resource_status", {})
    assert "Posture: TIGHT" in out
    assert "Guidance:" in out


# ── review-round fixes: off-switch, invariant clamp, schema registration ──────


def test_off_switch_disables_line_not_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    # pressure_gb=0 disables the injected line even at critically low memory,
    # but the true posture is still reported for the pull tool.
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 1.0)
    status = rs.probe(_cfg(0.0, 2.0))
    assert status.posture == rs.POSTURE_CRITICAL
    assert status.context_line() == ""            # line suppressed
    assert "CRITICAL" in "\n".join(status.summary_lines())  # tool still honest


def test_thresholds_clamped_when_inverted(monkeypatch: pytest.MonkeyPatch) -> None:
    # critical > pressure is clamped so the tight tier stays reachable.
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 6.0)
    status = rs.probe(_cfg(4.0, 8.0))
    assert status.critical_gb == 4.0
    # 6 GB is above the (clamped) 4 GB pressure line → ample, not critical.
    assert status.posture == rs.POSTURE_AMPLE


def test_tool_registered_and_rejects_stray_args() -> None:
    from kiro_crew.validation import (
        MCP_CORE_SCHEMAS,
        ValidationError,
        validate_tool_args,
    )

    assert "resource_status" in MCP_CORE_SCHEMAS
    schema = MCP_CORE_SCHEMAS["resource_status"]
    assert validate_tool_args({}, schema) == {}          # zero-arg call is valid
    with pytest.raises(ValidationError):
        validate_tool_args({"bogus": 1}, schema)          # stray arg rejected


# ── prewarm_allowance: host-derived cap on idle pre-warmed sessions ─────────


@pytest.mark.parametrize(
    "avail,expected",
    [
        (0.5, 0),   # critical band: no idle agent process at all
        (2.0, 0),   # inclusive, like _classify
        (2.01, 1),  # tight band: one
        (4.0, 1),
        (4.01, 3),  # ample: the historical fixed cap
        (64.0, 3),
    ],
)
def test_prewarm_allowance_bands(avail: float, expected: int) -> None:
    assert rs.prewarm_allowance(avail, _cfg(4.0, 2.0)) == expected


@pytest.mark.parametrize(
    "avail,expected",
    [
        (6.0, 0),    # under the tuned critical band
        (8.0, 0),    # inclusive, like _classify
        (8.01, 1),   # tuned tight band
        (16.0, 1),
        (16.01, 3),  # ample on the tuned scale
        (64.0, 3),
    ],
)
def test_prewarm_allowance_follows_tuned_thresholds(avail: float, expected: int) -> None:
    """A host tuned via ``agent.resource_pressure_gb`` / ``resource_critical_gb``
    gets an allowance on the SAME scale as its posture: the bands come from
    ``_resolve_thresholds(cfg)``, not from the shipped defaults."""
    tuned = _cfg(16.0, 8.0)
    assert rs.prewarm_allowance(avail, tuned) == expected
    # Cross-check against the posture for the same reading and config.
    posture = rs._classify(avail, *rs._resolve_thresholds(tuned))
    assert rs.prewarm_allowance(avail, tuned) == rs._PREWARM_BY_POSTURE[posture]


def test_prewarm_allowance_disabled_pressure_keeps_the_cap() -> None:
    """``pressure_gb == 0`` is the documented off switch for the posture line;
    the allowance honours it the same way and never shrinks the population."""
    off = _cfg(0.0, 0.0)
    for avail in (0.5, 2.0, 4.0, 64.0):
        assert rs.prewarm_allowance(avail, off) == rs.PREWARM_MAX_LIVE


def test_prewarm_allowance_loads_config_when_omitted(monkeypatch) -> None:
    """Omitting *cfg* resolves the bands through the same loader ``probe`` uses."""
    monkeypatch.setattr(rs, "_load_config", lambda: _cfg(16.0, 8.0))
    assert rs.prewarm_allowance(6.0) == 0
    assert rs.prewarm_allowance(12.0) == 1
    assert rs.prewarm_allowance(20.0) == rs.PREWARM_MAX_LIVE


def test_prewarm_allowance_unreadable_probe_keeps_the_fixed_cap(monkeypatch) -> None:
    """A host the probe cannot measure is never made worse by the allowance."""
    monkeypatch.setattr(rs, "_read_available_gb", lambda: -1.0)
    assert rs.prewarm_allowance() == rs.PREWARM_MAX_LIVE
    assert rs.prewarm_allowance(-1.0) == rs.PREWARM_MAX_LIVE


def test_prewarm_allowance_reads_the_shared_probe(monkeypatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 1.0)
    assert rs.prewarm_allowance(cfg=_cfg(4.0, 2.0)) == 0
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 3.0)
    assert rs.prewarm_allowance(cfg=_cfg(4.0, 2.0)) == 1


def test_prewarm_allowance_tracks_the_advisory_thresholds() -> None:
    """The bands are the posture thresholds, not a second scale: a host that
    reads CRITICAL pre-warms nothing and one that reads TIGHT pre-warms one."""
    defaults = _cfg(rs._DEFAULT_PRESSURE_GB, rs._DEFAULT_CRITICAL_GB)
    assert rs.prewarm_allowance(rs._DEFAULT_CRITICAL_GB - 0.01, defaults) == 0
    assert rs._classify(rs._DEFAULT_CRITICAL_GB - 0.01, rs._DEFAULT_PRESSURE_GB,
                        rs._DEFAULT_CRITICAL_GB) == rs.POSTURE_CRITICAL
    assert rs.prewarm_allowance(rs._DEFAULT_PRESSURE_GB - 0.01, defaults) == 1
    assert rs._classify(rs._DEFAULT_PRESSURE_GB - 0.01, rs._DEFAULT_PRESSURE_GB,
                        rs._DEFAULT_CRITICAL_GB) == rs.POSTURE_TIGHT


# ── agent-slice task ceiling ─────────────────────────────────────────────────
#
# The slice's pids ceiling is the one whose breach fails fork for every agent
# under that slice at once, so these cases cover the read, the tight band, both
# render paths, and the invariance the design promises: reported, never gated.

_TOKEN = "kirocrew-agents-abc123def456.slice"


def _slice_tree(
    tmp_path,
    current: str = "29500\n",
    limit: str = "32768\n",
    *,
    own: str | None = "22000\n",
    token: str = _TOKEN,
):
    """Build a fake agent-slice cgroup tree; returns its directory."""
    slice_dir = tmp_path / "kirocrew-agents.slice"
    slice_dir.mkdir()
    (slice_dir / "pids.current").write_text(current, encoding="utf-8")
    (slice_dir / "pids.max").write_text(limit, encoding="utf-8")
    if own is not None:
        child = slice_dir / token
        child.mkdir()
        (child / "pids.current").write_text(own, encoding="utf-8")
    return slice_dir


def _patch_slice(monkeypatch, slice_dir, token: str = _TOKEN) -> None:
    """Point the probe at a fake tree, through the real cgroup reader."""
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_agents_slice_cgroup_dir", lambda: slice_dir)
    monkeypatch.setattr(sandbox, "_agents_slice_name", lambda: token)


def _tasks(monkeypatch, current: int, limit: int, own: int = -1):
    """Stub the task figures directly, for the render and gate cases."""
    monkeypatch.setattr(rs, "_read_agent_slice_tasks", lambda: (current, limit, own))


def test_slice_tasks_read_from_cgroup(tmp_path, monkeypatch) -> None:
    """All three figures come from the real single-value cgroup reader."""
    _patch_slice(monkeypatch, _slice_tree(tmp_path))
    assert _REAL_SLICE_PROBE() == (29500, 32768, 22000)


def test_slice_tasks_max_sentinel_means_no_ceiling(tmp_path, monkeypatch) -> None:
    """``pids.max`` holding ``max`` is a slice with no wall, reported as 0."""
    _patch_slice(monkeypatch, _slice_tree(tmp_path, limit="max\n"))
    current, limit, _own = _REAL_SLICE_PROBE()
    assert (current, limit) == (29500, 0)


def test_slice_tasks_unreadable_ceiling_is_not_an_absent_one(tmp_path, monkeypatch) -> None:
    """A ceiling that cannot be read must not be published as "no ceiling".

    A slice released between the directory check and this read fails the read,
    and reporting that as an absent limit is a reassurance nothing measured. The
    two answers are kept distinct: ``0`` is the kernel's sentinel, ``-1`` is a
    failure.
    """
    slice_dir = _slice_tree(tmp_path)
    (slice_dir / "pids.max").unlink()
    _patch_slice(monkeypatch, slice_dir)
    current, limit, _own = _REAL_SLICE_PROBE()
    assert current == 29500
    assert limit == -1


def test_slice_tasks_unparseable_ceiling_is_unreadable(tmp_path, monkeypatch) -> None:
    """Content that is neither the sentinel nor digits reads as unknown."""
    _patch_slice(monkeypatch, _slice_tree(tmp_path, limit="not-a-number\n"))
    _current, limit, _own = _REAL_SLICE_PROBE()
    assert limit == -1


def test_slice_tasks_absent_slice_is_unknown(monkeypatch) -> None:
    """No slice directory (not Linux, no delegation) reads as unknown, not zero."""
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_agents_slice_cgroup_dir", lambda: None)
    assert _REAL_SLICE_PROBE() == (-1, -1, -1)


def test_slice_tasks_degraded_token_leaves_own_unattributed(tmp_path, monkeypatch) -> None:
    """Without a per-instance child, this install's share is unknown.

    The aggregate still reads, because the ceiling is the shared slice's; what
    cannot be claimed is a share of it, so ``own`` stays -1 rather than taking
    credit for a co-resident gateway's tasks.
    """
    slice_dir = _slice_tree(tmp_path)
    _patch_slice(monkeypatch, slice_dir, token="kirocrew-agents.slice")
    current, limit, own = _REAL_SLICE_PROBE()
    assert (current, limit) == (29500, 32768)
    assert own == -1


def test_slice_tasks_released_child_is_a_true_zero(tmp_path, monkeypatch) -> None:
    """An install with no live scope holds no tasks; systemd drops the directory."""
    _patch_slice(monkeypatch, _slice_tree(tmp_path, own=None))
    assert _REAL_SLICE_PROBE() == (29500, 32768, 0)


def test_slice_tasks_unreadable_child_count_is_unknown(tmp_path, monkeypatch) -> None:
    """A child directory whose count will not parse is unknown, not zero."""
    _patch_slice(monkeypatch, _slice_tree(tmp_path, own="not-a-number\n"))
    assert _REAL_SLICE_PROBE() == (29500, 32768, -1)


def test_slice_tasks_probe_never_raises(monkeypatch) -> None:
    """Any failure inside the probe degrades to unknown; it must never raise."""
    from kiro_crew import sandbox

    def _boom():
        raise RuntimeError("cgroup gone")

    monkeypatch.setattr(sandbox, "_agents_slice_cgroup_dir", _boom)
    assert _REAL_SLICE_PROBE() == (-1, -1, -1)


@pytest.mark.parametrize(
    "current,limit,tight",
    [
        (29492, 32768, True),   # boundary: exactly the 90% ratio
        (29491, 32768, False),  # one task below it
        (32768, 32768, True),
        (1000, 32768, False),
        (29500, 0, False),      # no ceiling → nothing to approach
        (-1, 32768, False),     # unreadable count raises nothing
        (29500, -1, False),     # unreadable ceiling raises nothing
    ],
)
def test_slice_tasks_tight_band(current: int, limit: int, tight: bool) -> None:
    status = rs.ResourceStatus(
        available_gb=32.0,
        cpu_count=8,
        load_per_cpu=0.1,
        posture=rs.POSTURE_AMPLE,
        pressure_gb=4.0,
        critical_gb=2.0,
        slice_tasks=current,
        slice_tasks_limit=limit,
    )
    assert status.slice_tasks_tight is tight


def test_slice_tasks_text_shapes(monkeypatch) -> None:
    """The one reading both surfaces print, in its four shapes."""
    _tasks(monkeypatch, 29500, 32768, 22000)
    text = rs.probe(_cfg(4.0, 2.0)).slice_tasks_text()
    assert text == "29500 of 32768 tasks (90%), this instance 22000"
    _tasks(monkeypatch, 29500, 0, -1)
    assert rs.probe(_cfg(4.0, 2.0)).slice_tasks_text() == "29500 tasks, no ceiling set"
    # An unreadable ceiling reads differently from an absent one, so a released
    # slice is never printed as a slice without a limit.
    _tasks(monkeypatch, 29500, -1, -1)
    assert rs.probe(_cfg(4.0, 2.0)).slice_tasks_text() == "29500 tasks, ceiling unreadable"
    _tasks(monkeypatch, -1, -1, -1)
    assert rs.probe(_cfg(4.0, 2.0)).slice_tasks_text() == ""


def test_tasks_tight_raises_the_line_on_ample_memory(monkeypatch) -> None:
    """The case the memory figure cannot express: plenty of RAM, no pids left."""
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 32.0)
    _tasks(monkeypatch, 31000, 32768, 22000)
    status = rs.probe(_cfg(4.0, 2.0))
    line = status.context_line()
    assert status.posture == rs.POSTURE_AMPLE
    assert line.startswith("[RESOURCES]")
    assert "task" in line
    assert "31000 of 32768" in line
    # The line must not borrow the memory advisory's wording for a host with 32 GB
    # free: a reader acting on "memory is tight" would shed the wrong resource.
    assert "CRITICALLY" not in line
    assert "memory is tight" not in line
    assert "memory is fine" in line


def test_tasks_tight_never_claims_memory_it_could_not_read(monkeypatch) -> None:
    """An unreadable memory probe is not a clean bill of health for memory.

    ``unknown`` is not under pressure, so the task-only line is the branch that
    renders; it must report memory as unreadable rather than fine, because a
    reader told memory is fine stops considering it as the constraint.
    """
    monkeypatch.setattr(rs, "_read_available_gb", lambda: -1.0)
    _tasks(monkeypatch, 31000, 32768, 22000)
    status = rs.probe(_cfg(4.0, 2.0))
    line = status.context_line()
    assert status.posture == rs.POSTURE_UNKNOWN
    assert "31000 of 32768" in line
    assert "memory is fine" not in line
    assert "unreadable" in line


def test_under_pressure_stays_memory_only(monkeypatch) -> None:
    """A full slice with ample memory is not memory pressure.

    ``under_pressure`` is what several callers read as "memory is short", so a
    task count must never set it, however tight the slice is.
    """
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 32.0)
    _tasks(monkeypatch, 32768, 32768, 32768)
    status = rs.probe(_cfg(4.0, 2.0))
    assert status.slice_tasks_tight is True
    assert status.under_pressure is False


def test_tasks_tight_rides_the_memory_line_as_a_clause(monkeypatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 1.0)
    _tasks(monkeypatch, 31000, 32768, 22000)
    line = rs.probe(_cfg(4.0, 2.0)).context_line()
    assert "CRITICALLY" in line  # the memory advisory is intact
    assert "also near its task ceiling" in line


def test_tasks_clause_absent_when_below_the_band(monkeypatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 1.0)
    _tasks(monkeypatch, 1000, 32768, 500)
    line = rs.probe(_cfg(4.0, 2.0)).context_line()
    assert "CRITICALLY" in line
    assert "task" not in line


def test_off_switch_also_suppresses_the_task_line(monkeypatch) -> None:
    """``pressure_gb == 0`` is one off switch for the injected line, not two."""
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 32.0)
    _tasks(monkeypatch, 32768, 32768, 22000)
    status = rs.probe(_cfg(0.0, 0.0))
    assert status.context_line() == ""
    # The pull tool stays honest while the injected line is off.
    assert "TIGHT" in "\n".join(status.summary_lines())


def test_summary_lines_carry_the_task_reading(monkeypatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 32.0)
    _tasks(monkeypatch, 31000, 32768, 22000)
    joined = "\n".join(rs.probe(_cfg(4.0, 2.0)).summary_lines())
    assert "Agent slice tasks: 31000 of 32768 tasks (95%), this instance 22000" in joined
    assert "TIGHT" in joined  # the band marker on the task line


def test_summary_lines_omit_an_unmeasurable_slice(monkeypatch) -> None:
    """A host with no cgroup task ceiling gets no line at all, not 'unknown'."""
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 32.0)
    _tasks(monkeypatch, -1, -1, -1)
    joined = "\n".join(rs.probe(_cfg(4.0, 2.0)).summary_lines())
    assert "Agent slice tasks" not in joined


def test_task_count_never_gates_admission(monkeypatch) -> None:
    """Reported, never gated: a full slice with ample memory still admits."""
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 32.0)
    _tasks(monkeypatch, 32768, 32768, 32768)
    decision = rs.admission_check(_cfg(4.0, 2.0))
    assert decision.admitted is True
    assert decision.posture == rs.POSTURE_AMPLE
    assert decision.reason == ""


def test_task_count_never_shrinks_prewarm(monkeypatch) -> None:
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 32.0)
    _tasks(monkeypatch, 32768, 32768, 32768)
    assert rs.prewarm_allowance(cfg=_cfg(4.0, 2.0)) == rs.PREWARM_MAX_LIVE


def test_task_count_does_not_move_the_posture(monkeypatch) -> None:
    """The posture is a memory scalar at any task count."""
    monkeypatch.setattr(rs, "_read_available_gb", lambda: 32.0)
    for current in (0, 16000, 32768):
        _tasks(monkeypatch, current, 32768, current)
        assert rs.probe(_cfg(4.0, 2.0)).posture == rs.POSTURE_AMPLE
