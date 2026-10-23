"""Per-chat backend: ``switch_slot_backend`` and the ``session_set_backend`` verb.

One path, pinned end to end: the switch's validation (the same resolver and
refusal codes ``spawn_run(backend=...)`` uses), its busy refusal, the session
reset that makes the next turn start on the new backend, the slot field's
persistence, the allocation keyword it adds (and does not add when unset), and
the agent verb reaching the switch behind the session-control gate.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp_backends import ACP_BACKEND_KIRO
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_handlers import switch_slot_backend
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.dashboard.session_control import SessionControlError
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext
from kiro_crew.testing.wait import async_wait_until


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
    state.sessions.allocation_backend_override = MagicMock(return_value=None)
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
async def test_a_pick_is_saved_and_the_next_turn_resets_onto_it():
    # The swap happens between turns: nothing is torn down now, a reset is
    # queued for the next turn start to run before it acquires a session.
    slot = _ChatSlot("test")
    slot._dirty = False
    state = _mock_state(slot, provider=MagicMock())
    status, data = await _post(state, {"backend": "codex"})
    assert (status, data) == (200, {"ok": True, "backend": "codex"})
    assert slot.backend == "codex"
    assert slot._dirty is True
    state.sessions.reset.assert_not_awaited()
    assert slot._pending_reset_history_key == effective_session_key(slot)
    state.push_slots_update.assert_called_once()


@pytest.mark.asyncio
async def test_an_empty_pick_returns_to_the_default():
    slot = _ChatSlot("test")
    slot.backend = "codex"
    state = _mock_state(slot)
    status, data = await _post(state, {"backend": ""})
    assert (status, data["backend"]) == (200, "")
    assert slot.backend == ""
    state.sessions.reset.assert_not_awaited()


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
async def test_a_turn_in_flight_finishes_on_the_old_backend():
    # A switch during a turn is saved and applied at the next turn start; the
    # running turn's session is not touched.
    slot = _ChatSlot("test")
    running = MagicMock()
    running.done.return_value = False
    slot.task = running
    state = _mock_state(slot, provider=MagicMock())
    status, _ = await _post(state, {"backend": "codex"})
    assert status == 200
    assert slot.backend == "codex"
    state.sessions.reset.assert_not_awaited()
    running.cancel.assert_not_called()
    assert slot._pending_reset_history_key == effective_session_key(slot)


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
async def test_a_chat_with_no_live_session_keeps_the_pick():
    """Nothing to tear down: the next turn's session starts on the pick."""
    slot = _ChatSlot("test")
    state = _mock_state(slot, provider=None)
    status, data = await _post(state, {"backend": "codex"})
    assert (status, data) == (200, {"ok": True, "backend": "codex"})
    assert slot.backend == "codex"
    assert slot._pending_reset_history_key is None


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


def _provider_runs(ctx, backend: str) -> None:
    # The scripted session comes up on the backend the turn allocated it for,
    # as a real one does, so the post-claim pick check keeps it.
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    assert ctx.provider is not None
    ctx.provider.capabilities = capabilities_for(backend)


@pytest.mark.asyncio
@pytest.mark.parametrize(("pick", "expected"), [("codex", {"backend_override": "codex"}), ("", {})])
async def test_a_turn_allocates_its_session_with_the_pick(pick, expected):
    from turn_harness import TurnContext, TurnScript, run_turn

    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent

    def _arrange(ctx: TurnContext) -> None:
        ctx.slot.backend = pick
        _provider_runs(ctx, expected.get("backend_override", ""))

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


