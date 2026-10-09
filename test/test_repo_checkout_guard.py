"""A branch switch's reservation orders it against every turn and sub-agent run in the repository."""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import repo_checkout_guard as guard
from kiro_crew.dashboard.handlers import git_branches as gb
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.subagent import SubagentInfo, SubagentManager


class _SeenEvent(asyncio.Event):
    """A reservation event that says when something starts waiting on it."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def wait(self) -> bool:
        self.waiting.set()
        return await super().wait()


@pytest.fixture(autouse=True)
def _clean_registry():
    guard._reserved.clear()
    yield
    guard._reserved.clear()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    return root


def _reserve_seen(root: str) -> _SeenEvent:
    assert guard.reserve(root)
    event = _SeenEvent()
    guard._reserved[root] = event
    return event


def test_one_reservation_per_root(repo):
    root = os.path.realpath(repo)
    assert guard.reserve(root) is True
    assert guard.reserve(root) is False
    assert guard.is_reserved(root)
    guard.release(root)
    assert not guard.is_reserved(root)
    guard.release(root)  # releasing twice is harmless


@pytest.mark.asyncio
async def test_no_reservation_returns_without_touching_the_path():
    await guard.wait_for_checkout(None)
    await guard.wait_for_checkout("/no/such/project")
    await guard.wait_for_subagent_checkout(None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["root", "sub", "parent"])
async def test_a_project_in_or_around_the_repo_waits_for_the_release(repo, where):
    root = os.path.realpath(repo)
    project = {"root": repo, "sub": repo / "sub", "parent": repo.parent}[where]
    event = _reserve_seen(root)
    waiter = asyncio.create_task(guard.wait_for_checkout(str(project)))
    await asyncio.wait_for(event.waiting.wait(), 5)
    assert not waiter.done()
    guard.release(root)
    await asyncio.wait_for(waiter, 5)


@pytest.mark.asyncio
async def test_an_unrelated_project_does_not_wait(repo):
    guard.reserve(os.path.realpath(repo))
    await asyncio.wait_for(guard.wait_for_checkout(str(repo.parent / "other")), 5)


@pytest.mark.asyncio
async def test_a_long_checkout_is_waited_out_not_abandoned(repo, monkeypatch, caplog):
    """No deadline: a checkout that outlasts the log interval still holds the work back."""
    monkeypatch.setattr(guard, "WAIT_LOG_SECS", 0.01)
    root = os.path.realpath(repo)
    guard.reserve(root)
    waiter = asyncio.create_task(guard.wait_for_checkout(str(repo), waiter="session chat-9"))

    async def logged():
        while "session chat-9 still waiting" not in caplog.text:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(logged(), 5)
    assert not waiter.done()
    guard.release(root)
    await asyncio.wait_for(waiter, 5)


@pytest.mark.asyncio
async def test_a_reservation_dropped_mid_lookup_is_rechecked(repo, monkeypatch):
    root = os.path.realpath(repo)
    guard.reserve(root)
    real = guard._covering_root

    def drop_then_lookup(project, roots):
        found = real(project, roots)
        guard._reserved.pop(root, None)
        return found

    monkeypatch.setattr(guard, "_covering_root", drop_then_lookup)
    await asyncio.wait_for(guard.wait_for_checkout(str(repo)), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["result", "error", "cancelled"])
async def test_release_when_done_follows_the_worker_not_its_awaiter(repo, outcome):
    root = os.path.realpath(repo)
    guard.reserve(root)
    worker: asyncio.Future = asyncio.get_running_loop().create_future()
    guard.release_when_done(root, worker)
    assert guard.is_reserved(root)
    if outcome == "result":
        worker.set_result(({}, 200))
    elif outcome == "error":
        worker.set_exception(OSError("git died"))
    else:
        worker.cancel()
    await asyncio.sleep(0)
    assert not guard.is_reserved(root)


def test_covering_root_skips_roots_on_another_drive(monkeypatch, repo):
    def other_drive(paths):
        raise ValueError("different drives")

    monkeypatch.setattr(guard.os.path, "commonpath", other_drive)
    assert guard._covering_root(str(repo), [os.path.realpath(repo)]) is None


def _parent_state(project, key="chat-1"):
    slot = _ChatSlot(key)
    slot.project = str(project)
    return SimpleNamespace(_slots={key: slot}), slot


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["cwd", "parent_project"])
async def test_a_subagent_run_in_the_repo_waits(repo, tmp_path, via):
    from kiro_crew.dashboard.chat_utils import effective_session_key

    state, slot = _parent_state(repo if via == "parent_project" else tmp_path / "other")
    info = SimpleNamespace(
        parent_session_key=effective_session_key(slot),
        cwd=str(repo / "sub") if via == "cwd" else "",
    )
    root = os.path.realpath(repo)
    event = _reserve_seen(root)
    waiter = asyncio.create_task(guard.wait_for_subagent_checkout(state, info))
    await asyncio.wait_for(event.waiting.wait(), 5)
    assert not waiter.done()
    guard.release(root)
    await asyncio.wait_for(waiter, 5)


@pytest.mark.asyncio
async def test_a_subagent_of_another_session_does_not_wait(repo, tmp_path):
    state, _slot = _parent_state(repo)
    broken = MagicMock()
    broken.project = str(repo)
    state._slots["broken"] = broken  # cannot be keyed: skipped, not fatal
    guard.reserve(os.path.realpath(repo))
    info = SimpleNamespace(parent_session_key="dashboard:someone-else", cwd="")
    await asyncio.wait_for(guard.wait_for_subagent_checkout(state, info), 5)


@pytest.mark.asyncio
async def test_dashboard_state_installs_the_subagent_gate(tmp_path, repo):
    manager = SimpleNamespace()
    state = _make_state(tmp_path, subagents=manager)
    assert state.subagents is manager
    assert asyncio.iscoroutinefunction(manager.pre_run_gate)
    root = os.path.realpath(repo)
    event = _reserve_seen(root)
    info = SimpleNamespace(parent_session_key="", cwd=str(repo))
    run = asyncio.create_task(manager.pre_run_gate(info))
    await asyncio.wait_for(event.waiting.wait(), 5)
    assert not run.done()
    guard.release(root)
    await asyncio.wait_for(run, 5)


@pytest.mark.asyncio
async def test_a_subagent_run_executes_only_after_the_gate(tmp_path):
    order: list[str] = []
    release = asyncio.Event()
    gated = asyncio.Event()
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

    async def gate(info):
        order.append("gate")
        gated.set()
        await release.wait()

    async def run_inner(info, session_key):
        order.append("run")
        info.done = True

    manager.pre_run_gate = gate
    manager._run_inner = run_inner
    info = SubagentInfo(id="sa-1", task="edit a file")
    run = asyncio.create_task(manager._run(info))
    await asyncio.wait_for(gated.wait(), 5)
    assert order == ["gate"] and not run.done()
    release.set()
    await asyncio.wait_for(run, 5)
    assert order == ["gate", "run"]


@pytest.mark.asyncio
async def test_a_stop_during_the_gate_ends_the_run_as_stopped(tmp_path):
    gated = asyncio.Event()
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

    async def gate(info):
        gated.set()
        await asyncio.Event().wait()

    manager.pre_run_gate = gate
    manager._run_inner = AsyncMock()
    info = SubagentInfo(id="sa-3", task="edit a file")
    info.user_stopped = True
    run = asyncio.create_task(manager._run(info))
    await asyncio.wait_for(gated.wait(), 5)
    run.cancel()
    await asyncio.wait_for(asyncio.gather(run, return_exceptions=True), 5)
    manager._run_inner.assert_not_awaited()
    assert info.done


def _run(run_id, *, cwd="", parent=""):
    return SimpleNamespace(id=run_id, conversation_key="", cwd=cwd, parent_session_key=parent)


def test_subagent_folders_walk_the_lineage_to_the_root_chat(repo, tmp_path):
    from kiro_crew.dashboard.chat_utils import effective_session_key

    state, slot = _parent_state(repo)
    child = _run("c1", cwd=str(tmp_path / "other"), parent=effective_session_key(slot))
    grandchild = _run("g1", parent="subagent:c1")
    state.subagents = SimpleNamespace(all_agents=[child, grandchild])
    assert guard.subagent_folders(state, grandchild) == [str(tmp_path / "other"), str(repo)]
    looped = _run("loop", parent="subagent:loop")
    state.subagents = SimpleNamespace(all_agents=[looped])
    assert guard.subagent_folders(state, looped) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["own_cwd", "ancestor_cwd", "elsewhere"])
async def test_busy_check_sees_a_running_subagent_working_in_the_repo(repo, tmp_path, where):
    """A run started from a chat outside the repository, working inside it."""
    outside = tmp_path / "other"
    parent = _run("p1", cwd=str(repo) if where == "ancestor_cwd" else "", parent="dashboard:x")
    child = _run("c1", cwd=str(repo / "sub") if where == "own_cwd" else "", parent="subagent:p1")

    class _Subagents:
        running = [child]
        all_agents = [parent, child]

        async def has_pending_work_for_async(self, key):
            return False

    slot = _ChatSlot("chat-x")
    slot.project = str(outside)
    state = SimpleNamespace(_slots={slot.key: slot}, subagents=_Subagents())
    busy = await gb._repo_has_running_work(state, os.path.realpath(repo))
    assert busy is (where != "elsewhere")


@pytest.mark.asyncio
async def test_busy_check_fails_closed_on_an_unreadable_registry(repo):
    class _Broken:
        @property
        def running(self):
            raise RuntimeError("registry unavailable")

    state = SimpleNamespace(_slots={}, subagents=_Broken())
    assert await gb._repo_has_running_work(state, os.path.realpath(repo)) is True


@pytest.mark.asyncio
async def test_a_non_coroutine_gate_is_ignored(tmp_path):
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager.pre_run_gate = MagicMock()
    manager._run_inner = AsyncMock()
    info = SubagentInfo(id="sa-2", task="t")
    await asyncio.wait_for(manager._run(info), 5)
    manager.pre_run_gate.assert_not_called()
    manager._run_inner.assert_awaited_once()


class _Stop(Exception):
    pass


def _stop_at_setup(monkeypatch) -> list[str]:
    reached: list[str] = []

    async def setup(*args, **kwargs):
        reached.append("setup")
        raise _Stop

    monkeypatch.setattr(chat_runner, "_refresh_genuine_turn_allowances", setup)
    return reached


@pytest.mark.asyncio
async def test_a_turn_does_no_work_until_the_checkout_is_released(tmp_path, repo, monkeypatch):
    """The wait sits at ``_run_chat``'s entry, before the first await of its setup."""
    reached = _stop_at_setup(monkeypatch)
    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-1")
    slot.project = str(repo / "sub")
    state._slots[slot.key] = slot
    root = os.path.realpath(repo)
    event = _reserve_seen(root)

    turn = asyncio.create_task(chat_runner._run_chat(state, slot, "edit a file"))
    await asyncio.wait_for(event.waiting.wait(), 5)
    assert reached == [] and not turn.done()

    guard.release(root)
    with pytest.raises(_Stop):
        await asyncio.wait_for(turn, 5)
    assert reached == ["setup"]


