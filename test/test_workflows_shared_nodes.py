"""Workflow steps on their launching KAS chat's own process (``workflows/shared_nodes.py``).

A step of a run launched from a KAS chat whose process may host sub-agents opens
its session on that process; every case that cannot share safely keeps the
dedicated ``get_or_create`` start it always had.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.workflows import shared_nodes
from kiro_crew.workflows.agent_exec import build_agent_fn
from kiro_crew.workflows.agent_pool import _WorkflowSessionWorker, build_pooled_agent_fn
from kiro_crew.workflows.shared_nodes import (
    KAS_SHARED_NODES_PER_PARENT,
    acquire_step_session,
    shared_host,
)

PARENT = "dashboard:chat-1"

#: Bound on every wait in this file, so a regression fails instead of hanging.
WAIT_S = 60.0


async def _first(event: asyncio.Event, task: "asyncio.Future[Any]") -> None:
    """Return when *event* is set or *task* finishes, whichever is first."""
    waiter = asyncio.ensure_future(event.wait())
    try:
        await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()


WORK_DIR = "/home/u/.kiro/crew/workspace"


def _cfg(*, sharing: bool = True, effort: str = "", backend: str = "kas") -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agent.session_sharing = sharing
    cfg.agent.acp_backend = backend
    if effort:
        cfg.resolve_session_effort = lambda agent, crew_agent=None: effort  # type: ignore[method-assign]
    return cfg


@pytest.fixture
def cfg(monkeypatch):
    holder = {"cfg": _cfg()}
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda cls: holder["cfg"])
    )
    return holder


def _runtime(backend: str = ACP_BACKEND_KAS) -> MagicMock:
    runtime = MagicMock(acp_backend=backend, pid=UNALLOCATABLE_PID, _work_dir=WORK_DIR)
    return runtime


class _Provider(SimpleNamespace):
    def set_delete_delay(self, seconds: float) -> None:
        self.delete_delay = seconds


class _Sessions:
    """The SessionManager surface the steps reach, with a KAS chat at PARENT."""

    _pool_agent = "reviewer"  # a pool agent other than the dedicated default

    def __init__(self, runtime: Any = None, *, eligible: bool = True, aws_differs: bool = False):
        self.runtime = runtime if runtime is not None else _runtime()
        self.eligible = eligible
        self.aws_differs = aws_differs
        self.chat = SimpleNamespace(_client=SimpleNamespace(_runtime=self.runtime))
        self.opened: list[tuple[str, str, dict]] = []
        self.created: list[tuple[str, dict]] = []
        self.released: list[tuple[str, bool]] = []
        self.destroyed: list[str] = []
        self.open_raises: BaseException | None = None
        self.open_returns_dedicated = False

    def get_provider(self, key: str) -> Any:
        return self.chat if key == PARENT else None

    def is_session_sharing_eligible(self, key: str) -> bool:
        return self.eligible and key == PARENT

    def aws_profile_differs_from_parent(self, key: str, child: Any) -> bool:
        return self.aws_differs

    async def open_task_session(self, parent: str, key: str, **kwargs: Any):
        self.opened.append((parent, key, kwargs))
        if self.open_raises is not None:
            raise self.open_raises
        runtime = MagicMock() if self.open_returns_dedicated else kwargs["runtime"]
        provider = _Provider(_runtime=runtime, _handle=MagicMock(), tag="shared")
        return provider, True, False

    async def get_or_create(self, key: str, **kwargs: Any):
        self.created.append((key, kwargs))
        return _Provider(tag="dedicated"), True, False

    def release(self, key: str, cleanup: bool = True) -> None:
        self.released.append((key, cleanup))

    async def destroy(self, key: str) -> None:
        self.destroyed.append(key)


def _host(sessions: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("agent", None)
    kwargs.setdefault("cwd", None)
    kwargs.setdefault("cfg", KiroCrewConfig.load())
    return shared_host(sessions, PARENT, **kwargs)


class TestSharedHost:
    def test_a_qualifying_kas_chat_hosts_the_step(self, cfg):
        sessions = _Sessions()
        assert _host(sessions) is sessions.runtime

    def test_session_sharing_off_keeps_every_step_dedicated(self, cfg):
        cfg["cfg"] = _cfg(sharing=False)
        assert _host(_Sessions()) is None

    def test_a_chat_process_that_cannot_host_sub_agents_keeps_the_step_dedicated(self, cfg):
        # Old engine, no session/close, machine mode, looser sandbox, chat lease:
        # each answers is_session_sharing_eligible False (runtime_hosts_subagents).
        assert _host(_Sessions(eligible=False)) is None

    def test_a_step_whose_dedicated_start_would_get_another_backend_keeps_it(self, cfg):
        cfg["cfg"] = _cfg(backend="codex")
        assert _host(_Sessions()) is None

    def test_a_kiro_cli_chat_is_unchanged(self, cfg):
        assert _host(_Sessions(_runtime(ACP_BACKEND_KIRO))) is None

    def test_no_launching_chat_keeps_the_step_dedicated(self, cfg):
        assert shared_host(_Sessions(), "", agent=None, cwd=None) is None
        assert shared_host(_Sessions(), "workflow-wf_1", agent=None, cwd=None) is None

    @pytest.mark.parametrize(
        "pin",
        [
            {"effort_kwargs": {"reasoning_effort_override": "high"}},
            {"backend_kwargs": {"backend_override": "kiro"}},
            {"named": True},
            {"extra_env": {"X": "1"}},
        ],
        ids=["effort", "backend", "named-session", "extra-env"],
    )
    def test_a_pin_only_a_dedicated_start_applies_keeps_it_dedicated(self, cfg, pin):
        assert _host(_Sessions(), **pin) is None

    def test_the_effort_check_lets_a_crew_pin_be_found(self, cfg):
        seen = []

        def _resolve(agent, crew_agent=None):
            seen.append((agent, crew_agent))
            return "high" if agent == "reviewer" else ""

        cfg["cfg"].resolve_session_effort = _resolve
        assert _host(_Sessions(), agent="reviewer") is None
        # No crew claim passed: the resolver infers the crew, as a dedicated start does.
        assert seen == [("reviewer", None)]

    def test_a_step_that_resolves_to_a_crew_keeps_it_dedicated(self, cfg):
        sessions = _Sessions()
        with patch(
            "kiro_crew.config.loader.resolve_crew_identity",
            side_effect=lambda c, agent, crew: "worker" if agent == "worker" else "",
        ):
            assert _host(sessions, agent="worker") is None
            assert _host(sessions, agent="plain") is sessions.runtime

    def test_an_unnamed_step_whose_default_agent_is_a_crew_keeps_it_dedicated(self, cfg):
        sessions = _Sessions()
        with patch(
            "kiro_crew.config.loader.resolve_crew_identity",
            side_effect=lambda c, agent, crew: "kirocrew" if agent == "kirocrew" else "",
        ):
            assert _host(sessions) is None

    def test_a_configured_session_effort_keeps_it_dedicated(self, cfg):
        cfg["cfg"] = _cfg(effort="high")
        assert _host(_Sessions()) is None

    def test_a_member_or_restricted_run_keeps_it_dedicated(self, cfg):
        member = SimpleNamespace(
            memory_mode="persistent", execution_context=SimpleNamespace(member_id="m1")
        )
        temporary = SimpleNamespace(memory_mode="temporary", execution_context=None)
        plain = SimpleNamespace(
            memory_mode="persistent", execution_context=SimpleNamespace(member_id=None)
        )
        sessions = _Sessions()
        assert _host(sessions, memory_scope=member) is None
        assert _host(sessions, memory_scope=temporary) is None
        assert _host(sessions, memory_scope=plain) is sessions.runtime

    def test_a_cwd_other_than_the_chats_folder_keeps_it_dedicated(self, cfg):
        sessions = _Sessions()
        assert _host(sessions, cwd="/repo/worktree") is None
        assert _host(sessions, cwd=WORK_DIR + "/") is sessions.runtime

    def test_an_aws_profile_split_keeps_it_dedicated(self, cfg):
        assert _host(_Sessions(aws_differs=True)) is None

    def test_the_cap_sends_the_next_step_to_its_own_process(self, cfg):
        sessions = _Sessions()
        for i in range(KAS_SHARED_NODES_PER_PARENT - 1):
            shared_nodes._placed[f"wf:r:{i}"] = PARENT
        assert _host(sessions) is sessions.runtime
        shared_nodes._placed["wf:r:last"] = PARENT
        assert _host(sessions) is None
        shared_nodes.forget("wf:r:last")
        assert _host(sessions) is sessions.runtime

    def test_no_config_answers_dedicated(self, cfg):
        assert _host(_Sessions(), cfg=None) is None

    def test_a_failing_check_answers_dedicated(self, cfg):
        sessions = _Sessions()
        sessions.get_provider = MagicMock(side_effect=RuntimeError("boom"))
        assert _host(sessions) is None


def _acquire(sessions: Any, key: str = "wf:r:0", **kwargs: Any):
    kwargs.setdefault("agent", None)
    kwargs.setdefault("model", None)
    kwargs.setdefault("cwd", None)
    return acquire_step_session(sessions, key, parent_session_key=PARENT, **kwargs)


class TestAcquire:
    @pytest.mark.asyncio
    async def test_a_shared_step_opens_on_the_chats_runtime_and_gets_its_model(self, cfg):
        sessions = _Sessions()
        with patch("kiro_crew.agent_sdk.drivers.acp.apply_configured_model", AsyncMock()) as apply:
            provider, is_new, _ = await _acquire(sessions, model="claude-haiku-5.5")
        assert provider.tag == "shared" and is_new
        assert sessions.opened[0][2]["runtime"] is sessions.runtime
        assert sessions.created == []
        assert shared_nodes.is_placed("wf:r:0")
        apply.assert_awaited_once()
        assert apply.await_args.args[1] == "claude-haiku-5.5"
        assert apply.await_args.kwargs["backend"] == ACP_BACKEND_KAS

    @pytest.mark.asyncio
    async def test_an_unpinned_step_sends_no_model(self, cfg):
        sessions = _Sessions()
        with (
            patch("kiro_crew.agent_sdk.drivers.acp.apply_configured_model", AsyncMock()) as apply,
            patch("kiro_crew.session._session_model", return_value=None),
        ):
            await _acquire(sessions)
        apply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_step_with_no_agent_opens_on_the_dedicated_default_agent(self, cfg):
        from kiro_crew.acp.client_start import CLIENT_NAME

        sessions = _Sessions()
        with patch("kiro_crew.session._session_model", return_value=None) as ladder:
            await _acquire(sessions)
        # Not the chat's agent and not the pool agent: the one a dedicated start runs.
        assert sessions.opened[0][2]["agent"] == CLIENT_NAME
        assert ladder.call_args.args[1] is None

    @pytest.mark.asyncio
    async def test_an_unpinned_step_gets_the_model_a_dedicated_start_resolves(self, cfg):
        sessions = _Sessions()
        with (
            patch("kiro_crew.agent_sdk.drivers.acp.apply_configured_model", AsyncMock()) as apply,
            patch("kiro_crew.session._session_model", return_value="crew-pin-model") as ladder,
        ):
            await _acquire(sessions, agent="reviewer")
        assert ladder.call_args.args[1] == "reviewer"
        assert ladder.call_args.kwargs["session_key"] == "wf:r:0"
        assert apply.await_args.args[1] == "crew-pin-model"

    @pytest.mark.asyncio
    async def test_a_failed_shared_open_falls_back_to_a_dedicated_start(self, cfg):
        sessions = _Sessions()
        sessions.open_raises = RuntimeError("session/new refused")
        provider, _, _ = await _acquire(sessions, model="m")
        assert provider.tag == "dedicated"
        assert sessions.created[0][1]["model"] == "m"
        assert not shared_nodes.is_placed("wf:r:0")

    @pytest.mark.asyncio
    async def test_a_cancelled_shared_open_frees_its_place(self, cfg):
        sessions = _Sessions()
        sessions.open_raises = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await _acquire(sessions)
        await asyncio.wait_for(asyncio.gather(*shared_nodes._teardowns), WAIT_S)
        assert not shared_nodes.is_placed("wf:r:0")

    @pytest.mark.asyncio
    async def test_a_step_cancelled_mid_open_ends_the_session_the_open_creates(self, cfg):
        sessions = _Sessions()
        answered, opened = asyncio.Event(), asyncio.Event()
        real_open = sessions.open_task_session

        async def _slow_open(*args: Any, **kwargs: Any):
            opened.set()
            await answered.wait()
            return await real_open(*args, **kwargs)

        sessions.open_task_session = _slow_open
        step = asyncio.ensure_future(_acquire(sessions))
        try:
            await asyncio.wait_for(_first(opened, step), WAIT_S)
            assert opened.is_set(), f"the step took no shared open: {step.result()}"
            step.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(step, WAIT_S)
            # The open is still in flight: its place stays taken.
            assert shared_nodes.is_placed("wf:r:0")
        finally:
            answered.set()
        await asyncio.wait_for(asyncio.gather(*shared_nodes._teardowns), WAIT_S)
        assert sessions.released == [("wf:r:0", True)]
        assert sessions.destroyed == ["wf:r:0"]
        assert not shared_nodes.is_placed("wf:r:0")

    @pytest.mark.asyncio
    async def test_an_open_that_took_its_own_dedicated_path_is_replaced(self, cfg):
        sessions = _Sessions()
        sessions.open_returns_dedicated = True
        provider, _, _ = await _acquire(sessions, model="m")
        assert provider.tag == "dedicated"
        assert sessions.released == [("wf:r:0", True)]
        assert sessions.destroyed == ["wf:r:0"]
        assert sessions.created[0][1]["model"] == "m"
        assert not shared_nodes.is_placed("wf:r:0")

    @pytest.mark.asyncio
    async def test_a_cancel_while_its_model_is_applied_ends_the_session(self, cfg):
        sessions = _Sessions()
        with patch(
            "kiro_crew.agent_sdk.drivers.acp.apply_configured_model",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _acquire(sessions, model="m")
        assert sessions.released == [("wf:r:0", True)]
        assert sessions.destroyed == ["wf:r:0"]
        assert not shared_nodes.is_placed("wf:r:0")

    @pytest.mark.asyncio
    async def test_a_dedicated_step_gets_every_argument_it_had(self, cfg):
        cfg["cfg"] = _cfg(sharing=False)
        sessions = _Sessions()
        await _acquire(
            sessions,
            agent="a",
            model="m",
            cwd="/w",
            extra_env={"X": "1"},
            effort_kwargs={"reasoning_effort_override": "low"},
            backend_kwargs={"backend_override": "kiro"},
        )
        assert sessions.opened == []
        assert sessions.created == [
            (
                "wf:r:0",
                {
                    "agent": "a",
                    "model": "m",
                    "cwd": "/w",
                    "extra_env": {"X": "1"},
                    "reasoning_effort_override": "low",
                    "backend_override": "kiro",
                },
            )
        ]


def _stream(texts: dict[str, Any]):
    async def _fake(provider, prompt, **kwargs):
        result = texts[prompt]
        if isinstance(result, BaseException):
            raise result
        return result

    return _fake


class TestBuildAgentFn:
    @pytest.mark.asyncio
    async def test_shared_steps_end_their_session_and_a_failure_stays_in_its_step(self, cfg):
        sessions = _Sessions()
        agent_fn = build_agent_fn(sessions, run_id="r", session_key=PARENT)
        texts = {"a": "A", "b": RuntimeError("model error"), "c": "C"}
        with (
            patch("kiro_crew.workflows.agent_exec.stream_and_collect", _stream(texts)),
            patch("kiro_crew.workflows.agent_exec.wait_for_declared_mcp", AsyncMock()),
            patch("kiro_crew.workflows.agent_exec.vet_step_spawn", AsyncMock(return_value=None)),
            patch("kiro_crew.workflows.agent_exec.persist_step_usage_row", AsyncMock()),
            patch("kiro_crew.messaging.identity.publish_turn_identity", AsyncMock()),
        ):
            results = await asyncio.gather(
                agent_fn("a", {}), agent_fn("b", {}), agent_fn("c", {}), return_exceptions=True
            )
        assert results[0] == "A" and results[2] == "C"
        assert isinstance(results[1], RuntimeError)
        assert sorted(k for _, k, _ in sessions.opened) == ["wf:r:0", "wf:r:1", "wf:r:2"]
        # Every step, the failed one included, ends its session on the chat's process.
        assert sorted(sessions.destroyed) == ["wf:r:0", "wf:r:1", "wf:r:2"]
        assert shared_nodes._placed == {}

    @pytest.mark.asyncio
    async def test_sharing_off_keeps_the_old_dedicated_teardown(self, cfg):
        cfg["cfg"] = _cfg(sharing=False)
        sessions = _Sessions()
        agent_fn = build_agent_fn(sessions, run_id="r", session_key=PARENT)
        with (
            patch("kiro_crew.workflows.agent_exec.stream_and_collect", _stream({"a": "A"})),
            patch("kiro_crew.workflows.agent_exec.wait_for_declared_mcp", AsyncMock()),
            patch("kiro_crew.workflows.agent_exec.vet_step_spawn", AsyncMock(return_value=None)),
            patch("kiro_crew.workflows.agent_exec.persist_step_usage_row", AsyncMock()),
            patch("kiro_crew.messaging.identity.publish_turn_identity", AsyncMock()),
        ):
            assert await agent_fn("a", {}) == "A"
        assert sessions.opened == [] and [k for k, _ in sessions.created] == ["wf:r:0"]
        assert sessions.released == [("wf:r:0", True)] and sessions.destroyed == []


class TestPoolWorker:
    @pytest.mark.asyncio
    async def test_a_worker_starts_on_the_chats_process_and_frees_its_place(self, cfg):
        sessions = _Sessions()
        worker = _WorkflowSessionWorker(
            sessions, key="wf-pool:r:0", agent=None, model=None, cwd=None, parent_session_key=PARENT
        )
        with patch.object(
            _WorkflowSessionWorker, "_configured_delete_delay", AsyncMock(return_value=0)
        ):
            await worker.start()
        assert sessions.opened[0][1] == "wf-pool:r:0"
        assert shared_nodes.is_placed("wf-pool:r:0")
        await worker.shutdown()
        assert sessions.destroyed == ["wf-pool:r:0"]
        assert not shared_nodes.is_placed("wf-pool:r:0")

    @pytest.mark.asyncio
    async def test_retiring_a_worker_cancelled_mid_open_keeps_its_place_until_the_open_ends(
        self, cfg
    ):
        """A worker cancelled while its (re)start's open is in flight, then retired."""
        sessions = _Sessions()
        answered, opened = asyncio.Event(), asyncio.Event()
        real_open = sessions.open_task_session

        async def _slow_open(*args: Any, **kwargs: Any):
            opened.set()
            await answered.wait()
            return await real_open(*args, **kwargs)

        sessions.open_task_session = _slow_open
        worker = _WorkflowSessionWorker(
            sessions, key="wf-pool:r:0", agent=None, model=None, cwd=None, parent_session_key=PARENT
        )
        with patch.object(
            _WorkflowSessionWorker, "_configured_delete_delay", AsyncMock(return_value=0)
        ):
            start = asyncio.ensure_future(worker.start())
            try:
                await asyncio.wait_for(_first(opened, start), WAIT_S)
                assert opened.is_set(), "the worker took no shared open"
                start.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(start, WAIT_S)
                # WorkerPool retires the unhealthy worker; the open has not answered.
                await worker.shutdown()
                assert shared_nodes.is_placed("wf-pool:r:0")
            finally:
                answered.set()
        await asyncio.wait_for(asyncio.gather(*shared_nodes._teardowns), WAIT_S)
        assert not shared_nodes.is_placed("wf-pool:r:0")
        assert sessions.destroyed.count("wf-pool:r:0") == 2

    @pytest.mark.asyncio
    async def test_a_worker_with_an_effort_stays_dedicated(self, cfg):
        sessions = _Sessions()
        worker = _WorkflowSessionWorker(
            sessions,
            key="wf-pool:r:1",
            agent=None,
            model=None,
            cwd=None,
            effort="high",
            parent_session_key=PARENT,
        )
        with patch.object(
            _WorkflowSessionWorker, "_configured_delete_delay", AsyncMock(return_value=0)
        ):
            await worker.start()
        assert sessions.opened == []
        assert sessions.created[0][1]["reasoning_effort_override"] == "high"


