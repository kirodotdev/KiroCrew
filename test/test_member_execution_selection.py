"""Member execution settings reach provider construction and survive recovery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from member_memory_helpers import patch_private_memory_supported

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
)
from kiro_crew.config.loader import KiroCrewConfig, resolve_effective_model
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.members import member_thread_session_alias
from kiro_crew.memory_stores import (
    memory_stores_root,
    provision_member_memory,
    require_member_memory_store,
)
from kiro_crew.session import _session_model
from kiro_crew.subagent import prepare_spawn_execution
from kiro_crew.subagent_persistence import (
    create_agent_folder,
    read_run_execution,
    update_state,
)


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE])
@pytest.mark.parametrize(
    "session_key",
    ["dashboard:review", member_thread_session_alias("review"), "subagent:review"],
)
def test_member_backend_and_model_reach_factory(backend, session_key):
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
    cfg.agents["review"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", acp_backend=backend, model="member-test-model"
    )
    with patch("kiro_crew.providers.acp.AcpProvider") as provider:
        cfg.create_provider_factory()(session_key, agent="kirocrew", crew_agent="review")
    assert provider.call_args.kwargs["acp_backend"] == backend
    assert provider.call_args.kwargs["model"] == "member-test-model"
    assert provider.call_args.kwargs["crew_agent"] == "review"


def test_inherit_and_explicit_kiro_are_distinct_after_config_round_trip():
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CODEX
    cfg.agents["inherit"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.agents["kiro"] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend=ACP_BACKEND_KIRO)
    cfg.save()
    restored = KiroCrewConfig.load()
    assert restored.agents["inherit"].acp_backend is None
    assert restored.resolve_session_backend(agent="inherit") == ACP_BACKEND_CODEX
    assert restored.agents["kiro"].acp_backend == ACP_BACKEND_KIRO
    assert restored.resolve_session_backend(agent="kiro") == ACP_BACKEND_KIRO


def test_member_backend_does_not_inherit_foreign_global_model():
    cfg = KiroCrewConfig()
    cfg.agent.model = "global-test-model"
    cfg.agents["review"] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend=ACP_BACKEND_CODEX)
    assert _session_model(cfg, "review") is None
    assert resolve_effective_model(cfg, "review") == ""
    with patch("kiro_crew.providers.acp.AcpProvider") as provider:
        cfg.create_provider_factory()("subagent:review", agent="kirocrew", crew_agent="review")
    assert provider.call_args.kwargs["model"] == ""


def test_inherited_member_backend_preserves_legacy_global_model_selection():
    cfg = KiroCrewConfig()
    cfg.agent.member_acp_backend = ACP_BACKEND_CLAUDE
    cfg.agent.model = "global-test-model"
    cfg.agents["review"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    with patch("kiro_crew.providers.acp.AcpProvider") as provider:
        cfg.create_provider_factory()(
            member_thread_session_alias("review"), agent="kirocrew", crew_agent="review"
        )
    assert provider.call_args.kwargs["acp_backend"] == ACP_BACKEND_CLAUDE
    assert provider.call_args.kwargs["model"] == "global-test-model"


def test_explicit_model_overrides_member_default():
    cfg = KiroCrewConfig()
    cfg.agents["review"] = KiroCrewAgentConfig(
        acp_backend=ACP_BACKEND_CODEX, model="member-test-model"
    )
    with patch("kiro_crew.providers.acp.AcpProvider") as provider:
        cfg.create_provider_factory()(
            "subagent:review", crew_agent="review", model_override="task-test-model"
        )
    assert provider.call_args.kwargs["model"] == "task-test-model"


@pytest.mark.parametrize(
    "member_pins,caller_pins,expected",
    [
        pytest.param({}, {}, ("role-test-model", "low"), id="role-defaults"),
        pytest.param({}, {"model": ""}, ("role-test-model", "low"), id="empty-caller-model"),
        pytest.param(
            {"model": "member-test-model", "reasoning_effort": "medium"},
            {},
            ("member-test-model", "medium"),
            id="member-pins",
        ),
        pytest.param(
            {"model": "member-test-model"},
            {},
            ("member-test-model", "low"),
            id="member-model-role-effort",
        ),
        pytest.param(
            {"reasoning_effort": "medium"},
            {},
            ("role-test-model", "medium"),
            id="role-model-member-effort",
        ),
        pytest.param(
            {"model": "member-test-model", "reasoning_effort": "medium"},
            {"model": "caller-test-model", "reasoning_effort": "max"},
            ("caller-test-model", "max"),
            id="caller-pins",
        ),
        pytest.param(
            {"model": "member-test-model", "reasoning_effort": "medium"},
            {"model": "auto"},
            ("auto", "medium"),
            id="explicit-caller-auto",
        ),
    ],
)
def test_member_spawn_captures_same_backend_role_precedence(member_pins, caller_pins, expected):
    cfg = KiroCrewConfig()
    cfg.agent.model = "global-test-model"
    cfg.agent.reasoning_effort = "high"
    cfg.agent.role_models = {"subagent": "role-test-model"}
    cfg.agent.role_efforts = {"subagent": "low"}
    cfg.agents["review"] = KiroCrewAgentConfig(kiro_agent="kirocrew", **member_pins)
    with patch.object(KiroCrewConfig, "load") as load:
        execution = prepare_spawn_execution(
            agent="kirocrew", crew_agent="review", _config=cfg, **caller_pins
        )
    load.assert_not_called()
    assert not execution.error
    assert execution.acp_backend == ACP_BACKEND_KIRO
    assert (execution.model, execution.reasoning_effort) == expected


@pytest.mark.parametrize("role_model", ["", "auto"])
def test_member_spawn_without_role_pins_retains_session_defaults(role_model):
    cfg = KiroCrewConfig()
    cfg.agent.model = "global-test-model"
    cfg.agent.reasoning_effort = "high"
    cfg.agent.role_models = {"subagent": role_model}
    cfg.agents["review"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    execution = prepare_spawn_execution(agent="kirocrew", crew_agent="review", _config=cfg)
    assert (execution.model, execution.reasoning_effort) == ("global-test-model", "high")


@pytest.mark.parametrize("stored_model", ["auto", "  ", ""])
@pytest.mark.parametrize("default_source", ["role", "global", "template"])
@pytest.mark.parametrize("caller_model", [None, "auto"])
def test_persisted_member_inherit_model_preserves_next_tier(
    monkeypatch, stored_model, default_source, caller_model
):
    cfg = KiroCrewConfig()
    cfg.agent.model = "global-test-model"
    if default_source == "role":
        cfg.agent.role_models = {"subagent": "role-test-model"}
    agent = "review-template" if default_source == "template" else "kirocrew"
    cfg.agents["review"] = KiroCrewAgentConfig(kiro_agent=agent, model=stored_model)
    cfg.save()
    cfg = KiroCrewConfig.load()
    assert cfg.agents["review"].model == stored_model
    monkeypatch.setattr(
        KiroCrewConfig,
        "_resolve_named_agent_model",
        staticmethod(lambda name, agents_dir=None: "template-test-model"),
    )
    expected = "auto" if caller_model == "auto" else f"{default_source}-test-model"
    if default_source != "role":
        assert cfg.acp_effective_model(agent, caller_model, crew_agent="review") == (
            "" if caller_model == "auto" else expected
        )
    execution = prepare_spawn_execution(
        agent=agent, crew_agent="review", model=caller_model, _config=cfg
    )
    assert not execution.error
    assert execution.model == expected
    with patch("kiro_crew.providers.acp.AcpProvider") as provider:
        cfg.create_provider_factory()(
            "subagent:review",
            agent=agent,
            crew_agent="review",
            model_override=caller_model if default_source != "role" else execution.model,
        )
    assert provider.call_args.kwargs["model"] == ("" if caller_model == "auto" else expected)


@pytest.mark.parametrize(
    "backend,role_model,expected_model",
    [
        (ACP_BACKEND_KIRO, "opus-4.8-1m", "claude-opus-4.8"),
        (ACP_BACKEND_CLAUDE, "opus-4.8-1m", "global.anthropic.claude-opus-4-8[1m]"),
        (ACP_BACKEND_CODEX, "codex-test-model", "codex-test-model"),
    ],
)
def test_member_spawn_translates_same_backend_role_model(backend, role_model, expected_model):
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = backend
    cfg.agent.model = "global-test-model"
    cfg.agent.role_models = {"subagent": role_model}
    cfg.agents["review"] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend=backend)
    execution = prepare_spawn_execution(agent="kirocrew", crew_agent="review", _config=cfg)
    assert execution.acp_backend == backend
    assert execution.model == expected_model
    with patch("kiro_crew.providers.acp.AcpProvider") as provider:
        cfg.create_provider_factory()(
            "subagent:review",
            agent="kirocrew",
            crew_agent=execution.crew_agent,
            acp_backend_override=execution.acp_backend,
            model_override=execution.model,
        )
    assert provider.call_args.kwargs["model"] == expected_model


@pytest.mark.parametrize("backend", [ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])
def test_member_spawn_excludes_foreign_backend_role_defaults(backend):
    cfg = KiroCrewConfig()
    cfg.agent.model = "global-test-model"
    cfg.agent.reasoning_effort = "medium"
    cfg.agent.role_models = {"subagent": "role-test-model"}
    cfg.agent.role_efforts = {"subagent": "high"}
    cfg.agents["review"] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend=backend)
    execution = prepare_spawn_execution(agent="kirocrew", crew_agent="review", _config=cfg)
    assert execution.acp_backend == backend
    assert (execution.model, execution.reasoning_effort) == ("auto", "medium")


@pytest.mark.parametrize("override", [None, ""])
def test_captured_empty_effort_does_not_inherit_a_later_default(override):
    cfg = KiroCrewConfig()
    cfg.agents["review"] = KiroCrewAgentConfig(
        acp_backend=ACP_BACKEND_CODEX,
        model="member-test-model",
        reasoning_effort="high",
    )
    with (
        patch("kiro_crew.providers.acp.AcpProvider") as provider,
        patch("kiro_crew.config.loader.model_supports_effort", return_value=True),
    ):
        cfg.create_provider_factory()(
            "subagent:review",
            crew_agent="review",
            reasoning_effort_override=override,
        )
    expected = {"member-test-model": "high"} if override is None else {}
    assert provider.call_args.kwargs["effort_per_model"] == expected


def test_resume_settings_do_not_trust_editable_run_state():
    create_agent_folder(
        "execution01",
        crew_agent="review",
        acp_backend=ACP_BACKEND_CODEX,
        model="member-test-model",
        reasoning_effort="high",
    )
    update_state("execution01", acp_backend=ACP_BACKEND_KIRO, model="tampered-model")
    assert read_run_execution("execution01") == {
        "crew_agent": "review",
        "acp_backend": ACP_BACKEND_CODEX,
        "model": "member-test-model",
        "reasoning_effort": "high",
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
@pytest.mark.parametrize("queued", [False, True])
async def test_legacy_continuation_does_not_rediscover_member_settings(queued):
    from kiro_crew.subagent import SubagentManager

    create_agent_folder("legacyrun1", memory_mode="persistent")
    cfg = KiroCrewConfig()
    cfg.agents["kirocrew"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    sessions = MagicMock()
    sessions.admission_closed = False
    sessions.resumable_sid.return_value = "legacy-session"
    sessions.is_continuable.return_value = False
    context = MagicMock()
    context.hooks.auto_approve_subagent_spawn = True
    manager = SubagentManager(sessions=sessions, ctx_builder=context)
    manager._run = AsyncMock()
    manager._spawn_stagger_secs = 0
    with (
        patch.object(KiroCrewConfig, "load", return_value=cfg),
        patch("kiro_crew.subagent._validate_agent", return_value=("kirocrew", "", "")),
        patch.object(
            manager, "_should_stagger_queue", return_value=(queued, not queued)
        ) as should_queue,
        patch(
            "kiro_crew.subagent.prepare_spawn_execution",
            side_effect=AssertionError("continuation rediscovered execution settings"),
        ),
    ):
        info = manager.continue_conversation("legacyrun1", "follow-up", agent="kirocrew")
        assert info is not None and not info.error
        assert info.crew_agent is None
        assert info.acp_backend is None
        if queued:
            assert info.queued
            should_queue.return_value = (False, True)
            manager._drain_queue()
            assert not manager._queue
        await manager._tasks[info.id]


@pytest.mark.usefixtures("healthy_host_memory")
@pytest.mark.parametrize("explicit_crew", [False, True])
@pytest.mark.parametrize("defaults", ["member", "role", "empty"])
def test_admission_freezes_member_defaults_before_queueing(monkeypatch, explicit_crew, defaults):
    from kiro_crew.subagent import SubagentManager

    cfg = KiroCrewConfig()
    cfg.agent.session_sharing = True
    cfg.agents["kirocrew"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    monkeypatch.setattr(KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: ""))
    if defaults == "member":
        cfg.agents["kirocrew"].acp_backend = ACP_BACKEND_CODEX
        cfg.agents["kirocrew"].model = "member-test-model"
        cfg.agents["kirocrew"].reasoning_effort = "high"
        expected = (ACP_BACKEND_CODEX, "member-test-model", "high")
    elif defaults == "role":
        cfg.agent.model = "global-test-model"
        cfg.agent.reasoning_effort = "high"
        cfg.agent.role_models = {"subagent": "role-test-model"}
        cfg.agent.role_efforts = {"subagent": "low"}
        expected = (ACP_BACKEND_KIRO, "role-test-model", "low")
    else:
        expected = (ACP_BACKEND_KIRO, "auto", "")
    cfg.save()
    sessions = MagicMock()
    sessions.admission_closed = False
    sessions.is_session_sharing_eligible.return_value = True
    manager = SubagentManager(sessions=sessions, ctx_builder=None)
    monkeypatch.setattr(manager, "_should_stagger_queue", lambda _: (True, False))
    monkeypatch.setattr(manager, "_emit_queue_depth", lambda *_: None)
    info = manager.spawn(
        "review the change",
        agent="kirocrew",
        crew_agent="kirocrew" if explicit_crew else None,
        parent_session_key="dashboard:parent",
    )
    assert info is not None and info.queued
    assert info.crew_agent == "kirocrew"
    assert (info.acp_backend, info.model, info.reasoning_effort) == expected
    assert manager._should_use_session_sharing(info) is False

    cfg.agents["kirocrew"].acp_backend = ACP_BACKEND_KIRO
    cfg.agents["kirocrew"].model = "changed-test-model"
    cfg.agents["kirocrew"].reasoning_effort = "low"
    cfg.agent.role_models = {"subagent": "changed-role-model"}
    cfg.agent.role_efforts = {"subagent": "max"}
    cfg.save()
    queued = manager._queue.pop()
    again = manager.spawn(**queued, _from_queue=True)
    assert again is not None and again.queued
    assert (again.acp_backend, again.model, again.reasoning_effort) == expected
    manager._queue.clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
@pytest.mark.parametrize("role_pinned", [False, True])
async def test_protected_continuation_retains_captured_role_defaults(monkeypatch, role_pinned):
    from kiro_crew.subagent import SubagentManager

    cfg = KiroCrewConfig()
    cfg.agents["review"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    monkeypatch.setattr(KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: ""))
    if role_pinned:
        cfg.agent.role_models = {"subagent": "role-test-model"}
        cfg.agent.role_efforts = {"subagent": "low"}
    store = await asyncio.to_thread(provision_member_memory, cfg, "review")
    await asyncio.to_thread(cfg.save)
    execution = await asyncio.to_thread(
        prepare_spawn_execution, agent="kirocrew", crew_agent="review", _config=cfg
    )
    expected = ("role-test-model", "low") if role_pinned else ("auto", "")
    assert (execution.model, execution.reasoning_effort) == expected
    await asyncio.to_thread(
        create_agent_folder,
        "rolesresume",
        memory_store=store,
        crew_agent=execution.crew_agent,
        acp_backend=execution.acp_backend,
        model=execution.model,
        reasoning_effort=execution.reasoning_effort,
    )
    cfg.agent.role_models = {"subagent": "changed-role-model"}
    cfg.agent.role_efforts = {"subagent": "max"}
    cfg.agents["review"].model = "changed-member-model"
    cfg.agents["review"].reasoning_effort = "high"
    await asyncio.to_thread(cfg.save)
    await asyncio.to_thread(update_state, "rolesresume", model="tampered-model")
    sessions = MagicMock()
    sessions.admission_closed = False
    sessions.resumable_sid.return_value = "retained-session"
    sessions.is_continuable.return_value = False
    context = MagicMock()
    context.hooks.auto_approve_subagent_spawn = True
    manager = SubagentManager(sessions=sessions, ctx_builder=context)
    manager._run = AsyncMock()
    with (
        patch("kiro_crew.subagent._validate_agent", return_value=("kirocrew", "", "")),
        patch.object(manager, "_should_stagger_queue", return_value=(False, True)),
        patch(
            "kiro_crew.subagent.prepare_spawn_execution",
            side_effect=AssertionError("continuation rediscovered execution settings"),
        ),
    ):
        info = manager.continue_conversation("rolesresume", "follow-up", agent="kirocrew")
        assert info is not None and not info.error
        assert (info.crew_agent, info.acp_backend, info.memory_store) == (
            "review",
            ACP_BACKEND_KIRO,
            store,
        )
        assert (info.model, info.reasoning_effort) == expected
        await asyncio.wait_for(manager._tasks[info.id], timeout=5)


@pytest.mark.asyncio
async def test_backend_switch_explicitly_clears_model_and_round_trips_kiro(monkeypatch):
    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_update

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda _: True
    )
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
    cfg.agents["review"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", model="old-test-model", reasoning_effort="high"
    )
    cfg.save()
    app = web.Application()
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    async with TestClient(TestServer(app)) as client:
        result = await client.put(
            "/api/agents/review",
            json={"acp_backend": ACP_BACKEND_KIRO, "model": "", "reasoning_effort": ""},
        )
        assert result.status == 200, await result.text()
        restored = KiroCrewConfig.load()
        assert restored.agents["review"].acp_backend == ACP_BACKEND_KIRO
        assert restored.agents["review"].model == ""
        assert restored.agents["review"].reasoning_effort == ""
        result = await client.put("/api/agents/review", json={"acp_backend": "unknown-harness"})
        assert result.status == 400
        assert KiroCrewConfig.load().agents["review"].acp_backend == ACP_BACKEND_KIRO


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend,choices,status,expected",
    [
        pytest.param(ACP_BACKEND_KIRO, {}, 400, None, id="missing-both"),
        pytest.param(ACP_BACKEND_KIRO, {"model": ""}, 400, None, id="missing-effort"),
        pytest.param(ACP_BACKEND_KIRO, {"reasoning_effort": "low"}, 400, None, id="missing-model"),
        pytest.param(
            ACP_BACKEND_KIRO, {"provision_memory": True}, 400, None, id="before-provisioning"
        ),
        pytest.param(
            ACP_BACKEND_KIRO,
            {"model": "", "reasoning_effort": ""},
            200,
            ("", ""),
            id="explicit-clear",
        ),
        pytest.param(
            ACP_BACKEND_KIRO,
            {"model": "chosen-test-model", "reasoning_effort": "low"},
            200,
            ("chosen-test-model", "low"),
            id="explicit-pins",
        ),
        pytest.param(None, {}, 200, ("saved-test-model", "high"), id="unchanged-backend"),
    ],
)
async def test_backend_change_requires_explicit_choices_before_mutation(
    monkeypatch, member_routes, backend, choices, status, expected
):
    from kiro_crew.config.loader import config_path
    from kiro_crew.dashboard.handlers import agents

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
    cfg.default_agent = "review"
    cfg.agents["review"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew",
        model="saved-test-model",
        reasoning_effort="high",
        description="Original",
    )
    await asyncio.to_thread(cfg.save)
    before = await asyncio.to_thread(config_path().read_bytes)
    persist = MagicMock(wraps=agents.persist_member_config)
    provision = MagicMock(wraps=agents.provision_member_memory)
    refresh = AsyncMock()
    retire = AsyncMock(return_value=None)
    monkeypatch.setattr(agents, "persist_member_config", persist)
    monkeypatch.setattr(agents, "provision_member_memory", provision)
    monkeypatch.setattr(agents, "_refresh_session_defaults", refresh)
    monkeypatch.setattr(agents, "_retire_legacy_member_contexts", retire)
    async with TestClient(TestServer(member_routes)) as client:
        response = await client.put(
            "/api/agents/review",
            json={"acp_backend": backend, "description": "Changed", **choices},
        )
        assert response.status == status, await response.text()
        payload = await response.json()
    provision.assert_not_called()
    retire.assert_not_awaited()
    if status == 400:
        assert payload["code"] == "backend_choices_required"
        assert await asyncio.to_thread(config_path().read_bytes) == before
        persist.assert_not_called()
        refresh.assert_not_awaited()
    else:
        saved = (await asyncio.to_thread(KiroCrewConfig.load)).agents["review"]
        assert saved.acp_backend == backend
        assert (saved.model, saved.reasoning_effort) == expected
        assert saved.description == "Changed"
        persist.assert_called_once()
        if backend is None:
            refresh.assert_not_awaited()
        else:
            refresh.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE])
async def test_create_member_with_explicit_backend_allocates_private_memory(monkeypatch, backend):
    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agents_create

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda _: True
    )
    patch_private_memory_supported(monkeypatch)
    app = web.Application()
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/agents", json={"name": "review", "kiro_agent": "kirocrew", "acp_backend": backend}
        )
        assert response.status == 200, await response.text()
    cfg = KiroCrewConfig.load()
    member = cfg.agents["review"]
    assert member.acp_backend == backend
    assert cfg.memory_stores[member.memory_store].memory_version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choices",
    [{}, {"model": "auto"}, {"reasoning_effort": "high"}, {"model": "", "reasoning_effort": ""}],
)
async def test_private_member_rejects_codex_before_creation_or_backend_change(monkeypatch, choices):
    from kiro_crew import member_memory_auth
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_PRIVATE_MEMORY_MCP
    from kiro_crew.dashboard.handlers.agents import (
        api_kirocrew_agent_update,
        api_kirocrew_agents_create,
    )

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda _: True
    )
    monkeypatch.setattr(
        member_memory_auth,
        "private_memory_execution_supported",
        lambda **kwargs: kwargs.get("acp_backend") in ACP_BACKENDS_PRIVATE_MEMORY_MCP,
    )
    app = web.Application()
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/agents",
            json={
                "name": "unsupported",
                "kiro_agent": "kirocrew",
                "acp_backend": ACP_BACKEND_CODEX,
            },
        )
        assert response.status == 409
        assert "unsupported" not in KiroCrewConfig.load().agents
        response = await client.post(
            "/api/agents",
            json={"name": "review", "kiro_agent": "kirocrew", "acp_backend": ACP_BACKEND_KIRO},
        )
        assert response.status == 200, await response.text()
        before = KiroCrewConfig.load().agents["review"]
        with patch("kiro_crew.dashboard.handlers.agents.persist_member_config") as persist:
            response = await client.put(
                "/api/agents/review",
                json={"acp_backend": ACP_BACKEND_CODEX, **choices},
            )
        persist.assert_not_called()
        assert response.status == 409
        assert (await response.json())["code"] == "member_memory_unavailable"
        after = KiroCrewConfig.load().agents["review"]
        assert after.acp_backend == before.acp_backend
        assert after.memory_store == before.memory_store


def test_claude_member_accepts_catalog_alias_for_advertised_wire_model(monkeypatch):
    from kiro_crew import model_registry
    from kiro_crew.dashboard.handlers import agents

    choice = next(
        row["model_name"]
        for row in model_registry.display_list("claude_code")
        if row["model_name"] != "auto"
    )
    wire = model_registry.to_provider_id(choice, "claude_code")
    monkeypatch.setattr(agents, "_advertised_cc_models", lambda *_: [{"model_name": wire}])
    assert agents._model_pin_rejected(choice, None, "acp", backend=ACP_BACKEND_CLAUDE) is None
    assert agents._model_pin_rejected(
        "unavailable-test-model", None, "acp", backend=ACP_BACKEND_CLAUDE
    )


@pytest.mark.asyncio
async def test_unchanged_inherited_backend_does_not_block_member_management(monkeypatch):
    from kiro_crew.dashboard.handlers.agents import (
        api_kirocrew_agent_update,
        api_kirocrew_agents_create,
    )

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda _: True
    )
    patch_private_memory_supported(monkeypatch)
    app = web.Application()
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/agents", json={"name": "review", "kiro_agent": "kirocrew"}
        )
        assert response.status == 200, await response.text()
        cfg = KiroCrewConfig.load()
        cfg.agent.acp_backend = ACP_BACKEND_CODEX
        cfg.agent.member_acp_backend = ACP_BACKEND_CODEX
        cfg.save()
        response = await client.put(
            "/api/agents/review", json={"description": "Updated role", "acp_backend": None}
        )
        assert response.status == 200, await response.text()
    assert KiroCrewConfig.load().agents["review"].description == "Updated role"


@pytest.fixture
def member_routes(monkeypatch):
    from kiro_crew import agent_state, member_memory_auth, sandbox
    from kiro_crew.dashboard.handlers import agents

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda _: True
    )
    monkeypatch.setattr(agents, "list_agents", lambda: [])
    # Keep backend admission real while making OS capability independent of the host.
    monkeypatch.setattr(member_memory_auth, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(sandbox, "_clamp_sandbox_mode", lambda value: value)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **kwargs: "namespace")

    def enroll(name, store):
        agent_state.set_crewmate_record(name, generation=store, template="kirocrew", hired_at="")

    async def create_hired(request):
        return await agents._create_crew(request, await request.json(), enroll=enroll)

    app = web.Application()
    app.router.add_post("/api/agents", agents.api_kirocrew_agents_create)
    app.router.add_post("/create-hired", create_hired)
    app.router.add_put("/api/agents/{name}", agents.api_kirocrew_agent_update)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "update", "provision"])
@pytest.mark.parametrize("enrolled", [False, True])
@pytest.mark.parametrize(
    "global_backend,member_backend,submitted_backend,member_status,worker_status",
    [
        (ACP_BACKEND_KIRO, ACP_BACKEND_CODEX, None, 409, 200),
        (ACP_BACKEND_CODEX, ACP_BACKEND_KIRO, None, 200, 409),
        (ACP_BACKEND_CODEX, ACP_BACKEND_CLAUDE, None, 200, 409),
        (ACP_BACKEND_CODEX, ACP_BACKEND_CODEX, ACP_BACKEND_KIRO, 200, 200),
        (ACP_BACKEND_KIRO, ACP_BACKEND_KIRO, ACP_BACKEND_CODEX, 409, 409),
    ],
)
async def test_private_admission_uses_verified_member_or_worker_route(
    monkeypatch,
    member_routes,
    action,
    enrolled,
    global_backend,
    member_backend,
    submitted_backend,
    member_status,
    worker_status,
):
    from kiro_crew import agent_state
    from kiro_crew.config.loader import config_path
    from kiro_crew.dashboard.handlers import agents

    status = member_status if enrolled else worker_status
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    cfg.agent.acp_backend = global_backend
    cfg.agent.member_acp_backend = member_backend
    if action != "create":
        cfg.agents["review"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, description="Original role"
        )
        if action == "update":
            await asyncio.to_thread(provision_member_memory, cfg, "review")
        if enrolled:
            await asyncio.to_thread(
                agent_state.set_crewmate_record,
                "review",
                generation=cfg.agents["review"].memory_store,
                template="kirocrew",
                hired_at="",
            )
    await asyncio.to_thread(cfg.save)
    await asyncio.to_thread(KiroCrewConfig.load)
    before = await asyncio.to_thread(config_path().read_bytes)
    root = memory_stores_root()
    stores_before = set(root.iterdir()) if root.exists() else set()
    retire = AsyncMock(return_value=None)
    monkeypatch.setattr(agents, "_retire_legacy_member_contexts", retire)
    read_record = MagicMock(wraps=agent_state.get_crewmate_record)
    monkeypatch.setattr(agent_state, "get_crewmate_record", read_record)

    body = {"acp_backend": submitted_backend, "description": "Changed role"}
    async with TestClient(TestServer(member_routes)) as client:
        if action == "create":
            response = await client.post(
                "/create-hired" if enrolled else "/api/agents",
                json={**body, "name": "review", "kiro_agent": "kirocrew"},
            )
        else:
            body.update(model="", reasoning_effort="")
            if action == "provision":
                body["provision_memory"] = True
            response = await client.put("/api/agents/review", json=body)
        assert response.status == status, await response.text()
        payload = await response.json()
    assert read_record.call_count == int(action != "create" and submitted_backend is None)
    if status == 409:
        assert payload["code"] == "member_memory_unavailable"
        assert "Codex ACP" in payload["error"]
        assert "Global Memory V1 was not used" in payload["error"]
        assert await asyncio.to_thread(config_path().read_bytes) == before
        assert (set(root.iterdir()) if root.exists() else set()) == stores_before
        retire.assert_not_awaited()
        return

    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    saved = loaded.agents["review"]
    assert saved.acp_backend == submitted_backend
    assert saved.description == "Changed role"
    store = await asyncio.to_thread(require_member_memory_store, loaded, "review")
    assert store != "default"
    assert loaded.memory_stores[store].memory_version == 2
    assert loaded.memory_stores[store].owner_member == "review"
    assert (
        "review" in await asyncio.to_thread(agent_state.enrolled_member_ids, loaded.agents)
    ) == enrolled
    assert loaded.resolve_session_backend(
        session_key=member_thread_session_alias("review"), crew_agent="review"
    ) == (member_backend if submitted_backend is None else submitted_backend)
    assert loaded.resolve_session_backend(session_key="subagent:review", crew_agent="review") == (
        global_backend if submitted_backend is None else submitted_backend
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "create_member",
        "create_worker",
        "update",
        "clear_backend",
        "provision",
        "worker",
        "inherited_worker",
    ],
)
async def test_model_validation_uses_member_or_worker_namespace(member_routes, action):
    from kiro_crew import agent_state
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    cfg = KiroCrewConfig()
    worker_backend = ACP_BACKEND_KIRO if action == "create_worker" else ACP_BACKEND_CODEX
    cfg.agent.acp_backend = worker_backend
    cfg.agent.member_acp_backend = ACP_BACKEND_CLAUDE
    if action not in {"create_member", "create_worker"}:
        cfg.agents["review"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew",
            acp_backend=(ACP_BACKEND_CODEX if action in {"clear_backend", "provision"} else None),
        )
        if action in {"update", "clear_backend"}:
            await asyncio.to_thread(provision_member_memory, cfg, "review")
        if action in {"update", "clear_backend", "provision"}:
            await asyncio.to_thread(
                agent_state.set_crewmate_record,
                "review",
                generation=cfg.agents["review"].memory_store,
                template="kirocrew",
                hired_at="",
            )
    await asyncio.to_thread(cfg.save)
    member_routes["state"] = SimpleNamespace(
        _slots={},
        sessions=SimpleNamespace(
            refresh_defaults=AsyncMock(),
            active_providers=lambda: [
                SimpleNamespace(
                    capabilities=capabilities_for(backend),
                    available_models=lambda model=model: [{"modelId": model}],
                )
                for backend, model in [
                    (worker_backend, "worker-test-model"),
                    (ACP_BACKEND_CLAUDE, "member-test-model"),
                ]
            ],
        ),
    )
    worker = action in {"create_worker", "worker", "inherited_worker"}
    rejected_model, accepted_model = (
        ("member-test-model", "worker-test-model")
        if worker
        else ("worker-test-model", "member-test-model")
    )
    async with TestClient(TestServer(member_routes)) as client:
        for model, status in [(rejected_model, 400), (accepted_model, 200)]:
            body = {"model": model}
            if action in {"clear_backend", "provision"}:
                body["acp_backend"] = None
            if action == "worker":
                body["acp_backend"] = ACP_BACKEND_CODEX
            if "acp_backend" in body:
                body["reasoning_effort"] = ""
            if action == "provision":
                body["provision_memory"] = True
            if action in {"create_member", "create_worker"}:
                response = await client.post(
                    "/create-hired" if action == "create_member" else "/api/agents",
                    json={**body, "name": "review", "kiro_agent": "kirocrew"},
                )
            else:
                response = await client.put("/api/agents/review", json=body)
            assert response.status == status, await response.text()
            if status == 400:
                assert (await response.json())["code"] == "invalid_model"
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert loaded.agents["review"].acp_backend == (
        ACP_BACKEND_CODEX if action == "worker" else None
    )
    assert loaded.agents["review"].model == accepted_model
    session_key = "subagent:review" if worker else member_thread_session_alias("review")
    with patch("kiro_crew.providers.acp.AcpProvider") as provider:
        loaded.create_provider_factory()(session_key, crew_agent="review")
    assert provider.call_args.kwargs["acp_backend"] == (
        worker_backend if worker else ACP_BACKEND_CLAUDE
    )
    assert provider.call_args.kwargs["model"] == accepted_model


@pytest.mark.parametrize("selected_backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CODEX])
@pytest.mark.parametrize("matching_session", [False, True])
@pytest.mark.parametrize(
    "model", ["kiro-test-model", "worker-test-model", "unknown-test-model", "auto", ""]
)
def test_model_validation_ignores_other_provider_namespaces(
    selected_backend, matching_session, model, monkeypatch
):
    from kiro_crew import model_registry
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard.handlers import agents

    other_backend = ACP_BACKEND_CODEX if selected_backend == ACP_BACKEND_KIRO else ACP_BACKEND_KIRO
    models = {ACP_BACKEND_KIRO: "kiro-test-model", ACP_BACKEND_CODEX: "worker-test-model"}
    providers = [
        SimpleNamespace(
            capabilities=capabilities_for(backend),
            available_models=lambda backend=backend: [{"modelId": models[backend]}],
        )
        for backend in (
            [other_backend, selected_backend, other_backend]
            if matching_session
            else [other_backend]
        )
    ]
    monkeypatch.setattr(model_registry, "advertised_models", lambda _: [])
    request = SimpleNamespace(
        app={"state": SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: providers))}
    )
    reason = agents._model_pin_rejected(model, request, "acp", backend=selected_backend)
    should_accept = not matching_session or model in ("", "auto", models[selected_backend])
    assert (reason is None) == should_accept


@pytest.mark.asyncio
async def test_unverifiable_model_route_refuses_pin_without_blocking_unrelated_edits(
    member_routes, monkeypatch
):
    from kiro_crew import agent_state

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CODEX
    cfg.agent.member_acp_backend = ACP_BACKEND_CLAUDE
    cfg.agents["review"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    await asyncio.to_thread(cfg.save)

    def unreadable(*args, **kwargs):
        raise OSError("unreadable enrollment")

    monkeypatch.setattr(agent_state, "get_crewmate_record", unreadable)
    async with TestClient(TestServer(member_routes)) as client:
        response = await client.put(
            "/api/agents/review", json={"model": "member-test-model", "description": "Rejected"}
        )
        assert response.status == 503, await response.text()
        assert (await response.json())["code"] == "members_unavailable"
        loaded = await asyncio.to_thread(KiroCrewConfig.load)
        assert loaded.agents["review"].model == ""
        assert loaded.agents["review"].description == ""
        response = await client.put("/api/agents/review", json={"description": "Accepted"})
        assert response.status == 200, await response.text()
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert loaded.agents["review"].description == "Accepted"