@pytest.mark.asyncio
async def test_a_slash_command_after_a_switch_judges_the_next_turns_backend(_floor_monkeypatch):
    # The switch queued a reset, so the live Kiro session is retiring: a slash
    # command before the next turn must be judged on the picked backend.
    from kiro_crew import members

    _floor_monkeypatch.setattr(members, "select_provider_backend", lambda *a: a[3] or "")
    live = MagicMock()
    _floor_monkeypatch.setattr(
        chat_runner, "capabilities_of", lambda p: SimpleNamespace(backend=ACP_BACKEND_KIRO)
    )
    slot = _ChatSlot("test")
    slot.backend = "claude"
    slot._pending_reset_history_key = "dashboard:test"
    state = _mock_state(slot, provider=live)
    state.sessions.is_provider_alive = AsyncMock(return_value=True)
    assert await chat_runner._slash_command_backend(state, "dashboard:test", slot) == "claude"
    # Without a queued reset the live session still answers for itself.
    slot._pending_reset_history_key = None
    assert (
        await chat_runner._slash_command_backend(state, "dashboard:test", slot) == ACP_BACKEND_KIRO
    )


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
    state.sessions._has_allocation_reservation = MagicMock(return_value=False)
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
    # The person can see the switch sends the conversation to the new backend.
    assert "replayed" in notice["content"]


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


def test_a_no_op_switch_reports_that_nothing_changed(_verb):
    sc, state, caller, _target, _audits = _verb
    _set_backend(sc, state, caller)
    assert _set_backend(sc, state, caller)["changed"] is False


def test_the_tool_says_a_no_op_switch_changed_nothing():
    dash = InMemoryDashboardClient(
        {
            "POST /api/session-control/set-backend": lambda request: {
                "ok": True,
                "target": "chat-2",
                "backend": "codex",
                "changed": False,
            }
        }
    )
    ctx = ToolContext(dash, Caller.strict("dashboard:caller"))
    out = TABLE.call("session_set_backend", {"target": "chat-2", "backend": "codex"}, ctx)
    assert "nothing changed" in out and "replayed" not in out


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
        _provider_runs(ctx, "codex" if pick == "codex" else "")

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
async def test_a_switch_carries_the_conversation_to_the_new_backend():
    # The new backend's cold start replays the chat, as a model switch does, so
    # a chat moved off a spent quota picks up where the old backend stopped.
    from kiro_crew.context import build_session_replay

    slot = _ChatSlot("test")
    slot.append("user", "before the switch")
    state = _mock_state(slot)
    status, _ = await _post(state, {"backend": "codex"})
    assert status == 200
    slot.append("user", "after the switch")
    replay = build_session_replay(None, "k", pending_messages=list(slot.messages)) or ""
    assert "before the switch" in replay and "after the switch" in replay


@pytest.mark.asyncio
async def test_a_switch_that_changes_nothing_posts_no_notice():
    slot = _ChatSlot("test")
    slot.backend = "codex"
    state = _mock_state(slot)
    await _post(state, {"backend": "codex"})
    assert slot.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pick", "allowed"),
    [pytest.param("kiro", "", id="kiro-pick"), pytest.param("", "claude", id="unpicked")],
)
async def test_the_app_agent_host_check_follows_the_pick(monkeypatch, pick, allowed):
    # On a Claude gateway with no live session, an app-agent chat picked onto Kiro
    # is judged on Kiro; an unpicked chat is still judged on the gateway default.
    from types import SimpleNamespace

    from kiro_crew import members
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
    from kiro_crew.dashboard.chat_turn.app_agent_binding import _app_agent_turn_backend

    monkeypatch.setattr(
        members,
        "select_provider_backend",
        lambda k, m, d, o=None: ACP_BACKEND_KIRO if o is not None else ACP_BACKEND_CLAUDE,
    )
    slot = _ChatSlot("test")
    slot.backend = pick
    state = SimpleNamespace(
        sessions=SimpleNamespace(
            get_provider=lambda key: None, is_provider_alive=AsyncMock(return_value=False)
        )
    )
    cfg = SimpleNamespace(agent=SimpleNamespace(member_acp_backend="", acp_backend="claude"))
    assert await _app_agent_turn_backend(state, "dashboard:test", cfg, slot) == allowed


