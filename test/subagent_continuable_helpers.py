"""Shared fakes and fixtures for the continuable subagent conversation tests.

``test_subagent_continuable.py`` and its ``test_subagent_continuable_*.py`` siblings
import from here: the mocked SessionManager (``_mock_sessions``) and context builder,
the ``_manager`` builder, ``_stop_reason`` for a failing run's diagnostic, and the
``continuation_runtime`` fixture (real allocation and protected state; only the
external native provider is doubled). ``_close_subagent_managers`` is autouse, so
every test module binds it. Other suites import some of these names from
``test_subagent_continuable``, which re-exports them.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager

# Subagent-registry isolation is provided globally by the autouse
# ``_isolate_subagents_dir`` fixture in ``conftest.py``.


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``."""


def _mock_sessions(resumed: bool = False) -> MagicMock:
    """Mock SessionManager with async methods + continuable API.

    *resumed* is the third element of get_or_create's return — continuation
    tests set True to satisfy the fail-closed resume guard.
    """
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    # Read synchronously after every turn; as AsyncMock children they
    # would hand back coroutines nobody awaits.
    provider.context_window_tokens = lambda: 100000
    provider.context_used_tokens = lambda: 0
    provider.session_id = "sid-123"
    provider.cwd = ""

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # noqa: unreachable — makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, resumed))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(
        side_effect=lambda key: ("template", sessions.get_agent(key))
    )
    sessions.mark_continuable = MagicMock()
    sessions.unmark_continuable = MagicMock()
    sessions.is_continuable = MagicMock(return_value=False)
    sessions.resumable_sid = MagicMock(return_value="sid-123")
    sessions.forget_conversation = MagicMock(return_value="sid-123")
    sessions.conversation_provider = MagicMock(return_value="acp")
    sessions.get_provider = MagicMock(return_value=None)
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


def _manager(sessions: MagicMock | None = None) -> SubagentManager:
    return SubagentManager(
        sessions=sessions or _mock_sessions(),
        ctx_builder=_mock_ctx_builder(),
    )


def _stop_reason(info: SubagentInfo) -> str:
    """Every marker that says WHY a run stopped, for an assertion message.

    A run cancelled from outside takes the auto-continue branch, which sets
    neither ``error`` nor ``done``: without these markers in the message, "the
    run was cancelled" is indistinguishable from "the run produced the wrong
    answer" -- the reading that let a real loop stall be reported as a
    memory-mode mismatch.
    """
    return (
        f"error={info.error!r} done={info.done} user_stopped={info.user_stopped} "
        f"reaped={info.reaped} cancel_retry_used={info._cancel_retry_used} "
        f"recovering={info._recovering} mode_ready={info._memory_mode_ready} "
        f"mode={info.memory_mode!r} turns={info.turns}"
    )


