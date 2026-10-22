"""Edit-resend's boundaries in :mod:`kiro_crew.dashboard.chat_regenerate`.

What edit-and-resend may touch, who may call it, and what its commit re-checks: the
prospective copy leaves the live slot's pending question and cards alone until the commit
adopts them; an app cannot edit a slot it does not own; a session with attached sub-agents
refuses; the slot is reserved across the durable boundaries, so a concurrent send queues; rows and cards that
arrive during the boundary belong to the new timeline; and the commit refuses when the
slot is rebound, replaced or its reservation displaced. Regenerate, switch-variant, the
recreate race, the rest of edit-resend and the refusal codes are in
``test_chat_regenerate_cov80.py``; the shared app and fixtures are in
``chat_regenerate_cov80_helpers.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from chat_regenerate_cov80_helpers import state_fixture  # noqa: F401
from chat_regenerate_cov80_helpers import _client, _make_regen_app

from kiro_crew.dashboard.chat_regenerate import api_chat_slot_edit_resend
from kiro_crew.testing.wait import async_wait_until

# ── edit-resend: the prospective copy must not touch the live slot ──
# ``copy.copy`` is shallow, so reassigning ``messages`` alone leaves every other
# mutable attribute aliased to the live slot's object -- and ``append`` writes
# through four of them. These pin that a REFUSED edit leaves all four alone, and
# that the commit is what adopts them.


def _arm_pending_question(slot) -> tuple[str, list]:
    """Register one non-blocking question card and a retirement spy on *slot*."""
    announced: list = []
    slot._question_pending = {"q1": {"blocking": False, "prompt": "which host?"}}
    slot._on_question_retired = lambda key, ids: announced.append((key, list(ids)))
    return "q1", announced


@pytest.mark.asyncio
async def test_edit_resend_refusal_leaves_the_live_pending_and_cards_alone(state) -> None:
    """A refused edit must publish nothing to the live slot.

    The prospective ``append`` runs BEFORE all four rejection points, so an
    un-severed shallow copy pushes the edited row into the live ``_pending``
    queue (the open stream reader's next drain renders it), wakes ``event``, and
    announces the live question cards as retired -- leaving a phantom row on
    screen and a card-less "needs input" behind for an edit the server refused.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot.event.clear()
    question_id, announced = _arm_pending_question(slot)
    state.sessions.discard_conversation = AsyncMock(side_effect=OSError("map write failed"))

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history"),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            # flake-ok: pre-existing one-yield barrier after the refused edit's 503, before reading the live slot's pending queue and cards, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    assert slot._pending == []
    assert not slot.event.is_set()
    assert announced == []
    assert question_id in slot._question_pending


@pytest.mark.asyncio
async def test_edit_resend_commit_adopts_the_prepared_pending_and_retires_cards(state) -> None:
    """The commit is the ONE place the prepared state becomes live.

    Severing the copy must not lose the work: on success the edited row still
    reaches the live pending queue, ``event`` is still woken for the stream
    reader, and the question retirement the prospective append computed is
    announced HERE, through the live callback the copy was denied.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot.event.clear()
    question_id, announced = _arm_pending_question(slot)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            # flake-ok: pre-existing one-yield barrier after the committed edit's 200, before reading the adopted pending queue and cards, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    assert [m["content"] for m in slot._pending] == ["edited"]
    assert slot.event.is_set()
    assert announced == [("s1", [question_id])]
    assert question_id not in slot._question_pending


@pytest.mark.asyncio
async def test_edit_resend_commit_advances_the_lifetime_message_counter(state) -> None:
    """``total_messages`` is a lifetime counter, and the prospective ``append``
    bumps only the COPY's int.

    Left stale, the edited row is invisible to every reader of it:
    ``_get_active_workspace`` picks the max-counter slot to decide which
    workspace's lessons to load, and the Slack mirror compares the counter
    against its own start value to decide whether anything happened. A refused
    edit must not advance it either.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    before = slot.total_messages

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            # flake-ok: pre-existing one-yield barrier after the committed edit's 200, before reading the lifetime message counter, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    # One new row landed, so the lifetime counter advanced by exactly one --
    # truncating the window deliberately does not roll it back.
    assert slot.total_messages == before + 1

    # A refused edit leaves it alone.
    state.sessions.discard_conversation = AsyncMock(side_effect=OSError("map write failed"))
    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history"),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "again"}
            )
            assert resp.status == 503
            # flake-ok: pre-existing one-yield barrier after the refused edit's 503, before reading the lifetime message counter again, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    assert slot.total_messages == before + 1


