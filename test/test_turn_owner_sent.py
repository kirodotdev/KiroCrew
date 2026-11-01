"""The turn's OWNER mark: ``_turn_owner_sent`` and ``_directive_owner_origin``.

``_turn_user_sent`` says a person sent the running turn. It does not say which
one: ``/kirocrew dashboard`` mints a dashboard token for a channel user the
install allow-lists, and their sends reach ``api_chat`` as a non-app request
exactly as the owner's do. The crew-scoped session reads
(``handlers_instances_crew_reads``) disclose the owner's peer corpus and need the
narrower fact, so the gateway records ``is_owner_dashboard_request`` for the
request that dispatched each turn and keeps it for the turn's duration:

* a dashboard send, regenerate or rewind stamps it on the slot immediately
  before dispatch; the turn's outermost entry keeps it only for a user-sent
  opener; the turn's end clears it;
* a queued entry carries it as ``_directive_owner_origin`` (never persisted), and
  the drain reduces it over EVERY consumed entry before dispatching the successor;
* a steer that is not the owner's clears the running turn's mark, and a steer
  requeued at the turn's end carries the mark its own sender earned.

These tests drive the real runner, the real delivery seam and the real
``api_chat`` handler; only the model is stubbed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.slot_queue_repository import sanitize_restored_queue

NON_OWNER = "telegram:4242"


def _busy(slot):
    """``turn_running`` is derived (``task is not None and not task.done()``)."""
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _as(subject: str):
    """A middleware that makes every request a dashboard-token send by *subject*.

    Inserted ahead of the harness's owner middleware, which only fills claims that
    are absent, so the request reaches ``api_chat`` as a non-app sender who is or
    is not the owner by ``user`` alone -- the shape a token minted for a channel
    user has.
    """

    @web.middleware
    async def _mw(request: web.Request, handler):
        request["app"] = ""
        request["user"] = subject
        return await handler(request)

    return _mw


def _app(state, subject: str | None = None) -> web.Application:
    app = _make_app(state)
    if subject is not None:
        app.middlewares.insert(0, _as(subject))
    return app


# ── the runner: adopt at entry, clear at exit ──


@pytest.mark.asyncio
def _one_turn(state, slot, seen: list[bool]):
    """Wire *state* to run one bare turn whose model records the owner mark."""
    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent

    async def _stream(_msg):
        seen.append(slot._turn_owner_sent)
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
        yield AcpEvent(kind=EVENT_COMPLETE)

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "user", "channel", "expected"),
    [
        (True, True, False, True),
        (True, False, False, False),
        (True, True, True, False),
        (False, True, False, False),
    ],
)
async def test_the_turn_keeps_the_owner_mark_only_for_a_user_sent_opener(
    tmp_path, monkeypatch, declared: bool, user: bool, channel: bool, expected: bool
) -> None:
    """The dispatcher's ``_directive_owner_origin`` becomes the turn's mark at entry.

    Adopted only when the opener is also user-sent, so it cannot carry into a loop
    wake, a cron, an app or a channel turn; it rides this call alone, so it cannot
    carry into any later turn at all. Cleared at the turn's end.
    """
    from test_acp_tool_identity import _stub_state

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    seen: list[bool] = []
    _one_turn(state, slot, seen)

    await cr._run_chat(
        state,
        slot,
        "hello",
        _directive_user_origin=user,
        _directive_channel_origin=channel,
        _directive_owner_origin=declared,
    )
    assert seen == [expected]
    assert slot._turn_owner_sent is False, "the mark ends with the turn"


@pytest.mark.asyncio
async def test_an_owner_dispatch_that_never_starts_leaves_nothing_for_the_next_turn(
    tmp_path, monkeypatch
) -> None:
    """The owner's mark rides the dispatch's own call, never the slot.

    A send whose Shared Crew admission expires or is refused, or a task cancelled
    before its first step, never enters ``_run_chat``. Had the mark been declared
    on the slot, it would have stayed there, and the next user-sent turn from a
    path that declares nothing (``openai_compat`` with a non-owner dashboard
    token) would have adopted it.
    """
    from test_acp_tool_identity import _stub_state

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    seen: list[bool] = []
    _one_turn(state, slot, seen)

    never_started = cr._run_chat(
        state, slot, "owner words", _directive_user_origin=True, _directive_owner_origin=True
    )
    never_started.close()
    await cr._run_chat(state, slot, "someone else", _directive_user_origin=True)
    assert seen == [False]


@pytest.mark.asyncio
async def test_a_stale_mark_is_never_inherited_by_a_turn_that_declares_nothing(
    tmp_path, monkeypatch
) -> None:
    """A mark left on the slot is not read; only this turn's own call is.

    The case a slot-level mark alone would get wrong: an earlier turn left
    ``_turn_owner_sent`` True (a hand-off's nested exit, a crash) and the next
    dispatch is user-sent but comes from a path that declares nothing --
    ``openai_compat``, the spec-builder runtime -- so a dashboard-token holder who
    is not the owner would have run with the owner's mark. The entry takes the
    call's own value (False) instead of keeping what it finds.
    """
    from test_acp_tool_identity import _stub_state

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    seen: list[bool] = []
    _one_turn(state, slot, seen)

    slot._turn_owner_sent = True  # stale, from whatever came before
    await cr._run_chat(state, slot, "hello", _directive_user_origin=True)
    assert seen == [False]


@pytest.mark.asyncio
async def test_a_nested_exit_clears_the_mark_too(tmp_path, monkeypatch) -> None:
    """A ``/prompts get`` hand-off ends the turn from the depth-1 frame.

    The outermost frame runs no tail once it has handed off, so a clear gated on
    ``depth == 0`` would leave the owner's mark on the slot after the turn. The
    mark is cleared on every exit instead; the entry does not re-read it at
    depth 1, so the nested frame runs under the mark the outermost entry adopted.
    """
    from test_acp_tool_identity import _stub_state

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    seen: list[bool] = []
    _one_turn(state, slot, seen)

    slot._turn_owner_sent = True  # adopted by the outermost entry that handed off
    slot._turn_user_sent = True
    await cr._run_chat(state, slot, "expanded prompt", _directive_user_origin=True, _prompt_depth=1)
    assert seen == [True], "the nested frame keeps the outermost entry's mark"
    assert slot._turn_owner_sent is False, "and its exit clears it"


# ── the steer seam: narrow at admission, carry through the requeue ──


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_origin", [False, True])
async def test_a_steer_that_is_not_the_owners_clears_the_running_turns_mark(
    owner_origin: bool,
) -> None:
    """Narrowed at admission, before the RPC, like the user-sent mark.

    ``user_origin=True`` on both: the steer that clears the mark here is a
    person's -- a dashboard-token holder who is not the owner -- which is exactly
    the case ``_turn_user_sent`` cannot see.
    """
    from kiro_crew.dashboard import chat_delivery
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1")
    slot._turn_user_sent = True
    slot._turn_owner_sent = True
    client = MagicMock(supports_steer=True, steer_needs_loss_recovery=False)
    client.steer = AsyncMock(return_value=None)
    slot._acp_client = client
    await chat_delivery.steer_into_running_turn(
        MagicMock(), slot, "read the other crew's chat", user_origin=True, owner_origin=owner_origin
    )
    assert slot._turn_user_sent is True
    assert slot._turn_owner_sent is owner_origin


@pytest.mark.asyncio
async def test_the_steer_seam_defaults_to_not_the_owner() -> None:
    """A caller that says nothing earns no mark: the default is the refusing one."""
    from kiro_crew.dashboard import chat_delivery
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1")
    slot._turn_owner_sent = True
    client = MagicMock(supports_steer=True, steer_needs_loss_recovery=False)
    client.steer = AsyncMock(return_value=None)
    slot._acp_client = client
    await chat_delivery.steer_into_running_turn(MagicMock(), slot, "x", user_origin=True)
    assert slot._turn_owner_sent is False


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True])
async def test_a_requeued_steer_carries_the_mark_its_sender_earned(tmp_path, owner: bool) -> None:
    """A steer the turn ended before consuming runs as its own turn.

    Its entry takes ``_directive_owner_origin`` from the lockstep map the steer
    seam wrote, and the map is popped with its siblings.
    """
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    text = "steer that never landed"
    slot._pending_steers.append(text)
    slot._steer_user_origin[text] = True
    slot._steer_channel_origin[text] = False
    slot._steer_owner_origin[text] = owner
    slot._steer_admissions[text] = sc.containment_meta(state, slot)

    cr._requeue_unconsumed_steers(state, slot)

    (entry,) = slot._queue
    assert entry.get("_directive_user_origin") is True
    assert entry.get("_directive_owner_origin", False) is owner
    assert slot._steer_owner_origin == {}, "popped in lockstep with the other maps"


@pytest.mark.asyncio
async def test_a_requeued_steer_on_an_app_slot_earns_no_owner_mark(tmp_path) -> None:
    """The owner mark never outranks the user one: not user-origin, not owner-origin."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot._app = "some-app"
    text = "steer into an app slot"
    slot._pending_steers.append(text)
    slot._steer_user_origin[text] = True
    slot._steer_owner_origin[text] = True
    slot._steer_admissions[text] = sc.containment_meta(state, slot)

    cr._requeue_unconsumed_steers(state, slot)

    (entry,) = slot._queue
    assert "_directive_user_origin" not in entry
    assert "_directive_owner_origin" not in entry


# ── the recovery requeue: a second turn of the same request keeps the mark ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "narrowed", "expected"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
async def test_a_recovery_requeue_carries_the_mark_as_the_dying_turn_held_it(
    tmp_path, monkeypatch, declared: bool, narrowed: bool, expected: bool
) -> None:
    """The runtime dies under the owner's turn: the retry is still the owner's.

    ``_queue_recovery`` requeues the request with the turn's user and channel
    provenance; without the owner mark the drain would compute ``False`` for the
    successor, and the owner's retried crew-read lookup would be refused as
    ``not_owner_turn``. The mark is read off the slot as it stands at the requeue,
    so a non-owner steer that narrowed the dying turn narrows the retry too, and a
    turn that declared nothing earns nothing. Drives the real runner through the
    real dead-process arm; only the model is stubbed.
    """
    from test_acp_tool_identity import _stub_state

    from kiro_crew.acp.transport_errors import AcpProcessDied
    from kiro_crew.dashboard.chat_utils_injected_rows import SYNTHETIC_RECOVERY_KIND

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True

    async def _die(_msg):
        if narrowed:
            # A dashboard-token holder who is not the owner steers in before the
            # runtime dies; the admission seam narrows the running turn's mark.
            slot._turn_owner_sent = False
        raise AcpProcessDied("kiro-cli exited")
        yield  # pragma: no cover -- makes this an async generator

    client = MagicMock()
    client.stream = _die
    client.stream_command = _die
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.reset = AsyncMock()
    monkeypatch.setattr(cr, "_start_next_queued_turn", AsyncMock(return_value=False))

    with patch.object(cr, "_recovery_delay", new_callable=AsyncMock):
        await cr._run_chat(
            state,
            slot,
            "read the other crew's chat",
            _directive_user_origin=True,
            _directive_owner_origin=declared,
        )

    (entry,) = [e for e in slot._queue if e.get("kind") == SYNTHETIC_RECOVERY_KIND]
    assert entry.get("_directive_user_origin") is True, "the retry is still the person's"
    assert entry.get("_directive_owner_origin", False) is expected
    assert slot._turn_owner_sent is False, "the dying turn's mark still ends with it"


# ── the drain: reduce over every consumed entry, stamp before dispatch ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marks", "expected"),
    [([True], True), ([True, True], True), ([True, False], False), ([False], False)],
)
async def test_the_drain_stamps_the_successor_only_when_every_entry_is_the_owners(
    tmp_path, monkeypatch, marks: list[bool], expected: bool
) -> None:
    """``all``, not ``any``: a merged batch that folds another dashboard user's
    words in with the owner's would otherwise run them under the owner's
    authority. Merging is on so the two-entry cases drain as ONE turn."""
    from test_acp_tool_identity import _stub_state

    from kiro_crew.config import KiroCrewConfig

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    for index, owner in enumerate(marks):
        slot.queue_append(
            f"queued {index}", directive_user_origin=True, directive_owner_origin=owner
        )
    seen: list[bool] = []

    async def _run(_state, the_slot, _message, **kwargs):
        seen.append(kwargs.get("_directive_owner_origin", False))

    monkeypatch.setattr(cr, "_run_chat", _run)
    config = KiroCrewConfig()
    config.dashboard.merge_queued_messages = True
    with patch.object(KiroCrewConfig, "load", return_value=config):
        assert await cr._start_next_queued_turn(state, slot) is True
        assert slot.task is not None
        await slot.task
    assert slot.queue_depth == 0, "every entry drained into the one turn"
    assert seen == [expected]