class TestPooledAgentFn:
    @pytest.mark.asyncio
    async def test_a_pooled_fan_out_opens_every_worker_on_the_chats_process(self, cfg):
        sessions = _Sessions()
        agent_fn, pool = build_pooled_agent_fn(sessions, run_id="r", session_key=PARENT)
        with (
            patch(
                "kiro_crew.workflows.agent_pool.stream_and_collect", _stream({"a": "A", "b": "B"})
            ),
            patch("kiro_crew.workflows.agent_pool.wait_for_declared_mcp", AsyncMock()),
            patch("kiro_crew.workflows.agent_pool.vet_step_spawn", AsyncMock(return_value=None)),
            patch("kiro_crew.workflows.agent_pool.persist_step_usage_row", AsyncMock()),
            patch("kiro_crew.workflows.agent_pool.publish_turn_identity", AsyncMock()),
            patch.object(
                _WorkflowSessionWorker, "_configured_delete_delay", AsyncMock(return_value=0)
            ),
        ):
            assert sorted(await asyncio.gather(agent_fn("a", {}), agent_fn("b", {}))) == ["A", "B"]
            await pool.shutdown()
        assert sorted(k for _, k, _ in sessions.opened) == ["wf-pool:r:0", "wf-pool:r:1"]
        assert sessions.created == []
        assert sorted(sessions.destroyed) == ["wf-pool:r:0", "wf-pool:r:1"]
        assert shared_nodes._placed == {}


