"""A finished streamed turn must release its token rows, on every transport.

``_ChatSlot.append`` files each streamed ``chunk`` row into BOTH ``messages``
and ``_pending`` -- the same dict object in two lists. Every finalize path used
to rewrite ``messages`` alone, so ``_pending`` kept the only surviving reference
to the whole token stream. On the WebSocket transport (the dashboard's normal
one) nothing ever drains that queue during a turn: the SSE drain loop is
skipped, and the only WS-path drain runs at the START of the slot's NEXT turn.
A slot that finishes a long streamed turn and is then abandoned therefore held
its entire token stream for the process lifetime, and the slot count is not
capped, so the aggregate is unbounded.

The release cannot be unconditional. For an HTTP SSE reader (``/api/chat``) or
an OpenAI-compatible reader (``/v1/chat/completions``) ``_pending`` IS the
delivery queue, so a row still in it has NOT reached the client and dropping it
would truncate the answer mid-sentence. These tests pin both halves: the rows
are really gone when nobody can deliver them, and really kept when somebody
can.
"""

from __future__ import annotations

import asyncio
import gc
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_runner import _flush_segment
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService


def _stream(slot: _ChatSlot, tokens: list[str]) -> list[dict]:
    """Append tokens as the runner does and return the row objects it created."""
    for tok in tokens:
        slot.append("chunk", tok, "chunk", broadcast=False)
    return [m for m in slot.messages if m.get("role") == "chunk"]


def _slot_still_refers(slot: _ChatSlot, rows: list[dict]) -> bool:
    """True when either slot list is still a referrer of any of ``rows``.

    ``gc.get_referrers`` rather than ``weakref``: a plain dict is not
    weak-referenceable, and the question here is precisely whether the SLOT is
    the thing keeping the row alive.
    """
    owners = {id(slot.messages), id(slot._pending)}
    for row in rows:
        if any(id(ref) in owners for ref in gc.get_referrers(row)):
            return True
    return False


# ── WS transport: the rows must actually be released ──


def test_purge_chunks_releases_the_same_objects_from_both_lists(tmp_path):
    """The identity check: the exact dicts leave ``_pending``, not just ``messages``.

    Asserting on ``messages`` alone is what let the leak survive -- the window
    rewrite always looked correct.
    """
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")
    rows = _stream(slot, ["al", "pha", "-be", "ta"])
    assert len(rows) == 4
    assert [m for m in slot._pending if m.get("role") == "chunk"] == rows

    released = slot.purge_chunks()

    assert released == 4
    assert [m for m in slot.messages if m.get("role") == "chunk"] == []
    # Identity, not equality: a row that merely compares unequal could still be
    # one of the retained dicts under a different key.
    pending_ids = {id(m) for m in slot._pending}
    assert pending_ids.isdisjoint({id(r) for r in rows})


def test_released_chunk_rows_are_no_longer_owned_by_the_slot(tmp_path):
    """Neither slot list refers to the rows any more, so they are really freed."""
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")
    rows = _stream(slot, ["one", "two", "three"])
    assert _slot_still_refers(slot, rows) is True

    slot.purge_chunks()

    assert _slot_still_refers(slot, rows) is False


def test_abandoned_ws_slot_retains_nothing_after_a_normal_turn(tmp_path):
    """The SUCCESS path releases too -- the case an abandoned slot actually hits.

    ``_flush_segment`` rewrites the window by SLICE rather than by
    comprehension, so it is invisible to a search for the purge idiom while
    being the path a completed streamed turn normally takes.
    """
    state = _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")
    state._slots[slot.key] = slot
    rows = _stream(slot, ["The ", "answer ", "is ", "42."])

    _flush_segment(state, slot, "The answer is 42.", broadcast=False)

    # The turn's prose survives as one finalized assistant row...
    assert [m["role"] for m in slot.messages] == ["assistant"]
    assert slot.messages[0]["content"] == "The answer is 42."
    # ...and no chunk row is left anywhere on the slot.
    assert [m for m in slot._pending if m.get("role") == "chunk"] == []
    assert _slot_still_refers(slot, rows) is False