@pytest.mark.asyncio
async def test_the_app_agent_host_check_after_a_switch_judges_the_next_turns_backend(monkeypatch):
    # The switch queued a reset, so the live KAS session is retiring: the turn
    # about to start runs on the picked Kiro backend, not on KAS.
    from types import SimpleNamespace

    from kiro_crew import members
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO
    from kiro_crew.dashboard.chat_turn import app_agent_binding

    monkeypatch.setattr(
        members, "select_provider_backend", lambda k, m, d, o=None: ACP_BACKEND_KIRO
    )
    monkeypatch.setattr(chat_runner, "_provider_backend", lambda p: "kas")
    slot = _ChatSlot("test")
    slot.backend = "kiro"
    slot._pending_reset_history_key = "dashboard:test"
    state = SimpleNamespace(
        sessions=SimpleNamespace(
            get_provider=lambda key: MagicMock(), is_provider_alive=AsyncMock(return_value=True)
        )
    )
    cfg = SimpleNamespace(agent=SimpleNamespace(member_acp_backend="", acp_backend="kas"))
    turn_backend = app_agent_binding._app_agent_turn_backend
    assert await turn_backend(state, "dashboard:test", cfg, slot) == ACP_BACKEND_KIRO
    # Without a queued reset the live session still answers for itself.
    slot._pending_reset_history_key = None
    assert await turn_backend(state, "dashboard:test", cfg, slot) == "kas"


@pytest.mark.asyncio
async def test_a_switch_deferred_by_sub_agents_leaves_the_live_backend_answering(monkeypatch):
    # Sub-agents attached defer the queued reset, so the next turn still runs on
    # the live KAS session: /clear and the app-agent check must judge KAS.
    from types import SimpleNamespace

    from kiro_crew import members
    from kiro_crew.dashboard.chat_turn import app_agent_binding

    monkeypatch.setattr(
        members, "select_provider_backend", lambda k, m, d, o=None: ACP_BACKEND_KIRO
    )
    monkeypatch.setattr(chat_runner, "_provider_backend", lambda p: "kas")
    monkeypatch.setattr(chat_runner, "capabilities_of", lambda p: SimpleNamespace(backend="kas"))
    attached = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_runner, "subagents_attached_async", attached)
    slot = _ChatSlot("test")
    slot.backend = "kiro"
    slot._pending_reset_history_key = "dashboard:test"
    state = SimpleNamespace(
        sessions=SimpleNamespace(
            get_provider=lambda key: MagicMock(), is_provider_alive=AsyncMock(return_value=True)
        )
    )
    cfg = SimpleNamespace(agent=SimpleNamespace(member_acp_backend="", acp_backend="kas"))
    assert await chat_runner._slash_command_backend(state, "dashboard:test", slot) == "kas"
    turn_backend = app_agent_binding._app_agent_turn_backend
    assert await turn_backend(state, "dashboard:test", cfg, slot) == "kas"
    # With no sub-agents the reset applies at the next turn, so the pick answers.
    attached.return_value = False
    assert (
        await chat_runner._slash_command_backend(state, "dashboard:test", slot) == ACP_BACKEND_KIRO
    )
    assert await turn_backend(state, "dashboard:test", cfg, slot) == ACP_BACKEND_KIRO


@pytest.mark.asyncio
async def test_compact_after_a_switch_is_not_refused_by_the_retiring_backend(tmp_path):
    # A live DeepSeek session cannot compact, but the switch to Kiro queued a
    # reset: /compact must not answer with DeepSeek's refusal.
    from chat_runner_coverage_helpers import _drive, _runner_state, _set_stream, _slot

    from kiro_crew.messaging.commands import compact_unsupported_reply

    state, client = _runner_state(tmp_path)
    _set_stream(client, [])
    live = MagicMock(manual_compact_unsupported_backend="deepseek")
    state.sessions.get_provider = MagicMock(return_value=live)
    state.sessions.is_provider_alive = AsyncMock(return_value=True)
    slot = _slot()
    slot.backend = "kiro"
    slot._pending_reset_history_key = effective_session_key(slot)
    await _drive(state, slot, "/compact")
    refusal = compact_unsupported_reply("deepseek")
    assert not any(m.get("content") == refusal for m in slot.messages), slot.messages


