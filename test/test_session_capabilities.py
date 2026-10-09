"""Exercise capability adoption through the real manager with an external provider double."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes, _make_state, drain_background_tasks
from dashboard_owner_helpers import as_owner

from kiro_crew import agent, agent_discovery, agent_state
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_MEMBER_CAPABILITIES,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
)
from kiro_crew.agent_capabilities import CapabilityService, prepare_member_capabilities
from kiro_crew.config import loader as config_loader
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    refresh_materialized_agents,
)
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history
from kiro_crew.dashboard.handlers.agent_capabilities import _SERVICE
from kiro_crew.dashboard.routes.agents import register
from kiro_crew.history import ConversationLog
from kiro_crew.member_memory_auth import bind_private_session_store, read_private_session_store
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import provision_member_memory
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent, LLMProvider
from kiro_crew.session import SessionManager
from kiro_crew.session_agent_selection import session_agent_selection_kind
from kiro_crew.session_capabilities import CapabilityStartupError, runtime_view
from kiro_crew.skills import SkillsLoader


class FakeProvider(LLMProvider):
    """External harness: fresh process, saved template loading, controllable startup."""

    def __init__(self, key, template, cwd, specs, *, private=True):
        self.key = key
        self.template = template
        self._cwd = cwd
        self.specs = specs
        self.member_context = private
        self.incarnation = ""
        self.sid = ""
        self.active = ""
        self.supported = True
        self.fail = False
        self.after_start = None
        self.starts = 0
        self.stops = 0

    async def start(self):
        self.starts += 1
        if self.fail:
            raise RuntimeError("external startup failure")
        spec = await asyncio.to_thread(
            lambda: json.loads((self.specs / (self.template + ".json")).read_text())
        )
        self.active = spec["name"]
        self.incarnation = uuid.uuid4().hex
        self.sid = uuid.uuid4().hex
        if self.after_start:
            await self.after_start(self)

    async def shutdown(self):
        self.stops += 1
        self.incarnation = ""

    async def stream(self, message):
        if False:
            yield None

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
        return self.sid

    @property
    def process_instance(self):
        return self.incarnation

    @property
    def member_capabilities_supported(self):
        return self.supported

    @property
    def loaded_capability_template(self):
        return self.active


def save(service, member="A", *, enroll=False, prompt=None):
    request = {"revision": service.get(member)["revision"], "enroll": enroll}
    if prompt is not None:
        request["operations"] = [
            {"section": "prompt", "id": "prompt", "action": "set", "value": prompt}
        ]
    preview = service.preview(member, request)
    return service.put(member, {**request, "preview_token": preview["preview_token"]})


@pytest.fixture
def world(tmp_path, monkeypatch):
    home, specs, project = tmp_path / "home", tmp_path / "agents", tmp_path / "project"
    for path in (home, specs, project):
        path.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", specs)
    monkeypatch.setattr(agent_state, "_state_path", lambda: home / "agent_model_state.json")
    pass  # Member routing does not depend on OS isolation.
    cfg = KiroCrewConfig.load()
    cfg.workspaces[cfg.default_workspace] = WorkspaceConfig(dir=str(project))
    stores = {}
    for name in ("A", "B"):
        cfg.agents[name] = KiroCrewAgentConfig(kiro_agent="parent")
        stores[name] = provision_member_memory(cfg, name)
    cfg.session.pool_size = 1
    cfg.save()
    spec = {"name": "parent", "prompt": "original", "tools": [], "includeMcpJson": False}
    (specs / "parent.json").write_text(json.dumps(spec), encoding="utf-8")
    service = CapabilityService()
    log = ConversationLog()
    for name in stores:
        key = "dashboard:" + name
        bind_private_session_store(key, stores[name])
        log.update_metadata(key, {"agent": name, "memory_store": stores[name]})
        log.append(key, "user", "keep this history")
    made = []

    def factory(key, agent=None, cwd=None, **kwargs):
        provider = FakeProvider(key, agent, cwd, specs)
        made.append(provider)
        return provider

    return service, KiroCrewConfig.load(), factory, made, project, stores, log


@pytest.fixture
def dashboard_capability_world(world, tmp_path, monkeypatch):
    """Real owner selection, enrollment and allocation; no external harness or model."""
    # The real discovery refresh publishes these process-wide snapshots.
    # Register their restoration before enrollment or refresh can change them.
    for name in (
        "_MATERIALIZED_AGENTS",
        "_MATERIALIZED_AGENTS_READY",
        "_MATERIALIZED_AGENTS_GENERATION",
        "_MATERIALIZED_REFRESH_ISSUED",
        "_MATERIALIZED_REFRESH_APPLIED",
    ):
        monkeypatch.setattr(config_loader, name, getattr(config_loader, name))
    service, cfg, factory, made, project, stores, log = world
    cfg.default_agent = "A"
    cfg.session.pool_size = 0
    cfg.session.eager_spawn = True
    cfg.save()
    save(service, enroll=True)
    cfg = KiroCrewConfig.load()
    prepared = prepare_member_capabilities("A", str(project))
    template = "separate-template"
    specs = tmp_path / "agents"
    (specs / f"{template}.json").write_text(
        json.dumps(
            {"name": template, "prompt": "template only", "tools": [], "includeMcpJson": False}
        ),
        encoding="utf-8",
    )
    # Discovery reads the same real temporary specs as the capability service.
    monkeypatch.setattr(agent_discovery, "_KIRO_AGENTS_DIR", specs)
    monkeypatch.setattr("kiro_crew.config.loader.kiro_agents_dir", lambda: specs)
    refresh_materialized_agents()
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **kw: None)
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
    monkeypatch.setattr(chat_runner, "_armed_prefetches", {})
    monkeypatch.setattr(chat_runner, "_arm_generation", 0)
    monkeypatch.setattr(chat_runner, "_eager_spawn_sem", asyncio.Semaphore(1))
    states = []
    prompts = []

    def new_state(*, supported=True):
        def dashboard_factory(key, **kwargs):
            provider = factory(key, **kwargs)
            # The external double declares the isolation requested by this
            # fixture. Allocation still validates the real protected assignment.
            provider.member_context = key == "dashboard:A"
            provider.supported = supported

            async def stream(message, **stream_kwargs):
                prompts.append((provider, message))
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="The selected task is complete.")
                yield LLMEvent(kind=EVENT_COMPLETE)

            provider.stream = stream
            return provider

        builder = ContextBuilder(
            memory=MemoryStore(workspace=project),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        # Context generation and title models are independent of selection.
        # Memory ownership, selection publication and allocation remain real.
        builder.build_message = MagicMock(return_value=("task", None))
        builder.ensure_store = AsyncMock(return_value=object())
        state = _make_state(tmp_path, context_builder=builder)
        state.conversation_log = log
        state.sessions = SessionManager(cfg, provider_factory=dashboard_factory)
        states.append(state)
        return state

    return SimpleNamespace(
        new_state=new_state,
        states=states,
        made=made,
        prompts=prompts,
        prepared=prepared,
        template=template,
        project=project,
        stores=stores,
        log=log,
    )


async def _create_capability_dashboard_slot(state, name, selected, project):
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await asyncio.wait_for(
            client.post(
                "/api/chat/slots",
                json={"name": name, "agent": selected, "project": str(project)},
            ),
            15,
        )
        assert response.status == 200, await response.text()
    return state._slots[name]


async def _close_capability_dashboards(world):
    """Join dashboard writers and real managers while the fixture homes still exist."""
    try:
        for state in world.states:
            await asyncio.wait_for(drain_background_tasks(state), 15)
    finally:
        for state in world.states:
            await asyncio.wait_for(state.sessions.close_all(drain_timeout=0), 15)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["dispatch", "eager", "restore"])
@pytest.mark.parametrize("supported", [True, False], ids=["full_spec_host", "unsupported_host"])
async def test_dashboard_template_keeps_namespace_through_real_manager(
    dashboard_capability_world, entry, supported
):
    """An enrolled default cannot replace an explicitly selected provider template."""
    world = dashboard_capability_world
    state = world.new_state(supported=supported)
    key = "dashboard:template-chat"
    try:
        slot = await asyncio.wait_for(
            _create_capability_dashboard_slot(
                state, "template-chat", world.template, world.project
            ),
            20,
        )
        assert await asyncio.to_thread(session_agent_selection_kind, key, world.template) == (
            "template"
        )
        if entry == "restore":
            # Rehydration intentionally ignores empty newborn conversations.
            # Seed prior messages under the owner-created protected selection;
            # the transcript cannot manufacture that namespace itself.
            await asyncio.to_thread(world.log.append, key, "user", "An earlier template turn.")
            await asyncio.to_thread(world.log.append, key, "assistant", "Earlier template reply.")
            await asyncio.to_thread(
                world.log.update_metadata,
                key,
                {"agent": world.template, "project": str(world.project)},
            )
            state = world.new_state(supported=supported)
            slot = _rehydrate_slot_from_history(state, "template-chat")
            assert slot is not None
            assert slot.agent == world.template
        if entry == "eager":
            await asyncio.wait_for(chat_runner._eager_spawn(state, slot), 20)
        else:
            await asyncio.wait_for(chat_runner._run_chat(state, slot, "Run this template."), 20)
        await asyncio.wait_for(drain_background_tasks(state), 15)

        # Assert what the real allocation boundary gave the external process,
        # not just the agent argument that chat_runner gave SessionManager.
        assert [provider.template for provider in world.made] == [world.template], (
            f"{entry}: real manager substituted default A's enrolled generation "
            f"{world.prepared['template']!r} for selected template {world.template!r}"
        )
        provider = world.made[0]
        assert provider.active == world.template
        assert provider.starts == 1
        assert not provider.member_context
        assert await asyncio.to_thread(read_private_session_store, key) is None
        assert (
            state.sessions.capability_runtime_view("A", world.prepared["revision"])["sessions"]
            == []
        )
        if entry == "eager":
            assert world.prompts == []
            await asyncio.wait_for(chat_runner._run_chat(state, slot, "Use the warm template."), 20)
            await asyncio.wait_for(drain_background_tasks(state), 15)
            assert world.made == [provider]
            assert provider.starts == 1
        assert [item[0] for item in world.prompts] == [provider]
        assert await asyncio.to_thread(session_agent_selection_kind, key, world.template) == (
            "template"
        )
    finally:
        await _close_capability_dashboards(world)


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False], ids=["adopts_member", "refuses_unsupported"])
async def test_dashboard_enrolled_member_controls_through_real_manager(
    dashboard_capability_world, supported, caplog
):
    """Keeping template chats ordinary must not bypass real member adoption or refusal."""
    world = dashboard_capability_world
    state = world.new_state(supported=supported)
    key = "dashboard:A"
    try:
        slot = await asyncio.wait_for(
            _create_capability_dashboard_slot(state, "A", "A", world.project), 20
        )
        assert await asyncio.to_thread(session_agent_selection_kind, key, "A") == "member"
        await asyncio.wait_for(chat_runner._run_chat(state, slot, "Run the member."), 20)
        await asyncio.wait_for(drain_background_tasks(state), 15)
        assert len(world.made) == 1
        provider = world.made[0]
        assert provider.template == world.prepared["template"]
        assert provider.member_context is True
        assert await asyncio.to_thread(read_private_session_store, key) == world.stores["A"]
        view = state.sessions.capability_runtime_view("A", world.prepared["revision"])
        if supported:
            assert provider.active == world.prepared["template"]
            assert provider.starts == 1
            assert [item[0] for item in world.prompts] == [provider]
            assert view["status"] == "applied"
            assert [row["session_key"] for row in view["sessions"]] == [key]
        else:
            assert provider.starts == 0
            assert world.prompts == []
            assert not state.sessions.has_session(key)
            assert view["status"] == "failed"
            assert "capability_harness_unsupported" in caplog.text
    finally:
        await _close_capability_dashboards(world)


@pytest.mark.asyncio
async def test_new_runtime_adopts_and_busy_session_keeps_old_version(world):
    service, cfg, factory, made, project, stores, log = world
    await asyncio.to_thread(save, service, enroll=True)
    manager = SessionManager(cfg, provider_factory=factory)
    key = "dashboard:A"
    before = await asyncio.to_thread(log.recent, key)
    try:
        provider, is_new, resumed = await manager.get_or_create(key, agent="A", cwd=str(project))
        first = await asyncio.to_thread(prepare_member_capabilities, "A", project)
        assert is_new and not resumed
        assert manager.capability_runtime_view("A", first["revision"])["status"] == "applied"
        assert manager.get_agent(key) == "A"
        assert not manager.is_session_sharing_eligible(key)
        assert not manager.consume_needs_reinjection(key)
        await asyncio.to_thread(save, service, prompt="new prompt")
        latest = await asyncio.to_thread(prepare_member_capabilities, "A", project)
        assert first["revision"] != latest["revision"]
        view = manager.capability_runtime_view("A", latest["revision"])
        assert view["status"] == "pending" and view["sessions"][0]["busy"]
        assert provider.starts == 1 and provider.stops == 0
        manager.release(key)
        again, is_new, resumed = await manager.get_or_create(key, agent="A", cwd=str(project))
        assert again is provider and not is_new and not resumed
        assert len(made) == 1
        manager.release(key)
        await manager.reset(key)
        fresh, is_new, resumed = await manager.get_or_create(key, agent="A", cwd=str(project))
        assert fresh is not provider and is_new
        assert manager.capability_runtime_view("A", latest["revision"])["status"] == "applied"
        assert await asyncio.to_thread(read_private_session_store, key) == stores["A"]
        assert await asyncio.to_thread(log.recent, key) == before
        assert not manager._sessions[key].provider_switch_replay
        manager.release(key)
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["start", "mode", "unsupported", "old_process", "save_race"])
async def test_startup_failure_never_applies_and_real_retry_works(world, fault):
    service, cfg, factory, made, project, stores, log = world
    await asyncio.to_thread(save, service, enroll=True)

    def faulty_factory(*args, **kwargs):
        provider = factory(*args, **kwargs)
        if len(made) == 1:
            provider.fail = fault == "start"
            provider.supported = fault != "unsupported"
            if fault == "old_process":
                provider.incarnation = "already-running"

            async def change_at_start(p):
                if fault == "mode":
                    p.active = "parent"
                if fault == "save_race":
                    await asyncio.to_thread(save, service, prompt="racing save")

            provider.after_start = change_at_start
        return provider

    manager = SessionManager(cfg, provider_factory=faulty_factory)
    try:
        with pytest.raises(RuntimeError):
            await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        assert not manager.has_session("dashboard:A")
        latest = await asyncio.to_thread(prepare_member_capabilities, "A", project)
        assert manager.capability_runtime_view("A", latest["revision"])["status"] == "failed"
        provider, new, _ = await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        assert new and provider is made[-1]
        assert manager.capability_runtime_view("A", latest["revision"])["status"] == "applied"
        assert await asyncio.to_thread(read_private_session_store, "dashboard:A") == stores["A"]
        manager.release("dashboard:A")
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
async def test_runtime_status_tracks_process_and_handle_identity(world):
    service, cfg, factory, made, project, _, _ = world
    await asyncio.to_thread(save, service, enroll=True)
    manager = SessionManager(cfg, provider_factory=factory)
    try:
        provider, _, _ = await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        prepared = await asyncio.to_thread(prepare_member_capabilities, "A", project)
        original_process, original_sid = provider.incarnation, provider.sid
        provider.incarnation = "replacement-process"
        assert manager.capability_runtime_view("A", prepared["revision"])["status"] == "unverified"
        provider.incarnation = original_process
        provider.sid = "new-handle-on-old-process"
        assert manager.capability_runtime_view("A", prepared["revision"])["status"] == "unverified"
        provider.sid = original_sid
        assert manager.capability_runtime_view("A", prepared["revision"])["status"] == "applied"
        manager._sessions["dashboard:A"].adopt_provider(provider)
        assert manager.capability_runtime_view("A", prepared["revision"])["status"] == "pending"
        manager.release("dashboard:A")
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
async def test_same_parent_members_get_distinct_runtime_versions(world):
    service, cfg, factory, made, project, stores, _ = world
    await asyncio.to_thread(save, service, "A", enroll=True)
    await asyncio.to_thread(save, service, "B", enroll=True, prompt="B prompt")
    manager = SessionManager(cfg, provider_factory=factory)
    try:
        for member in ("A", "B"):
            key = "dashboard:" + member
            await manager.get_or_create(key, agent=member, cwd=str(project))
            manager.release(key)
            prepared = await asyncio.to_thread(prepare_member_capabilities, member, project)
            view = manager.capability_runtime_view(member, prepared["revision"])
            assert view["status"] == "applied"
            assert [row["session_key"] for row in view["sessions"]] == [key]
            assert await asyncio.to_thread(read_private_session_store, key) == stores[member]
        assert made[0].template != made[1].template
        assert made[0].incarnation != made[1].incarnation
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
async def test_actual_runtime_cwd_must_match_saved_project(world):
    service, cfg, factory, _, project, _, _ = world
    await asyncio.to_thread(save, service, enroll=True)

    def wrong_cwd_factory(*args, **kwargs):
        provider = factory(*args, **kwargs)
        provider._cwd = str(project.parent)
        return provider

    manager = SessionManager(cfg, provider_factory=wrong_cwd_factory)
    try:
        with pytest.raises(CapabilityStartupError, match="cwd_changed"):
            await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        assert not manager.has_session("dashboard:A")
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
async def test_enrolled_member_without_cwd_uses_its_configured_workspace(world):
    service, cfg, factory, _, project, _, _ = world
    await asyncio.to_thread(save, service, enroll=True)
    manager = SessionManager(cfg, provider_factory=factory)
    try:
        provider, _, _ = await manager.get_or_create("dashboard:A", agent="A")
        assert Path(provider.cwd) == project
        prepared = await asyncio.to_thread(prepare_member_capabilities, "A", project)
        assert manager.capability_runtime_view("A", prepared["revision"])["status"] == "applied"
        manager.release("dashboard:A")
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
async def test_private_task_uses_dedicated_provider_and_same_store(world):
    service, cfg, factory, _, project, stores, log = world
    await asyncio.to_thread(save, service, enroll=True)
    key = "taskrunner:capability-step"
    await asyncio.to_thread(bind_private_session_store, key, stores["A"])
    await asyncio.to_thread(log.update_metadata, key, {"memory_store": stores["A"]})
    manager = SessionManager(cfg, provider_factory=factory)
    try:
        provider, new, _ = await manager.open_task_session(
            "dashboard:A", key, agent="A", cwd=str(project)
        )
        prepared = await asyncio.to_thread(prepare_member_capabilities, "A", project)
        assert new and provider.loaded_capability_template == prepared["template"]
        assert manager.capability_runtime_view("A", prepared["revision"])["status"] == "applied"
        assert not manager._subagent_runtimes
        assert await asyncio.to_thread(read_private_session_store, key) == stores["A"]
        manager.release(key)
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
async def test_governance_change_during_startup_is_not_applied(world):
    from kiro_crew.platform.context import _install, current_context

    service, cfg, factory, _, project, _, _ = world
    await asyncio.to_thread(save, service, enroll=True)

    def racing_factory(*args, **kwargs):
        provider = factory(*args, **kwargs)

        async def install_new_generation(_):
            _install(current_context(), notify=False)

        provider.after_start = install_new_generation
        return provider

    manager = SessionManager(cfg, provider_factory=racing_factory)
    try:
        with pytest.raises(RuntimeError):
            await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        assert not manager.has_session("dashboard:A")
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_real_provider_support_is_explicit_and_unstarted_is_unverified(tmp_path, backend):
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider(work_dir=tmp_path, acp_backend=backend)
    assert provider.member_capabilities_supported is (backend in ACP_BACKENDS_MEMBER_CAPABILITIES)
    assert provider.loaded_capability_template == ""


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN) + ["byo-harness"])
def test_real_session_provider_member_support_is_explicit(tmp_path, backend):
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
    from kiro_crew.acp.session_provider import AcpSessionProvider

    runtime = AcpRuntime(work_dir=tmp_path, acp_backend=backend)
    handle = AcpSessionHandle("member", asyncio.Queue(), runtime, watchdog=WatchdogSettings())
    provider = AcpSessionProvider(handle, runtime, owns_runtime=True)
    assert provider.member_capabilities_supported is (backend in ACP_BACKENDS_MEMBER_CAPABILITIES)
    assert provider.loaded_capability_template == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("fault", [None, "pre", "wire", "post"])
async def test_real_acp_mode_ack_is_required_for_loaded_template(
    tmp_path, monkeypatch, resume, fault
):
    from unittest.mock import AsyncMock, MagicMock, Mock

    from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.acp.session_provider import AcpSessionProvider

    handles = []

    def capture_handle(*args, **kwargs):
        handle = AcpSessionHandle(*args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr("kiro_crew.acp.runtime.AcpSessionHandle", capture_handle)
    pre = Mock(wraps=agent.require_fresh_derived_spec)
    post = Mock(wraps=agent.require_unchanged_derived_spec)
    if fault == "pre":
        pre.side_effect = agent.DerivedSpecStale("pre-check failure")
    if fault == "post":
        post.side_effect = agent.DerivedSpecStale("post-check failure")
    monkeypatch.setattr(agent, "require_fresh_derived_spec", pre)
    monkeypatch.setattr(agent, "require_unchanged_derived_spec", post)

    template = "member-generation"
    runtime = AcpRuntime(work_dir=tmp_path, agent=template, expect_mcp_reports=False)
    reader = asyncio.StreamReader()
    process = MagicMock(returncode=None)
    process.stdout = reader
    process.stdin.drain = AsyncMock()
    runtime._process = process
    runtime._initialized = True
    runtime._process_instance = "fresh-incarnation"
    methods = []

    def answer(raw):
        request = json.loads(raw)
        methods.append(request["method"])
        result = {}
        if request["method"] in ("session/new", "session/load"):
            result = {
                "sessionId": "native-history-id",
                "modes": {
                    "currentModeId": "old-mode",
                    "availableModes": [{"id": template}],
                },
            }
        response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        if fault == "wire" and request["method"] == "session/set_mode":
            response.pop("result")
            response["error"] = {"code": -32603, "message": "wire failure"}
        reader.feed_data((json.dumps(response) + "\n").encode())

    process.stdin.write.side_effect = answer
    runtime._can_load_session = True
    pump = asyncio.create_task(runtime._reader_loop())
    try:
        start = (
            runtime.load_session(str(tmp_path / "native.json"), "native-history-id", agent=template)
            if resume
            else runtime.create_session(agent=template)
        )
        if fault:
            with pytest.raises(AcpRuntimeError, match="failure"):
                await asyncio.wait_for(start, 5)
            assert len(handles) == 1
            handle = handles[0]
            dedicated = AcpSessionProvider(handle, runtime, owns_runtime=True)
            assert runtime.is_alive()  # No stamp even while the owning process survives.
            assert handle.active_agent != template
            assert dedicated.loaded_capability_template == ""
            assert handle.session_id not in runtime._session_queues
            assert methods == [
                "session/load" if resume else "session/new",
                *([] if fault == "pre" else ["session/set_mode"]),
                "_kiro.dev/session/terminate",
            ]
            pre.assert_called_once_with(template, tmp_path)
            assert post.call_count == (1 if fault == "post" else 0)
            return
        handle = await asyncio.wait_for(start, 5)
        dedicated = AcpSessionProvider(handle, runtime, owns_runtime=True)
        shared = AcpSessionProvider(handle, runtime)
        assert "session/set_mode" in methods
        assert dedicated.loaded_capability_template == template
        assert shared.loaded_capability_template == ""
        await asyncio.wait_for(handle.set_mode("another-mode"), 5)
        assert dedicated.loaded_capability_template == ""
    finally:
        reader.feed_eof()
        await asyncio.wait_for(pump, 5)


@pytest.mark.asyncio
async def test_warm_process_is_not_claimed_for_enrolled_member(world):
    import time

    service, cfg, factory, made, project, _, _ = world
    await asyncio.to_thread(save, service, enroll=True)
    manager = SessionManager(cfg, provider_factory=factory)
    warm = FakeProvider("", "parent", str(project), project.parent / "agents", private=False)
    await warm.start()
    await manager._warm_pool.put((warm, time.monotonic()))
    try:
        provider, new, _ = await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        assert new and provider is not warm
        assert manager._warm_pool.qsize() == 1
        assert warm.starts == 1 and warm.stops == 0
        manager.release("dashboard:A")
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
async def test_saved_bytes_changed_during_start_never_get_a_stamp(world):
    from kiro_crew.agent_capabilities import CapabilityError

    service, cfg, factory, made, project, _, _ = world
    await asyncio.to_thread(save, service, enroll=True)

    def tampering_factory(*args, **kwargs):
        provider = factory(*args, **kwargs)

        async def change_bytes(p):
            path = p.specs / (p.template + ".json")

            def rewrite():
                spec = json.loads(path.read_text())
                spec["prompt"] = "out-of-band replacement"
                path.write_text(json.dumps(spec), encoding="utf-8")

            await asyncio.to_thread(rewrite)

        provider.after_start = change_bytes
        return provider

    manager = SessionManager(cfg, provider_factory=tampering_factory)
    try:
        with pytest.raises(CapabilityError, match="materialization_changed"):
            await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        assert not manager.has_session("dashboard:A")
        assert manager.capability_runtime_view("A", "unknown")["status"] == "failed"
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
async def test_parent_update_reconciles_only_for_new_runtime(world):
    service, cfg, factory, _, project, stores, log = world
    await asyncio.to_thread(save, service, enroll=True)
    manager = SessionManager(cfg, provider_factory=factory)
    try:
        old, _, _ = await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        manager.release("dashboard:A")
        old_file = old.specs / (old.template + ".json")
        old_bytes = await asyncio.to_thread(old_file.read_bytes)

        def edit_parent():
            path = old.specs / "parent.json"
            spec = json.loads(path.read_text())
            spec["prompt"] = "ordinary upstream update"
            path.write_text(json.dumps(spec), encoding="utf-8")

        await asyncio.to_thread(edit_parent)
        same, new, _ = await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        assert same is old and not new
        manager.release("dashboard:A")
        assert await asyncio.to_thread(old_file.read_bytes) == old_bytes
        key = "dashboard:A-new-runtime"
        await asyncio.to_thread(bind_private_session_store, key, stores["A"])
        await asyncio.to_thread(log.update_metadata, key, {"memory_store": stores["A"]})
        fresh, new, _ = await manager.get_or_create(key, agent="A", cwd=str(project))
        assert new and fresh.template != old.template
        assert await asyncio.to_thread(old_file.read_bytes) == old_bytes
        latest = await asyncio.to_thread(prepare_member_capabilities, "A", project)
        view = manager.capability_runtime_view("A", latest["revision"])
        assert [row["status"] for row in view["sessions"]] == ["pending", "applied"]
        assert old.stops == 0
        manager.release(key)
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
async def test_http_reports_observed_runtime_without_applying_preview(world):
    service, cfg, factory, _, project, _, _ = world
    manager = SessionManager(cfg, provider_factory=factory)

    @web.middleware
    async def identity(request, handler):
        request["user"] = "owner"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(owner_id="owner", sessions=manager, push_refresh=lambda _: None)
    app[_SERVICE] = service
    register(app)
    endpoint = "/api/agents/A/capabilities"
    try:
        async with TestClient(TestServer(app)) as client:
            current = await (await client.get(endpoint)).json()
            body = {"revision": current["revision"], "enroll": True}
            response = await client.post(endpoint + "/preview", json=body)
            assert response.status == 200
            preview = await response.json()
            response = await client.put(
                endpoint, json={**body, "preview_token": preview["preview_token"]}
            )
            assert response.status == 200
            saved = await response.json()
            assert saved["runtime"]["status"] == "pending"
            provider, _, _ = await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
            current = await (await client.get(endpoint)).json()
            assert current["runtime"]["status"] == "applied"
            assert "warnings" not in current
            assert set(current["runtime"]) == {"status", "saved_revision", "sessions"}
            assert current["runtime"]["sessions"][0]["busy"] is True
            body = {
                "revision": current["revision"],
                "operations": [
                    {"section": "prompt", "id": "prompt", "action": "set", "value": "draft"}
                ],
            }
            response = await client.post(endpoint + "/preview", json=body)
            assert response.status == 200
            preview = await response.json()
            assert preview["runtime"]["status"] != "applied"
            unchanged = await (await client.get(endpoint)).json()
            assert unchanged["runtime"]["status"] == "applied"
            assert unchanged["revision"] == current["revision"]
            response = await client.put(
                endpoint, json={**body, "preview_token": preview["preview_token"]}
            )
            assert response.status == 200
            newer = await response.json()
            assert newer["runtime"]["status"] == "pending"
            assert newer["runtime"]["saved_revision"] != current["runtime"]["saved_revision"]
            assert provider.starts == 1 and provider.stops == 0
            manager.release("dashboard:A")
            await manager.reset("dashboard:A")
            fresh, _, _ = await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
            observed = await (await client.get(endpoint)).json()
            assert observed["runtime"]["status"] == "applied"
            assert observed["runtime"]["saved_revision"] == newer["runtime"]["saved_revision"]

            def change_saved_bytes():
                path = fresh.specs / (fresh.template + ".json")
                spec = json.loads(path.read_text(encoding="utf-8"))
                spec["prompt"] = "outside the published version"
                path.write_text(json.dumps(spec), encoding="utf-8")

            await asyncio.to_thread(change_saved_bytes)
            broken = await (await client.get(endpoint)).json()
            assert broken["runtime"]["status"] == "failed"
            assert broken["runtime"]["error_code"] == "materialization_changed"
            manager.release("dashboard:A")
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
async def test_runtime_application_requires_mcp_registration_evidence(world, monkeypatch):
    service, cfg, factory, _, project, _, _ = world

    def enroll_with_connection():
        request = {
            "revision": service.get("A")["revision"],
            "enroll": True,
            "operations": [
                {
                    "section": "mcpServers",
                    "id": "docs",
                    "action": "set",
                    "value": {"command": "example-mcp"},
                }
            ],
        }
        preview = service.preview("A", request)
        service.put("A", {**request, "preview_token": preview["preview_token"]})

    await asyncio.to_thread(enroll_with_connection)
    manager = SessionManager(cfg, provider_factory=factory)
    try:
        provider, _, _ = await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        prepared = await asyncio.to_thread(prepare_member_capabilities, "A", project)
        revision = prepared["revision"]
        assert manager.capability_runtime_view("A", revision)["status"] == "unverified"
        report = McpSessionReport()
        report.begin_session([])
        monkeypatch.setattr(provider, "mcp_session_report", lambda: report)
        report.record_event(EVENT_MCP_SERVER_INITIALIZED, "unrelated")
        assert manager.capability_runtime_view("A", revision)["status"] == "unverified"
        report.record_event(EVENT_MCP_SERVER_INIT_FAILURE, "docs", "private startup details")
        failed = manager.capability_runtime_view("A", revision)
        assert failed["status"] == "failed"
        assert failed["sessions"][0]["error_code"] == "capability_mcp_failed"
        assert "private startup details" not in json.dumps(failed)
        report.record_event(EVENT_MCP_OAUTH_REQUEST, "docs")
        assert manager.capability_runtime_view("A", revision)["status"] == "pending"
        report.record_event(EVENT_MCP_SERVER_INITIALIZED, "docs")
        assert manager.capability_runtime_view("A", revision)["status"] == "applied"
        report.record_unresolved_refs(["@missing"])
        assert manager.capability_runtime_view("A", revision)["status"] == "failed"
        assert provider.starts == 1 and provider.stops == 0
        manager.release("dashboard:A")
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["accepted", "overrides"])
async def test_cold_start_never_publishes_malformed_persisted_transport(world, field):
    service, cfg, factory, made, project, _, _ = world

    def prepare_corruption():
        specs = project.parent / "agents"
        parent_path = specs / "parent.json"
        parent = json.loads(parent_path.read_text())
        parent["mcpServers"] = {"search": {"command": "search", "args": []}}
        parent_path.write_text(json.dumps(parent))
        save(service, enroll=True)
        current = KiroCrewConfig.load().agents["A"].kiro_agent
        path = agent_state._state_path()
        state = json.loads(path.read_text())
        broken = {"command": []}
        state[current]["capabilities"][field]["mcpServers"]["search"] = (
            broken if field == "accepted" else {"action": "set", "value": broken}
        )
        path.write_text(json.dumps(state))
        return (
            path,
            path.read_bytes(),
            (path.parent / "config.json").read_bytes(),
            {p: p.read_bytes() for p in specs.glob("*.json")},
        )

    state_path, state_before, config_before, specs_before = await asyncio.to_thread(
        prepare_corruption
    )
    manager = SessionManager(cfg, provider_factory=factory)
    try:
        with pytest.raises(CapabilityStartupError, match="^capability_state_unreadable$"):
            await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        assert not manager.has_session("dashboard:A")
        assert made == []

        def unchanged():
            assert state_path.read_bytes() == state_before
            assert (state_path.parent / "config.json").read_bytes() == config_before
            assert {
                p: p.read_bytes() for p in (project.parent / "agents").glob("*.json")
            } == specs_before

        await asyncio.to_thread(unchanged)
    finally:
        await manager.close_all(drain_timeout=0)


def test_capability_runtime_facade_projects_owned_state_without_exporting_it():
    from kiro_crew.session_allocation import SessionRegistryState

    state = SessionRegistryState()
    attempt = {
        "member": "A",
        "status": "failed",
        "saved_revision": "saved",
        "error_code": "capability_startup_failed",
    }
    state.capability_failures.update({"failed": dict(attempt), "live": dict(attempt)})
    state.sessions["live"] = SimpleNamespace(
        capability_member="A",
        loaded_capabilities=None,
        provider=object(),
        semaphore=asyncio.Semaphore(1),
    )
    state.capability_failures["other"] = {**attempt, "member": "B"}
    manager = object.__new__(SessionManager)
    manager._allocation_state = state
    view = manager.capability_runtime_view("A", "saved")
    assert view == runtime_view(state, "A", "saved")
    assert view["status"] == "failed"
    assert [(row["session_key"], row["status"]) for row in view["sessions"]] == [
        ("live", "pending"),
        ("failed", "failed"),
    ]
    view["sessions"][1]["error_code"] = "changed by caller"
    view["sessions"].clear()
    assert state.capability_failures["failed"] == attempt
    assert set(state.sessions) == {"live"}
    assert manager.capability_runtime_view("absent", "")["status"] == "unverified"
    assert manager.capability_runtime_view("absent", "saved")["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("fault", [None, "withheld", "changed", "wire", "state"])
async def test_claude_saved_projection_requires_consumed_matching_spec(
    world, monkeypatch, resume, fault
):
    from kiro_crew.acp.client import AcpError
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.session_capabilities import loaded_stamp, prepare_runtime, verify_saved

    service, _, _, _, project, _, _ = world
    await asyncio.to_thread(save, service, enroll=True)
    prepared = await asyncio.to_thread(prepare_runtime, "A", "A", str(project))
    provider = AcpProvider(
        work_dir=project, agent=prepared.template, acp_backend=ACP_BACKEND_CLAUDE
    )
    client = provider.client
    client.member_context = True
    client._process = MagicMock(returncode=None)
    client._process_instance = "claude-incarnation"
    client._claude_settings_authored = fault != "withheld"
    client._session_mcp_cache = await asyncio.to_thread(client._resolve_session_mcp_servers)
    if fault == "changed":
        client._session_agent_spec = {**client._session_agent_spec, "prompt": "not saved"}
    if resume:
        client._resume_session_id = "claude-history"
    sent = []

    async def send(method, params):
        sent.append((method, params))
        return len(sent)

    async def answer(request_id, **kwargs):
        method = sent[request_id - 1][0]
        if method == "initialize":
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
        if fault == "wire":
            raise AcpError("session creation refused")
        return {"sessionId": "claude-history", "modes": {"currentModeId": "default"}}

    monkeypatch.setattr(client, "_send_request", send)
    monkeypatch.setattr(client, "_wait_for_response", answer)
    for method in (
        "_persist_advertised_models_if_changed",
        "_apply_startup_model",
        "_pin_claude_starting_mode",
        "_drain_notifications",
    ):
        monkeypatch.setattr(client, method, AsyncMock())

    async def reseed():
        client._invalidate_session_mcp_projection()

    monkeypatch.setattr(client, "_reseed_after_capture", reseed)
    if fault == "state":
        monkeypatch.setattr(
            agent_state, "get_capabilities", MagicMock(side_effect=ValueError("unreadable state"))
        )
    assert provider.loaded_capability_template == ""
    if fault == "wire":
        # Session creation itself refuses, so there is no session to confirm.
        with pytest.raises(AcpError):
            await asyncio.wait_for(client._initialize_session(), 5)
        assert provider.loaded_capability_template == ""
        with pytest.raises(CapabilityStartupError, match="unverified"):
            loaded_stamp(provider, prepared)
        return
    await asyncio.wait_for(client._initialize_session(), 5)
    assert sent[1][0] == ("session/load" if resume else "session/new")
    if fault:
        # The session exists and holds the array; the projection it consumed is what
        # refuses it -- the refusal is the confirmation's, not a failed session start.
        with pytest.raises(AcpError):
            await client.confirm_member_projection()
        assert provider.loaded_capability_template == ""
        with pytest.raises(CapabilityStartupError, match="unverified"):
            loaded_stamp(provider, prepared)
        return
    await client.confirm_member_projection()
    assert "mcpServers" in sent[1][1]
    assert provider.loaded_capability_template == prepared.template
    await asyncio.to_thread(verify_saved, prepared, str(project))
    assert loaded_stamp(provider, prepared).revision == prepared.revision
    assert provider.capability_projection_gaps == ("native_tools",)
    client._process.returncode = 0
    assert provider.loaded_capability_template == ""
    client._process.returncode = None
    client._invalidate_session_mcp_projection()
    assert provider.loaded_capability_template == ""


def _codex_session_provider(template: str, consumed_spec, *, alive: bool = True):
    """An AcpSessionProvider on a codex runtime, with the spec its array consumed.

    The handle carries the runtime's confirmation result fields with their
    defaults, the way a fresh ``AcpSessionHandle`` does before the mirrored arm
    confirms anything.
    """
    from kiro_crew.acp.session_provider import AcpSessionProvider

    runtime = MagicMock()
    runtime.acp_backend = ACP_BACKEND_CODEX
    runtime._agent = template
    runtime.is_alive.return_value = alive
    handle = MagicMock(
        consumed_agent_spec=consumed_spec,
        confirmed_projection_template="",
        capability_projection_gaps=(),
    )
    return AcpSessionProvider(handle, runtime, owns_runtime=True)


def _codex_runtime():
    """An AcpRuntime shaped like a member's codex runtime, without spawning it."""
    from kiro_crew.acp.runtime import AcpRuntime

    runtime = AcpRuntime.__new__(AcpRuntime)
    runtime._member_context = True
    runtime._agent = "saved-member"
    return runtime


