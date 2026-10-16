"""A branch switch's reservation orders it against every turn and sub-agent run in the repository."""

from __future__ import annotations

import asyncio
import os
import sys
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
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout


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
    guard._tracked.clear()
    yield
    guard._reserved.clear()
    guard._tracked.clear()


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
    await await_until(event.waiting)
    assert not waiter.done()
    guard.release(root)
    await await_until(waiter)


@pytest.mark.asyncio
async def test_an_unrelated_project_does_not_wait(repo):
    guard.reserve(os.path.realpath(repo))
    await await_until(asyncio.ensure_future(guard.wait_for_checkout(str(repo.parent / "other"))))


@pytest.mark.asyncio
async def test_a_long_checkout_is_waited_out_not_abandoned(repo, monkeypatch, caplog):
    """No deadline: a checkout that outlasts the log interval still holds the work back."""
    monkeypatch.setattr(guard, "WAIT_LOG_SECS", 0.01)
    root = os.path.realpath(repo)
    guard.reserve(root)
    waiter = asyncio.create_task(guard.wait_for_checkout(str(repo), waiter="session chat-9"))

    await async_wait_until(
        lambda: "session chat-9 still waiting" in caplog.text,
        describe=lambda: caplog.text,
    )
    assert not waiter.done()
    guard.release(root)
    await await_until(waiter)


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
    await await_until(asyncio.ensure_future(guard.wait_for_checkout(str(repo))))


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
        cwd=str(repo / "sub") if via == "cwd" else str(tmp_path / "own"),
    )
    root = os.path.realpath(repo)
    event = _reserve_seen(root)
    waiter = asyncio.create_task(guard.wait_for_subagent_checkout(state, info))
    await await_until(event.waiting)
    assert not waiter.done()
    guard.release(root)
    await await_until(waiter)


@pytest.mark.asyncio
async def test_a_subagent_of_another_session_does_not_wait(repo, tmp_path):
    state, _slot = _parent_state(repo)
    broken = MagicMock()
    broken.project = str(repo)
    state._slots["broken"] = broken  # cannot be keyed: skipped, not fatal
    guard.reserve(os.path.realpath(repo))
    info = SimpleNamespace(parent_session_key="dashboard:someone-else", cwd=str(tmp_path / "own"))
    await await_until(asyncio.ensure_future(guard.wait_for_subagent_checkout(state, info)))


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
    await await_until(event.waiting)
    assert not run.done()
    guard.release(root)
    await await_until(run)


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
    await await_until(gated)
    assert order == ["gate"] and not run.done()
    release.set()
    await await_until(run)
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
    await await_until(gated)
    run.cancel()
    await await_until(asyncio.gather(run, return_exceptions=True))
    manager._run_inner.assert_not_awaited()
    assert info.done


def _run(run_id, *, cwd="", parent="", **extra):
    return SimpleNamespace(
        id=run_id, conversation_key="", cwd=cwd, parent_session_key=parent, **extra
    )


def test_subagent_folders_walk_the_lineage_to_the_root_chat(repo, tmp_path):
    from kiro_crew.dashboard.chat_utils import effective_session_key

    state, slot = _parent_state(repo)
    started = str(tmp_path / "started")
    child = _run("c1", cwd=str(tmp_path / "other"), parent=effective_session_key(slot))
    # No override: the folder its session started in is its own.
    grandchild = _run("g1", parent="subagent:c1", _session_cwd=started)
    state.subagents = SimpleNamespace(all_agents=[child, grandchild])
    assert guard.subagent_folders(state, grandchild) == [
        started,
        str(tmp_path / "other"),
        str(repo),
    ]
    looped = _run("loop", parent="subagent:loop", _session_cwd=started)
    state.subagents = SimpleNamespace(all_agents=[looped])
    assert guard.subagent_folders(state, looped) == [started]


@pytest.mark.parametrize("selectors", [{}, {"crew": "reviewer"}, {"agent": "builder"}])
def test_a_run_not_placed_yet_has_an_unknown_lineage(tmp_path, selectors):
    """No override and no session yet: where it starts is the allocator's choice.

    A member's workspace, a resumed conversation's stored folder, the pool's or a
    per-session work folder; a parent chat's project does not decide it either.
    """
    state, slot = _parent_state(tmp_path / "outside")
    from kiro_crew.dashboard.chat_utils import effective_session_key

    run = _run("r1", parent=effective_session_key(slot), **selectors)
    state.subagents = SimpleNamespace(all_agents=[run])
    with pytest.raises(guard.LineageUnknown):
        guard.subagent_folders(state, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
async def test_busy_check_sees_a_parentless_member_run_in_the_repo(repo, started):
    """The reported case: no parent, no cwd, placed in the member's workspace."""
    run = _run("m1", crew="reviewer", _session_cwd=str(repo) if started else "")

    class _Subagents:
        running = [run]
        all_agents = [run]

        async def has_pending_work_for_async(self, key):
            return False

    state = SimpleNamespace(_slots={}, subagents=_Subagents())
    assert await gb._repo_has_running_work(state, os.path.realpath(repo)) is True


@pytest.mark.asyncio
async def test_a_run_not_placed_yet_waits_out_any_checkout(repo):
    state = SimpleNamespace(_slots={}, subagents=SimpleNamespace(all_agents=[]))
    root = os.path.realpath(repo)
    event = _reserve_seen(root)
    waiter = asyncio.create_task(guard.wait_for_subagent_checkout(state, _run("m1", crew="x")))
    await await_until(event.waiting)
    assert not waiter.done()
    guard.release(root)
    await await_until(waiter)


@pytest.mark.asyncio
async def test_a_continuation_reaches_the_gate_with_no_stale_session_folder(tmp_path):
    """The previous execution's folder must not stand in for this one's placement."""
    seen: list[str] = []
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

    async def gate(info):
        seen.append(getattr(info, "_session_cwd", None))

    manager.pre_run_gate = gate
    manager._run_inner = AsyncMock()
    info = SubagentInfo(id="sa-4", task="follow up")
    setattr(info, "_session_cwd", str(tmp_path / "previous"))
    await await_until(asyncio.ensure_future(manager._run(info)))
    assert seen == [""]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["own_cwd", "ancestor_cwd", "elsewhere"])
