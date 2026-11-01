"""Steering, releasing and continuing a kept run: off the loop, on the run's own model.

``steer_run`` typed errors and provider dispatch; ``release_conversation`` and the
reaper's TTL sweep; continuation paths that reach the task store only off the
gateway loop; a stale resume entry that never holds a conversation; a default-cwd
continuation; and a continuation that keeps the model and reasoning effort its run
was spawned with unless the caller names a model.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import subagent_continuable_helpers
from subagent_continuable_helpers import _manager, _mock_sessions, _stop_reason

from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_persistence import create_agent_folder, write_run_agent
from kiro_crew.testing.wait import async_wait_until, await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_close_subagent_managers = subagent_continuable_helpers._close_subagent_managers


class TestSteerRun:
    @pytest.mark.asyncio
    async def test_unknown_id(self) -> None:
        manager = _manager()
        ok, detail = await manager.steer_run("nope", "hi")
        assert not ok and detail == "not_found"

    @pytest.mark.asyncio
    async def test_finished_run_refused(self) -> None:
        manager = _manager()
        manager._agents["a1"] = SubagentInfo(id="a1", task="t", done=True)
        ok, detail = await manager.steer_run("a1", "hi")
        assert not ok and detail.startswith("not_running")

    @pytest.mark.asyncio
    async def test_steer_dedicated_provider(self) -> None:
        sessions = _mock_sessions()
        provider = AsyncMock()
        provider.steer = AsyncMock(return_value=True)
        sessions.get_provider = MagicMock(return_value=provider)
        manager = _manager(sessions)
        manager._agents["a1"] = SubagentInfo(id="a1", task="t")
        with patch("kiro_crew.subagent.sel"):
            ok, detail = await manager.steer_run("a1", "course correct")
        assert ok and detail == "ok"
        provider.steer.assert_awaited_once_with("course correct")

    @pytest.mark.asyncio
    async def test_steer_shared_provider(self) -> None:
        manager = _manager()
        shared = AsyncMock()
        shared.steer = AsyncMock(return_value=True)
        info = SubagentInfo(id="a1", task="t")
        info._session_sharing = True
        info._shared_provider = shared
        manager._agents["a1"] = info
        with patch("kiro_crew.subagent.sel"):
            ok, _ = await manager.steer_run("a1", "adjust")
        assert ok
        shared.steer.assert_awaited_once_with("adjust")

    @pytest.mark.asyncio
    async def test_a_session_that_can_lose_a_delivered_steer_is_refused(self) -> None:
        """codex can drop a steer it reported delivered; a subagent run cannot
        requeue it, so the steer is refused and the parent is told to follow up."""
        manager = _manager()
        shared = AsyncMock()
        shared.steer = AsyncMock(return_value=True)
        shared.steer_needs_loss_recovery = True
        info = SubagentInfo(id="a1", task="t")
        info._session_sharing = True
        info._shared_provider = shared
        manager._agents["a1"] = info
        ok, detail = await manager.steer_run("a1", "adjust")
        assert not ok and detail.startswith("steer_unsupported") and "follow_up" in detail
        shared.steer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_session_reachable(self) -> None:
        """A live run with no reachable session now gets the startup
        grace, then the typed ``session_starting`` refusal (retryable) —
        not the old terminal bare ``no_session``."""
        import kiro_crew.subagent as subagent_mod

        sessions = _mock_sessions()
        sessions.get_provider = MagicMock(return_value=None)
        manager = _manager(sessions)
        manager._agents["a1"] = SubagentInfo(id="a1", task="t")
        with (
            patch.object(subagent_mod, "_STEER_STARTUP_WAIT_SECS", 0.05),
            patch.object(subagent_mod, "_STEER_STARTUP_POLL_SECS", 0.01),
        ):
            ok, detail = await manager.steer_run("a1", "hi")
        assert not ok and detail.startswith("session_starting")


# ── release_conversation + TTL sweep ──


class TestReleaseAndSweep:
    def test_release_busy_refused(self) -> None:
        manager = _manager()
        manager._agents["c1"] = SubagentInfo(id="c1", task="t")
        ok, detail = manager.release_conversation("c1")
        assert not ok and detail.startswith("conversation_busy")

    def test_queued_continuation_blocks_release_and_continue(self) -> None:
        """A continuation waiting in the spawn queue
        must count as busy — otherwise spawn_release deletes the session
        files the queued run needs (it would die with resume_failed), and a
        second continue could race the same conversation."""
        manager = _manager()
        manager._queue.append(
            {
                "task": "queued follow-up",
                "conversation_key": "subagent:qc1",
                "_preassigned_id": "newrun99",
            }
        )
        ok, detail = manager.release_conversation("qc1")
        assert not ok and detail.startswith("conversation_busy")
        with patch("kiro_crew.subagent.sel"):
            info = manager.continue_conversation("qc1", "another follow-up")
        assert info is not None and info.done
        assert info.error.startswith("conversation_busy")

    def test_queued_plain_run_blocks_release_of_its_own_conversation(self) -> None:
        """A queued plain run (no conversation_key) occupies its own
        preassigned id's conversation."""
        manager = _manager()
        manager._queue.append(
            {"task": "queued plain", "conversation_key": "", "_preassigned_id": "qp1"}
        )
        ok, detail = manager.release_conversation("qp1")
        assert not ok and detail.startswith("conversation_busy")

    def test_release_deletes_files_and_registry(self) -> None:
        sessions = _mock_sessions()
        manager = _manager(sessions)
        # flake-ok: live-clock registry stamp; release never reads its age
        manager._conversations["subagent:c1"] = time.time()
        with patch("kiro_crew.subagent._cleanup_session_files_sync") as cleanup:
            ok, detail = manager.release_conversation("c1")
        assert ok and detail == "released"
        cleanup.assert_called_once_with("sid-123", "acp")
        assert "subagent:c1" not in manager._conversations
        sessions.forget_conversation.assert_called_once_with("subagent:c1")

    def test_release_gone_when_no_sid(self) -> None:
        sessions = _mock_sessions()
        sessions.forget_conversation = MagicMock(return_value=None)
        manager = _manager(sessions)
        ok, detail = manager.release_conversation("c1")
        assert not ok and detail.startswith("conversation_gone")

    @pytest.mark.asyncio
    async def test_sweep_expires_only_idle_past_ttl(self) -> None:
        sessions = _mock_sessions()
        manager = _manager(sessions)
        # flake-ok: live-clock read handed to the sweep as its clock; ages are relative to it
        now = time.time()
        manager._conversations["subagent:old1"] = now - 7 * 3600  # expired
        manager._conversations["subagent:new1"] = now - 60  # fresh
        with patch("kiro_crew.subagent._cleanup_session_files_sync"):
            await manager._sweep_conversations_async(now)
        assert "subagent:old1" not in manager._conversations
        assert "subagent:new1" in manager._conversations

    @pytest.mark.asyncio
    async def test_sweep_drops_malformed_registry_key(self) -> None:
        manager = _manager()
        # flake-ok: live-clock read handed to the sweep as its clock; ages are relative to it
        now = time.time()
        manager._conversations["malformed"] = now - 7 * 3600
        with patch.object(manager, "release_conversation_async", AsyncMock()) as release:
            await manager._sweep_conversations_async(now)
        assert "malformed" not in manager._conversations
        release.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_refreshes_busy_conversation(self) -> None:
        sessions = _mock_sessions()
        manager = _manager(sessions)
        # flake-ok: live-clock read handed to the sweep as its clock; ages are relative to it
        now = time.time()
        manager._conversations["subagent:busy1"] = now - 7 * 3600
        live = SubagentInfo(id="busy1", task="t")  # not done
        manager._agents["busy1"] = live
        await manager._sweep_conversations_async(now)
        assert manager._conversations["subagent:busy1"] == now  # refreshed