def _codex_handle(consumed_spec):
    """A handle shaped like a fresh codex session, before any confirmation."""
    return MagicMock(
        session_id="codex-session",
        consumed_agent_spec=consumed_spec,
        confirmed_projection_template="",
        capability_projection_gaps=(),
    )


@pytest.mark.asyncio
async def test_codex_member_confirms_on_the_shared_runtime(monkeypatch):
    """Codex runs on the shared AcpRuntime path. The runtime confirms the member's
    consumed spec on the mirrored arm, and records the saved template and its
    projection gaps on the session handle; before confirmation the handle reports
    none, so loaded_stamp() refuses."""
    from kiro_crew.agent_capabilities import _digest

    spec = {"name": "saved-member", "tools": ["*"], "hooks": {"x": 1}}
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _: {"materialized": _digest(spec)})
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_MEMBER_CAPABILITIES
    handle = _codex_handle(spec)
    await _codex_runtime()._confirm_member_projection(handle)
    assert handle.confirmed_projection_template == "saved-member"
    assert handle.capability_projection_gaps == ("hooks",)


def test_codex_provider_reports_the_handle_confirmation(monkeypatch):
    """The provider answers the Capabilities pane from the handle the runtime
    confirmed, so the confirmation belongs to the session it checked: a warm
    worker's fresh conversation, built from a spec edited in between, does not
    inherit the old session's result."""
    from kiro_crew.agent_capabilities import _digest

    spec = {"name": "saved-member", "tools": ["*"], "hooks": {"x": 1}}
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _: {"materialized": _digest(spec)})
    provider = _codex_session_provider("saved-member", spec)
    assert provider.loaded_capability_template == ""
    provider._handle.confirmed_projection_template = "saved-member"
    provider._handle.capability_projection_gaps = ("hooks",)
    assert provider.loaded_capability_template == "saved-member"
    assert provider.capability_projection_gaps == ("hooks",)


