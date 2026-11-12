"""A stricter ``agent.sandbox`` recycles a session spawned under a looser tier.

A process keeps the tier it was spawned under, so each holder of a live agent
process meets the new tier at its next turn: a claim, a channel member's next
message, and a shared runtime's next reuse. Pins session.md, "A stricter
``agent.sandbox`` recycles a session at its next turn".
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_helpers import _raw_sid

from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN
from kiro_crew.channel import Channel, run_channel_agent
from kiro_crew.config import KiroCrewConfig
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import async_wait_until, until_parked

_KEY = "dashboard:chat-1"
#: Lost-run ceiling for a task the test itself unblocks; it settles in milliseconds,
#: so one that never does fails here by name, well inside the suite's ``--timeout``.
_LOST_RUN_SECS = 30.0


def _manager(tier: str) -> tuple[SessionManager, list[MagicMock], list[str]]:
    """A manager whose factory spawns at the configured tier, as the real one does."""
    made: list[MagicMock] = []
    events: list[str] = []
    cfg = KiroCrewConfig()
    cfg.agent.sandbox = tier

    def factory(*args: object, **kwargs: object) -> MagicMock:
        provider = MagicMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock(side_effect=lambda: events.append("shutdown"))
        provider.is_alive = MagicMock(return_value=True)
        provider.is_process_alive = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        provider.runtime_abort_target = MagicMock(return_value=None)
        provider.cwd = "/ws"
        provider.memory_mode = kwargs.get("memory_mode", "persistent")
        provider.spawn_identity = ""
        provider._runtime = None
        provider.sandbox_mode = mgr._cfg.agent.sandbox
        made.append(provider)
        return provider

    mgr = SessionManager(cfg, provider_factory=factory)
    return mgr, made, events


@pytest.mark.parametrize(
    ("spawned", "current", "tightened"),
    [
        ("off", "auto", True),
        ("auto", "strict", True),
        ("off", "strict", True),
        ("auto", "auto", False),
        ("strict", "auto", False),
        ("auto", "off", False),
        (None, "strict", False),
    ],
)
def test_tightened_follows_the_governance_scale(spawned, current, tightened):
    from kiro_crew.session_allocation_parts.companions import sandbox_tier_tightened

    assert sandbox_tier_tightened(spawned, current) is tightened


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_every_harness_reports_the_tier_it_spawns_under(backend):
    """The claim reads the provider, never the harness, so every backend must answer."""
    assert AcpProvider(acp_backend=backend, sandbox_mode="off").sandbox_mode == "off"


def test_the_runtime_path_reports_its_runtime_tier():
    """On the kiro path the provider's client becomes a session on an ``AcpRuntime``.

    A task step's tenant session on a runtime it does not own reports that runtime's.
    """
    runtime = SimpleNamespace(sandbox_mode="off")
    assert AcpSessionProvider(MagicMock(), runtime, owns_runtime=True).sandbox_mode == "off"
    assert AcpSessionProvider(MagicMock(), runtime).sandbox_mode == "off"


@pytest.mark.asyncio
async def test_a_stricter_tier_recycles_the_session_at_its_next_claim():
    mgr, made, events = _manager("off")
    first, _, _ = await mgr.get_or_create(_KEY)
    mgr.release(_KEY)
    follow_up = ("1", "sent while the last turn ran", {})
    mgr._allocation_boundary()._sessions[_KEY].queue.append(follow_up)
    mgr._cfg.agent.sandbox = "auto"

    ended = AsyncMock()
    kill = MagicMock(side_effect=lambda provider: events.append("tree kill"))
    with (
        patch("kiro_crew.session._sync_kill_provider", kill),
        patch("kiro_crew.session_allocation.record_session_ended", ended),
    ):
        second, _, _ = await mgr.get_or_create(_KEY)

    assert second is not first and second.sandbox_mode == "auto"
    kill.assert_called_once_with(first)
    assert events == ["tree kill", "shutdown"], "the tree must die before the shutdown"
    assert ended.await_args.kwargs["end_reason"] == "retired"
    assert list(mgr._allocation_boundary()._sessions[_KEY].queue) == [follow_up]
    mgr.release(_KEY)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_temporary_session_is_shut_down_before_any_tree_kill():
    """Its shutdown deletes the harness record, on KAS through the live engine."""
    mgr, _, events = _manager("off")
    execution = SimpleNamespace(memory_mode="temporary", member_id=None)
    kill = MagicMock(side_effect=lambda provider: events.append("tree kill"))
    with (
        patch("kiro_crew.execution_context.read_session_execution", lambda _key: execution),
        patch("kiro_crew.session._sync_kill_provider", kill),
    ):
        first, _, _ = await mgr.get_or_create(_KEY)
        mgr.release(_KEY)
        mgr._cfg.agent.sandbox = "auto"
        second, _, _ = await mgr.get_or_create(_KEY)

    assert second is not first and second.sandbox_mode == "auto"
    assert events == ["shutdown"], "a tree killed first leaves no engine to delete the record"
    mgr.release(_KEY)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_looser_tier_keeps_the_running_session():
    mgr, made, _ = _manager("strict")
    first, _, _ = await mgr.get_or_create(_KEY)
    mgr.release(_KEY)
    mgr._cfg.agent.sandbox = "auto"

    kill = MagicMock()
    with patch("kiro_crew.session._sync_kill_provider", kill):
        second, _, _ = await mgr.get_or_create(_KEY)

    assert second is first
    kill.assert_not_called()
    mgr.release(_KEY)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_turn_in_flight_finishes_on_the_old_process():
    mgr, made, events = _manager("off")
    first, _, _ = await mgr.get_or_create(_KEY)  # this claim is the running turn
    mgr._cfg.agent.sandbox = "auto"

    kill = MagicMock(side_effect=lambda provider: events.append("tree kill"))
    with patch("kiro_crew.session._sync_kill_provider", kill):
        waiter = asyncio.create_task(mgr.get_or_create(_KEY))
        await until_parked(mgr._allocation_boundary()._sessions[_KEY].semaphore)
        assert events == [], "the recycle did not wait for the running turn"
        mgr.release(_KEY)
        second, _, _ = await asyncio.wait_for(waiter, _LOST_RUN_SECS)

    assert second is not first
    kill.assert_called_once_with(first)
    mgr.release(_KEY)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_permit_handed_to_a_parked_claim_does_not_keep_the_looser_session():
    """A release hands the permit to a parked claim before that claim runs or names itself.

    That is no running turn: the refused claim's eviction must go ahead, and the parked
    claim then finds the session gone and claims the successor.
    """
    mgr, _, _ = _manager("off")
    first, _, _ = await mgr.get_or_create(_KEY)  # this claim is the running turn
    mgr._cfg.agent.sandbox = "auto"
    allocation = mgr._allocation_boundary()
    session = allocation._sessions[_KEY]

    with patch("kiro_crew.session._sync_kill_provider", MagicMock()) as kill:
        parked = asyncio.create_task(mgr.get_or_create(_KEY))
        await until_parked(session.semaphore)
        mgr.release(_KEY)  # what a refused claim's release does, with no await before the evict
        assert session.semaphore.locked() and session.turn_owner is None
        await allocation._evict_stale_session(_KEY, session)
        assert allocation._sessions.get(_KEY) is not session, "the hand-off kept the session"
        second, _, _ = await asyncio.wait_for(parked, _LOST_RUN_SECS)

    kill.assert_called_once_with(first)
    assert second is not first and second.sandbox_mode == "auto"
    mgr.release(_KEY)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_an_agentless_claim_rebuilds_the_recycled_session_as_its_own_agent():
    """A subagent completion claims with no agent; the successor keeps the session's."""
    mgr, _, _ = _manager("off")
    spawn = mgr._provider_factory
    asked: list[tuple[object, object]] = []

    def factory(*args: object, **kwargs: object) -> MagicMock:
        asked.append((kwargs.get("agent"), kwargs.get("model_override")))
        return spawn(*args, **kwargs)

    mgr._provider_factory = factory
    first, _, _ = await mgr.get_or_create(_KEY, agent="reviewer", model="model-a")
    mgr.release(_KEY)
    mgr._cfg.agent.sandbox = "auto"

    with patch("kiro_crew.session._sync_kill_provider", MagicMock()):
        second, _, _ = await mgr.get_or_create(_KEY)

    assert second is not first and second.sandbox_mode == "auto"
    assert asked == [("reviewer", "model-a")] * 2, "the successor took the claim's defaults"
    mgr.release(_KEY)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_claim_that_names_a_model_keeps_it_through_the_recycle():
    """A chat switched to another model claims with it; the ended session's model loses."""
    mgr, _, _ = _manager("off")
    spawn = mgr._provider_factory
    asked: list[tuple[object, object]] = []

    def factory(*args: object, **kwargs: object) -> MagicMock:
        asked.append((kwargs.get("agent"), kwargs.get("model_override")))
        return spawn(*args, **kwargs)

    mgr._provider_factory = factory
    await mgr.get_or_create(_KEY, agent="reviewer", model="model-a")
    mgr.release(_KEY)
    mgr._cfg.agent.sandbox = "auto"

    with patch("kiro_crew.session._sync_kill_provider", MagicMock()):
        await mgr.get_or_create(_KEY, model="model-b")

    assert asked == [("reviewer", "model-a"), ("reviewer", "model-b")], "the claim's model was lost"
    mgr.release(_KEY)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_an_app_session_is_recycled_under_its_own_app():
    """An app's session carries ``owning_app`` in its identity; the re-entry passes it once."""
    mgr, _, _ = _manager("off")
    first, _, _ = await mgr.get_or_create(_KEY, owning_app="notes")
    first.owning_app = "notes"  # what a cold start stamps on an ACP provider
    mgr.release(_KEY)
    mgr._cfg.agent.sandbox = "auto"

    with patch("kiro_crew.session._sync_kill_provider", MagicMock()):
        second, _, _ = await mgr.get_or_create(_KEY, owning_app="notes")

    assert second is not first and second.sandbox_mode == "auto"
    mgr.release(_KEY)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_claim_held_for_life_is_recycled_on_request():
    """What a channel member calls between messages, against the real manager."""
    mgr, made, _ = _manager("off")
    key = "channel:c1:a1"
    first, _, _ = await mgr.get_or_create(key)
    mgr.mark_lifecycle_lease(key)
    mgr._session_map.set(key, "sid-1")
    assert await mgr.recycle_if_sandbox_tightened(key) is False

    mgr._cfg.agent.sandbox = "strict"
    with patch("kiro_crew.session._sync_kill_provider", MagicMock()) as kill:
        assert await mgr.recycle_if_sandbox_tightened(key) is True

    kill.assert_called_once_with(first)
    assert mgr.get_provider(key) is None
    assert _raw_sid(mgr, key) == "sid-1", "the recycle dropped the conversation"
    second, _, _ = await mgr.get_or_create(key)
    assert second.sandbox_mode == "strict"
    mgr.release(key)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_turn_that_took_the_permit_after_the_claim_is_not_killed():
    """``try_acquire`` needs no claim, so it can take the permit the refused claim gave back."""
    mgr, made, _ = _manager("off")
    first, _, _ = await mgr.get_or_create(_KEY)
    mgr.release(_KEY)
    mgr._cfg.agent.sandbox = "auto"
    allocation = mgr._allocation_boundary()
    session = allocation._sessions[_KEY]
    assert await mgr.try_acquire(_KEY)  # a /compact, say, between the claim and the pop

    with patch("kiro_crew.session._sync_kill_provider", MagicMock()) as kill:
        await allocation._evict_stale_session(_KEY, session)
    assert allocation._sessions[_KEY] is session
    kill.assert_not_called()
    mgr.release(_KEY)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_an_identity_retirement_keeps_its_own_eviction():
    """A looser session also flagged for a wrong account is evicted as before, busy or not."""
    mgr, made, _ = _manager("off")
    first, _, _ = await mgr.get_or_create(_KEY)
    mgr.release(_KEY)
    mgr._cfg.agent.sandbox = "auto"
    allocation = mgr._allocation_boundary()
    session = allocation._sessions[_KEY]
    assert await mgr.try_acquire(_KEY)
    session.retire_on_identity_change = True

    ended = AsyncMock()
    with (
        patch("kiro_crew.session._sync_kill_provider", MagicMock()) as kill,
        patch("kiro_crew.session_allocation.record_session_ended", ended),
    ):
        await allocation._evict_stale_session(_KEY, session)

    assert _KEY not in allocation._sessions
    kill.assert_not_called()
    assert ended.await_args.kwargs["end_reason"] == "evicted"
    await mgr.close_all()


