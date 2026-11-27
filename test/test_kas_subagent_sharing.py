"""A KAS chat's ``spawn_run`` sub-agents share its process, under ``agent.session_sharing``.

KAS is in ``ACP_BACKENDS_SESSION_SHARING``; its parent's own process must also
qualify: it can close a session and keep the record, runs an engine at or above
the chat-sharing floor, is not in machine mode, was not spawned under a looser
sandbox tier than a fresh start gets, and serves no other chat. A shared KAS
sub-agent's teardown closes its session with ``session/close`` (record kept for
``spawn_continue``) instead of the engine's record-deleting ``_kiro/session/delete``.
Every case that cannot share stays on the dedicated arm; each has a test here.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import kas_subagent_sharing as kss
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_KAS_SESSION_DELETE,
    METHOD_SESSION_CLOSE,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_SESSION_SHARING
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.run_start import KAS_SHARED_CHILDREN_PER_PARENT

_SID = "sess_5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_CLOSE_CAPS = {"sessionCapabilities": {"close": {}, "delete": {}}}
_PARENT = "dashboard:kas-parent"


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers) -> None:
    """Every manager built here opens tasks.db; close it at teardown, not at GC."""


def _cfg(*, session_sharing: bool = True, sandbox: str = "standard") -> MagicMock:
    cfg = MagicMock()
    cfg.agent.session_sharing = session_sharing
    cfg.agent.sandbox = sandbox
    cfg.agent.role_models = {}
    cfg.agent.role_efforts = {}
    cfg.resolve_session_effort = MagicMock(return_value="")
    return cfg


@pytest.fixture
def cfg():
    """One config for both readers (``kiro_crew.subagent`` and the host check)."""
    value = _cfg()
    with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=value):
        yield value


def _runtime(
    tmp_path,
    *,
    backend: str = ACP_BACKEND_KAS,
    caps=None,
    engine: str = "0.66.127",
    sandbox_mode: str = "standard",
) -> AcpRuntime:
    runtime = AcpRuntime(work_dir=tmp_path, sandbox_mode=sandbox_mode, acp_backend=backend)
    runtime._process = object()  # type: ignore[assignment]
    runtime._agent_capabilities = dict(_CLOSE_CAPS if caps is None else caps)
    runtime._harness.relay_engine_version = engine
    return runtime


def _started_provider(runtime: AcpRuntime, *, chat_shared: bool = False) -> AcpProvider:
    """A provider after its start put an ``AcpSessionProvider`` in ``_client``."""
    provider = AcpProvider(acp_backend=runtime.acp_backend)
    handle = AcpSessionHandle(_SID, asyncio.Queue(), runtime)
    provider._client = AcpSessionProvider(  # type: ignore[assignment]
        handle, runtime, shared_runtime=chat_shared
    )
    return provider


def test_kas_is_a_session_sharing_member():
    assert ACP_BACKEND_KAS in ACP_BACKENDS_SESSION_SHARING


class TestRuntimeHostsSubagents:
    def test_a_qualifying_kas_process_hosts_sub_agents(self, tmp_path, cfg):
        assert kss.runtime_hosts_subagents(_runtime(tmp_path)) is True

    def test_an_engine_without_session_close_keeps_them_dedicated(self, tmp_path, cfg):
        """Its teardown could only delete the record ``spawn_continue`` loads."""
        runtime = _runtime(tmp_path, caps={"sessionCapabilities": {"delete": {}}})
        assert kss.runtime_hosts_subagents(runtime) is False

    @pytest.mark.parametrize("engine", ["0.66.103", "", "not-a-version"])
    def test_an_engine_below_the_floor_or_unknown_keeps_them_dedicated(self, tmp_path, cfg, engine):
        assert kss.runtime_hosts_subagents(_runtime(tmp_path, engine=engine)) is False

    def test_the_bundle_engine_version_is_read_too(self, tmp_path, cfg):
        runtime = _runtime(tmp_path, engine="")
        runtime._harness.bundle_engine_version = "0.66.127"
        assert kss.runtime_hosts_subagents(runtime) is True

    def test_machine_mode_keeps_them_dedicated(self, tmp_path, cfg):
        runtime = _runtime(tmp_path)
        runtime._harness.spawn_machine_auth = object()
        assert kss.runtime_hosts_subagents(runtime) is False

    def test_a_looser_spawn_sandbox_than_configured_keeps_them_dedicated(self, tmp_path, cfg):
        cfg.agent.sandbox = "strict"
        assert kss.runtime_hosts_subagents(_runtime(tmp_path, sandbox_mode="standard")) is False

    def test_a_process_serving_other_chats_keeps_them_dedicated(self, tmp_path, cfg):
        assert kss.runtime_hosts_subagents(_runtime(tmp_path), chat_shared=True) is False

    def test_another_backend_is_not_this_rule(self, tmp_path, cfg):
        assert kss.runtime_hosts_subagents(_runtime(tmp_path, backend=ACP_BACKEND_KIRO)) is False
        assert kss.runtime_hosts_subagents(None) is False


class TestProviderEligibility:
    def test_a_qualifying_kas_parent_is_eligible(self, tmp_path, cfg):
        assert _started_provider(_runtime(tmp_path)).is_session_sharing_eligible is True

    def test_a_kas_parent_whose_process_does_not_qualify_is_not(self, tmp_path, cfg):
        runtime = _runtime(tmp_path, engine="0.66.103")
        assert _started_provider(runtime).is_session_sharing_eligible is False

    def test_a_kas_parent_on_a_chat_shared_process_is_not_eligible(self, tmp_path, cfg):
        provider = _started_provider(_runtime(tmp_path), chat_shared=True)
        assert provider.is_session_sharing_eligible is False

    def test_a_kiro_parent_is_unchanged(self, tmp_path, cfg):
        runtime = _runtime(tmp_path, backend=ACP_BACKEND_KIRO, engine="", caps={})
        assert _started_provider(runtime).is_session_sharing_eligible is True

    def test_a_harness_outside_the_set_stays_dedicated(self, tmp_path, cfg):
        assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_SESSION_SHARING
        provider = AcpProvider(acp_backend=ACP_BACKEND_CLAUDE)
        assert provider.is_session_sharing_eligible is False


class _KasParent:
    """A manager whose parent session is a real KAS provider over a real runtime."""

    def __init__(self, tmp_path, **runtime_kwargs) -> None:
        self.runtime = _runtime(tmp_path, **runtime_kwargs)
        self.provider = _started_provider(self.runtime)
        sessions = MagicMock()
        sessions.get_provider = MagicMock(return_value=self.provider)
        sessions.is_session_sharing_eligible = MagicMock(
            side_effect=lambda _key: self.provider.is_session_sharing_eligible
        )
        sessions.aws_profile_split_possible = MagicMock(return_value=False)
        sessions._subagent_runtimes = {}
        self.sessions = sessions
        self.manager = SubagentManager(
            sessions=sessions, ctx_builder=MagicMock(), is_yolo=lambda: True
        )

    def info(self, **kwargs) -> SubagentInfo:
        kwargs.setdefault("agent", "kirocrew")
        info = SubagentInfo(
            id=kwargs.pop("id", "child"), task="t", parent_session_key=_PARENT, **kwargs
        )
        self.manager._agents[info.id] = info
        return info

    def shared(self, info: SubagentInfo, child_aws_profile=None) -> bool:
        return self.manager._sharing_plan(info, child_aws_profile=child_aws_profile).shared


class TestSharingPlanOnKas:
    """The one decision the run and admission both read, on a KAS parent."""

    def test_a_template_child_shares(self, tmp_path, cfg):
        parent = _KasParent(tmp_path)
        assert parent.shared(parent.info()) is True

    def test_a_model_pin_still_shares(self, tmp_path, cfg):
        parent = _KasParent(tmp_path)
        assert parent.shared(parent.info(model="claude-haiku-5.5")) is True

    def test_a_configured_effort_keeps_an_unpinned_kas_child_dedicated(self, tmp_path, cfg):
        cfg.resolve_session_effort.return_value = "high"
        parent = _KasParent(tmp_path)
        parent.manager._get_parent_runtime = MagicMock(  # type: ignore[method-assign]
            return_value=parent.runtime
        )
        assert parent.shared(parent.info()) is False
        cfg.resolve_session_effort.assert_called_once_with("kirocrew", "")

    def test_a_configured_effort_does_not_change_an_unpinned_kiro_child(self, tmp_path, cfg):
        cfg.resolve_session_effort.return_value = "high"
        parent = _KasParent(tmp_path, backend=ACP_BACKEND_KIRO, engine="", caps={})
        parent.manager._get_parent_runtime = MagicMock(  # type: ignore[method-assign]
            return_value=parent.runtime
        )
        assert parent.shared(parent.info()) is True
        cfg.resolve_session_effort.assert_not_called()

    def test_an_unreadable_configured_effort_keeps_a_kas_child_dedicated(self, tmp_path, cfg):
        cfg.resolve_session_effort.side_effect = RuntimeError("effort config unavailable")
        parent = _KasParent(tmp_path)
        parent.manager._get_parent_runtime = MagicMock(  # type: ignore[method-assign]
            return_value=parent.runtime
        )
        assert parent.shared(parent.info()) is False

    def test_session_sharing_off_keeps_kas_children_dedicated(self, tmp_path, cfg):
        cfg.agent.session_sharing = False
        parent = _KasParent(tmp_path)
        assert parent.shared(parent.info()) is False

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"keep": True}, id="keep"),
            pytest.param({"keep": True, "conversation_key": "subagent:old"}, id="continuation"),
            pytest.param({"bare": True}, id="bare"),
            pytest.param({"allowed_tools": ["read"]}, id="allowed_tools"),
            pytest.param({"backend": ACP_BACKEND_KAS}, id="backend-pin"),
            pytest.param({"reasoning_effort": "high"}, id="effort-pin"),
        ],
    )
    def test_a_spawn_that_owns_its_process_stays_dedicated(self, tmp_path, cfg, kwargs):
        parent = _KasParent(tmp_path)
        assert parent.shared(parent.info(**kwargs)) is False

    def test_a_role_effort_pin_stays_dedicated(self, tmp_path, cfg):
        cfg.agent.role_efforts = {"subagent": "low"}
        parent = _KasParent(tmp_path)
        assert parent.shared(parent.info()) is False

    def test_a_different_aws_profile_stays_dedicated(self, tmp_path, cfg):
        from kiro_crew.aws_project_profiles import ResolvedAwsProfile

        parent = _KasParent(tmp_path)
        parent.sessions.aws_profile_split_possible = MagicMock(return_value=True)
        parent.sessions.aws_profile_differs_from_parent = MagicMock(return_value=True)
        assert parent.shared(parent.info(), ResolvedAwsProfile("other")) is False

    @pytest.mark.parametrize(
        "runtime_kwargs",
        [
            pytest.param({"engine": "0.66.103"}, id="old-engine"),
            pytest.param({"caps": {"sessionCapabilities": {}}}, id="no-close"),
        ],
    )
    def test_a_parent_process_that_does_not_qualify_keeps_it_dedicated(
        self, tmp_path, cfg, runtime_kwargs
    ):
        parent = _KasParent(tmp_path, **runtime_kwargs)
        assert parent.shared(parent.info()) is False

    def test_machine_mode_keeps_it_dedicated(self, tmp_path, cfg):
        parent = _KasParent(tmp_path)
        parent.runtime._harness.spawn_machine_auth = object()
        assert parent.shared(parent.info()) is False

    def test_a_sandbox_mismatch_keeps_it_dedicated(self, tmp_path, cfg):
        cfg.agent.sandbox = "strict"
        parent = _KasParent(tmp_path, sandbox_mode="standard")
        assert parent.shared(parent.info()) is False


class TestPerParentCap:
    """A KAS parent's process takes at most ``KAS_SHARED_CHILDREN_PER_PARENT`` children."""

    @staticmethod
    def _parent(tmp_path, backend: str, live_shared: int) -> _KasParent:
        parent = _KasParent(tmp_path, backend=backend, engine="0.66.127")
        parent.manager._get_parent_runtime = MagicMock(  # type: ignore[method-assign]
            return_value=parent.runtime
        )
        for n in range(live_shared):
            parent.info(id=f"s{n}")._session_sharing = True
        done = parent.info(id="finished")
        done._session_sharing = True
        done.done = True
        other = SubagentInfo(id="elsewhere", task="t", parent_session_key="dashboard:other")
        other._session_sharing = True
        parent.manager._agents[other.id] = other
        return parent

    @staticmethod
    def _taken(parent: _KasParent, info: SubagentInfo) -> bool:
        return parent.manager._run_events._kas_shared_slot_taken(info)

    def test_below_the_cap_a_kas_child_reserves_a_place(self, tmp_path, cfg):
        parent = self._parent(tmp_path, ACP_BACKEND_KAS, KAS_SHARED_CHILDREN_PER_PARENT - 1)
        info = parent.info(id="new")
        assert self._taken(parent, info) is False
        assert info._shared_slot_reserved is True

    def test_at_the_cap_the_next_kas_child_takes_its_own_process(self, tmp_path, cfg):
        parent = self._parent(tmp_path, ACP_BACKEND_KAS, KAS_SHARED_CHILDREN_PER_PARENT)
        info = parent.info(id="new")
        assert self._taken(parent, info) is True
        assert info._shared_slot_reserved is False

    def test_two_starts_racing_for_the_last_place_get_one(self, tmp_path, cfg):
        """A reservation counts before its session exists, so the second start is refused."""
        parent = self._parent(tmp_path, ACP_BACKEND_KAS, KAS_SHARED_CHILDREN_PER_PARENT - 1)
        first, second = parent.info(id="first"), parent.info(id="second")
        assert self._taken(parent, first) is False
        assert self._taken(parent, second) is True

    def test_a_reservation_given_up_frees_the_place(self, tmp_path, cfg):
        parent = self._parent(tmp_path, ACP_BACKEND_KAS, KAS_SHARED_CHILDREN_PER_PARENT - 1)
        first, second = parent.info(id="first"), parent.info(id="second")
        assert self._taken(parent, first) is False
        first._shared_slot_reserved = False  # what the dedicated fallback does
        assert self._taken(parent, second) is False

    def test_a_kiro_parent_has_no_such_cap(self, tmp_path, cfg):
        parent = self._parent(tmp_path, ACP_BACKEND_KIRO, KAS_SHARED_CHILDREN_PER_PARENT)
        info = parent.info(id="new")
        assert self._taken(parent, info) is False
        assert info._shared_slot_reserved is False