def test_kiro_member_start_takes_no_projection_step():
    """Member confirmation belongs behind the existing adapter-only routing gate (H13)."""
    import inspect

    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.providers.acp import AcpProvider

    for method in (
        AcpProvider._start_kiro_runtime_impl,
        AcpRuntime.create_session,
        AcpRuntime._finish_create_session,
        AcpRuntime.load_session,
    ):
        source = inspect.getsource(method)
        assert "_confirm_member_projection" not in source
        assert "_member_projection_needs_confirmation" not in source
    routing = inspect.getsource(AcpSessionHandle.apply_session_permission_routing)
    assert routing.index("return") < routing.index("_confirm_member_projection")
    assert "self.mirror_used" in routing


def test_the_kiro_construction_path_carries_no_member_confirmation():
    """harness-parity H13: the shared construction path gains no member step.

    The confirmation is called from the adapter-only arm of the client startup path,
    after the session exists -- NOT from a step of ``AcpClient._initialize_session``,
    which every Kiro knowledge-worker session walks. So the shared derived-spec step
    keeps its original callback and no line Kiro executes changes at all. Asserted on
    the source, because a call that returned early and a guard that was never entered
    leave the same session behind.
    """
    import ast
    import inspect
    import textwrap

    from kiro_crew.acp.client import AcpClient
    from kiro_crew.providers.acp import AcpProvider

    init = inspect.getsource(AcpClient._initialize_session)
    assert "_check_consumed_session_spec" not in init
    assert "_confirm_member_projection" not in init
    assert "ACP_BACKENDS_MEMBER_CAPABILITIES" not in init
    assert "await asyncio.to_thread(require_unchanged_derived_spec, sent_snapshot)" in init

    # The confirmation sits on ONE arm of the provider's existing backend split: the
    # legacy AcpClient path. The Kiro/KAS arm above it reaches none of it.
    tree = ast.parse(textwrap.dedent(inspect.getsource(AcpProvider.start)))
    split = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Attribute)
        and node.test.attr == "is_acp_runtime_backend"
    ]
    assert len(split) == 1, "the provider's backend split is the arm this rides"
    client_arm = split[0].orelse
    calls = [
        node
        for stmt in client_arm
        for node in ast.walk(stmt)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "confirm_member_projection"
    ]
    assert len(calls) == 1, (
        "the member confirmation must be called from the adapter-only arm of the "
        "client startup path, once, after ensure_ready; found "
        f"{len(calls)}"
    )
    # And nothing on the runtime arm names it.
    for stmt in split[0].body:
        names = {
            node.func.attr
            for node in ast.walk(stmt)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "confirm_member_projection" not in names

    # The same adapter-only arm re-arms the gate on the turn path, where the direct
    # client can respawn into a NEW, unconfirmed session.
    stream_tree = ast.parse(textwrap.dedent(inspect.getsource(AcpProvider.stream)))
    rearmed = [
        node
        for node in ast.walk(stream_tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Call)
        and getattr(node.test.func, "id", "") == "isinstance"
        for stmt in node.body
        for node in ast.walk(stmt)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "confirm_member_projection"
    ]
    assert len(rearmed) == 1, (
        "the turn path's client arm must re-arm the member gate, or a session that "
        f"respawned on that turn runs unconfirmed; found {len(rearmed)}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend, member, session_id, confirms",
    [
        (ACP_BACKEND_KIRO, True, "s", False),
        (ACP_BACKEND_CLAUDE, False, "s", False),
        (ACP_BACKEND_CLAUDE, True, "", False),
        (ACP_BACKEND_CLAUDE, True, "s", True),
        (ACP_BACKEND_DEEPSEEK, True, "s", True),
    ],
)
async def test_the_member_confirmation_covers_only_a_live_member_session(
    tmp_path, backend, member, session_id, confirms
):
    """The call is a no-op for everything that projects nothing: a non-member, a kiro
    member (which loads its spec natively, with no array to confirm) and a session
    that was never created all reach no confirmation. Claude and deepseek are the
    projecting harnesses this client drives, so they do."""
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    client.member_context = member
    client._session_id = session_id
    client._confirm_member_projection = MagicMock()
    await client.confirm_member_projection()
    assert client._confirm_member_projection.called is confirms


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend, client_arm",
    [
        (ACP_BACKEND_CLAUDE, True),
        (ACP_BACKEND_DEEPSEEK, True),
        (ACP_BACKEND_KIRO, False),
    ],
)
async def test_the_provider_confirms_from_its_client_arm_only(
    tmp_path, monkeypatch, backend, client_arm
):
    """The wiring the H13 placement rests on: the confirmation runs from the legacy
    client arm of ``AcpProvider.start`` -- AFTER the session exists, so the caller's
    post-start ``loaded_stamp`` read sees it -- and the runtime arm (kiro, KAS, codex)
    never reaches the call at all."""
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider(work_dir=tmp_path, agent="saved-member", acp_backend=backend)
    provider.memory_mode = "persistent"
    monkeypatch.setattr(provider, "_apply_effort_overlay", lambda: None)
    monkeypatch.setattr(provider, "_apply_tool_search_overlay", lambda: None)
    monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
    monkeypatch.setattr(provider, "_apply_initial_effort", AsyncMock())
    provider._client.ensure_ready = AsyncMock()
    provider._client.confirm_member_projection = AsyncMock()

    await provider.start()

    if client_arm:
        provider._client.confirm_member_projection.assert_awaited_once_with()
    else:
        provider._client.confirm_member_projection.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_confirmed_session_is_not_judged_again(tmp_path):
    """A start that re-enters on the same live session must not re-judge it: the saved
    intent can move after the session was created, and that cannot retroactively
    unmake what the host already consumed."""
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client.member_context = True
    client._session_id = "claude-live"
    client._confirm_member_projection = MagicMock()

    await client.confirm_member_projection()
    await client.confirm_member_projection()

    assert client._confirm_member_projection.call_count == 1
    # A new session is judged afresh.
    client._session_id = "claude-next"
    await client.confirm_member_projection()
    assert client._confirm_member_projection.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