@pytest.mark.asyncio
async def test_the_continuation_path_takes_no_store_call_on_the_loop(monkeypatch) -> None:
    """A continuation dispatched from the gateway loop reaches the task store
    only on its writer thread -- dispatch, the run's mode publication, and the
    terminal settle.

    ``store.loop_thread_calls`` is what the claim rests on, not the guard alone:
    ``OnLoopDBGuard.check`` raises, and most store call sites sit inside an
    ``except Exception`` that swallows the raise, so a violation shows up as a
    number and not as a failure. The guard is armed as well, for the sites that
    do propagate. The counter also covers the SHAPE this pins against: both
    writes the sync entry would take here (``taskq_accept``, ``taskq_claim``)
    wait on ``TaskStore._lock``, and a 1s hold by the writer thread freezes a
    coroutine caller's loop for the whole hold -- measured 0 of ~95 due 10ms
    heartbeat ticks served through the sync entry against 91 through this one.
    """
    from kiro_crew.subagent_manager import admission as admission_mod
    from kiro_crew.subagent_persistence import create_agent_folder
    from kiro_crew.taskq import store as store_mod

    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    create_agent_folder("no-loop-db", task="original", memory_mode="persistent")
    manager = _manager(_mock_sessions(resumed=True))
    await manager.wait_taskq_ready()
    manager._spawn_stagger_secs = 0.0
    store = manager._admission.taskq_store()
    assert store is not None, "this pin needs the real durable store"
    before = store.loop_thread_calls
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    info = await manager.continue_conversation_async("no-loop-db", "follow up")
    assert info is not None and not info.error, getattr(info, "error", None)
    task = manager._tasks.get(info.id)
    if task is not None:
        await await_until(asyncio.gather(task, return_exceptions=True), what="the run finishing")
    # The posted store writes join ``_report_tasks``; they land still under the guard.
    while manager._report_tasks:
        await await_until(
            asyncio.gather(*manager._report_tasks, return_exceptions=True),
            what="the posted store writes landing",
        )
    assert store.loop_thread_calls == before  # the reads below are the test's own
    monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
    # The run really ran: a refusal or a cancellation would take no store call
    # either, and would pass an assertion that only counted.
    #
    # ``queued`` is checked FIRST and on its own, because it is the one
    # never-started outcome the two markers below miss: a handle the claim
    # refused carries ``_memory_mode_ready`` at its dataclass DEFAULT of True
    # (only a registered run has it set from the conversation key), so a
    # continuation that never left the queue passes the pair.
    assert not info.queued and info.done, _stop_reason(info)
    assert info._memory_mode_ready and not info._cancel_retry_used, _stop_reason(info)
    assert store.get(info.id) is not None


