"""``session_run_queued``: run one of the caller's own queued messages now.

Pinned here:

* the target gate is ``authorize_target`` plus a hard creator fence, so a
  session the caller did not create is refused whatever class of caller it is;
* only an entry the caller queued with ``session_send`` runs, judged by the
  sender stamp (slot key AND tab identity), so a person's typed message and
  another session's delivery are never promoted;
* a running target is stopped cooperatively with the entry promoted and the rest
  of the queue kept; a repeat while that stop is pending is a no-op;
* an idle target dispatches the entry through the Run-now path, and the entry is
  re-checked after the lock await;
* the route is a strict internal-secret path and the tool posts the verified key.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import _call_tool_inner
from kiro_crew.validation import SESSION_RUN_QUEUED_SCHEMA, ValidationError, validate_tool_args

_VERIFIED = "dashboard:chat-verified"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture
def started(monkeypatch):
    """The idle dispatch, recorded instead of starting a real turn."""
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_handlers, "_start_next_queued_turn", start)
    return start


def _setup(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-3")
    target = state.get_or_create_slot("chat-2")
    target._created_by = "chat-1"
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    return state, caller, target


def _running(target) -> None:
    task = MagicMock()
    task.done.return_value = False
    target.task = task


def _mine(state, target, text: str) -> str:
    return target.queue_append(text, meta=sc.send_origin_meta(state, "chat-1"))


def _peer(state, target, text: str) -> str:
    return target.queue_append(text, meta=sc.send_origin_meta(state, "chat-3"))


def _person(target, text: str) -> str:
    return target.queue_append(text, directive_user_origin=True)


def _call(state, caller, queue_id: str, **kwargs) -> dict:
    kwargs.setdefault("target", "chat-2")
    return asyncio.run(
        sc.run_queued_target(
            state, caller_session_key=slot_history_key(caller), queue_id=queue_id, **kwargs
        )
    )


def _ids(target) -> list[str]:
    return [item["id"] for item in target._queue]


# ── running target ───────────────────────────────────────────────────────────


def test_a_running_target_is_stopped_with_your_entry_promoted(tmp_path):
    state, caller, target = _setup(tmp_path)
    _running(target)
    first = _person(target, "the person's next message")
    mine = _mine(state, target, "correction")
    out = _call(state, caller, mine)
    assert out["outcome"] == "stopping"
    assert out["queue_id"] == mine
    # Promoted to the front, nothing dropped.
    assert _ids(target) == [mine, first]
    state.sessions.stop_turn.assert_awaited_once()
    assert state.sessions.stop_turn.await_args.kwargs["preserve_queue"] is True
    assert state.sessions.stop_turn.await_args.kwargs["force"] is False
    assert target._stop_state == "soft_pending"
    assert target._run_now_queue_id == mine
    # The stop card the person reading the target sees.
    assert target._stop_event_id
    assert any(target._stop_event_id in str(m.get("content")) for m in target.messages)


def test_an_orchestrating_running_target_is_refused(tmp_path):
    """A multi-stage plan owns what runs next, so nothing may be claimed.

    Mutation guard: dropping the orchestrating refusal claims the stop and
    promotes the entry into a plan's queue.
    """
    state, caller, target = _setup(tmp_path)
    _running(target)
    target._in_stage_execution = True
    first = _person(target, "the person's next message")
    mine = _mine(state, target, "correction")
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, mine)
    assert exc.value.code == "slot_orchestrating"
    # Nothing mutated: queue order, stop posture and the provider call.
    assert _ids(target) == [first, mine]
    assert target._stop_state == "idle"
    assert target._stop_event_id is None
    state.sessions.stop_turn.assert_not_awaited()


def test_a_provider_reporting_no_active_turn_is_not_a_stop(tmp_path):
    """``stop_turn`` returning ``idle`` cancelled nothing, so neither did we.

    Mutation guard: hardcoding ``stopping`` claims a stop and a start that did
    not happen.
    """
    state, caller, target = _setup(tmp_path)
    _running(target)
    state.sessions.stop_turn = AsyncMock(return_value="idle")
    mine = _mine(state, target, "correction")
    out = _call(state, caller, mine)
    assert out["outcome"] == "idle"
    assert out["stop"] == "idle"
    assert target._run_now_queue_id == ""
    # Promoted either way, so a second call runs it.
    assert _ids(target) == [mine]


def test_a_compacting_running_target_is_declined_untouched(tmp_path, monkeypatch):
    """An automatic compaction holds the session: no claim, no promotion, no stop.

    Mutation guard: dropping the compaction probe claims the stop and rejects
    the turn's pending waits for a Stop the compaction then declines.
    """
    state, caller, target = _setup(tmp_path)
    _running(target)
    first = _person(target, "the person's next message")
    mine = _mine(state, target, "correction")
    monkeypatch.setattr(chat_handlers, "_compaction_in_flight", lambda _s, _k: True)
    out = _call(state, caller, mine)
    assert out["outcome"] == "compacting"
    assert _ids(target) == [first, mine]
    assert target._run_now_queue_id == ""
    assert target._stop_state == "idle"
    state.sessions.stop_turn.assert_not_awaited()


def test_a_compaction_committing_during_the_stop_is_reported(tmp_path):
    state, caller, target = _setup(tmp_path)
    _running(target)
    mine = _mine(state, target, "correction")
    state.sessions.stop_turn = AsyncMock(return_value="compacting")
    out = _call(state, caller, mine)
    assert out["outcome"] == "compacting"
    assert target._run_now_queue_id == ""
    assert target._stop_state == "idle"


def test_a_repeat_while_the_stop_is_pending_is_a_noop(tmp_path):
    """A retried call must not escalate or re-promote.

    Mutation guard: dropping the pending-stop branch opens a second card and
    re-runs stop_turn on the in-flight cancel.
    """
    state, caller, target = _setup(tmp_path)
    _running(target)
    a = _mine(state, target, "a")
    b = _mine(state, target, "b")
    target._stop_state = "soft_pending"
    out = _call(state, caller, b)
    assert out["outcome"] == "noop"
    assert out["info"] == "stop already in progress"
    assert _ids(target) == [a, b]
    state.sessions.stop_turn.assert_not_awaited()


@pytest.mark.parametrize("entry_present", [True, False])
def test_the_run_now_binding_is_taken_once(tmp_path, entry_present):
    _state, _caller, target = _setup(tmp_path)
    queue_id = target.queue_append("selected")
    target._run_now_queue_id = queue_id
    if not entry_present:
        target.queue_remove_by_id(queue_id)

    assert chat_runner._take_run_now_queue_id(target) == (queue_id if entry_present else None)
    assert target._run_now_queue_id == ""
    assert chat_runner._take_run_now_queue_id(target) is None


def test_the_cancelled_turn_tail_binds_its_successor_drain():
    source = inspect.getsource(chat_runner._run_chat)
    assert "run_now_queue_id = _take_run_now_queue_id(slot)" in source
    assert "required_queue_id=run_now_queue_id" in source


# ── idle target ──────────────────────────────────────────────────────────────


def test_an_idle_target_dispatches_your_entry(tmp_path, started):
    state, caller, target = _setup(tmp_path)
    mine = _mine(state, target, "go")
    out = _call(state, caller, mine)
    assert out["outcome"] == "started"
    started.assert_awaited_once_with(
        state, target, allow_user_during_subagents=True, required_queue_id=mine
    )
    state.sessions.stop_turn.assert_not_awaited()


def test_an_idle_dispatch_that_finds_the_entry_gone_is_refused(tmp_path, started):
    state, caller, target = _setup(tmp_path)
    mine = _mine(state, target, "go")
    started.return_value = False
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, mine)
    assert exc.value.code == "queue_item_unavailable"
    assert exc.value.status == 409


def test_an_idle_target_mid_stop_is_refused(tmp_path, started):
    state, caller, target = _setup(tmp_path)
    mine = _mine(state, target, "go")
    target._stop_state = "soft_pending"
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, mine)
    assert exc.value.code == "slot_stopping"
    started.assert_not_awaited()


def test_the_entry_is_rechecked_after_the_lock_await(tmp_path, started, monkeypatch):
    """A stamp that stops matching while the call waited stops the dispatch.

    Mutation guard: dropping the recheck call runs an entry the gate would now
    refuse.
    """
    state, caller, target = _setup(tmp_path)
    mine = _mine(state, target, "go")
    real = chat_handlers._idle_run_now_refusal

    def _swap_stamp(slot):
        slot._queue[0]["meta"] = sc.send_origin_meta(state, "chat-3")
        return real(slot)

    monkeypatch.setattr(chat_handlers, "_idle_run_now_refusal", _swap_stamp)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, mine)
    assert exc.value.code == "not_your_entry"
    started.assert_not_awaited()


# ── which entries qualify ────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["person", "peer"])
@pytest.mark.parametrize("running", [True, False])
def test_an_entry_you_did_not_queue_is_refused(tmp_path, started, who, running):
    """Mutation guard: skipping the ownership check runs someone else's message."""
    state, caller, target = _setup(tmp_path)
    if running:
        _running(target)
    mine = _mine(state, target, "mine")
    other = _person(target, "theirs") if who == "person" else _peer(state, target, "theirs")
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, other)
    assert exc.value.code == "not_your_entry"
    assert _ids(target) == [mine, other]
    state.sessions.stop_turn.assert_not_awaited()
    started.assert_not_awaited()


