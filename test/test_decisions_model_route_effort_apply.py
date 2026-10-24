"""Jev picks the parent turn's effort inside the session's range, and the owner's returns.

A routed effort is pushed to the running session only: it is never the owner's
saved level, so the turn after it, or a turn that is not routed, puts the
owner's level back. The client double models what makes the order observable --
``set_model`` re-applies the TARGET model's stored level, a push replaces it, and
the level live when the prompt streams is the level the turn ran at.
"""

# Fixtures imported from the ratchet suite are used by name, as test arguments.
# flake8: noqa: F401,F811

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from test_decisions_model_route_ratchet import (
    BASE_MODEL,
    CHEAP_MODEL,
    PICKED_MODEL,
    _following_client,
    _routed,
    _strip_rows,
    _switched,
    _turn,
    answers,
    clean_registry,
    consented,
)
from test_decisions_strip_rides_message import _quiet_sel, _runner_state, _settle

from kiro_crew.config.sections import DecisionsConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_api import slot_guards
from kiro_crew.dashboard.chat_turn import directives, session_reset
from kiro_crew.dashboard.chat_turn.jev_route_effort import _JEV_ROUTE_EFFORT_ATTR
from kiro_crew.dashboard.recovery_replays import ReplayFamily, replays_of
from kiro_crew.decisions import gate as gate_mod
from kiro_crew.llm_helpers import pick_epoch_host

OWNER_EFFORT = "medium"
RANGE = {"models": [], "effort_min": "low", "effort_max": "high"}
NO_EFFORT_BOUNDS = {"models": [], "effort_min": "", "effort_max": ""}
#: What ``set_model`` re-applies on a switch: each model's own stored level.
STORED = {BASE_MODEL: OWNER_EFFORT, CHEAP_MODEL: "xhigh", PICKED_MODEL: "minimal"}


def _effort_client(state, client, *, owner=OWNER_EFFORT, stored=None):
    """Make *client* a following client with a live effort; returns the level seen per stream.

    Levels are keyed on the model, as the provider keys them: ``_resolve_effort``
    answers for the LIVE model and ``owner_effort(model)`` for the one asked about
    (the live one when none is named).
    ``client.live_effort`` is the level on the wire, and ``state.sessions.release``
    records it into ``client.released_at``.
    """
    _following_client(state, client)
    table = dict(STORED if stored is None else stored)
    if owner:
        table[BASE_MODEL] = owner
    else:
        table.pop(BASE_MODEL, None)
    live = {"level": table.get(BASE_MODEL, "default")}
    ran_at: list[str] = []
    client.live_effort = live
    client.released_at = []
    state.sessions.release = MagicMock(
        side_effect=lambda _key: client.released_at.append(live["level"])
    )

    switch, stream = client.set_model.side_effect, client.stream.side_effect

    async def _set_model(name: str) -> None:
        await switch(name)
        live["level"] = table.get(name, "default")

    async def _push(level: str) -> bool:
        live["level"] = level
        return True

    def _stream(*args, **kwargs):
        ran_at.append(live["level"])
        return stream(*args, **kwargs)

    client.set_model = AsyncMock(side_effect=_set_model)
    client.push_live_effort = AsyncMock(side_effect=_push)
    client.stream = MagicMock(side_effect=_stream)
    client.supports_effort = MagicMock(return_value=True)
    client._resolve_effort = MagicMock(side_effect=lambda: table.get(client.served_model))
    client.owner_effort = MagicMock(
        side_effect=lambda model=None: table.get(client.served_model if model is None else model)
    )
    client._effort_per_model = {}
    client._apply_effort_overlay = MagicMock(return_value=True)
    return ran_at


def _ranged(key, jev_range=RANGE):
    slot = _routed(key)
    slot.jev_range = dict(jev_range)
    return slot


def _rows(tmp_path):
    rows = []
    for path in sorted((tmp_path / "decisions").glob("*.jsonl")):
        rows += [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
    return rows


@pytest.mark.asyncio
async def test_a_complex_turn_runs_at_the_ranges_highest_effort(tmp_path, answers):
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client)
    answers("complex")

    await _turn(state, _ranged("chat-eff-1"), "redesign the scheduler")

    assert ran_at == ["high"]


@pytest.mark.asyncio
async def test_a_simple_turn_runs_at_the_ranges_lowest_effort(tmp_path, answers):
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client)
    answers("simple")

    await _turn(state, _ranged("chat-eff-2"), "rename this variable")

    assert _switched(client) == [CHEAP_MODEL]
    assert ran_at == ["low"]