@pytest.fixture
def switch_boundary(monkeypatch, repo):
    root = os.path.realpath(repo)
    monkeypatch.setattr(gb, "_resolve_repo_root", AsyncMock(return_value=root))
    monkeypatch.setattr(gb, "sel", MagicMock())
    monkeypatch.setattr(gb, "deny_non_dashboard_caller", lambda *args: None)
    monkeypatch.setattr(gb, "require_owner_dashboard_request", AsyncMock(return_value=None))
    body = {"path": root, "branch": "feature"}
    monkeypatch.setattr(gb, "read_bounded_json", AsyncMock(return_value=(body, None)))
    return root


def _switch_request(state):
    return SimpleNamespace(app={"state": state}, query={}, get=lambda key: "owner")


@pytest.mark.asyncio
async def test_a_turn_published_before_the_switch_refuses_it(
    tmp_path, repo, switch_boundary, monkeypatch
):
    """Ordering one: the turn is on its slot first, so the busy check sees it."""
    switch = MagicMock(return_value=({"ok": True, "branch": "feature"}, 200))
    monkeypatch.setattr(gb, "_switch_sync", switch)
    slot = _ChatSlot("chat-1")
    slot.project = str(repo)
    gate = asyncio.Event()
    slot.task = asyncio.create_task(gate.wait())
    state = SimpleNamespace(_slots={slot.key: slot}, subagents=None)
    try:
        response = await gb.api_project_git_switch(_switch_request(state))
    finally:
        gate.set()
        await slot.task
    assert response.status == 409
    switch.assert_not_called()
    assert not guard.is_reserved(switch_boundary)


