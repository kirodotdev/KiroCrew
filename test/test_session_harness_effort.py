"""A session's effort control offers what its harness takes, before and after it reports.

Until a session reported, the composer judged it by the displayed model's name over the
levels whichever session reported last. A crew on Claude Code showed no control before
its first turn, and a codex session showed one its build could not take. On a live codex
session a build that advertises no ``reasoning_effort`` option drops every pick
(``change_effort`` skips it), yet a GPT-5.x model still read as effort-capable.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import pin_harness_choice

from kiro_crew.acp_backends import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import api_chat_slot_selection_capabilities
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.effort import EFFORT_LEVELS
from kiro_crew.providers.acp import AcpProvider

# ── selection-capabilities before the session reports ──


def _state(slot: _ChatSlot, live: tuple = ()) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=None)
    state.sessions.active_providers = MagicMock(return_value=list(live))
    return state


def _config(monkeypatch, *, member: str, resolves_to: str = "") -> None:
    monkeypatch.setattr(
        chat_handlers.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(
            agent=SimpleNamespace(acp_backend="", member_acp_backend=member), agents={}
        ),
    )
    monkeypatch.setattr(
        chat_handlers, "resolve_effective_model", lambda _cfg, _agent, **_k: resolves_to
    )


async def _caps(state: DashboardState, slot: str) -> dict:
    app = web.Application()
    app["state"] = state
    app.router.add_get(
        "/api/chat/slots/{slot}/selection-capabilities", api_chat_slot_selection_capabilities
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/chat/slots/{slot}/selection-capabilities")
        assert resp.status == 200
        return await resp.json()


@pytest.mark.asyncio
async def test_a_cold_claude_thread_offers_effort_for_the_model_its_crew_runs(monkeypatch):
    """The crew pins an effort-capable model while its slot still shows ``auto``."""
    _config(monkeypatch, member="claude", resolves_to="claude-opus-5.5")

    data = await _caps(_state(_ChatSlot("member-helper")), "member-helper")

    assert data["effort_supported"] is True
    assert data["effort_levels"] == list(EFFORT_LEVELS)


@pytest.mark.asyncio
async def test_a_cold_member_thread_resolves_its_model_on_the_routed_harness(monkeypatch):
    """The crew's pin is scoped to the member route's harness, not the configured one:
    unscoped, a claude pin is dropped under a kiro default and ``auto`` is judged."""
    _config(monkeypatch, member="claude")
    monkeypatch.setattr(
        chat_handlers,
        "resolve_effective_model",
        lambda _cfg, _agent, backend=None, **_k: "claude-opus-5.5" if backend == "claude" else "",
    )

    data = await _caps(_state(_ChatSlot("member-helper")), "member-helper")

    assert data["effort_supported"] is True


@pytest.mark.asyncio
async def test_a_cold_slot_judges_effort_on_the_model_a_foreign_pin_falls_to(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig

    config = KiroCrewConfig.load()
    config.agent.model = "claude-haiku-4.5"
    config.save()
    pin_harness_choice(acp_backend="", member_acp_backend="")
    monkeypatch.setattr(
        chat_handlers.model_registry,
        "_ADVERTISED_MODELS",
        {"acp": ["claude-haiku-4.5"], "codex": ["openai.gpt-5.6-sol"]},
    )
    slot = _ChatSlot("foreign-pin", model="openai.gpt-5.6-sol")

    data = await _caps(_state(slot), slot.key)

    assert data["backend"] == ""
    assert data["effort_supported"] is False


@pytest.mark.asyncio
async def test_a_cold_app_slot_judges_the_model_its_app_starts_on(monkeypatch):
    """An app-owned slot starts on ``apps.<name>.model``, so that model judges effort."""
    _config(monkeypatch, member="claude")
    monkeypatch.setattr(
        chat_handlers,
        "resolve_effective_model",
        lambda _cfg, _agent, app_name="", **_k: (
            "claude-opus-5.5" if app_name == "my-app" else "auto"
        ),
    )
    slot = _ChatSlot("app-helper")
    slot._app = "my-app"

    data = await _caps(_state(slot), "app-helper")

    assert data["effort_supported"] is True


@pytest.mark.asyncio
async def test_a_cold_codex_thread_reads_unknown_until_its_harness_says(monkeypatch):
    """Unknown, not unsupported: no live codex session has said what its build takes."""
    _config(monkeypatch, member="codex")

    data = await _caps(_state(_ChatSlot("member-helper")), "member-helper")

    assert data["effort_supported"] is None
    assert data["effort_levels"] == []


@pytest.mark.asyncio
async def test_a_cold_codex_thread_takes_a_live_codex_sessions_answer(monkeypatch):
    _config(monkeypatch, member="codex")
    live = MagicMock()
    live.capabilities = capabilities_for(ACP_BACKEND_CODEX)
    live.supports_effort.return_value = True
    live.get_valid_effort_levels.return_value = ["low", "medium", "high"]

    data = await _caps(_state(_ChatSlot("member-helper"), (live,)), "member-helper")

    assert data["effort_supported"] is None
    assert data["effort_levels"] == ["low", "medium", "high"]


@pytest.mark.asyncio
async def test_a_cold_codex_thread_omits_levels_the_write_path_would_reject(monkeypatch):
    _config(monkeypatch, member="codex")
    live = _codex(effort=True)
    live.get_valid_effort_levels.return_value = ["low", "zzz17080"]

    data = await _caps(_state(_ChatSlot("member-helper"), (live,)), "member-helper")

    assert data["effort_levels"] == ["low"]


def _codex(*, effort: bool) -> MagicMock:
    live = MagicMock()
    live.capabilities = capabilities_for(ACP_BACKEND_CODEX)
    live.supports_effort.return_value = effort
    live.get_valid_effort_levels.return_value = ["low", "high"] if effort else []
    return live


@pytest.mark.asyncio
async def test_a_cold_codex_thread_takes_its_own_crews_session_over_another(monkeypatch):
    """Another crew's codex session can run a build that takes no effort."""
    _config(monkeypatch, member="codex")
    cold, sibling = _ChatSlot("member-helper", agent="helper"), _ChatSlot("side", agent="helper")
    own = _codex(effort=True)
    state = _state(cold, (own, _codex(effort=False)))
    state._slots[sibling.key] = sibling
    state.sessions.get_provider = lambda key: own if key == effective_session_key(sibling) else None

    data = await _caps(state, "member-helper")

    assert data["effort_supported"] is None
    assert data["effort_levels"] == ["low", "high"]


