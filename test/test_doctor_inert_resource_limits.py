"""``kirocrew doctor`` names the ``resource_limits`` keys Windows accepts but ignores.

On Windows the Job object bounds only per-spawn ``max_processes`` and
``max_memory_mb``. Every other key in the startup warning's list needs a systemd
user manager or a POSIX rlimit, and the default-on ``max_open_files`` (unset means
1024) is never installed there either. The Memory Pressure section says so in one
advisory row; other platforms print nothing new.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import cli_doctor
from kiro_crew.config.sections_limits import ResourceLimitsConfig
from kiro_crew.doctor_checks import resources


def _arrange(
    monkeypatch: pytest.MonkeyPatch, *, windows: bool, limits: ResourceLimitsConfig
) -> None:
    monkeypatch.setattr(cli_doctor.platform_compat, "IS_WINDOWS", windows)
    config = SimpleNamespace(resource_limits=limits)
    monkeypatch.setattr(cli_doctor, "KiroCrewConfig", SimpleNamespace(load=lambda: config))


def test_on_windows_each_set_inert_key_and_the_default_fd_cap_are_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arrange(
        monkeypatch,
        windows=True,
        limits=ResourceLimitsConfig(
            cpu_weight=200,
            max_cpu_percent=50,
            max_cpu_seconds=600,
            max_total_memory_mb=4096,
            max_total_processes=512,
            max_processes=64,
            max_memory_mb=2048,
        ),
    )
    lines = resources._windows_inert_limit_lines()
    text = "\n".join(lines)
    assert "⚠" in lines[0]
    for key in (
        "cpu_weight",
        "max_cpu_percent",
        "max_cpu_seconds",
        "max_total_memory_mb",
        "max_total_processes",
    ):
        assert f"resource_limits.{key}" in text, key
    assert "resource_limits.max_open_files (default 1024)" in text
    # The two keys the Job object does enforce are never called inert.
    assert "resource_limits.max_processes" not in text
    assert "resource_limits.max_memory_mb" not in text


def test_an_fd_cap_turned_off_is_not_named(monkeypatch: pytest.MonkeyPatch) -> None:
    # 0 leaves the inherited limit alone, so there is nothing Windows fails to do.
    _arrange(monkeypatch, windows=True, limits=ResourceLimitsConfig(max_open_files=0))
    assert resources._windows_inert_limit_lines() == [
        "  inert limits:    ✅ no resource_limits key set that Windows ignores"
    ]


def test_off_windows_the_row_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _arrange(monkeypatch, windows=False, limits=ResourceLimitsConfig(cpu_weight=200))
    assert resources._windows_inert_limit_lines() == []


def test_an_unreadable_config_degrades_to_a_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_doctor.platform_compat, "IS_WINDOWS", True)

    def _boom() -> object:
        raise ValueError("bad config")

    monkeypatch.setattr(cli_doctor, "KiroCrewConfig", SimpleNamespace(load=_boom))
    assert resources._windows_inert_limit_lines() == [
        "  inert limits:    ⚠  could not read resource_limits"
    ]


def test_the_memory_pressure_section_prints_the_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _arrange(monkeypatch, windows=True, limits=ResourceLimitsConfig(max_cpu_percent=50))
    monkeypatch.setattr(resources, "_gateway_memory_lines", lambda: [])
    monkeypatch.setattr(cli_doctor.sys, "platform", "win32")
    issues: list[str] = []
    cli_doctor._doctor_memory_pressure(issues)
    out = capsys.readouterr().out
    assert "inert limits:" in out and "resource_limits.max_cpu_percent" in out
    assert issues == [], "advisory: the row must not add an issue"
