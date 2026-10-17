"""Per-chat backend: ``switch_slot_backend`` and the ``session_set_backend`` verb.

One path, pinned end to end: the switch's validation (the same resolver and
refusal codes ``spawn_run(backend=...)`` uses), its busy refusal, the session
reset that makes the next turn start on the new backend, the slot field's
persistence, the allocation keyword it adds (and does not add when unset), and
the agent verb reaching the switch behind the session-control gate.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_handlers import switch_slot_backend
from kiro_crew.dashboard.session_control import SessionControlError
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext


@pytest.fixture(autouse=True)
def selectable(_floor_monkeypatch):
    """Kiro, Claude and Codex can start; Pi is missing; KAS needs a restart."""
    from kiro_crew import acp_backends
    from kiro_crew.agent_sdk import backend_install
    from kiro_crew.agent_sdk.backend_install import INSTALLED, MISSING, BackendInstallState

    installs = {"": INSTALLED, "claude": INSTALLED, "codex": INSTALLED, "pi": MISSING}
    installs["kas"] = INSTALLED
    _floor_monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: sorted(installs))
    _floor_monkeypatch.setattr(
        backend_install,
        "probe_backend",
        lambda b: BackendInstallState(
            b,
            b or "kiro",
            installs[b],
            missing_components=("pi-acp",) if installs[b] == MISSING else (),
            install_command="npm i -g pi-acp" if installs[b] == MISSING else "",
            restart_required=b == "kas",
        ),
    )


def _mock_state(slot: _ChatSlot, provider: object = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=provider)
    return state


async def _post(state: DashboardState, body: dict, still_ours=lambda: True) -> tuple[int, dict]:
    slot = next(iter(state._slots.values()))
    try:
        out = await switch_slot_backend(
            state, slot, slot.key, body["backend"], still_ours=still_ours
        )
    except SessionControlError as refused:
        return refused.status, {"code": refused.code, "error": refused.message}
    return 200, out


# ── Endpoint ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_pick_is_saved_and_the_session_restarts_on_it():
    slot = _ChatSlot("test")
    slot._dirty = False
    state = _mock_state(slot)
    status, data = await _post(state, {"backend": "codex"})
    assert (status, data) == (200, {"ok": True, "backend": "codex"})
    assert slot.backend == "codex"
    assert slot._dirty is True
    state.sessions.reset.assert_awaited_once()
    state.push_slots_update.assert_called_once()


@pytest.mark.asyncio
async def test_an_empty_pick_returns_to_the_default():
    slot = _ChatSlot("test")
    slot.backend = "codex"
    state = _mock_state(slot)
    status, data = await _post(state, {"backend": ""})
    assert (status, data["backend"]) == (200, "")
    assert slot.backend == ""
    state.sessions.reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_same_pick_changes_nothing():
    slot = _ChatSlot("test")
    slot.backend = "claude"
    state = _mock_state(slot)
    status, _ = await _post(state, {"backend": "claude"})
    assert status == 200
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinning_the_backend_a_chat_already_runs_on_tears_nothing_down(_floor_monkeypatch):
    # On a Kiro gateway, "" (follow the default) and "kiro" run the same backend:
    # the pin is recorded, but the session, its model and its pending pick stay.
    from unittest.mock import MagicMock as _MM

    from kiro_crew.dashboard import chat_handlers

    _floor_monkeypatch.setattr(
        chat_handlers.KiroCrewConfig,
        "load",
        classmethod(lambda cls: _MM(agent=_MM(acp_backend="", member_acp_backend=""))),
    )
    slot = _ChatSlot("test")
    slot.model = "claude-sonnet-4.5"
    pending = object()
    slot._pending_model_pick = pending
    state = _mock_state(slot)
    status, data = await _post(state, {"backend": "kiro"})
    assert (status, data) == (200, {"ok": True, "backend": "kiro", "changed": False})
    assert slot.backend == "kiro"
    assert (slot.model, slot._pending_model_pick) == ("claude-sonnet-4.5", pending)
    state.sessions.reset.assert_not_awaited()


@pytest.mark.parametrize(
    ("pick", "code"),
    [
        ("nope", "unknown_backend"),
        ("pi", "backend_not_installed"),
        ("kas", "backend_restart_required"),
    ],
)
@pytest.mark.asyncio
async def test_a_backend_that_cannot_start_is_refused(pick, code):
    slot = _ChatSlot("test")
    state = _mock_state(slot)
    status, data = await _post(state, {"backend": pick})
    assert (status, data["code"]) == (400, code)
    # The backends that can start, as spawn_run's refusal lists them.
    assert "['claude', 'codex', 'kiro']" in data["error"]
    assert slot.backend == ""
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_turn_in_flight_keeps_the_backend():
    slot = _ChatSlot("test")
    running = MagicMock()
    running.done.return_value = False
    slot.task = running
    state = _mock_state(slot)
    status, data = await _post(state, {"backend": "codex"})
    assert (status, data["code"]) == (409, "turn_in_flight")
    assert slot.backend == ""
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_switch_clears_the_previous_backends_model():
    slot = _ChatSlot("test")
    slot.model = "claude-opus-5.5"
    gen = slot._model_pick_gen
    state = _mock_state(slot)
    status, _ = await _post(state, {"backend": "codex"})
    assert status == 200
    assert (slot.model, slot.jev_route) == ("", False)
    assert slot._model_pick_gen == gen + 1


@pytest.mark.asyncio
async def test_a_switch_drops_a_pending_model_pick_for_the_old_backend():
    slot = _ChatSlot("test")
    slot._pending_model_pick = object()
    gen = slot._model_pick_gen
    state = _mock_state(slot)
    status, _ = await _post(state, {"backend": "codex"})
    assert status == 200
    assert slot._pending_model_pick is None
    assert slot._model_pick_gen == gen + 1


@pytest.mark.asyncio
async def test_the_switch_holds_the_model_pick_lock_across_its_reset():
    # A model pick cannot land between the switch clearing the pending pick
    # and a rollback restoring it.
    slot = _ChatSlot("test")
    state = _mock_state(slot)
    held: list[bool] = []

    async def _reset(*_args, **_kwargs):
        held.append(slot._model_pick_lock.locked())
        return True

    state.sessions.reset = AsyncMock(side_effect=_reset)
    status, _ = await _post(state, {"backend": "codex"})
    assert status == 200
    assert held == [True]
    assert not slot._model_pick_lock.locked()


@pytest.mark.asyncio
async def test_a_rolled_back_switch_keeps_the_pending_model_pick_current():
    pending = object()
    slot = _ChatSlot("test")
    slot._pending_model_pick = pending
    gen = slot._model_pick_gen
    state = _mock_state(slot, provider=_idle_live_provider())
    state.sessions.reset = AsyncMock(return_value=False)
    status, _ = await _post(state, {"backend": "codex"})
    assert status == 409
    assert (slot._pending_model_pick, slot._model_pick_gen) == (pending, gen)


@pytest.mark.asyncio
async def test_a_crewmate_dm_session_is_refused():
    slot = _ChatSlot("member-alice")
    state = _mock_state(slot)
    status, data = await _post(state, {"backend": "codex"})
    assert (status, data["code"]) == (409, "member_session")
    assert slot.backend == ""


def _idle_live_provider():
    from kiro_crew.providers.base import LLMProvider

    provider = MagicMock(spec=LLMProvider)
    provider.has_active_turn = MagicMock(return_value=False)
    return provider


@pytest.mark.asyncio
async def test_a_live_session_that_keeps_declining_rolls_the_pick_back():
    slot = _ChatSlot("test")
    slot.model = "claude-opus-5.5"
    state = _mock_state(slot, provider=_idle_live_provider())
    state.sessions.reset = AsyncMock(return_value=False)
    status, data = await _post(state, {"backend": "codex"})
    assert (status, data["code"]) == (409, "turn_in_flight")
    assert (slot.backend, slot.model) == ("", "claude-opus-5.5")
    # The idle decline was retried once before giving up.
    assert state.sessions.reset.await_count == 2


@pytest.mark.asyncio
async def test_a_chat_with_no_live_session_keeps_the_pick():
    """``reset`` answers False when there is no session to tear down."""
    slot = _ChatSlot("test")
    state = _mock_state(slot, provider=None)
    state.sessions.reset = AsyncMock(return_value=False)
    status, data = await _post(state, {"backend": "codex"})
    assert (status, data) == (200, {"ok": True, "backend": "codex"})
    assert slot.backend == "codex"


@pytest.mark.asyncio
async def test_a_gate_that_turns_false_after_the_sub_agent_probe_changes_nothing():
    """The probe awaits; a target that left the caller's reach meanwhile is not switched."""
    slot = _ChatSlot("test")
    state = _mock_state(slot)
    answers = iter([True, True, False])
    status, data = await _post(state, {"backend": "codex"}, still_ours=lambda: next(answers))
    assert (status, data["code"]) == (404, "target_replaced")
    assert slot.backend == ""
    state.sessions.reset.assert_not_awaited()