def test_a_stale_resume_entry_does_not_hold_a_conversation() -> None:
    """A ``_resume_id`` window entry is a RESIDENT run asking for the lane slot
    it yielded, not an unstarted spawn, so it never answers
    ``_conversation_busy`` -- the same separation the pump's grant loop, the
    refill census, the eviction and the child reserve make.

    An entry whose run is still live is answered by the ``_agents`` scan first.
    The case that reaches this branch is a run that ENDED with its request still
    queued: the queued-stop path leaves the entry alone by design (dropping it
    published a "never started" terminal over a live run) and only the bounded
    waiter's give-up arm withdraws one, while the pump returns above its resume
    loop whenever no slot is free. Counted, that entry refuses every
    continuation and every release of the conversation with a
    ``conversation_busy`` naming a run that is already done.
    """
    from kiro_crew.subagent_persistence import create_agent_folder

    create_agent_folder("resume-held", task="original", memory_mode="persistent")
    manager = _manager(_mock_sessions(resumed=True))
    manager._agents["resume-held"] = SubagentInfo(
        id="resume-held", task="original", done=True, user_stopped=True
    )
    manager._queue.append(
        {
            "_resume_id": "resume-held",
            "_preassigned_id": "resume-held",
            "parent_session_key": "web-1",
            "batch_id": "",
            "reason": "children finished",
        }
    )
    assert manager._conversation_busy("subagent:resume-held") is None
    ok, detail = manager.release_conversation("resume-held")
    assert "conversation_busy" not in detail, detail
    # An UNSTARTED entry for the same conversation still holds it.
    manager._queue.append({"_preassigned_id": "resume-held"})
    held = manager._conversation_busy("subagent:resume-held")
    assert held is not None and held.queued and held.id == "resume-held"


