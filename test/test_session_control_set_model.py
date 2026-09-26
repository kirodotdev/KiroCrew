"""``session_set_model``: a pending model pick on an idle session.

The verb records the pick; ``apply_pending_model_pick`` commits it at the start
of the target's next turn after re-running the target gate in the same
synchronous step. The tests cover the gate at both points, the refusals that
change nothing, and the MCP tool's rendering.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import _call_tool_inner


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _key(slot) -> str:
    return slot_history_key(slot)


def _busy(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _set_model(state, caller, target: str, model: str) -> dict:
    return asyncio.run(
        sc.set_model_target(state, caller_session_key=_key(caller), target=target, model=model)
    )


def test_an_idle_target_gets_a_pending_pick_and_keeps_its_model(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model

    out = _set_model(state, caller, "chat-2", "sonnet")

    assert out == {"ok": True, "target": "chat-2", "model": out["model"], "pending": True}
    assert out["model"]
    assert target.model == before, "nothing changes until the next turn starts"
    assert target._pending_model_pick.model == out["model"]


def test_the_next_turn_commits_the_pick(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.jev_route = True
    gen = target._model_pick_gen
    out = _set_model(state, caller, "chat-2", "sonnet")

    changed = sc.apply_pending_model_pick(state, target)

    assert changed is True
    assert target.model == out["model"]
    assert target.jev_route is False
    assert target._model_pick_gen == gen + 1
    assert target._pending_model_pick is None
    assert sc.apply_pending_model_pick(state, target) is False, "a pick applies once"


def test_a_later_pick_replaces_an_unapplied_one(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _set_model(state, caller, "chat-2", "sonnet")
    second = _set_model(state, caller, "chat-2", "opus")

    sc.apply_pending_model_pick(state, target)

    assert target.model == second["model"]


def test_a_pick_is_dropped_if_the_target_became_channel_linked(tmp_path):
    """The turn-start commit re-runs the gate. Mutation guard: committing the
    stored pick without it lets the change reach a channel-backed session."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    target.linked_session_key = "slack:1786300000.000100"

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before
    assert target._pending_model_pick is None


def test_a_pick_is_dropped_if_the_target_became_mirrored(tmp_path):
    """An outbound mirror added while the pick waits makes the target unreachable."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    state.sessions.set_mirror_link(_key(target), "C0FFEE", "1786300000.000200")

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before
    assert target._pending_model_pick is None


def test_a_pick_is_dropped_if_the_caller_slot_was_reused(tmp_path):
    """A closed caller's key handed to a new occupant does not inherit the pick.
    Mutation guard: authorizing on the key alone would commit it."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    assert target._pending_model_pick.caller_tab_id == caller._tab_id
    # A new occupant under the same key carries a different tab identity.
    caller._tab_id = "replacement-tab"

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before