def _text_client(text: str, seen: list[str]) -> SimpleNamespace:
    async def _stream(message, **_options):
        seen.append(message)
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=text)
        yield SimpleNamespace(kind=EVENT_COMPLETE)

    return SimpleNamespace(stream=_stream)


class _MemberSessions:
    """The manager surface a channel member uses; ``tightened`` stands in for the tier."""

    def __init__(self, clients: list[SimpleNamespace]) -> None:
        self._sessions: dict[str, SimpleNamespace] = {}
        self._clients = clients
        self.tightened = False
        self.recycle_checks = 0

    async def get_or_create(self, key, agent=None, approval_policy="", **kwargs):
        client = self._clients.pop(0)
        self._sessions[key] = SimpleNamespace(provider=client)
        return client, True, False

    async def run_identity_check(self):
        return None

    def is_retiring(self, key):
        return False

    async def recycle_if_sandbox_tightened(self, key):
        self.recycle_checks += 1
        if not self.tightened:
            return False
        self.tightened = False
        self._sessions.pop(key, None)
        return True

    def get_provider(self, key):
        entry = self._sessions.get(key)
        return entry.provider if entry is not None else None

    def mark_lifecycle_lease(self, key):
        return True

    def set_lifecycle_turn_active(self, key, active):
        return True

    def release(self, key, **kwargs):
        pass