async def test_busy_check_sees_a_running_subagent_working_in_the_repo(repo, tmp_path, where):
    """A run started from a chat outside the repository, working inside it."""
    outside = tmp_path / "other"
    parent = _run(
        "p1", cwd=str(repo) if where == "ancestor_cwd" else str(outside), parent="dashboard:x"
    )
    child = _run(
        "c1", cwd=str(repo / "sub") if where == "own_cwd" else str(outside), parent="subagent:p1"
    )

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
    await await_until(asyncio.ensure_future(manager._run(info)))
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
    await await_until(event.waiting)
    assert reached == [] and not turn.done()

    guard.release(root)
    with pytest.raises(_Stop):
        await await_until(turn)
    assert reached == ["setup"]


@pytest.mark.asyncio
async def test_a_turn_records_the_project_it_starts_in(tmp_path, repo, monkeypatch):
    """Recorded before the wait, so a busy check during it already sees the turn."""
    _stop_at_setup(monkeypatch)
    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-1")
    slot.project = str(repo / "sub")
    slot._checkout_turn_unplaced = True  # left over from an earlier turn
    state._slots[slot.key] = slot
    root = os.path.realpath(repo)
    event = _reserve_seen(root)

    turn = asyncio.create_task(chat_runner._run_chat(state, slot, "edit a file"))
    await await_until(event.waiting)
    assert slot._checkout_turn_projects == (str(repo / "sub"),)
    assert slot._checkout_turn_unplaced is False
    guard.release(root)
    with pytest.raises(_Stop):
        await await_until(turn)


def _running_slot(project: str, *, turn_projects=(), unplaced=False, running=True) -> _ChatSlot:
    slot = _ChatSlot("chat-1")
    slot.project = project
    slot._checkout_turn_projects = tuple(turn_projects)
    slot._checkout_turn_unplaced = unplaced
    slot._turn_admission_reserved = running  # what `running` reads while a turn is admitted
    return slot


