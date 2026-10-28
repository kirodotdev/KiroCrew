"""A returned sub-agent result is held only under the attempt that spawned it.

The spawn record (``POST /api/spawn`` names the attempt holding the caller's session
when the request arrived, with its prompt in flight), the call anchor (one ``call_id``
per ``spawn_sub_agents`` call), a queued member's carried attempt (and the member that
ends before it starts, which carries none) and the done read's stamp. Split from
``test_spawn_sub_agents_settle_at_release.py``, whose helpers these tests share.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_spawn_sub_agents_duplicate_never_loss import (
    _cron_orchestrator,
    _late_mark,
    _new_sessions,
    _prompt_in_flight,
    _returned_child,
    _run_as_far_as_it_goes,
    _spawn_from_the_parents_tool,
)
from test_spawn_sub_agents_settle_at_release import _hold_until, _settled
from test_subagent_injection_live_turn import _HANG_CEILING_S


async def _read_done(state: Any, run: Any, key: str) -> None:
    """The parent's own ``spawn_sub_agents`` call reads ``run`` done
    (``GET /api/spawn/{id}``): internal auth, its own session."""
    from test_subagent_queued_visibility import _Req

    from kiro_crew.dashboard.messaging_api import run_views

    req = _Req(state, run.id)
    req["internal_auth"] = True
    req.headers["X-Session-Key"] = key
    assert (await run_views.api_spawn_status(req)).status == 200


def _spawn_state(tmp_path: Any, key: str, *children: Any) -> tuple[Any, Any]:
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    sessions = _new_sessions()
    state.sessions = sessions
    records = {child.id: child for child in children}
    state.subagents = MagicMock(get=MagicMock(side_effect=records.get), max_concurrent=4)
    return state, sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("after", ["another_task", "the_same_tasks_next_claim", "no_turn"])
async def test_a_read_after_the_spawning_attempt_ended_holds_nothing(
    tmp_path: Any, after: str
) -> None:
    """The child was spawned under T. T ends with no landing; the call reads on and
    reads the child done and marks it under whatever holds the permit next (another
    task's turn, the same task's next claim) or while nothing holds it. No such read
    stamps the child and no such mark is held, so the completion is delivered."""
    from kiro_crew.slack.gateway_runtime.subagent_injection import INLINE_DELIVER, _inline_verdict

    key = "cron:j1:r1"
    child = _returned_child("c1", key)
    state, sessions = _spawn_state(tmp_path, key, child)
    await sessions.get_or_create(key)  # T
    _prompt_in_flight(sessions, key)
    await _spawn_from_the_parents_tool(state, key, child)
    assert child._spawned_under == sessions.turn_token(key) is not None
    sessions.release(key)  # T is cut: no landing note
    # The successor (if any) has its own prompt in flight: only the spawn record
    # tells it from the spawning turn.

    holds, ends = asyncio.Event(), asyncio.Event()
    holder: asyncio.Task[None] | None = None
    if after == "another_task":
        holder = asyncio.create_task(_hold_until(sessions, key, holds, ends))
        await asyncio.wait_for(holds.wait(), _HANG_CEILING_S)
    elif after == "the_same_tasks_next_claim":
        await sessions.get_or_create(key)
    await _read_done(state, child, key)
    flagged = _late_mark(sessions, state.subagents, key, child.id)

    assert child._seen_done_under is None, "a read after the spawning attempt ended named one"
    assert flagged == set(), "a mark after the spawning attempt ended was held"
    assert child._collected_inline is False
    assert _inline_verdict(state.subagents, sessions, (child.id,))[0] == INLINE_DELIVER
    if holder is not None:
        ends.set()
        await asyncio.wait_for(holder, _HANG_CEILING_S)
    elif after == "the_same_tasks_next_claim":
        sessions.release(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("spawn", ["no_turn_held", "not_internal"])
async def test_a_spawn_with_no_holding_of_its_own_records_nothing_and_holds_nothing(
    tmp_path: Any, spawn: str
) -> None:
    """A child spawned while no turn holds the parent's permit, or without internal
    auth, records no spawning attempt. Read done and marked under a live turn of the
    parent later, it holds nothing: the completion is delivered, never lost. (A
    spawn whose caller is not the parent session it names is refused before any
    child exists: ``internal_memory_scope``'s 409.)"""
    from kiro_crew.slack.gateway_runtime.subagent_injection import INLINE_DELIVER, _inline_verdict

    key = "cron:j1:r1"
    child = _returned_child("c1", key)
    state, sessions = _spawn_state(tmp_path, key, child)
    if spawn == "no_turn_held":
        await _spawn_from_the_parents_tool(state, key, child)
        await sessions.get_or_create(key)
    else:
        await sessions.get_or_create(key)
        await _spawn_from_the_parents_tool(state, key, child, internal=False)
    assert child._spawned_under is None
    await _read_done(state, child, key)
    flagged = _late_mark(sessions, state.subagents, key, child.id)
    sessions.note_turn_landed(key)
    sessions.release(key)

    assert child._seen_done_under is None
    assert flagged == set()
    assert child._collected_inline is False
    assert _inline_verdict(state.subagents, sessions, (child.id,))[0] == INLINE_DELIVER


@pytest.mark.asyncio
async def test_a_child_spawned_read_and_marked_under_one_landed_turn_is_confirmed(
    tmp_path: Any,
) -> None:
    """The ordinary sequence over the real cron route: the turn's own call spawns the
    child, reads it done and marks it, and the turn lands. The mark is held under
    that turn and confirmed, so the completion takes no second turn."""
    from chat_test_helpers import _make_state

    key = "cron:j1:r1"
    child = _returned_child("c1", key)
    sessions = _new_sessions()
    orch = _cron_orchestrator(sessions, [child])
    orch.ctx_builder.build_message = MagicMock(
        side_effect=lambda announce, *_a, **_k: (announce, None)
    )
    state = _make_state(tmp_path)
    state.sessions = sessions
    state.subagents = orch.subagent_mgr
    turns: list[str] = []

    async def stream(_client: Any, message: str, **_kw: Any) -> str:
        turns.append(message)
        return "answered"

    with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
        await sessions.get_or_create(key)
        _prompt_in_flight(sessions, key)
        await _spawn_from_the_parents_tool(state, key, child)
        holding = sessions.turn_token(key)
        await _read_done(state, child, key)
        flagged = _late_mark(sessions, orch.subagent_mgr, key, child.id)

        assert child._spawned_under == child._seen_done_under == holding is not None
        assert flagged == {child.id}
        assert child._inline_state == "pending" and child._inline_token == holding
        sessions.note_turn_landed(key)
        sessions.release(key)
        route = asyncio.create_task(
            orch._finish_subagent_cron_or_notify(
                child, announce="c1 done", body="b", title="t", carried=(child.id,), parent_key=key
            )
        )
        await asyncio.wait_for(route, _HANG_CEILING_S)
        await _settled(orch)

    assert child._inline_state == "confirmed"
    assert turns == [], f"a confirmed child took a second turn: {turns!r}"
    assert orch._inline_parked == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["parent_internal", "another_session", "not_internal"])
