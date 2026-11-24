"""``session_run_queued``: run a message the caller queued on a session it manages now.

Pinned here:

* the target gate is ``authorize_target`` plus ``session_queue``'s manager fence,
  so a session the caller does not manage is refused and a handoff moves the verb;
* only an entry the caller queued qualifies (``session_queue``'s own rule): a
  person's message and another session's are refused ``not_your_entry``;
* a running target is stopped cooperatively with the entry promoted and marked,
  the rest of the queue kept; a repeat while that stop is pending changes nothing;
* the turn's end drains exactly the chosen entry, even with sub-agents attached;
* a compaction declines the stop as the card's Run now does;
* an idle target dispatches the entry through the card's own dispatch;
* an open queue editor or a pending conversation reset holds the mark at the
  drain (system injections still pass) and refuses the idle dispatch
  ``queue_held``; an entry a person has since edited loses its mark;
* the route is a strict internal-secret path and the tool posts the verified key.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import _busy

from kiro_crew.dashboard import chat_handlers, chat_runner, queue_edit_hold
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_control_run_queued as scrq
from kiro_crew.dashboard import session_managers
from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND, slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.slot_queue_repository import RUN_NOW_CHOSEN_KEY, run_now_chosen_id
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext
from kiro_crew.validation import SESSION_RUN_QUEUED_SCHEMA, ValidationError, validate_tool_args

_VERIFIED = "dashboard:chat-verified"


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _sel():
    """The SEL is a process singleton; every line the verb writes lands here."""
    log = MagicMock()
    with (
        patch.object(chat_handlers, "sel", return_value=log),
        patch.object(scrq, "sel", return_value=log),
    ):
        yield log


def _setup(tmp_path):
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    state.get_or_create_slot("chat-3")
    target = state.get_or_create_slot("chat-2")
    target._created_by = "chat-1"
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state.subagents = None
    return state, caller, target


def _sent_meta(state, sender_key: str) -> dict:
    return {**sc.send_origin_meta(state, sender_key), sc.SESSION_SEND_QUEUED_META: True}


def _mine(state, target, text: str) -> str:
    return target.queue_append(text, meta=_sent_meta(state, "chat-1"))


def _peer(state, target, text: str) -> str:
    return target.queue_append(text, meta=_sent_meta(state, "chat-3"))


def _person(target, text: str) -> str:
    return target.queue_append(text, directive_user_origin=True)


def _call(state, caller, queue_id: str, target: str = "chat-2") -> dict:
    return asyncio.run(
        scrq.run_queued_target(
            state, caller_session_key=slot_history_key(caller), target=target, queue_id=queue_id
        )
    )


def _refused(state, caller, queue_id: str, target: str = "chat-2") -> str:
    with pytest.raises(sc.SessionControlError) as exc:
        _call(state, caller, queue_id, target)
    return exc.value.code


def _ids(target) -> list[str]:
    return [item["id"] for item in target._queue]


def _entry(target, queue_id: str) -> dict:
    return next(item for item in target._queue if item["id"] == queue_id)


def _hold(target, kind: str, monkeypatch) -> None:
    """Open an editor on a person's card (``edit``) or defer a discard (``teardown``)."""
    if kind == "edit":
        monkeypatch.setattr(queue_edit_hold, "_clock", lambda: 0.0)
        card = _person(target, "a card a person is editing")
        _entry(target, card)[queue_edit_hold.EDIT_HOLD_KEY] = {"tab-1": 100.0}
    else:
        target._pending_discard_conversation_key = "conv-1"
        target._pending_discard_from_wake = False


def _release(target, kind: str) -> None:
    if kind == "edit":
        for item in target._queue:
            queue_edit_hold.release_edit_hold(item)
    else:
        target._pending_discard_conversation_key = None


def _drain(state, target):
    """One turn-end drain; returns (started, the prompt the turn got or None)."""
    with (
        patch.object(chat_runner.KiroCrewConfig, "load") as load,
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())) as run,
    ):
        load.return_value.dashboard.merge_queued_messages = False
        started = asyncio.run(chat_runner._start_next_queued_turn(state, target))
    return started, (run.call_args.args[2] if run.call_args else None)


# ── the gates ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["person", "peer"])
def test_an_entry_the_caller_did_not_queue_is_refused(tmp_path, who):
    state, caller, target = _setup(tmp_path)
    _busy(target)
    qid = _person(target, "stop here") if who == "person" else _peer(state, target, "theirs")
    assert _refused(state, caller, qid) == "not_your_entry"
    state.sessions.stop_turn.assert_not_awaited()
    assert target._stop_state == "idle"
    assert run_now_chosen_id(target) is None