@pytest.mark.parametrize("mirror_used", [False, True])
@pytest.mark.parametrize("member_context", [False, True])
async def test_member_confirmation_uses_adapter_routing_seam(
    monkeypatch, backend, mirror_used, member_context
):
    from kiro_crew import acp_tool_gate
    from kiro_crew.acp.session_handle import AcpSessionHandle

    runtime = _codex_runtime()
    runtime._acp_backend = backend
    runtime._member_context = member_context
    runtime._confirm_member_projection = AsyncMock()
    handle = _codex_handle(None)
    handle._runtime = runtime
    handle.mirror_used = mirror_used
    handle.set_config_option = AsyncMock()
    monkeypatch.setattr(acp_tool_gate, "session_config_issue", lambda *_: None)

    await AcpSessionHandle.apply_session_permission_routing(handle)

    if backend == ACP_BACKEND_CODEX and mirror_used and member_context:
        runtime._confirm_member_projection.assert_awaited_once_with(handle)
    else:
        runtime._confirm_member_projection.assert_not_awaited()


def test_member_confirmation_gate_follows_the_mirrored_array():
    """The gate asks whether a MIRROR built this session's array and whether the
    session is a member's -- never whether the array carried a derived spec."""
    runtime = _codex_runtime()
    assert runtime._member_projection_needs_confirmation(True) is True
    assert runtime._member_projection_needs_confirmation(False) is False
    runtime._member_context = False
    assert runtime._member_projection_needs_confirmation(True) is False