# ── Allocation ────────────────────────────────────────────────────────────────


def test_an_unpicked_chat_adds_no_allocation_keyword():
    assert chat_runner.slot_backend_override(_ChatSlot("test")) == {}


@pytest.mark.parametrize(("pick", "backend_id"), [("codex", "codex"), ("kiro", "")])
def test_a_pick_reaches_allocation_as_the_backend_id(pick, backend_id):
    slot = _ChatSlot("test")
    slot.backend = pick
    assert chat_runner.slot_backend_override(slot) == {"backend_override": backend_id}


@pytest.mark.asyncio
@pytest.mark.parametrize(("pick", "expected"), [("codex", {"backend_override": "codex"}), ("", {})])
async def test_a_turn_allocates_its_session_with_the_pick(pick, expected):
    from turn_harness import TurnContext, TurnScript, run_turn

    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent

    def _arrange(ctx: TurnContext) -> None:
        ctx.slot.backend = pick

    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="an answer"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            setup=_arrange,
        )
    )
    [alloc] = [call for call in record.session_calls if call.name == "get_or_create"]
    assert {k: v for k, v in alloc.kwargs.items() if k == "backend_override"} == expected


@pytest.mark.asyncio
async def test_a_cold_picked_chat_reports_its_own_backend_for_capabilities(_floor_monkeypatch):
    from kiro_crew import members
    from kiro_crew.dashboard import chat_handlers

    seen: list = []
    _floor_monkeypatch.setattr(
        members, "select_provider_backend", lambda *a: seen.append(a[3]) or "codex"
    )
    slot = _ChatSlot("test")
    slot.backend = "codex"
    assert await chat_handlers._configured_backend_for_slot(slot) == "codex"
    assert seen == ["codex"]