def test_a_pick_is_dropped_if_the_caller_is_gone(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    state._slots.pop("chat-1")

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before


def test_a_newer_picker_choice_wins_over_the_pending_pick(tmp_path):
    """Mutation guard: without the pick-generation check the stale pick would
    overwrite a model the user chose after it was queued."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    _set_model(state, caller, "chat-2", "sonnet")
    # The model picker's explicit pick: new model, bumped generation.
    target.model = "opus"
    target._model_pick_gen += 1

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == "opus"


def test_a_caller_fenced_after_queuing_is_rechecked(tmp_path, monkeypatch):
    """A False fence verdict stored at call time is re-derived at turn start."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    assert target._pending_model_pick.caller_fenced is False
    # Now fenced: the caller may reach only sessions it created, and it did not
    # create chat-2.
    monkeypatch.setattr(sc, "_caller_is_ownership_fenced", lambda _state, _key: True)

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before


@pytest.mark.parametrize("model", ["auto", "Auto", " auto "])
def test_auto_is_owner_only(tmp_path, model):
    """With the Jev preview on, a slot on ``auto`` routes each turn through Jev."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", model)

    assert exc.value.code == "model_owner_only"
    assert target._pending_model_pick is None


def test_an_empty_model_is_refused(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "  ")

    assert exc.value.code == "model_rejected"


def test_an_alias_is_folded_to_the_kiro_cli_id(tmp_path, monkeypatch):
    """``sonnet`` is not a kiro-cli id; stored as-is it would be withheld at
    session start and the target would stay on the default."""
    monkeypatch.setattr(sc.model_registry, "acp_id_correction", lambda m: f"wire-{m}")
    monkeypatch.setattr(sc, "is_claude_code", lambda _provider: False)
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    out = _set_model(state, caller, "chat-2", "sonnet")

    assert out["model"] == "wire-sonnet"
    assert target._pending_model_pick.model == "wire-sonnet"


def test_an_alias_is_left_alone_on_a_non_kiro_backend(tmp_path, monkeypatch):
    """Another backend's ids live in their own namespace; folding them to a
    kiro-cli id would name a model that harness does not serve."""
    from types import SimpleNamespace

    monkeypatch.setattr(sc.model_registry, "acp_id_correction", lambda m: f"wire-{m}")
    monkeypatch.setattr(sc, "is_claude_code", lambda _provider: False)
    monkeypatch.setattr(
        sc, "capabilities_for", lambda _backend: SimpleNamespace(model_id_namespace="claude_code")
    )
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")

    out = _set_model(state, caller, "chat-2", "claude-opus-4-8")

    assert out["model"] == "claude-opus-4-8"


def test_a_padded_model_is_stored_stripped(tmp_path, monkeypatch):
    monkeypatch.setattr(sc.model_registry, "acp_id_correction", lambda _m: None)
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    out = _set_model(state, caller, "chat-2", "  some-model ")

    assert out["model"] == "some-model"
    assert target._pending_model_pick.model == "some-model"


def test_a_pick_equal_to_the_pin_still_resets_while_a_fallback_serves(tmp_path):
    """With a throttle fallback on the wire, an unchanged pin still needs the
    reset, the same way the model picker treats it."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    out = _set_model(state, caller, "chat-2", "sonnet")
    target.model = out["model"]
    target._active_fallback_model = "some-fallback"

    assert sc.apply_pending_model_pick(state, target) is True


def test_the_turn_arms_the_reset_for_a_live_or_spawning_session_only():
    """Source pin: the reset is armed when a session is registered or an eager
    spawn is in flight, and not otherwise, so a cold start on the new model is
    not torn down at turn end."""
    import kiro_crew.dashboard.chat_runner as runner

    source = Path(runner.__file__).read_text(encoding="utf-8")
    body = source[source.index("if apply_pending_model_pick(state, slot):") :]
    body = body[: body.index("await _consume_pending_reset(state, slot)")]
    assert "_pending_reset_history_key" in body
    assert "get_provider(" in body
    assert "_eager_spawn_task" in body


def test_the_turn_start_commit_cannot_suspend():
    """The gate and the write must run with no await between them."""
    assert not inspect.iscoroutinefunction(sc.apply_pending_model_pick)


def test_the_turn_applies_the_pick_before_it_acquires_a_session():
    """Source-order pin: the pick is applied in ``_run_chat`` before the pending
    reset is consumed and before get_or_create reads ``slot.model``."""
    import kiro_crew.dashboard.chat_runner as runner

    source = Path(runner.__file__).read_text(encoding="utf-8")
    body = source[source.index("async def _run_chat(") :]
    apply_at = body.index("apply_pending_model_pick(state, slot)")
    assert apply_at < body.index("await _consume_pending_reset(state, slot)")
    assert apply_at < body.index("_requested_model = slot.model")


def test_a_busy_target_is_refused_and_gets_no_pick(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "sonnet")

    assert exc.value.code == "target_busy"
    assert exc.value.status == 409
    assert "session busy, model not changed" in exc.value.message
    assert target._pending_model_pick is None


def test_a_target_with_attached_subagents_is_refused(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    async def _children(*_a, **_kw):
        return chat_handlers.web.json_response(
            {"error": "sub-agents are running", "code": "slot_subagents_running"}, status=409
        )

    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", _children)

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "sonnet")

    assert exc.value.code == "target_busy"
    assert target._pending_model_pick is None


def test_a_turn_starting_during_the_idle_check_is_refused(tmp_path, monkeypatch):
    """The idle check re-runs after the sub-agent probe's await."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    async def _turn_starts_meanwhile(*_a, **_kw):
        _busy(target)
        return None

    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", _turn_starts_meanwhile)

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "sonnet")

    assert exc.value.code == "target_busy"
    assert target._pending_model_pick is None


def test_a_caller_fenced_at_call_time_stays_fenced(tmp_path, monkeypatch):
    """A True verdict stored with the pick is kept even when the fence can no
    longer be derived at turn start."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    before = target.model
    _set_model(state, caller, "chat-2", "sonnet")
    pick = target._pending_model_pick
    target._pending_model_pick = sc.PendingModelPick(
        model=pick.model,
        caller_session_key=pick.caller_session_key,
        caller_tab_id=pick.caller_tab_id,
        caller_fenced=True,
        pick_gen=pick.pick_gen,
    )
    monkeypatch.setattr(sc, "_caller_is_ownership_fenced", lambda _state, _key: False)

    assert sc.apply_pending_model_pick(state, target) is False
    assert target.model == before


def test_a_target_linked_during_the_idle_check_is_refused(tmp_path, monkeypatch):
    """The gate re-runs after the sub-agent probe's await, right before the pick
    is stored."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    async def _link_meanwhile(*_a, **_kw):
        target.linked_session_key = "slack:1786300000.000100"
        return None

    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", _link_meanwhile)

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "sonnet")

    assert exc.value.code == "linked_session_target"
    assert target._pending_model_pick is None


def test_auto_jev_is_owner_only(tmp_path):
    from kiro_crew.dashboard.chat_handlers import JEV_ROUTE_MODEL

    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", JEV_ROUTE_MODEL)

    assert exc.value.code == "model_owner_only"
    assert target._pending_model_pick is None


def test_an_out_of_bounds_target_is_refused(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-hidden", memory_mode="incognito")

    with pytest.raises(sc.SessionControlError):
        _set_model(state, caller, "chat-hidden", "sonnet")


def test_a_remote_crew_target_is_refused(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.executor = "remote"

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "sonnet")

    assert exc.value.code == "remote_target_unsupported"
    assert target._pending_model_pick is None


def test_a_credential_shaped_model_is_refused_before_it_is_stored(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")

    with pytest.raises(sc.SessionControlError) as exc:
        _set_model(state, caller, "chat-2", "ghp_" + "a" * 36)

    assert exc.value.code == "model_rejected"
    assert target._pending_model_pick is None


def test_model_rejection_check_receives_resolved_provider(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")
    seen: list[str | None] = []

    def _capture_provider(_model_name, provider=None):
        seen.append(provider)
        return None

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers._model_rejected_reason", _capture_provider
    )

    _set_model(state, caller, "chat-2", "sonnet")

    assert seen and seen[0] is not None


# ── Route ────────────────────────────────────────────────────────────────────


def _request(tmp_path, *, internal: bool, body: dict):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-2")
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/set-model"
    request.method = "POST"
    request.headers = {"X-Session-Key": _key(caller)}
    request.query = {}
    request.get = lambda key, default=None: (
        True if (key in ("internal_auth", "peer_verified") and internal) else default
    )

    async def _json():
        return body

    request.json = _json
    return request


def test_route_without_the_secret_is_forbidden(tmp_path):
    req = _request(tmp_path, internal=False, body={"target": "chat-2", "model": "sonnet"})
    resp = asyncio.run(handlers_sc.api_session_control_set_model(req))
    assert resp.status == 403


def test_route_refuses_a_non_string_model(tmp_path):
    req = _request(tmp_path, internal=True, body={"target": "chat-2", "model": 5})
    resp = asyncio.run(handlers_sc.api_session_control_set_model(req))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "bad_request"


def test_route_renders_busy_as_409(tmp_path, monkeypatch):
    req = _request(tmp_path, internal=True, body={"target": "chat-2", "model": "sonnet"})

    async def _busy_verb(*_a, **_kw):
        raise sc.SessionControlError("session busy", status=409, code="target_busy")

    monkeypatch.setattr(sc, "set_model_target", _busy_verb)
    resp = asyncio.run(handlers_sc.api_session_control_set_model(req))
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "target_busy"


# ── MCP tool ─────────────────────────────────────────────────────────────────

_VERIFIED = "dashboard:chat-verified"


def test_tool_carries_the_verified_key_and_reports_the_model():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post",
            return_value={"ok": True, "target": "chat-2", "model": "sonnet", "pending": True},
        ) as post,
    ):
        out = _call_tool_inner("session_set_model", {"target": "chat-2", "model": "sonnet"})
    assert post.call_args.args[0] == "/api/session-control/set-model"
    assert post.call_args.kwargs["session_key"] == _VERIFIED
    assert "`chat-2` will switch to `sonnet` when its next turn starts" in out


def test_a_read_shows_the_model_and_the_pending_pick(tmp_path):
    """A caller can see whether its pick is still pending or has taken."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.model = "old-model"
    out = _set_model(state, caller, "chat-2", "sonnet")

    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert read["model"] == "old-model"
    assert read["pending_model"] == out["model"]

    sc.apply_pending_model_pick(state, target)
    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert read["model"] == out["model"]
    assert "pending_model" not in read


def test_a_read_redacts_a_credential_shaped_model(tmp_path):
    """The owner's picker stores any string, so the read must not echo a secret."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    secret = "ghp_" + "a" * 36
    target.model = secret

    read = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert secret not in read["model"]


def test_the_read_tool_renders_the_model_and_the_pending_pick():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._get",
            return_value={
                "target": "chat-2",
                "title": "w",
                "messages": [],
                "total": 0,
                "next_since": 0,
                "model": "old-model",
                "pending_model": "new-model",
            },
        ),
    ):
        out = _call_tool_inner("session_read_message", {"target": "chat-2"})
    assert "model old-model" in out
    assert "pending model new-model for its next turn" in out


def test_tool_reports_a_busy_refusal_as_an_error():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch(
            "kiro_crew.mcp_dashboard._post",
            return_value={"error": "session busy, model not changed"},
        ),
    ):
        out = _call_tool_inner("session_set_model", {"target": "chat-2", "model": "sonnet"})
    assert out.startswith("Error:")
    assert "session busy, model not changed" in out