def test_release_is_idempotent_and_leaves_other_rows_alone(tmp_path):
    """Only ``chunk`` rows go; a queued wire frame or assistant row stays."""
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")
    slot.append("assistant", "kept", "msg msg-a", broadcast=False)
    _stream(slot, ["a", "b"])
    slot.push_wire_frame("context_usage", json.dumps({"pct": 1.0}))

    assert slot.purge_chunks() == 2
    assert slot.purge_chunks() == 0

    roles = [m.get("role") for m in slot._pending]
    assert "chunk" not in roles
    assert "assistant" in roles
    assert "context_usage" in roles


# ── SSE / OpenAI transports: undelivered rows must survive ──


def test_active_sse_reader_keeps_every_chunk(tmp_path):
    """``_has_reader`` refuses the release; the reader still drains all tokens."""
    state = _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")
    state._slots[slot.key] = slot
    slot._has_reader = True  # set by the /api/chat handler for a non-WS turn
    tokens = ["Str", "eam", "ed ", "out", "put"]
    _stream(slot, tokens)

    assert slot.purge_chunks() == 0

    delivered = [m["content"] for m in slot.drain() if m.get("role") == "chunk"]
    assert delivered == tokens


def test_attached_consumer_scope_keeps_every_chunk(tmp_path):
    """The OpenAI paths never set ``_has_reader``, so the counter must cover them."""
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")
    tokens = ["to", "ken", "s"]

    with slot.pending_consumer():
        assert slot._has_reader is False
        _stream(slot, tokens)
        assert slot.purge_chunks() == 0
        assert [m["content"] for m in slot._pending if m.get("role") == "chunk"] == tokens

    # Detaching retries the release the purge deferred, so the rows are ALREADY
    # gone here -- a second purge finds nothing left to free. Before the retry
    # existed this read 3, which is the leak: the refusal was final, so nothing
    # freed those rows until the slot happened to take another turn.
    assert slot.purge_chunks() == 0
    assert [m for m in slot._pending if m.get("role") == "chunk"] == []


def test_flush_segment_keeps_undelivered_chunks_for_a_reader(tmp_path):
    """The success path must not truncate a stream either."""
    state = _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")
    state._slots[slot.key] = slot
    tokens = ["par", "tial"]
    _stream(slot, tokens)

    with slot.pending_consumer():
        _flush_segment(state, slot, "partial", broadcast=False)
        assert [m["content"] for m in slot._pending if m.get("role") == "chunk"] == tokens


def test_consumer_scope_is_counted_not_boolean(tmp_path):
    """Two readers on one slot: the first to leave must not unlock the queue."""
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")

    with slot.pending_consumer():
        with slot.pending_consumer():
            assert slot._pending_consumers == 2
        assert slot._pending_consumers == 1
        assert slot.pending_has_consumer is True
    assert slot._pending_consumers == 0
    assert slot.pending_has_consumer is False


def test_consumer_scope_releases_on_exception(tmp_path):
    """A reader that dies mid-stream must not pin the queue forever."""
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")

    with pytest.raises(asyncio.CancelledError):
        with slot.pending_consumer():
            raise asyncio.CancelledError()

    assert slot.pending_has_consumer is False


# ── End-to-end: the OpenAI-compatible endpoint still returns every token ──


class _ReadyPrerequisite(KiroPrerequisiteService):
    async def session_ready(self) -> bool:
        return True

    # The endpoint fails closed on a FRESH probe, not on the latch.
    async def verified_ready(self, *, max_age_secs: float) -> bool:
        del max_age_secs
        return True


_READY_PREREQUISITE = object.__new__(_ReadyPrerequisite)


@pytest.mark.asyncio
async def test_openai_compat_response_survives_a_turn_end_purge(tmp_path):
    """A real turn that purges mid-flight must not lose the caller's answer.

    Exercises the handler, not the helpers: the consumer scope has to be armed
    around the turn dispatch, because the first ``await`` inside the response
    helper is what lets the turn run.
    """
    state = _make_state(tmp_path)
    slot = _ChatSlot(key="openai:slot-1")
    state._slots[slot.key] = slot
    state.get_or_create_slot = MagicMock(return_value=slot)

    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "model": "kiro",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
    )
    request.app = {"state": state, "kiro_prerequisite_service": _READY_PREREQUISITE}
    request.get = MagicMock(side_effect=lambda k, d="": d)
    request.remote = "127.0.0.1"

    tokens = ["Hello", ", ", "world", "!"]

    async def fake_run_chat(_state, sl, _prompt, *, _directive_user_origin):
        assert _directive_user_origin is True
        for tok in tokens:
            sl.append("chunk", tok, "chunk", broadcast=False)
        # The turn finalizes: this is the call that used to be a plain window
        # rewrite and is now also a queue release.
        sl.purge_chunks()
        sl.append("done", "", "done", broadcast=False)
        sl.event.set()

    from kiro_crew.dashboard import openai_compat

    with patch.object(openai_compat, "_run_chat", side_effect=fake_run_chat):
        resp = await openai_compat.api_completions(request)

    data = json.loads(resp.body)
    assert data["choices"][0]["message"]["content"] == "".join(tokens)


