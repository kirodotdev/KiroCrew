"""``session_set_model``'s ``reasoning_effort`` argument.

The level rides the same pending pick as the model: validated against the set
the effort dropdown route accepts, committed by ``apply_pending_model_pick`` at
the target's next turn start in the same synchronous step as the gate, and
yielding to a level the user picks from the dropdown in the meantime.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider

_VERIFIED = "dashboard:chat-verified"


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _key(slot) -> str:
    return slot_history_key(slot)


def _set(state, caller, target: str, **kwargs) -> dict:
    return asyncio.run(
        sc.set_model_target(state, caller_session_key=_key(caller), target=target, **kwargs)
    )


def _pair(tmp_path):
    state = _make_state(tmp_path)
    return state, state.get_or_create_slot("chat-1"), state.get_or_create_slot("chat-2")


def test_an_effort_only_pick_keeps_the_model_and_commits_the_level(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.model = "pinned-model"
    target.jev_route = True
    gen = target._model_pick_gen

    out = _set(state, caller, "chat-2", reasoning_effort="high")

    assert out == {"ok": True, "target": "chat-2", "reasoning_effort": "high", "pending": True}
    assert target.reasoning_effort == "", "nothing changes until the next turn starts"

    assert sc.apply_pending_model_pick(state, target) is True, "a changed level needs a reset"
    assert target.reasoning_effort == "high"
    assert target.model == "pinned-model"
    # An effort-only pick is not a model pick: routing and the model-fallback
    # restore guard are left as they were.
    assert target.jev_route is True
    assert target._model_pick_gen == gen


def test_a_combined_pick_commits_both(tmp_path):
    state, caller, target = _pair(tmp_path)

    out = _set(state, caller, "chat-2", model="sonnet", reasoning_effort="low")

    assert out["model"] and out["reasoning_effort"] == "low"
    assert sc.apply_pending_model_pick(state, target) is True
    assert target.model == out["model"]
    assert target.reasoning_effort == "low"


def test_an_unchanged_level_needs_no_reset(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.reasoning_effort = "medium"

    _set(state, caller, "chat-2", reasoning_effort="medium")

    assert sc.apply_pending_model_pick(state, target) is False
    assert target._pending_model_pick is None


@pytest.mark.parametrize("level", ["turbo", "HIGH", " high", "high\n", "", "minimal", "default"])
def test_an_unknown_level_is_refused_before_anything_is_stored(tmp_path, level):
    """Only the five standard levels pass. "" is refused because on kiro-cli the
    model default only takes once the workspace effort overlay is cleared, which
    only the dropdown route does. A level only one harness advertises (Pi's
    ``minimal``, Claude's ``default``) is refused even when the process has seen
    it: it would not fold onto every backend the target could cold-start on."""
    state, caller, target = _pair(tmp_path)

    with pytest.raises(sc.SessionControlError) as exc:
        _set(state, caller, "chat-2", reasoning_effort=level)

    assert exc.value.code == "effort_rejected"
    assert target._pending_model_pick is None


def test_a_call_with_neither_setting_is_refused(tmp_path):
    state, caller, target = _pair(tmp_path)

    with pytest.raises(sc.SessionControlError) as exc:
        _set(state, caller, "chat-2")

    assert exc.value.code == "bad_request"
    assert target._pending_model_pick is None


def test_a_level_on_a_model_that_takes_none_is_stored_without_a_reset(tmp_path):
    """The dropdown keeps a level for a model that takes none as a stored value
    and leaves the live session alone; the turn-start commit must agree, or an
    agent's pick restarts the session for nothing."""
    state, caller, target = _pair(tmp_path)
    _set(state, caller, "chat-2", reasoning_effort="high")
    # Installed after the pick: a mock provider would read as a turn in flight.
    live = MagicMock(spec=AcpProvider)
    live.supports_effort.return_value = False
    live.has_active_turn.return_value = False
    state.sessions.get_provider = lambda _key: live

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.reasoning_effort == "high"


def test_a_level_on_a_capable_live_model_needs_a_reset(tmp_path):
    state, caller, target = _pair(tmp_path)
    _set(state, caller, "chat-2", reasoning_effort="high")
    # Installed after the pick: a mock provider would read as a turn in flight.
    live = MagicMock(spec=AcpProvider)
    live.supports_effort.return_value = True
    live.has_active_turn.return_value = False
    state.sessions.get_provider = lambda _key: live

    assert sc.apply_pending_model_pick(state, target) is True
    assert target.reasoning_effort == "high"


@pytest.mark.parametrize("spec", [LLMProvider, AcpSessionProvider])
def test_a_provider_on_the_base_supports_effort_default_keeps_the_reset(spec):
    """The base ``supports_effort`` returns a placeholder False. Read as an
    answer, it would store the level on such a provider and never apply it."""
    provider = MagicMock(spec=spec)
    provider.supports_effort.return_value = False

    assert sc.effort_commit_needs_reset(provider) is True


def test_a_level_on_a_base_provider_session_still_resets(tmp_path):
    state, caller, target = _pair(tmp_path)
    _set(state, caller, "chat-2", reasoning_effort="high")
    # Installed after the pick: a mock provider would read as a turn in flight.
    live = MagicMock(spec=AcpSessionProvider)
    live.supports_effort.return_value = False
    live.has_active_turn.return_value = False
    state.sessions.get_provider = lambda _key: live

    assert sc.apply_pending_model_pick(state, target) is True
    assert target.reasoning_effort == "high"


@pytest.mark.parametrize(
    ("model", "pair_id", "expected"),
    [
        ("gpt-6-astra[max]", True, "gpt-6-astra"),
        ("gpt-6-astra", True, ""),
        ("gpt-6-astra[max]", False, ""),
        (None, True, ""),
    ],
)
def test_legacy_effort_base(model, pair_id, expected):
    assert sc.legacy_effort_base(model, pair_id_backend=pair_id) == expected


def test_a_pair_id_backend_folds_the_legacy_suffix_off_the_pin(tmp_path, monkeypatch):
    """On a backend that spells effort into the model id the pin must not keep
    claiming the old level once a new one is committed."""
    monkeypatch.setattr(sc, "ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS", frozenset({"stub-pair"}))
    monkeypatch.setattr(sc, "select_provider_backend", lambda *_a: "stub-pair")
    state, caller, target = _pair(tmp_path)
    target.model = "gpt-6-astra[max]"
    _set(state, caller, "chat-2", reasoning_effort="low")

    assert sc.apply_pending_model_pick(state, target) is True
    assert target.model == "gpt-6-astra"
    assert target.reasoning_effort == "low"


def test_a_pair_id_model_pick_supersedes_the_pending_effort(tmp_path, monkeypatch):
    """On a pair-id backend the model picker is the effort control. Mutation
    guard: checking only the effort generation would strip the user's newer
    ``[max]`` pin and commit the stale level."""
    monkeypatch.setattr(sc, "ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS", frozenset({"stub-pair"}))
    monkeypatch.setattr(sc, "select_provider_backend", lambda *_a: "stub-pair")
    state, caller, target = _pair(tmp_path)
    target.model = "gpt-6-astra[low]"
    _set(state, caller, "chat-2", reasoning_effort="high")
    target.model = "gpt-6-astra[max]"
    target._model_pick_gen += 1  # the model picker's explicit pick

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == "gpt-6-astra[max]"
    assert target.reasoning_effort == ""


def test_a_newer_dropdown_level_wins_over_the_pending_one(tmp_path):
    """Mutation guard: without the effort generation check the stale pick
    would overwrite a level the user chose after it was queued."""
    state, caller, target = _pair(tmp_path)
    _set(state, caller, "chat-2", reasoning_effort="high")
    target.reasoning_effort = "low"  # the dropdown's pick

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.reasoning_effort == "low"


def test_a_dropdown_round_trip_to_the_original_level_still_wins(tmp_path):
    """The user moves away and back to the level the caller saw. Mutation
    guard: comparing levels instead of write generations would read that as
    untouched and commit the stale pick."""
    state, caller, target = _pair(tmp_path)
    target.reasoning_effort = "low"
    _set(state, caller, "chat-2", reasoning_effort="high")
    target.reasoning_effort = "max"
    target.reasoning_effort = "low"

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.reasoning_effort == "low"


def test_a_same_level_rewrite_does_not_drop_a_newer_pick(tmp_path):
    """The dropdown route re-commits the level it already wrote after awaiting
    its live push. A pick queued during that await is newer than the user's
    choice and must survive the rewrite. Mutation guard: bumping the
    generation on a same-value write."""
    state, caller, target = _pair(tmp_path)
    target.reasoning_effort = "low"
    _set(state, caller, "chat-2", reasoning_effort="high")
    target.reasoning_effort = "low"

    assert sc.apply_pending_model_pick(state, target) is True
    assert target.reasoning_effort == "high"


def test_each_half_yields_to_its_own_control_only(tmp_path):
    """A newer model pick drops the pending model but keeps the pending
    effort, and a newer dropdown level drops only the effort."""
    state, caller, target = _pair(tmp_path)
    _set(state, caller, "chat-2", model="sonnet", reasoning_effort="high")
    target.model = "opus"
    target._model_pick_gen += 1  # the model picker's explicit pick

    assert sc.apply_pending_model_pick(state, target) is True
    assert target.model == "opus"
    assert target.reasoning_effort == "high"

    out = _set(state, caller, "chat-2", model="sonnet", reasoning_effort="max")
    target.reasoning_effort = "low"  # the dropdown's pick

    sc.apply_pending_model_pick(state, target)
    assert target.model == out["model"]
    assert target.reasoning_effort == "low"


def test_an_effort_pick_is_dropped_if_the_target_became_channel_linked(tmp_path):
    """The turn-start gate covers the effort half too."""
    state, caller, target = _pair(tmp_path)
    _set(state, caller, "chat-2", reasoning_effort="high")
    target.linked_session_key = "slack:1786300000.000100"

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.reasoning_effort == ""


def test_a_busy_target_is_refused_and_gets_no_effort(tmp_path):
    state, caller, target = _pair(tmp_path)
    task = MagicMock()
    task.done.return_value = False
    target.task = task

    with pytest.raises(sc.SessionControlError) as exc:
        _set(state, caller, "chat-2", reasoning_effort="high")

    assert exc.value.code == "target_busy"
    assert target._pending_model_pick is None


def test_an_effort_switch_in_flight_refuses_the_pick(tmp_path, monkeypatch):
    """The dropdown holds the per-session switch lock across its commit, live
    push and rollback; a level captured inside that window would be dropped by
    the rollback's generation bump. Mutation guard: removing the lock check."""
    state, caller, target = _pair(tmp_path)
    held = MagicMock()
    held.locked.return_value = True
    monkeypatch.setattr(sc, "slot_switch_session_lock", lambda _key: held)

    with pytest.raises(sc.SessionControlError) as exc:
        _set(state, caller, "chat-2", reasoning_effort="high")

    assert exc.value.code == "target_busy"
    assert target._pending_model_pick is None


def test_a_model_only_pick_ignores_the_effort_switch_lock(tmp_path, monkeypatch):
    state, caller, target = _pair(tmp_path)
    held = MagicMock()
    held.locked.return_value = True
    monkeypatch.setattr(sc, "slot_switch_session_lock", lambda _key: held)

    out = _set(state, caller, "chat-2", model="claude-opus-5.5")

    assert out["model"] == "claude-opus-5.5"


def test_a_remote_crew_target_is_refused(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.executor = "remote"

    with pytest.raises(sc.SessionControlError) as exc:
        _set(state, caller, "chat-2", reasoning_effort="high")

    assert exc.value.code == "relay_archive_read_only"
    assert target._pending_model_pick is None


def test_a_read_shows_the_level_and_the_pending_level(tmp_path):
    state, caller, target = _pair(tmp_path)
    target.reasoning_effort = "medium"
    _set(state, caller, "chat-2", reasoning_effort="high")

    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert read["reasoning_effort"] == "medium"
    assert read["pending_reasoning_effort"] == "high"
    assert "pending_model" not in read, "an effort-only pick has no pending model"

    sc.apply_pending_model_pick(state, target)
    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert read["reasoning_effort"] == "high"
    assert "pending_reasoning_effort" not in read


# ── Route ────────────────────────────────────────────────────────────────────


def _request(tmp_path, body: dict):
    from test_session_control_set_model import _request as _base_request

    return _base_request(tmp_path, internal=True, body=body)


def test_route_refuses_a_non_string_effort(tmp_path):
    req = _request(tmp_path, {"target": "chat-2", "reasoning_effort": 3})
    resp = asyncio.run(handlers_sc.api_session_control_set_model(req))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "bad_request"


@pytest.mark.parametrize("field", ["model", "reasoning_effort"])
def test_route_refuses_an_explicit_null(tmp_path, field):
    # A present field must be a string; null is not read as "absent".
    body = {"target": "chat-2", "model": "claude-opus-5.5", "reasoning_effort": "high"}
    body[field] = None
    resp = asyncio.run(handlers_sc.api_session_control_set_model(_request(tmp_path, body)))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "bad_request"


def test_route_passes_absent_fields_as_none(tmp_path, monkeypatch):
    seen: dict = {}

    async def _verb(_state, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(sc, "set_model_target", _verb)
    req = _request(tmp_path, {"target": "chat-2", "reasoning_effort": "low"})
    resp = asyncio.run(handlers_sc.api_session_control_set_model(req))
    assert resp.status == 200
    assert seen["model"] is None
    assert seen["reasoning_effort"] == "low"


# ── MCP tool ─────────────────────────────────────────────────────────────────


def _call(name: str, args: dict, route: str, response: dict):
    """One frame of ``name`` as the verified caller, against one dashboard route."""
    dash = InMemoryDashboardClient({route: response})
    out = TABLE.call(name, args, ToolContext(dash, Caller.strict(_VERIFIED)))
    return out, dash.requests


def _tool(args: dict, response: dict):
    return _call("session_set_model", args, "POST /api/session-control/set-model", response)


def test_tool_sends_only_the_fields_given():
    out, (post,) = _tool(
        {"target": "chat-2", "reasoning_effort": "high"},
        {"ok": True, "target": "chat-2", "reasoning_effort": "high", "pending": True},
    )
    assert post.body == {"target": "chat-2", "reasoning_effort": "high"}
    assert "`chat-2` will switch to reasoning effort `high` when its next turn starts" in out


def test_tool_reports_both():
    out, (post,) = _tool(
        {"target": "chat-2", "model": "sonnet", "reasoning_effort": "low"},
        {
            "ok": True,
            "target": "chat-2",
            "model": "sonnet",
            "reasoning_effort": "low",
            "pending": True,
        },
    )
    assert post.body == {
        "target": "chat-2",
        "model": "sonnet",
        "reasoning_effort": "low",
    }
    assert "switch to `sonnet` at reasoning effort `low`" in out


def test_tool_refuses_a_call_with_neither_setting():
    # The table turns a refusal into the frame's error text; nothing is sent.
    out, sent = _tool({"target": "chat-2"}, {"ok": True})
    assert "reasoning_effort" in out
    assert sent == []


def test_the_read_tool_renders_the_levels():
    out, _ = _call(
        "session_read_message",
        {"target": "chat-2"},
        "GET /api/session-control/read",
        {
            "target": "chat-2",
            "title": "w",
            "messages": [],
            "total": 0,
            "next_since": 0,
            "reasoning_effort": "medium",
            "pending_reasoning_effort": "high",
        },
    )
    assert "reasoning effort medium" in out
    assert "pending reasoning effort high for its next turn" in out