@pytest.mark.asyncio
async def test_a_slash_command_on_a_cold_picked_chat_judges_its_own_backend(_floor_monkeypatch):
    from kiro_crew import members

    seen: list = []
    _floor_monkeypatch.setattr(
        members, "select_provider_backend", lambda *a: seen.append(a[3]) or "codex"
    )
    slot = _ChatSlot("test")
    slot.backend = "codex"
    state = _mock_state(slot, provider=None)
    assert await chat_runner._slash_command_backend(state, "dashboard:test", slot) == "codex"
    assert seen == ["codex"]


def test_a_backend_change_counts_as_a_binding_change_for_the_eager_handshake():
    slot = _ChatSlot("test")
    before = chat_runner._slot_binding(slot)
    slot.backend = "codex"
    assert chat_runner._slot_binding(slot) != before


# ── Persistence ───────────────────────────────────────────────────────────────


def test_the_pick_round_trips_through_the_metadata_line():
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec

    row = next(r for r in codec.FIELDS if r.key == "backend")
    slot = _ChatSlot("test")
    folds = codec.SaveFolds(memory_mode="persistent")
    assert row.line is not None and row.line(slot, folds) is codec.OMIT
    slot.backend = "codex"
    assert row.line(slot, folds) == "codex"
    restored = _ChatSlot("again")
    read = MagicMock(slot=restored, meta={"backend": "codex"})
    assert row.read is not None
    row.read(read)
    assert restored.backend == "codex"
    # A cleared pick reaches a line already on disk through the merge.
    slot.backend = ""
    assert row.merge is not None and row.merge(slot, folds) == ""