# ── edit-resend: app isolation ──
# This endpoint discards the slot's NATIVE ACP conversation, so an app token
# reaching a slot it does not own destroys a resume identity it has no claim on.


def _app_request(state, slot_name: str, app_token: str, body: dict):
    """A mocked edit-resend request carrying *app_token* as its app identity."""
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{slot_name}/edit-resend",
        match_info={"slot": slot_name},
        app=_make_regen_app(state),
    )
    request["app"] = app_token

    async def _json():
        return body

    request.json = _json  # type: ignore[method-assign]
    return request


@pytest.mark.asyncio
async def test_edit_resend_denies_an_app_that_does_not_own_the_slot(state) -> None:
    """404 rather than 403: a non-owning token must not be able to use the status
    code to probe which slots exist. Nothing is discarded and nothing moves."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        resp = await api_chat_slot_edit_resend(
            _app_request(state, "s1", "other-app", {"index": 0, "content": "edited"})
        )

    assert resp.status == 404
    assert resp.text is not None and "slot_not_found" in resp.text
    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_denies_an_app_reaching_a_channel_linked_session(state) -> None:
    """Owning the slot is not owning the session it is linked to.

    ``effective_session_key`` resolves a channel-linked slot onto the channel's
    own conversation, so an app edit-resend would discard the native identity of
    a session the app does not own. Same 404 shape (anti-enumeration).
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot._app = "some-app"
    slot.linked_session_key = "slack:1234567890.123"
    original_messages = list(slot.messages)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        resp = await api_chat_slot_edit_resend(
            _app_request(state, "s1", "some-app", {"index": 0, "content": "edited"})
        )

    assert resp.status == 404
    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_reauthorizes_the_slot_after_the_body_read(state) -> None:
    """Reading the body is an await, and a slot can be replaced across it.

    A delete-and-recreate under the same name is a DIFFERENT conversation that
    would pass any name-based re-check, so the guard requires the same slot
    OBJECT. Not app-only: a dashboard caller must not land a destructive edit on
    a replaced slot either.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    request = make_mocked_request(
        "POST",
        "/api/chat/slots/s1/edit-resend",
        match_info={"slot": "s1"},
        app=_make_regen_app(state),
    )
    request["app"] = ""

    async def _json():
        # The replacement lands while the body is being read.
        state._slots["s1"] = state.get_or_create_slot("s2")
        return {"index": 0, "content": "edited"}

    request.json = _json  # type: ignore[method-assign]

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        resp = await api_chat_slot_edit_resend(request)

    assert resp.status == 404
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


# ── edit-resend: a busy SESSION is not the same question as a busy slot ──
# ``discard_conversation`` is a full teardown: it drops the native conversation
# AND releases the shared sub-agent runtime. ``slot.running`` tracks only this
# slot's own task, so it answers False in both states below.


@pytest.mark.asyncio
async def test_edit_resend_refuses_while_subagents_are_attached(state) -> None:
    """The discard releases the shared runtime the parent's children run on, and
    the parent turn ends FIRST -- so ``running`` is False while they keep going.
    Without this guard an edit destroys work it has no part in."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    subs = MagicMock()
    subs.running_agents_for = MagicMock(return_value=["child-1"])
    subs._queued_depth = MagicMock(return_value=0)
    state.subagents = subs

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_subagents_running"

    # Probed on the session the discard would have torn down, not on the slot name.
    subs.running_agents_for.assert_called_with("dashboard:s1")
    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_when_the_subagent_probe_fails(state) -> None:
    """An unreadable probe is UNKNOWN children, not zero children. The shared
    predicate fails closed on a None running-probe; pinning it here keeps this
    endpoint from being the one that reads a failure as "safe to tear down"."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    subs = MagicMock()
    subs.running_agents_for = MagicMock(return_value=None)
    state.subagents = subs

    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "slot_subagents_running"

    state.sessions.discard_conversation.assert_not_awaited()


# ── edit-resend: the slot is reserved across the durable boundaries ──


@pytest.mark.asyncio
async def test_edit_resend_reserves_the_slot_so_a_concurrent_send_queues(state) -> None:
    """``slot.running`` must read True while the boundaries are pending.

    ``running`` derives from ``slot.task`` and the send path is not serialized on
    ``slot._lock``, so without the reservation a send arriving during the three
    awaited boundaries sees an idle slot, appends its row and dispatches a
    competing turn -- which the commit would then erase. Reserved, that send
    takes the queue path instead, and the commit leaves the entry alone.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    observed: dict = {}

    async def _discard(key, **kwargs):
        observed["running"] = slot.running
        observed["arrived_id"] = slot.queue_append("sent during the edit")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            # flake-ok: pre-existing one-yield barrier after the committed edit's 200, before reading whether the diverted send stayed queued, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    assert observed["running"] is True
    assert observed["arrived_id"] in [entry["id"] for entry in slot._queue]