class TestOpenTaskSessionOnAChosenRuntime:
    @pytest.mark.asyncio
    async def test_a_step_for_a_crew_states_that_crew_on_the_host(self):
        from kiro_crew.session_capabilities import CapabilityPreparation

        cfg = KiroCrewConfig()
        mgr = SessionManager(cfg, provider_factory=MagicMock())
        handle = MagicMock(session_id="step-1", is_turn_active=False, memory_mode="persistent")
        handle.destroy = AsyncMock()
        handle.kas_registered_agents = []
        handle.kas_launch_snapshots = []
        runtime = MagicMock(sandbox_mode=cfg.agent.sandbox, acp_backend=ACP_BACKEND_KIRO)
        runtime.is_alive = MagicMock(return_value=True)
        runtime.create_session = AsyncMock(return_value=handle)
        prepared = CapabilityPreparation(member="worker")
        with patch("kiro_crew.session_capabilities.prepare_runtime", return_value=prepared):
            provider, _, _ = await mgr.open_task_session(
                PARENT, "wf:r:1", agent="worker", runtime=runtime
            )
        assert runtime.create_session.await_args.kwargs["crew_agent"] == "worker"
        assert provider._session_crew_agent == "worker"
        mgr.release("wf:r:1")
        await mgr.destroy("wf:r:1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_session_opens_on_the_given_runtime_with_its_key(self):
        cfg = KiroCrewConfig()
        factory = MagicMock(side_effect=AssertionError("no run runtime is bootstrapped"))
        mgr = SessionManager(cfg, provider_factory=factory)
        handle = MagicMock(session_id="step-0", is_turn_active=False, memory_mode="persistent")
        handle.destroy = AsyncMock()
        handle.kas_registered_agents = []
        handle.kas_launch_snapshots = []
        runtime = MagicMock(sandbox_mode=cfg.agent.sandbox, acp_backend=ACP_BACKEND_KIRO)
        runtime.is_alive = MagicMock(return_value=True)
        runtime.create_session = AsyncMock(return_value=handle)
        runtime.kill = AsyncMock()
        key = "wf:r:0"

        provider, is_new, _ = await mgr.open_task_session(
            PARENT, key, agent="specialist", runtime=runtime
        )

        assert is_new and provider._runtime is runtime
        assert provider._session_key == key
        # A fresh conversation on a reused worker keeps the step's agent.
        assert provider._session_agent == "specialist"
        # No crew of its own: it states "" instead of inheriting the host's.
        assert runtime.create_session.await_args.kwargs["crew_agent"] == ""
        assert runtime.create_session.await_args.kwargs["member_context"] is False
        assert provider._session_crew_agent == ""
        assert provider._owns_runtime is False
        assert runtime.create_session.await_args.kwargs["session_key"] == key
        assert PARENT not in mgr._subagent_runtimes
        factory.assert_not_called()
        mgr.release(key)
        await mgr.destroy(key)
        handle.destroy.assert_awaited()
        runtime.kill.assert_not_awaited()
        await mgr.close_all()