@pytest.mark.asyncio
async def test_the_followup_watcher_dispatches_no_store_call_on_the_loop(monkeypatch) -> None:
    """The follow-up watcher is the manager's OWN continuation caller
    (``spawn_steer mode="follow_up"``), and it dispatches from a task on the
    gateway loop -- so the entry it picks is what decides whether a queued
    correction costs the loop two ``BEGIN IMMEDIATE`` waits.

    Pinned separately from the dispatch pin because a counter over
    ``continue_conversation_async`` says nothing about which entry
    ``_deliver_followups`` calls: swapping that one line to the sync entry
    leaves the other pin green.
    """
    from kiro_crew.subagent_manager import admission as admission_mod
    from kiro_crew.subagent_persistence import create_agent_folder
    from kiro_crew.taskq import store as store_mod

    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    create_agent_folder("followup-conv", task="original", memory_mode="persistent")
    manager = _manager(_mock_sessions(resumed=True))
    await manager.wait_taskq_ready()
    manager._spawn_stagger_secs = 0.0
    store = manager._admission.taskq_store()
    assert store is not None, "this pin needs the real durable store"
    # A finished run whose task is already popped: what the watcher waits for
    # before it dispatches the queue as ONE continuation.
    done = SubagentInfo(
        id="followup-conv",
        task="original",
        conversation_key="subagent:followup-conv",
        done=True,
    )
    done.pending_followups = ["also fix the test"]
    manager._agents["followup-conv"] = done
    before = store.loop_thread_calls
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    await await_until(
        asyncio.create_task(manager._deliver_followups(done)), what="the watcher dispatching"
    )
    child = next((a for a in manager._agents.values() if a.id != "followup-conv"), None)
    assert child is not None, "the watcher dispatched no continuation"
    task = manager._tasks.get(child.id)
    if task is not None:
        await await_until(asyncio.gather(task, return_exceptions=True), what="the run finishing")
    # The posted store writes join ``_report_tasks``; they land still under the guard.
    while manager._report_tasks:
        await await_until(
            asyncio.gather(*manager._report_tasks, return_exceptions=True),
            what="the posted store writes landing",
        )
    assert store.loop_thread_calls == before  # the reads below are the test's own
    monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
    # The dispatch really happened (a settled queue with no child would pass a
    # count-only assertion), and it STARTED: a claim the store refused comes
    # back queued and takes no on-loop call either.
    assert done.pending_followups == []
    assert not child.queued, _stop_reason(child)
    assert store.get(child.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("moved_pool", [False, True], ids=["same-default", "changed-default"])
async def test_default_cwd_continuation_does_not_request_override(tmp_path, moved_pool):
    from kiro_crew.config.loader import KiroCrewConfig

    project = tmp_path / "original-project"
    project.mkdir()
    other = tmp_path / "other-project"
    other.mkdir()

    def disable_overrides():
        cfg = KiroCrewConfig.load()
        cfg.agent.subagent_cwd_allowed_roots = []
        cfg.save()

    await asyncio.to_thread(disable_overrides)
    sessions = _mock_sessions()
    sessions._pool_cwd = str(project)
    provider = sessions.get_or_create.return_value[0]
    provider.cwd = str(project)
    manager = _manager(sessions)
    manager._spawn_stagger_secs = 0
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        original = manager.spawn("first task", keep=True)
        assert original is not None and not original.error
        await await_until(manager._tasks[original.id], what="the original run finishing")
        assert not original.error
        sessions.get_or_create.return_value = (provider, True, True)
        if moved_pool:
            sessions._pool_cwd = str(other)
        recorded = await asyncio.to_thread(manager.recorded_cwd, original.id)
        followup = manager.continue_conversation(original.id, "next task", cwd=recorded)
        assert followup is not None
        if moved_pool:
            assert recorded == str(project)
            assert "cwd override is disabled" in followup.error
            assert sessions.get_or_create.await_count == 1
        else:
            assert recorded == "" and not followup.error
            await await_until(manager._tasks[followup.id], what="the follow-up run finishing")
            assert not followup.error
            assert sessions.get_or_create.await_count == 2


# A continuation is another turn of the SAME run: it keeps the model and the
# reasoning effort the run was spawned with, unless the caller names a model.
# The provider request is read from ``get_or_create``'s kwargs. Synthetic ids.


def _pinned_run(manager: SubagentManager, run_id: str) -> SubagentInfo:
    run = SubagentInfo(id=run_id, task="first task", done=True)
    run.model = "model-a"
    run.reasoning_effort = "high"
    run.requested_model = "model-a"
    manager._agents[run_id] = run
    return run


def _provider_request(sessions: MagicMock) -> dict[str, object]:
    kwargs = sessions.get_or_create.call_args.kwargs
    return {
        "model": kwargs.get("model"),
        "reasoning_effort_override": kwargs.get("reasoning_effort_override"),
    }


@pytest.mark.asyncio
async def test_continuation_without_a_model_keeps_the_runs_model_and_effort() -> None:
    await asyncio.to_thread(create_agent_folder, "pinned-b", memory_mode="persistent")
    await asyncio.to_thread(write_run_agent, "pinned-b", "")
    sessions = _mock_sessions(resumed=True)
    manager = _manager(sessions)
    _pinned_run(manager, "pinned-b")
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch.object(manager, "_promote_conversation", return_value=object()),
    ):
        info = manager.continue_conversation("pinned-b", "follow-up work")
        assert info is not None and not info.error, info.error
        await await_until(manager._tasks[info.id], what="the run finishing")
    assert _provider_request(sessions) == {
        "model": "model-a",
        "reasoning_effort_override": "high",
    }
    assert info.requested_model == "model-a"


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        (
            {"pinned_model": "model-a", "reasoning_effort": "high"},
            {"model": "model-a", "reasoning_effort_override": "high"},
        ),
        # ``requested_model`` is the effective pin, which also holds a role pin
        # or a routed tier, so it is never read back as the caller's pin: a run
        # whose caller pinned nothing records "" and asks for no model, and so
        # does a record written before ``pinned_model`` existed.
        (
            {"pinned_model": "", "reasoning_effort": ""},
            {"model": None, "reasoning_effort_override": None},
        ),
        ({}, {"model": None, "reasoning_effort_override": None}),
    ],
    ids=["caller-pinned", "caller-pinned-nothing", "record-without-the-field"],
)
@pytest.mark.asyncio
async def test_continuation_after_a_restart_keeps_the_recorded_model_and_effort(
    recorded: dict[str, str], expected: dict[str, object]
) -> None:
    # After a gateway restart the run is gone from memory; its state.json
    # records the model the caller pinned and the effort it was spawned with.
    await asyncio.to_thread(create_agent_folder, "pinned-c", memory_mode="persistent")
    await asyncio.to_thread(write_run_agent, "pinned-c", "")
    sessions = _mock_sessions(resumed=True)
    sessions.resumable_sid = MagicMock(side_effect=[None, "sid-from-state"])
    manager = _manager(sessions)
    state = {
        "session_id": "sid-from-state",
        "provider": "acp",
        "cwd": "",
        "keep": True,
        "requested_model": "model-a",
        "resolved_model": "model-a",
        **recorded,
    }
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.read_state", return_value=state),
        patch.object(manager, "_promote_conversation", return_value=object()),
    ):
        info = manager.continue_conversation("pinned-c", "follow-up after restart")
        assert info is not None and not info.error, info.error
        await await_until(manager._tasks[info.id], what="the run finishing")
    assert _provider_request(sessions) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True], ids=["in-memory", "after-a-restart"])