@pytest.mark.asyncio
async def test_compact_refused_when_the_queued_reset_left_the_old_backend_live(tmp_path):
    # Sub-agents keep the queued reset waiting, so the turn claims the DeepSeek
    # session after all: /compact is refused there, not sent to wait for a
    # compaction status DeepSeek never sends.
    from chat_runner_coverage_helpers import _quiet_sel, _runner_state, _set_stream, _settle, _slot

    from kiro_crew.messaging.commands import compact_unsupported_reply

    state, client = _runner_state(tmp_path)
    _set_stream(client, [])
    client.manual_compact_unsupported_backend = "deepseek"
    client.wait_for_compaction = AsyncMock(return_value={"type": "timeout"})
    live = MagicMock(manual_compact_unsupported_backend="deepseek")
    state.sessions.get_provider = MagicMock(return_value=live)
    state.sessions.is_provider_alive = AsyncMock(return_value=True)
    slot = _slot()
    slot.backend = "kiro"
    slot._pending_reset_history_key = effective_session_key(slot)
    audit = MagicMock()
    with _quiet_sel() as mock_sel:
        mock_sel.return_value = audit
        slot._empty_response_retries = 2
        await chat_runner._run_chat(state, slot, "/compact")
    await _settle(slot)
    refusal = compact_unsupported_reply("deepseek")
    assert any(m.get("content") == refusal for m in slot.messages), slot.messages
    client.prompt.assert_not_called()
    client.wait_for_compaction.assert_not_called()
    # The refusal is audited like the pre-claim /compact refusal.
    outcomes = [
        (c.kwargs.get("tool_name"), c.kwargs.get("outcome"), c.kwargs.get("metadata"))
        for c in audit.log_tool_invocation.call_args_list
    ]
    assert ("/compact", "auto_managed_backend", {"backend": "deepseek", "slot": slot.key}) in (
        outcomes
    ), outcomes


@pytest.mark.asyncio
async def test_the_pre_turn_backfill_leaves_a_switched_chats_cleared_model(tmp_path):
    # A switch clears slot.model and queues a reset. A turn that still runs on
    # the old session (sub-agents attached) must not write the old backend's
    # model back as a pin.
    from kiro_crew.dashboard.chat_turn.model_fallback import _settle_session_model

    class _Client:
        class client:  # noqa: N801 - the attribute the backfill reads
            _model = "claude-model"

    slot = _ChatSlot("s1")
    slot.backend = "codex"
    slot.model = ""
    slot._pending_reset_history_key = "s1"
    await _settle_session_model(
        slot,
        _Client(),
        "acp",
        "hello",
        is_new=True,
        resumed=False,
        _is_refusal_retry_turn=False,
        _synthetic_recovery_turn=False,
        crew_model="crew-pinned-model",
    )
    assert slot.model == "", slot.model


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "expected"),
    [pytest.param("claude", "claude", id="picked"), pytest.param(None, None, id="unpicked")],
)
async def test_the_inherited_model_probe_resolves_the_picks_ladder(monkeypatch, override, expected):
    # The probe asks the resolver for the same backend the turn allocates on, so
    # a pinned chat's probe and allocation read the same model ladder.
    from types import SimpleNamespace

    from kiro_crew import session as session_mod
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard.chat_turn.model_fallback import _inherited_default_model

    seen: list[object] = []

    def _resolver(cfg, agent, **kw):
        seen.append(kw.get("backend_override"))
        return "m"

    monkeypatch.setattr(session_mod, "_session_model", _resolver)
    sessions = SimpleNamespace(_cfg=KiroCrewConfig())
    await _inherited_default_model(
        sessions,
        _ChatSlot("test"),
        "dashboard:test",
        agent=None,
        crew_agent="",
        backend_override=override,
    )
    assert seen == [expected]


def _claude_only_key() -> str:
    from kiro_crew import model_registry

    return next(
        e["model_name"]
        for e in model_registry.display_list("claude_code")
        if model_registry.is_canonical_key(e["model_name"]) and e["model_name"] != "auto"
    )


def _codex_when_picked(monkeypatch) -> None:
    from kiro_crew import members
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX

    monkeypatch.setattr(
        members,
        "select_provider_backend",
        lambda k, m, d, o=None: (
            ACP_BACKEND_CODEX if o and "codex" in str(o) else ACP_BACKEND_CLAUDE
        ),
    )