def test_a_restored_entry_never_carries_the_owner_mark() -> None:
    """Provenance does not cross a restart: the transcript line is agent-writable."""
    restored = sanitize_restored_queue(
        [
            {
                "id": "q1",
                "content": "hi",
                "_directive_user_origin": True,
                "_directive_owner_origin": True,
            }
        ]
    )
    (entry,) = restored
    assert "_directive_owner_origin" not in entry
    assert "_directive_user_origin" not in entry


def test_editing_a_queued_entry_gives_it_the_editors_mark(tmp_path) -> None:
    """The replacement words are the editor's: an owner's entry rewritten by
    another dashboard user drains without the owner mark, and the reverse earns it."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    qid = slot.queue_append(
        "owner's words", directive_user_origin=True, directive_owner_origin=True
    )
    assert slot.queue_edit_by_id(qid, "someone else's words", directive_user_origin=True)
    (entry,) = slot._queue
    assert "_directive_owner_origin" not in entry
    assert slot.queue_edit_by_id(
        qid, "owner again", directive_user_origin=True, directive_owner_origin=True
    )
    assert entry.get("_directive_owner_origin") is True


# ── api_chat: the three delivery paths, for the owner and for another person ──


@pytest.mark.asyncio
@pytest.mark.parametrize(("subject", "expected"), [(None, True), (NON_OWNER, False)])
async def test_an_idle_send_passes_the_mark_to_its_dispatch(
    tmp_path, monkeypatch, subject: str | None, expected: bool
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    seen: list[bool] = []

    async def fake_run_chat(_st, sl, _msg, **_kwargs):
        seen.append(_kwargs.get("_directive_owner_origin", False))

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1")

    async with TestClient(TestServer(_app(state, subject))) as client:
        resp = await client.post("/api/chat?ws=1", json={"message": "hello", "slot": "s1"})
        assert resp.status == 200
    assert seen == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(("subject", "expected"), [(None, True), (NON_OWNER, False)])
async def test_a_send_into_a_busy_slot_marks_its_queue_entry(
    tmp_path, monkeypatch, subject: str | None, expected: bool
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("s1"))

    async with TestClient(TestServer(_app(state, subject))) as client:
        resp = await client.post("/api/chat?ws=1", json={"message": "later", "slot": "s1"})
        assert resp.status == 200
        assert (await resp.json()).get("queued") is True
    (entry,) = slot._queue
    assert entry.get("_directive_user_origin") is True, "a person's send, owner or not"
    assert entry.get("_directive_owner_origin", False) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("subject", "expected"), [(None, True), (NON_OWNER, False)])
async def test_a_send_held_behind_sub_agents_marks_its_queue_entry(
    tmp_path, monkeypatch, subject: str | None, expected: bool
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    subs = MagicMock()
    subs.running_agents_for = MagicMock(return_value=[{"id": "a1"}])
    state = _make_state(tmp_path, subagents=subs)
    slot = state.get_or_create_slot("s1")

    async with TestClient(TestServer(_app(state, subject))) as client:
        resp = await client.post("/api/chat?ws=1", json={"message": "parked", "slot": "s1"})
        assert resp.status == 200
        assert (await resp.json()).get("queued") is True
    (entry,) = slot._queue
    assert entry.get("_directive_owner_origin", False) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("subject", "expected"), [(None, True), (NON_OWNER, False)])
async def test_a_steer_reports_its_senders_mark_to_the_seam(
    tmp_path, monkeypatch, subject: str | None, expected: bool
) -> None:
    """The composer's steer tells the seam who sent it; the seam decides the rest."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    _busy(state.get_or_create_slot("s1"))
    seen: list[dict] = []

    async def fake_steer(_state, _slot, _message, **kwargs):
        seen.append(kwargs)
        return "steered"

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.steer_into_running_turn", fake_steer)
    async with TestClient(TestServer(_app(state, subject))) as client:
        resp = await client.post(
            "/api/chat?ws=1", json={"message": "now", "slot": "s1", "steer": True}
        )
        assert resp.status == 200
        assert (await resp.json()).get("steered") is True
    (kwargs,) = seen
    assert kwargs["user_origin"] is True
    assert kwargs["owner_origin"] is expected