@pytest.mark.asyncio
async def test_a_turn_admitted_during_the_busy_probe_waits_for_git(
    tmp_path, repo, switch_boundary, monkeypatch
):
    """Ordering two, the reported race: a turn starts while the sub-agent probe is
    suspended, after the probe had nothing to see; it must not run beside git."""
    from kiro_crew.dashboard import chat_utils

    reached = _stop_at_setup(monkeypatch)
    probing = asyncio.Event()
    probe_done = asyncio.Event()

    class _Subagents:
        async def has_pending_work_for_async(self, key):
            probing.set()
            await probe_done.wait()
            return False

    started = threading.Event()
    finish = threading.Event()

    def slow_switch(root, branch, create, track):
        started.set()
        assert finish.wait(5)
        return {"ok": True, "branch": branch}, 200

    monkeypatch.setattr(gb, "_switch_sync", slow_switch)
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    slot = _ChatSlot("chat-1")
    slot.project = str(repo / "sub")
    switch_state = SimpleNamespace(_slots={slot.key: slot}, subagents=_Subagents())
    turn_state = _make_state(tmp_path)
    turn_state._slots[slot.key] = slot

    request = asyncio.create_task(gb.api_project_git_switch(_switch_request(switch_state)))
    await asyncio.wait_for(probing.wait(), 5)
    event = _SeenEvent()
    guard._reserved[switch_boundary] = event  # same reservation, observable waiters
    turn = asyncio.create_task(chat_runner._run_chat(turn_state, slot, "edit a file"))
    await asyncio.wait_for(event.waiting.wait(), 5)
    probe_done.set()
    assert await asyncio.to_thread(started.wait, 5)
    assert reached == [] and not turn.done() and not event.is_set()

    finish.set()
    response = await asyncio.wait_for(request, 5)
    assert response.status == 200
    with pytest.raises(_Stop):
        await asyncio.wait_for(turn, 5)
    assert reached == ["setup"]


