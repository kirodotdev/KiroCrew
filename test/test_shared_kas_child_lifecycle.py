"""Sub-agents on their parent's KAS process: one session id open in one process.

KAS fences each session with a cross-process lease (``session-lease.ts`` in the
engine): the process that has a session loaded holds it, and another process
that loads the same id gets a copy whose every turn is refused with
``SessionHeldElsewhereError`` until the holder lets go. So no record is ever
written twice, and the harm of a session open in two processes is a refused
turn. These tests pin the three lifecycle points where one id could be open in
two processes, or a child be killed that nothing meant to stop:

* a ``spawn_continue`` admitted while the finished child's ``session/close`` on
  the parent's process is still in flight, or never confirmed;
* a parent recycle whose kill is refused because a child is mid-turn, which left
  the parent's own session resident on that process while its successor loaded
  it on a new one;
* a parent recycle that killed a child bound to the process but not yet in its
  first turn (the MCP readiness wait), because only a turn defended it.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_continuable_helpers import _manager

from kiro_crew import runtime_ownership as ro
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")


@pytest.fixture(autouse=True)
def _clean_ownership_tables():
    ro._reset_for_tests()
    yield
    ro._reset_for_tests()


class _Runtime:
    """A shared ACP process: its kill passes the real ownership gate."""

    def __init__(self, pid: int, backend: str = ACP_BACKEND_KAS) -> None:
        self.pid = pid
        self.acp_backend = backend
        self.is_cloud_sandbox = False
        self.subagent_departed = False
        self.process_tree_confirmed_dead = False
        self._unconfirmed_teardowns: set[str] = set()
        self._session_queues: dict[str, object] = {}
        self._alive = True
        self._keeping: list[str] = []
        self.kills: list[str] = []

    def is_alive(self) -> bool:
        return self._alive

    def can_release(self) -> bool:
        return True

    #: Whether the host answers a repeated ``session/close`` (it finished the close
    #: after Crew's first request timed out), or still holds the session.
    answers_close = True

    async def evict_session(self, session_id: str) -> bool:
        self.closes_resent = getattr(self, "closes_resent", 0) + 1
        if self.answers_close:
            self._unconfirmed_teardowns.discard(session_id)
        return self.answers_close

    @contextlib.contextmanager
    def keep_record_on_teardown(self, session_id: str):
        self._keeping.append(session_id)
        try:
            yield
        finally:
            self._keeping.remove(session_id)

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        if not ro.authorize_runtime_kill(self, reason=reason, caller="test"):
            return
        self.kills.append(reason)
        self._alive = False
        self.process_tree_confirmed_dead = True


class _Handle:
    """A session on that process; records each close and whether it kept the record."""

    def __init__(self, session_id: str, runtime: _Runtime) -> None:
        self.session_id = session_id
        self.memory_mode = "persistent"
        self.kas_memory_mode = ""
        self.is_turn_active = False
        self.keep_transcript = True
        self.closes: list[bool] = []
        self._runtime = runtime
        self._queue = object()
        runtime._session_queues[session_id] = self._queue

    async def destroy(self) -> None:
        self.closes.append(self.session_id in self._runtime._keeping)
        self._runtime._session_queues.pop(self.session_id, None)


def _parent(runtime: _Runtime, sid: str = "sess-parent") -> tuple[AcpSessionProvider, _Handle]:
    handle = _Handle(sid, runtime)
    provider = AcpSessionProvider(
        handle,  # type: ignore[arg-type]
        runtime,  # type: ignore[arg-type]
        owns_runtime=True,
        session_key="dashboard:chat-1",
    )
    return provider, handle


async def _bind_child(runtime: _Runtime, child_id: str = "child") -> tuple[SubagentInfo, _Handle]:
    """A child placed on the parent's process: the real bind, before its first turn."""
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=None)
    info = SubagentInfo(
        id=child_id, task="t", agent="kirocrew", parent_session_key="dashboard:chat-1"
    )
    handle = _Handle(f"sess-{child_id}", runtime)
    cfg = KiroCrewConfig(agent=AgentConfig())
    with patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)):
        await manager._bind_shared_handle(info, f"subagent:{child_id}", runtime, handle)
    return info, handle


# -- R2: a recycle whose kill a live child refuses --