def test_a_session_the_caller_does_not_manage_is_refused(tmp_path):
    state, caller, target = _setup(tmp_path)
    target._created_by = "chat-3"
    _busy(target)
    qid = _mine(state, target, "mine, on someone else's worker")
    assert _refused(state, caller, qid) == "not_creator"
    state.sessions.stop_turn.assert_not_awaited()


def test_a_handoff_moves_the_verb_to_the_new_manager(tmp_path, monkeypatch):
    """Mutation guard: a creator fence (``_created_by``) would keep the giver's reach."""
    state, caller, target = _setup(tmp_path)
    _busy(target)
    qid = _mine(state, target, "queued before the handoff")
    monkeypatch.setattr(session_managers, "holder", lambda slot: "chat-3")
    monkeypatch.setattr(sc, "_caller_is_ownership_fenced", lambda *_a, **_k: True)
    assert _refused(state, caller, qid) == "not_creator"


def test_an_unknown_entry_is_not_found(tmp_path):
    state, caller, target = _setup(tmp_path)
    _busy(target)
    assert _refused(state, caller, "nope") == "entry_not_found"


def test_a_relay_archive_is_refused(tmp_path):
    state, caller, target = _setup(tmp_path)
    qid = _mine(state, target, "x")
    target.executor = "remote"
    assert _refused(state, caller, qid) == "relay_archive_read_only"


# ── running target ───────────────────────────────────────────────────────────


def test_a_running_target_is_stopped_with_the_entry_promoted_and_the_queue_kept(tmp_path):
    state, caller, target = _setup(tmp_path)
    _busy(target)
    a = _person(target, "a person's message")
    b = _peer(state, target, "another session's")
    c = _mine(state, target, "the correction")
    out = _call(state, caller, c)
    assert out == {
        "ok": True,
        "target": "chat-2",
        "queue_id": c,
        "outcome": "stopping",
        "stop": "soft",
    }
    kwargs = state.sessions.stop_turn.await_args.kwargs
    assert kwargs["force"] is False and kwargs["preserve_queue"] is True
    assert _ids(target) == [c, a, b]
    assert run_now_chosen_id(target) == c


def test_a_repeat_while_the_stop_is_pending_changes_nothing(tmp_path):
    state, caller, target = _setup(tmp_path)
    _busy(target)
    a = _mine(state, target, "first")
    b = _mine(state, target, "second")
    target._stop_state = "soft_pending"
    out = _call(state, caller, b)
    assert out["outcome"] == "noop"
    state.sessions.stop_turn.assert_not_awaited()
    assert _ids(target) == [a, b]
    assert run_now_chosen_id(target) is None


def test_a_compaction_declines_before_anything_moves(tmp_path):
    state, caller, target = _setup(tmp_path)
    _busy(target)
    a = _mine(state, target, "first")
    b = _mine(state, target, "second")
    with patch.object(chat_handlers, "_compaction_in_flight", return_value=True):
        out = _call(state, caller, b)
    assert out["outcome"] == "compacting"
    state.sessions.stop_turn.assert_not_awaited()
    assert _ids(target) == [a, b]
    assert target._stop_state == "idle"


def test_a_compaction_after_the_probe_leaves_the_entry_at_the_front_unmarked(tmp_path):
    """Decision F1: the card's Run now answers the same way."""
    state, caller, target = _setup(tmp_path)
    _busy(target)
    a = _mine(state, target, "first")
    b = _mine(state, target, "second")
    state.sessions.stop_turn = AsyncMock(return_value="compacting")
    out = _call(state, caller, b)
    assert out["outcome"] == "compacting"
    assert _ids(target) == [b, a]
    assert run_now_chosen_id(target) is None
    assert target._stop_state == "idle"


def test_a_later_choice_replaces_an_earlier_mark(tmp_path):
    state, caller, target = _setup(tmp_path)
    _busy(target)
    a = _mine(state, target, "first")
    b = _mine(state, target, "second")
    state.sessions.stop_turn = AsyncMock(return_value="idle")
    assert _call(state, caller, a)["outcome"] == "idle"
    target._stop_state = "idle"
    _call(state, caller, b)
    assert [item.get(RUN_NOW_CHOSEN_KEY, False) for item in target._queue] == [True, False]
    assert _ids(target) == [b, a]