@pytest.mark.asyncio
async def test_the_turn_after_a_routed_effort_goes_back_to_the_owners(tmp_path, answers):
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client)
    slot = _ranged("chat-eff-3")

    answers("complex")
    await _turn(state, slot, "redesign the scheduler")
    answers("medium")
    await _turn(state, slot, "explain this function")

    assert ran_at == ["high", OWNER_EFFORT]


@pytest.mark.asyncio
async def test_a_turn_that_is_no_longer_routed_restores_the_owners_effort(tmp_path, answers):
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client)
    slot = _ranged("chat-eff-4")

    answers("complex")
    await _turn(state, slot, "redesign the scheduler")
    # The owner picks a model by hand: the slot is not armed for routing.
    slot.jev_route = False
    slot.model = BASE_MODEL
    await _turn(state, slot, "and now the tests")

    assert ran_at == ["high", OWNER_EFFORT]


@pytest.mark.asyncio
async def test_a_routed_effort_writes_neither_the_override_nor_the_overlay(tmp_path, answers):
    state, client = _runner_state(tmp_path)
    _effort_client(state, client)
    slot = _ranged("chat-eff-5")

    answers("complex")
    await _turn(state, slot, "redesign the scheduler")
    answers("simple")
    await _turn(state, slot, "rename this variable")

    client.push_live_effort.assert_awaited()
    assert client._effort_per_model == {}
    client._apply_effort_overlay.assert_not_called()
    client.change_effort.assert_not_awaited()
    client.clear_effort.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_switched_turn_runs_at_the_routed_effort_not_the_targets_stored_one(
    tmp_path, answers, monkeypatch
):
    pinned = {"simple": CHEAP_MODEL, "medium": "", "complex": PICKED_MODEL}
    monkeypatch.setattr(
        gate_mod,
        "_snapshot",
        lambda: MagicMock(decisions=DecisionsConfig(model_route=dict(pinned))),
    )
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client)
    answers("complex")

    await _turn(state, _ranged("chat-eff-6"), "redesign the scheduler")

    assert _switched(client) == [PICKED_MODEL]
    assert ran_at == [
        "high"
    ], "set_model re-applied the target's stored level; the push must follow"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, ""], ids=["none", "empty"])
async def test_a_session_with_no_baseline_effort_routes_none_and_says_why(tmp_path, answers, owner):
    state, client = _runner_state(tmp_path)
    _effort_client(state, client, owner=owner, stored={})
    answers("simple")

    slot = _ranged("chat-eff-7")
    await _turn(state, slot, "rename this variable")

    client.push_live_effort.assert_not_awaited()
    assert _switched(client) == [CHEAP_MODEL], "the model still routes"
    rows = _rows(tmp_path)
    assert [r.get("error") for r in rows] == [None, None], "no error row"
    (outcome,) = [r for r in rows if "model_chosen" in r]
    assert outcome["tier"] == "simple"
    assert outcome["effort_skipped"] == "baseline-unknown"
    assert "effort_chosen" not in outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "returns-false"])
async def test_a_failed_effort_push_keeps_the_turn_running_and_never_resets(
    tmp_path, answers, monkeypatch, failure
):
    reset = AsyncMock()
    monkeypatch.setattr(slot_guards, "_reset_slot_session_or_warn", reset)
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client)
    if failure == "raises":
        client.push_live_effort = AsyncMock(side_effect=RuntimeError("adapter said no"))
    else:
        client.push_live_effort = AsyncMock(return_value=False)
    answers("simple")
    slot = _ranged("chat-eff-8")

    await _turn(state, slot, "rename this variable")

    # A push that raised may have landed late, so the turn's end puts the owner's back.
    pushes = [call("low")] + ([call(STORED[CHEAP_MODEL])] if failure == "raises" else [])
    assert client.push_live_effort.await_args_list == pushes
    assert len(ran_at) == 1, "the prompt was still streamed"
    assert _switched(client) == [CHEAP_MODEL]
    state.sessions.reset.assert_not_awaited()
    reset.assert_not_awaited()
    client.change_effort.assert_not_awaited()
    client.clear_effort.assert_not_awaited()
    assert "effort_chosen" not in _strip_rows(slot)[0], "an unapplied effort is not claimed"


@pytest.mark.asyncio
async def test_a_model_with_no_effort_channel_routes_no_effort(tmp_path, answers):
    state, client = _runner_state(tmp_path)
    _effort_client(state, client)
    client.supports_effort = MagicMock(return_value=False)
    answers("simple")
    slot = _ranged("chat-eff-9")

    await _turn(state, slot, "rename this variable")

    client.push_live_effort.assert_not_awaited()
    assert not [r for r in _rows(tmp_path) if r.get("error")]
    assert "effort_chosen" not in _strip_rows(slot)[0]


