"""Coverage tests for ``kiro_crew.dashboard.chat_runner``: the merge cards a turn carries.

Covers ``adopt_alias_note_context``, which hands a turn the merge-card context queued on
another slot of its session, ``take_turn_context``, and the verbatim replays (after an
empty response, an image failure, a busy prompt, an owed completion, a refusal or a
thinking-binding rebuild) that carry a card exactly once.
``test_chat_runner_coverage.py`` describes the two harnesses the family uses; the
shared builders are in ``chat_runner_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import (
    _async_iter,
    _complete,
    _drive,
    _quiet_sel,
    _runner_state,
    _set_stream,
    _settle,
    _slot,
    _state,
)

from kiro_crew.acp.client import AcpConversationBindingMismatch
from kiro_crew.acp.types import EVENT_TEXT_CHUNK
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.base import LLMEvent
from kiro_crew.testing.wait import await_until


def _adopt(slot: _ChatSlot, slots: list[_ChatSlot]) -> int:
    """``adopt_alias_note_context`` for a slot that observed its own identity.

    The turn resolves the identity once (``resolve_turn_transcript_identity``)
    and hands it to the mover; a slot that has saved or hydrated carries it as
    ``_disk_meta_created_at``, which is what these direct calls pass.
    """
    return chat_runner.adopt_alias_note_context(
        slot, slots, slot_created_at=slot._disk_meta_created_at
    )


class TestAdoptAliasNoteContext:
    """A merge card queued on one slot of a session reaches the turn another slot runs."""

    @staticmethod
    def _aliases():
        # A parent session open in two slots: a ``workflow-<run_id>`` slot bound
        # to the chat's session and the chat itself, minted beside it by a create
        # that names the chat's key.
        first, twin = _slot("chat-cov-first"), _slot("chat-cov-twin")
        twin.linked_session_key = chat_runner.effective_session_key(first)
        first._disk_meta_created_at = twin._disk_meta_created_at = "2026-10-01T12:00:00+00:00"
        return first, twin

    @staticmethod
    def _card(content: str, session: str, *, source: str = "fork merge", n: int = 0) -> dict:
        """A merge card's context half, as the flush stamps it."""
        return {
            "content": content,
            "source": source,
            "noteSession": session,
            "noteId": f"card-{n}",
            "noteTranscript": "dashboard:chat-cov-first",
        }

    def test_a_card_queued_on_the_other_slot_is_drained_by_this_turn(self):
        first, twin = self._aliases()
        session = chat_runner.effective_session_key(first)
        first._pending_context = [
            self._card("merged summary", session),
            {"content": "panel state", "source": "panel"},
        ]

        assert _adopt(twin, [first, twin]) == 1
        out = chat_runner.drain_pending_context(twin)

        assert "merged summary" in out
        # Context with no session stamp is the first slot's own.
        assert first._pending_context == [{"content": "panel state", "source": "panel"}]

    def test_a_duplicate_card_already_on_the_turn_slot_is_not_moved(self):
        first, twin = self._aliases()
        session = chat_runner.effective_session_key(twin)
        queued = self._card("merged summary", session)
        queued["noteTranscript"] = "slack:123.456"
        duplicate = dict(queued, noteTranscript="slack_123.456")
        twin._pending_context = [queued]
        first._pending_context = [duplicate]

        assert _adopt(twin, [first, twin]) == 0
        assert sum(entry.get("noteId") == queued["noteId"] for entry in twin._pending_context) == 1
        assert first._pending_context == []

    def test_duplicate_cards_on_two_holders_are_moved_once(self):
        first, twin = self._aliases()
        session = chat_runner.effective_session_key(twin)
        second_holder = _slot("chat-cov-second-holder")
        second_holder.linked_session_key = session
        second_holder._disk_meta_created_at = twin._disk_meta_created_at
        first_card = self._card("merged summary", session)
        first_card["noteTranscript"] = "slack:123.456"
        duplicate = dict(first_card, noteTranscript="slack_123.456")
        first._pending_context = [first_card]
        second_holder._pending_context = [duplicate]

        assert _adopt(twin, [first, second_holder, twin]) == 1
        assert (
            sum(entry.get("noteId") == first_card["noteId"] for entry in twin._pending_context) == 1
        )
        assert first._pending_context == []
        assert second_holder._pending_context == []

    def test_a_plain_note_stays_on_the_slot_it_was_queued_on(self):
        first, twin = self._aliases()
        session = chat_runner.effective_session_key(first)
        # A /note's context half names its session but carries no card identity.
        note = {"content": "plain note", "source": "note", "noteSession": session}
        first._pending_context = [note]

        assert _adopt(twin, [first, twin]) == 0
        assert first._pending_context == [note]
        assert twin._pending_context == []
        assert "plain note" in chat_runner.drain_pending_context(first)

    def test_a_card_for_another_session_stays_where_it_was_queued(self):
        first, twin = self._aliases()
        card = self._card("elsewhere", "dashboard:other")
        first._pending_context = [card]

        assert _adopt(twin, [first, twin]) == 0
        assert first._pending_context == [card]
        assert twin._pending_context == []

    def test_a_card_never_crosses_transcript_incarnations(self):
        first, twin = self._aliases()
        first._disk_meta_created_at = "2026-10-01T12:00:01+00:00"
        card = self._card("deleted chat", chat_runner.effective_session_key(first))
        first._pending_context = [card]

        assert _adopt(twin, [first, twin]) == 0
        assert first._pending_context == [card]
        assert twin._pending_context == []

    def test_an_empty_identity_on_either_side_fails_closed(self):
        """The rule's owner refuses an unproven incarnation whichever side lacks it:
        a turn slot handed no identity, and a holder that observed none."""
        first, twin = self._aliases()
        session = chat_runner.effective_session_key(first)
        card = self._card("unproven", session)
        first._pending_context = [card]

        assert chat_runner.adopt_alias_note_context(twin, [first, twin], slot_created_at="") == 0
        assert first._pending_context == [card]

        first._disk_meta_created_at = ""
        assert (
            chat_runner.adopt_alias_note_context(
                twin, [first, twin], slot_created_at="2026-10-01T12:00:00+00:00"
            )
            == 0
        )
        assert first._pending_context == [card]
        assert twin._pending_context == []

    def test_a_card_never_crosses_an_app_boundary(self):
        first, twin = self._aliases()
        first._app = "some-app"
        session = chat_runner.effective_session_key(first)
        card = self._card("app card", session)
        first._pending_context = [card]

        assert _adopt(twin, [first, twin]) == 0
        assert first._pending_context == [card]

    def test_a_source_at_its_cap_leaves_the_rest_on_the_other_slot(self):
        first, twin = self._aliases()
        session = chat_runner.effective_session_key(first)
        cap = chat_runner._MAX_CONTEXT_PER_SOURCE

        def card(n: int) -> dict:
            return self._card(f"card {n}", session, source="watch", n=n)

        twin._pending_context = [card(n) for n in range(4)]
        first._pending_context = [card(n) for n in range(4, 4 + cap)]

        assert _adopt(twin, [first, twin]) == cap - 4
        assert [e["content"] for e in twin._pending_context] == [f"card {n}" for n in range(cap)]
        # The newest wait on their own slot for a later turn, in order.
        assert [e["content"] for e in first._pending_context] == [
            f"card {n}" for n in range(cap, cap + 4)
        ]

    def test_a_full_queue_leaves_the_rest_on_the_other_slot(self):
        first, twin = self._aliases()
        session = chat_runner.effective_session_key(first)
        cap = chat_runner._MAX_PENDING_CONTEXT
        # One source each, so only the queue's total binds.
        twin._pending_context = [{"content": f"own {n}", "source": f"s{n}"} for n in range(cap - 2)]
        first._pending_context = [
            self._card(f"card {n}", session, source=f"t{n}", n=n) for n in range(5)
        ]

        assert _adopt(twin, [first, twin]) == 2
        assert len(twin._pending_context) == cap
        assert [e["content"] for e in first._pending_context] == ["card 2", "card 3", "card 4"]

    def test_an_expired_card_stays_for_its_own_slot_to_discard(self):
        first, twin = self._aliases()
        session = chat_runner.effective_session_key(first)
        stale = self._card("stale", session, source="watch")
        first._pending_context = [{**stale, "injectedAt": 0, "maxAge": 1}]

        assert _adopt(twin, [first, twin]) == 0
        assert [e["content"] for e in first._pending_context] == ["stale"]
        assert twin._pending_context == []

    def test_an_expired_entry_holds_no_place_in_its_sources_cap(self):
        first, twin = self._aliases()
        session = chat_runner.effective_session_key(first)
        cap = chat_runner._MAX_CONTEXT_PER_SOURCE
        twin._pending_context = [
            {"content": f"dead {n}", "source": "watch", "injectedAt": 0, "maxAge": 1}
            for n in range(cap)
        ]
        first._pending_context = [self._card("live", session, source="watch")]

        assert _adopt(twin, [first, twin]) == 1
        assert twin._pending_context[-1]["content"] == "live"

    def test_expired_entries_hold_no_place_in_the_queue_cap(self):
        first, twin = self._aliases()
        session = chat_runner.effective_session_key(first)
        twin._pending_context = [
            {"content": f"dead {n}", "source": f"dead-{n}", "injectedAt": 0, "maxAge": 1}
            for n in range(chat_runner._MAX_PENDING_CONTEXT)
        ]
        first._pending_context = [self._card("live merge", session)]

        assert _adopt(twin, [first, twin]) == 1
        assert twin._pending_context[-1]["content"] == "live merge"
        assert first._pending_context == []