@pytest.mark.asyncio
async def test_a_channel_member_recycles_before_its_next_message():
    ch = Channel(id="c1", topic="review")
    agent = ch.add_agent(role="dev", agent_name="dev")
    assert agent is not None
    old_seen: list[str] = []
    new_seen: list[str] = []
    sessions = _MemberSessions([_text_client("old", old_seen), _text_client("new", new_seen)])

    task = asyncio.create_task(run_channel_agent(agent, ch, sessions))
    try:
        await async_wait_until(lambda: agent.state == "listening")
        sessions.tightened = True
        await ch.post("human", "please review this")
        await async_wait_until(lambda: bool(new_seen), describe=lambda: old_seen)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, _LOST_RUN_SECS)

    assert sessions.recycle_checks == 1
    assert not old_seen, "the message ran on the process spawned under the looser tier"


@pytest.mark.asyncio
async def test_a_looser_companion_runtime_is_replaced():
    """With no parent tier to mirror, the replacement takes the stricter configured one."""
    cfg = KiroCrewConfig()
    cfg.agent.sandbox = "strict"
    mgr = SessionManager(cfg)
    parent = "taskrunner:run1:runtime"
    old = MagicMock(sandbox_mode="auto")
    old.is_alive = MagicMock(return_value=True)
    old.kill = AsyncMock()
    mgr._subagent_runtimes[parent] = old

    class _FakeRuntime:
        def __init__(self, agent: str | None = None, **kwargs: object) -> None:
            self.sandbox_mode = kwargs.get("sandbox_mode", "auto")

        async def spawn(self, start_priority=None) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    with patch("kiro_crew.session._load_bg_runtime_types", return_value=(_FakeRuntime, OSError)):
        runtime = await mgr.get_subagent_runtime(parent)
        again = await mgr.get_subagent_runtime(parent)

    assert runtime is not old and runtime.sandbox_mode == "strict"
    assert again is runtime, "the replacement must not be replaced again"
    old.kill.assert_awaited_once()
    assert old.kill.await_args.kwargs["expected"] is True