@pytest.mark.asyncio
async def test_a_range_without_effort_bounds_pushes_no_effort(tmp_path, answers):
    state, client = _runner_state(tmp_path)
    _effort_client(state, client)
    slot = _ranged("chat-eff-10", NO_EFFORT_BOUNDS)

    answers("simple")
    await _turn(state, slot, "rename this variable")
    answers("complex")
    await _turn(state, slot, "redesign the scheduler")

    client.push_live_effort.assert_not_awaited()
    assert all("effort_chosen" not in row for row in _strip_rows(slot))


@pytest.mark.asyncio
async def test_the_routed_record_names_the_effort_chosen_and_the_owners(tmp_path, answers):
    state, client = _runner_state(tmp_path)
    _effort_client(state, client)
    slot = _ranged("chat-eff-11")
    answers("complex")

    await _turn(state, slot, "redesign the scheduler")

    (row,) = _strip_rows(slot)
    assert row["effort_chosen"] == "high"
    assert row["baseline_effort"] == OWNER_EFFORT


@pytest.mark.asyncio
async def test_a_switch_to_a_model_with_no_stored_effort_runs_at_the_tiers_and_names_the_owners(
    tmp_path, answers
):
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client, stored={})
    answers("simple")
    slot = _ranged("chat-eff-12")

    await _turn(state, slot, "rename this variable")

    assert _switched(client) == [CHEAP_MODEL]
    assert ran_at == ["low"], "the target has no level of its own: the tier's stands"
    (row,) = _strip_rows(slot)
    assert row["effort_chosen"] == "low"
    assert row["baseline_effort"] == OWNER_EFFORT, "the owner's level for the baseline model"
    assert not [r for r in _rows(tmp_path) if r.get("error")]
    assert client.released_at == [OWNER_EFFORT], "restored from the baseline model's level"


@pytest.mark.asyncio
async def test_a_routed_turn_puts_the_owners_effort_back_before_its_session_is_released(
    tmp_path, answers
):
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client)
    answers("complex")

    await _turn(state, _ranged("chat-eff-13"), "redesign the scheduler")

    assert ran_at == ["high"]
    assert client.released_at == [OWNER_EFFORT], "a channel prompt next would run at this level"


@pytest.mark.asyncio
@pytest.mark.parametrize("family", list(ReplayFamily), ids=lambda family: family.value)
async def test_a_routed_turn_with_a_replay_armed_still_restores_the_owners_before_release(
    tmp_path, answers, family
):
    state, client = _runner_state(tmp_path)
    _effort_client(state, client)
    slot = _ranged("chat-eff-14")
    stream = client.stream.side_effect

    def _arm_a_replay(*args, **kwargs):
        replays_of(slot).arm(
            family, entry_id="replay-1", session_key="", stop_gen=0, session_stop_gen=0
        )
        return stream(*args, **kwargs)

    client.stream.side_effect = _arm_a_replay
    answers("complex")

    await _turn(state, slot, "redesign the scheduler")

    assert client.released_at == [OWNER_EFFORT], "the replay does not run at the routed level"


@pytest.mark.asyncio
async def test_a_synthetic_recovery_turns_gate_retries_a_pending_restore(tmp_path):
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client)
    client.live_effort["level"] = "high"
    setattr(pick_epoch_host(client), _JEV_ROUTE_EFFORT_ATTR, OWNER_EFFORT)

    with _quiet_sel():
        await chat_runner._run_chat(
            state,
            _ranged("chat-eff-18"),
            "redesign the scheduler",
            _directive_user_origin=True,
            _synthetic_recovery_turn=True,
        )

    assert ran_at == [OWNER_EFFORT]


@pytest.mark.asyncio
async def test_a_turn_ending_on_a_model_with_its_own_level_restores_that_models(tmp_path, answers):
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client)
    answers("simple")

    await _turn(state, _ranged("chat-eff-19"), "rename this variable")

    assert _switched(client) == [CHEAP_MODEL]
    assert ran_at == ["low"]
    assert client.released_at == [STORED[CHEAP_MODEL]], "the dropdown shows the live model's level"