@pytest.fixture
def continuation_runtime(tmp_path, monkeypatch):
    """Real allocation and protected state; only the external native provider is doubled."""
    import json
    import uuid
    from types import SimpleNamespace

    from kiro_crew import agent, agent_state, session
    from kiro_crew.agent_capabilities import CapabilityService, prepare_member_capabilities
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import (
        bind_private_session_store,
        private_memory_store_for_session,
    )
    from kiro_crew.memory_stores import provision_member_memory
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent, LLMProvider

    home, specs, project, native = (
        tmp_path / name for name in ("home", "agents", "project", "native")
    )
    for path in (home, specs, project, native):
        path.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", specs)
    monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", specs)
    # Model resolution and the native provider must read the same agent specs.
    monkeypatch.setattr("kiro_crew.config.loader.kiro_agents_dir", lambda: specs)
    monkeypatch.setattr(agent_state, "_state_path", lambda: home / "agent_model_state.json")
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", native)
    # Keep real V2 provisioning, ownership, delegation and binding checks.
    for name in ("worker", "member-parent"):
        (specs / f"{name}.json").write_text(
            json.dumps({"name": name, "prompt": name, "tools": [], "includeMcpJson": False}),
            encoding="utf-8",
        )
    cfg = KiroCrewConfig.load()
    cfg.workspaces[cfg.default_workspace] = WorkspaceConfig(dir=str(project))
    cfg.session.pool_size = 0
    cfg.agent.subagent_cwd_allowed_roots = [str(project)]
    cfg.agents["owner"] = KiroCrewAgentConfig(kiro_agent="worker")
    store = provision_member_memory(cfg, "owner")
    cfg.save()
    history = ConversationLog()
    parent = "dashboard:owner"
    bind_private_session_store(parent, store)
    history.update_metadata(parent, {"memory_store": store})
    made = []
    managers = []

    class NativeProvider(LLMProvider):
        def __init__(self, key, template, cwd, crew_agent, private):
            self.key, self.template, self._cwd = key, template, cwd
            self.crew_agent = crew_agent
            self._private_memory = private
            self.active = ""
            self.incarnation = ""
            self.messages = []
            self.resume_sid = ""
            self.client = SimpleNamespace(
                _session_id="",
                _pid=None,
                resumed=False,
                set_resume_session_id=self.set_resume_session_id,
            )

        def set_resume_session_id(self, sid):
            self.resume_sid = sid

        async def prepare_private_memory(self):
            pass

        async def start(self):
            def load():
                spec = json.loads((specs / f"{self.template}.json").read_text(encoding="utf-8"))
                if self.resume_sid:
                    assert (native / f"{self.resume_sid}.json").is_file()
                sid = self.resume_sid or uuid.uuid4().hex
                (native / f"{sid}.json").write_text("{}", encoding="utf-8")
                (native / f"{sid}.jsonl").write_text(
                    '{"role":"user","content":"original context"}\n', encoding="utf-8"
                )
                return spec["name"], sid

            self.active, self.client._session_id = await asyncio.to_thread(load)
            self.client.resumed = bool(self.resume_sid)
            self.incarnation = uuid.uuid4().hex

        async def shutdown(self):
            self.incarnation = ""

        async def stream(self, message):
            owner = next(
                manager for manager in reversed(managers) if manager.get_provider(self.key) is self
            )
            self.capability_stamp = owner._sessions[self.key].loaded_capabilities
            self.messages.append(message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield LLMEvent(kind=EVENT_COMPLETE)

        async def approve_tool(self, request_id, *, always=False):
            pass

        async def reject_tool(self, request_id):
            pass

        def context_usage_pct(self):
            return 0

        def is_process_alive(self):
            return bool(self.incarnation)

        def is_alive(self):
            return self.is_process_alive()

        @property
        def cwd(self):
            return self._cwd

        @property
        def session_id(self):
            return self.client._session_id

        @property
        def process_instance(self):
            return self.incarnation

        @property
        def member_capabilities_supported(self):
            return True

        @property
        def loaded_capability_template(self):
            return self.active

    # Expose native session/load through the manager's external-provider seam.
    is_acp_provider = session._is_acp_provider
    monkeypatch.setattr(
        session,
        "_is_acp_provider",
        lambda provider: isinstance(provider, NativeProvider) or is_acp_provider(provider),
    )

    def factory(key, agent=None, cwd=None, crew_agent=None, **kwargs):
        provider = NativeProvider(
            key, agent, cwd, crew_agent, bool(private_memory_store_for_session(key))
        )
        provider.model_override = kwargs.get("model_override")
        made.append(provider)
        return provider

    def new_manager():
        sessions = session.SessionManager(KiroCrewConfig.load(), provider_factory=factory)
        managers.append(sessions)
        manager = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder())
        manager._ctx_builder.conversation_log = history
        manager._spawn_stagger_secs = 0
        return sessions, manager

    def enroll_collision():
        cfg = KiroCrewConfig.load()
        cfg.agents["worker"] = KiroCrewAgentConfig(kiro_agent="member-parent")
        peer_store = provision_member_memory(cfg, "worker")
        cfg.save()
        service = CapabilityService()
        request = {"revision": service.get("worker")["revision"], "enroll": True}
        preview = service.preview("worker", request)
        service.put("worker", {**request, "preview_token": preview["preview_token"]})
        prepared = prepare_member_capabilities("worker", project)
        assert prepared["template"] != "worker"
        return prepared["template"], peer_store

    def update_member():
        service = CapabilityService()
        request = {
            "revision": service.get("worker")["revision"],
            "operations": [
                {"section": "prompt", "id": "prompt", "action": "set", "value": "updated member"}
            ],
        }
        preview = service.preview("worker", request)
        service.put("worker", {**request, "preview_token": preview["preview_token"]})
        return prepare_member_capabilities("worker", project)["template"]

    return SimpleNamespace(
        new_manager=new_manager,
        enroll_collision=enroll_collision,
        made=made,
        parent=parent,
        store=store,
        project=str(project),
        history=history,
        update_member=update_member,
        specs=specs,
    )
