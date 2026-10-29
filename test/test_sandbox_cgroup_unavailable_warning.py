"""The cgroup-unavailable SECURITY warning names what is actually enforced here.

On Windows the per-spawn Job object still caps each agent tree's processes and
memory, and ``RLIMIT_NOFILE`` is never installed (there is no ``resource`` module).
A warning that said "no ceilings" and "RLIMIT_NOFILE still applies" was wrong on
both counts there. On a POSIX host without cgroup v2 the rlimit IS live, so its
clause stays.
"""

from __future__ import annotations

import logging
import sys

import pytest

from kiro_crew import platform_compat, sandbox_cgroup


def _warning(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(sandbox_cgroup, "_CGROUP_WARNED", False)
    with caplog.at_level(logging.WARNING, logger=sandbox_cgroup.logger.name):
        sandbox_cgroup._warn_cgroup_unavailable("not Linux")
    lines = [r.getMessage() for r in caplog.records if "cgroup v2" in r.getMessage()]
    assert len(lines) == 1, lines
    return lines[0]


def test_on_windows_the_warning_names_the_job_ceiling_and_no_rlimit(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    # No ``resource`` module, as on Windows: importing it raises ImportError.
    monkeypatch.setitem(sys.modules, "resource", None)
    line = _warning(caplog, monkeypatch)
    assert "RLIMIT_NOFILE" not in line
    # The Job object covers the ACP runtime only, so the line still says the
    # ceilings are off and names what is left without one.
    assert "fork-bomb / memory-DoS ceilings are NOT enforced" in line
    assert "Job object: it caps the ACP agent runtime's" in line
    assert "in a pod the gateway's Job also covers what it launches" in line
    assert "other subprocesses get no ceiling of their own" in line
    assert "each agent tree" not in line


def test_on_a_posix_host_the_rlimit_clause_stays(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("resource")
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    line = _warning(caplog, monkeypatch)
    assert "agent subprocess fork-bomb / memory-DoS ceilings are NOT enforced" in line
    assert "RLIMIT_NOFILE still applies." in line
    assert "Job object" not in line


def test_the_warning_is_still_emitted_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _warning(caplog, monkeypatch)
    caplog.clear()
    sandbox_cgroup._warn_cgroup_unavailable("not Linux")
    assert not [r for r in caplog.records if "cgroup v2" in r.getMessage()]