@pytest.mark.asyncio
async def test_non_derived_member_is_confirmed_and_starts(world):
    """A member's own template is not derived, so ``require_fresh_derived_spec``
    answers None for it while the mirror still builds that session's array from the
    template. The confirmation follows the ARRAY's origin, so this member is
    confirmed and its start stamps cleanly; gating it on the derived snapshot left
    ``confirmed_projection_template`` empty and ``loaded_stamp`` refused the start."""
    from kiro_crew.agent import require_fresh_derived_spec
    from kiro_crew.providers.mirrors.registry import mirror_for
    from kiro_crew.session_capabilities import loaded_stamp, prepare_runtime

    service, _, _, _, project, _, _ = world
    await asyncio.to_thread(save, service, enroll=True)
    prepared = await asyncio.to_thread(prepare_runtime, "A", "A", str(project))
    assert prepared.template
    snapshot = await asyncio.to_thread(require_fresh_derived_spec, prepared.template, str(project))
    assert snapshot is None, "the member template is derived -- this defect needs a non-derived one"
    projection = await asyncio.to_thread(
        mirror_for(ACP_BACKEND_CODEX).session_projection, prepared.template, work_dir=project
    )
    consumed = projection.agent_spec
    assert consumed is not None
    assert projection.derived_spec_snapshot is None

    provider = _codex_session_provider(prepared.template, consumed)
    runtime = _codex_runtime()
    runtime._agent = prepared.template
    # The bracket this bug came from: the derived-spec gate answers "nothing to
    # check" for this session, which is why the confirmation skipped it.
    assert runtime._mirrored_spec_check_needed(projection.derived_spec_snapshot) is False

    # Before confirmation the session stands exactly where the finding left it.
    with pytest.raises(CapabilityStartupError, match="unverified"):
        loaded_stamp(provider, prepared)

    assert runtime._member_projection_needs_confirmation(True) is True
    await runtime._confirm_member_projection(provider._handle)
    assert provider.loaded_capability_template == prepared.template
    assert loaded_stamp(provider, prepared).revision == prepared.revision


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [None, {"name": "saved-member", "tools": []}])
async def test_codex_member_refused_when_consumed_spec_differs(monkeypatch, consumed):
    """No consumed spec (array not built from a mirror) or a different one (the file
    changed after the intent was saved) both refuse: the session never reports the
    saved template, so the member cannot be claimed as running on codex."""
    from kiro_crew.acp.client import AcpError
    from kiro_crew.agent_capabilities import _digest

    saved = {"name": "saved-member", "tools": ["*"]}
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _: {"materialized": _digest(saved)})
    runtime = _codex_runtime()
    runtime.terminate_session = AsyncMock()
    handle = _codex_handle(consumed)
    with pytest.raises(AcpError, match="capability_runtime_unverified"):
        await runtime._confirm_member_projection(handle)
    runtime.terminate_session.assert_awaited_once_with("codex-session")
    assert handle.confirmed_projection_template == ""