@pytest.mark.asyncio
async def test_a_refused_parent_kill_closes_the_parents_own_session_on_that_process() -> None:
    """The child mid-turn keeps the process alive, so the parent's session must leave it.

    Left resident, KAS keeps the parent's lease on the old process and refuses
    every turn the successor runs on its fresh load, until the child finishes.
    """
    runtime = _Runtime(pid=7101)
    parent, handle = _parent(runtime)
    child_turn = ro.claim_runtime_tenancy(runtime, holder="subagent:x", session_key="subagent:x")

    await parent.shutdown()

    assert runtime.is_alive() and not runtime.kills, "the child is mid-turn: the kill is refused"
    assert handle.closes == [True], (
        "the parent's session stayed resident on the process the child keeps alive: "
        f"closes={handle.closes}"
    )
    assert "sess-parent" not in runtime._session_queues
    ro.release_runtime_tenancy(child_turn)


@pytest.mark.asyncio
async def test_an_admitted_parent_kill_sends_no_close() -> None:
    """With nobody else on the process the recycle is the kill alone, as before."""
    runtime = _Runtime(pid=7102)
    parent, handle = _parent(runtime)

    await parent.shutdown()

    assert runtime.kills and handle.closes == []


@pytest.mark.asyncio
async def test_a_kiro_cli_parent_kill_refused_by_a_child_is_unchanged() -> None:
    """Only KAS fences a session by process; the kiro-cli recycle is left as it was."""
    runtime = _Runtime(pid=7103, backend=ACP_BACKEND_KIRO)
    parent, handle = _parent(runtime)
    child_turn = ro.claim_runtime_tenancy(runtime, holder="subagent:x", session_key="subagent:x")

    await parent.shutdown()

    assert not runtime.kills and handle.closes == []
    ro.release_runtime_tenancy(child_turn)


# -- R3: a recycle and a child that has not prompted yet --


@pytest.mark.asyncio
async def test_a_recycle_spares_a_shared_child_that_has_not_started_its_turn() -> None:
    """Bound to the process but still in its MCP wait: no turn defends it yet.

    Without a hold the recycle's kill takes the process from under it, and the run
    ends as a runtime death with no retry. The child holds the process from its
    bind, so the kill is refused, and the child's own end kills the orphan.
    """
    runtime = _Runtime(pid=7201)
    parent, parent_handle = _parent(runtime)
    info, child_handle = await _bind_child(runtime)

    await parent.shutdown()

    assert runtime.is_alive() and not runtime.kills, (
        "a recycle killed the process a bound child had not started its turn on: "
        f"kills={runtime.kills}"
    )
    assert parent_handle.closes == [True], "the parent's session must leave the process"

    await info._shared_provider.shutdown()

    assert child_handle.closes == [True], "the child closes its session, keeping its record"
    assert runtime.kills, "the last party off an orphaned process must end it"


@pytest.mark.asyncio
async def test_a_bound_child_leaving_a_live_parent_ends_nothing() -> None:
    """The ordinary end of a child: the parent still owns the process."""
    runtime = _Runtime(pid=7202)
    await ro.RUNTIME_OWNERSHIP.acquire(runtime, "dashboard:chat-1", AsyncMock(return_value=runtime))
    info, _ = await _bind_child(runtime)

    await info._shared_provider.shutdown()

    assert not runtime.kills
    assert ro.RUNTIME_TENANCY.claims_on(runtime) == 0, "the child's hold must go with it"


@pytest.mark.asyncio
async def test_a_stopped_child_no_longer_holds_the_process_after_its_shutdown() -> None:
    """A hard Stop or a stricter sandbox stops the children, then kills: the hold must not
    outlive the child's own teardown, or the kill would wait on a child already gone."""
    runtime = _Runtime(pid=7203)
    info, _ = await _bind_child(runtime)
    assert ro.authorize_runtime_kill(runtime, reason="probe", caller="t") is False

    await info._shared_provider.shutdown()

    assert ro.authorize_runtime_kill(runtime, reason="hard stop", caller="t") is True


@pytest.mark.asyncio
async def test_a_kiro_cli_child_takes_no_hold_at_bind() -> None:
    """The non-KAS shared arm is unchanged: a turn alone defends it."""
    runtime = _Runtime(pid=7204, backend=ACP_BACKEND_KIRO)
    await _bind_child(runtime)

    assert ro.RUNTIME_TENANCY.claims_on(runtime) == 0