class TestSharedTeardownKeepsTheRecord:
    """The shared arm's teardown (``AcpSessionProvider.shutdown``, not owning the runtime)."""

    @staticmethod
    async def _teardown(runtime: AcpRuntime, *, keep_transcript: bool) -> list[str]:
        sent: list[str] = []

        async def _send(method: str, params: dict, timeout: float = 0.0) -> dict:
            sent.append(method)
            return {}

        runtime._send_and_await = _send  # type: ignore[method-assign]
        handle = AcpSessionHandle(_SID, asyncio.Queue(), runtime)
        runtime._session_queues[_SID] = handle._queue  # type: ignore[attr-defined]
        provider = AcpSessionProvider(handle, runtime, session_key="subagent:r1")
        provider.set_keep_transcript(keep_transcript)
        await provider.shutdown()
        return sent

    @pytest.mark.asyncio
    async def test_a_kept_transcript_closes_the_session_and_keeps_the_record(self, tmp_path):
        runtime = _runtime(tmp_path)
        sent = await self._teardown(runtime, keep_transcript=True)
        assert sent == [METHOD_SESSION_CLOSE]
        assert _SID not in runtime._session_queues
        assert _SID not in getattr(runtime, "_record_keeping_sessions", set())

    @pytest.mark.asyncio
    async def test_reaping_an_active_shared_child_keeps_its_record(self, tmp_path, monkeypatch):
        from test_session_sharing import _mock_ctx_builder_auto, _mock_sessions

        runtime = _runtime(tmp_path)
        monkeypatch.setattr(runtime, "is_alive", lambda: True)
        monkeypatch.setattr(runtime, "send_notification", AsyncMock())
        handle = AcpSessionHandle(_SID, asyncio.Queue(), runtime)
        handle._turn_done.clear()
        runtime._session_queues[_SID] = handle._queue
        provider = AcpSessionProvider(handle, runtime, session_key="subagent:reap1")
        assert handle.is_turn_active
        assert handle.keep_transcript is False
        sent: list[str] = []

        async def _send(method: str, params: dict, timeout: float = 0.0) -> dict:
            sent.append(method)
            return {}

        monkeypatch.setattr(runtime, "_send_and_await", _send)
        sessions = _mock_sessions(sharing_eligible=True)
        manager = SubagentManager(
            sessions=sessions, ctx_builder=_mock_ctx_builder_auto(), is_yolo=lambda: True
        )
        info = SubagentInfo(
            id="reap1",
            task="long task",
            parent_session_key=_PARENT,
            _session_sharing=True,
            _shared_provider=provider,
        )
        manager._agents[info.id] = info
        manager._tasks[info.id] = MagicMock(done=MagicMock(return_value=False))

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            await manager._force_reap(info.id, info, 1800.0, reason="deadline")

        assert sent == [METHOD_SESSION_CLOSE]
        assert handle.keep_transcript is True
        assert _SID not in runtime._session_queues
        assert runtime._process is not None
        sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_transcript_not_kept_is_deleted_as_before(self, tmp_path):
        sent = await self._teardown(_runtime(tmp_path), keep_transcript=False)
        assert sent == [METHOD_KAS_SESSION_DELETE]

    @pytest.mark.asyncio
    async def test_an_engine_without_close_deletes_as_before(self, tmp_path):
        runtime = _runtime(tmp_path, caps={"sessionCapabilities": {"delete": {}}})
        sent = await self._teardown(runtime, keep_transcript=True)
        assert sent == [METHOD_KAS_SESSION_DELETE]