def test_codex_member_template_not_reported_from_dead_runtime(monkeypatch):
    from kiro_crew.agent_capabilities import _digest

    spec = {"name": "saved-member", "tools": ["*"]}
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _: {"materialized": _digest(spec)})
    provider = _codex_session_provider("saved-member", spec, alive=False)
    provider._handle.confirmed_projection_template = "saved-member"
    assert provider.loaded_capability_template == ""


def test_deepseek_member_confirms_on_the_spec_its_array_was_built_from(tmp_path, monkeypatch):
    """deepseek confirms on the AcpClient path like claude. The mirror must hand back
    the spec it parsed: a projection that drops it leaves nothing to confirm, and every
    deepseek member session would be refused."""
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.agent_capabilities import _digest
    from kiro_crew.providers.mirrors.registry import mirror_for

    agents = tmp_path / "agents"
    agents.mkdir()
    spec = {"name": "saved-member", "tools": ["*"], "mcpServers": {}}
    (agents / "saved-member.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents, raising=False)
    monkeypatch.setenv("KIRO_AGENTS_DIR", str(agents))
    projection = mirror_for(ACP_BACKEND_DEEPSEEK).session_projection(
        "saved-member", work_dir=tmp_path
    )
    consumed = projection.agent_spec
    assert consumed is not None
    monkeypatch.setattr(
        agent_state, "get_capabilities", lambda _: {"materialized": _digest(consumed)}
    )
    client = AcpClient(work_dir=tmp_path, agent="saved-member", acp_backend=ACP_BACKEND_DEEPSEEK)
    client._session_mcp_withheld = False
    client.member_context = True
    client._confirm_member_projection(consumed)
    assert client.capability_projection_gaps == ()


def test_deepseek_member_spec_reaches_its_session_array():
    """deepseek confirms on the AcpClient path like claude, which needs a mirror that
    carries the spec it parsed; without one there is nothing to confirm."""
    from kiro_crew.providers.mirrors.registry import has_mirror

    assert has_mirror(ACP_BACKEND_DEEPSEEK)
    assert ACP_BACKEND_DEEPSEEK in ACP_BACKENDS_MEMBER_CAPABILITIES


@pytest.mark.asyncio
async def test_projection_gaps_keep_saved_runtime_unverified(world, monkeypatch):
    service, cfg, factory, _, project, _, _ = world
    await asyncio.to_thread(save, service, enroll=True)
    manager = SessionManager(cfg, provider_factory=factory)
    try:
        await manager.get_or_create("dashboard:A", agent="A", cwd=str(project))
        prepared = await asyncio.to_thread(prepare_member_capabilities, "A", project)
        assert manager.capability_runtime_view("A", prepared["revision"])["status"] == "applied"
        monkeypatch.setattr(
            FakeProvider, "capability_projection_gaps", property(lambda _: ("hooks",))
        )
        view = manager.capability_runtime_view("A", prepared["revision"])
        assert view["status"] == "unverified"
        assert view["sessions"][0]["status"] == "unverified"
        manager.release("dashboard:A")
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.parametrize(
    "field", [None, "hooks", "toolsSettings", "excludedTools", "allowedTools", "per_tool_mounts"]
)
def test_claude_projection_gaps_are_explicit(tmp_path, monkeypatch, field):
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.agent_capabilities import _digest

    spec = {"name": "saved-member", "tools": ["*"]}
    if field == "per_tool_mounts":
        spec["tools"] = ["*", "@docs/search"]
    elif field:
        spec[field] = {"entry": "value"}
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _: {"materialized": _digest(spec)})
    client = AcpClient(work_dir=tmp_path, agent="saved-member", acp_backend=ACP_BACKEND_CLAUDE)
    client._claude_settings_authored = True
    client.member_context = True
    client._confirm_member_projection(spec)
    expected = "auto_approval" if field == "allowedTools" else field
    assert client.capability_projection_gaps == ((expected,) if expected else ())
    assert client.loaded_capability_template == ""