class TestTakeTurnContext:
    """A turn moves context between slots and reports cards consumed only when its session holds a merge card."""

    @staticmethod
    def _card(content: str, session: str, *, n: int = 0) -> dict:
        return {
            "content": content,
            "source": "fork merge",
            "noteSession": session,
            "noteId": f"card-{n}",
            "noteTranscript": "dashboard:chat-cov-first",
        }

    @staticmethod
    def _register(state: DashboardState, *slots: _ChatSlot) -> None:
        for slot in slots:
            state._slots[slot.key] = slot

    def test_a_turn_whose_session_holds_no_card_changes_no_other_queue(self, tmp_path, monkeypatch):
        """The ordinary turn: no merge card anywhere on its session, while a slot of
        another session holds one. The drain is the slot's own and the other
        slot's queue is left untouched."""
        state = _state(tmp_path)
        own, other = _slot("chat-cov-own"), _slot("chat-cov-other")
        own._pending_context = [{"content": "panel state", "source": "panel"}]
        other_queue = [
            self._card("someone else's merge", chat_runner.effective_session_key(other)),
            {"content": "their note", "source": "note"},
        ]
        other._pending_context = other_queue
        self._register(state, own, other)
        control = _slot("chat-cov-control")
        control._pending_context = [dict(entry) for entry in own._pending_context]

        def never(*args, **kwargs):
            raise AssertionError("a turn whose session holds no card moved context between slots")

        monkeypatch.setattr(chat_runner, "adopt_alias_note_context", never)

        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, own, transcript_identity=own._disk_meta_created_at
        )

        assert prefix == chat_runner.drain_pending_context(control)
        assert "panel state" in prefix
        assert consumed == []
        assert own._pending_context == []
        assert other._pending_context is other_queue
        assert other_queue == [
            self._card("someone else's merge", chat_runner.effective_session_key(other)),
            {"content": "their note", "source": "note"},
        ]

    def test_a_card_on_the_other_slot_of_the_session_is_taken_and_reported(self, tmp_path):
        state = _state(tmp_path)
        first, twin = _slot("chat-cov-first"), _slot("chat-cov-twin")
        twin.linked_session_key = chat_runner.effective_session_key(first)
        first._disk_meta_created_at = twin._disk_meta_created_at = "2026-10-01T12:00:00+00:00"
        card = self._card("merged summary", chat_runner.effective_session_key(first))
        first._pending_context = [card, {"content": "panel state", "source": "panel"}]
        self._register(state, first, twin)

        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, twin, transcript_identity=twin._disk_meta_created_at
        )

        assert "merged summary" in prefix
        assert consumed == [card] and consumed[0] is card
        assert twin._pending_context == []
        assert first._pending_context == [{"content": "panel state", "source": "panel"}]

    def test_a_duplicate_card_on_a_holder_is_removed_and_consumed_once(self, tmp_path):
        state = _state(tmp_path)
        first, twin = _slot("chat-cov-first"), _slot("chat-cov-twin")
        twin.linked_session_key = chat_runner.effective_session_key(first)
        first._disk_meta_created_at = twin._disk_meta_created_at = "2026-10-01T12:00:00+00:00"
        session = chat_runner.effective_session_key(first)
        queued = self._card("merged summary", session)
        queued["noteTranscript"] = "slack:123.456"
        duplicate = dict(queued, noteTranscript="slack_123.456")
        twin._pending_context = [queued]
        first._pending_context = [duplicate]
        self._register(state, first, twin)

        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, twin, transcript_identity=twin._disk_meta_created_at
        )

        assert prefix.count("merged summary") == 1
        assert consumed == [queued]
        assert first._pending_context == []
        assert twin._pending_context == []

    def test_a_card_the_turn_slot_cannot_deliver_does_not_block_the_holders_copy(self, tmp_path):
        """The turn slot's own copy names a session its rebinding left behind, so the
        drain drops it. It must not count as the copy already queued, or the holder's
        deliverable copy is removed without being moved and the card reaches no prompt."""
        state = _state(tmp_path)
        first, twin = _slot("chat-cov-first"), _slot("chat-cov-twin")
        twin.linked_session_key = chat_runner.effective_session_key(first)
        first._disk_meta_created_at = twin._disk_meta_created_at = "2026-10-01T12:00:00+00:00"
        session = chat_runner.effective_session_key(first)
        stale = self._card("stale summary", "dashboard:chat-cov-before-rebind")
        stale["noteTranscript"] = "slack:123.456"
        live = dict(stale, content="merged summary", noteSession=session)
        live["noteTranscript"] = "slack_123.456"
        twin._pending_context = [stale]
        first._pending_context = [live]
        self._register(state, first, twin)

        prefix, _without_cards, _consumed = chat_runner.take_turn_context(
            state, twin, transcript_identity=twin._disk_meta_created_at
        )

        assert prefix.count("merged summary") == 1
        assert "stale summary" not in prefix
        assert first._pending_context == []
        assert twin._pending_context == []

    def test_a_duplicate_card_in_one_queue_is_framed_once_while_plain_context_repeats(self):
        own = _slot("chat-cov-own")
        session = chat_runner.effective_session_key(own)
        first = self._card("merged summary", session)
        first["noteTranscript"] = "slack:123.456"
        duplicate = dict(first, noteTranscript="slack_123.456")
        plain = {"content": "same plain context", "source": "panel"}
        own._pending_context = [first, duplicate, plain, dict(plain)]

        prefix = chat_runner.drain_pending_context(own)

        assert prefix.count("merged summary") == 1
        assert prefix.count("same plain context") == 2
        assert own._pending_context == []

    def test_a_card_on_the_turn_slot_is_consumed_whatever_session_it_names(self, tmp_path):
        """A card the slot's rebinding made another session's is dropped by the drain,
        and the drop is this turn's consumption of it, so the slot's own queue counts
        by card identity alone."""
        state = _state(tmp_path)
        own = _slot("chat-cov-own")
        card = self._card("foreign summary", "dashboard:chat-cov-before-rebind")
        own._pending_context = [card]
        self._register(state, own)

        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, own, transcript_identity=own._disk_meta_created_at
        )

        assert "foreign summary" not in prefix
        assert consumed == [card]
        assert own._pending_context == []

    def test_a_card_that_stays_on_the_other_slot_is_not_reported_consumed(self, tmp_path):
        state = _state(tmp_path)
        first, twin = _slot("chat-cov-first"), _slot("chat-cov-twin")
        twin.linked_session_key = chat_runner.effective_session_key(first)
        first._disk_meta_created_at = twin._disk_meta_created_at = "2026-10-01T12:00:00+00:00"
        session = chat_runner.effective_session_key(first)
        cap = chat_runner._MAX_CONTEXT_PER_SOURCE
        twin._pending_context = [self._card(f"own {n}", session, n=n) for n in range(cap)]
        waiting = self._card("waits", session, n=cap)
        first._pending_context = [waiting]
        self._register(state, first, twin)

        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, twin, transcript_identity=twin._disk_meta_created_at
        )

        assert "waits" not in prefix
        assert len(consumed) == cap and waiting not in consumed
        assert first._pending_context == [waiting]

    @staticmethod
    def _twins_with_a_transcript(state: DashboardState) -> tuple[_ChatSlot, _ChatSlot, str]:
        """A hydrated holder and a chat minted beside it that has observed nothing yet.

        The transcript is on disk and the holder carries its ``created_at``; the
        minted twin's ``_disk_meta_created_at`` is still ``""``, as after
        ``api_chat_slot_create``, before its first save.
        """
        first, twin = _slot("chat-cov-first"), _slot("chat-cov-twin")
        twin.linked_session_key = chat_runner.effective_session_key(first)
        key = chat_runner.slot_history_key(twin)
        assert key == chat_runner.slot_history_key(first)
        state.conversation_log.append(key, "user", "the conversation so far")
        identity = state.conversation_log.thread_transcript_identity(key)
        assert identity
        first._disk_meta_created_at = identity
        assert twin._disk_meta_created_at == ""
        return first, twin, key

    @pytest.mark.asyncio
    async def test_a_turn_slot_that_observed_no_identity_reads_it_from_its_transcript(
        self, tmp_path
    ):
        """The minted chat's first turn: the fence opens on the identity the
        conversation log reports for the slot's own transcript, so the card is
        not held back until the slot's first save."""
        state = _state(tmp_path)
        first, twin, key = self._twins_with_a_transcript(state)
        card = self._card("merged summary", chat_runner.effective_session_key(first))
        first._pending_context = [card]
        self._register(state, first, twin)

        identity = await chat_runner.resolve_turn_transcript_identity(state, twin)
        assert identity == first._disk_meta_created_at
        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, twin, transcript_identity=identity
        )

        assert "merged summary" in prefix
        assert consumed == [card]
        assert first._pending_context == []
        assert twin._pending_context == []
        # The read resolves the slot's identity; it does not hydrate the slot.
        assert twin._disk_meta_created_at == ""

    @pytest.mark.asyncio
    async def test_a_turn_slot_whose_transcript_was_deleted_takes_nothing(
        self, tmp_path, monkeypatch
    ):
        """The reported hole, at the mover: after a permanent delete the log reports
        no identity for the recycled key (the warm memo goes with the file), and
        once a replacement file exists its ``created_at`` is a new one."""
        state = _state(tmp_path)
        monkeypatch.setattr(
            "kiro_crew.history.metadata_now_iso", lambda: "2026-10-01T12:00:00+00:00"
        )
        first, twin, key = self._twins_with_a_transcript(state)
        card = self._card("deleted chat", chat_runner.effective_session_key(first))
        first._pending_context = [card]
        self._register(state, first, twin)

        assert state.conversation_log.delete_session(key) is True
        assert state.conversation_log.thread_transcript_identity(key) is None
        identity = await chat_runner.resolve_turn_transcript_identity(state, twin)
        assert identity == ""
        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, twin, transcript_identity=identity
        )
        assert "deleted chat" not in prefix
        assert consumed == []
        assert first._pending_context == [card]

        monkeypatch.setattr(
            "kiro_crew.history.metadata_now_iso", lambda: "2026-10-01T12:00:01+00:00"
        )
        state.conversation_log.append(key, "user", "new conversation")
        replacement_identity = state.conversation_log.thread_transcript_identity(key)
        assert replacement_identity and replacement_identity != first._disk_meta_created_at
        identity = await chat_runner.resolve_turn_transcript_identity(state, twin)
        assert identity == replacement_identity
        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, twin, transcript_identity=identity
        )
        assert "deleted chat" not in prefix
        assert consumed == []
        assert first._pending_context == [card]
        assert twin._pending_context == []

    @pytest.mark.asyncio
    async def test_an_observed_identity_is_not_read_from_disk_again(self, tmp_path, monkeypatch):
        state = _state(tmp_path)
        first, twin, key = self._twins_with_a_transcript(state)
        twin._disk_meta_created_at = first._disk_meta_created_at
        first._pending_context = [self._card("held", chat_runner.effective_session_key(first))]
        self._register(state, first, twin)

        def never(_key):
            raise AssertionError("a slot that observed its identity read the transcript")

        monkeypatch.setattr(state.conversation_log, "thread_transcript_identity", never)

        identity = await chat_runner.resolve_turn_transcript_identity(state, twin)
        assert identity == first._disk_meta_created_at

    @pytest.mark.asyncio
    async def test_an_expired_card_on_another_slot_is_not_a_holder(self, tmp_path, monkeypatch):
        """An expired card is not a holder: the mover refuses it
        (``context_entry_expired``) and nothing is reported consumed, so a slot
        whose only card for this session has expired resolves ``""`` with no read,
        and the drain leaves the card for its own slot to discard."""
        state = _state(tmp_path)
        first, twin, key = self._twins_with_a_transcript(state)
        stale = self._card("stale merge", chat_runner.effective_session_key(first))
        first._pending_context = [{**stale, "injectedAt": 0, "maxAge": 1}]
        self._register(state, first, twin)

        def never(_key):
            raise AssertionError("a turn whose only holder entry expired read the transcript")

        monkeypatch.setattr(state.conversation_log, "thread_transcript_identity", never)

        assert chat_runner._session_card_holders(state, twin) == []
        identity = await chat_runner.resolve_turn_transcript_identity(state, twin)
        assert identity == ""
        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, twin, transcript_identity=identity
        )

        assert "stale merge" not in prefix
        assert consumed == []
        assert [e["content"] for e in first._pending_context] == ["stale merge"]
        assert twin._pending_context == []

    @pytest.mark.asyncio
    async def test_a_state_without_a_conversation_log_or_a_legacy_transcript_resolves_no_identity(
        self, tmp_path
    ):
        """Finding 4: a metadata line with no ``created_at`` opens nothing, which is
        vacuous because the merge route queues no card on such a session; and a
        state built without a log has no transcript to read."""
        state = _state(tmp_path)
        first, twin = _slot("chat-cov-first"), _slot("chat-cov-twin")
        twin.linked_session_key = chat_runner.effective_session_key(first)
        first._pending_context = [self._card("held", chat_runner.effective_session_key(first))]
        self._register(state, first, twin)
        key = chat_runner.slot_history_key(twin)
        path = state.conversation_log._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"_type": "metadata"}) + "\n", encoding="utf-8")
        assert state.conversation_log.get_metadata(key) == {"_type": "metadata"}

        assert await chat_runner.resolve_turn_transcript_identity(state, twin) == ""

        state.conversation_log = None
        assert await chat_runner.resolve_turn_transcript_identity(state, twin) == ""

    @pytest.mark.asyncio
    async def test_the_ordinary_turn_reads_no_transcript_metadata(self, tmp_path, monkeypatch):
        """The identity is read only when a same-app slot holds a card for this
        session: a turn with no such holder, including one beside a holder for
        another session, resolves ``""`` without reading the log, and the drain
        itself never reads it."""
        state = _state(tmp_path)
        own, other = _slot("chat-cov-own"), _slot("chat-cov-other")
        own._pending_context = [
            {"content": "panel state", "source": "panel"},
            self._card("own card", chat_runner.effective_session_key(own)),
        ]
        other._pending_context = [
            self._card("someone else's merge", chat_runner.effective_session_key(other))
        ]
        self._register(state, own, other)

        def never(_key):
            raise AssertionError("an ordinary turn read transcript metadata")

        monkeypatch.setattr(state.conversation_log, "thread_transcript_identity", never)

        identity = await chat_runner.resolve_turn_transcript_identity(state, own)
        assert identity == ""
        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, own, transcript_identity=identity
        )

        assert "panel state" in prefix and "own card" in prefix
        assert len(consumed) == 1
        assert len(other._pending_context) == 1

    def test_the_given_identity_and_exactly_the_holder_list_reach_the_mover(
        self, tmp_path, monkeypatch
    ):
        """One owner of the rule: the drain compares nothing itself and reads no
        metadata; the identity its caller resolved and exactly the holder slots
        (``_session_card_holders``) reach the mover, which is the only place that
        compares them, so there is no second predicate for the two to drift on."""
        state = _state(tmp_path)
        first, twin, key = self._twins_with_a_transcript(state)
        bystander = _slot("chat-cov-bystander")
        bystander._pending_context = [{"content": "their note", "source": "note"}]
        card = self._card("merged summary", chat_runner.effective_session_key(first))
        first._pending_context = [card]
        self._register(state, first, twin, bystander)

        def never(_key):
            raise AssertionError("the drain read transcript metadata")

        monkeypatch.setattr(state.conversation_log, "thread_transcript_identity", never)
        handed: list[tuple[object, list[_ChatSlot], dict]] = []
        real_adopt = chat_runner.adopt_alias_note_context

        def recording_adopt(slot_arg, slots_arg, **kwargs):
            handed.append((slot_arg, list(slots_arg), kwargs))
            return real_adopt(slot_arg, slots_arg, **kwargs)

        monkeypatch.setattr(chat_runner, "adopt_alias_note_context", recording_adopt)
        assert chat_runner._session_card_holders(state, twin) == [(first, [card])]

        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, twin, transcript_identity=first._disk_meta_created_at
        )

        assert handed == [(twin, [first], {"slot_created_at": first._disk_meta_created_at})]
        assert "merged summary" in prefix
        assert consumed == [card]
        assert bystander._pending_context == [{"content": "their note", "source": "note"}]

    @pytest.mark.usefixtures("close_skills_loaders")
    @pytest.mark.asyncio
    async def test_the_fallback_read_runs_off_the_event_loop(self, tmp_path, monkeypatch):
        """The minted chat's first turn, through ``_run_chat``: the read of its
        transcript's ``created_at`` (a stat and, on a memo miss, an open) runs in a
        worker thread, not on the loop. Inside the read ``asyncio.get_running_loop()``
        raises and the reading thread is not the loop's, and the card it admits
        reaches the prompt."""
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        state, client = _runner_state(tmp_path, context_builder=builder)
        client.client = MagicMock(pop_pending_oauth_requests=MagicMock(return_value=[]))
        first, twin, key = self._twins_with_a_transcript(state)
        card = self._card("merged summary", chat_runner.effective_session_key(first))
        first._pending_context = [card]
        self._register(state, first, twin)
        loop_thread = threading.get_ident()
        reads: list[tuple[bool, bool]] = []
        real_read = state.conversation_log.thread_transcript_identity

        def recording_read(read_key):
            try:
                asyncio.get_running_loop()
                on_loop = True
            except RuntimeError:
                on_loop = False
            reads.append((on_loop, threading.get_ident() == loop_thread))
            return real_read(read_key)

        monkeypatch.setattr(state.conversation_log, "thread_transcript_identity", recording_read)
        _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), _complete()])

        with patch.object(
            chat_runner, "generate_session_summary", new=AsyncMock(return_value=None)
        ):
            await _drive(state, twin, "first message")
            await await_until(
                asyncio.gather(*list(state._background_tasks), return_exceptions=True),
                what="the moved-card background task cleanup",
            )

        assert reads == [(False, False)], reads
        prompt = client.stream.call_args_list[0].args[0]
        assert "merged summary" in prompt
        assert first._pending_context == []

    def test_a_card_the_mover_refuses_is_not_reported_consumed(self, tmp_path):
        """The turn hands the mover every card a same-app slot holds for its session
        and lets the rule decide; a refused card stays in its holder's queue and
        ``_consumed_merge_contexts`` leaves it out."""
        state = _state(tmp_path)
        first, twin = _slot("chat-cov-first"), _slot("chat-cov-twin")
        twin.linked_session_key = chat_runner.effective_session_key(first)
        first._disk_meta_created_at = "2026-10-01T12:00:01+00:00"
        twin._disk_meta_created_at = "2026-10-01T12:00:00+00:00"
        card = self._card("deleted chat", chat_runner.effective_session_key(first))
        first._pending_context = [card]
        self._register(state, first, twin)

        prefix, _without_cards, consumed = chat_runner.take_turn_context(
            state, twin, transcript_identity=twin._disk_meta_created_at
        )

        assert prefix == ""
        assert consumed == []
        assert first._pending_context == [card]
        assert twin._pending_context == []