@pytest.mark.asyncio
async def test_edit_resend_abort_hands_a_diverted_send_to_the_queue_drain(state) -> None:
    """A send diverted by the reservation must never be stranded.

    On abort no turn ran, so the entry the reservation pushed to the queue has
    no drain trigger of its own; the reserved task hands it to the canonical
    successor dispatch, which re-validates holds before starting anything.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    async def _discard(key, **kwargs):
        slot.queue_append("sent during the edit")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)
    state.sessions.aflush = AsyncMock(side_effect=OSError("map write failed"))
    drain = AsyncMock(return_value=True)

    with (
        patch("kiro_crew.dashboard.chat_regenerate._start_next_queued_turn", new=drain),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run,
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            await async_wait_until(
                lambda: drain.await_count, describe=lambda: "the queue drain was never handed off"
            )

    drain.assert_awaited_once()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_abort_leaves_a_pre_existing_queue_entry_waiting(state) -> None:
    """An entry queued BEFORE the reservation keeps its own trigger.

    Only a send DIVERTED by this reservation lost its drain, so an abort must not
    dispatch on behalf of work that was already waiting -- that would start a
    turn the user never unblocked.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot.queue_append("queued long before the edit")
    state.sessions.aflush = AsyncMock(side_effect=OSError("map write failed"))
    drain = AsyncMock(return_value=True)

    with (
        patch("kiro_crew.dashboard.chat_regenerate._start_next_queued_turn", new=drain),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run,
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            # flake-ok: pre-existing first yield of the two-yield window after the aborted edit's 503 in which a wrongful queue drain would run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
            # flake-ok: pre-existing second yield of that window, before asserting no queue drain ran, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    drain.assert_not_awaited()
    run.assert_not_awaited()
    assert len(slot._queue) == 1


# ── edit-resend: rows that arrive during the boundary belong to the new timeline ──


@pytest.mark.asyncio
async def test_edit_resend_commit_keeps_a_row_injected_during_the_boundary(state) -> None:
    """A workflow/cron completion landing mid-boundary must survive the commit.

    Those injectors append WITHOUT taking ``slot._lock`` -- ``workflow_inject``
    calls ``append_and_surface`` straight on the event loop -- so a wholesale
    ``slot.messages = prospective_slot.messages`` silently drops the injected
    row. The rewrite save cannot put it back either: a rewrite deliberately
    skips the cross-process-append scan, so carrying it in the live window is
    what keeps it. It reaches disk on the next ordinary flush, which is why the
    boundary itself writes exactly ONCE.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    saved_windows: list[list[str]] = []

    async def _discard(key, **kwargs):
        # Stands in for inject_workflow_result: an append on the live slot while
        # this handler holds slot._lock.
        slot.append("assistant", "workflow finished", "msg msg-a")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    def _save(_state, saved_slot, msgs_snapshot=None, **_kwargs):
        window = msgs_snapshot if msgs_snapshot is not None else saved_slot.messages
        saved_windows.append([m["content"] for m in window])
        return True

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history", side_effect=_save),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            # flake-ok: pre-existing one-yield barrier after the committed edit's 200, before reading the merged window and the save count, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    # The truncation still happened, the edit is live, and the injected row was
    # carried rather than replaced away.
    assert [m["content"] for m in slot.messages] == ["edited", "workflow finished"]
    assert "workflow finished" in [m["content"] for m in slot._pending]
    # Order is not accidental, and the claim is "never EARLIER" rather than
    # "strictly later". ``monotonic_transcript_ts`` only ever moves a row
    # forward, and it floors on the window tail the appender saw: the edited row
    # was floored on the (empty) truncated prefix, the arrived row on the
    # original tail. On a coarse clock -- Windows advances the system clock in
    # ~15.6 ms steps, which is exactly why that helper exists -- both reads of
    # ``now`` return the same instant and the two rows legitimately carry an
    # IDENTICAL ts. Asserting ``<`` passed on Linux and failed on Windows for
    # that reason. What must hold is that the merge never stamps the arrived row
    # BEFORE the edited one, which would reorder the transcript; list order is
    # what separates a tie, and the content assertion above pins that.
    # flake-ok: pre-existing tie-tolerant order check of two transcript stamps, which a tie satisfies, moved verbatim; tracked for a de-flake pass
    assert slot.messages[0]["ts"] <= slot.messages[1]["ts"]
    # The boundary writes ONCE, and the carried row is left to the ordinary flush.
    # A second guarded save for it would have to be awaited after the commit,
    # where ``dispatch_commit`` is already True and only ``dispatch_ready.set()``
    # remains -- so a rebind landing on that await would release this handler's
    # prompt against another conversation. ``_dirty`` is what makes the merged
    # window durable instead.
    assert saved_windows == [["edited"]]
    assert slot._dirty is True


@pytest.mark.asyncio
async def test_edit_resend_commit_does_not_resurrect_a_card_retired_meanwhile(state) -> None:
    """The prospective question map is PRE-await, so adopting it wholesale would
    restore a card an arrived row already retired -- re-rendering a card whose
    answer channel is gone. The commit intersects instead: retired by either
    side stays retired."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot._question_pending = {
        "q1": {"blocking": False, "prompt": "which host?"},
        "q2": {"blocking": False, "prompt": "which region?"},
    }
    announced: list = []
    slot._on_question_retired = lambda key, ids: announced.append((key, sorted(ids)))

    async def _discard(key, **kwargs):
        # An arrived user-role row retires every live non-blocking card.
        slot.append("user", "asked elsewhere")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            # flake-ok: pre-existing one-yield barrier after the committed edit's 200, before reading the question map and the retirement announcements, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    assert slot._question_pending == {}
    # The arrived append already announced the retirement; the commit must not
    # announce the same ids a second time.
    assert announced == [("s1", ["q1", "q2"])]