def test_a_stamp_from_a_previous_occupant_of_the_key_is_not_yours(tmp_path):
    """Mutation guard: comparing only the slot key reads this entry as yours."""
    state, caller, target = _setup(tmp_path)
    qid = target.queue_append(
        "old chat-1", meta={sc.SEND_ORIGIN_META_KEY: {"slot": "chat-1", "tab": "gone"}}
    )
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, qid)
    assert exc.value.code == "not_your_entry"


def test_a_person_entry_never_reads_as_yours_even_with_a_stamp(tmp_path):
    state, caller, target = _setup(tmp_path)
    qid = target.queue_append(
        "typed", meta=sc.send_origin_meta(state, "chat-1"), directive_user_origin=True
    )
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, qid)
    assert exc.value.code == "not_your_entry"


@pytest.mark.parametrize("shape", ["kind", "payload", "on_consumed", "on_irreversibly_consumed"])
def test_a_stamped_producer_entry_is_not_yours(tmp_path, shape):
    """Mutation guard: dropping the plain-entry check runs a callback-bound entry early."""
    state, caller, target = _setup(tmp_path)
    qid = _mine(state, target, "retry of my message")
    entry = target._queue[0]
    if shape in ("kind", "payload"):
        entry[shape] = "recovery"
    else:
        entry[f"_{shape}"] = lambda *_a: None
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, qid)
    assert exc.value.code == "not_your_entry"