# ── verbatim replay of a turn that drained a merge card ──────────────────


@pytest.mark.usefixtures("close_skills_loaders")
class TestVerbatimReplayMergeCards:
    """A turn that does not land puts its merge-card context back on the queue,
    so a verbatim replay of its message must not carry the card's frame as
    well: the replay turn drains the restored entry and would otherwise hand
    the model the same card twice in one prompt."""

    CARD = "merged summary of the fork"
    PLAIN = "panel state"
    COMPLETION = "[Subagent completion event] the child finished"

    @classmethod
    def _queue_card_and_plain(cls, state: DashboardState, slot: _ChatSlot) -> None:
        slot._pending_context = [
            {
                "content": cls.CARD,
                "source": "fork merge",
                "noteSession": chat_runner.effective_session_key(slot),
                "noteId": "card-replay",
                "noteTranscript": "dashboard:chat-cov-1",
            },
            {"content": cls.PLAIN, "source": "panel"},
        ]
        state._slots[slot.key] = slot

    @staticmethod
    def _script(client, *turns) -> None:
        """Script ``client.stream`` turn by turn; a turn given an exception raises it."""
        calls = {"n": 0}

        async def _raising(exc):
            raise exc
            yield  # pragma: no cover

        def _stream(*_args, **_kwargs):
            calls["n"] += 1
            turn = turns[min(calls["n"], len(turns)) - 1]
            if isinstance(turn, BaseException):
                return _raising(turn)
            return _async_iter(turn)

        client.stream = MagicMock(side_effect=_stream)

    @classmethod
    def _state_with_context_builder(cls, tmp_path):
        """The pending-context drain runs on the context-builder prompt path only."""
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        state, client = _runner_state(tmp_path, context_builder=builder)
        client.client = MagicMock(pop_pending_oauth_requests=MagicMock(return_value=[]))
        slot = _slot()
        cls._queue_card_and_plain(state, slot)
        return state, client, slot

    @staticmethod
    def _delivered(prompt: str) -> str:
        """The prompt past any replayed conversation history: what this turn delivers."""
        history_end = "[END CONVERSATION HISTORY]"
        if history_end in prompt:
            return prompt[prompt.rindex(history_end) + len(history_end) :]
        return prompt

    @staticmethod
    async def _run_and_await_replay(state, slot, message: str, **run_kwargs: object) -> None:
        with (
            _quiet_sel(),
            patch.object(chat_runner, "generate_session_summary", new=AsyncMock(return_value=None)),
        ):
            await chat_runner._run_chat(state, slot, message, **run_kwargs)
            replay = slot.task
            assert replay is not None, "the empty turn queued no replay"
            # The replay's own exception is the error arm's to report, so it
            # returns as a result here; an expired bound still fails by name.
            await await_until(
                asyncio.gather(replay, return_exceptions=True),
                what="the queued replay completion",
            )
            await _settle(slot)
            await await_until(
                asyncio.gather(*list(state._background_tasks), return_exceptions=True),
                what="the replay background task cleanup",
            )

    def test_the_prefix_without_cards_is_the_drain_of_the_other_entries(self, tmp_path):
        """Pins ``prefix_without_cards`` to the drain of the same queue without its card."""
        state = _state(tmp_path)
        whole, plain_only = _slot("chat-cov-whole"), _slot("chat-cov-plain")
        card = {
            "content": self.CARD,
            "source": "fork merge",
            "noteSession": "dashboard:chat-cov-whole",
            "noteId": "card-frame",
            "noteTranscript": "dashboard:chat-cov-whole",
        }
        plain = {"content": self.PLAIN, "source": "panel"}
        whole._pending_context = [plain, card, dict(plain, content="second panel")]
        plain_only._pending_context = [plain, dict(plain, content="second panel")]
        state._slots[whole.key] = whole

        taken = chat_runner.take_turn_context(
            state, whole, transcript_identity=whole._disk_meta_created_at
        )

        assert taken.prefix_without_cards == chat_runner.drain_pending_context(plain_only)
        assert taken.consumed == [card]
        assert taken.prefix.count(self.CARD) == 1
        assert self.CARD not in taken.prefix_without_cards

    def test_a_queue_without_a_card_strips_nothing(self, tmp_path):
        state = _state(tmp_path)
        slot = _slot("chat-cov-plain")
        slot._pending_context = [{"content": self.PLAIN, "source": "panel"}]
        state._slots[slot.key] = slot

        taken = chat_runner.take_turn_context(
            state, slot, transcript_identity=slot._disk_meta_created_at
        )

        assert taken.prefix_without_cards == taken.prefix
        assert self.PLAIN in taken.prefix
        assert taken.consumed == []

    @pytest.mark.asyncio
    async def test_an_empty_first_response_replays_the_card_once(self, tmp_path):
        state, client, slot = self._state_with_context_builder(tmp_path)
        self._script(
            client,
            [_complete()],
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), _complete()],
        )

        await self._run_and_await_replay(state, slot, "what landed?")

        prompts = [self._delivered(call.args[0]) for call in client.stream.call_args_list]
        assert len(prompts) == 2, prompts
        first, replay = prompts
        assert first.count(self.CARD) == 1 and first.count(self.PLAIN) == 1
        assert replay.count(self.CARD) == 1, replay
        assert replay.count(self.PLAIN) == 1, replay
        assert replay.endswith("what landed?")
        # The landed replay consumed the card's context: nothing is left for a later turn.
        assert slot._pending_context == []
        assert slot._inflight_merge_contexts == []

    @pytest.mark.asyncio
    async def test_a_failed_replay_keeps_the_card_for_the_next_landed_turn(self, tmp_path):
        state, client, slot = self._state_with_context_builder(tmp_path)
        self._script(
            client,
            [_complete()],
            RuntimeError("provider went away"),
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), _complete()],
        )

        await self._run_and_await_replay(state, slot, "what landed?")

        assert client.stream.call_count == 2
        assert [entry["content"] for entry in slot._pending_context] == [self.CARD]

        with patch.object(
            chat_runner, "generate_session_summary", new=AsyncMock(return_value=None)
        ):
            await _drive(state, slot, "and again?")
            await await_until(
                asyncio.gather(*list(state._background_tasks), return_exceptions=True),
                what="the failed-replay background task cleanup",
            )

        prompts = [self._delivered(call.args[0]) for call in client.stream.call_args_list]
        assert len(prompts) == 3, prompts
        assert prompts[2].count(self.CARD) == 1, prompts[2]
        # Plain context rode the replayed message and is not put back by a
        # failed turn, so it reaches no later turn.
        assert self.PLAIN not in prompts[2]
        assert slot._pending_context == []

    @pytest.mark.asyncio
    async def test_a_retained_image_recovery_replays_the_card_once(self, tmp_path):
        """The no-activity image-poison recovery re-queues the turn's text; the
        turn raised before its card's context was consumed, so the recovery turn drains the
        restored card and the replayed text must not carry its frame as well."""
        from kiro_crew.acp.client import AcpError

        state, client, slot = self._state_with_context_builder(tmp_path)
        poisoned = AcpError("The model could not process an image", transient=False)
        poisoned.structural_terminal = True
        poisoned.image_format_unsupported = True
        self._script(
            client,
            poisoned,
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), _complete()],
        )

        with patch.object(chat_runner, "_recovery_delay", new=AsyncMock()):
            await self._run_and_await_replay(state, slot, "what landed?")

        state.sessions.discard_conversation.assert_awaited_once()
        prompts = [self._delivered(call.args[0]) for call in client.stream.call_args_list]
        assert len(prompts) == 2, prompts
        first, replay = prompts
        assert first.count(self.CARD) == 1 and first.count(self.PLAIN) == 1
        assert replay.count(self.CARD) == 1, replay
        assert replay.count(self.PLAIN) == 1, replay
        assert replay.endswith("what landed?")
        assert slot._pending_context == []
        assert slot._inflight_merge_contexts == []

    @staticmethod
    def _record_queue_inserts(monkeypatch, slot) -> list[dict]:
        inserts: list[dict] = []
        repository = slot._queue_repository
        real_insert = repository.queue_insert

        def _record(owner, index, content, kind="", *args, **kwargs):
            inserts.append({"content": content, "kind": kind})
            return real_insert(owner, index, content, kind, *args, **kwargs)

        monkeypatch.setattr(repository, "queue_insert", _record)
        return inserts

    @pytest.mark.asyncio
    async def test_an_owed_completion_replays_the_card_once(self, tmp_path, monkeypatch):
        """A sub-agent completion rejected for a retained image is queued again as
        the completion it is. The turn raised before its card's context was consumed, so the
        completion's replay must not carry the card's frame as well."""
        from kiro_crew.acp.client import AcpError

        state, client, slot = self._state_with_context_builder(tmp_path)
        poisoned = AcpError("The model could not process an image", transient=False)
        poisoned.structural_terminal = True
        poisoned.image_format_unsupported = True
        self._script(
            client,
            poisoned,
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), _complete()],
        )
        inserts = self._record_queue_inserts(monkeypatch, slot)

        with patch.object(chat_runner, "_recovery_delay", new=AsyncMock()):
            await self._run_and_await_replay(
                state, slot, self.COMPLETION, _turn_actor="subagent", _on_consumed=MagicMock()
            )

        [requeued] = [entry for entry in inserts if self.COMPLETION in entry["content"]]
        assert requeued["kind"] == chat_runner.SUBAGENT_COMPLETION_KIND, inserts
        assert self.CARD not in requeued["content"], requeued
        prompts = [self._delivered(call.args[0]) for call in client.stream.call_args_list]
        assert len(prompts) == 2, prompts
        replay = prompts[1]
        assert replay.count(self.CARD) == 1, replay
        assert replay.endswith(self.COMPLETION)
        assert slot._pending_context == []

    @pytest.mark.asyncio
    async def test_a_prompt_busy_replay_carries_the_card_once(self, tmp_path):
        """A provider reset after the prompt-busy retries ran out re-queues the
        turn's text verbatim. The reset turn put its card back, so the text must
        not carry the card's frame as well."""
        from kiro_crew.llm_helpers import PromptBusyExhaustedError

        state, client, slot = self._state_with_context_builder(tmp_path)
        self._script(
            client,
            PromptBusyExhaustedError(),
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), _complete()],
        )

        await self._run_and_await_replay(state, slot, "what landed?")

        prompts = [self._delivered(call.args[0]) for call in client.stream.call_args_list]
        assert len(prompts) == 2, prompts
        first, replay = prompts
        assert first.count(self.CARD) == 1
        assert replay.count(self.CARD) == 1, replay
        assert replay.endswith("what landed?")
        assert slot._pending_context == []

    @pytest.mark.asyncio
    async def test_a_signed_out_cli_holds_a_completion_without_its_card(self, tmp_path):
        """A signed-out CLI holds the popped completion at the queue head for after
        sign-in, and the failed turn put its card back. The held text carries no
        card frame and is still marked as replaying the completion."""
        from kiro_crew.acp.transport_errors import AcpAuthRequired

        state, client, slot = self._state_with_context_builder(tmp_path)
        self._script(client, AcpAuthRequired("Please sign in"))

        with (
            _quiet_sel(),
            patch.object(chat_runner, "generate_session_summary", new=AsyncMock(return_value=None)),
        ):
            await chat_runner._run_chat(
                state, slot, self.COMPLETION, _turn_actor="subagent", _on_consumed=MagicMock()
            )
            await _settle(slot)

        [held] = slot._queue
        assert held["kind"] == chat_runner.SUBAGENT_COMPLETION_KIND
        assert self.CARD not in held["content"], held["content"]
        assert held["content"].endswith(self.COMPLETION)
        assert held.get(chat_runner._REPLAYS_COMPLETION_KEY) is True
        assert [entry["content"] for entry in slot._pending_context] == [self.CARD]

    @pytest.mark.asyncio
    async def test_a_refusal_fallback_replay_carries_the_card_once(self, tmp_path, monkeypatch):
        """A refused turn lands, so it consumes its merge card's context instead of
        putting it back on the queue. The verbatim replay to the fallback model is then
        the card's only delivery: its prompt must carry the frame, exactly once."""
        from kiro_crew.acp.types import STOP_REASON_REFUSAL, RefusalInfo

        monkeypatch.setattr(chat_runner, "_configured_refusal_fallback", lambda: "opus-test")
        state, client, slot = self._state_with_context_builder(tmp_path)
        # The swap witnesses ``set_model`` through ``served_model``; a bare
        # AsyncMock attribute is not a model id and the swap would not land.
        client.served_model = "fable-5"
        client._explicit_pick_epoch = 0
        client.set_model = AsyncMock(
            side_effect=lambda model: setattr(client, "served_model", model)
        )
        self._script(
            client,
            [_complete(STOP_REASON_REFUSAL, refusal=RefusalInfo(category="CYBER"))],
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), _complete()],
        )

        await self._run_and_await_replay(state, slot, "what landed?")

        client.set_model.assert_awaited_once_with("opus-test")
        prompts = [self._delivered(call.args[0]) for call in client.stream.call_args_list]
        assert len(prompts) == 2, prompts
        first, replay = prompts
        assert first.count(self.CARD) == 1 and first.count(self.PLAIN) == 1
        assert replay.count(self.CARD) == 1, replay
        assert replay.count(self.PLAIN) == 1, replay
        assert replay.endswith("what landed?")
        # The refused turn consumed the card's context and the replay restored nothing:
        # no later turn drains it a second time.
        assert slot._pending_context == []
        assert slot._inflight_merge_contexts == []

    @pytest.mark.asyncio
    async def test_a_thinking_binding_rebuild_replays_the_card_once(self, tmp_path):
        """The rebuild re-queues the turn's own ``message``, frame included, so
        ``_queue_recovery`` is what strips the card: the turn raised before its
        card's context was consumed, and the recovery turn drains the restored card."""
        state, client, slot = self._state_with_context_builder(tmp_path)
        mismatch = AcpConversationBindingMismatch(
            "Invalid `signature` in `thinking` block. The block is bound to a different conversation."
        )
        self._script(
            client,
            mismatch,
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), _complete()],
        )

        await self._run_and_await_replay(state, slot, "what landed?")

        state.sessions.discard_conversation.assert_awaited_once()
        prompts = [self._delivered(call.args[0]) for call in client.stream.call_args_list]
        assert len(prompts) == 2, prompts
        first, replay = prompts
        assert first.count(self.CARD) == 1 and first.count(self.PLAIN) == 1
        assert replay.count(self.CARD) == 1, replay
        assert replay.count(self.PLAIN) == 1, replay
        assert replay.endswith("what landed?")
        assert slot._pending_context == []
        assert slot._inflight_merge_contexts == []
