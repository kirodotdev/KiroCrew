"""``kirocrew doctor`` prints the host's commit charge against its commit limit on Windows.

A Windows host can reach its commit limit with physical memory still free, and new
threads and processes then fail to start. The Memory Pressure section's freeze check
reads Linux procfs and cannot see it, so on Windows the section adds one advisory
``commit:`` line; other platforms print nothing new.
"""

from __future__ import annotations

import pytest

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import resources

_MIB = 1024 * 1024


def _arrange(monkeypatch: pytest.MonkeyPatch, *, windows: bool, commit: object) -> None:
    monkeypatch.setattr(cli_doctor.sys, "platform", "win32" if windows else "darwin")
    monkeypatch.setattr(cli_doctor.platform_compat, "IS_WINDOWS", windows)
    monkeypatch.setattr(cli_doctor.platform_compat, "system_commit", lambda: commit)
    monkeypatch.setattr(resources, "_gateway_memory_lines", lambda: [])
    monkeypatch.setattr(resources, "_windows_inert_limit_lines", lambda: [])


def test_on_windows_the_section_prints_the_commit_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _arrange(monkeypatch, windows=True, commit=(73_098 * _MIB, 14_467 * _MIB))
    issues: list[str] = []

    cli_doctor._doctor_memory_pressure(issues)

    out = capsys.readouterr().out
    assert "  commit:          58,631 of 73,098 MiB committed (80% of the commit limit)\n" in out
    assert out.index("commit:") < out.index("not applicable")
    assert issues == [], "advisory: the line must not add an issue"


def test_an_unreadable_reading_degrades_to_a_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _arrange(monkeypatch, windows=True, commit=None)
    issues: list[str] = []

    cli_doctor._doctor_memory_pressure(issues)

    assert "  commit:          ⚠️  could not read the commit charge" in capsys.readouterr().out
    assert issues == []


def test_off_windows_the_line_is_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _arrange(monkeypatch, windows=False, commit=(73_098 * _MIB, 14_467 * _MIB))

    cli_doctor._doctor_memory_pressure([])

    assert "commit:" not in capsys.readouterr().out