def _orphan():
    """A grandchild whose parent run's card was dismissed, so its record is gone."""
    return _run("g1", parent="subagent:dismissed")


def test_a_dismissed_ancestor_makes_the_lineage_unknown():
    state = SimpleNamespace(_slots={}, subagents=SimpleNamespace(all_agents=[]))
    with pytest.raises(guard.LineageUnknown):
        guard.subagent_folders(state, _orphan())


@pytest.mark.asyncio
async def test_busy_check_counts_a_run_with_an_unknown_lineage(repo):
    orphan = _orphan()
    state = SimpleNamespace(
        _slots={}, subagents=SimpleNamespace(running=[orphan], all_agents=[orphan])
    )
    assert await gb._repo_has_running_work(state, os.path.realpath(repo)) is True


@pytest.mark.asyncio
async def test_a_run_with_an_unknown_lineage_waits_out_every_checkout(repo, tmp_path):
    state = SimpleNamespace(_slots={}, subagents=SimpleNamespace(all_agents=[]))
    elsewhere = os.path.realpath(tmp_path / "other")
    event = _reserve_seen(elsewhere)
    waiter = asyncio.create_task(guard.wait_for_subagent_checkout(state, _orphan()))
    await asyncio.wait_for(event.waiting.wait(), 5)
    assert not waiter.done()
    guard.release(elsewhere)
    await asyncio.wait_for(waiter, 5)