@pytest.mark.asyncio
async def test_a_looser_run_runtime_is_replaced():
    cfg = KiroCrewConfig()
    cfg.agent.sandbox = "strict"
    fresh = MagicMock(sandbox_mode="strict")
    fresh.is_alive = MagicMock(return_value=True)
    fresh.create_session = AsyncMock(return_value=MagicMock(session_id="step"))
    fresh.terminate_session = AsyncMock()
    bootstrap = AsyncMock()
    bootstrap._client = MagicMock(_runtime=fresh, _handle=MagicMock(session_id="boot"))
    mgr = SessionManager(cfg, provider_factory=MagicMock(return_value=bootstrap))
    parent = "taskrunner:run1:runtime"
    old = MagicMock(sandbox_mode="auto")
    old.is_alive = MagicMock(return_value=True)
    old.kill = AsyncMock()
    mgr._subagent_runtimes[parent] = old

    runtime = await mgr._get_or_bootstrap_run_runtime(parent, agent="kirocrew")

    assert runtime is fresh
    old.kill.assert_awaited_once()
    assert old.kill.await_args.kwargs["expected"] is True
    assert mgr._subagent_runtimes[parent] is fresh


@pytest.mark.asyncio
async def test_a_retried_task_step_moves_to_a_stricter_run_runtime():
    """A step's session is a tenant on the run's runtime, so it reports that runtime's tier.

    Its claim refuses it and ends that one session, never the shared process, and the
    retry lands on the replacement runtime the owner's path spawns.
    """
    cfg = KiroCrewConfig()
    cfg.agent.sandbox = "auto"
    runtimes: list[MagicMock] = []
    handles: list[MagicMock] = []

    def _handle(**kwargs: object) -> MagicMock:
        handle = MagicMock(
            session_id=f"step-{len(handles)}",
            is_turn_active=False,
            memory_mode=kwargs.get("memory_mode", "persistent"),
        )
        handle.destroy = AsyncMock()
        handle.kas_registered_agents = []
        handle.kas_launch_snapshots = []
        handles.append(handle)
        return handle

    def factory(*_args: object, **_kwargs: object) -> AsyncMock:
        runtime = MagicMock(sandbox_mode=mgr._cfg.agent.sandbox)
        runtime.is_alive = MagicMock(return_value=True)
        runtime.create_session = AsyncMock(side_effect=_handle)
        runtime.terminate_session = AsyncMock()
        runtime.kill = AsyncMock()
        runtimes.append(runtime)
        bootstrap = AsyncMock()
        bootstrap._client = MagicMock(_runtime=runtime, _handle=MagicMock(session_id="boot"))
        return bootstrap

    mgr = SessionManager(cfg, provider_factory=factory)
    parent, key = "taskrunner:run1:runtime", "taskrunner:run1:task0"
    first, _, _ = await mgr.open_task_session(parent, key, agent="kirocrew")
    mgr.release(key)
    mgr._cfg.agent.sandbox = "strict"

    with patch("kiro_crew.session._sync_kill_provider", MagicMock()):
        second, is_new, _ = await asyncio.wait_for(
            mgr.open_task_session(parent, key, agent="kirocrew"), _LOST_RUN_SECS
        )

    assert second is not first and is_new
    assert second.sandbox_mode == "strict"
    handles[0].destroy.assert_awaited_once()
    # Killed once, by the runtime's own replacement path, not by the tenant's eviction.
    runtimes[0].kill.assert_awaited_once()
    assert "stricter" in runtimes[0].kill.await_args.kwargs["reason"]
    assert mgr._subagent_runtimes[parent] is runtimes[1]
    mgr.release(key)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_background_handle_issued_before_a_tightening_refuses_the_looser_turn():
    """A handle passes its looser check once; a turn queued on it meets the tier again."""
    from kiro_crew.session import BACKGROUND_KEY

    mgr, _, _ = _manager("off")
    first, _, _ = await mgr.get_or_create(BACKGROUND_KEY)
    mgr.release(BACKGROUND_KEY)
    semaphore = mgr._sessions[BACKGROUND_KEY].semaphore
    may_finish = asyncio.Event()
    seen: list[str] = []

    async def _stream(message, **_options):
        seen.append(message)
        await may_finish.wait()
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="old")
        yield SimpleNamespace(kind=EVENT_COMPLETE)

    first.stream = _stream
    with patch.object(mgr, "_bg_backend_supports_runtime", return_value=False):
        running = await asyncio.wait_for(mgr.get_bg_session(), _LOST_RUN_SECS)
        queued = await asyncio.wait_for(mgr.get_bg_session(), _LOST_RUN_SECS)
    turn = asyncio.create_task(_texts(running))
    await async_wait_until(lambda: bool(seen))
    late = asyncio.create_task(_texts(queued))
    await until_parked(semaphore)
    mgr._cfg.agent.sandbox = "strict"
    may_finish.set()

    try:
        assert await asyncio.wait_for(turn, _LOST_RUN_SECS) == ["old"]
        with pytest.raises(RuntimeError, match="background session unavailable"):
            await asyncio.wait_for(late, _LOST_RUN_SECS)
    finally:
        await running.destroy()
        await queued.destroy()

    assert len(seen) == 1, "the queued turn streamed on the process spawned under the looser tier"
    assert not semaphore.locked(), "the refused turn kept the permit"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_background_session_recycles_after_its_next_turn():
    """Its turns take the permit directly, so the recycle after each turn replaces it."""
    from kiro_crew.session import BACKGROUND_KEY

    mgr, made, _ = _manager("off")
    first, _, _ = await mgr.get_or_create(BACKGROUND_KEY)
    mgr.release(BACKGROUND_KEY)
    first.context_usage_pct = MagicMock(return_value=0.0)
    mgr._cfg.agent.sandbox = "auto"

    await mgr.recycle_background()

    assert mgr._sessions[BACKGROUND_KEY].provider is not first
    assert mgr._sessions[BACKGROUND_KEY].provider.sandbox_mode == "auto"
    first.shutdown.assert_awaited_once()
    await mgr.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old", "new", "retired"), [("auto", "strict", True), ("strict", "auto", False)]
)
async def test_the_shared_background_runtime_is_retired_on_a_tightening(old, new, retired):
    from _hot_reload_helpers import cfg_with
    from _hot_reload_helpers import change as _change

    mgr, _, _ = _manager(old)
    mgr.refresh_defaults = AsyncMock()  # type: ignore[method-assign]
    retire = AsyncMock(return_value=True)
    mgr._background_runtime.retire_for_agent_env_change = retire  # type: ignore[method-assign]

    await mgr._on_config_change(
        _change(cfg_with("agent", sandbox=new), "agent.sandbox", old=cfg_with("agent", sandbox=old))
    )

    assert retire.await_count == int(retired)