def test_an_unknown_entry_is_not_found(tmp_path):
    state, caller, _target = _setup(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, "nope")
    assert exc.value.code == "entry_not_found"
    assert exc.value.status == 404


# ── the target gate ──────────────────────────────────────────────────────────


def test_the_gate_is_authorize_target(tmp_path, started, monkeypatch):
    state, caller, target = _setup(tmp_path)
    mine = _mine(state, target, "go")
    seen: list[dict] = []
    real = sc.authorize_target

    def _spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(sc, "authorize_target", _spy)
    _call(state, caller, mine)
    # The gate, then the idle path's synchronous re-check.
    assert [kw["operation"] for kw in seen] == ["run_queued", "run_queued"]
    assert seen[1]["skip_enabled_check"] is True


def test_a_session_the_caller_did_not_create_is_refused(tmp_path):
    """Even for an unfenced owner caller that ``authorize_target`` would admit.

    Mutation guard: dropping the creator check promotes into the person's tab.
    """
    state, caller, target = _setup(tmp_path)
    mine = _mine(state, target, "go")
    target._created_by = ""
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, mine)
    assert exc.value.code == "not_creator"


def test_a_remote_target_is_refused(tmp_path):
    state, caller, target = _setup(tmp_path)
    mine = _mine(state, target, "go")
    target.executor = "remote"
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, mine)
    assert exc.value.code == "remote_target_unsupported"


def test_a_session_cannot_run_its_own_queue(tmp_path):
    state, caller, _target = _setup(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, "q", target="chat-1")
    assert exc.value.code == "self_target"


# ── schema ───────────────────────────────────────────────────────────────────