async def test_a_continuation_keeps_the_runs_lineage_depth(restart: bool) -> None:
    # The run's parent record is gone, so admission alone would count the
    # follow-up one level below a sub-agent; the run's own depth (live, or
    # from state.json after a restart) keeps it where the run sat.
    conv_id = "deep-after-restart" if restart else "deep-in-memory"
    await asyncio.to_thread(create_agent_folder, conv_id, memory_mode="persistent")
    await asyncio.to_thread(write_run_agent, conv_id, "")
    sessions = _mock_sessions(resumed=True)
    if restart:
        sessions.resumable_sid = MagicMock(side_effect=[None, "sid-from-state"])
    manager = _manager(sessions)
    state = {"session_id": "sid-from-state", "provider": "acp", "cwd": "", "keep": True}
    if restart:
        state["depth"] = 4
    else:
        manager._agents[conv_id] = SubagentInfo(id=conv_id, task="first task", done=True, depth=4)
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.read_state", return_value=state),
        patch.object(manager, "_promote_conversation", return_value=object()),
    ):
        info = manager.continue_conversation(
            conv_id, "follow-up", parent_session_key="subagent:gone"
        )
        assert info is not None and not info.error, info.error
        await await_until(manager._tasks[info.id], what="the run finishing")
    assert info.depth == 4


@pytest.mark.asyncio
async def test_a_follow_up_keeps_the_runs_model_and_effort() -> None:
    await asyncio.to_thread(create_agent_folder, "pinned-a", memory_mode="persistent")
    await asyncio.to_thread(write_run_agent, "pinned-a", "")
    sessions = _mock_sessions(resumed=True)
    manager = _manager(sessions)
    run = _pinned_run(manager, "pinned-a")
    run.done = False
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch.object(manager, "_promote_conversation", return_value=object()),
    ):
        ok, detail = await manager.follow_up_run("pinned-a", "one more thing")
        assert ok, detail
        run.done = True  # the run's turn ends, so the watcher dispatches
        await async_wait_until(
            lambda: sessions.get_or_create.await_count,
            describe=lambda: f"the follow-up was never dispatched; tasks={sorted(manager._tasks)!r}",
        )
        children = [t for k, t in manager._tasks.items() if k != "pinned-a"]
        await await_until(
            asyncio.gather(*children, return_exceptions=True), what="the follow-up run finishing"
        )
    assert _provider_request(sessions) == {
        "model": "model-a",
        "reasoning_effort_override": "high",
    }