class _PoolSessions:
    """What a workflow pool worker asks of the manager across a reuse."""

    def __init__(self) -> None:
        self.cold_starts = 0

    async def get_or_create(self, key, **kwargs):
        self.cold_starts += 1
        provider = MagicMock()
        provider.new_conversation = AsyncMock()
        return provider, True, False

    async def end_children_for(self, key):
        return None

    async def run_identity_check(self):
        return None

    def is_retiring(self, key):
        return False

    async def recycle_if_sandbox_tightened(self, key):
        return True


@pytest.mark.asyncio
async def test_a_workflow_pool_worker_restarts_at_its_next_task():
    from kiro_crew.workflows.agent_pool import _WorkflowSessionWorker

    sessions = _PoolSessions()
    worker = _WorkflowSessionWorker(
        sessions, key="wf-pool:run1:0", agent="kirocrew", model=None, cwd=None
    )
    await worker.start()
    old = worker._provider

    await worker.reset()

    assert sessions.cold_starts == 2 and worker._provider is not old
    old.new_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cancelled_eviction_still_tears_the_process_down():
    """Past the pop no registry entry reaches the process, so a closed chat must not strand it."""
    mgr, _, events = _manager("off")
    first, _, _ = await mgr.get_or_create(_KEY)
    mgr.release(_KEY)
    mgr._cfg.agent.sandbox = "auto"
    unlinking = threading.Event()
    unlink_may_finish = threading.Event()

    def _slow_unlink(session: object) -> None:
        unlinking.set()
        unlink_may_finish.wait(_LOST_RUN_SECS)

    released = AsyncMock()
    kill = MagicMock(side_effect=lambda provider: events.append("tree kill"))
    with (
        patch("kiro_crew.session._unlink_session_queue", _slow_unlink),
        patch("kiro_crew.session._sync_kill_provider", kill),
        patch("kiro_crew.session_allocation.release_session_lease", released),
    ):
        claim = asyncio.create_task(mgr.get_or_create(_KEY))
        try:
            await async_wait_until(unlinking.is_set)
            claim.cancel()
        finally:
            unlink_may_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(claim, _LOST_RUN_SECS)

    released.assert_awaited_once_with(first)
    kill.assert_called_once_with(first)
    assert events == ["tree kill", "shutdown"], "the cancellation cut the teardown short"
    await mgr.close_all()


