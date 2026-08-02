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