@pytest.mark.asyncio
async def test_a_model_pick_is_rechecked_after_a_switch_it_waited_behind(monkeypatch):
    # The pick is checked against Claude, then waits on the slot lock a backend
    # switch holds. The switch moves the chat to Codex; the pick is checked
    # again under the lock and refused, so a Claude-only model never lands on
    # the Codex chat.
    from aiohttp.test_utils import TestClient, TestServer
    from test_chat_slot_switch_atomicity import _make_app, _mock_state

    _codex_when_picked(monkeypatch)
    slot = _ChatSlot("test")
    slot.backend = "claude"
    slot.model = ""
    state = _mock_state(slot)
    state.sessions.reset.return_value = True
    async with TestClient(TestServer(_make_app(state))) as client:
        await slot._lock.acquire()
        request = asyncio.create_task(
            client.post("/api/chat/slots/test/model", json={"model": _claude_only_key()})
        )
        await async_wait_until(
            lambda: getattr(slot._lock, "_waiters", None),
            describe=lambda: "the request never reached the lock",
        )
        slot.backend = "codex"
        slot._lock.release()
        resp = await request
    assert resp.status == 400
    assert slot.model == ""


@pytest.mark.asyncio
async def test_a_model_pick_after_a_switch_skips_the_retiring_session(monkeypatch):
    # GPT/Opus: a switch to Claude queued a reset, but the old Kiro session is
    # still live. A Claude model picked before the next turn must not be sent
    # to that Kiro session (which refuses it as unavailable); it is stored for
    # the session the next turn starts.
    from aiohttp.test_utils import TestClient, TestServer
    from test_chat_slot_switch_atomicity import _make_app, _mock_state

    from kiro_crew.acp.transport_errors import AcpModelUnavailable
    from kiro_crew.providers.acp import AcpProvider

    _codex_when_picked(monkeypatch)
    slot = _ChatSlot("test")
    slot.backend = "claude"
    slot.model = ""
    slot._pending_reset_history_key = "test"
    retiring = MagicMock(spec=AcpProvider)
    retiring.is_claude_backend = False
    retiring.has_active_turn.return_value = False
    retiring.client = MagicMock()
    retiring.client.set_model = AsyncMock(side_effect=AcpModelUnavailable("x", backend="kiro"))
    state = _mock_state(slot, provider=retiring)
    state.sessions.reset.return_value = True
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/test/model", json={"model": _claude_only_key()})
        body = await resp.text()
    assert resp.status == 200, body
    retiring.client.set_model.assert_not_awaited()
    assert slot.model == _claude_only_key()


def test_the_agent_model_pick_is_rejudged_on_a_backend_switched_meanwhile(tmp_path, monkeypatch):
    # session_set_model checks the model, then awaits the sub-agent probe. A
    # switch to Codex landing in that await is caught before the pick is stored.
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard.chat_utils import slot_history_key

    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    _codex_when_picked(monkeypatch)
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.backend = "claude"

    async def _switch_meanwhile(*_a, **_kw):
        target.backend = "codex"
        return None

    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", _switch_meanwhile)
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.set_model_target(
                state,
                caller_session_key=slot_history_key(caller),
                target="chat-2",
                model=_claude_only_key(),
            )
        )
    assert exc.value.code == "model_rejected"
    assert target._pending_model_pick is None


@pytest.mark.asyncio
async def test_an_eager_prefetch_on_a_disallowed_pick_sends_no_model(monkeypatch):
    # Same rule as the turn: when the gate degrades the pick to its fallback,
    # the model chosen for the pick is not sent, so the prefetched session the
    # first turn reuses runs the fallback's default.
    from unittest.mock import patch

    from eager_spawn_helpers import _cfg
    from eager_spawn_helpers import _mock_state as _eager_state

    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    _deny(monkeypatch, "codex")
    slot = _ChatSlot("t1")
    slot.backend = "codex"
    slot.model = "gpt-5.6-sol"
    state = _eager_state(slot)
    with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
        await chat_runner._eager_spawn(state, slot)
    kwargs = state.sessions.get_or_create.await_args.kwargs
    assert kwargs.get("backend_override") == "codex"
    assert kwargs.get("model") is None