# ── Agent tool (session_set_backend) ──────────────────────────────────────────


@pytest.fixture
def _verb(_floor_monkeypatch, tmp_path):
    """A real dashboard state with two chats, session control on, SEL calls captured."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard import session_control as sc

    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    _floor_monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", MagicMock())
    audits: list[dict] = []
    _floor_monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target._created_by = caller.key
    state.sessions.get_provider = MagicMock(return_value=None)
    state.sessions.reset = AsyncMock(return_value=True)
    return sc, state, caller, target, audits


def _set_backend(sc, state, caller, target="chat-2", backend="codex", fenced=None):
    from kiro_crew.dashboard.chat_utils import slot_history_key

    return asyncio.run(
        sc.set_backend_target(
            state,
            caller_session_key=slot_history_key(caller),
            target=target,
            backend=backend,
            caller_fenced=fenced,
        )
    )


def test_the_verb_switches_a_reachable_session_and_records_it(_verb):
    sc, state, caller, target, audits = _verb
    out = _set_backend(sc, state, caller)
    assert out == {"ok": True, "target": "chat-2", "backend": "codex"}
    assert target.backend == "codex"
    assert (audits[-1]["operation"], audits[-1]["outcome"]) == ("set_backend", "allowed")
    notice = target.messages[-1]
    assert notice["meta"]["kind"] == "session_reload"
    assert "`codex`" in notice["content"] and "`chat-1`" in notice["content"]
    # The notice marks where a replay to the new backend starts.
    from kiro_crew.context import REPLAY_CUTOFF_META_KEY

    assert notice["meta"][REPLAY_CUTOFF_META_KEY] is True


def test_the_notice_loads_a_lazy_transcript_off_the_loop_first(_verb, _floor_monkeypatch):
    sc, state, caller, target, _audits = _verb
    seen: list[int] = []

    def _notices() -> int:
        return sum((m.get("meta") or {}).get("kind") == "session_reload" for m in target.messages)

    async def _load(slot):
        assert slot is target
        seen.append(_notices())

    from kiro_crew.dashboard import chat_handlers

    _floor_monkeypatch.setattr(chat_handlers, "load_window_off_loop", _load)
    _set_backend(sc, state, caller)
    # Loaded once, before the notice was appended.
    assert seen == [0] and _notices() == 1


def test_re_picking_the_current_backend_posts_no_notice(_verb):
    sc, state, caller, target, audits = _verb
    _set_backend(sc, state, caller)
    before = len(target.messages)
    _set_backend(sc, state, caller)
    assert len(target.messages) == before
    assert audits[-1]["detail"]["changed"] is False


def test_a_refused_switch_posts_no_notice(_verb):
    sc, state, caller, target, _audits = _verb
    before = len(target.messages)
    with pytest.raises(sc.SessionControlError):
        _set_backend(sc, state, caller, backend="pi")
    assert len(target.messages) == before


def test_the_verb_refuses_the_caller_itself(_verb):
    sc, state, caller, _target, _audits = _verb
    with pytest.raises(sc.SessionControlError) as exc:
        _set_backend(sc, state, caller, target="chat-1")
    assert exc.value.code == "self_target"
    assert caller.backend == ""


def test_a_fenced_caller_cannot_reach_a_session_it_did_not_create(_verb):
    sc, state, caller, target, _audits = _verb
    target._created_by = ""
    with pytest.raises(sc.SessionControlError) as exc:
        _set_backend(sc, state, caller, fenced=True)
    assert exc.value.code == "not_creator"
    assert target.backend == ""
    state.sessions.reset.assert_not_awaited()


def test_the_verb_is_refused_when_session_control_is_off(_verb, _floor_monkeypatch):
    sc, state, caller, target, _audits = _verb
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    with pytest.raises(sc.SessionControlError):
        _set_backend(sc, state, caller)
    assert target.backend == ""


def test_the_verb_reports_the_switchs_own_refusal_and_audits_it(_verb):
    sc, state, caller, target, audits = _verb
    with pytest.raises(sc.SessionControlError) as exc:
        _set_backend(sc, state, caller, backend="pi")
    assert exc.value.status == 400
    assert target.backend == ""
    assert audits[-1]["outcome"] == "denied"


def test_the_tool_accepts_an_empty_backend_to_return_to_the_default():
    seen: list = []

    def reply(request):
        seen.append(request.body)
        return {"ok": True, "target": "chat-2", "backend": ""}

    dash = InMemoryDashboardClient({"POST /api/session-control/set-backend": reply})
    ctx = ToolContext(dash, Caller.strict("dashboard:caller"))
    out = TABLE.call("session_set_backend", {"target": "chat-2", "backend": ""}, ctx)
    assert "gateway default" in out and not out.startswith("Error")
    assert seen == [{"target": "chat-2", "backend": ""}]


def test_the_tool_refuses_a_call_without_a_backend():
    dash = InMemoryDashboardClient({})
    ctx = ToolContext(dash, Caller.strict("dashboard:caller"))
    out = TABLE.call("session_set_backend", {"target": "chat-2"}, ctx)
    assert out.startswith("Error") and "backend" in out


def test_the_tool_posts_to_the_session_control_route():
    from kiro_crew.mcp_tools.dashboard_client import DashboardRequest

    seen: list[DashboardRequest] = []

    def reply(request):
        seen.append(request)
        return {"ok": True, "target": "chat-2", "backend": "codex"}

    dash = InMemoryDashboardClient({"POST /api/session-control/set-backend": reply})
    ctx = ToolContext(dash, Caller.strict("dashboard:caller"))
    out = TABLE.call("session_set_backend", {"target": "chat-2", "backend": "codex"}, ctx)
    assert "codex" in out and not out.startswith("Error")
    assert seen[0].body == {"target": "chat-2", "backend": "codex"}
    assert seen[0].session_key == "dashboard:caller"


# ── Fork ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fork_keeps_the_parents_backend_pick(tmp_path, monkeypatch):
    # A fork carries the parent's model, a model id of the picked backend, so it
    # carries the pick with it.
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app, _make_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    parent.append("user", "hello", "msg msg-u")
    parent.append("assistant", "hi", "msg msg-a")
    parent._resumed_count = len(parent.messages)
    parent._disk_window_len = len(parent.messages)
    parent.backend = "codex"
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/parent/fork", json={})
        body = await resp.json()
    assert resp.status == 200
    assert state._slots[body["key"]].backend == "codex"


@pytest.mark.asyncio
async def test_the_dashboard_model_check_judges_a_picked_chat_on_its_pick(monkeypatch):
    # A person's model pick on a chat an agent moved to another backend is
    # checked against that backend, on the single-chat and the all-chats route.
    from aiohttp.test_utils import TestClient, TestServer
    from test_chat_slot_switch_atomicity import _make_app, _mock_state

    from kiro_crew import members, model_registry

    key = next(
        e["model_name"]
        for e in model_registry.display_list("claude_code")
        if model_registry.is_canonical_key(e["model_name"]) and e["model_name"] != "auto"
    )
    seen: list[object] = []

    def _gate(session_key, member_backend, default_backend, override=None):
        seen.append(override)
        return default_backend

    monkeypatch.setattr(members, "select_provider_backend", _gate)
    slot = _ChatSlot("test")
    slot.backend = "claude"
    pick = chat_runner.slot_backend_override(slot)["backend_override"]
    state = _mock_state(slot)
    async with TestClient(TestServer(_make_app(state))) as client:
        await client.post("/api/chat/slots/test/model", json={"model": key})
        await client.post("/api/chat/slots/model", json={"model": key})
    assert seen == [pick, pick]


def _deny(monkeypatch, *denied: str) -> None:
    from kiro_crew import acp_backends
    from kiro_crew.agent_sdk import backends

    real = backends.resolve_selected_backend

    def gate(value: str) -> str:
        return "" if value in denied else real(value)

    # The members gate reads the name through ``acp_backends``.
    for module in (backends, acp_backends):
        monkeypatch.setattr(module, "resolve_selected_backend", gate)


@pytest.mark.asyncio
async def test_a_turn_on_a_disallowed_pick_starts_on_the_fallbacks_default_model(monkeypatch):
    from turn_harness import TurnContext, TurnScript, run_turn

    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent

    _deny(monkeypatch, "codex")

    def _arrange(ctx: TurnContext) -> None:
        ctx.slot.backend = "codex"
        ctx.slot.model = "gpt-5.6-sol"

    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="an answer"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            setup=_arrange,
        )
    )
    [alloc] = [call for call in record.session_calls if call.name == "get_or_create"]
    # The pick is kept and the factory's gate degrades it to Kiro (the existing
    # fallback). The model chosen for the pick is not sent to the fallback: the
    # turn starts on its default model.
    assert alloc.kwargs.get("backend_override") == "codex"
    assert alloc.kwargs.get("model") is None
    assert record.stop_reason == "end_turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("pick", ["", "kiro", "codex"])
async def test_a_pick_the_gate_keeps_sends_its_model(monkeypatch, pick):
    from turn_harness import TurnContext, TurnScript, run_turn

    from kiro_crew import acp_backends
    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
    from kiro_crew.agent_sdk import backends

    real = backends.resolve_selected_backend

    def gate(value: str) -> str:
        return "codex" if value == "codex" else real(value)

    for module in (backends, acp_backends):
        monkeypatch.setattr(module, "resolve_selected_backend", gate)

    def _arrange(ctx: TurnContext) -> None:
        ctx.slot.backend = pick
        ctx.slot.model = "a-picked-model"

    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="an answer"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ],
            setup=_arrange,
        )
    )
    [alloc] = [call for call in record.session_calls if call.name == "get_or_create"]
    assert alloc.kwargs.get("model") == "a-picked-model"


@pytest.mark.asyncio
async def test_the_all_chats_pick_skips_a_pinned_chat_it_does_not_suit(monkeypatch):
    # A pinned chat whose backend does not run the model keeps its own model
    # and is reported as unchanged; the unpinned chats still switch.
    from aiohttp.test_utils import TestClient, TestServer
    from test_chat_slot_switch_atomicity import _make_app, _mock_state

    from kiro_crew import members, model_registry
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX

    key = next(
        e["model_name"]
        for e in model_registry.display_list("claude_code")
        if model_registry.is_canonical_key(e["model_name"]) and e["model_name"] != "auto"
    )
    monkeypatch.setattr(
        members,
        "select_provider_backend",
        lambda k, m, d, o=None: ACP_BACKEND_CODEX if o else ACP_BACKEND_CLAUDE,
    )
    a = _ChatSlot("a")
    b = _ChatSlot("b")
    b.backend = "codex"
    b.model = "gpt-5.6-sol"
    state = _mock_state(a)
    state._slots = {"a": a, "b": b}
    state.sessions.reset.return_value = True
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/model", json={"model": key})
        body = await resp.json()
    assert resp.status == 200
    assert "incompatible" not in body
    assert body["unchanged"] == ["b"]
    assert b.model == "gpt-5.6-sol"
    assert "a" in body["switched"]


@pytest.mark.asyncio
async def test_the_all_chats_pick_rechecks_a_chat_unpinned_while_it_runs(monkeypatch):
    # The up-front check skips pinned chats. A chat unpinned while an earlier
    # chat's reset awaits is checked again under its locks, so it never takes
    # a model its now-current backend cannot run.
    from aiohttp.test_utils import TestClient, TestServer
    from test_chat_slot_switch_atomicity import _make_app, _mock_state

    from kiro_crew import members, model_registry
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX

    key = next(
        e["model_name"]
        for e in model_registry.display_list("claude_code")
        if model_registry.is_canonical_key(e["model_name"]) and e["model_name"] != "auto"
    )
    monkeypatch.setattr(
        members,
        "select_provider_backend",
        lambda k, m, d, o=None: ACP_BACKEND_CLAUDE if o else ACP_BACKEND_CODEX,
    )
    a = _ChatSlot("a")
    b = _ChatSlot("b")
    a.backend = b.backend = "claude"
    b.model = "gpt-5.6-sol"
    state = _mock_state(a)
    state._slots = {"a": a, "b": b}

    async def _reset_then_unpin(*_a, **_kw):
        b.backend = ""
        return True

    state.sessions.reset.side_effect = _reset_then_unpin
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/model", json={"model": key})
        body = await resp.json()
    assert resp.status == 200
    assert "a" in body["switched"]
    assert body["unchanged"] == ["b"]
    assert b.model == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_a_direct_switch_cuts_every_later_replay():
    # The shared switch writes the cutoff itself, so any caller (the agent tool
    # now, the dropdown later) cuts the history for every later cold start.
    from kiro_crew.context import REPLAY_CUTOFF_META_KEY, build_session_replay

    slot = _ChatSlot("test")
    slot.append("user", "before the switch")
    state = _mock_state(slot)
    status, _ = await _post(state, {"backend": "codex"})
    assert status == 200
    notice = slot.messages[-1]
    assert notice["meta"][REPLAY_CUTOFF_META_KEY] is True
    slot.append("user", "after the switch")
    for _cold_start in range(2):
        replay = build_session_replay(None, "k", pending_messages=list(slot.messages)) or ""
        assert "after the switch" in replay and "before the switch" not in replay


@pytest.mark.asyncio
async def test_a_switch_that_changes_nothing_adds_no_cutoff():
    slot = _ChatSlot("test")
    slot.backend = "codex"
    state = _mock_state(slot)
    await _post(state, {"backend": "codex"})
    assert slot.messages == []


@pytest.mark.asyncio
async def test_a_rolled_back_switch_adds_no_cutoff():
    from kiro_crew.providers.base import LLMProvider

    slot = _ChatSlot("test")
    state = _mock_state(slot, provider=MagicMock(spec=LLMProvider))
    state.sessions.get_provider.return_value.has_active_turn.return_value = True
    state.sessions.reset = AsyncMock(return_value=False)
    status, _ = await _post(state, {"backend": "codex"})
    assert status == 409
    assert slot.messages == []


@pytest.mark.asyncio
async def test_side_chat_after_a_switch_sends_no_pre_switch_history():
    from kiro_crew.dashboard.side_context import build_side_message
    from kiro_crew.dashboard.side_state import SideState

    slot = _ChatSlot("test")
    slot.append("user", "parent before")
    slot._side = SideState(open=True)
    slot._side.append_user("side before")
    slot._side.append_assistant("side answer before")
    state = _mock_state(slot)
    await _post(state, {"backend": "codex"})
    slot.append("user", "parent after")
    slot._side.append_user("side after")
    slot._side.append_assistant("side answer after")
    message = build_side_message(slot, "now?", is_first_turn=True)
    assert "parent after" in message and "side after" in message
    assert "parent before" not in message
    assert "side before" not in message and "side answer before" not in message


class _Log:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def read_messages_chained(self, session_key: str) -> list[dict]:
        return list(self.rows)


def test_replay_after_a_backend_switch_starts_at_the_switch():
    # Every cold start after a switch, not only the first, replays only what came
    # after it, from the disk transcript and from the live window alike.
    from kiro_crew.context import REPLAY_CUTOFF_META_KEY, build_session_replay

    disk = [
        {"role": "user", "content": "before the switch"},
        {"role": "assistant", "content": "an old answer"},
        {
            "role": "assistant",
            "content": "Backend switched",
            "meta": {REPLAY_CUTOFF_META_KEY: True},
        },
        {"role": "user", "content": "after the switch"},
    ]
    replay = build_session_replay(_Log(disk), "dashboard:k") or ""
    assert "after the switch" in replay
    assert "before the switch" not in replay and "an old answer" not in replay
    live = build_session_replay(_Log([]), "dashboard:k", pending_messages=disk) or ""
    assert "after the switch" in live and "before the switch" not in live


def test_replay_without_a_switch_keeps_the_whole_conversation():
    from kiro_crew.context import build_session_replay

    rows = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "second"}]
    replay = build_session_replay(_Log(rows), "dashboard:k") or ""
    assert "first" in replay and "second" in replay