async def test_only_the_parents_own_internal_spawn_names_the_holding_attempt(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    """The real ``POST /api/spawn`` while a turn holds the parent's permit. The
    parent's own internal spawn (``internal_auth``, ``X-Session-Key`` is the parent)
    records that turn's attempt on the child. A caller presenting another
    session's key gets no child at all (the route refuses a parent it is not), and
    a call without internal auth gets a child that names nothing."""
    from test_subagent_queued_visibility import _Req

    from kiro_crew.dashboard.handlers import messaging
    from kiro_crew.dashboard.handlers._shared_member_scope import _MEMBER_SCOPE_KEY, MemberScope

    key = "cron:j1:r1"
    child = _returned_child("c1", key)
    state, sessions = _spawn_state(tmp_path, key, child)
    handed: list[Any] = []

    async def _hand_off(*_a: Any, **_kw: Any) -> Any:
        handed.append(child)
        return child

    async def _persistent(*_a: Any) -> str:
        return "persistent"

    monkeypatch.setattr(messaging, "_spawn_on_loop", _hand_off)
    monkeypatch.setattr(messaging, "_spawn_request_memory_mode", _persistent)
    await sessions.get_or_create(key)
    _prompt_in_flight(sessions, key)
    asker = "cron:other:r1" if caller == "another_session" else key
    req = _Req(state, body={"task": "work", "parent_session": key})
    if caller != "not_internal":
        req["internal_auth"] = True
    req[_MEMBER_SCOPE_KEY] = MemberScope(asker, True, "")  # the transport's attestation
    req.headers["X-Session-Key"] = asker
    resp = await messaging.api_spawn(req)
    holding = sessions.turn_token(key)
    sessions.release(key)

    if caller == "parent_internal":
        assert resp.status == 200
        assert child._spawned_under == holding is not None
    elif caller == "another_session":
        assert resp.status == 409 and handed == [], "another session spawned for this parent"
        assert child._spawned_under is None
    else:
        assert resp.status == 200 and handed == [child]
        assert child._spawned_under is None, "a call without internal auth named the attempt"


def test_the_spawning_attempt_is_named_only_for_the_parents_own_session(tmp_path: Any) -> None:
    """``_session_turn_holding`` names an attempt only for an internal caller whose
    ``X-Session-Key`` is the parent session itself, and only while the holder's
    prompt is in flight."""
    from test_subagent_queued_visibility import _Req

    from kiro_crew.dashboard.messaging_api.run_views import _session_turn_holding

    sessions = MagicMock()
    sessions.turn_token = MagicMock(return_value=("cron:j1:r1", "task", 7))
    sessions.turn_prompt_in_flight = MagicMock(return_value=True)
    state = SimpleNamespace(sessions=sessions)

    def ask(caller: str, *, internal: bool = True, parent: str = "cron:j1:r1") -> Any:
        req = _Req(state)
        if internal:
            req["internal_auth"] = True
        req.headers["X-Session-Key"] = caller
        return _session_turn_holding(req, state, parent)  # type: ignore[arg-type]

    assert ask("cron:j1:r1") == ("cron:j1:r1", "task", 7)
    assert ask("cron:other:r1") is None
    assert ask("") is None
    assert ask("cron:j1:r1", internal=False) is None
    assert ask("", parent="") is None
    sessions.turn_prompt_in_flight = MagicMock(return_value=False)  # claimed, not prompting
    assert ask("cron:j1:r1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("spawned", ["an_earlier_attempt", "nothing"])
async def test_a_mark_is_held_only_when_the_spawn_also_names_the_holding_attempt(
    tmp_path: Any, spawned: str
) -> None:
    """``_flag_collected_inline`` checks both records against the attempt holding the
    permit: a child whose done-read stamp names it but whose spawn names an earlier
    attempt, or no attempt, is not held, so its completion is delivered."""
    from kiro_crew.slack.gateway_runtime.subagent_injection import INLINE_DELIVER, _inline_verdict

    key = "cron:j1:r1"
    child = _returned_child("c1", key)
    state, sessions = _spawn_state(tmp_path, key, child)
    await sessions.get_or_create(key)  # T
    earlier = sessions.turn_token(key)
    sessions.release(key)
    await sessions.get_or_create(key)  # U, the same task's next claim
    holding = sessions.turn_token(key)
    assert earlier != holding
    child._spawned_under = earlier if spawned == "an_earlier_attempt" else None
    child._seen_done_under = holding
    flagged = _late_mark(sessions, state.subagents, key, child.id)
    sessions.note_turn_landed(key)
    sessions.release(key)

    assert flagged == set(), "a mark was held under an attempt that did not spawn the child"
    assert child._collected_inline is False
    assert _inline_verdict(state.subagents, sessions, (child.id,))[0] == INLINE_DELIVER


# ── no tool call belongs to a holder whose prompt is not in flight ───────────


@pytest.mark.asyncio
async def test_a_spawn_and_a_done_read_while_the_holder_has_not_sent_its_prompt_name_nothing(
    tmp_path: Any,
) -> None:
    """The cut turn's model issues a new ``spawn_sub_agents`` call after the cut,
    while a sibling's injection has claimed the permit but is still building its
    message. The spawn records nothing and the done read stamps nothing, so even
    once the injection's prompt is in flight and the call marks the child there,
    no mark is held: the completion is delivered."""
    from kiro_crew.slack.gateway_runtime.subagent_injection import INLINE_DELIVER, _inline_verdict

    key = "cron:j1:r1"
    child = _returned_child("c1", key)
    state, sessions = _spawn_state(tmp_path, key, child)
    await sessions.get_or_create(key)  # the sibling's injection claims the permit
    _prompt_in_flight(sessions, key, False)  # and is still building its message
    assert sessions.turn_token(key) is not None
    assert sessions.turn_prompt_in_flight(key) is False
    await _spawn_from_the_parents_tool(state, key, child)
    await _read_done(state, child, key)
    assert child._spawned_under is None, "a spawn named a holder with no prompt in flight"
    assert child._seen_done_under is None, "a read named a holder with no prompt in flight"

    _prompt_in_flight(sessions, key)  # the injection's prompt goes out
    await _read_done(state, child, key)
    flagged = _late_mark(sessions, state.subagents, key, child.id)
    sessions.note_turn_landed(key)
    sessions.release(key)

    assert child._seen_done_under is None
    assert flagged == set()
    assert _inline_verdict(state.subagents, sessions, (child.id,))[0] == INLINE_DELIVER


@pytest.mark.asyncio
async def test_a_done_read_waits_for_the_spawning_turns_prompt_to_be_in_flight(
    tmp_path: Any,
) -> None:
    """A child spawned under T is read done while T holds the permit with no
    prompt in flight (between two of T's prompts): nothing is stamped. Read again
    while T's prompt is in flight, it is stamped under T."""
    key = "cron:j1:r1"
    child = _returned_child("c1", key)
    state, sessions = _spawn_state(tmp_path, key, child)
    await sessions.get_or_create(key)
    _prompt_in_flight(sessions, key)
    await _spawn_from_the_parents_tool(state, key, child)
    holding = sessions.turn_token(key)
    _prompt_in_flight(sessions, key, False)
    await _read_done(state, child, key)
    assert child._seen_done_under is None
    _prompt_in_flight(sessions, key)
    await _read_done(state, child, key)
    assert child._seen_done_under == holding == child._spawned_under is not None
    sessions.release(key)


@pytest.mark.asyncio
async def test_a_prompt_is_in_flight_only_under_a_held_permit_and_a_provider_that_says_so() -> None:
    """``SessionManager.turn_prompt_in_flight``: False with no session, with no turn
    holding the permit, and for a provider that cannot report a turn (no
    ``has_active_turn``, one that raises, one that answers anything but True)."""
    key = "cron:j1:r1"
    sessions = _new_sessions()
    assert sessions.turn_prompt_in_flight(key) is False  # no session
    await sessions.get_or_create(key)
    provider = sessions.get_provider(key)
    _prompt_in_flight(sessions, key)
    assert sessions.turn_prompt_in_flight(key) is True
    provider.has_active_turn = MagicMock(return_value=MagicMock())
    assert sessions.turn_prompt_in_flight(key) is False
    provider.has_active_turn = MagicMock(side_effect=RuntimeError("gone"))
    assert sessions.turn_prompt_in_flight(key) is False
    provider.has_active_turn = None
    assert sessions.turn_prompt_in_flight(key) is False
    _prompt_in_flight(sessions, key)
    sessions.release(key)
    assert sessions.turn_prompt_in_flight(key) is False  # no turn holds the permit


# ── one spawn_sub_agents call is anchored to the attempt its first member named ─


@pytest.mark.asyncio
async def test_a_cut_calls_member_posted_under_the_successors_prompt_records_nothing(
    tmp_path: Any,
) -> None:
    """The call's first member is posted under T. T is cut; a successor claims the
    permit and its prompt goes out. The call's next member, posted now, sees an
    in-flight holder, but the call is anchored to T, so it records nothing: read
    done and marked under the successor, which lands, it is still delivered."""
    from kiro_crew.slack.gateway_runtime.subagent_injection import INLINE_DELIVER, _inline_verdict

    key = "cron:j1:r1"
    first, later = _returned_child("c1", key), _returned_child("c2", key)
    state, sessions = _spawn_state(tmp_path, key, first, later)
    await sessions.get_or_create(key)  # T
    _prompt_in_flight(sessions, key)
    await _spawn_from_the_parents_tool(state, key, first, call_id="call1")
    t_holding = sessions.turn_token(key)
    sessions.release(key)  # T is cut between two member POSTs
    holds, ends = asyncio.Event(), asyncio.Event()
    successor = asyncio.create_task(_hold_until(sessions, key, holds, ends))
    await asyncio.wait_for(holds.wait(), _HANG_CEILING_S)
    _prompt_in_flight(sessions, key)  # the successor's prompt is in flight
    await _spawn_from_the_parents_tool(state, key, later, call_id="call1")
    await _read_done(state, later, key)
    flagged = _late_mark(sessions, state.subagents, key, later.id)
    ends.set()
    await asyncio.wait_for(successor, _HANG_CEILING_S)

    assert first._spawned_under == t_holding is not None
    assert later._spawned_under is None, "a cut call's later member recorded the successor"
    assert later._seen_done_under is None
    assert flagged == set()
    assert _inline_verdict(state.subagents, sessions, (later.id,))[0] == INLINE_DELIVER


@pytest.mark.asyncio
async def test_every_member_of_one_call_under_one_attempt_records_it(tmp_path: Any) -> None:
    """Members of one call posted under one attempt all record it, and that turn's
    marks of them are held under it, pending its end."""
    key = "cron:j1:r1"
    children = [_returned_child(f"c{i}", key) for i in range(3)]
    state, sessions = _spawn_state(tmp_path, key, *children)
    await sessions.get_or_create(key)
    _prompt_in_flight(sessions, key)
    await _spawn_from_the_parents_tool(state, key, *children, call_id="call1")
    holding = sessions.turn_token(key)
    for child in children:
        await _read_done(state, child, key)
    flagged = _late_mark(sessions, state.subagents, key, *(c.id for c in children))
    sessions.release(key)

    assert [c._spawned_under for c in children] == [holding] * 3 and holding is not None
    assert flagged == {c.id for c in children}
    assert [c._inline_token for c in children] == [holding] * 3


@pytest.mark.asyncio
async def test_a_call_whose_first_member_named_nothing_anchors_nothing_for_the_rest(
    tmp_path: Any,
) -> None:
    """The call's first member is posted while the holder has no prompt in flight
    and names nothing. A later member posted once a prompt is in flight still
    records nothing: the call is anchored to None."""
    key = "cron:j1:r1"
    first, later = _returned_child("c1", key), _returned_child("c2", key)
    state, sessions = _spawn_state(tmp_path, key, first, later)
    await sessions.get_or_create(key)
    _prompt_in_flight(sessions, key, False)
    await _spawn_from_the_parents_tool(state, key, first, call_id="call1")
    _prompt_in_flight(sessions, key)
    await _spawn_from_the_parents_tool(state, key, later, call_id="call1")
    sessions.release(key)

    assert first._spawned_under is None
    assert later._spawned_under is None, "a member recorded what its call's first member did not"


@pytest.mark.asyncio
async def test_a_member_with_no_call_id_is_judged_by_its_own_holding(tmp_path: Any) -> None:
    """A body with no ``call_id`` (an older kirocrew-core, the CLI, ``spawn_run``)
    records whatever in-flight attempt holds the permit at its own POST, as before
    the anchor: after a cut it records the successor (the window the anchor
    closes for a ``spawn_sub_agents`` call that sends one)."""
    key = "cron:j1:r1"
    first, later = _returned_child("c1", key), _returned_child("c2", key)
    state, sessions = _spawn_state(tmp_path, key, first, later)
    await sessions.get_or_create(key)
    _prompt_in_flight(sessions, key)
    await _spawn_from_the_parents_tool(state, key, first)
    t_holding = sessions.turn_token(key)
    sessions.release(key)
    holds, ends = asyncio.Event(), asyncio.Event()
    successor = asyncio.create_task(_hold_until(sessions, key, holds, ends))
    await asyncio.wait_for(holds.wait(), _HANG_CEILING_S)
    _prompt_in_flight(sessions, key)
    await _spawn_from_the_parents_tool(state, key, later)
    s_holding = sessions.turn_token(key)
    ends.set()
    await asyncio.wait_for(successor, _HANG_CEILING_S)

    assert first._spawned_under == t_holding is not None
    assert later._spawned_under == s_holding is not None and s_holding != t_holding
    assert sessions.parent_turns._call_anchors == {}, "a body with no call id was anchored"


def test_the_call_anchor_map_keeps_only_the_newest_calls() -> None:
    """``ParentTurnLedger.call_attempt`` holds at most ``CALL_ANCHOR_LIMIT`` calls and
    drops the oldest first; a call that is asked about again is kept as newest."""
    from kiro_crew.parent_turns import CALL_ANCHOR_LIMIT, ParentTurnLedger

    ledger = ParentTurnLedger()
    token = ("cron:j1:r1", "task", 1)
    assert ledger.call_attempt("cron:j1:r1", "first", token) == token
    for n in range(CALL_ANCHOR_LIMIT - 1):
        ledger.call_attempt("cron:j1:r1", f"c{n}", token)
    assert ledger.call_attempt("cron:j1:r1", "first", token) == token  # refreshed
    ledger.call_attempt("cron:j1:r1", "one-more", None)
    assert len(ledger._call_anchors) == CALL_ANCHOR_LIMIT
    assert ("cron:j1:r1", "c0") not in ledger._call_anchors, "the oldest call was kept"
    assert ("cron:j1:r1", "first") in ledger._call_anchors
    other = ("cron:j1:r1", "task", 2)
    assert ledger.call_attempt("cron:j1:r1", "first", other) is None
    assert ledger.call_attempt("cron:j1:r1", "one-more", token) is None


# ── a spawn names the attempt its request arrived under ─────────────────────


async def _post_member_slowly(
    state: Any, key: str, child: Any, call_id: str, entered: asyncio.Event, go: asyncio.Event
) -> int:
    """One member POST through the real ``api_spawn``, as ``wave.py`` posts it, whose
    memory-mode read waits for ``go`` (a busy thread pool, a slow disk). Only the
    manager hand-off (returns ``child``) is replaced; the holding read, the call
    anchor and the recording run as in production."""
    from test_subagent_queued_visibility import _Req

    from kiro_crew.dashboard.handlers import messaging
    from kiro_crew.dashboard.handlers._shared_member_scope import _MEMBER_SCOPE_KEY, MemberScope

    async def _hand_off(*_a: Any, **_kw: Any) -> Any:
        return child

    async def _slow_memory_mode(*_a: Any) -> str:
        entered.set()
        await go.wait()
        return "persistent"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messaging, "_spawn_on_loop", _hand_off)
        mp.setattr(messaging, "_spawn_request_memory_mode", _slow_memory_mode)
        body = {"task": "work", "parent_session": key}
        if call_id:
            body["call_id"] = call_id
        req = _Req(state, body=body)
        req["internal_auth"] = True
        req[_MEMBER_SCOPE_KEY] = MemberScope(key, True, "")  # the transport's attestation
        req.headers["X-Session-Key"] = key
        return (await messaging.api_spawn(req)).status


async def _drive_a_post_across_the_cut(tmp_path: Any, *, cut: bool, call_id: str) -> dict[str, Any]:
    """The real cron route, ``_inject_with_retry``, ``POST /api/spawn``,
    ``GET /api/spawn/{id}`` and mark flagging, over a real ``SessionManager``.

    The cron run's turn T has C running from an earlier spawn (``spawn_run``, never
    marked), which keeps its session past a cut. T's next ``spawn_sub_agents`` call
    posts its first member A, and that POST waits in the handler. With ``cut``, the
    wake budget then cancels the run (released, no landing note) and C's completion
    claims the session for its injection S. S's prompt is in flight when A's POST
    goes on, and T's call, still running, reads A done and marks it inside S."""
    from chat_test_helpers import _make_state
    from test_subagent_queued_visibility import _Req

    from kiro_crew.dashboard.messaging_api import run_views

    sessions = _new_sessions()
    key = "cron:j1:r1"
    a = _returned_child("child-a", key)
    a.done = False
    c = _returned_child("child-c", key)
    orch = _cron_orchestrator(sessions, [a, c])
    orch.ctx_builder.build_message = MagicMock(
        side_effect=lambda announce, *_a, **_k: (announce, None)
    )
    state = _make_state(tmp_path)
    state.sessions = sessions
    state.subagents = orch.subagent_mgr
    entered, go = asyncio.Event(), asyncio.Event()
    seen: dict[str, Any] = {"turns": []}

    async def call_reads(aid: str) -> None:
        req = _Req(state, aid)
        req["internal_auth"] = True
        req.headers["X-Session-Key"] = key
        assert (await run_views.api_spawn_status(req)).status == 200

    async def call_finishes() -> None:
        """A finishes; T's call reads it done, writes its reply and marks it."""
        seen["post_status"] = await asyncio.wait_for(seen["post"], _HANG_CEILING_S)
        seen["a_spawned_under"] = a._spawned_under
        a.done = True
        await call_reads(a.id)
        seen["marked"] = _late_mark(sessions, orch.subagent_mgr, key, a.id)
        seen["a_route"] = asyncio.create_task(
            orch._finish_subagent_cron_or_notify(
                a, announce="child-a done", body="b", title="t", carried=(a.id,), parent_key=key
            )
        )
        await _run_as_far_as_it_goes(seen["a_route"])

    async def stream(_client: Any, message: str, **kw: Any) -> str:
        seen["turns"].append(message)
        _prompt_in_flight(sessions, key)  # this injection's prompt is answering
        if message == "child-c done":
            seen["s_token"] = sessions.turn_token(key)
            go.set()  # A's POST goes on while S's prompt is in flight
            await call_finishes()
        on_complete = kw.get("on_complete")
        if on_complete is not None:
            on_complete(SimpleNamespace(stop_reason="end_turn"))  # this injected turn landed
        _prompt_in_flight(sessions, key, False)
        return f"answered {message}"

    with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
        await sessions.get_or_create(key)  # T, the cron run's turn
        _prompt_in_flight(sessions, key)
        seen["t_token"] = sessions.turn_token(key)
        seen["post"] = asyncio.create_task(_post_member_slowly(state, key, a, call_id, entered, go))
        await asyncio.wait_for(entered.wait(), _HANG_CEILING_S)  # A's POST is in the handler
        if cut:
            sessions.release(key)  # the wake budget cancelled the run: no landing note
            _prompt_in_flight(sessions, key, False)
            c_route = asyncio.create_task(
                orch._finish_subagent_cron_or_notify(
                    c, announce="child-c done", body="b", title="t", carried=(c.id,), parent_key=key
                )
            )
            await asyncio.wait_for(c_route, _HANG_CEILING_S)
        else:
            go.set()
            await call_finishes()
            sessions.note_turn_landed(key)  # T lands
            sessions.release(key)
        await _settled(orch)
        if "a_route" in seen:
            await asyncio.wait_for(seen["a_route"], _HANG_CEILING_S)
        await _settled(orch)
    seen["a_state"] = a._inline_state
    seen["parked_after"] = dict(orch._inline_parked)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("call_id", ["call2", ""], ids=["call_id", "no_call_id"])
async def test_a_first_member_posted_across_the_cut_never_records_the_successors_attempt(
    tmp_path: Any, call_id: str
) -> None:
    """A's POST arrived while T's prompt was in flight, and T was cut while the
    handler awaited. When the handler reaches the spawn, S holds the permit with its
    prompt in flight; A names the attempt it arrived under, never S's, so T's call
    reading and marking A inside S holds nothing, and A's completion is delivered."""
    seen = await _drive_a_post_across_the_cut(tmp_path, cut=True, call_id=call_id)

    assert seen["post_status"] == 200
    assert seen["a_spawned_under"] != seen["s_token"], "A recorded the successor's attempt"
    assert seen["turns"][:1] == ["child-c done"]
    assert "child-a done" in seen["turns"], (
        "a member whose POST the cut turn's call made was acknowledged by the "
        f"successor's landing and never delivered (turns={seen['turns']!r}, "
        f"spawned_under={seen['a_spawned_under']!r}, successor={seen['s_token']!r}, "
        f"cut turn={seen['t_token']!r}, state={seen['a_state']!r})"
    )
    assert seen["parked_after"] == {}


@pytest.mark.asyncio
async def test_a_first_member_posted_without_a_cut_is_confirmed_by_its_turn(tmp_path: Any) -> None:
    """The same POST with no cut: A records T's attempt, is read and marked under T,
    and T's landing confirms it, so no completion turn plays."""
    seen = await _drive_a_post_across_the_cut(tmp_path, cut=False, call_id="call2")

    assert seen["post_status"] == 200
    assert seen["a_spawned_under"] == seen["t_token"] is not None
    assert seen["a_state"] == "confirmed"
    assert seen["turns"] == [], f"turns={seen['turns']!r}"
    assert seen["parked_after"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_id",
    ["has-dash", "x" * 33, 7, ["a"], "abc\u0661\u0662"],
    ids=["dash", "too_long", "int", "list", "non_ascii_digits"],
)
async def test_an_invalid_call_id_is_refused_before_any_spawn(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, call_id: Any
) -> None:
    """``call_id`` is checked at the trust boundary: ASCII letters and digits, at most
    32 characters, a string. Anything else is a 400 coded ``invalid_call_id``, and no
    child is created."""
    from test_subagent_queued_visibility import _Req

    from kiro_crew.dashboard.handlers import messaging
    from kiro_crew.dashboard.handlers._shared_member_scope import _MEMBER_SCOPE_KEY, MemberScope

    key = "cron:j1:r1"
    state, _sessions = _spawn_state(tmp_path, key)
    spawn = AsyncMock()

    async def _persistent(*_a: Any) -> str:
        return "persistent"

    monkeypatch.setattr(messaging, "_spawn_on_loop", spawn)
    monkeypatch.setattr(messaging, "_spawn_request_memory_mode", _persistent)
    req = _Req(state, body={"task": "work", "parent_session": key, "call_id": call_id})
    req["internal_auth"] = True
    req[_MEMBER_SCOPE_KEY] = MemberScope(key, True, "")
    req.headers["X-Session-Key"] = key
    resp = await messaging.api_spawn(req)

    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_call_id"
    spawn.assert_not_awaited()


# ── a member the admission gate queued carries its spawning attempt ──────────


async def _real_manager(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real ``SubagentManager`` whose stagger queues every member but the first,
    with its run body replaced by one that finishes at once. The admission gate,
    the queue, the pump and the record it builds when a member starts are real."""
    from overload_fakes import mock_ctx, mock_sessions, wait_taskq_open

    import kiro_crew.subagent as subagent_mod
    from kiro_crew.subagent import SubagentManager
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=4)
    await wait_taskq_open(mgr)
    mgr._spawn_stagger_secs = 3600.0  # one start per hour: members 2..n wait in the queue
    # The stagger compares against the last start, which a fresh manager holds as
    # monotonic 0.0. On a host up for less than the stagger (a fresh CI runner) that
    # would queue the first member too, so the test sets the last start one stagger ago.
    mgr._last_spawn_ts = time.monotonic() - mgr._spawn_stagger_secs

    async def finished(info: Any) -> None:
        info.result = f"{info.id} result"
        info.done = True

    monkeypatch.setattr(mgr, "_run", AsyncMock(side_effect=finished))
    return mgr


async def _stop_manager(mgr: Any) -> None:
    from kiro_crew.testing.wait import await_until

    mgr._shutting_down = True
    tasks = [task for task in mgr._tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await await_until(
            asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True)), timeout=5
        )
    mgr._taskq.close()


async def _post_member(state: Any, key: str, call_id: str, monkeypatch: Any) -> Any:
    """One member POST of a ``spawn_sub_agents`` call through the real ``api_spawn``
    and the real manager hand-off; returns the record the route answered with."""
    from test_subagent_queued_visibility import _Req

    from kiro_crew.dashboard.handlers import messaging
    from kiro_crew.dashboard.handlers._shared_member_scope import _MEMBER_SCOPE_KEY, MemberScope

    async def _persistent(*_a: Any) -> str:
        return "persistent"

    monkeypatch.setattr(messaging, "_spawn_request_memory_mode", _persistent)
    req = _Req(state, body={"task": "work", "parent_session": key, "call_id": call_id})
    req["internal_auth"] = True
    req[_MEMBER_SCOPE_KEY] = MemberScope(key, True, "")
    req.headers["X-Session-Key"] = key
    resp = await messaging.api_spawn(req)
    assert resp.status == 200, resp.text
    import json

    return json.loads(resp.text)["id"]


async def _start_the_queue(mgr: Any, ids: list[str]) -> None:
    from kiro_crew.testing.wait import async_wait_until

    mgr._spawn_stagger_secs = 0.0

    def _all_started() -> bool:
        mgr._drain_queue()
        return all(mgr.get(aid) is not None and mgr.get(aid).done for aid in ids)

    await async_wait_until(
        _all_started,
        describe=lambda: (
            "the queued members never started: "
            f"{[(aid, getattr(mgr.get(aid), 'done', None)) for aid in ids]!r} "
            f"queue={len(mgr._queue)} running={mgr._running_count} "
            f"queued={[mgr.is_queued(aid) for aid in ids]!r}"
        ),
    )


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_queued_members_of_one_call_are_confirmed_with_no_completion_turn(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three members of one ``spawn_sub_agents`` call under a cron turn: the gate
    starts the first and queues the other two behind the stagger. The pump starts
    them later, each on a record of its own; every one carries the spawning
    attempt, is read done and marked under it, and the turn lands. Every member is
    confirmed, and no completion turn plays for any of them."""
    from chat_test_helpers import _make_state

    key = "cron:j1:r1"
    sessions = _new_sessions()
    mgr = await _real_manager(monkeypatch)
    state = _make_state(tmp_path)
    state.sessions = sessions
    state.subagents = mgr
    orch = _cron_orchestrator(sessions, [])
    orch.subagent_mgr.get = mgr.get
    orch.ctx_builder.build_message = MagicMock(
        side_effect=lambda announce, *_a, **_k: (announce, None)
    )
    turns: list[str] = []

    async def stream(_client: Any, message: str, **_kw: Any) -> str:
        turns.append(message)
        return "answered"

    try:
        with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
            await sessions.get_or_create(key)
            _prompt_in_flight(sessions, key)
            holding = sessions.turn_token(key)
            ids = [await _post_member(state, key, "call1", monkeypatch) for _ in range(3)]
            queued_at_post = [mgr.get(aid) is None for aid in ids]
            await _start_the_queue(mgr, ids)
            records = [mgr.get(aid) for aid in ids]
            for record in records:
                await _read_done(state, record, key)
            flagged = _late_mark(sessions, mgr, key, *ids)
            sessions.note_turn_landed(key)
            sessions.release(key)
            for record in records:
                route = asyncio.create_task(
                    orch._finish_subagent_cron_or_notify(
                        record,
                        announce=f"{record.id} done",
                        body="b",
                        title="t",
                        carried=(record.id,),
                        parent_key=key,
                    )
                )
                await asyncio.wait_for(route, _HANG_CEILING_S)
            await _settled(orch)
    finally:
        await _stop_manager(mgr)

    assert queued_at_post[1:] == [True, True], "the stagger did not queue members 2..n"
    assert [r._spawned_under for r in records] == [holding] * 3 and holding is not None
    assert flagged == set(ids)
    assert [r._inline_state for r in records] == ["confirmed"] * 3
    assert turns == [], f"a queued member's completion played a turn: {turns!r}"
    assert mgr._queued_spawn_attempts == {}, "a started member's carried attempt was kept"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_queued_member_that_starts_after_its_spawning_attempt_ended_holds_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member queued under T starts after T ended, under the same task's next
    turn U. It carries T, so its done read and its mark under U hold nothing and
    its completion is delivered."""
    from chat_test_helpers import _make_state

    from kiro_crew.slack.gateway_runtime.subagent_injection import INLINE_DELIVER, _inline_verdict

    key = "cron:j1:r1"
    sessions = _new_sessions()
    mgr = await _real_manager(monkeypatch)
    state = _make_state(tmp_path)
    state.sessions = sessions
    state.subagents = mgr
    try:
        await sessions.get_or_create(key)  # T
        _prompt_in_flight(sessions, key)
        t_holding = sessions.turn_token(key)
        ids = [await _post_member(state, key, "call1", monkeypatch) for _ in range(2)]
        sessions.release(key)  # T ends before the queued member starts
        await sessions.get_or_create(key)  # U
        _prompt_in_flight(sessions, key)
        await _start_the_queue(mgr, ids)
        later = mgr.get(ids[1])
        await _read_done(state, later, key)
        flagged = _late_mark(sessions, mgr, key, later.id)
        sessions.note_turn_landed(key)
        sessions.release(key)
    finally:
        await _stop_manager(mgr)

    assert later._spawned_under == t_holding is not None
    assert later._seen_done_under is None
    assert flagged == set()
    assert _inline_verdict(mgr, sessions, (later.id,))[0] == INLINE_DELIVER


def test_a_carried_attempt_is_bounded_and_dropped_when_the_row_never_starts() -> None:
    """``SubagentManager.carry_spawn_attempt`` keeps at most
    ``_QUEUED_SPAWN_ATTEMPT_LIMIT`` members, oldest dropped first; a row that ends
    unstarted (``_forget_pending_start``) drops its entry; a member already
    started gets the attempt on its live record."""
    from overload_fakes import mock_ctx, mock_sessions

    from kiro_crew.subagent import SubagentInfo, SubagentManager
    from kiro_crew.subagent_parts.run_limits import _QUEUED_SPAWN_ATTEMPT_LIMIT as LIMIT

    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    token = ("cron:j1:r1", "task", 1)
    for n in range(LIMIT + 1):
        mgr.carry_spawn_attempt(f"m{n}", token)
    assert len(mgr._queued_spawn_attempts) == LIMIT
    assert "m0" not in mgr._queued_spawn_attempts
    mgr._forget_pending_start("m5")
    assert "m5" not in mgr._queued_spawn_attempts
    live = SubagentInfo(id="live", task="t")
    mgr._agents["live"] = live
    mgr.carry_spawn_attempt("live", token)
    assert live._spawned_under == token and "live" not in mgr._queued_spawn_attempts


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_queued_member_stopped_before_it_starts_is_delivered_not_lost(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two members of one call under a cron turn; the stagger queues the second, which
    is then stopped before it starts (a queued Stop) through the real manager. Its
    synthetic terminal record carries no spawning attempt, so the call's read and mark
    under the spawning turn hold nothing and its completion is delivered after the
    turn lands: one duplicate turn, never a loss. The first member is confirmed."""
    from chat_test_helpers import _make_state

    from kiro_crew.slack.gateway_runtime.subagent_injection import INLINE_DELIVER, _inline_verdict
    from kiro_crew.testing.wait import async_wait_until

    key = "cron:j1:r1"
    sessions = _new_sessions()
    mgr = await _real_manager(monkeypatch)
    state = _make_state(tmp_path)
    state.sessions = sessions
    state.subagents = mgr
    orch = _cron_orchestrator(sessions, [])  # wires the ledger's release hook, as boot does
    orch.subagent_mgr.get = mgr.get
    try:
        await sessions.get_or_create(key)
        _prompt_in_flight(sessions, key)
        ids = [await _post_member(state, key, "call1", monkeypatch) for _ in range(2)]
        queued_at_post = mgr.get(ids[1]) is None
        carried_at_post = ids[1] in mgr._queued_spawn_attempts
        assert await mgr.cancel(ids[1])  # the queued Stop
        await async_wait_until(
            lambda: mgr.get(ids[1]) is not None and mgr.get(ids[1]).done,
            describe=lambda: f"the queued stop never registered: {mgr.get(ids[1])!r}",
        )
        await async_wait_until(lambda: mgr.get(ids[0]) is not None and mgr.get(ids[0]).done)
        stopped, first = mgr.get(ids[1]), mgr.get(ids[0])
        for record in (first, stopped):
            await _read_done(state, record, key)
        flagged = _late_mark(sessions, mgr, key, *ids)
        sessions.note_turn_landed(key)
        sessions.release(key)
        verdict_stopped = _inline_verdict(mgr, sessions, (stopped.id,))[0]
        first_state = first._inline_state
    finally:
        await _stop_manager(mgr)

    assert queued_at_post and carried_at_post, "the second member was not queued with its carry"
    assert stopped.queued is True and stopped.user_stopped is True
    assert stopped._spawned_under is None and stopped._seen_done_under is None
    assert ids[1] not in mgr._queued_spawn_attempts, "the unstarted row kept its carry"
    assert flagged == {first.id}
    assert first_state == "confirmed"
    assert verdict_stopped == INLINE_DELIVER, "the unstarted member's completion was not delivered"