@pytest.mark.asyncio
async def test_a_bind_on_a_process_being_torn_down_still_returns_its_provider() -> None:
    """A committed teardown refuses the hold; the bind must not raise past the
    published provider, or the start's dedicated fallback would drop a live handle."""
    runtime = _Runtime(pid=7205)
    assert ro.commit_runtime_teardown(runtime, ro.tenancy_epoch(runtime)) is True
    try:
        info, _ = await _bind_child(runtime)
    finally:
        ro.release_runtime_teardown(runtime)

    assert info._shared_provider is not None
    assert ro.RUNTIME_TENANCY.claims_on(runtime) == 0


# -- R1: spawn_continue while the child's close is still on the parent's process --


def _finished_shared_run(
    manager: SubagentManager, runtime: _Runtime, run_id: str = "orig"
) -> SubagentInfo:
    handle = _Handle(f"sess-{run_id}", runtime)
    provider = AcpSessionProvider(
        handle,  # type: ignore[arg-type]
        runtime,  # type: ignore[arg-type]
        session_key=f"subagent:{run_id}",
    )
    info = SubagentInfo(id=run_id, task="t", done=True)
    info._session_sharing = True
    info._shared_provider = provider
    manager._agents[run_id] = info
    return info


def _prelude(**_kw: object) -> dict:
    return {"task": "more", "keep": True, "conversation_key": "subagent:orig"}


def test_a_continuation_is_refused_while_the_childs_close_is_in_flight() -> None:
    """``done`` is set before the run's ``finally`` closes its shared session, so a
    continuation admitted on ``done`` alone loads the id on its own process while the
    parent's still holds it -- and KAS refuses that continuation's turn."""
    manager = _manager()
    runtime = _Runtime(pid=7301)
    _finished_shared_run(manager, runtime)
    manager._teardown_gates["orig"] = asyncio.Event()

    with (
        patch("kiro_crew.subagent.sel"),
        patch.object(manager, "spawn") as spawn,
        patch.object(manager, "_promote_conversation", return_value=object()),
    ):
        info = manager.continue_conversation("orig", "more work")

    spawn.assert_not_called()
    assert info is not None and str(info.error).startswith("conversation_busy"), info.error
    assert "still closing its session" in str(
        info.error
    ), "not told to use spawn_steer on a done run"
    assert manager.release_conversation("orig") == (
        False,
        "conversation_busy: run orig is still closing its session",
    ), "a release would delete the files the close is still holding"


@pytest.mark.asyncio
async def test_an_unconfirmed_close_holds_neither_a_release_nor_the_ttl_sweep() -> None:
    """Only the async continuation sends a timed-out close again, so the busy gate that
    ``release_conversation`` and the TTL sweep read must not count it: held there, the
    conversation would stay busy, and its idle clock refreshed, for the life of the
    parent's process."""
    manager = _manager()
    runtime = _Runtime(pid=7302)
    _finished_shared_run(manager, runtime)
    runtime._unconfirmed_teardowns.add("sess-orig")

    assert manager._conversation_busy("subagent:orig") is None
    ok, detail = manager.release_conversation("orig")
    assert not detail.startswith("conversation_busy"), detail

    manager._conversations["subagent:orig"] = 0.0
    release = AsyncMock(return_value=(True, "released"))
    with patch.object(manager, "release_conversation_async", release):
        await manager._sweep_conversations_async(now=1e9)

    release.assert_awaited_once_with("orig")


def test_a_continuation_after_the_close_is_admitted() -> None:
    """Nothing holds the session any more: the continuation goes ahead."""
    manager = _manager()
    runtime = _Runtime(pid=7303)
    _finished_shared_run(manager, runtime)
    assert manager._conversation_busy("subagent:orig") is None


