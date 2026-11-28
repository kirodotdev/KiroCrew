"""The orphan sweeps say what they drop, and the Windows orphan scan says it did not look.

A row whose tracked root already exited is pruned with one INFO line naming the root pid,
its owning gateway pid and how many live processes still list that root as an ancestor.
Malformed rows get one DEBUG summary per sweep, not a line each. On Windows the first
``_our_orphan_pids`` call in a process says the scan does not run there. No line carries
a path or a command line, and the rows each sweep keeps and removes are unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat, session_pid

_LOGGER = "kiro_crew.session_pid"
_DEAD_GW = 999_999
_DEAD_ROOT = 4242


@pytest.fixture()
def session_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "kiro_session_pids.txt"
    monkeypatch.setattr("kiro_crew.session_pid._session_pid_file_path", lambda: path)
    return path


@pytest.fixture()
def parent_map(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Serve one fixed process table on every host and count the reads."""
    reads: list[int] = []
    table = {5001: _DEAD_ROOT, 5002: 5001, 6000: 1}

    def snapshot() -> dict[int, int]:
        reads.append(1)
        return dict(table)

    monkeypatch.setattr(platform_compat, "_posix_process_parent_map", snapshot)
    monkeypatch.setattr(platform_compat, "_windows_process_parent_map", snapshot)
    return reads


def _dead(*pids: int):
    def liveness(pid: int) -> str:
        return platform_compat.PID_DEAD if pid in pids else platform_compat.PID_ALIVE

    return liveness


def _records(caplog: pytest.LogCaptureFixture, level: int, needle: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == level and needle in r.getMessage()
    ]


def _sweep(lines: list[str]) -> set[str]:
    _, dropped, _ = session_pid._sweep_pid_entries(
        lines,
        should_skip_tagged=lambda _gw, _pid: False,
        should_skip_bare=lambda _pid: False,
    )
    return dropped