def _release_turn(state, client, slot, order, **overrides):
    """A routed turn at its end: Slack mirror open, the owner's level marked for restore."""
    state.slack_client = MagicMock(append_task=AsyncMock())
    state.slack_client.stop_stream = AsyncMock(side_effect=lambda *_a: order.append("stop_stream"))
    state.sessions.release = MagicMock(side_effect=lambda _key: order.append("release"))
    state.sessions.reset = AsyncMock(side_effect=lambda _key: order.append("reset"))
    state.sessions.discard_conversation = AsyncMock(
        side_effect=lambda _key: order.append("discard")
    )
    client.push_live_effort = AsyncMock(side_effect=lambda level: order.append(f"push {level}"))
    client.owner_effort = MagicMock(return_value=None)
    setattr(pick_epoch_host(client), _JEV_ROUTE_EFFORT_ATTR, OWNER_EFFORT)
    fields = dict(
        _acquired=True,
        _deferred_options_expiry=False,
        _mirror_active_task="",
        _mirror_active_task_title="",
        _mirror_chan="C1",
        _mirror_stream_ts="1.0",
        _mirror_thread="",
        _seen_tool_identity=False,
        _turn_started=0.0,
        session_key="chat-eff-20",
        slot=slot,
        state=state,
        client=client,
        needs_session_reset=False,
        needs_conversation_discard=False,
    )
    return SimpleNamespace(**{**fields, **overrides})


@pytest.mark.asyncio
async def test_the_restore_runs_after_the_mirror_stream_stops_and_before_the_permit_is_released(
    tmp_path,
):
    state, client = _runner_state(tmp_path)
    order: list[str] = []
    turn = _release_turn(state, client, _ranged("chat-eff-20"), order)

    with patch.object(directives, "_report_unclaimed_directives", AsyncMock()):
        await session_reset._turn_release_session(turn)

    assert order == ["stop_stream", f"push {OWNER_EFFORT}", "release"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "teardown"),
    [("needs_session_reset", "reset"), ("needs_conversation_discard", "discard")],
)
async def test_a_turn_flagged_for_a_reset_or_discard_pushes_no_effort_at_release(
    tmp_path, flag, teardown
):
    state, client = _runner_state(tmp_path)
    order: list[str] = []
    turn = _release_turn(state, client, _ranged("chat-eff-21"), order, **{flag: True})

    with (
        patch.object(directives, "_report_unclaimed_directives", AsyncMock()),
        patch.object(session_reset, "_broadcast_expired_oauth_banners"),
    ):
        await session_reset._turn_release_session(turn)

    assert order == ["stop_stream", teardown, "release"], "the push could respawn a dead process"


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_its_effort_push_is_in_flight_still_restores_the_owners(
    tmp_path, answers
):
    state, client = _runner_state(tmp_path)
    _effort_client(state, client)
    live = client.live_effort
    pushed = client.push_live_effort.side_effect
    calls = 0

    async def _push_then_cancel(level: str) -> bool:
        nonlocal calls
        calls += 1
        await pushed(level)
        if calls == 1:
            raise asyncio.CancelledError
        return True

    client.push_live_effort.side_effect = _push_then_cancel
    answers("complex")

    await _turn(state, _ranged("chat-eff-15"), "redesign the scheduler")

    assert live["level"] == OWNER_EFFORT
    assert client.released_at == [OWNER_EFFORT]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "returns-false"])
async def test_a_failed_restore_is_retried_by_the_next_turns_gate(tmp_path, answers, failure):
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client)
    pushed = client.push_live_effort.side_effect
    calls = 0

    async def _push_failing_the_restore(level: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            if failure == "returns-false":
                return False
            raise RuntimeError("adapter said no")
        return await pushed(level)

    client.push_live_effort.side_effect = _push_failing_the_restore
    slot = _ranged("chat-eff-16")
    answers("complex")
    await _turn(state, slot, "redesign the scheduler")
    assert client.released_at == ["high"], "the restore did not land"

    slot.jev_route = False
    slot.model = BASE_MODEL
    await _turn(state, slot, "and now the tests")

    assert ran_at == ["high", OWNER_EFFORT]


@pytest.mark.asyncio
async def test_a_range_on_a_slot_that_is_not_on_auto_jev_bounds_neither_model_nor_effort(
    tmp_path, answers, monkeypatch
):
    """The implicit arm: model ``auto`` with the preview on, but the Jev row was never picked."""
    pinned = {"simple": "", "medium": "", "complex": PICKED_MODEL}
    monkeypatch.setattr(
        gate_mod,
        "_snapshot",
        lambda: MagicMock(decisions=DecisionsConfig(model_route=dict(pinned))),
    )
    state, client = _runner_state(tmp_path)
    ran_at = _effort_client(state, client)
    slot = _ranged(
        "chat-eff-17", {"models": [CHEAP_MODEL], "effort_min": "low", "effort_max": "high"}
    )
    slot.jev_route = False
    slot.model = "auto"
    answers("complex")

    await _turn(state, slot, "redesign the scheduler")

    assert _switched(client) == [PICKED_MODEL], "the range's model list bounds nothing"
    assert ran_at == [STORED[PICKED_MODEL]], "the range's effort bound pushes nothing"
    client.push_live_effort.assert_not_awaited()