@pytest.mark.asyncio
async def test_an_async_continuation_waits_for_the_childs_close_then_starts() -> None:
    """The ordinary race (a parent continuing the moment the completion lands) waits
    out the close instead of refusing, then dispatches once the id has left."""
    manager = _manager()
    runtime = _Runtime(pid=7304)
    _finished_shared_run(manager, runtime)
    gate = asyncio.Event()
    manager._teardown_gates["orig"] = gate
    spawn_async = AsyncMock(return_value=SubagentInfo(id="next", task="more"))

    with (
        patch.object(manager, "spawn_async", spawn_async),
        patch.object(manager, "_continue_prelude", side_effect=lambda *a, **kw: _prelude()),
    ):
        task = asyncio.create_task(manager.continue_conversation_async("orig", "more work"))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not spawn_async.called, "the continuation started while the close was in flight"
        gate.set()
        manager._teardown_gates.pop("orig", None)
        await await_until(task, what="the continuation after the close")

    spawn_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_async_continuation_gives_up_waiting_as_busy(monkeypatch) -> None:
    """A close that never settles ends the wait with the retryable refusal."""
    from kiro_crew.subagent_manager import continuation

    # A spent budget: the wait gives up at once, with no clock in the test.
    monkeypatch.setattr(continuation, "_SHARED_CLOSE_WAIT_SECS", 0, raising=False)
    manager = _manager()
    runtime = _Runtime(pid=7305)
    _finished_shared_run(manager, runtime)
    manager._teardown_gates["orig"] = asyncio.Event()
    spawn_async = AsyncMock()

    with patch.object(manager, "spawn_async", spawn_async), patch("kiro_crew.subagent.sel"):
        info = await await_until(
            asyncio.ensure_future(manager.continue_conversation_async("orig", "more work")),
            what="the continuation",
        )

    spawn_async.assert_not_called()
    assert info is not None and str(info.error).startswith("conversation_busy"), info.error


# -- nearby inputs: a cancel inside the new awaits, and a dead process --


class _CancelledClose(_Handle):
    async def destroy(self) -> None:
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_a_cancel_inside_the_parents_close_still_records_it_unconfirmed() -> None:
    """The restart path bounds a shutdown with ``wait_for``; a cancel landing in the
    close must not skip the unconfirmed-close record the retry drain reads."""
    runtime = _Runtime(pid=7401)
    handle = _CancelledClose("sess-parent", runtime)
    parent = AcpSessionProvider(
        handle,  # type: ignore[arg-type]
        runtime,  # type: ignore[arg-type]
        owns_runtime=True,
        session_key="dashboard:chat-1",
    )
    child_turn = ro.claim_runtime_tenancy(runtime, holder="subagent:x", session_key="subagent:x")

    with pytest.raises(asyncio.CancelledError):
        await parent.shutdown()

    assert parent._kas_shutdown_unconfirmed is True
    assert parent._kas_unconfirmed_session_id == "sess-parent"
    ro.release_runtime_tenancy(child_turn)


@pytest.mark.asyncio
async def test_a_cancel_inside_the_childs_close_still_lets_go_of_the_process() -> None:
    """A hold left behind by a cancelled teardown would refuse every later kill of the
    process for the life of the gateway."""
    runtime = _Runtime(pid=7402)
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=None)
    info = SubagentInfo(id="c", task="t", agent="kirocrew", parent_session_key="dashboard:chat-1")
    cfg = KiroCrewConfig(agent=AgentConfig())
    with patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)):
        await manager._bind_shared_handle(
            info, "subagent:c", runtime, _CancelledClose("sess-c", runtime)
        )
    assert ro.RUNTIME_TENANCY.claims_on(runtime) == 1

    with pytest.raises(asyncio.CancelledError):
        await info._shared_provider.shutdown()

    assert ro.RUNTIME_TENANCY.claims_on(runtime) == 0


@pytest.mark.asyncio
async def test_a_dead_process_is_sent_no_close_and_holds_no_continuation() -> None:
    """A crashed shared process: nothing is resident there any more."""
    runtime = _Runtime(pid=7403)
    parent, handle = _parent(runtime)
    child_turn = ro.claim_runtime_tenancy(runtime, holder="subagent:x", session_key="subagent:x")
    runtime._alive = False

    await parent.shutdown()

    assert handle.closes == []
    ro.release_runtime_tenancy(child_turn)


