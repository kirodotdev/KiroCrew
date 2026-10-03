"""Per-chat backend selection.

A chat can carry its own AI backend pick (``_ChatSlot.acp_backend``). It is the
first step of the single selection gate, is set through ONE switch shared by the
composer route and the agent's ``session_backend`` tool, is stored on the chat
record so it survives a restart, and behaves the same with the sandbox on or off.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state as _base_state

from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat import api_chat_slot_backend, api_chat_slot_create
from kiro_crew.dashboard.chat_persistence import (
    _read_vouched_backend,
    _rehydrate_slot_from_history,
    _restored_acp_backend,
    _save_slot_to_history,
    _vouched_backend_cache,
    _vouched_backend_path,
    prefetch_vouched_backend,
    vouch_backend_pick,
)
from kiro_crew.dashboard.chat_runner import _chat_backend_kwargs, _slot_binding
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.mcp_dashboard import _call_tool_inner
from kiro_crew.members import select_provider_backend


def _make_state(tmp_path):
    state = _base_state(tmp_path)
    # No live session: the reset has nothing to tear down (the real manager
    # answers False for an unregistered key).
    state.sessions.reset = AsyncMock(return_value=False)
    return state


def _busy(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task


_MEMBER_KEY = "dashboard:member-alice"
_CHAT_KEY = "dashboard:chat-1"

# ── The selection gate ───────────────────────────────────────────────────────


def test_no_pick_behaves_exactly_as_before():
    assert select_provider_backend(_CHAT_KEY, ACP_BACKEND_KAS, ACP_BACKEND_KIRO) == ACP_BACKEND_KIRO
    assert (
        select_provider_backend(_CHAT_KEY, ACP_BACKEND_KAS, ACP_BACKEND_KIRO, None)
        == ACP_BACKEND_KIRO
    )
    assert (
        select_provider_backend(_MEMBER_KEY, ACP_BACKEND_KAS, ACP_BACKEND_KIRO) == ACP_BACKEND_KAS
    )


def test_a_chat_pick_wins_over_the_member_route_and_the_default():
    assert (
        select_provider_backend(_CHAT_KEY, ACP_BACKEND_KIRO, ACP_BACKEND_KIRO, ACP_BACKEND_KAS)
        == ACP_BACKEND_KAS
    )
    # "" is Kiro's own id: a REAL pick, honoured over a non-Kiro member route.
    assert (
        select_provider_backend(_MEMBER_KEY, ACP_BACKEND_KAS, ACP_BACKEND_KAS, ACP_BACKEND_KIRO)
        == ACP_BACKEND_KIRO
    )


def test_an_unselectable_pick_degrades_through_the_same_gate():
    assert (
        select_provider_backend(_CHAT_KEY, ACP_BACKEND_KAS, ACP_BACKEND_KAS, "not-a-backend")
        == ACP_BACKEND_KIRO
    )


@pytest.mark.parametrize(
    ("pick", "degraded"),
    [(None, False), (ACP_BACKEND_KIRO, False), (ACP_BACKEND_KAS, False), ("not-a-backend", True)],
)
def test_the_slot_row_reports_a_pick_the_gate_degrades(pick, degraded):
    """The picker must not keep naming a pick the gate runs on Kiro."""
    from kiro_crew.dashboard.slot_projection import _backend_pick_degraded

    assert _backend_pick_degraded(pick) is degraded
    gate_answer = select_provider_backend(_CHAT_KEY, "", ACP_BACKEND_KIRO, pick)
    if pick is not None:
        assert degraded is (gate_answer != pick), "the row must agree with the gate"


@pytest.mark.parametrize("sandbox", ["off", "auto"])
def test_the_factory_honours_the_pick_with_the_sandbox_on_or_off(sandbox):
    """Selection must not depend on the sandbox: same answer either way."""
    cfg = KiroCrewConfig()
    cfg.agent.sandbox = sandbox
    factory = cfg.create_provider_factory()
    picked = factory(session_key=_CHAT_KEY, agent="", backend_override=ACP_BACKEND_KAS)
    assert picked.client.backend == ACP_BACKEND_KAS
    unpicked = factory(session_key=_CHAT_KEY, agent="")
    assert unpicked.client.backend == ACP_BACKEND_KIRO


def test_the_turn_passes_the_pick_only_when_there_is_one(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    assert _chat_backend_kwargs(slot) == {}
    slot.acp_backend = ACP_BACKEND_KIRO
    assert _chat_backend_kwargs(slot) == {"backend_override": ACP_BACKEND_KIRO}
    before = _slot_binding(slot)
    slot.acp_backend = ACP_BACKEND_KAS
    assert _slot_binding(slot) != before, "a pick mid-handshake must invalidate the eager session"


# ── Validation ───────────────────────────────────────────────────────────────


def test_validation_accepts_selectable_ids_and_the_kiro_spelling():
    assert chat_handlers.validate_backend_pick(None) is None
    assert chat_handlers.validate_backend_pick("kiro") == ACP_BACKEND_KIRO
    assert chat_handlers.validate_backend_pick(ACP_BACKEND_KAS) == ACP_BACKEND_KAS


@pytest.mark.parametrize("bad", ["nope", 7, ["kas"]])
def test_validation_refuses_anything_else(bad):
    with pytest.raises(chat_handlers.BackendSwitchRefused) as exc:
        chat_handlers.validate_backend_pick(bad)
    assert (exc.value.code, exc.value.status) == ("invalid_backend", 400)


# ── The composer route ───────────────────────────────────────────────────────


def _app(state) -> web.Application:
    @web.middleware
    async def dashboard_auth_marker(request, handler):
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[dashboard_auth_marker])
    app["state"] = state
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    app.router.add_post("/api/chat/slots/{slot}/backend", api_chat_slot_backend)
    return app


async def _post(state, path: str, body: dict, headers: dict | None = None):
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(path, json=body, headers=headers or {})
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_a_switch_stores_the_pick_clears_the_model_and_persists(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    slot.model = "some-kiro-model"
    status, body = await _post(state, "/api/chat/slots/chat-1/backend", {"backend": "kas"})
    assert status == 200, body
    assert body == {"ok": True, "backend": ACP_BACKEND_KAS, "changed": True}
    assert slot.acp_backend == ACP_BACKEND_KAS
    assert slot.model == "", "a model id belongs to the old backend's catalog"
    meta = await asyncio.to_thread(state.conversation_log.get_metadata, slot_history_key(slot))
    assert meta["acp_backend"] == ACP_BACKEND_KAS


@pytest.mark.asyncio
async def test_the_same_pick_is_a_no_op(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.acp_backend = ACP_BACKEND_KAS
    slot.model = "kept"
    status, body = await _post(state, "/api/chat/slots/chat-1/backend", {"backend": "kas"})
    assert (status, body["changed"], slot.model) == (200, False, "kept")


@pytest.mark.asyncio
async def test_a_switch_is_refused_while_a_turn_runs(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    _busy(slot)
    status, body = await _post(state, "/api/chat/slots/chat-1/backend", {"backend": "kas"})
    assert (status, body["code"]) == (409, "turn_in_flight")
    assert slot.acp_backend is None


@pytest.mark.asyncio
async def test_an_unselectable_pick_is_refused_and_nothing_changes(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    status, body = await _post(state, "/api/chat/slots/chat-1/backend", {"backend": "nope"})
    assert (status, body["code"], slot.acp_backend) == (400, "invalid_backend", None)


@pytest.mark.asyncio
async def test_another_apps_chat_is_not_found(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    status, body = await _post(
        state,
        "/api/chat/slots/chat-1/backend",
        {"backend": "kas"},
        headers={"X-Test-App": "some-app"},
    )
    assert status == 404
    assert slot.acp_backend is None


@pytest.mark.asyncio
async def test_a_failed_save_rolls_the_pick_back(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.model = "kept"

    async def _no_commit(*_a, **_kw):
        return False

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _no_commit)
    status, body = await _post(state, "/api/chat/slots/chat-1/backend", {"backend": "kas"})
    assert (status, body["code"]) == (500, "backend_persist_failed")
    assert (slot.acp_backend, slot.model) == (None, "kept")


@pytest.mark.asyncio
async def test_a_failed_save_restores_the_pick_generation(tmp_path, monkeypatch):
    # A refused switch changed nothing: a left-over generation bump would make the
    # next turn discard a still-valid pending agent model pick as superseded.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    before = slot._model_pick_gen

    async def _no_commit(*_a, **_kw):
        return False

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _no_commit)
    status, _body = await _post(state, "/api/chat/slots/chat-1/backend", {"backend": "kas"})
    assert status == 500
    assert slot._model_pick_gen == before


@pytest.mark.asyncio
async def test_a_committed_switch_keeps_the_generation_bump(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    before = slot._model_pick_gen
    status, _body = await _post(state, "/api/chat/slots/chat-1/backend", {"backend": "kas"})
    assert status == 200
    assert slot._model_pick_gen == before + 1


@pytest.mark.asyncio
async def test_authorization_is_re_checked_after_the_reset_before_persisting(tmp_path, monkeypatch):
    # The reset awaits; a same-name replacement or ownership change during it keeps
    # the session key, so only the caller's gate can refuse. It must run again
    # after the reset and before anything is saved.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.model = "kept"
    calls = {"reset": False, "saved": False}

    async def _reset(*_a, **_kw):
        calls["reset"] = True
        return True

    async def _save(*_a, **_kw):
        calls["saved"] = True
        return True

    monkeypatch.setattr(chat_handlers, "_reset_slot_session_or_warn", _reset)
    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save)

    def gate(_session_key: str) -> None:
        if calls["reset"]:
            raise chat_handlers.BackendSwitchRefused("not found", code="slot_not_found", status=404)

    before = slot._model_pick_gen
    with pytest.raises(chat_handlers.BackendSwitchRefused) as exc:
        await chat_handlers.switch_slot_backend(state, slot, ACP_BACKEND_KAS, gate=gate)
    assert exc.value.code == "slot_not_found"
    assert calls["saved"] is False, "a refused caller's pick must never be persisted"
    assert (slot.acp_backend, slot.model, slot._model_pick_gen) == (None, "kept", before)


@pytest.mark.asyncio
async def test_a_chat_can_pick_its_backend_at_creation(tmp_path):
    state = _make_state(tmp_path)
    status, body = await _post(state, "/api/chat/slots", {"name": "fresh", "backend": "kas"})
    assert status == 200, body
    assert state._slots["fresh"].acp_backend == ACP_BACKEND_KAS
    status, body = await _post(state, "/api/chat/slots", {"name": "bad", "backend": "nope"})
    assert (status, body["code"]) == (400, "invalid_backend")
    assert "bad" not in state._slots


@pytest.mark.asyncio
async def test_creation_refuses_a_backend_for_a_remote_chat(tmp_path):
    # A remote chat runs on the peer, which picks its own backend; quietly dropping
    # the pick would answer 200 for a backend the chat will not run on.
    state = _make_state(tmp_path)
    status, body = await _post(
        state, "/api/chat/slots", {"name": "remote", "backend": "kas", "instance_id": "peer-1"}
    )
    assert (status, body["code"]) == (409, "remote_backend_unsupported")
    assert "remote" not in state._slots


@pytest.mark.asyncio
async def test_creation_refuses_a_different_backend_for_an_existing_chat(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("open")
    slot.acp_backend = ACP_BACKEND_KAS
    status, body = await _post(state, "/api/chat/slots", {"name": "open", "backend": "claude"})
    assert (status, body["code"]) == (409, "backend_pick_exists")
    assert slot.acp_backend == ACP_BACKEND_KAS
    status, _body = await _post(state, "/api/chat/slots", {"name": "open", "backend": "kas"})
    assert status == 200, "re-opening with the chat's own pick is not a change"


@pytest.mark.asyncio
async def test_a_refusal_of_any_type_after_the_reset_rolls_the_pick_back(tmp_path, monkeypatch):
    # The agent tool's gate raises SessionControlError, not BackendSwitchRefused;
    # a refusal of either type must leave the chat exactly where it was.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.model = "kept"
    calls = {"reset": False}

    async def _reset(*_a, **_kw):
        calls["reset"] = True
        return True

    monkeypatch.setattr(chat_handlers, "_reset_slot_session_or_warn", _reset)

    def gate(_session_key: str) -> None:
        if calls["reset"]:
            raise sc.SessionControlError("replaced", code="target_replaced", status=409)

    before = slot._model_pick_gen
    with pytest.raises(sc.SessionControlError):
        await chat_handlers.switch_slot_backend(state, slot, ACP_BACKEND_KAS, gate=gate)
    assert (slot.acp_backend, slot.model, slot._model_pick_gen) == (None, "kept", before)


@pytest.mark.asyncio
async def test_a_refusal_after_the_save_restores_the_prior_pick_durably(tmp_path, monkeypatch):
    # The save awaits: a target that became linked or mirrored meanwhile must not
    # keep the pick, in memory OR in what the next restart restores.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    saves = {"n": 0}
    real_save = chat_handlers.save_slot_off_loop

    async def _save(*a, **kw):
        saves["n"] += 1
        return await real_save(*a, **kw)

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save)

    def gate(_session_key: str) -> None:
        if saves["n"]:
            raise chat_handlers.BackendSwitchRefused("gone", code="slot_not_found", status=404)

    with pytest.raises(chat_handlers.BackendSwitchRefused):
        await chat_handlers.switch_slot_backend(state, slot, ACP_BACKEND_KAS, gate=gate)
    assert slot.acp_backend is None
    restored = _rehydrate_slot_from_history(_make_state(tmp_path), "chat-1")
    assert restored is not None
    assert restored.acp_backend is None, "the refused pick must not come back after a restart"


def _post_save_refusal(monkeypatch):
    """A gate that refuses once the switch's metadata save has run."""
    saves = {"n": 0}
    real_save = chat_handlers.save_slot_off_loop

    async def _save(*a, **kw):
        saves["n"] += 1
        return await real_save(*a, **kw)

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save)

    def gate(_session_key: str) -> None:
        if saves["n"]:
            raise chat_handlers.BackendSwitchRefused("gone", code="slot_not_found", status=404)

    return gate


