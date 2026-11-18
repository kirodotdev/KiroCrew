"""POST /api/chat/slots/model with ``reasoning_effort``: one pass sets the model and the effort."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider

OPUS = "claude-opus-4.8"


def _app(state: DashboardState) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slot_reasoning_effort, api_chat_slots_model

    @web.middleware
    async def dashboard_auth_marker(request, handler):
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[dashboard_auth_marker])
    app["state"] = state
    app.router.add_post("/api/chat/slots/model", api_chat_slots_model)
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    return app


async def _post(
    state: DashboardState, body: dict, path: str = "/api/chat/slots/model"
) -> tuple[int, dict]:
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(path, json=body)
        return resp.status, await resp.json()


def _live(*, pushed: object = True, active: bool = False) -> MagicMock:
    provider = MagicMock(spec=AcpProvider)
    provider.supports_effort.return_value = True
    provider.accepts_effort_level.return_value = True
    provider.has_active_turn.return_value = active
    provider.change_effort = AsyncMock(return_value=pushed)
    provider.clear_effort = AsyncMock(return_value=pushed)
    return provider


def _state(tmp_path, provider: object = None, *, effort: str = "low") -> tuple:
    state = _make_state(tmp_path)
    state.sessions.reset = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=provider)
    slot = state.get_or_create_slot("a", model=OPUS)
    slot.reasoning_effort = effort
    state.push_slots_update = MagicMock()
    return state, slot


def _mark_running(slot: _ChatSlot) -> None:
    task = MagicMock()
    task.done.return_value = False
    slot.task = task


@pytest.mark.asyncio
async def test_a_model_change_commits_the_level_with_the_reset(tmp_path):
    state, slot = _state(tmp_path)
    slot.model = "claude-sonnet-4.6"

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": "high"})

    assert status == 200
    assert data["switched"] == ["a"]
    assert data["effort_deferred"] == []
    assert (slot.model, slot.reasoning_effort) == (OPUS, "high")
    state.sessions.reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_effort_only_change_pushes_live_and_never_resets(tmp_path):
    provider = _live()
    state, slot = _state(tmp_path, provider)

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": "high"})

    assert status == 200
    assert data["switched"] == ["a"]
    assert data["effort_deferred"] == []
    assert slot.reasoning_effort == "high"
    provider.change_effort.assert_awaited_once_with("high")
    state.sessions.reset.assert_not_awaited()
    state.sessions.record_effort_override.assert_called_once_with("dashboard:a", "high")


@pytest.mark.asyncio
async def test_a_running_session_is_skipped_by_default_and_deferred_when_not(tmp_path):
    provider = _live(active=True)
    state, slot = _state(tmp_path, provider)
    _mark_running(slot)

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": "high"})
    assert status == 200
    assert data["skipped_running"] == ["a"]
    assert slot.reasoning_effort == "low"

    status, data = await _post(
        state, {"model": OPUS, "reasoning_effort": "high", "skip_running": False}
    )
    assert status == 200
    assert data["effort_deferred"] == ["a"]
    assert slot.reasoning_effort == "high"
    # A push now would race the streaming turn, and no turn is torn down.
    provider.change_effort.assert_not_awaited()
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_setup", "bucket"),
    [
        ("raises", "failed"),
        ("not_offered", "failed"),
    ],
)
async def test_a_push_that_changed_nothing_leaves_the_slot_alone(tmp_path, provider_setup, bucket):
    provider = _live()
    if provider_setup == "raises":
        provider.change_effort = AsyncMock(side_effect=RuntimeError("boom"))
    if provider_setup == "not_offered":
        provider.accepts_effort_level.return_value = False
    state, slot = _state(tmp_path, provider)

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": "high"})

    assert status == 200
    assert data[bucket] == ["a"]
    assert data["switched"] == []
    assert slot.reasoning_effort == "low"
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_live_session_records_the_level_for_the_next_start(tmp_path):
    state, slot = _state(tmp_path)

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": "max"})

    assert status == 200
    assert data["switched"] == ["a"]
    assert slot.reasoning_effort == "max"
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_absent_level_leaves_every_effort_alone(tmp_path):
    provider = _live()
    state, slot = _state(tmp_path, provider)

    status, data = await _post(state, {"model": OPUS})

    assert status == 200
    assert data["unchanged"] == ["a"]
    assert slot.reasoning_effort == "low"
    provider.change_effort.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["turbo", 3, ["high"]])
async def test_an_invalid_level_is_refused_and_nothing_changes(tmp_path, bad):
    state, slot = _state(tmp_path)

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": bad})

    assert status == 400
    assert "reasoning_effort" in data["error"]
    assert slot.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_default_is_refused_before_any_slot_or_marker_changes(tmp_path, monkeypatch):
    # The reset path cannot clear kiro-cli's workspace effort overlay, so a bulk
    # Default could leave sessions on their old level while every surface said
    # Default. Default is set from each session's own effort dropdown.
    remember = MagicMock()
    monkeypatch.setattr(chat_handlers, "_remember_reasoning_effort_for_restore", remember)
    push = AsyncMock(return_value="applied")
    monkeypatch.setattr(chat_handlers, "_push_live_effort", push)
    provider = _live()
    state, slot = _state(tmp_path, provider)
    slot.model = "claude-sonnet-4.6"

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": ""})

    assert status == 400
    assert data["code"] == "invalid_reasoning_effort"
    assert (slot.model, slot.reasoning_effort) == ("claude-sonnet-4.6", "low")
    state.sessions.reset.assert_not_awaited()
    push.assert_not_awaited()
    provider.clear_effort.assert_not_awaited()
    remember.assert_not_called()


@pytest.mark.asyncio
async def test_a_relay_archive_keeps_its_effort(tmp_path):
    # The effort route refuses a relay archive (409), so the bulk pass must not
    # write a level onto one either.
    state, slot = _state(tmp_path)
    slot.executor = "remote"

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": "high"})

    assert status == 200
    assert data["unchanged"] == ["a"]
    assert slot.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_the_level_is_retained_for_restore_once_before_any_slot_changes(
    tmp_path, monkeypatch
):
    remember = MagicMock()
    monkeypatch.setattr(chat_handlers, "_remember_reasoning_effort_for_restore", remember)
    state, slot = _state(tmp_path, _live())
    state.get_or_create_slot("b", model=OPUS).reasoning_effort = "low"

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": "high"})

    assert status == 200
    assert sorted(data["switched"]) == ["a", "b"]
    remember.assert_called_once_with("high")


@pytest.mark.asyncio
async def test_a_marker_failure_refuses_before_any_slot_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        chat_handlers,
        "_remember_reasoning_effort_for_restore",
        MagicMock(side_effect=ValueError("validated effort marker limit reached")),
    )
    provider = _live()
    state, slot = _state(tmp_path, provider)

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": "high"})

    assert status == 503
    assert data["code"] == "effort_marker_unavailable"
    assert slot.reasoning_effort == "low"
    provider.change_effort.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_absent_level_writes_no_restore_marker(tmp_path, monkeypatch):
    remember = MagicMock()
    monkeypatch.setattr(chat_handlers, "_remember_reasoning_effort_for_restore", remember)
    state, _slot = _state(tmp_path, _live())

    status, _data = await _post(state, {"model": OPUS})

    assert status == 200
    remember.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "bucket", "deferred"),
    [
        ("applied", "switched", []),
        ("not_applied", "switched", ["a"]),
        ("refused", "failed", []),
        ("failed", "failed", []),
    ],
)
async def test_the_bulk_push_goes_through_the_shared_step(
    tmp_path, monkeypatch, outcome, bucket, deferred
):
    push = AsyncMock(return_value=outcome)
    monkeypatch.setattr(chat_handlers, "_push_live_effort", push)
    provider = _live()
    state, slot = _state(tmp_path, provider)

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": "high"})

    assert status == 200
    assert data[bucket] == ["a"]
    assert data["effort_deferred"] == deferred
    push.assert_awaited_once_with(provider, "high", slot.key)
    provider.change_effort.assert_not_awaited()
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["relaunch", "command_refused"])
async def test_a_refused_push_lands_in_failed(tmp_path, refusal):
    from kiro_crew.acp.command_refused import AcpCommandRefused

    provider = _live()
    if refusal == "relaunch":
        provider.switches_by_relaunch = True
        provider.change_effort = AsyncMock(side_effect=RuntimeError("cannot run that effort"))
    else:
        provider.switches_by_relaunch = False
        provider.change_effort = AsyncMock(side_effect=AcpCommandRefused("invalid effort"))
    state, slot = _state(tmp_path, provider)

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": "high"})

    assert status == 200
    assert data["failed"] == ["a"]
    assert slot.reasoning_effort == "low"
    state.sessions.record_effort_override.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "status", "body"),
    [
        ("applied", 200, {"ok": True, "reasoning_effort": "high"}),
        (
            "refused",
            400,
            {
                "error": "The agent did not accept the reasoning effort change.",
                "code": "effort_refused",
            },
        ),
    ],
)
async def test_the_single_session_route_pushes_through_the_same_step(
    tmp_path, monkeypatch, outcome, status, body
):
    push = AsyncMock(return_value=outcome)
    monkeypatch.setattr(chat_handlers, "_push_live_effort", push)
    provider = _live()
    state, slot = _state(tmp_path, provider)

    got_status, data = await _post(
        state, {"reasoning_effort": "high"}, path="/api/chat/slots/a/reasoning-effort"
    )

    assert (got_status, data) == (status, body)
    push.assert_awaited_once_with(provider, "high", "a")
    provider.change_effort.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effort", "pushed", "expected"),
    [
        ("high", True, "applied"),
        ("high", False, "not_applied"),
        ("", None, "overlay_busy"),
        ("high", RuntimeError("boom"), "failed"),
    ],
)
async def test_the_shared_push_names_each_result(effort, pushed, expected):
    provider = _live()
    provider.switches_by_relaunch = False
    mock = AsyncMock(side_effect=pushed) if isinstance(pushed, Exception) else None
    provider.change_effort = mock or AsyncMock(return_value=pushed)
    provider.clear_effort = AsyncMock(return_value=pushed)

    assert await chat_handlers._push_live_effort(provider, effort, "a") == expected
    if effort:
        provider.clear_effort.assert_not_awaited()
    else:
        provider.clear_effort.assert_awaited_once_with(every_model=True)
        provider.change_effort.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effort", "offered", "bucket"),
    [
        # A level the live session hides would be discarded at its next start.
        ("high", False, "failed"),
        ("high", True, "effort_deferred"),
    ],
)
async def test_a_running_session_defers_only_a_level_it_offers(tmp_path, effort, offered, bucket):
    provider = _live(active=True)
    provider.accepts_effort_level.return_value = offered
    state, slot = _state(tmp_path, provider)
    _mark_running(slot)

    status, data = await _post(
        state, {"model": OPUS, "reasoning_effort": effort, "skip_running": False}
    )

    assert status == 200
    assert data[bucket] == ["a"]
    if bucket == "effort_deferred":
        assert data["switched"] == ["a"]
        assert slot.reasoning_effort == effort
        state.sessions.record_effort_override.assert_called_once_with("dashboard:a", effort)
    else:
        assert data["switched"] == []
        assert data["effort_deferred"] == []
        assert slot.reasoning_effort == "low"
        state.sessions.record_effort_override.assert_not_called()
    provider.change_effort.assert_not_awaited()
    provider.clear_effort.assert_not_awaited()
    state.sessions.reset.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["_active_fallback_model", "_refusal_fallback_primary"])
async def test_a_fallback_serving_slot_takes_the_level_with_the_reset(
    tmp_path, monkeypatch, fallback
):
    # The pin equals the pick while the wire model is the fallback: a live push
    # would store the level under the fallback, and the restore would undo it.
    push = AsyncMock(return_value="applied")
    monkeypatch.setattr(chat_handlers, "_push_live_effort", push)
    provider = _live()
    state, slot = _state(tmp_path, provider)
    setattr(slot, fallback, "claude-sonnet-4.6" if fallback == "_active_fallback_model" else OPUS)
    gen = slot._model_pick_gen

    status, data = await _post(state, {"model": OPUS, "reasoning_effort": "high"})

    assert status == 200
    assert data["switched"] == ["a"]
    assert (slot.model, slot.reasoning_effort) == (OPUS, "high")
    assert slot._model_pick_gen == gen + 1
    state.sessions.reset.assert_awaited_once()
    push.assert_not_awaited()
    provider.change_effort.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["_active_fallback_model", "_refusal_fallback_primary"])
async def test_a_busy_fallback_serving_slot_is_skipped_even_when_not_skipping_running(
    tmp_path, monkeypatch, fallback
):
    # The panel cannot see fallback state and counts this as effort-only, which
    # never stops a reply: the reset must not tear the running turn down.
    push = AsyncMock(return_value="applied")
    monkeypatch.setattr(chat_handlers, "_push_live_effort", push)
    provider = _live(active=True)
    state, slot = _state(tmp_path, provider)
    _mark_running(slot)
    setattr(slot, fallback, "claude-sonnet-4.6" if fallback == "_active_fallback_model" else OPUS)

    status, data = await _post(
        state, {"model": OPUS, "reasoning_effort": "high", "skip_running": False}
    )

    assert status == 200
    assert data["skipped_running"] == ["a"]
    assert data["switched"] == []
    assert slot.reasoning_effort == "low"
    state.sessions.reset.assert_not_awaited()
    push.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["_active_fallback_model", "_refusal_fallback_primary"])
@pytest.mark.parametrize("body", [{"model": OPUS}, {"model": OPUS, "reasoning_effort": "low"}])
async def test_a_fallback_serving_slot_with_no_effort_change_is_unchanged(
    tmp_path, monkeypatch, fallback, body
):
    push = AsyncMock(return_value="applied")
    monkeypatch.setattr(chat_handlers, "_push_live_effort", push)
    state, slot = _state(tmp_path, _live())
    setattr(slot, fallback, "claude-sonnet-4.6" if fallback == "_active_fallback_model" else OPUS)

    status, data = await _post(state, body)

    assert status == 200
    assert data["unchanged"] == ["a"]
    assert data["switched"] == []
    state.sessions.reset.assert_not_awaited()
    push.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_model", "body", "executor", "bucket", "advances"),
    [
        # Re-picking the level the slot has overtakes a queued session_set_model
        # effort (model_switch.apply_pending_model_pick's generation fence).
        (OPUS, {"model": OPUS, "reasoning_effort": "low"}, "", "unchanged", True),
        ("claude-sonnet-4.6", {"model": OPUS, "reasoning_effort": "low"}, "", "switched", True),
        # No level picked, or a read-only relay archive: nothing to overtake.
        (OPUS, {"model": OPUS}, "", "unchanged", False),
        ("claude-sonnet-4.6", {"model": OPUS}, "", "switched", False),
        (OPUS, {"model": OPUS, "reasoning_effort": "low"}, "remote", "unchanged", False),
    ],
)
async def test_a_same_level_pick_overtakes_a_queued_effort_only_when_picked(
    tmp_path, current_model, body, executor, bucket, advances
):
    state, slot = _state(tmp_path)
    slot.model = current_model
    if executor:
        slot.executor = executor
    before = slot._effort_pick_gen

    status, data = await _post(state, body)

    assert status == 200
    assert data[bucket] == ["a"]
    assert slot.reasoning_effort == "low"
    assert (slot._effort_pick_gen != before) is advances