@pytest.mark.asyncio
async def test_a_cold_codex_thread_takes_the_newest_codex_sessions_answer(monkeypatch):
    """An older session holds what its build said at its own session/new."""
    _config(monkeypatch, member="codex")
    state = _state(_ChatSlot("member-helper"), (_codex(effort=False), _codex(effort=True)))

    data = await _caps(state, "member-helper")

    assert data["effort_supported"] is None
    assert data["effort_levels"] == ["low", "high"]


# ── a live codex session ──


def test_a_codex_build_without_its_effort_option_takes_no_effort(tmp_path):
    provider = AcpProvider(acp_backend=ACP_BACKEND_CODEX, work_dir=tmp_path)
    provider._client._model = "openai.gpt-5.6-sol"
    provider._client._acp_config_options = [{"id": "model", "options": []}]

    assert provider.supports_effort() is False

    provider._client._acp_config_options.append(
        {"id": "reasoning_effort", "options": [{"value": "low"}, {"value": "high"}]}
    )
    assert provider.supports_effort() is True


# ── resolved-model: the crew editor's Effort field ──


async def _resolved(*, member: str, state: object = None, model: str = "") -> dict:
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_resolved_model

    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default", model=model
    )
    cfg.save()
    # save() keeps the harness fields as stored, so the choice goes on disk.
    pin_harness_choice(acp_backend="", member_acp_backend=member)
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/agents/resolved-model", api_kirocrew_agent_resolved_model)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/agents/resolved-model", params={"agent": "writer"})
        assert resp.status == 200
        return json.loads(await resp.text())


@pytest.mark.asyncio
async def test_the_crew_editor_reads_a_cold_codex_route_as_unknown_not_unsupported():
    """With no codex session live, nothing says the pin is ignored, so the editor must not."""
    body = await _resolved(member="codex")

    assert body["effort_supported"] is None


@pytest.mark.asyncio
async def test_the_crew_editor_offers_no_effort_pin_a_live_codex_build_cannot_take():
    state = SimpleNamespace(
        sessions=SimpleNamespace(active_providers=lambda: [_codex(effort=False)])
    )

    body = await _resolved(member="codex", state=state)

    assert body["effort_supported"] is False