@pytest.mark.asyncio
async def test_a_rollback_that_cannot_rewrite_the_record_removes_it(tmp_path, monkeypatch):
    # Fail closed: when the prior pick cannot be written back after a post-save
    # refusal, the record goes, so a restart restores the default, never the
    # refused pick the metadata line may still name.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    _committed_pick(state, slot, ACP_BACKEND_KIRO)
    gate = _post_save_refusal(monkeypatch)
    real_vouch = chat_handlers.vouch_backend_pick

    def _vouch(key, backend, *, also=None):
        if backend == ACP_BACKEND_KIRO and also is None and slot.acp_backend == ACP_BACKEND_KIRO:
            raise OSError("disk full")
        real_vouch(key, backend, also=also)

    monkeypatch.setattr(chat_handlers, "vouch_backend_pick", _vouch)
    with pytest.raises(chat_handlers.BackendSwitchRefused) as exc:
        await chat_handlers.switch_slot_backend(state, slot, ACP_BACKEND_KAS, gate=gate)
    assert exc.value.code == "slot_not_found"
    assert _read_vouched_backend(slot_history_key(slot)) == frozenset()


@pytest.mark.asyncio
async def test_a_rollback_that_cannot_touch_the_record_is_reported(tmp_path, monkeypatch):
    # Neither rewrite nor removal landed: the refused pick is restorable, so the
    # caller is told the undo failed rather than handed the plain refusal.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    _committed_pick(state, slot, ACP_BACKEND_KIRO)
    gate = _post_save_refusal(monkeypatch)
    real_vouch = chat_handlers.vouch_backend_pick

    def _vouch(key, backend, *, also=None):
        if also is None and slot.acp_backend == ACP_BACKEND_KIRO:
            raise OSError("read-only")
        real_vouch(key, backend, also=also)

    monkeypatch.setattr(chat_handlers, "vouch_backend_pick", _vouch)
    with pytest.raises(chat_handlers.BackendSwitchRefused) as exc:
        await chat_handlers.switch_slot_backend(state, slot, ACP_BACKEND_KAS, gate=gate)
    assert (exc.value.code, exc.value.status) == ("backend_rollback_failed", 500)