def test_the_schema_requires_both_arguments():
    ok = validate_tool_args({"target": "chat-2", "queue_id": "abc123"}, SESSION_RUN_QUEUED_SCHEMA)
    assert ok["queue_id"] == "abc123"
    for bad in ({"target": "chat-2"}, {"target": "chat-2", "queue_id": "has space"}):
        with pytest.raises(ValidationError):
            validate_tool_args(bad, SESSION_RUN_QUEUED_SCHEMA)


# ── the route ────────────────────────────────────────────────────────────────


def _request(state, body: dict, *, internal: bool):
    caller = state.get_or_create_slot("chat-1")
    request = MagicMock()
    request.app = {"state": state}
    request.path = "/api/session-control/run-queued"
    request.method = "POST"
    request.headers = {"X-Session-Key": slot_history_key(caller)}

    async def _json():
        return body

    request.json = _json
    request.get = lambda key, default=None: (
        True if (key in ("internal_auth", "peer_verified") and internal) else default
    )
    return request


def test_the_route_refuses_a_cookie_only_caller(tmp_path):
    state, _caller, _target = _setup(tmp_path)
    req = _request(state, {"target": "chat-2", "queue_id": "q"}, internal=False)
    resp = asyncio.run(handlers_sc.api_session_control_run_queued(req))
    assert resp.status == 403


def test_the_route_serves_an_internal_caller(tmp_path, started):
    state, _caller, target = _setup(tmp_path)
    mine = _mine(state, target, "go")
    req = _request(state, {"target": "chat-2", "queue_id": mine}, internal=True)
    resp = asyncio.run(handlers_sc.api_session_control_run_queued(req))
    assert resp.status == 200
    assert json.loads(resp.body.decode())["outcome"] == "started"


def test_the_route_requires_a_queue_id(tmp_path):
    state, _caller, _target = _setup(tmp_path)
    req = _request(state, {"target": "chat-2"}, internal=True)
    resp = asyncio.run(handlers_sc.api_session_control_run_queued(req))
    assert resp.status == 400


def test_the_route_maps_a_refusal(tmp_path):
    state, _caller, target = _setup(tmp_path)
    p = _person(target, "person")
    req = _request(state, {"target": "chat-2", "queue_id": p}, internal=True)
    resp = asyncio.run(handlers_sc.api_session_control_run_queued(req))
    assert resp.status == 403
    assert json.loads(resp.body.decode())["code"] == "not_your_entry"


def test_the_route_is_a_strict_internal_path():
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-control/run-queued" in _STRICT_INTERNAL_API_PATHS


# ── the MCP tool ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("outcome", "needle"),
    [
        ("stopping", "Stopped the running turn"),
        ("idle", "has NOT started"),
        ("started", "Nothing was stopped."),
        ("noop", "nothing changed"),
        ("compacting", "is compacting its context"),
    ],
)
def test_the_tool_posts_the_verified_key_and_says_what_happened(outcome, needle):
    body = {"ok": True, "target": "chat-2", "queue_id": "q1", "outcome": outcome}
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value=body) as post,
    ):
        out = _call_tool_inner("session_run_queued", {"target": "chat-2", "queue_id": "q1"})
    (path, sent), kwargs = post.call_args
    assert path == "/api/session-control/run-queued"
    assert sent == {"target": "chat-2", "queue_id": "q1"}
    assert kwargs["session_key"] == _VERIFIED
    assert needle in out


def test_the_idle_outcome_text_claims_neither_a_stop_nor_a_start():
    """Mutation guard: falling through to the stopping text claims both."""
    body = {"ok": True, "target": "chat-2", "queue_id": "q1", "outcome": "idle"}
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value=body),
    ):
        out = _call_tool_inner("session_run_queued", {"target": "chat-2", "queue_id": "q1"})
    assert "no active turn" in out
    assert "nothing was stopped" in out.lower()
    assert "has NOT started" in out
    assert "runs next" not in out


def test_the_tool_refuses_an_unverifiable_caller():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
        patch("kiro_crew.mcp_dashboard._post") as post,
    ):
        out = _call_tool_inner("session_run_queued", {"target": "chat-2", "queue_id": "q1"})
    assert out.startswith("Error")
    post.assert_not_called()


def test_the_tool_reports_a_refusal_as_an_error():
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._post", return_value={"error": "not yours"}),
    ):
        out = _call_tool_inner("session_run_queued", {"target": "chat-2", "queue_id": "q1"})
    assert out == "Error: could not run that queued message: not yours"