# ── A refused release must be RETRIED, not forgotten ──
#
# The guard makes the release conditional, which introduces a second way to
# leak: the turn-end purge lands while a reader still holds the queue, is
# correctly refused, and then nothing ever asks again. For the OpenAI-compatible
# and SSE transports that is every turn, so the fix would have moved the leak
# rather than closed it.


def test_a_refused_release_is_retried_when_the_last_consumer_detaches(tmp_path):
    """The reported hole: the last OpenAI consumer detaching must retry the release."""
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")

    with slot.pending_consumer():
        rows = _stream(slot, ["a", "b", "c"])
        assert slot.purge_chunks() == 0  # refused -- the reader may still need them
        assert slot._pending_release_deferred is True

    assert slot._pending_release_deferred is False
    assert [m for m in slot._pending if m.get("role") == "chunk"] == []
    assert not _slot_still_refers(slot, rows)


def test_a_refused_release_is_retried_when_the_sse_reader_flag_clears(tmp_path):
    """``_has_reader`` is the other signal that can lift the guard."""
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")
    slot._has_reader = True
    rows = _stream(slot, ["x", "y"])

    assert slot.purge_chunks() == 0
    assert slot._pending_release_deferred is True

    slot._has_reader = False  # the /api/chat loop clears this on 'done'

    assert [m for m in slot._pending if m.get("role") == "chunk"] == []
    assert not _slot_still_refers(slot, rows)


def test_nested_consumers_retry_only_when_the_last_one_leaves(tmp_path):
    """An inner reader detaching must not release rows the outer one still owns."""
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")

    with slot.pending_consumer():
        with slot.pending_consumer():
            _stream(slot, ["1", "2"])
            assert slot.purge_chunks() == 0
        # Inner scope gone, outer still attached: rows must survive.
        assert [m["content"] for m in slot._pending if m.get("role") == "chunk"] == ["1", "2"]
    assert [m for m in slot._pending if m.get("role") == "chunk"] == []


def test_a_release_never_refused_does_not_fire_on_detach(tmp_path):
    """No deferral means no surprise release -- a reader mid-turn keeps its queue."""
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")

    with slot.pending_consumer():
        _stream(slot, ["q", "r"])  # streamed, but no purge was ever attempted
        assert slot._pending_release_deferred is False

    # Nothing asked for a release, so detaching must not invent one: these rows
    # are still this turn's undelivered output.
    assert [m["content"] for m in slot._pending if m.get("role") == "chunk"] == ["q", "r"]


def test_a_consumer_that_drained_cleanly_has_nothing_to_retry(tmp_path):
    """The ordinary path: drained rows are already gone, retry is a no-op."""
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")

    with slot.pending_consumer():
        tokens = ["ok", "!"]
        _stream(slot, tokens)
        assert slot.purge_chunks() == 0
        delivered = [m["content"] for m in slot.drain() if m.get("role") == "chunk"]
        assert delivered == tokens

    assert slot._pending == []


def test_openai_compat_response_survives_a_deferred_then_retried_release(tmp_path):
    """End-to-end ordering: the client gets the full body, the slot keeps nothing."""
    _make_state(tmp_path)
    slot = _ChatSlot(key="dashboard:chat-1")

    with slot.pending_consumer():
        rows = _stream(slot, ["Hello", ", ", "world", "!"])
        slot.purge_chunks()
        body = "".join(m["content"] for m in slot.drain() if m.get("role") == "chunk")

    assert body == "Hello, world!"
    assert not _slot_still_refers(slot, rows)