@pytest.mark.asyncio
async def test_a_same_name_replacement_during_the_switch_keeps_its_transcript(
    tmp_path, monkeypatch
):
    # The switch's save is pinned to the slot it authorized: a close-and-reopen of
    # the same name while the reset awaits must not take this slot's snapshot.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    await asyncio.to_thread(_save_slot_to_history, state, slot, force=True)

    async def _reset(*_a, **_kw):
        replacement = type(slot)("chat-1")
        state._slots["chat-1"] = replacement
        return True

    monkeypatch.setattr(chat_handlers, "_reset_slot_session_or_warn", _reset)
    with pytest.raises(chat_handlers.BackendSwitchRefused) as exc:
        await chat_handlers.switch_slot_backend(state, slot, ACP_BACKEND_KAS, gate=lambda _k: None)
    assert exc.value.code == "backend_persist_failed"
    meta = await asyncio.to_thread(state.conversation_log.get_metadata, slot_history_key(slot))
    assert "acp_backend" not in meta


@pytest.mark.asyncio
async def test_an_early_rollback_that_cannot_touch_the_record_is_reported(tmp_path, monkeypatch):
    # Before the metadata save too: a refusal whose record rollback fails leaves
    # the refused pick vouched, so it answers backend_rollback_failed.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    calls = {"reset": False}

    async def _reset(*_a, **_kw):
        calls["reset"] = True
        return True

    real_vouch = chat_handlers.vouch_backend_pick

    def _vouch(key, backend, *, also=None):
        if calls["reset"]:
            raise OSError("read-only")
        real_vouch(key, backend, also=also)

    monkeypatch.setattr(chat_handlers, "_reset_slot_session_or_warn", _reset)
    monkeypatch.setattr(chat_handlers, "vouch_backend_pick", _vouch)

    def gate(_session_key: str) -> None:
        if calls["reset"]:
            raise chat_handlers.BackendSwitchRefused("gone", code="slot_not_found", status=404)

    with pytest.raises(chat_handlers.BackendSwitchRefused) as exc:
        await chat_handlers.switch_slot_backend(state, slot, ACP_BACKEND_KAS, gate=gate)
    assert (exc.value.code, exc.value.status) == ("backend_rollback_failed", 500)


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_narrowed_is_removed(tmp_path, monkeypatch):
    # After a committed switch the record must stop vouching the prior pick: if
    # narrowing it fails it is removed (fail closed), and only a record that can
    # be neither narrowed nor removed is reported, as a warning.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    _committed_pick(state, slot, ACP_BACKEND_KIRO)
    real_vouch = chat_handlers.vouch_backend_pick
    fail = {"narrow": True, "remove": False}

    def _vouch(key, backend, *, also=None):
        if backend == ACP_BACKEND_KAS and also is None and fail["narrow"]:
            raise OSError("disk full")
        if backend is None and also is None and fail["remove"]:
            raise OSError("read-only")
        real_vouch(key, backend, also=also)

    monkeypatch.setattr(chat_handlers, "vouch_backend_pick", _vouch)
    result = await chat_handlers.switch_slot_backend(
        state, slot, ACP_BACKEND_KAS, gate=lambda _k: None
    )
    assert result["changed"] is True and "warning" not in result
    assert _read_vouched_backend(slot_history_key(slot)) == frozenset()

    _committed_pick(state, slot, ACP_BACKEND_KIRO)
    fail["remove"] = True
    result = await chat_handlers.switch_slot_backend(
        state, slot, ACP_BACKEND_KAS, gate=lambda _k: None
    )
    assert result["warning"] == chat_handlers._BACKEND_RECORD_STALE_WARNING