@pytest.mark.asyncio
async def test_the_crew_editor_takes_the_crews_own_codex_session_over_another():
    own = _codex(effort=True)
    thread = _ChatSlot("member-writer", agent="writer")
    sessions = SimpleNamespace(
        active_providers=lambda: [own, _codex(effort=False)],
        get_provider=lambda key: own if key == effective_session_key(thread) else None,
    )
    state = SimpleNamespace(sessions=sessions, _slots={thread.key: thread})

    body = await _resolved(member="codex", state=state, model="openai.gpt-6.1-sol")

    assert body["effort_supported"] is None
    assert body["effort_levels"] == ["low", "high"]


@pytest.mark.asyncio
@pytest.mark.parametrize("crew", ["writer", "other"], ids=["its-own-crews", "another-crews"])
async def test_no_codex_sessions_model_answers_for_a_crews_pin(tmp_path, crew):
    """Its build says the option exists; the model it runs can be a slot pick, not the pin."""
    live = AcpProvider(acp_backend=ACP_BACKEND_CODEX, work_dir=tmp_path)
    live._client._model = "deepseek-chat"  # takes no effort, and is not the crew's pin
    live._client._acp_config_options = [
        {"id": "reasoning_effort", "options": [{"value": "low"}, {"value": "high"}]}
    ]
    assert live.supports_effort() is False
    thread = _ChatSlot("member-x", agent=crew)
    sessions = SimpleNamespace(active_providers=lambda: [live], get_provider=lambda _key: live)
    state = SimpleNamespace(sessions=sessions, _slots={thread.key: thread})

    body = await _resolved(member="codex", state=state, model="openai.gpt-6.1-sol")

    assert body["effort_supported"] is None
    assert body["effort_levels"] == ["low", "high"]


@pytest.mark.asyncio
@pytest.mark.parametrize("member", ["claude", "", ACP_BACKEND_GOOSE, ACP_BACKEND_PI])
async def test_the_crew_editor_judges_the_pick_off_a_pair_id_route(tmp_path, member):
    """Only a pair-id build answers for any pick; every other verdict is one model's.

    A live session serving the pin is present, so a model-scoped answer would be sent
    if the route still offered one.
    """
    live = MagicMock()
    live.capabilities = capabilities_for(member)
    live.served_model = "pinned-model"
    live.supports_effort.return_value = True
    live.get_valid_effort_levels.return_value = ["low", "high"]
    state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: [live]))

    body = await _resolved(member=member, state=state, model="pinned-model")

    assert "effort_supported" not in body
    assert "effort_levels" not in body


@pytest.mark.asyncio
async def test_the_crew_editor_offers_only_the_levels_a_crew_pin_can_save():
    """A codex build's own levels can include off and minimal, which the crew-pin write refuses."""
    live = MagicMock()
    live.capabilities = capabilities_for(ACP_BACKEND_CODEX)
    live.served_model = "openai.gpt-6.1-sol"
    live.supports_effort.return_value = True
    live.get_valid_effort_levels.return_value = ["off", "minimal", "low", "medium", "high", "xhigh"]
    state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: [live]))

    body = await _resolved(member="codex", state=state, model="openai.gpt-6.1-sol")

    assert body["effort_levels"] == ["low", "medium", "high", "xhigh"]


# ── goose: another session's per-model answer is not the harness's ──


def _goose(tmp_path, effort_option: dict | None) -> AcpProvider:
    """A real goose session whose config options went through its own sync.

    ``_sync_effort_levels`` registers what the session advertises (goose's ``off``)
    into the process-wide vocabulary, as a production session does on every store.
    """
    live = AcpProvider(acp_backend=ACP_BACKEND_GOOSE, work_dir=tmp_path)
    live._client._model = "goose-model-without-thinking"
    live._client._resolved_model_id = live._client._model
    options: list[dict] = [{"id": "model", "options": []}]
    if effort_option is not None:
        options.append(effort_option)
    live._client._acp_config_options = options
    live._client._sync_effort_levels()
    return live


def _goose_state(live: AcpProvider, slot: _ChatSlot):
    """*live* belongs to another crew: no slot of *slot*'s crew maps onto it."""
    sessions = SimpleNamespace(active_providers=lambda: [live], get_provider=lambda _key: None)
    return SimpleNamespace(sessions=sessions, _slots={slot.key: slot})