@pytest.mark.asyncio
async def test_saved_claude_member_uses_existing_essentials_path(world, monkeypatch):
    from kiro_crew.member_essential_context import documents_for_member

    service, _, _, _, project, _, _ = world
    # A file:// resource must sit under the home directory; keep the project
    # inside it wherever the test's temporary tree happens to be.
    monkeypatch.setenv("HOME", str(project.parent))
    resource = project / "member-guide.md"
    resource.write_text("SAVED_MEMBER_RESOURCE", encoding="utf-8")
    uri = "file://" + str(resource)
    request = {
        "revision": service.get("A")["revision"],
        "enroll": True,
        "operations": [
            {"section": "prompt", "id": "prompt", "action": "set", "value": "SAVED_MEMBER_PROMPT"},
            {"section": "resources", "id": uri, "action": "set", "value": uri},
        ],
    }
    preview = await asyncio.to_thread(service.preview, "A", request)
    await asyncio.to_thread(
        service.put, "A", {**request, "preview_token": preview["preview_token"]}
    )
    prepared = await asyncio.to_thread(prepare_member_capabilities, "A", project)
    documents = await asyncio.to_thread(
        documents_for_member, prepared["template"], str(project), inherits_default_resources=False
    )
    bodies = "\n".join(body for _, body in documents)
    assert "SAVED_MEMBER_PROMPT" in bodies
    assert "SAVED_MEMBER_RESOURCE" in bodies


def test_nonmember_claude_does_not_read_member_capability_state(tmp_path, monkeypatch):
    from kiro_crew.acp.client import AcpClient

    read = MagicMock(side_effect=ValueError("unreadable member state"))
    monkeypatch.setattr(agent_state, "get_capabilities", read)
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client._confirm_member_projection(None)
    read.assert_not_called()
    assert client.loaded_capability_template == ""