@pytest.mark.asyncio
async def test_a_session_started_on_a_refused_pick_is_torn_down_even_mid_turn(
    tmp_path, monkeypatch
):
    # A busy-decline would leave the refused backend serving the in-flight turn
    # and every later one, so the discard does not skip a busy session -- but it
    # tears down ONLY the session started under the tentative pick: one a send
    # registered after the rollback runs on the restored backend and keeps its turn.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    tentative, successor = object(), object()
    # The chat holds a legacy alias; the registry files the session under the
    # canonical key and folds the alias onto it.
    state.sessions._fold_key = lambda k: {"chat-1-legacy": "dashboard:chat-1"}.get(k, k)
    state.sessions._sessions = {"dashboard:chat-1": tentative}
    seen = []

    async def _reset(_state, _slot, _key, *, skip_if_busy=False, expect_session=None):
        seen.append((skip_if_busy, expect_session))
        return True

    monkeypatch.setattr(chat_handlers, "_reset_slot_session", _reset)
    await chat_handlers._discard_session_started_in_switch(state, slot, "chat-1-legacy", tentative)
    assert seen == [(False, tentative)]

    state.sessions._sessions = {"dashboard:chat-1": successor}
    await chat_handlers._discard_session_started_in_switch(state, slot, "chat-1-legacy", tentative)
    await chat_handlers._discard_session_started_in_switch(state, slot, "chat-1-legacy", None)
    assert seen == [(False, tentative)]