def test_the_turn_end_runs_exactly_the_chosen_entry_while_subagents_are_out(tmp_path):
    """The chosen entry runs next at the turn's end, past the child hold.

    Mutation guard: without the mark, the hold dequeues the sub-agent completion
    first and the caller's entry waits for the wave; a mark dropped before the
    drain lets the head of the queue run instead.
    """
    state, caller, target = _setup(tmp_path)
    task = _busy(target).task
    state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["child-1"]))
    p = _peer(state, target, "another session's")
    s = target.queue_append("[Subagent completion event] done", kind=SUBAGENT_COMPLETION_KIND)
    c = _mine(state, target, "the correction")
    assert _call(state, caller, c)["outcome"] == "stopping"

    # The cancelled turn ends: its tail drains as ``_hand_off_queue`` does.
    task.done.return_value = True
    target._stop_state = "idle"
    with (
        patch.object(chat_runner.KiroCrewConfig, "load") as load,
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
        patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())) as run,
    ):
        load.return_value.dashboard.merge_queued_messages = True
        assert asyncio.run(chat_runner._start_next_queued_turn(state, target)) is True

    assert spawn.call_count == 1
    assert "the correction" in run.call_args.args[2]
    assert _ids(target) == [p, s]
    assert run_now_chosen_id(target) is None


def test_the_card_route_never_marks_an_entry(tmp_path):
    """The person's Run now keeps its own drain: only the verb writes the mark."""
    from aiohttp.test_utils import make_mocked_request

    state, _caller, target = _setup(tmp_path)
    _busy(target)
    a = target.queue_append("a")
    b = target.queue_append("b")
    request = make_mocked_request("POST", "/api/chat/slots/chat-2/interrupt", app={"state": state})
    request.match_info.update({"slot": "chat-2"})
    with patch.object(
        chat_handlers, "read_bounded_json", new=AsyncMock(return_value=({"queue_id": b}, None))
    ):
        resp = asyncio.run(chat_handlers.api_chat_slot_interrupt(request))
    assert json.loads(resp.body.decode()) == {"ok": True, "outcome": "soft"}
    assert _ids(target) == [b, a]
    assert run_now_chosen_id(target) is None


# ── idle target ──────────────────────────────────────────────────────────────


def test_an_idle_target_dispatches_the_entry_through_the_card_dispatch(tmp_path):
    state, caller, target = _setup(tmp_path)
    _mine(state, target, "first")
    b = _mine(state, target, "second")
    with patch.object(
        chat_handlers, "_start_next_queued_turn", new=AsyncMock(return_value=True)
    ) as start:
        out = _call(state, caller, b)
    assert out["outcome"] == "started"
    start.assert_awaited_once_with(
        state, target, allow_user_during_subagents=True, required_queue_id=b
    )
    state.sessions.stop_turn.assert_not_awaited()


def test_an_idle_entry_cancelled_under_the_lock_is_refused(tmp_path):
    """The gate re-runs after the lock await, so a vanished entry starts nothing."""
    state, caller, target = _setup(tmp_path)
    b = _mine(state, target, "second")

    class _Lock:
        async def __aenter__(self):
            target.queue_remove_by_id(b)

        async def __aexit__(self, *exc):
            return False

    target._lock = _Lock()
    with patch.object(
        chat_handlers, "_start_next_queued_turn", new=AsyncMock(return_value=True)
    ) as start:
        assert _refused(state, caller, b) == "entry_not_found"
    start.assert_not_awaited()


def test_an_idle_dispatch_that_starts_nothing_is_refused(tmp_path):
    state, caller, target = _setup(tmp_path)
    b = _mine(state, target, "second")
    with patch.object(chat_handlers, "_start_next_queued_turn", new=AsyncMock(return_value=False)):
        assert _refused(state, caller, b) == "queue_item_unavailable"


# ── schema, route and tool ───────────────────────────────────────────────────


def test_the_schema_requires_a_bounded_queue_id():
    for bad in ({"target": "chat-2"}, {"target": "chat-2", "queue_id": "x" * 65}):
        with pytest.raises(ValidationError):
            validate_tool_args(bad, SESSION_RUN_QUEUED_SCHEMA)


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
    assert asyncio.run(handlers_sc.api_session_control_run_queued(req)).status == 403


def test_the_route_serves_an_internal_caller_and_maps_refusals(tmp_path):
    state, _caller, target = _setup(tmp_path)
    _busy(target)
    p = _person(target, "a person's")
    c = _mine(state, target, "mine")
    ok = asyncio.run(
        handlers_sc.api_session_control_run_queued(
            _request(state, {"target": "chat-2", "queue_id": c}, internal=True)
        )
    )
    assert ok.status == 200
    assert json.loads(ok.body.decode())["outcome"] == "stopping"
    target._stop_state = "idle"
    refused = asyncio.run(
        handlers_sc.api_session_control_run_queued(
            _request(state, {"target": "chat-2", "queue_id": p}, internal=True)
        )
    )
    assert refused.status == 403
    assert json.loads(refused.body.decode())["code"] == "not_your_entry"
    missing = asyncio.run(
        handlers_sc.api_session_control_run_queued(
            _request(state, {"target": "chat-2"}, internal=True)
        )
    )
    assert missing.status == 400


