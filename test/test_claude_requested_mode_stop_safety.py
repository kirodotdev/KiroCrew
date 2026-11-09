"""Identity and callable-surface guards for requested-``auto`` policy stops."""

from __future__ import annotations

import asyncio
import threading
import weakref
from unittest import mock

import pytest
from test_claude_requested_mode import _client, _harness, _mode_report, _modes

from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.testing.ids import unallocatable_pids
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.mark.parametrize(
    ("observed_start", "stops_tree"),
    [("ours", True), ("theirs", False), (None, False)],
    ids=["matching", "recycled", "unknown"],
)
def test_closed_loop_posix_tree_stop_requires_the_captured_identity(
    tmp_path, monkeypatch, observed_start, stops_tree
):
    """The root is scanned and its verified group signalled only while its start id matches."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    root, stored, fresh = unallocatable_pids(3)
    client = _client(tmp_path)
    process = mock.MagicMock(returncode=None)
    process.pid = root
    client._process = process
    client._start_time = "ours"
    client._child_pids = {stored: mock.sentinel.stored_child}
    scanned: list[int] = []
    groups: list[tuple[int, str | None]] = []
    signalled: list[tuple[int, int]] = []
    swept: list[dict[int, object]] = []

    monkeypatch.setattr(mode_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(
        mode_mod.platform_compat, "get_process_start_id", lambda _pid: observed_start
    )
    monkeypatch.setattr(
        mode_mod.runtime_process_tree,
        "_get_child_pids",
        lambda pid: scanned.append(pid) or [stored, fresh],
    )
    monkeypatch.setattr(
        mode_mod.runtime_process_tree,
        "_capture_child_records",
        lambda pids: {pid: mock.sentinel.fresh_child for pid in pids},
    )

    def _isolated_group(pid: int, start: str | None) -> int:
        groups.append((pid, start))
        return pid

    monkeypatch.setattr(mode_mod.process_identity, "isolated_group_of", _isolated_group)
    monkeypatch.setattr(
        mode_mod.platform_compat,
        "kill_process_group",
        lambda group, sig: signalled.append((group, sig)) or True,
    )
    monkeypatch.setattr(
        mode_mod.platform_compat,
        "kill_process_tree",
        mock.Mock(side_effect=AssertionError("numeric-pid tree kill")),
    )
    monkeypatch.setattr(
        mode_mod.runtime_process_tree,
        "_kill_escaped_children",
        lambda children: swept.append(dict(children)),
    )

    mode_mod._stop_claude_without_its_loop(client, process)

    assert scanned == ([root] if stops_tree else [])
    assert groups == ([(root, "ours")] if stops_tree else [])
    assert signalled == ([(root, mode_mod.platform_compat.SIGKILL)] if stops_tree else [])
    assert set(swept[0]) == ({stored, fresh} if stops_tree else {stored})
    process.kill.assert_not_called()


def test_closed_loop_windows_stop_without_a_start_id_kills_only_the_process_object(
    tmp_path, monkeypatch
):
    """An unpinned Windows fallback never resolves the captured pid as a tree."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    root = unallocatable_pids(1)[0]
    client = _client(tmp_path)
    process = mock.MagicMock(returncode=None)
    process.pid = root
    client._process = process
    client._start_time = None

    monkeypatch.setattr(mode_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        mode_mod.runtime_process_tree,
        "_get_child_pids",
        mock.Mock(side_effect=AssertionError("unpinned child scan")),
    )
    monkeypatch.setattr(
        mode_mod.platform_compat,
        "kill_process_tree_pinned",
        mock.Mock(side_effect=AssertionError("tree kill without a pin")),
    )
    monkeypatch.setattr(
        mode_mod.platform_compat,
        "kill_process_tree",
        mock.Mock(side_effect=AssertionError("numeric-pid tree kill")),
    )

    mode_mod._stop_claude_without_its_loop(client, process)

    process.kill.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancelled"])
async def test_a_failed_policy_stop_runs_its_tree_kill_fallback_off_the_event_loop(
    tmp_path, monkeypatch, failure
):
    """A client stop that raises or is cancelled hands the tree kill to an executor thread."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    child = unallocatable_pids(1)[0]
    client = _client(tmp_path)
    process = mock.MagicMock(returncode=None)
    client._process = process
    client._start_time = "ours"
    client._child_pids = {child: mock.sentinel.child}
    entered = asyncio.Event()
    fallbacks: list[tuple[threading.Thread, dict[str, object]]] = []

    async def _stop() -> None:
        entered.set()
        if failure == "error":
            raise RuntimeError("tree stop failed")
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_claude_stop_session", _stop)
    monkeypatch.setattr(
        mode_mod,
        "_stop_claude_without_its_loop",
        lambda _client, _process, **kw: fallbacks.append((threading.current_thread(), kw)),
    )

    task = asyncio.create_task(mode_mod._stop_claude_auto_for_policy(client, process))
    await asyncio.wait_for(entered.wait(), FALLBACK_TIMEOUT_SECS)
    if failure == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)
    else:
        await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)

    assert [(thread is threading.current_thread(), kw) for thread, kw in fallbacks] == [
        (False, {"start_time": "ours", "child_pids": {child: mock.sentinel.child}})
    ]
    assert fallbacks[0][0].name.startswith("mc-subproc")


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["zero-tools", "side-read-only"])
async def test_a_requested_auto_session_without_callable_tools_is_not_watched_or_stopped(
    tmp_path, monkeypatch, surface
):
    """A trust deny or approving report cannot stop a session with no callable tool."""
    from kiro_crew.acp import client_claude_mode as mode_mod

    registry = weakref.WeakKeyDictionary()
    monkeypatch.setattr(mode_mod, "_live_requested_auto_clients", registry)
    client = _client(tmp_path, side_read_only=surface == "side-read-only")
    client._spec_zero_tools = surface == "zero-tools"
    client._session_id = "s-1"
    process = mock.MagicMock(returncode=None)
    client._process = process
    _sent, killed = _harness(client, monkeypatch)

    client._track_claude_starting_mode({"modes": _modes("default")})
    client._claude_mode_settling = False
    stop = client._note_claude_mode_report(
        JsonRpcMessage(
            method="session/update",
            params={"sessionId": "s-1", "update": _mode_report("acceptEdits")},
        )
    )
    mode_mod._on_claude_trust_denied(frozenset({"trust"}))
    await asyncio.sleep(0)

    assert client._claude_mode_watched is False
    assert client not in registry
    assert stop is None
    assert killed == []
    process.kill.assert_not_called()