class TestTheRunHonoursTheCap:
    """Through ``spawn``: the reservation decides the arm, and a fallback gives it back."""

    @staticmethod
    def _manager(live_shared: int):
        from test_session_sharing import _mock_ctx_builder_auto, _mock_sessions

        sessions = _mock_sessions(sharing_eligible=True)
        runtime = sessions.get_subagent_runtime.return_value
        runtime.acp_backend = ACP_BACKEND_KAS
        sessions._subagent_runtimes = {"dashboard:slot1": runtime}
        manager = SubagentManager(
            sessions=sessions, ctx_builder=_mock_ctx_builder_auto(), is_yolo=lambda: True
        )
        for n in range(live_shared):
            sibling = SubagentInfo(id=f"live{n}", task="t", parent_session_key="dashboard:slot1")
            sibling._session_sharing = True
            manager._agents[sibling.id] = sibling
        return manager, sessions, runtime

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("healthy_host_memory")
    async def test_a_child_past_the_cap_takes_a_dedicated_process(self):
        from test_session_sharing import _cfg_patch, _wait_until_done

        manager, sessions, runtime = self._manager(KAS_SHARED_CHILDREN_PER_PARENT)
        with (
            _cfg_patch() as load,
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            load.return_value.resolve_session_effort.return_value = ""
            info = manager.spawn("t", parent_session_key="dashboard:slot1")
            await _wait_until_done(info)
        runtime.create_session.assert_not_awaited()
        sessions.get_or_create.assert_awaited()
        assert info._session_sharing is False
        assert info._shared_slot_reserved is False

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("healthy_host_memory")
    async def test_a_child_below_the_cap_shares_and_holds_its_place(self):
        from test_session_sharing import _cfg_patch, _wait_until_done

        manager, sessions, runtime = self._manager(KAS_SHARED_CHILDREN_PER_PARENT - 1)
        with (
            _cfg_patch() as load,
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            load.return_value.resolve_session_effort.return_value = ""
            info = manager.spawn("t", parent_session_key="dashboard:slot1")
            await _wait_until_done(info)
        runtime.create_session.assert_awaited()
        sessions.get_or_create.assert_not_awaited()
        assert info._shared_slot_reserved is True

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("healthy_host_memory")
    async def test_a_shared_start_that_falls_back_gives_its_place_back(self):
        from test_session_sharing import _cfg_patch, _wait_until_done

        manager, sessions, runtime = self._manager(0)
        runtime.create_session.side_effect = RuntimeError("runtime dead")
        with (
            _cfg_patch() as load,
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            load.return_value.resolve_session_effort.return_value = ""
            info = manager.spawn("t", parent_session_key="dashboard:slot1")
            await _wait_until_done(info)
        runtime.create_session.assert_awaited()
        sessions.get_or_create.assert_awaited()
        assert info._shared_slot_reserved is False