@pytest.mark.asyncio
async def test_edit_resend_commit_keeps_a_blocking_card_answered_meanwhile(state) -> None:
    """An answer landing mid-boundary must not be undone by the commit.

    Answering pops the id from the LIVE dict, so a commit that assigned the
    frozen pre-await copy back would restore the card with its answer channel
    already gone. A BLOCKING card is the shape that reaches this: an append never
    retires one, so the edit's own ``user`` row cannot be what cleared it.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    state.mark_question_pending("s1", blocking=True, card_id="ask-1")
    assert "ask-1" in slot._question_pending
    observed: dict = {}

    async def _discard(key, **kwargs):
        observed["cleared"] = state.clear_question_pending("s1", card_id="ask-1")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            # flake-ok: pre-existing one-yield barrier after the committed edit's 200, before reading whether the answered card stayed cleared, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    assert observed["cleared"] is True
    assert "ask-1" not in slot._question_pending, (
        "the commit resurrected a card that was answered during the boundary, "
        "so the slot awaits input against a completed round-trip"
    )


@pytest.mark.asyncio
async def test_edit_resend_commit_keeps_a_card_that_arrived_meanwhile(state) -> None:
    """A card marked mid-boundary must survive the commit.

    ``post_question_card`` is addressed by slot key, so a card can be marked on a
    slot whose turn is being replaced. It is in the LIVE dict and not in the
    frozen pre-await copy, so any commit keyed on that copy erases it -- and the
    client has already been shown it.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    async def _discard(key, **kwargs):
        state.mark_question_pending("s1", blocking=True, card_id="ask-late")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            # flake-ok: pre-existing one-yield barrier after the committed edit's 200, before reading whether the late card survived, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    assert "ask-late" in slot._question_pending, (
        "the commit erased a card that was raised during the boundary, so the "
        "client renders a card the server no longer believes is pending"
    )