@pytest.mark.asyncio
async def test_an_eager_prefetch_on_an_allowed_pick_keeps_its_model(monkeypatch):
    from unittest.mock import patch

    from eager_spawn_helpers import _cfg
    from eager_spawn_helpers import _mock_state as _eager_state

    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    slot = _ChatSlot("t1")
    slot.backend = "kiro"
    slot.model = "a-picked-model"
    state = _eager_state(slot)
    with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
        await chat_runner._eager_spawn(state, slot)
    assert state.sessions.get_or_create.await_args.kwargs.get("model") == "a-picked-model"


# ── Between turns ─────────────────────────────────────────────────────────────


def _cfg(default: str = ""):
    from types import SimpleNamespace

    return SimpleNamespace(agent=SimpleNamespace(acp_backend=default, member_acp_backend=""))


def _live_on(backend: str) -> MagicMock:
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    live = MagicMock()
    live.capabilities = capabilities_for(backend)
    return live


def test_a_turn_start_resets_a_session_that_came_up_off_the_pick():
    # A hard Stop's respawn started on the old backend before the switch: the
    # switch had no session to queue a reset for, so the next turn start
    # compares the live session's backend with the pick and resets it.
    slot = _ChatSlot("test")
    slot.backend = "codex"
    state = _mock_state(slot, provider=_live_on("claude"))
    chat_runner._arm_reset_off_backend_pick(state, slot, _cfg("claude"))
    assert slot._pending_reset_history_key == effective_session_key(slot)


def test_a_turn_start_keeps_a_session_already_on_the_pick():
    slot = _ChatSlot("test")
    slot.backend = "codex"
    state = _mock_state(slot, provider=_live_on("codex"))
    chat_runner._arm_reset_off_backend_pick(state, slot, _cfg("claude"))
    assert slot._pending_reset_history_key is None


def test_a_turn_start_leaves_an_unpicked_chat_alone():
    # An unpicked chat follows the gateway default, exactly as before.
    slot = _ChatSlot("test")
    state = _mock_state(slot, provider=_live_on("claude"))
    chat_runner._arm_reset_off_backend_pick(state, slot, _cfg(""))
    assert slot._pending_reset_history_key is None


@pytest.mark.asyncio
async def test_a_switch_during_a_hard_stop_respawn_lands_on_the_next_turn():
    # GPT's case: the switch runs while the respawn is still starting (no live
    # session yet) and succeeds; the respawn then registers the old backend.
    # The next turn start sees the mismatch and resets onto the pick.
    slot = _ChatSlot("test")
    slot.backend = "claude"
    state = _mock_state(slot, provider=None)
    status, _ = await _post(state, {"backend": "codex"})
    assert status == 200
    state.sessions.get_provider.return_value = _live_on("claude")
    chat_runner._arm_reset_off_backend_pick(state, slot, _cfg(""))
    assert slot._pending_reset_history_key == effective_session_key(slot)


def _gateway(_floor_monkeypatch, default: str = "") -> None:
    from kiro_crew.dashboard import chat_handlers

    _floor_monkeypatch.setattr(
        chat_handlers.KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: MagicMock(agent=MagicMock(acp_backend=default, member_acp_backend=""))
        ),
    )


@pytest.mark.asyncio
async def test_switching_back_before_the_next_turn_is_announced(_floor_monkeypatch):
    # Opus: codex -> "" -> codex while idle on a live codex session. The second
    # switch is a change from the stored pick, so it is announced, not hidden
    # behind the still-live codex session.
    _gateway(_floor_monkeypatch)
    slot = _ChatSlot("test")
    slot.backend = "codex"
    state = _mock_state(slot, provider=_live_on("codex"))
    await _post(state, {"backend": ""})
    status, data = await _post(state, {"backend": "codex"})
    assert (status, data) == (200, {"ok": True, "backend": "codex"})
    assert "`codex`" in slot.messages[-1]["content"]
    assert state.push_slots_update.call_count == 2