def test_the_route_is_a_strict_internal_path():
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/session-control/run-queued" in _STRICT_INTERNAL_API_PATHS


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ({"outcome": "stopping", "stop": "soft"}, "Stopped the running turn"),
        ({"outcome": "started"}, "Nothing was stopped."),
        ({"outcome": "noop", "info": "stop already in progress"}, "nothing changed"),
        ({"outcome": "compacting"}, "keeps its place in the queue"),
        ({"outcome": "compacting", "stop": "compacting"}, "is at the front of the queue"),
    ],
)
def test_the_tool_posts_the_verified_key_and_says_what_happened(body, said):
    dash = InMemoryDashboardClient(
        {"POST /api/session-control/run-queued": {"target": "chat-2", "queue_id": "q1", **body}}
    )
    out = TABLE.call(
        "session_run_queued",
        {"target": "chat-2", "queue_id": "q1"},
        ToolContext(dash, Caller.strict(_VERIFIED)),
    )
    (post,) = dash.requests
    assert post.path == "/api/session-control/run-queued"
    assert post.body == {"target": "chat-2", "queue_id": "q1"}
    assert post.session_key == _VERIFIED
    assert said in out


def test_the_tool_reports_a_refusal_as_an_error():
    dash = InMemoryDashboardClient({"POST /api/session-control/run-queued": {"error": "not yours"}})
    out = TABLE.call(
        "session_run_queued",
        {"target": "chat-2", "queue_id": "q"},
        ToolContext(dash, Caller.strict(_VERIFIED)),
    )
    assert out == "Error: could not run that queued message: not yours"


def test_a_person_run_now_after_the_verb_outranks_its_mark(tmp_path):
    """Mutation guard: a kept mark made the turn's end run the agent's entry instead."""
    from aiohttp.test_utils import make_mocked_request

    state, caller, target = _setup(tmp_path)
    _busy(target)
    mine = _mine(state, target, "the agent's choice")
    card = _person(target, "the person's choice")
    state.sessions.stop_turn = AsyncMock(return_value="idle")
    _call(state, caller, mine)
    assert run_now_chosen_id(target) == mine
    target._stop_state = "idle"
    request = make_mocked_request("POST", "/api/chat/slots/chat-2/interrupt", app={"state": state})
    request.match_info.update({"slot": "chat-2"})
    with patch.object(
        chat_handlers, "read_bounded_json", new=AsyncMock(return_value=({"queue_id": card}, None))
    ):
        asyncio.run(chat_handlers.api_chat_slot_interrupt(request))
    assert _ids(target) == [card, mine]
    assert run_now_chosen_id(target) is None


# ── holds ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["edit", "teardown"])
def test_a_held_queue_keeps_the_mark_and_still_passes_a_completion(tmp_path, monkeypatch, kind):
    """Mutation guard: a mark read under the hold parked the completion behind it."""
    state, caller, target = _setup(tmp_path)
    task = _busy(target).task
    state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["child-1"]))
    s = target.queue_append("[Subagent completion event] done", kind=SUBAGENT_COMPLETION_KIND)
    c = _mine(state, target, "the correction")
    assert _call(state, caller, c)["outcome"] == "stopping"
    _hold(target, kind, monkeypatch)
    task.done.return_value = True
    target._stop_state = "idle"

    started, prompt = _drain(state, target)
    assert started is True
    assert "[Subagent completion event] done" in prompt
    assert "the correction" not in prompt
    assert s not in _ids(target)
    assert run_now_chosen_id(target) == c

    _release(target, kind)
    started, prompt = _drain(state, target)
    assert started is True
    assert "the correction" in prompt
    assert run_now_chosen_id(target) is None


@pytest.mark.parametrize("kind", ["edit", "teardown"])
def test_an_idle_target_under_a_hold_is_refused_and_keeps_the_entry(tmp_path, monkeypatch, kind):
    """Mutation guard: the card's own dispatch passes both holds for a person's pick."""
    state, caller, target = _setup(tmp_path)
    b = _mine(state, target, "the correction")
    _hold(target, kind, monkeypatch)
    with patch.object(
        chat_handlers, "_start_next_queued_turn", new=AsyncMock(return_value=True)
    ) as start:
        assert _refused(state, caller, b) == "queue_held"
    start.assert_not_awaited()
    assert b in _ids(target)


def test_an_entry_a_person_edited_loses_its_mark(tmp_path):
    """Mutation guard: the mark ran the person's re-stamped words past the child hold."""
    state, caller, target = _setup(tmp_path)
    _busy(target)
    c = _mine(state, target, "the correction")
    state.sessions.stop_turn = AsyncMock(return_value="idle")
    _call(state, caller, c)
    assert run_now_chosen_id(target) == c
    target.queue_edit_by_id(c, "the person's rewrite", directive_user_origin=True)
    assert _entry(target, c)[RUN_NOW_CHOSEN_KEY] is True
    assert run_now_chosen_id(target) is None