async def _texts(handle: object) -> list[str]:
    return [event.text async for event in handle.prompt("hi") if event.kind == EVENT_TEXT_CHUNK]


@pytest.mark.asyncio
@pytest.mark.parametrize("alive", [True, False], ids=["live", "dead"])
async def test_the_provider_backed_background_turn_runs_under_the_stricter_tier(alive):
    """A backend with no shared runtime hands out the idle ``_bg`` entry itself.

    No turn streams on the looser process, live or dead: while no replacement starts
    the handle is refused, and once one does the turn runs on it.
    """
    from kiro_crew.session import BACKGROUND_KEY

    mgr, _, _ = _manager("off")
    first, _, _ = await mgr.get_or_create(BACKGROUND_KEY)
    mgr.release(BACKGROUND_KEY)
    first.context_usage_pct = MagicMock(return_value=0.0)
    first.is_process_alive.return_value = alive
    mgr._cfg.agent.sandbox = "strict"
    seen: list[str] = []
    first.stream = _text_client("old", seen).stream
    spawn = mgr._provider_factory

    def _streaming_factory(*args: object, **kwargs: object) -> MagicMock:
        provider = spawn(*args, **kwargs)
        provider.stream = _text_client("new", seen).stream
        return provider

    with (
        patch.object(mgr, "_bg_backend_supports_runtime", return_value=False),
        patch("kiro_crew.session._sync_kill_provider", MagicMock()),
    ):
        mgr._provider_factory = MagicMock(side_effect=OSError("no spawn"))
        with pytest.raises(RuntimeError, match="background session unavailable"):
            await asyncio.wait_for(mgr.get_bg_session(), _LOST_RUN_SECS)
        assert BACKGROUND_KEY not in mgr._sessions, "the looser entry outlived the refusal"

        mgr._provider_factory = _streaming_factory
        handle = await asyncio.wait_for(mgr.get_bg_session(), _LOST_RUN_SECS)
    try:
        texts = await asyncio.wait_for(_texts(handle), _LOST_RUN_SECS)
    finally:
        await handle.destroy()

    assert texts == ["new"], "the turn streamed on the process spawned under the looser tier"
    assert mgr._sessions[BACKGROUND_KEY].provider.sandbox_mode == "strict"
    first.shutdown.assert_awaited_once()
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_recycled_shared_chat_founds_its_own_process_under_a_floor():
    """Under a ``strict`` floor, ``auto`` and ``strict`` are one effective tier.

    Two chats share a runtime spawned at raw ``auto``; the operator then sets
    ``strict``. The claim compares raw tiers, so it recycles the chat, and the other
    chat's lease keeps the runtime alive. A placement keyed on the effective tier
    alone put the successor back on it, so every later turn recycled again.
    """
    from chat_runtime_sharing_helpers import confirming, make_placement

    import kiro_crew.providers.chat_share as share_mod
    from kiro_crew.runtime_ownership import RuntimeOwnership
    from kiro_crew.sandbox import effective_sandbox_mode

    table = RuntimeOwnership()
    spawned: list[SimpleNamespace] = []
    cfg = KiroCrewConfig()
    cfg.agent.sandbox = "auto"

    def factory(key: str, **kwargs: object) -> MagicMock:
        provider = MagicMock()
        provider.is_alive = MagicMock(return_value=True)
        provider.is_process_alive = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        provider.runtime_abort_target = MagicMock(return_value=None)
        provider.cwd = "/ws"
        provider.memory_mode = "persistent"
        provider.spawn_identity = ""
        provider._runtime_lease = None
        provider.sandbox_mode = None

        async def start() -> None:
            raw = mgr._cfg.agent.sandbox
            tier = effective_sandbox_mode(raw)
            placement = make_placement(session_key=key, sandbox_mode=raw, sandbox_tier=tier)
            placement._read_confirmation = confirming(("gen-1", "acct-A", tier, False, True))

            async def spawn() -> SimpleNamespace:
                runtime = SimpleNamespace(pid=7000 + len(spawned), sandbox_mode=raw)
                runtime.is_alive = lambda: True
                spawned.append(runtime)
                return runtime

            runtime = await placement.place(spawn)
            provider._runtime_lease = placement.lease
            provider.sandbox_mode = runtime.sandbox_mode
            provider.runtime = runtime

        async def release() -> None:
            lease, provider._runtime_lease = provider._runtime_lease, None
            if lease is not None:
                await table.release(lease)

        provider.start = AsyncMock(side_effect=start)
        provider.acquire_runtime_lease = AsyncMock()  # the placement already holds it
        provider.release_runtime_lease = AsyncMock(side_effect=release)
        provider.shutdown = AsyncMock(side_effect=release)
        return provider

    mgr = SessionManager(cfg, provider_factory=factory)
    chat_a, chat_b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
    with (
        patch.object(share_mod, "RUNTIME_OWNERSHIP", table),
        patch("kiro_crew.sandbox._governance_sandbox_floor", return_value="strict"),
        patch("kiro_crew.session._sync_kill_provider", MagicMock()),
    ):
        for key in (chat_a, chat_b):
            await mgr.get_or_create(key)
            mgr.release(key)
        a_first = mgr._allocation_boundary()._sessions[chat_a].provider
        b_first = mgr._allocation_boundary()._sessions[chat_b].provider
        assert a_first.runtime is b_first.runtime, "the two chats did not share a runtime"

        mgr._cfg.agent.sandbox = "strict"
        a_second, _, _ = await mgr.get_or_create(chat_a)
        mgr.release(chat_a)
        a_third, _, _ = await mgr.get_or_create(chat_a)
        mgr.release(chat_a)

        assert a_second is not a_first
        assert a_second.runtime is not b_first.runtime, "the successor rejoined the looser runtime"
        assert a_second.sandbox_mode == "strict"
        assert a_third is a_second, "the next turn recycled the chat again"
        await mgr.close_all()