@pytest.mark.asyncio
async def test_switching_back_drops_a_model_queued_for_the_abandoned_pick(_floor_monkeypatch):
    # GPT F1: kiro -> claude, a claude model is queued, then back to kiro while
    # the live session is still kiro. The queued claude model must not survive.
    _gateway(_floor_monkeypatch)
    slot = _ChatSlot("test")
    state = _mock_state(slot, provider=_live_on(""))
    await _post(state, {"backend": "claude"})
    slot._pending_model_pick = object()
    slot.model = "claude-only-model"
    status, _ = await _post(state, {"backend": "kiro"})
    assert status == 200
    assert (slot.model, slot._pending_model_pick) == ("", None)


def _claim_returning(provider) -> AsyncMock:
    return AsyncMock(return_value=(provider, True, False))


@pytest.mark.asyncio
async def test_a_respawn_registered_after_the_turn_start_check_is_reclaimed_on_the_pick(
    monkeypatch,
):
    # GPT: the hard Stop's respawn registers the old backend only after the
    # pre-claim check found no session, so the turn claimed it. Under the lease
    # the turn resets it and claims again on the pick.
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
    slot = _ChatSlot("test")
    slot.backend = "codex"
    state = _mock_state(slot)
    stale, fresh = _live_on("claude"), _live_on("codex")
    claim = _claim_returning(fresh)
    got = await chat_runner._reclaim_off_backend_pick(
        state, slot, _cfg("claude"), (stale, False, False), claim
    )
    state.sessions.reset.assert_awaited_once_with(effective_session_key(slot))
    assert got == (fresh, True, False)


@pytest.mark.asyncio
async def test_a_claim_already_on_the_pick_is_kept(monkeypatch):
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
    slot = _ChatSlot("test")
    slot.backend = "codex"
    state = _mock_state(slot)
    live = _live_on("codex")
    claim = _claim_returning(_live_on("codex"))
    got = await chat_runner._reclaim_off_backend_pick(
        state, slot, _cfg("claude"), (live, False, False), claim
    )
    assert got == (live, False, False)
    state.sessions.reset.assert_not_awaited()
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unpicked_claim_is_left_alone(monkeypatch):
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
    slot = _ChatSlot("test")
    state = _mock_state(slot)
    live = _live_on("claude")
    got = await chat_runner._reclaim_off_backend_pick(
        state, slot, _cfg(""), (live, False, False), _claim_returning(None)
    )
    assert got == (live, False, False)
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cleared_pick_reclaims_a_late_respawn_still_on_the_old_pick(monkeypatch):
    # GPT: the pick was cleared while a hard Stop's respawn on it was starting;
    # the respawn registered after the pre-claim check, carrying the old
    # backend_override. Under the lease the turn resets it onto the default.
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
    slot = _ChatSlot("test")
    state = _mock_state(slot)
    state.sessions.allocation_backend_override = MagicMock(return_value="claude")
    fresh = _live_on("")
    got = await chat_runner._reclaim_off_backend_pick(
        state, slot, _cfg(""), (_live_on("claude"), False, False), _claim_returning(fresh)
    )
    state.sessions.reset.assert_awaited_once_with(effective_session_key(slot))
    assert got == (fresh, True, False)


@pytest.mark.asyncio
async def test_a_claim_off_the_pick_with_children_attached_queues_the_reset(monkeypatch):
    # The reset would release the runtime attached sub-agents run on, so it is
    # queued for a later turn instead, as the pre-claim path does.
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=True))
    slot = _ChatSlot("test")
    slot.backend = "codex"
    live = _live_on("claude")
    state = _mock_state(slot, provider=live)
    got = await chat_runner._reclaim_off_backend_pick(
        state, slot, _cfg("claude"), (live, False, False), _claim_returning(None)
    )
    assert got == (live, False, False)
    state.sessions.reset.assert_not_awaited()
    assert slot._pending_reset_history_key == effective_session_key(slot)


def test_a_turn_start_resets_an_unpicked_chat_still_on_an_old_pick():
    # GPT F2: the pick was cleared while a hard Stop's respawn on it was still
    # starting; the respawn came up carrying the old backend_override.
    slot = _ChatSlot("test")
    state = _mock_state(slot, provider=_live_on("claude"))
    state.sessions.allocation_backend_override = MagicMock(return_value="claude")
    chat_runner._arm_reset_off_backend_pick(state, slot, _cfg(""))
    assert slot._pending_reset_history_key == effective_session_key(slot)
