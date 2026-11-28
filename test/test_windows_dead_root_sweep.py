"""Windows session-root sweeps keep an exited root's entry while its tree may outlive it."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew import session_pid

_GW = 400
_ROOT = 500
_ROOT_TOKEN = "1000"
_ENTRY = f"{_GW}:{_ROOT}:{_ROOT_TOKEN}"


@pytest.fixture
def win(monkeypatch, tmp_path):
    """A Windows host whose gateway and recorded root have both exited."""
    state = SimpleNamespace(parent_map={}, start_times={}, snapshot_calls=0, kills=[])

    def parent_map():
        state.snapshot_calls += 1
        if isinstance(state.parent_map, Exception):
            raise state.parent_map
        return dict(state.parent_map)

    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(pc, "pid_liveness", lambda _pid: pc.PID_DEAD)
    monkeypatch.setattr(pc, "pid_exists", lambda _pid: False)
    monkeypatch.setattr(pc, "windows_tree_cleanup_pending", lambda *_a: False)
    monkeypatch.setattr(pc, "retry_pending_windows_process_trees", lambda: ())
    monkeypatch.setattr(pc, "_windows_process_parent_map", parent_map)
    monkeypatch.setattr(pc, "process_start_time", state.start_times.get)
    monkeypatch.setattr(pc, "kill_process_tree", lambda *a: state.kills.append(a))
    monkeypatch.setattr(pc, "kill_pid", lambda *a: state.kills.append(a))
    monkeypatch.setattr(session_pid, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(session_pid, "_cleanup_orphaned_mcp_servers", lambda: 0)
    monkeypatch.setattr(session_pid, "_prune_stale_session_pid_files", lambda **_k: None)
    monkeypatch.setattr(session_pid, "_prune_stale_session_token_files", lambda: None)
    state.pid_file = tmp_path / "kiro_session_pids.txt"
    state.pid_file.write_text(_ENTRY + "\n", encoding="utf-8")
    return state


def _entries(state) -> list[str]:
    return state.pid_file.read_text(encoding="utf-8").split()


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.parametrize("child_start", ["2000", _ROOT_TOKEN])
@pytest.mark.parametrize("sweep", ["startup", "roots"])
def test_live_child_of_an_exited_root_keeps_the_entry_and_warns_once(
    win, caplog, sweep, child_start
):
    win.parent_map = {_ROOT + 1: _ROOT, _ROOT + 2: _ROOT + 1, 7: 4}
    win.start_times[_ROOT + 1] = child_start
    run = (
        (lambda: session_pid.cleanup_orphaned_sessions(narrow_with_leaders=False))
        if sweep == "startup"
        else session_pid.cleanup_orphaned_session_roots
    )

    with caplog.at_level(logging.WARNING, logger="kiro_crew.session_pid"):
        run()
        run()

    assert _entries(win) == [_ENTRY]
    assert win.kills == [], "a dead root's suspected tree must not be signalled"
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert f"PID {_ROOT} has exited" in warnings[0] and f"PIDs {_ROOT + 1}" in warnings[0]


@pytest.mark.parametrize("sweep", ["startup", "roots"])
def test_unreadable_process_table_keeps_the_entry(win, caplog, sweep):
    win.parent_map = OSError("snapshot refused")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.session_pid"):
        if sweep == "startup":
            session_pid.cleanup_orphaned_sessions(narrow_with_leaders=False)
        else:
            session_pid.cleanup_orphaned_session_roots()

    assert _entries(win) == [_ENTRY]
    assert win.kills == []
    assert any("could not be read" in w for w in _warnings(caplog))


@pytest.mark.parametrize(
    "parent_map",
    [
        # 600 predates the root: it was born under a previous holder of the root's number.
        {600: _ROOT, 7: 4},
        # A surviving grandchild whose intermediate parent (501) has exited.
        {_ROOT + 2: _ROOT + 1, 7: 4},
    ],
    ids=["older-process", "orphaned-grandchild"],
)
@pytest.mark.parametrize("sweep", ["startup", "roots"])
def test_exited_root_with_no_possible_child_is_pruned_with_a_warning(
    win, caplog, sweep, parent_map
):
    win.parent_map = parent_map
    win.start_times[600] = "999"

    with caplog.at_level(logging.WARNING, logger="kiro_crew.session_pid"):
        if sweep == "startup":
            session_pid.cleanup_orphaned_sessions(narrow_with_leaders=False)
        else:
            session_pid.cleanup_orphaned_session_roots()

    assert _entries(win) == []
    assert win.kills == []
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert f"PID {_ROOT} has exited and no possible direct child" in warnings[0]


def test_entry_is_pruned_once_the_suspected_child_exits(win):
    win.parent_map = {_ROOT + 1: _ROOT}
    win.start_times[_ROOT + 1] = "2000"
    session_pid.cleanup_orphaned_session_roots()
    assert _entries(win) == [_ENTRY]

    win.parent_map = {}
    session_pid.cleanup_orphaned_session_roots()
    assert _entries(win) == []
    assert session_pid._DEAD_ROOT_RETAIN_WARNED == set()


@pytest.mark.parametrize(
    ("token", "child_start"),
    [(None, "999"), ("1000", None), ("not-a-filetime", "2000"), ("1000", "1000")],
)
def test_unknown_creation_order_counts_as_a_possible_child(win, token, child_start):
    win.parent_map = {_ROOT + 1: _ROOT}
    if child_start is not None:
        win.start_times[_ROOT + 1] = child_start

    assert session_pid._windows_dead_root_retained("e", _ROOT, token) is True


def test_posix_prunes_an_exited_root_without_reading_the_process_table(win, monkeypatch):
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    win.parent_map = {_ROOT + 1: _ROOT}

    session_pid.cleanup_orphaned_session_roots()

    assert _entries(win) == []
    assert win.snapshot_calls == 0