# ── edit-resend: the commit re-checks the transcript it was authorized against ──


@pytest.mark.asyncio
async def test_edit_resend_refuses_the_commit_when_the_slot_is_rebound(state) -> None:
    """A slot rebound to another transcript mid-save must not be replaced.

    A cron injection can re-link the slot -- hydrating it with another
    conversation's state -- while the history write is in flight. The save's own
    ``expected_history_key`` guard cannot see it, because the snapshot froze the
    old routing; this loop-side re-check is the only fence that can, and without
    it the commit silently overwrites the injected conversation.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)

    async def _rebinding_discard(key, **kwargs):
        # The slot moves to another transcript while the edit persists. The
        # save is stubbed to accept the write, so ONLY the commit-side re-check
        # can refuse -- which is exactly what this pins.
        slot.linked_session_key = "slack:9876543210.999"
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_rebinding_discard)
    run = AsyncMock()

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            MagicMock(return_value=True),
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_slot_rebound"
            # flake-ok: pre-existing one-yield barrier after the refused commit's 503, before reading the unchanged window, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_the_commit_when_the_slot_is_replaced(state) -> None:
    """A close-and-recreate under the same name is a DIFFERENT conversation.

    The transcript key is unchanged by such a swap, so the rebind fence cannot
    see it; identity has to be the slot OBJECT -- the same discipline
    ``_reauthorize_after_await`` applies across the body-read await.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)

    async def _replacing_discard(key, **kwargs):
        # Same name, different object -- as a close-and-recreate produces.
        state._slots["s1"] = state.get_or_create_slot("s2")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_replacing_discard)
    run = AsyncMock()

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            MagicMock(return_value=True),
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_slot_rebound"
            # flake-ok: pre-existing one-yield barrier after the refused commit's 503, before reading the unchanged window, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_the_commit_when_the_reservation_is_displaced(state) -> None:
    """If something else took ``slot.task``, committing would run this handler's
    turn ALONGSIDE whatever now owns the slot -- two concurrent turns writing one
    window. The reservation must still be the slot's task at commit time."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    usurpers: list = []

    async def _displacing_discard(key, **kwargs):
        async def _other_turn() -> None:
            # flake-ok: pre-existing 10 s park of the stand-in usurping turn, which the finally block cancels, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 10 s park of the stand-in usurping turn, which the finally block cancels, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(10)

        # Another dispatcher claims the slot while the boundary is pending.
        usurpers.append(asyncio.create_task(_other_turn()))
        slot.task = usurpers[-1]
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_displacing_discard)
    run = AsyncMock()

    try:
        with (
            patch(
                "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
                MagicMock(return_value=True),
            ),
            patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
        ):
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
                )
                assert resp.status == 503
                assert (await resp.json())["code"] == "edit_resend_slot_rebound"
                # flake-ok: pre-existing one-yield barrier after the refused commit's 503, before reading the unchanged window, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0)

        assert slot.messages == original_messages
        run.assert_not_awaited()
    finally:
        for pending in usurpers:
            pending.cancel()