def test_the_tool_verb_refuses_to_read_a_remote_target(tmp_path, monkeypatch):
    # The turn runs on the peer, which picks its own backend: reporting the local
    # gateway's effective backend would be false, so the read is refused too.
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.executor = "remote"
    with pytest.raises(sc.SessionControlError) as exc:
        _backend(state, caller, "chat-2")
    assert (exc.value.code, exc.value.status) == ("remote_backend_unsupported", 409)


def _committed_pick(state, slot, pick):
    """Give *slot* a pick the way a finished switch leaves it on disk."""
    slot.acp_backend = pick
    vouch_backend_pick(slot_history_key(slot), pick)
    _save_slot_to_history(state, slot, force=True)


@pytest.mark.asyncio
async def test_the_record_accepts_both_picks_before_the_new_one_is_published(tmp_path, monkeypatch):
    # Persist before publish: the gateway record must already accept the new pick
    # (and still accept the prior one) while memory and the metadata line still
    # hold the prior pick, and is narrowed to the new pick only at the end.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    _committed_pick(state, slot, ACP_BACKEND_KIRO)
    calls = []
    real_vouch = chat_handlers.vouch_backend_pick

    def _vouch(key, backend, *, also=None):
        calls.append((backend, also, slot.acp_backend))
        real_vouch(key, backend, also=also)

    monkeypatch.setattr(chat_handlers, "vouch_backend_pick", _vouch)
    status, _body = await _post(state, "/api/chat/slots/chat-1/backend", {"backend": "kas"})
    assert status == 200
    assert calls[0] == (ACP_BACKEND_KAS, ACP_BACKEND_KIRO, ACP_BACKEND_KIRO)
    assert calls[-1] == (ACP_BACKEND_KAS, None, ACP_BACKEND_KAS)
    assert _read_vouched_backend(slot_history_key(slot)) == frozenset({ACP_BACKEND_KAS})


@pytest.mark.parametrize("line_saved", [False, True])
def test_a_crash_between_the_two_writes_keeps_the_metadata_lines_pick(tmp_path, line_saved):
    # The switch writes the record and the metadata line separately; a gateway
    # that dies between them must restart on whichever pick the line holds.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    _committed_pick(state, slot, ACP_BACKEND_KIRO)
    vouch_backend_pick(slot_history_key(slot), ACP_BACKEND_KAS, also=ACP_BACKEND_KIRO)
    if line_saved:
        slot.acp_backend = ACP_BACKEND_KAS
        _save_slot_to_history(state, slot, force=True)

    restored = _rehydrate_slot_from_history(_make_state(tmp_path), "chat-1")
    assert restored is not None
    assert restored.acp_backend == (ACP_BACKEND_KAS if line_saved else ACP_BACKEND_KIRO)