async def _continue_after_unconfirmed_close(
    runtime: _Runtime,
) -> tuple[SubagentInfo | None, AsyncMock]:
    """An async continuation of a finished run whose close *runtime* lists unconfirmed."""
    manager = _manager()
    _finished_shared_run(manager, runtime)
    runtime._unconfirmed_teardowns.add("sess-orig")
    spawn_async = AsyncMock(return_value=SubagentInfo(id="next", task="more"))
    with (
        patch.object(manager, "spawn_async", spawn_async),
        patch.object(manager, "_continue_prelude", side_effect=lambda *a, **kw: _prelude()),
        patch("kiro_crew.subagent.sel"),
    ):
        info = await await_until(
            asyncio.ensure_future(manager.continue_conversation_async("orig", "more work")),
            what="the continuation",
        )
    return info, spawn_async


def _refused_as_closing(info: SubagentInfo | None, spawn_async: AsyncMock) -> None:
    spawn_async.assert_not_called()
    assert info is not None and info.done
    assert str(info.error).startswith(
        "conversation_busy: run orig is still closing its session on the parent's process"
    ), info.error


@pytest.mark.parametrize("host_answers", [True, False])
@pytest.mark.asyncio
async def test_an_async_continuation_resends_an_unconfirmed_close_once(host_answers) -> None:
    """A close that timed out is never re-answered on its own, so without a second
    ask the conversation would stay busy for the whole life of the parent's process."""
    runtime = _Runtime(pid=7501)
    runtime.answers_close = host_answers

    info, spawn_async = await _continue_after_unconfirmed_close(runtime)

    assert runtime.closes_resent == 1
    if host_answers:
        spawn_async.assert_awaited_once()
    else:
        _refused_as_closing(info, spawn_async)


@pytest.mark.asyncio
async def test_a_dead_transport_with_the_child_still_running_keeps_the_continuation_refused() -> (
    None
):
    """A stdin stall marks the transport dead while the child still runs, so the
    session may still be resident: the re-sent close stays unconfirmed, and that answer,
    not the transport's liveness, decides."""
    runtime = _Runtime(pid=7502)
    runtime._alive = False
    runtime.answers_close = False

    info, spawn_async = await _continue_after_unconfirmed_close(runtime)

    assert runtime.closes_resent == 1
    _refused_as_closing(info, spawn_async)


@pytest.mark.asyncio
async def test_an_exited_process_whose_close_is_confirmed_admits_the_continuation() -> None:
    """``evict_session`` confirms a close on a process that exited with its whole tree
    confirmed dead while leaving the id listed: its answer admits the continuation."""
    runtime = _Runtime(pid=7503)
    runtime._alive = False
    runtime.process_tree_confirmed_dead = True
    runtime.evict_session = AsyncMock(return_value=True)  # type: ignore[method-assign]

    info, spawn_async = await _continue_after_unconfirmed_close(runtime)

    runtime.evict_session.assert_awaited_once_with("sess-orig")
    assert "sess-orig" in runtime._unconfirmed_teardowns
    spawn_async.assert_awaited_once()


class _GateEndingUnconfirmed:
    """A teardown gate whose wait runs out just as the run's close ends unconfirmed."""

    def __init__(self, runtime: _Runtime) -> None:
        self._runtime = runtime
        self._set = False

    def is_set(self) -> bool:
        return self._set

    async def wait(self) -> bool:
        self._runtime._unconfirmed_teardowns.add("sess-orig")
        self._set = True
        raise asyncio.TimeoutError


@pytest.mark.asyncio
async def test_a_close_ending_unconfirmed_as_the_wait_runs_out_still_refuses() -> None:
    """The teardown gate is set by the time the wait gives up, so the busy check
    passes: the unconfirmed close left behind must still be judged."""
    runtime = _Runtime(pid=7504)
    runtime.answers_close = False
    manager = _manager()
    _finished_shared_run(manager, runtime)
    manager._teardown_gates["orig"] = _GateEndingUnconfirmed(runtime)  # type: ignore[assignment]
    spawn_async = AsyncMock(return_value=SubagentInfo(id="next", task="more"))

    with (
        patch.object(manager, "spawn_async", spawn_async),
        patch.object(manager, "_continue_prelude", side_effect=lambda *a, **kw: _prelude()),
        patch("kiro_crew.subagent.sel"),
    ):
        info = await await_until(
            asyncio.ensure_future(manager.continue_conversation_async("orig", "more work")),
            what="the continuation",
        )

    assert runtime.closes_resent == 1
    _refused_as_closing(info, spawn_async)