@pytest.mark.asyncio
async def test_an_explicit_model_on_a_continuation_wins_over_the_runs_model() -> None:
    await asyncio.to_thread(create_agent_folder, "pinned-x", memory_mode="persistent")
    await asyncio.to_thread(write_run_agent, "pinned-x", "")
    sessions = _mock_sessions(resumed=True)
    manager = _manager(sessions)
    _pinned_run(manager, "pinned-x")
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch.object(manager, "_promote_conversation", return_value=object()),
    ):
        info = manager.continue_conversation("pinned-x", "follow-up work", model="model-b")
        assert info is not None and not info.error, info.error
        await await_until(manager._tasks[info.id], what="the run finishing")
    assert _provider_request(sessions)["model"] == "model-b"
    assert info.requested_model == "model-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True], ids=["in-memory", "after-a-restart"])
async def test_a_continuation_follows_a_later_change_of_the_role_pin(restart: bool) -> None:
    # A run whose caller pinned no model ran on the sub-agent role pin, which is
    # what ``requested_model`` records. Its continuation follows a later change
    # of that pin whether or not the gateway restarted in between: the record
    # keeps what the CALLER pinned, which is nothing here, and not the role pin
    # the run happened to resolve.
    conv_id = "role-after-restart" if restart else "role-in-memory"
    await asyncio.to_thread(create_agent_folder, conv_id, memory_mode="persistent")
    await asyncio.to_thread(write_run_agent, conv_id, "")
    sessions = _mock_sessions(resumed=True)
    manager = _manager(sessions)
    state = {
        "session_id": "sid-from-state",
        "provider": "acp",
        "cwd": "",
        "keep": True,
        "pinned_model": "",
        "requested_model": "role-model-x",
        "resolved_model": "role-model-x",
        "reasoning_effort": "",
    }
    if not restart:
        run = SubagentInfo(id=conv_id, task="first task", done=True)
        run.requested_model = "role-model-x"
        manager._agents[conv_id] = run
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.read_state", return_value=state),
        # The operator moved the role pin from X to Y after the run started.
        patch("kiro_crew.subagent._subagent_default_model", return_value="role-model-y"),
        patch.object(manager, "_promote_conversation", return_value=object()),
    ):
        info = manager.continue_conversation(conv_id, "follow-up work")
        assert info is not None and not info.error, info.error
        await await_until(manager._tasks[info.id], what="the run finishing")
    assert _provider_request(sessions)["model"] == "role-model-y"
    assert info.requested_model == "role-model-y"


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True], ids=["in-memory", "after-a-restart"])
async def test_a_continuation_naming_another_model_carries_no_effort(restart: bool) -> None:
    # Effort follows the model: the run's ``high`` was set for ``model-a``, so a
    # caller who names ``model-b`` gets that model's own effort resolution (role
    # pin, else default) instead of the run's.
    conv_id = "named-after-restart" if restart else "named-in-memory"
    await asyncio.to_thread(create_agent_folder, conv_id, memory_mode="persistent")
    await asyncio.to_thread(write_run_agent, conv_id, "")
    sessions = _mock_sessions(resumed=True)
    manager = _manager(sessions)
    state = {
        "session_id": "sid-from-state",
        "provider": "acp",
        "cwd": "",
        "keep": True,
        "pinned_model": "model-a",
        "reasoning_effort": "high",
    }
    if not restart:
        _pinned_run(manager, conv_id)
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.read_state", return_value=state),
        patch.object(manager, "_promote_conversation", return_value=object()),
    ):
        info = manager.continue_conversation(conv_id, "follow-up work", model="model-b")
        assert info is not None and not info.error, info.error
        await await_until(manager._tasks[info.id], what="the run finishing")
    assert _provider_request(sessions) == {"model": "model-b", "reasoning_effort_override": None}