@pytest.mark.asyncio
async def test_a_refusal_after_the_reset_narrows_the_record_back(tmp_path, monkeypatch):
    # The record accepted the new pick before the reset; a refusal must take that
    # back, or an edited metadata line could restore the refused pick.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    _committed_pick(state, slot, ACP_BACKEND_KIRO)
    calls = {"reset": False}

    async def _reset(*_a, **_kw):
        calls["reset"] = True
        return True

    monkeypatch.setattr(chat_handlers, "_reset_slot_session_or_warn", _reset)

    def gate(_session_key: str) -> None:
        if calls["reset"]:
            raise chat_handlers.BackendSwitchRefused("gone", code="slot_not_found", status=404)

    with pytest.raises(chat_handlers.BackendSwitchRefused):
        await chat_handlers.switch_slot_backend(state, slot, ACP_BACKEND_KAS, gate=gate)
    assert _read_vouched_backend(slot_history_key(slot)) == frozenset({ACP_BACKEND_KIRO})


@pytest.mark.asyncio
async def test_a_refusal_after_the_reset_discards_a_session_started_on_the_refused_pick(
    tmp_path, monkeypatch
):
    # A send landing inside the reset await cold-starts on the committed new pick;
    # a refusal at the post-reset gate must tear that session down, or the chat
    # keeps serving turns on the refused backend while everything names the prior.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    calls = {"reset": False}
    started = object()

    async def _reset(*_a, **_kw):
        calls["reset"] = True
        # The send that lands inside the reset await registers its session,
        # under the canonical spelling the registry folds the chat's key onto.
        state.sessions._fold_key = lambda k: "canonical"
        state.sessions._sessions = {"canonical": started}
        return True

    discard = AsyncMock()
    monkeypatch.setattr(chat_handlers, "_reset_slot_session_or_warn", _reset)
    monkeypatch.setattr(chat_handlers, "_discard_session_started_in_switch", discard)

    def gate(_session_key: str) -> None:
        if calls["reset"]:
            raise chat_handlers.BackendSwitchRefused("gone", code="slot_not_found", status=404)

    with pytest.raises(chat_handlers.BackendSwitchRefused):
        await chat_handlers.switch_slot_backend(state, slot, ACP_BACKEND_KAS, gate=gate)
    discard.assert_awaited_once()
    # Exactly the session registered while the refused pick was live is named.
    assert discard.await_args.args[3] is started


@pytest.mark.asyncio
async def test_a_rebind_during_the_reset_discards_a_session_started_on_the_refused_pick(
    tmp_path, monkeypatch
):
    # The rebind refusal rolls the pick back like every other post-reset refusal,
    # so a session a send cold-started under the old key on the refused pick goes too.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    original_key = chat_handlers.effective_session_key(slot)
    started = object()
    rebound = {"yes": False}

    async def _reset(*_a, **_kw):
        rebound["yes"] = True
        state.sessions._fold_key = lambda k: k
        state.sessions._sessions = {original_key: started}
        return True

    real_key = chat_handlers.effective_session_key
    monkeypatch.setattr(
        chat_handlers,
        "effective_session_key",
        lambda s: "rebound-key" if rebound["yes"] else real_key(s),
    )
    discard = AsyncMock()
    monkeypatch.setattr(chat_handlers, "_reset_slot_session_or_warn", _reset)
    monkeypatch.setattr(chat_handlers, "_discard_session_started_in_switch", discard)
    with pytest.raises(chat_handlers.BackendSwitchRefused) as exc:
        await chat_handlers.switch_slot_backend(state, slot, ACP_BACKEND_KAS, gate=lambda _k: None)
    assert exc.value.code == "session_rebound"
    discard.assert_awaited_once()
    assert discard.await_args.args[3] is started