_OFF_ONLY = {"id": "thinking_effort", "options": [{"value": "off"}]}
_NO_LEVELS = {"id": "thinking_effort", "options": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("option", [_OFF_ONLY, _NO_LEVELS], ids=["off-only", "no-levels"])
async def test_no_goose_sessions_model_answers_for_a_crews_pin(tmp_path, monkeypatch, option):
    """An off-only or empty option is the other session's model speaking, not goose's build."""
    live = _goose(tmp_path, option)
    if option is _OFF_ONLY:
        assert live.supports_effort() is False
        assert "off" in chat_handlers.get_reasoning_effort_values()
    cold = _ChatSlot("member-writer", agent="writer")

    _config(monkeypatch, member="goose")
    caps = await _caps(_goose_state(live, cold), cold.key)

    assert caps["effort_supported"] is None
    assert caps["effort_levels"] == []


@pytest.mark.asyncio
async def test_a_goose_build_without_its_effort_option_takes_no_effort(tmp_path, monkeypatch):
    """The absence is definite only when the borrowed session serves the target model."""
    live = _goose(tmp_path, None)
    cold = _ChatSlot("member-writer", agent="writer")

    _config(monkeypatch, member="goose", resolves_to=live.served_model)
    caps = await _caps(_goose_state(live, cold), cold.key)

    assert caps["effort_supported"] is False
    assert caps["effort_levels"] == []


@pytest.mark.asyncio
async def test_a_cold_goose_slot_offers_the_levels_a_real_goose_session_registered(
    tmp_path, monkeypatch
):
    """``off`` reaches the runtime vocabulary through a real sync, and a slot can take it."""
    option = {
        "id": "thinking_effort",
        "options": [{"value": "off"}, {"value": "low"}, {"value": "high"}],
    }
    live = _goose(tmp_path, option)
    cold = _ChatSlot("member-writer", agent="writer")

    _config(monkeypatch, member="goose", resolves_to=live.served_model)
    caps = await _caps(_goose_state(live, cold), cold.key)

    assert caps["effort_supported"] is True
    assert caps["effort_levels"] == ["off", "low", "high"]


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_id", [8, 9], ids=["plain-model", "reasoning-model-restored"])
@pytest.mark.parametrize("target_model", ["stub/plain-1", "stub/think-1", "", "auto"])
async def test_opencode_borrowed_answer_requires_the_same_served_model(
    tmp_path, monkeypatch, frame_id, target_model
):
    """Replay the captured option disappearing and returning on a borrowed live session."""
    capture = Path(__file__).parent / "fixtures" / "acp_frames" / "opencode" / "effort-live.jsonl"
    live = AcpProvider(acp_backend=ACP_BACKEND_OPENCODE, work_dir=tmp_path)
    for line in capture.read_text(encoding="utf-8").splitlines():
        frame = json.loads(line)
        options = frame.get("result", {}).get("configOptions")
        if options is not None:
            live._client._acp_config_options = options
            live._client._model = next(o["currentValue"] for o in options if o["id"] == "model")
            live._client._capture_available_models(frame["result"])
            live._client._sync_effort_levels()
        if frame.get("id") == frame_id:
            break
    reasoning = frame_id == 9
    assert live._client._model == ("stub/think-1" if reasoning else "stub/plain-1")
    assert live.supports_effort() is reasoning
    assert live.get_valid_effort_levels() == (["low", "medium", "high"] if reasoning else [])
    cold = _ChatSlot("member-writer", agent="writer")
    state = _state(cold, (live,))  # No session of this crew owns the borrowed provider.

    _config(monkeypatch, member="opencode", resolves_to=target_model)
    answer = await _caps(state, cold.key)

    same_model = target_model == live.served_model
    assert answer["effort_supported"] is (reasoning if same_model else None)
    assert answer["effort_levels"] == (
        ["low", "medium", "high"] if same_model and reasoning else []
    )


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION))
@pytest.mark.parametrize("supported", [True, False])
@pytest.mark.parametrize("served", ["target", "other", "", "auto"])
def test_model_scoped_borrowing_checks_served_identity_even_for_own_crew(
    backend, supported, served
):
    from kiro_crew.dashboard.handlers.agents_harness_effort import harness_effort

    live = MagicMock()
    live.capabilities = capabilities_for(backend)
    live.served_model = served
    live.supports_effort.return_value = supported
    live.get_valid_effort_levels.return_value = ["low", "high"] if supported else []
    own = _ChatSlot("member-writer", agent="writer")
    state = _state(own, (live,))
    state.sessions.get_provider.return_value = live

    answer = harness_effort(state, backend, "target", "writer")

    assert answer == {
        "effort_supported": supported if served == "target" else None,
        "effort_levels": ["low", "high"] if served == "target" and supported else [],
    }