def test_a_dead_root_row_logs_one_info_line_with_owner_and_descendants(
    caplog: pytest.LogCaptureFixture, parent_map: list[int], tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    row = f"{_DEAD_GW}:{_DEAD_ROOT}:TOKEN"
    with patch.object(platform_compat, "pid_liveness", side_effect=_dead(_DEAD_ROOT)):
        dropped = _sweep([row])

    assert dropped == {row}
    lines = _records(caplog, logging.INFO, "already exited")
    assert lines == [
        f"Orphan sweep: tracked root pid={_DEAD_ROOT} already exited; row pruned "
        f"(owner gateway pid={_DEAD_GW}, live_descendants=2)"
    ]
    assert "TOKEN" not in lines[0] and str(tmp_path) not in lines[0]


def test_a_bare_dead_row_names_no_owner(
    caplog: pytest.LogCaptureFixture, parent_map: list[int]
) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    with patch.object(platform_compat, "pid_liveness", side_effect=_dead(_DEAD_ROOT)):
        dropped = _sweep([str(_DEAD_ROOT)])

    assert dropped == {str(_DEAD_ROOT)}
    assert _records(caplog, logging.INFO, "already exited") == [
        f"Orphan sweep: tracked root pid={_DEAD_ROOT} already exited; row pruned "
        "(owner gateway pid=none, live_descendants=2)"
    ]


def test_the_process_table_is_read_once_per_sweep_and_only_for_a_dead_root(
    caplog: pytest.LogCaptureFixture, parent_map: list[int]
) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    rows = [f"{_DEAD_GW}:{_DEAD_ROOT}", f"{_DEAD_GW}:{_DEAD_ROOT + 1}"]
    with patch.object(
        platform_compat, "pid_liveness", side_effect=_dead(_DEAD_ROOT, _DEAD_ROOT + 1)
    ):
        assert _sweep(rows) == set(rows)
    assert len(_records(caplog, logging.INFO, "already exited")) == 2
    assert len(parent_map) == 1

    parent_map.clear()
    with patch.object(platform_compat, "pid_liveness", side_effect=_dead()):
        _sweep(["not-a-pid"])
    assert parent_map == []


@pytest.mark.parametrize("failure", ["raises", "empty"])
def test_an_unreadable_process_table_reads_unknown_and_still_prunes(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def snapshot() -> dict[int, int]:
        if failure == "raises":
            raise OSError("snapshot failed")
        return {}

    monkeypatch.setattr(platform_compat, "_posix_process_parent_map", snapshot)
    monkeypatch.setattr(platform_compat, "_windows_process_parent_map", snapshot)
    caplog.set_level(logging.INFO, logger=_LOGGER)
    row = f"{_DEAD_GW}:{_DEAD_ROOT}"
    with patch.object(platform_compat, "pid_liveness", side_effect=_dead(_DEAD_ROOT)):
        assert _sweep([row]) == {row}
    assert _records(caplog, logging.INFO, "already exited") == [
        f"Orphan sweep: tracked root pid={_DEAD_ROOT} already exited; row pruned "
        f"(owner gateway pid={_DEAD_GW}, live_descendants=unknown)"
    ]


def test_a_failing_log_call_does_not_change_the_prune(parent_map: list[int]) -> None:
    row = f"{_DEAD_GW}:{_DEAD_ROOT}"
    with (
        patch.object(platform_compat, "pid_liveness", side_effect=_dead(_DEAD_ROOT)),
        patch.object(session_pid.logger, "info", side_effect=RuntimeError("handler")),
    ):
        assert _sweep([row]) == {row}


def test_malformed_rows_log_one_debug_summary_and_no_line_each(
    caplog: pytest.LogCaptureFixture, parent_map: list[int]
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    malformed = ["1:2:3:4", "x:5", "0:5", "abc", "-3"]
    with patch.object(platform_compat, "pid_liveness", side_effect=_dead()):
        dropped = _sweep(malformed)

    assert dropped == set(malformed)
    assert _records(caplog, logging.DEBUG, "malformed") == [
        "Orphan sweep: 5 malformed row(s) dropped"
    ]
    assert _records(caplog, logging.INFO, "already exited") == []
    assert parent_map == []


def test_session_root_sweep_logs_a_dead_root_and_a_malformed_summary(
    caplog: pytest.LogCaptureFixture, parent_map: list[int], session_pid_file: Path
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    dead_row = f"{_DEAD_GW}:{_DEAD_ROOT}:TOKEN"
    malformed = ["1:2:3:4", "0:7"]
    session_pid_file.write_text("\n".join([dead_row, *malformed]) + "\n", encoding="utf-8")

    with (
        patch.object(platform_compat, "pid_liveness", side_effect=_dead(_DEAD_GW, _DEAD_ROOT)),
        patch.object(platform_compat, "retry_pending_windows_process_trees", return_value=[]),
        patch.object(platform_compat, "windows_tree_cleanup_pending", return_value=False),
    ):
        assert session_pid.cleanup_orphaned_session_roots() == 0

    assert session_pid_file.read_text(encoding="utf-8").split() == []
    assert _records(caplog, logging.INFO, "already exited") == [
        f"Orphan sweep: tracked root pid={_DEAD_ROOT} already exited; row pruned "
        f"(owner gateway pid={_DEAD_GW}, live_descendants=2)"
    ]
    assert _records(caplog, logging.DEBUG, "malformed") == [
        "Orphan sweep: 2 malformed row(s) dropped"
    ]


def test_the_windows_orphan_scan_says_once_per_process_that_it_was_skipped(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_pid, "_orphan_scan_skip_logged", False)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    caplog.set_level(logging.INFO, logger=_LOGGER)

    assert session_pid._our_orphan_pids() == []
    assert session_pid._our_orphan_pids() == []

    assert _records(caplog, logging.INFO, "orphan scan skipped") == [
        "orphan scan skipped: not supported on this platform"
    ]