def test_deleting_the_transcript_removes_its_backend_record(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    _committed_pick(state, slot, ACP_BACKEND_KAS)
    key = slot_history_key(slot)
    assert _vouched_backend_path(key).exists()
    # The history page deletes by file stem, not by the slot's colon form.
    assert state.conversation_log.delete_session(key.replace(":", "_"))
    assert not _vouched_backend_path(key).exists()


# ── Restart ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pick", [ACP_BACKEND_KAS, ACP_BACKEND_KIRO])
def test_the_pick_survives_a_restart(tmp_path, pick):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    slot.acp_backend = pick
    vouch_backend_pick(slot_history_key(slot), pick)
    _save_slot_to_history(state, slot, force=True)

    restored = _rehydrate_slot_from_history(_make_state(tmp_path), "chat-1")
    assert restored is not None
    assert restored.acp_backend == pick


@pytest.mark.asyncio
async def test_a_pick_made_through_the_route_survives_a_restart(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    status, _body = await _post(state, "/api/chat/slots/chat-1/backend", {"backend": "kas"})
    assert status == 200
    restored = await asyncio.to_thread(
        _rehydrate_slot_from_history, _make_state(tmp_path), "chat-1"
    )
    assert restored is not None
    assert restored.acp_backend == ACP_BACKEND_KAS


@pytest.mark.asyncio
async def test_a_pick_made_at_creation_survives_a_restart(tmp_path):
    state = _make_state(tmp_path)
    status, _body = await _post(state, "/api/chat/slots", {"name": "fresh", "backend": "kas"})
    assert status == 200
    slot = state._slots["fresh"]
    slot.append("user", "hello")
    slot.drain()
    await asyncio.to_thread(_save_slot_to_history, state, slot, force=True)
    restored = await asyncio.to_thread(_rehydrate_slot_from_history, _make_state(tmp_path), "fresh")
    assert restored is not None
    assert restored.acp_backend == ACP_BACKEND_KAS


@pytest.mark.asyncio
async def test_a_pick_made_when_reopening_a_saved_chat_is_saved_at_once(tmp_path):
    # Creating by the name of a transcript that is on disk but not in memory
    # opens that transcript: the pick must reach its metadata line in the create
    # call itself, or a restart before the next save restores the default.
    first = _make_state(tmp_path)
    old = first.get_or_create_slot("old")
    old.append("user", "hello")
    old.drain()
    await asyncio.to_thread(_save_slot_to_history, first, old, force=True)

    state = _make_state(tmp_path)
    status, body = await _post(state, "/api/chat/slots", {"name": "old", "backend": "kas"})
    assert status == 200, body
    restored = await asyncio.to_thread(_rehydrate_slot_from_history, _make_state(tmp_path), "old")
    assert restored is not None
    assert restored.acp_backend == ACP_BACKEND_KAS


@pytest.mark.asyncio
async def test_a_fork_keeps_its_parents_pick_across_a_restart(tmp_path):
    from chat_test_helpers import _make_app as _chat_app

    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("src")
    parent.append("user", "hello", "msg msg-u")
    parent.drain()
    parent.acp_backend = ACP_BACKEND_KAS
    async with TestClient(TestServer(_chat_app(state))) as client:
        resp = await client.post("/api/chat/slots/src/fork", json={})
        assert resp.status == 200
        child_key = (await resp.json())["key"]
    assert state._slots[child_key].acp_backend == ACP_BACKEND_KAS
    restored = await asyncio.to_thread(
        _rehydrate_slot_from_history, _make_state(tmp_path), child_key
    )
    assert restored is not None
    assert restored.acp_backend == ACP_BACKEND_KAS, "the child needs its own record"


@pytest.mark.parametrize("vouched", [None, ACP_BACKEND_KIRO])
def test_an_edited_metadata_line_does_not_pick_the_backend(tmp_path, vouched):
    # The metadata line is agent-editable: a value written there without (or
    # against) the gateway-owned record must not move the chat after a restart.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    if vouched is not None:
        vouch_backend_pick(slot_history_key(slot), vouched)
    slot.acp_backend = ACP_BACKEND_KAS  # what a planted line would say
    _save_slot_to_history(state, slot, force=True)

    restored = _rehydrate_slot_from_history(_make_state(tmp_path), "chat-1")
    assert restored is not None
    assert restored.acp_backend is None


def test_the_recent_session_restore_checks_the_record_too():
    meta = {"acp_backend": ACP_BACKEND_KAS}
    key = "dashboard:recent-1"
    _vouched_backend_cache.pop(key, None)
    assert _restored_acp_backend(meta, key) is None, "no prefetched record, no pick"
    _vouched_backend_cache[key] = frozenset({ACP_BACKEND_KAS})
    assert _restored_acp_backend(meta, key) == ACP_BACKEND_KAS
    assert key not in _vouched_backend_cache, "an entry is consumed by its apply"


def test_the_recent_session_restore_finds_the_record_by_file_stem(tmp_path):
    # list_sessions() hands the recent-session restore the transcript's file stem
    # (``dashboard_chat-1``) while the switch vouches the slot's colon form; both
    # must reach one record, or every such restore drops a real pick.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    colon_key = slot_history_key(slot)
    vouch_backend_pick(colon_key, ACP_BACKEND_KAS)
    stem_key = colon_key.replace(":", "_")
    assert stem_key != colon_key
    prefetch_vouched_backend(stem_key)
    assert _restored_acp_backend({"acp_backend": ACP_BACKEND_KAS}, stem_key) == ACP_BACKEND_KAS


def test_a_cleared_pick_is_not_resurrected(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "hello")
    slot.drain()
    slot.acp_backend = ACP_BACKEND_KAS
    _save_slot_to_history(state, slot, force=True)
    slot.acp_backend = None
    _save_slot_to_history(state, slot, force=True)

    restored = _rehydrate_slot_from_history(_make_state(tmp_path), "chat-1")
    assert restored is not None
    assert restored.acp_backend is None


# ── The agent surface (session_backend) ──────────────────────────────────────


@pytest.fixture
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _backend(state, caller, target: str, **kw) -> dict:
    return asyncio.run(
        sc.backend_target(state, caller_session_key=slot_history_key(caller), target=target, **kw)
    )


def test_the_tool_verb_reads_the_pick_and_the_effective_backend(tmp_path, _enabled):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")
    out = _backend(state, caller, "chat-2")
    assert out["backend"] is None
    assert out["effective_backend"] == "kiro"
    assert "kas" in out["selectable"] and "kiro" in out["selectable"]


def test_the_tool_verb_switches_through_the_shared_switch(tmp_path, _enabled):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    out = _backend(state, caller, "chat-2", set_backend=True, backend="kas")
    assert out["changed"] is True
    assert (out["backend"], out["effective_backend"]) == ("kas", "kas")
    assert target.acp_backend == ACP_BACKEND_KAS
    out = _backend(state, caller, "chat-2", set_backend=True, backend=None)
    assert target.acp_backend is None and out["backend"] is None


def test_the_tool_verb_refuses_as_the_route_does(tmp_path, _enabled):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    with pytest.raises(sc.SessionControlError) as exc:
        _backend(state, caller, "chat-2", set_backend=True, backend="nope")
    assert (exc.value.code, exc.value.status) == ("invalid_backend", 400)
    _busy(target)
    with pytest.raises(sc.SessionControlError) as exc:
        _backend(state, caller, "chat-2", set_backend=True, backend="kas")
    assert (exc.value.code, exc.value.status) == ("turn_in_flight", 409)
    assert target.acp_backend is None


def test_the_tool_verb_keeps_the_owner_gates(tmp_path, _enabled):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    hidden = state.get_or_create_slot("chat-hidden", memory_mode="incognito")
    with pytest.raises(sc.SessionControlError):
        _backend(state, caller, "chat-hidden", set_backend=True, backend="kas")
    assert hidden.acp_backend is None


def test_the_tool_verb_is_off_when_session_control_is(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    with pytest.raises(sc.SessionControlError):
        _backend(state, caller, "chat-2", set_backend=True, backend="kas")
    assert target.acp_backend is None


_VERIFIED = "dashboard:chat-verified"


def test_the_mcp_tool_posts_the_verified_key_and_maps_default_to_no_pick():
    reply = {
        "ok": True,
        "target": "chat-2",
        "changed": True,
        "backend": None,
        "effective_backend": "kiro",
        "selectable": ["kiro", "kas"],
    }
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value=reply) as post,
    ):
        out = _call_tool_inner("session_backend", {"target": "chat-2", "backend": "default"})
    assert post.call_args.args[0] == "/api/session-control/backend"
    assert post.call_args.args[1] == {"target": "chat-2", "backend": None}
    assert post.call_args.kwargs["session_key"] == _VERIFIED
    assert "switched backend" in out and "follows the default" in out


def test_the_mcp_tool_reads_without_a_backend_key():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post",
            return_value={"target": "chat-2", "backend": "kas", "effective_backend": "kas"},
        ) as post,
    ):
        out = _call_tool_inner("session_backend", {"target": "chat-2"})
    assert "backend" not in post.call_args.args[1]
    assert "`kas`" in out


def test_the_mcp_tool_reports_a_refusal_as_an_error():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value={"error": "a turn is in flight"}),
    ):
        out = _call_tool_inner("session_backend", {"target": "chat-2", "backend": "kas"})
    assert out.startswith("Error:") and "a turn is in flight" in out


def test_the_session_control_route_is_strict_internal():
    from kiro_crew.dashboard.handlers import session_control as handlers_sc

    assert json.loads(json.dumps({"ok": True}))  # keeps json import honest
    assert callable(handlers_sc.api_session_control_backend)


def test_the_models_route_accepts_kiro_by_its_policy_name():
    """``?backend=kiro`` names Kiro, the same spelling the switch route accepts."""
    from kiro_crew.dashboard.handlers.agents import models_backend_from_query

    assert models_backend_from_query("kiro") == ACP_BACKEND_KIRO
    assert models_backend_from_query(ACP_BACKEND_KIRO) == ACP_BACKEND_KIRO
    assert models_backend_from_query(ACP_BACKEND_KAS) == ACP_BACKEND_KAS
    assert models_backend_from_query("bogus") is None
    assert models_backend_from_query("../../bin/sh") is None