def _sessions_with(cwd: str | None = None, *, error: Exception | None = None):
    def get_provider(_key):
        if error is not None:
            raise error
        return None if cwd is None else SimpleNamespace(cwd=cwd)

    return SimpleNamespace(get_provider=get_provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
async def test_busy_check_sees_a_turn_whose_project_was_changed_mid_turn(repo, tmp_path, running):
    """A project picked mid-turn moves ``project`` now, the session only next turn."""
    slot = _running_slot(
        str(tmp_path / "picked-mid-turn"), turn_projects=[str(repo)], running=running
    )
    state = SimpleNamespace(_slots={slot.key: slot}, subagents=None)
    root = os.path.realpath(repo)
    # An idle slot's record is the last turn's, which the busy check ignores.
    assert await gb._repo_has_running_work(state, root) is running
    assert (slot in gb._slots_in_repo(state, root)) is running


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
async def test_busy_check_reads_the_folder_the_live_session_runs_in(repo, tmp_path, running):
    """``set_project``, a member rebinding or a reset left armed: ``project`` and
    the turn's records all name elsewhere, but the live session runs in the repo."""
    elsewhere = str(tmp_path / "other")
    slot = _running_slot(elsewhere, turn_projects=[elsewhere], running=running)
    state = SimpleNamespace(
        _slots={slot.key: slot}, subagents=None, sessions=_sessions_with(str(repo / "sub"))
    )
    root = os.path.realpath(repo)
    assert await gb._repo_has_running_work(state, root) is running


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
async def test_an_unplaced_turn_is_busy_in_every_repository(repo, tmp_path, running):
    """Its session could land anywhere, so no repository can be switched under it."""
    slot = _running_slot(str(tmp_path / "other"), unplaced=True, running=running)
    state = SimpleNamespace(_slots={slot.key: slot}, subagents=None)
    assert await gb._repo_has_running_work(state, os.path.realpath(repo)) is running


@pytest.mark.asyncio
async def test_an_unreadable_session_registry_counts_as_busy(repo, tmp_path):
    slot = _running_slot(str(tmp_path / "other"))
    state = SimpleNamespace(
        _slots={slot.key: slot}, subagents=None, sessions=_sessions_with(error=RuntimeError())
    )
    assert await gb._repo_has_running_work(state, os.path.realpath(repo)) is True
    # An idle slot does not ask the registry at all.
    slot._turn_admission_reserved = False
    assert await gb._repo_has_running_work(state, os.path.realpath(repo)) is False


def test_a_running_root_chat_lends_its_turn_folders_to_the_lineage(repo, tmp_path):
    from kiro_crew.dashboard.chat_utils import effective_session_key

    slot = _running_slot(
        str(tmp_path / "picked-mid-turn"), turn_projects=[str(repo)], unplaced=True
    )
    child = _run("c1", cwd=str(tmp_path / "own"), parent=effective_session_key(slot))
    state = SimpleNamespace(
        _slots={slot.key: slot},
        subagents=SimpleNamespace(all_agents=[child]),
        sessions=_sessions_with(str(repo / "sub")),
    )
    folders = guard.subagent_folders(state, child)
    assert str(repo) in folders and str(repo / "sub") in folders


def _turn_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    state.sessions.get_pid = MagicMock(return_value=None)
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    return state


async def _finish(turn: asyncio.Task) -> None:
    turn.cancel()
    await await_until(asyncio.gather(turn, return_exceptions=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/prompts", "/prompts get review"])
async def test_prompt_commands_read_the_project_the_entry_wait_covered(
    tmp_path, repo, monkeypatch, command
):
    """A project picked during the entry wait may name a repository mid-switch,
    so the pre-claim prompt reads stay on the project the wait covered."""
    state = _turn_state(tmp_path, monkeypatch)
    read_from: list[object] = []

    def list_prompts(project_dir):
        read_from.append(project_dir)
        return []

    def resolve(message, project):
        read_from.append(project)
        return message, "not_found", ""

    monkeypatch.setattr(chat_runner, "_list_aim_prompts", list_prompts)
    monkeypatch.setattr(chat_runner, "_resolve_prompt_mention", resolve)
    slot = state.get_or_create_slot("s1")
    slot._titled = True
    slot.project = str(repo / "sub")
    root = os.path.realpath(repo)
    event = _reserve_seen(root)

    turn = asyncio.create_task(chat_runner._run_chat(state, slot, command))
    await await_until(event.waiting)
    slot.project = str(tmp_path / "other")  # picked while the turn waited
    guard.release(root)
    await async_wait_until(lambda: read_from)
    assert [str(p) for p in read_from] == [str(repo / "sub")]
    await _finish(turn)


@pytest.mark.asyncio
async def test_a_turn_allocating_its_session_waits_out_a_checkout_anywhere(
    tmp_path, repo, monkeypatch
):
    """Before the claim the session's folder is unknown, so a checkout of a
    repository the slot's project is not in still holds the claim back."""
    state = _turn_state(tmp_path, monkeypatch)
    claimed: list[bool] = []

    async def get_or_create(*_args, **_kwargs):
        claimed.append(slot._checkout_turn_unplaced)
        raise _Stop

    state.sessions.get_or_create = get_or_create
    slot = state.get_or_create_slot("s1")
    slot._titled = True
    slot.project = str(tmp_path / "other")
    slot._turn_admission_reserved = True
    root = os.path.realpath(repo)
    event = _reserve_seen(root)

    turn = asyncio.create_task(chat_runner._run_chat(state, slot, "edit a file"))
    await await_until(event.waiting)
    assert claimed == [] and slot._checkout_turn_unplaced is True
    # Any other switch arriving now refuses: this turn could land in its repo.
    assert await gb._repo_has_running_work(state, os.path.realpath(tmp_path / "unrelated")) is True
    guard.release(root)
    await async_wait_until(lambda: claimed == [True])
    await _finish(turn)


@pytest.mark.asyncio
async def test_a_claimed_session_records_its_folder_and_waits_out_its_checkout(
    tmp_path, repo, monkeypatch
):
    """A session that runs where ``project`` does not point (a deferred reset left
    armed, a resumed conversation's stored folder) is recorded by its own report."""
    state = _turn_state(tmp_path, monkeypatch)
    client = MagicMock()
    client.cwd = str(repo / "sub")
    root = os.path.realpath(repo)
    seen: dict[str, _SeenEvent] = {}

    async def get_or_create(*_args, **_kwargs):
        # A switch of the repo starts while the session is being claimed.
        seen["event"] = _reserve_seen(root)
        return client, False, False

    state.sessions.get_or_create = get_or_create
    slot = state.get_or_create_slot("s1")
    slot._titled = True
    slot.project = str(tmp_path / "other")
    slot._turn_admission_reserved = True

    turn = asyncio.create_task(chat_runner._run_chat(state, slot, "edit a file"))
    await async_wait_until(lambda: "event" in seen)
    await await_until(seen["event"].waiting)
    assert str(repo / "sub") in slot._checkout_turn_projects
    assert slot._checkout_turn_unplaced is False
    assert await gb._repo_has_running_work(state, root) is True
    guard.release(root)
    await _finish(turn)


@pytest.mark.asyncio
async def test_a_session_that_reports_no_folder_leaves_the_turn_unplaced(
    tmp_path, repo, monkeypatch
):
    """A provider that never says where it runs keeps switches refused for the turn."""
    state = _turn_state(tmp_path, monkeypatch)
    client = MagicMock()
    client.cwd = ""
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    slot = state.get_or_create_slot("s1")
    slot._titled = True
    other = str(tmp_path / "other")
    slot.project = other
    slot._turn_admission_reserved = True

    turn = asyncio.create_task(chat_runner._run_chat(state, slot, "edit a file"))
    # The claim's record landed (entry, allocation, the session's empty report).
    await async_wait_until(lambda: len(slot._checkout_turn_projects) == 3)
    assert slot._checkout_turn_projects == (other, other, "")
    assert slot._checkout_turn_unplaced is True
    assert await gb._repo_has_running_work(state, os.path.realpath(repo)) is True
    await _finish(turn)


@pytest.mark.asyncio
async def test_a_stop_and_a_new_row_during_the_wait_stay_visible_to_the_turn(
    tmp_path, repo, monkeypatch
):
    """The wait follows the Stop snapshots and the replay-row freeze.

    A Stop pressed while the turn waits must still read as pressed when it
    reaches dispatch, and a row appended meanwhile must not become its trigger.
    """
    seen: dict[str, object] = {}

    async def setup(*args, **kwargs):
        frame = sys._getframe(1)
        while frame is not None and frame.f_code.co_name != "_run_chat":
            frame = frame.f_back
        assert frame is not None
        seen["stop_gen"] = frame.f_locals["_stop_gen_at_entry"]
        seen["replay"] = frame.f_locals["_current_replay_message"]
        raise _Stop

    monkeypatch.setattr(chat_runner, "_refresh_genuine_turn_allowances", setup)
    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-1")
    slot.project = str(repo)
    slot.messages.append({"role": "user", "content": "edit a file"})
    trigger = slot.messages[-1]
    state._slots[slot.key] = slot
    root = os.path.realpath(repo)
    event = _reserve_seen(root)
    gen_before = slot._stop_generation

    turn = asyncio.create_task(chat_runner._run_chat(state, slot, "edit a file"))
    await await_until(event.waiting)
    slot._stop_generation += 1
    slot.messages.append({"role": "user", "content": "edit a file"})

    guard.release(root)
    with pytest.raises(_Stop):
        await await_until(turn)
    assert seen["stop_gen"] == gen_before
    assert seen["replay"] is trigger


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
        assert finish.wait(default_timeout())
        return {"ok": True, "branch": branch}, 200

    monkeypatch.setattr(gb, "_switch_sync", slow_switch)
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    slot = _ChatSlot("chat-1")
    slot.project = str(repo / "sub")
    switch_state = SimpleNamespace(_slots={slot.key: slot}, subagents=_Subagents())
    turn_state = _make_state(tmp_path)
    turn_state._slots[slot.key] = slot

    request = asyncio.create_task(gb.api_project_git_switch(_switch_request(switch_state)))
    await await_until(probing)
    event = _SeenEvent()
    guard._reserved[switch_boundary] = event  # same reservation, observable waiters
    turn = asyncio.create_task(chat_runner._run_chat(turn_state, slot, "edit a file"))
    await await_until(event.waiting)
    probe_done.set()
    await async_wait_until(started.is_set, describe=lambda: "the checkout worker never started")
    assert reached == [] and not turn.done() and not event.is_set()

    finish.set()
    response = await await_until(request)
    assert response.status == 200
    with pytest.raises(_Stop):
        await await_until(turn)
    assert reached == ["setup"]


def _orphan():
    """A grandchild whose parent run's card was dismissed, so its record is gone."""
    return _run("g1", cwd=os.path.join(os.sep, "unrelated", "place"), parent="subagent:dismissed")


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
    await await_until(event.waiting)
    assert not waiter.done()
    guard.release(elsewhere)
    await await_until(waiter)


# ── Workflow steps: sessions opened outside chat slots and the sub-agent manager ──


def _no_slots():
    return SimpleNamespace(_slots={}, subagents=None)


@pytest.mark.asyncio
async def test_tracked_work_is_busy_everywhere_until_placed_then_where_it_runs(repo, tmp_path):
    state, root = _no_slots(), os.path.realpath(repo)
    async with guard.tracked_work(waiter="workflow r1") as work:
        # Its session could land anywhere until it reports a folder.
        assert await gb._repo_has_running_work(state, root) is True
        # A session that reports no folder leaves it unplaced, even with a prompt
        # project recorded beside it.
        await work.place(SimpleNamespace(cwd=""), str(tmp_path / "other"))
        assert await gb._repo_has_running_work(state, root) is True
        await work.place(SimpleNamespace(cwd=str(tmp_path / "other")))
        assert await gb._repo_has_running_work(state, root) is False
        # The prompt is built against the repo though the session runs elsewhere.
        await work.place(SimpleNamespace(cwd=str(tmp_path / "other")), str(repo / "sub"))
        assert await gb._repo_has_running_work(state, root) is True
    assert await gb._repo_has_running_work(state, root) is False
    assert guard.tracked_work_folders() == ([], True)


@pytest.mark.asyncio
async def test_tracked_work_waits_out_any_checkout_then_one_of_its_folder(repo, tmp_path):
    elsewhere = os.path.realpath(tmp_path / "other")
    first = _reserve_seen(elsewhere)
    steps: list[str] = []
    go = asyncio.Event()

    async def step():
        async with guard.tracked_work(waiter="workflow r1") as work:
            steps.append("entered")
            await go.wait()
            await work.place(SimpleNamespace(cwd=str(repo / "sub")))
            steps.append("placed")

    task = asyncio.create_task(step())
    await await_until(first.waiting)
    # Registered unplaced before it waits, so a switch arriving now refuses.
    assert steps == [] and guard.tracked_work_folders() == ([], False)
    guard.release(elsewhere)
    await async_wait_until(lambda: steps == ["entered"])
    root = os.path.realpath(repo)
    second = _reserve_seen(root)
    go.set()
    await await_until(second.waiting)
    assert steps == ["entered"]
    guard.release(root)
    await await_until(task)
    assert steps == ["entered", "placed"] and guard.tracked_work_folders() == ([], True)


class _WfProvider:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.at_reset: list[tuple[list[str], bool]] = []

    async def new_conversation(self) -> None:
        self.at_reset.append(guard.tracked_work_folders())

    def is_process_alive(self) -> bool:
        return True


class _WfSessions:
    """A session manager double that records the tracked state at each claim."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.at_claim: list[tuple[list[str], bool]] = []

    async def get_or_create(self, key, **_kwargs):
        self.at_claim.append(guard.tracked_work_folders())
        return _WfProvider(self.cwd), True, False

    def release(self, key, *, cleanup=False):
        return None

    async def destroy(self, key):
        return None

    async def reset(self, key, **_kwargs):
        return None

    async def end_children_for(self, key):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["cold", "pooled", "named"])
async def test_a_workflow_step_is_tracked_while_its_session_runs(repo, tmp_path, monkeypatch, path):
    """Every way a step reaches a session: the cold per-call builder, a warm
    pooled worker, and the pool's unpooled leg for a named ``session=``. The
    session runs in one folder and the prompt is built against the run's
    project, so both are recorded."""
    from kiro_crew.workflows import agent_exec, agent_pool

    elsewhere, project = str(tmp_path / "other"), str(repo / "sub")
    sessions = _WfSessions(elsewhere)
    during: list[tuple[list[str], bool]] = []

    async def run_step(*_args, on_complete=None, **_kwargs):
        during.append(guard.tracked_work_folders())
        on_complete(SimpleNamespace(stop_reason="end_turn", synthetic_completion=False))
        return "done"

    monkeypatch.setattr(agent_exec, "stream_and_collect", run_step)
    monkeypatch.setattr(agent_pool, "_run_step", run_step)
    monkeypatch.setattr("kiro_crew.messaging.identity.publish_turn_identity", AsyncMock())
    monkeypatch.setattr(agent_pool, "publish_turn_identity", AsyncMock())
    pool = None
    if path == "cold":
        fn = agent_exec.build_agent_fn(sessions, run_id="r1", cwd=project)
        opts: dict = {}
    else:
        fn, pool = agent_pool.build_pooled_agent_fn(sessions, run_id="r1", cwd=project)
        opts = {"session": "named"} if path == "named" else {}
    try:
        assert await fn("do the step", opts) is not None
    finally:
        if pool is not None:
            await pool.shutdown()
    # Unplaced while the session was being claimed, then placed where it runs.
    assert sessions.at_claim == [([], False)]
    assert len(during) == 1 and during[0][1] is True
    assert set(during[0][0]) == {elsewhere, project}
    assert guard.tracked_work_folders() == ([], True)


@pytest.mark.asyncio
async def test_a_pooled_worker_reset_is_tracked(repo, monkeypatch):
    """Reusing a warm worker opens a new conversation before the step's turn."""
    from kiro_crew.workflows import agent_pool

    sessions = _WfSessions(str(repo / "sub"))
    worker = agent_pool._WorkflowSessionWorker(
        sessions, key="wf-pool:r1:0", agent=None, model=None, cwd=None
    )
    await worker.start()
    assert guard.tracked_work_folders() == ([], True)
    await worker.reset()
    assert worker._provider.at_reset == [([], False)]
    assert guard.tracked_work_folders() == ([], True)


class _EndedProvider:
    """A session whose process runs until something ends it, reporting the
    runtime's termination evidence the way AcpProvider does."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.alive = True
        self.shutdowns = 0
        self.survives_shutdown = False

    @property
    def process_tree_confirmed_dead(self) -> bool:
        return not self.alive

    async def shutdown(self) -> None:
        self.shutdowns += 1
        if not self.survives_shutdown:
            self.alive = False


class _EndingSessions:
    """The parts of the session manager a step's teardown uses."""

    def __init__(self) -> None:
        self.held: dict[str, object] = {}
        self.generation = 0
        self.destroys: list[tuple[str, int, bool]] = []
        self.refuse = 0
        self.gate: asyncio.Event | None = None
        self.retry_gate: asyncio.Event | None = None
        self.tracked_at_destroy: list[bool] = []

    def get_provider(self, key):
        return self.held.get(key)

    def session_generation(self, key):
        return self.generation

    async def destroy_if(self, key, generation, should_destroy):
        self.tracked_at_destroy.append(bool(guard._tracked))
        if self.gate is not None:
            await self.gate.wait()
        if self.destroys and self.retry_gate is not None:
            await self.retry_gate.wait()
        allowed = should_destroy()
        self.destroys.append((key, generation, allowed))
        if self.refuse:
            self.refuse -= 1
            return False
        if not allowed:
            return False
        provider = self.held.pop(key)
        await provider.shutdown()
        return True


async def _cancelled_step(provider, stop, *, raise_instead=False):
    placed = asyncio.Event()

    async def step():
        async with guard.tracked_work(waiter="workflow r1") as work:
            await work.place(provider, stop=stop)
            placed.set()
            if raise_instead:
                raise RuntimeError("step failed")
            await asyncio.Event().wait()  # the turn's tools run until cancelled

    task = asyncio.create_task(step())
    await await_until(placed)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancelled", "raised"])
async def test_a_failed_step_ends_its_session_before_it_is_untracked(repo, ending):
    """Cancelling a step's task does not stop its native turn, so the session is
    destroyed, and its process confirmed gone, before the busy check stops
    seeing the step."""
    root, state = os.path.realpath(repo), _no_slots()
    sessions, provider = _EndingSessions(), _EndedProvider(str(repo / "sub"))
    sessions.held["wf:r1:0"] = provider
    sessions.gate = asyncio.Event()
    task = await _cancelled_step(
        provider,
        lambda retry: guard.end_managed_session(sessions, "wf:r1:0", provider, escalate=retry),
        raise_instead=ending == "raised",
    )
    if ending == "cancelled":
        task.cancel()
    await async_wait_until(lambda: sessions.tracked_at_destroy == [True])
    # The teardown is under way and the session still runs: still busy.
    assert not task.done()
    assert await gb._repo_has_running_work(state, root) is True
    sessions.gate.set()
    results = await await_until(asyncio.gather(task, return_exceptions=True), what="step teardown")
    expected = asyncio.CancelledError if ending == "cancelled" else RuntimeError
    assert isinstance(results[0], expected)
    assert sessions.destroys == [("wf:r1:0", 0, True)]
    assert provider.alive is False and provider.shutdowns == 1
    assert not guard._tracked
    assert await gb._repo_has_running_work(state, root) is False


@pytest.mark.asyncio
async def test_a_step_that_finishes_normally_keeps_its_session(repo):
    sessions, provider = _EndingSessions(), _EndedProvider(str(repo / "sub"))
    sessions.held["wf:r1:0"] = provider
    async with guard.tracked_work(waiter="workflow r1") as work:
        await work.place(
            provider,
            stop=lambda retry: guard.end_managed_session(
                sessions, "wf:r1:0", provider, escalate=retry
            ),
        )
        work.turn_ended(_completion("end_turn"))
    assert sessions.destroys == [] and provider.alive is True and not guard._tracked


@pytest.mark.asyncio
async def test_the_teardown_never_destroys_a_successor_session(repo, monkeypatch):
    """Another session now under the key is left alone; the step's own provider,
    absent from the key and not confirmed gone, is shut down directly."""
    monkeypatch.setattr(guard, "TEARDOWN_RETRY_SECS", 0)
    sessions, provider = _EndingSessions(), _EndedProvider(str(repo / "sub"))
    successor = _EndedProvider(str(repo / "sub"))
    sessions.held["named"] = successor
    task = await _cancelled_step(
        provider,
        lambda retry: guard.end_managed_session(sessions, "named", provider, escalate=retry),
    )
    task.cancel()
    await await_until(asyncio.gather(task, return_exceptions=True), what="step teardown")
    await async_wait_until(lambda: not guard._tracked)
    assert sessions.destroys == []
    assert successor.alive is True and sessions.held["named"] is successor
    assert provider.alive is False and provider.shutdowns == 1


@pytest.mark.asyncio
async def test_the_fence_is_checked_again_under_the_destroy(repo, monkeypatch):
    """A successor registered while the destroy waits for the registry is spared."""
    monkeypatch.setattr(guard, "TEARDOWN_RETRY_SECS", 0)
    sessions, provider = _EndingSessions(), _EndedProvider(str(repo / "sub"))
    successor = _EndedProvider(str(repo / "sub"))
    sessions.held["named"] = provider
    sessions.gate = asyncio.Event()
    task = await _cancelled_step(
        provider,
        lambda retry: guard.end_managed_session(sessions, "named", provider, escalate=retry),
    )
    task.cancel()
    await async_wait_until(lambda: sessions.tracked_at_destroy == [True])
    sessions.held["named"] = successor
    sessions.gate.set()
    await await_until(asyncio.gather(task, return_exceptions=True), what="step teardown")
    # The refused destroy is retried, which then finds the step's session gone.
    await async_wait_until(lambda: not guard._tracked)
    assert sessions.destroys == [("named", 0, False)]
    assert successor.alive is True and sessions.held["named"] is successor
    assert provider.alive is False and successor.shutdowns == 0


@pytest.mark.asyncio
async def test_the_teardown_retries_while_a_turn_holds_the_session(repo, monkeypatch):
    monkeypatch.setattr(guard, "TEARDOWN_RETRY_SECS", 0)
    sessions, provider = _EndingSessions(), _EndedProvider(str(repo / "sub"))
    sessions.held["named"] = provider
    sessions.refuse = 2  # a sibling step's turn holds it twice over
    sessions.retry_gate = asyncio.Event()
    task = await _cancelled_step(
        provider,
        lambda retry: guard.end_managed_session(sessions, "named", provider, escalate=retry),
    )
    task.cancel()
    # The step itself ends after the first attempt; the retry carries on with the
    # record kept until the sibling's turn lets go.
    await await_until(asyncio.gather(task, return_exceptions=True), what="step teardown")
    assert len(sessions.destroys) == 1 and guard._tracked
    sessions.retry_gate.set()
    await async_wait_until(lambda: not guard._tracked)
    assert [allowed for _key, _gen, allowed in sessions.destroys] == [True, True, True]
    assert provider.alive is False and "named" not in sessions.held


@pytest.mark.asyncio
async def test_a_failing_teardown_keeps_the_record_and_is_retried(repo, monkeypatch):
    """Dropping the record on a failed teardown would let a switch run under a
    process that may still be running the turn; it fails closed instead."""
    monkeypatch.setattr(guard, "TEARDOWN_RETRY_SECS", 0)
    root, state = os.path.realpath(repo), _no_slots()
    provider = _EndedProvider(str(repo / "sub"))
    attempts: list[int] = []
    may_succeed = asyncio.Event()

    async def stop(retry: bool) -> bool:
        attempts.append(retry)
        if not may_succeed.is_set():
            raise RuntimeError("destroy failed")
        return True

    task = await _cancelled_step(provider, stop)
    task.cancel()
    await await_until(asyncio.gather(task, return_exceptions=True), what="step teardown")
    await async_wait_until(lambda: len(attempts) >= 3)
    assert guard._tracked
    assert await gb._repo_has_running_work(state, root) is True
    may_succeed.set()
    await async_wait_until(lambda: not guard._tracked)


@pytest.mark.asyncio
async def test_a_destroy_that_leaves_the_process_tree_keeps_the_record(repo, monkeypatch):
    """A destroy that returns is not proof: until the runtime confirms its tree
    exited (a descendant waiting for the orphan sweep), the step stays busy."""
    monkeypatch.setattr(guard, "TEARDOWN_RETRY_SECS", 0)
    root, state = os.path.realpath(repo), _no_slots()
    sessions, provider = _EndingSessions(), _EndedProvider(str(repo / "sub"))
    provider.survives_shutdown = True
    sessions.held["wf:r1:0"] = provider
    task = await _cancelled_step(
        provider,
        lambda retry: guard.end_managed_session(sessions, "wf:r1:0", provider, escalate=retry),
    )
    task.cancel()
    await await_until(asyncio.gather(task, return_exceptions=True), what="step teardown")
    # Destroyed, then shut down again on retry, and the tree still stands.
    await async_wait_until(lambda: provider.shutdowns >= 3)
    assert sessions.destroys == [("wf:r1:0", 0, True)] and guard._tracked
    assert await gb._repo_has_running_work(state, root) is True
    provider.alive = False
    await async_wait_until(lambda: not guard._tracked)


@pytest.mark.asyncio
async def test_a_session_another_teardown_removed_is_ended_on_retry(repo, monkeypatch):
    """Absent from the registry is not gone: a teardown that removed the session
    and then failed leaves its process running, so a retry shuts it down."""
    monkeypatch.setattr(guard, "TEARDOWN_RETRY_SECS", 0)
    sessions, provider = _EndingSessions(), _EndedProvider(str(repo / "sub"))
    task = await _cancelled_step(
        provider,
        lambda retry: guard.end_managed_session(sessions, "wf:r1:0", provider, escalate=retry),
    )
    task.cancel()
    await await_until(asyncio.gather(task, return_exceptions=True), what="step teardown")
    await async_wait_until(lambda: not guard._tracked)
    assert sessions.destroys == [] and provider.shutdowns == 1


def _completion(stop_reason, *, synthetic=False):
    return SimpleNamespace(stop_reason=stop_reason, synthetic_completion=synthetic)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completion", "kept"),
    [
        (_completion("end_turn"), True),
        (_completion("max_tokens"), True),
        (_completion("cancelled"), True),
        (_completion(""), True),
        # No completion: the collector stopped at its tool-call ceiling.
        (None, False),
        # Terminals the session layer makes up while the backend may run on.
        (_completion("error: tool stall"), False),
        (_completion("error: cancel unacked"), False),
        (_completion("error: compaction failed"), False),
        (_completion("timeout"), False),
        (_completion("stale_recover"), False),
        (_completion("end_turn", synthetic=True), False),
    ],
    ids=lambda v: "none" if v is None else getattr(v, "stop_reason", str(v)) or "absent",
)
async def test_only_a_turn_the_backend_finished_keeps_its_session(repo, completion, kept):
    sessions, provider = _EndingSessions(), _EndedProvider(str(repo / "sub"))
    sessions.held["named"] = provider
    async with guard.tracked_work(waiter="workflow r1") as work:
        await work.place(
            provider,
            stop=lambda retry: guard.end_managed_session(
                sessions, "named", provider, escalate=retry
            ),
        )
        if completion is not None:
            work.turn_ended(completion)
    assert (sessions.destroys == []) is kept and provider.alive is kept
    assert not guard._tracked


@pytest.mark.asyncio
async def test_a_cancelled_retry_keeps_the_record(repo, monkeypatch):
    monkeypatch.setattr(guard, "TEARDOWN_RETRY_SECS", 0)
    provider = _EndedProvider(str(repo / "sub"))

    async def never(retry: bool) -> bool:
        return False

    task = await _cancelled_step(provider, never)
    task.cancel()
    await await_until(asyncio.gather(task, return_exceptions=True), what="step teardown")
    await async_wait_until(lambda: bool(guard._ending))
    retries = list(guard._ending)
    for pending in retries:
        pending.cancel()
    await await_until(asyncio.gather(*retries, return_exceptions=True), what="step teardown")
    assert guard._tracked


@pytest.mark.asyncio
async def test_a_second_cancel_stops_the_wait_not_the_teardown(repo):
    sessions, provider = _EndingSessions(), _EndedProvider(str(repo / "sub"))
    sessions.held["wf:r1:0"] = provider
    sessions.gate = asyncio.Event()
    task = await _cancelled_step(
        provider,
        lambda retry: guard.end_managed_session(sessions, "wf:r1:0", provider, escalate=retry),
    )
    task.cancel()
    await async_wait_until(lambda: sessions.tracked_at_destroy == [True])
    task.cancel()
    await await_until(asyncio.gather(task, return_exceptions=True), what="step teardown")
    # The step is gone, but its session is not yet: still tracked.
    assert guard._tracked and provider.alive is True
    sessions.gate.set()
    await async_wait_until(lambda: not guard._tracked)
    # The destroy already under way was finished, not cancelled and started over.
    assert len(sessions.tracked_at_destroy) == 1 and sessions.destroys == [("wf:r1:0", 0, True)]
    assert provider.alive is False and provider.shutdowns == 1


class _FailingTurnSessions(_WfSessions):
    """A session manager double whose sessions can be ended."""

    def __init__(self, cwd: str) -> None:
        super().__init__(cwd)
        self.providers: dict[str, _EndedProvider] = {}
        self.ended: list[tuple[str, bool]] = []
        self.destroy_fails = False
        self.leaves_process = False

    async def get_or_create(self, key, **_kwargs):
        self.at_claim.append(guard.tracked_work_folders())
        provider = _EndedProvider(self.cwd)
        provider.survives_shutdown = self.leaves_process
        self.providers[key] = provider
        return provider, True, False

    def get_provider(self, key):
        return self.providers.get(key)

    def session_generation(self, key):
        return 0

    async def destroy_if(self, key, generation, should_destroy):
        if not should_destroy():
            return False
        return await self._end(key)

    async def destroy(self, key):
        await self._end(key)

    async def _end(self, key):
        if self.destroy_fails:
            raise RuntimeError("destroy failed")
        provider = self.providers.pop(key, None)
        if provider is None:
            return False
        self.ended.append((key, bool(guard._tracked)))
        if not self.leaves_process:
            provider.alive = False
        return True


def _workflow_fn(path, sessions, monkeypatch, turn):
    from kiro_crew.workflows import agent_exec, agent_pool

    monkeypatch.setattr(agent_exec, "stream_and_collect", turn)
    monkeypatch.setattr(agent_pool, "stream_and_collect", turn)
    monkeypatch.setattr("kiro_crew.messaging.identity.publish_turn_identity", AsyncMock())
    monkeypatch.setattr(agent_pool, "publish_turn_identity", AsyncMock())
    if path == "cold":
        # A named session outlives a step that finishes normally.
        return agent_exec.build_agent_fn(sessions, run_id="r1"), None, {"session": "kept"}
    fn, pool = agent_pool.build_pooled_agent_fn(sessions, run_id="r1")
    return fn, pool, ({"session": "named"} if path == "named" else {})


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["raised", "ceiling", "tool stall"])
@pytest.mark.parametrize("path", ["cold", "pooled", "named"])
async def test_a_failed_workflow_step_ends_its_session_while_tracked(
    repo, monkeypatch, path, ending
):
    """Every way a step runs a turn ends that session when the turn fails, is
    ended by the session layer, or is cut off at the tool-call ceiling, while the
    busy check still sees the step."""
    sessions = _FailingTurnSessions(str(repo / "sub"))

    async def turn(*_args, on_complete=None, **_kwargs):
        if ending == "raised":
            raise RuntimeError("turn failed")
        if ending == "tool stall":
            # The session layer ended the turn itself; the backend may run on.
            on_complete(SimpleNamespace(stop_reason="error: tool stall"))
        # "ceiling": the collector stops reading with no completion at all.
        return "partial"

    fn, pool, opts = _workflow_fn(path, sessions, monkeypatch, turn)
    try:
        if ending == "raised":
            with pytest.raises(RuntimeError):
                await fn("do the step", opts)
        else:
            assert await fn("do the step", opts) == "partial"
    finally:
        if pool is not None:
            await pool.shutdown()
    assert len(sessions.ended) == 1
    _key, tracked_then = sessions.ended[0]
    assert tracked_then is True
    assert sessions.providers == {} and not guard._tracked


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["destroy raises", "process survives"])
async def test_a_pooled_worker_whose_destroy_fails_stays_busy(repo, monkeypatch, failure):
    """The worker's own best-effort shutdown would swallow the failure; the
    checkout teardown reports it, so the step stays busy and is retried. A
    destroy that returns while the process tree survives is a failure too."""
    monkeypatch.setattr(guard, "TEARDOWN_RETRY_SECS", 0)
    root, state = os.path.realpath(repo), _no_slots()
    sessions = _FailingTurnSessions(str(repo / "sub"))
    sessions.destroy_fails = failure == "destroy raises"
    sessions.leaves_process = failure == "process survives"

    async def turn(*_args, **_kwargs):
        raise RuntimeError("turn failed")

    fn, pool, opts = _workflow_fn("pooled", sessions, monkeypatch, turn)
    with pytest.raises(RuntimeError):
        await fn("do the step", opts)
    assert guard._tracked and await gb._repo_has_running_work(state, root) is True
    sessions.destroy_fails = False
    for provider in [*sessions.providers.values(), *_survivors(sessions)]:
        provider.survives_shutdown = False
    await async_wait_until(lambda: not guard._tracked)
    await pool.shutdown()
    assert sessions.providers == {}


def _survivors(sessions):
    """Providers a destroy removed from the double but left running."""
    return [w.provider for w in guard._tracked.values() if w.provider is not None]