# ── regenerate and rewind: the mark is this request's, not the original row's ──


def _conversation(state, key: str = "s1"):
    slot = state.get_or_create_slot(key)
    slot._titled = True
    slot.append("user", "first question", "msg msg-u")
    slot.append("assistant", "first answer", "msg msg-a")
    slot.drain()
    return slot


@pytest.mark.asyncio
@pytest.mark.parametrize(("subject", "expected"), [(None, True), (NON_OWNER, False)])
async def test_a_regenerate_passes_the_mark_to_its_dispatch(
    tmp_path, monkeypatch, subject: str | None, expected: bool
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    seen: list[bool] = []

    async def fake_run_chat(_st, sl, _msg, **_kwargs):
        seen.append(_kwargs.get("_directive_owner_origin", False))

    monkeypatch.setattr("kiro_crew.dashboard.chat_regenerate._run_chat", fake_run_chat)
    state = _make_state(tmp_path)
    _conversation(state)

    async with TestClient(TestServer(_app(state, subject))) as client:
        resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 200
        for _ in range(3):
            await asyncio.sleep(0)
    assert seen == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(("subject", "expected"), [(None, True), (NON_OWNER, False)])
async def test_a_rewind_passes_the_mark_to_its_dispatch(
    tmp_path, monkeypatch, subject: str | None, expected: bool
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    seen: list[bool] = []

    async def fake_run_chat(_st, sl, _msg, **_kwargs):
        seen.append(_kwargs.get("_directive_owner_origin", False))

    monkeypatch.setattr("kiro_crew.dashboard.chat_rewind._run_chat", fake_run_chat)
    state = _make_state(tmp_path)
    slot = _conversation(state)
    state.sessions._session_map.get = MagicMock(return_value="")

    async with TestClient(TestServer(_app(state, subject))) as client:
        resp = await client.post(
            "/api/chat/slots/s1/rewind",
            json={"at_message_index": 0, "content": "edited first question"},
        )
        assert resp.status == 200
        if slot.task is not None:
            await slot.task
    assert seen == [expected]


# ── the hard kill: the owner map is popped with its siblings ──


@pytest.mark.asyncio
async def test_a_hard_kill_pops_the_owner_map_with_the_other_steer_maps(
    tmp_path, monkeypatch
) -> None:
    """A second Stop discards pending steers and every per-steer map entry.

    The maps are keyed by message text and counted together against
    ``MAX_PENDING_STEERS`` (``retained_steer_count``), so an entry a hard kill
    left behind would hold a full message string for the slot's life and, after
    enough discarded steers, make every later steer queue instead of reaching the
    client. The owner map is popped in the same lockstep loop as its siblings.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.sessions.stop_turn = AsyncMock()
    slot = _busy(state.get_or_create_slot("s1"))
    slot._stop_state = "soft_pending"  # the first press already happened
    text = "steer the kill discards"
    slot._pending_steers = [text]
    slot._steer_user_origin[text] = True
    slot._steer_channel_origin[text] = False
    slot._steer_owner_origin[text] = True

    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/slots/s1/stop?force=true")
        assert resp.status == 200

    assert slot._pending_steers == []
    assert slot._steer_owner_origin == {}, "popped in lockstep with the other maps"
    assert slot._steer_user_origin == {} and slot._steer_channel_origin == {}
