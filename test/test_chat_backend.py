"""Chat-local backend selection, persistence, and allocation namespace safety."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_backend import chat_session_selection
from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.handlers import agents
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session import SessionManager


@pytest.fixture
def cfg(monkeypatch):
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ""
    cfg.agent.model = "kiro-only-pin"
    cfg.session.pool_size = 0
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    return cfg


@pytest.mark.asyncio
async def test_kiro_registry_row_is_accepted_as_the_actual_picker_request(state):
    from kiro_crew.agent_sdk.backend_install import BackendInstallState
    from kiro_crew.dashboard.handlers.acp_backend_status import _row

    row = _row(BackendInstallState("", "kiro", "installed"), {""})
    assert row["id"] == "kiro" and row["selectable"] is True
    slot = state.get_or_create_slot("kiro-picker", model="auto")
    status, body = await post_backend(state, slot, {"acp_backend": row["id"]})
    assert status == 200, body
    assert body["acp_backend"] == "" and slot.acp_backend == ""


def test_legacy_kiro_alias_in_slot_starts_explicit_kiro_not_configured_default(cfg, tmp_path):
    cfg.agent.acp_backend = "kas"
    slot = _ChatSlot("legacy-kiro", model="auto")
    slot.acp_backend = "kiro"
    selection = chat_session_selection(slot, cfg, "auto")
    selection.pop("model")
    assert selection["acp_backend_override"] == ""
    provider = cfg.create_provider_factory()(
        "dashboard:legacy-kiro", cwd=str(tmp_path), **selection
    )
    assert provider.client.backend == ""


@pytest.fixture
def state(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    sessions = MagicMock(count=0)
    sessions.get_provider.return_value = None
    sessions.reset = AsyncMock(return_value=True)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0,
        conversation_log=ConversationLog(base_dir=tmp_path / "history"),
    )
    state.push_slots_update = MagicMock()
    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", AsyncMock(return_value=None))
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire,expected",
    [
        ("kiro", ""),
        ("", ""),
        (None, "kas"),
        ("kas", "kas"),
        ("claude", "claude"),
        ("codex", "codex"),
        ("pi", "pi"),
        ("opencode", "opencode"),
        ("goose", "goose"),
        ("deepseek", "deepseek"),
    ],
)
async def test_picker_wire_values_do_not_conflate_kiro_and_default(state, cfg, wire, expected):
    cfg.agent.acp_backend = "kas"
    slot = state.get_or_create_slot("wire-matrix", model="auto")
    slot.acp_backend = "codex" if expected == "pi" else "pi"
    slot.model_backend = slot.acp_backend
    status, body = await post_backend(state, slot, {"acp_backend": wire})
    assert status == 200, body
    assert slot.acp_backend == (None if wire is None else expected)
    assert slot.model_backend == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["kiro", ""])
async def test_kiro_alias_cannot_bypass_narrowed_registry(state, monkeypatch, wire):
    monkeypatch.setattr(
        "kiro_crew.agent_sdk.backends.selectable_backends", lambda: frozenset({"pi"})
    )
    slot = state.get_or_create_slot("kiro-denied", model="keep")
    assert (await post_backend(state, slot, {"acp_backend": wire}))[0] == 400
    assert slot.acp_backend is None and slot.model == "keep"
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_clearing_override_follows_profile_instead_of_global(state, cfg):
    from kiro_crew.config.loader import KiroCrewAgentConfig

    cfg.agent.acp_backend = "kas"
    cfg.agents["profile"] = KiroCrewAgentConfig(acp_backend="codex")
    slot = state.get_or_create_slot("clear-to-profile", agent="profile", model="old")
    slot.acp_backend = "pi"
    slot.model_backend = "pi"
    status, body = await post_backend(state, slot, {"acp_backend": None})
    assert status == 200, body
    assert slot.acp_backend is None and slot.model_backend == "codex"


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["kiro", ""])
async def test_kiro_models_query_reaches_existing_auth_gate_without_defaulting_to_kas(
    cfg, monkeypatch, wire
):
    cfg.agent.acp_backend = "kas"
    guard = AsyncMock(return_value=web.json_response({"code": "kiro_unverified"}, status=503))
    monkeypatch.setattr(agents, "reject_if_kiro_unverified", guard)
    app = web.Application()
    app.router.add_get("/api/models", agents.api_models)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/models", params={"backend": wire})
        assert response.status == 503
        assert (await response.json())["code"] == "kiro_unverified"
    guard.assert_awaited_once()


async def post_backend(state, slot, body, *, caller_app=""):
    app = web.Application()
    app["state"] = state

    async def route(request):
        if caller_app:
            request["app"] = caller_app
        return await chat_handlers.api_chat_slot_backend(request)

    app.router.add_post("/api/chat/slots/{slot}/backend", route)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"/api/chat/slots/{slot.key}/backend", json=body)
        return response.status, await response.json()


@pytest.mark.asyncio
async def test_independent_chats_switch_clear_and_noop(state, cfg):
    a = state.get_or_create_slot("a", model="old-pin")
    b = state.get_or_create_slot("b", model="other-pin")
    a.append("user", "keep my history", "msg msg-u")
    a.drain()
    status, body = await post_backend(state, a, {"acp_backend": "pi"})
    assert status == 200 and body["acp_backend"] == "pi"
    assert a.model == "auto" and a.model_backend == "pi"
    assert b.acp_backend is None and b.model == "other-pin"
    assert cfg.agent.acp_backend == "" and cfg.agent.model == "kiro-only-pin"
    state.sessions.reset.assert_awaited_once()
    state.sessions.reset.reset_mock()
    assert (await post_backend(state, a, {"acp_backend": "pi"}))[0] == 200
    state.sessions.reset.assert_not_awaited()
    assert (await post_backend(state, a, {"acp_backend": None}))[0] == 200
    assert a.acp_backend is None and a.model_backend == ""
    assert a.messages[0]["content"] == "keep my history"


@pytest.mark.asyncio
async def test_pin_same_effective_backend_does_not_reset_model(state):
    slot = state.get_or_create_slot("a", model="keep-pin")
    assert (await post_backend(state, slot, {"acp_backend": ""}))[0] == 200
    assert slot.acp_backend == "" and slot.model == "keep-pin"
    state.sessions.reset.assert_not_awaited()
    assert (await post_backend(state, slot, {"acp_backend": None}))[0] == 200
    assert slot.acp_backend is None and slot.model == "keep-pin"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [None, "pi"])
@pytest.mark.parametrize("model_backend", [None, "", "pi"])
async def test_global_change_retires_stale_resident_backend(state, cfg, backend, model_backend):
    slot = state.get_or_create_slot("a", model="kiro-only-pin")
    slot.model_backend = model_backend
    provider = MagicMock(spec=AcpProvider)
    provider.client = MagicMock(backend="")
    provider.has_active_turn.return_value = False
    state.sessions.get_provider.return_value = provider
    cfg.agent.acp_backend = "pi"

    assert (await post_backend(state, slot, {"acp_backend": backend}))[0] == 200
    state.sessions.reset.assert_awaited_once_with("dashboard:a", skip_if_busy=True)
    assert slot.acp_backend == backend and slot.model_backend == "pi"
    assert slot.model == "auto"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [None, "pi"])
async def test_global_change_preserves_cold_restored_mismatch_until_reset(state, cfg, backend):
    slot = state.get_or_create_slot("a", model="kiro-only-pin")
    slot.model_backend = ""
    slot.append("user", "retained", "msg msg-u")
    slot.drain()
    assert await asyncio.to_thread(_save_slot_to_history, state, slot, force=True)
    del state._slots[slot.key]
    cfg.agent.acp_backend = "pi"
    restored = await asyncio.to_thread(
        _rehydrate_slot_from_history, state, slot.key, kiro_model_map={}
    )
    assert restored is not None and restored.model_backend == ""

    assert (await post_backend(state, restored, {"acp_backend": backend}))[0] == 200
    state.sessions.reset.assert_awaited_once()
    assert restored.model_backend == "pi" and restored.model == "auto"
    assert restored.messages[0]["content"] == "retained"


def test_app_owned_slot_keeps_its_model_across_foreign_backend_config(cfg):
    slot = _ChatSlot("app-slot", model="foreign-model-pin")
    slot._app = "issue-radar"
    slot._model_pick_gen = 4
    slot.model_backend = ""
    cfg.agent.acp_backend = "pi"

    selection = chat_session_selection(slot, cfg, cfg.agent.model)

    assert selection == {"model": "foreign-model-pin"}
    assert slot.model == "foreign-model-pin"
    assert slot._model_pick_gen == 4
    assert slot.model_backend == ""


@pytest.mark.asyncio
async def test_matching_resident_and_model_namespace_preserve_noop(state, cfg):
    slot = state.get_or_create_slot("a", model="pi-only-pin")
    slot.model_backend = "pi"
    cfg.agent.acp_backend = "pi"
    provider = MagicMock(spec=AcpProvider)
    provider.client = MagicMock(backend="pi")
    provider.has_active_turn.return_value = False
    state.sessions.get_provider.return_value = provider

    for backend in (None, "pi", None):
        assert (await post_backend(state, slot, {"acp_backend": backend}))[0] == 200
        assert slot.model == "pi-only-pin" and slot.model_backend == "pi"
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [None, ""])
async def test_revoked_old_backend_does_not_block_permitted_replacement(
    state, monkeypatch, backend
):
    slot = state.get_or_create_slot("a", model="pi-only-pin")
    slot.acp_backend = "pi"
    slot.model_backend = "pi"
    monkeypatch.setattr("kiro_crew.agent_sdk.backends.selectable_backends", lambda: frozenset({""}))

    assert (await post_backend(state, slot, {"acp_backend": backend}))[0] == 200
    state.sessions.reset.assert_awaited_once()
    assert slot.acp_backend == backend and slot.model_backend == ""
    assert slot.model == "auto"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"acp_backend": "unknown"},
        {"acp_backend": "default"},
        # vibe was here while it was dormant; it is selectable now, so the
        # invalid-value arm uses a never-registered spelling instead.
        {"acp_backend": "vibe-unregistered"},
        {"acp_backend": []},
        {"acp_backend": 1},
    ],
)
async def test_invalid_backend_never_mutates_or_resets(state, body):
    slot = state.get_or_create_slot("a", model="keep-pin")
    assert (await post_backend(state, slot, body))[0] == 400
    assert slot.acp_backend is None and slot.model == "keep-pin"
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_chat_backend_selectable_and_allocated(state, cfg, tmp_path):
    slot = state.get_or_create_slot("local", origin="user")
    assert slot.to_dict()["backend_selection_supported"] is True
    assert (await post_backend(state, slot, {"acp_backend": "pi"}))[0] == 200
    selection = chat_session_selection(slot, cfg, cfg.agent.model)
    model = selection.pop("model")
    provider = cfg.create_provider_factory()(
        "dashboard:local", model_override=model, cwd=str(tmp_path), **selection
    )
    assert provider.client.backend == "pi"
    assert provider.client._model in {"", "auto"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["turn", "eager", "provider", "queue", "lease"])
async def test_busy_switch_refused_without_commit(state, kind):
    slot = state.get_or_create_slot("a", model="keep-pin")
    task = None
    if kind in {"turn", "eager"}:
        task = asyncio.create_task(asyncio.sleep(30))
        if kind == "turn":
            slot.task = task
        else:
            slot._eager_spawn_task = task
    if kind in {"provider", "lease"}:
        provider = MagicMock(spec=AcpProvider)
        provider.has_active_turn.return_value = kind == "provider"
        state.sessions.get_provider.return_value = provider
        state.sessions.reset.return_value = False
    if kind == "queue":
        slot._queue.append("queued")
    try:
        status, body = await asyncio.wait_for(post_backend(state, slot, {"acp_backend": "pi"}), 30)
        assert status == 409 and body["code"] == "turn_in_flight"
        assert slot.acp_backend is None and slot.model == "keep-pin"
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_app_owner_cannot_override_backend(state):
    slot = state.get_or_create_slot("app-owned", model="foreign-model-pin", app="issue-radar")
    slot._model_pick_gen = 4
    slot.model_backend = ""

    status, body = await post_backend(state, slot, {"acp_backend": "pi"}, caller_app="issue-radar")

    assert status == 409 and body["code"] == "backend_managed"
    assert slot.model == "foreign-model-pin"
    assert slot._model_pick_gen == 4
    assert slot.model_backend == ""
    assert slot.acp_backend is None
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attr,value",
    [
        ("mode", "member"),
        ("mode", "crew"),
        ("mode", "design-critique"),
        ("mode", "unknown-managed-mode"),
        ("executor", "remote"),
        ("_app", "app"),
        ("linked_session_key", "slack:x"),
        ("channel_origin", True),
    ],
)
async def test_managed_chat_cannot_override_backend(state, attr, value):
    slot = state.get_or_create_slot("a", mode="orchestrator")
    setattr(slot, attr, value)
    status, body = await post_backend(state, slot, {"acp_backend": "pi"})
    assert status in {404, 409}  # App-owned chats also pass the ownership gate.
    if status == 409:
        assert body["code"] == "backend_managed"
    state.sessions.reset.assert_not_awaited()


@pytest.mark.parametrize("backend", [None, "", "pi", "claude"])
def test_backend_metadata_roundtrips_without_losing_transcript(state, backend):
    slot = state.get_or_create_slot("persist")
    slot.acp_backend = backend
    slot.model_backend = "" if backend is None else backend
    slot.append("user", "retained", "msg msg-u")
    slot.drain()
    assert _save_slot_to_history(state, slot, force=True)
    del state._slots[slot.key]
    restored = _rehydrate_slot_from_history(state, slot.key, kiro_model_map={})
    assert restored is not None
    assert restored.acp_backend == backend
    assert restored.model_backend == slot.model_backend
    assert restored.messages[0]["content"] == "retained"
    # Clearing persists null, not the old override carried through a metadata merge.
    restored.acp_backend = None
    restored._dirty = True
    assert _save_slot_to_history(state, restored, force=True)
    meta = state.conversation_log.get_metadata("dashboard:persist")
    assert "acp_backend" in meta and meta["acp_backend"] is None


def test_model_namespace_resets_on_inherited_default_change(cfg):
    slot = _ChatSlot("a", model="kiro-only-pin")
    chat_session_selection(slot, cfg, cfg.agent.model)
    cfg.agent.acp_backend = "pi"
    selection = chat_session_selection(slot, cfg, cfg.agent.model)
    assert selection["model"] == "auto"
    assert slot.model_backend == "pi"


@pytest.mark.parametrize("backend", ["", "claude", "kas", "codex", "pi"])
def test_factory_uses_chat_backend_and_not_foreign_global_pin(cfg, backend, tmp_path):
    slot = _ChatSlot("a", model="auto")
    slot.acp_backend = backend
    selection = chat_session_selection(slot, cfg, cfg.agent.model)
    model = selection.pop("model")
    provider = cfg.create_provider_factory()(
        "dashboard:a", model_override=model, cwd=str(tmp_path), **selection
    )
    assert provider.client.backend == backend
    assert provider.client._model in {"", "auto"}
    assert cfg.agent.acp_backend == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["", "pi"])
async def test_allocator_bypasses_pool_and_never_resumes_foreign_id(cfg, tmp_path, backend):
    from kiro_crew.session_lifecycle import allocation_identity

    captured = []

    def factory(key, **kwargs):
        provider = MagicMock(spec=AcpProvider)
        provider.client = MagicMock()
        provider.client.backend = kwargs["acp_backend_override"]
        provider.client.resumed = False
        provider.client._session_id = "new-pi-id"
        provider.client._pid = None
        provider.cwd = str(tmp_path)
        provider.is_process_alive.return_value = True
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        captured.append(provider)
        return provider

    mgr = SessionManager(cfg, provider_factory=factory)
    mgr._pool_size = 1
    mgr._session_map.set("dashboard:a", "foreign-id", provider="claude_code")
    with (
        patch.object(mgr, "_drain_and_claim", new=AsyncMock()) as pool,
        patch("kiro_crew.session._session_model", return_value=None) as resolve_model,
    ):
        try:
            provider, is_new, resumed = await mgr.get_or_create(
                "dashboard:a", acp_backend_override=backend
            )
            assert is_new and not resumed
            assert resolve_model.call_args.kwargs["backend_override"] == backend
            identity = allocation_identity(mgr, "dashboard:a", mgr._sessions["dashboard:a"])
            assert identity["acp_backend_override"] == backend
            captured[0].client.set_resume_session_id.assert_not_called()
            pool.assert_not_awaited()
            mgr.release("dashboard:a")
            await mgr.get_or_create("dashboard:b", model="auto", acp_backend_override=backend)
            pool.assert_not_awaited()
            mgr.release("dashboard:b")
        finally:
            await mgr.close_all()
    assert len(captured) == 2


@pytest.mark.asyncio
async def test_api_models_uses_requested_chat_backend(cfg, monkeypatch):
    request = MagicMock()
    request.query = {"backend": "pi"}
    models = [{"model_name": "anthropic/model", "display_name": "Model"}]
    monkeypatch.setattr(
        agents, "_advertised_backend_models", lambda _request, _backend, **_kwargs: models
    )

    response = await agents.api_models(request)

    assert response.status == 200
    assert isinstance(response.body, bytes)
    assert json.loads(response.body) == models
    assert cfg.agent.acp_backend == ""


@pytest.mark.asyncio
async def test_api_models_rejects_unselectable_chat_backend(cfg):
    request = MagicMock()
    request.query = {"backend": "not-selectable"}

    response = await agents.api_models(request)

    assert response.status == 400


@pytest.mark.asyncio
async def test_switch_lock_holds_new_allocation_until_commit(state, cfg):
    slot = state.get_or_create_slot("a")
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def reset(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(proceed.wait(), 30)
        return True

    async def allocate():
        async with slot._lock:
            return chat_session_selection(slot, cfg, cfg.agent.model)

    state.sessions.reset = reset
    switch = asyncio.create_task(post_backend(state, slot, {"acp_backend": "pi"}))
    await asyncio.wait_for(entered.wait(), 30)
    allocation = asyncio.create_task(allocate())
    try:
        await asyncio.sleep(0)
        assert not allocation.done()
        proceed.set()
        assert (await asyncio.wait_for(switch, 30))[0] == 200
        assert (await asyncio.wait_for(allocation, 30))["acp_backend_override"] == "pi"
    finally:
        proceed.set()
        await asyncio.gather(switch, allocation, return_exceptions=True)
