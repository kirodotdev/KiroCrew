"""Merging a fork back into its parent.

The planner decides which fork messages the parent does not have yet, the
draft route asks the background model to summarize them, and the merge route
writes the person's text into the parent as a note whose row carries a
``mergedFrom`` block. These tests drive the routes against a real conversation
log, with only the model call replaced.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state, close_before_resume

from kiro_crew.dashboard import chat_handlers, chat_merge_back, workflow_inject
from kiro_crew.dashboard.chat_merge_back import (
    MERGE_NOTE_SOURCE,
    MergePlan,
    api_chat_slot_merge_back,
    api_chat_slot_merge_back_draft,
    covered_digest,
    merge_cards,
    message_key,
    plan_merge,
)
from kiro_crew.dashboard.chat_persistence import (
    _apply_recent_session,
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.chat_utils import (
    adopt_variant_text,
    effective_session_key,
    slot_history_key,
    transcripts_share_file,
)
from kiro_crew.dashboard.handlers.sessions import api_session_delete
from kiro_crew.dashboard.slot_buffers import (
    AWAITING_DURABLE_WRITE,
    MAX_DEFERRED_NOTE_CHARS,
    MAX_FORK_PARENT_KEY_CHARS,
    MERGED_FROM_MAX_CREATED_AT_CHARS,
    MERGED_FROM_MAX_KEY_CHARS,
    MERGED_FROM_META_KEY,
    sanitize_merged_from,
    sanitize_restored_deferred_notes,
    serialize_deferred_notes,
)
from kiro_crew.dashboard.state import _MAX_CONTEXT_PER_SOURCE, _MAX_PENDING_CONTEXT
from kiro_crew.history import TranscriptBusy, TranscriptWithheld


def _row(role: str, content: str, mid: str | None) -> dict:
    row: dict = {"role": role, "content": content}
    if mid is not None:
        row["meta"] = {"mid": mid}
    return row


def _card(
    after: str, through: str, covered: list[dict], mid: str, fork_session: str = "dashboard:fork"
) -> dict:
    """A merge card in the parent: the fork messages after *after* through *through*."""
    block = {
        "session": fork_session,
        "slot": "fork",
        "title": "↳ Fork of Parent",
        "createdAt": "2026-10-02T18:00:00+00:00",
        "after": after,
        "through": through,
        "digest": covered_digest(covered),
        "messages": len(covered),
    }
    return {
        "role": "inject",
        "content": "summary",
        "meta": {"mid": mid, "noteId": f"note-{mid}", MERGED_FROM_META_KEY: block},
    }


class TestPlanMerge:
    def test_a_head_fork_brings_back_only_what_came_after_the_copy(self):
        parent = [_row("user", "p1", "a"), _row("assistant", "p2", "b"), _row("user", "p3", "c")]
        fork = [_row("user", "p1", "a"), _row("assistant", "p2", "b")]
        fork += [_row("user", "try redis", "x"), _row("assistant", "redis works", "y")]

        plan = plan_merge(fork, parent, [])

        assert [r["content"] for r in plan.rows] == ["try redis", "redis works"]
        assert plan.messages == 2
        assert plan.through == "y"
        assert [r["content"] for r in plan.context] == ["p1", "p2"]

    def test_a_tail_fork_is_recognised_by_the_ids_it_copied(self):
        parent = [_row("user", "p1", "a"), _row("assistant", "p2", "b"), _row("user", "p3", "c")]
        fork = [
            _row("assistant", "p2", "b"),
            _row("user", "p3", "c"),
            _row("assistant", "new", "z"),
        ]

        plan = plan_merge(fork, parent, [])

        assert [r["content"] for r in plan.rows] == ["new"]

    def test_a_second_merge_starts_after_the_first_cards_through(self):
        fork = [_row("user", "p1", "a"), _row("user", "q1", "x"), _row("assistant", "r1", "y")]
        fork += [_row("user", "q2", "w")]
        parent = [_row("user", "p1", "a"), _card("a", "y", fork[1:3], "card-1")]

        cards = merge_cards(parent, [], "dashboard:fork")
        plan = plan_merge(fork, parent, cards)

        assert [card["through"] for card in cards] == ["y"]
        assert [r["content"] for r in plan.rows] == ["q2"]
        assert plan.through == "w"

    def test_a_fork_rewound_past_its_merge_point_starts_from_what_remains(self):
        # The merge covered through "y", then the fork was rewound to before it
        # and continued. "y" is gone, so the last message the parent still has
        # is the copied one, and everything after it is new again.
        merged = [_row("user", "q1", "x"), _row("assistant", "r1", "y")]
        parent = [_row("user", "p1", "a"), _card("a", "y", merged, "card-1")]
        fork = [_row("user", "p1", "a"), _row("user", "redo", "v")]

        plan = plan_merge(fork, parent, merge_cards(parent, [], "dashboard:fork"))

        assert [r["content"] for r in plan.rows] == ["redo"]

    def test_a_fork_of_a_pre_id_parent_is_recognised_by_stamp_role_and_text(self):
        # Written before rows carried ids, so the parent's rows have none.
        parent = [
            {"role": "user", "content": "p1", "ts": "T1"},
            {"role": "assistant", "content": "the key is AKIAIOSFODNN7EXAMPLE", "ts": "T2"},
            {"role": "user", "content": "p3", "ts": "T3"},
        ]
        # The fork route minted its copies ids and redacted the reply it copied.
        fork = [
            {"role": "user", "content": "p1", "ts": "T1", "meta": {"mid": "f1"}},
            {
                "role": "assistant",
                "content": "the key is [REDACTED: credential]",
                "ts": "T2",
                "meta": {"mid": "f2"},
            },
            _row("user", "try redis", "x"),
        ]

        plan = plan_merge(fork, parent, [])

        assert plan.cursor == 1
        assert [r["content"] for r in plan.rows] == ["try redis"]

    def test_a_fork_whose_rows_carry_no_ids_still_names_where_its_messages_end(self):
        # Both chats written before rows carried ids.
        parent = [{"role": "user", "content": "p1", "ts": "T1"}]
        fork = [
            {"role": "user", "content": "p1", "ts": "T1"},
            {"role": "user", "content": "try redis", "ts": "T2"},
            {"role": "assistant", "content": "redis works", "ts": "T3"},
        ]

        plan = plan_merge(fork, parent, [])

        assert (plan.cursor, plan.messages) == (0, 2)
        # Derived from the line, so a second read of it names it the same way,
        # and a changed reply is a different message.
        assert plan.through == message_key(dict(fork[-1]))
        assert plan.through != message_key({**fork[-1], "content": "redis fails"})

    def test_a_parent_rewritten_from_its_first_message_makes_the_whole_fork_new(self):
        # The parent's first message was edited and sent again after the fork was
        # made, so the parent holds none of the rows the fork copied.
        parent = [_row("user", "p1, edited", "a2"), _row("assistant", "p2, again", "b2")]
        fork = [_row("user", "p1", "a"), _row("assistant", "p2", "b"), _row("user", "mine", "x")]

        plan = plan_merge(fork, parent, [])

        assert (plan.cursor, plan.messages) == (-1, 3)

    def test_each_pre_id_parent_row_matches_one_fork_row(self):
        # Two rows a coarse clock stamped alike; the fork copied only the first.
        parent = [{"role": "user", "content": "ok", "ts": "T1"}] * 2
        fork = [
            {"role": "user", "content": "ok", "ts": "T1", "meta": {"mid": "f1"}},
            _row("assistant", "mine", "x"),
        ]

        plan = plan_merge(fork, parent, [])

        assert (plan.cursor, plan.messages) == (0, 1)

    def test_a_card_whose_oldest_rows_are_gone_stops_counting(self):
        merged = [_row("user", "q1", "x"), _row("assistant", "r1", "y")]
        parent = [_row("user", "p1", "a"), _card("a", "y", merged, "card-1")]
        # Past its size cap the fork keeps only its newest rows, and the rotated
        # archive is deleted after its retention window: the copied message and
        # the start of the merged range are gone, the end is not. The rows left
        # hash differently from the card's digest, so the next draft covers the
        # surviving part of that range again.
        fork = [merged[1], _row("user", "q2", "w")]

        plan = plan_merge(fork, parent, merge_cards(parent, [], "dashboard:fork"))

        assert (plan.cursor, plan.through) == (-1, "w")
        assert [r["content"] for r in plan.rows] == ["r1", "q2"]

    def test_a_card_whose_range_lost_a_middle_message_stops_counting(self):
        merged = [
            _row("user", "q1", "x"),
            _row("assistant", "r1", "y"),
            _row("user", "q2", "z"),
        ]
        parent = [_row("user", "p1", "a"), _card("a", "z", merged, "card-1")]
        fork = [parent[0], merged[0], merged[2], _row("assistant", "r2", "w")]

        plan = plan_merge(fork, parent, merge_cards(parent, [], "dashboard:fork"))

        assert (plan.cursor, plan.through) == (0, "w")
        assert [r["content"] for r in plan.rows] == ["q1", "q2", "r2"]

    def test_a_surviving_reply_switched_after_rotation_is_new_again(self):
        merged = [_row("user", "q1", "x"), _row("assistant", "r1", "y")]
        parent = [_row("user", "p1", "a"), _card("a", "y", merged, "card-1")]
        fork = [_row("assistant", "r1 switched", "y"), _row("user", "q2", "w")]

        plan = plan_merge(fork, parent, merge_cards(parent, [], "dashboard:fork"))

        assert plan.cursor == -1
        assert [r["content"] for r in plan.rows] == ["r1 switched", "q2"]

    def test_nothing_new_plans_no_messages(self):
        parent = [_row("user", "p1", "a"), _row("assistant", "p2", "b")]
        fork = [_row("user", "p1", "a"), _row("assistant", "p2", "b")]

        plan = plan_merge(fork, parent, [])

        assert plan.messages == 0
        assert plan.through is None

    def test_another_forks_card_does_not_move_the_cursor(self):
        fork = [_row("user", "p1", "a"), _row("user", "mine", "x")]
        parent = [_row("user", "p1", "a"), _card("a", "x", fork[1:], "card-1", "dashboard:other")]

        cards = merge_cards(parent, [], "dashboard:fork")
        plan = plan_merge(fork, parent, cards)

        assert cards == []
        assert (plan.cursor, plan.through) == (0, "x")

    def test_a_held_merge_counts_before_it_lands(self):
        block = _card("a", "y", [_row("assistant", "r1", "y")], "m")["meta"][MERGED_FROM_META_KEY]
        held = [{"content": "s", "merged_from": block}]

        assert merge_cards([], held, "dashboard:fork") == [block]

    @pytest.mark.parametrize(
        ("role", "meta_extra"),
        [
            pytest.param("user", {"noteId": "note-1"}, id="a user row"),
            pytest.param("inject", {}, id="an inject row the flush never stamped"),
        ],
    )
    def test_only_a_delivered_note_row_can_claim_a_merge_card(self, role, meta_extra):
        block = _card("a", "y", [_row("assistant", "r1", "y")], "m")["meta"][MERGED_FROM_META_KEY]
        forged = {
            "role": role,
            "content": "claim",
            "meta": {**meta_extra, MERGED_FROM_META_KEY: block},
        }

        assert merge_cards([forged], [], "dashboard:fork") == []

    def test_a_merged_reply_switched_to_another_variant_is_new_again(self):
        merged = [_row("user", "try redis", "x"), _row("assistant", "redis works", "y")]
        parent = [_row("user", "p1", "a"), _card("a", "y", merged, "card-1")]
        # The switch keeps the reply's id and changes its text.
        fork = [_row("user", "p1", "a"), merged[0], _row("assistant", "redis failed", "y")]

        plan = plan_merge(fork, parent, merge_cards(parent, [], "dashboard:fork"))

        assert plan.cursor == 0
        assert [r["content"] for r in plan.rows] == ["try redis", "redis failed"]

    def test_a_card_after_a_changed_one_does_not_count(self):
        first = [_row("user", "q1", "x"), _row("assistant", "r1", "y")]
        second = [_row("user", "q2", "w"), _row("assistant", "r2", "z")]
        parent = [
            _row("user", "p1", "a"),
            _card("a", "y", first, "c1"),
            _card("y", "z", second, "c2"),
        ]
        fork = [_row("user", "p1", "a"), first[0], _row("assistant", "r1 switched", "y"), *second]

        plan = plan_merge(fork, parent, merge_cards(parent, [], "dashboard:fork"))

        # The chain stops at the changed range, so the second card cannot extend it.
        assert plan.cursor == 0
        assert plan.messages == 4

    def test_a_remerge_of_a_changed_range_counts_once_it_lands(self):
        original = [_row("user", "q1", "x"), _row("assistant", "r1", "y")]
        switched = [original[0], _row("assistant", "r1 switched", "y")]
        parent = [_row("user", "p1", "a"), _card("a", "y", original, "c1")]
        parent.append(_card("a", "y", switched, "c2"))
        fork = [_row("user", "p1", "a"), *switched]

        plan = plan_merge(fork, parent, merge_cards(parent, [], "dashboard:fork"))

        assert (plan.cursor, plan.messages) == (2, 0)

    def test_a_card_row_written_twice_counts_as_one_card(self):
        # An older build that ignores a hold entry's ``delivered`` flag replays
        # the card after a downgrade: the parent then holds two rows with one
        # ``noteId`` and one ``mergedFrom``. Both reach the same ``through``,
        # so the chain moves the cursor once and the plan is the one-row plan.
        covered = [_row("user", "q1", "x"), _row("assistant", "r1", "y")]
        card = _card("a", "y", covered, "card-1")
        parent = [_row("user", "p1", "a"), card]
        fork = [parent[0], *covered, _row("user", "q2", "w")]
        replayed = [*parent, {**card, "meta": {**card["meta"], "mid": "card-1-replay"}}]

        once = plan_merge(fork, parent, merge_cards(parent, [], "dashboard:fork"))
        twice = plan_merge(fork, replayed, merge_cards(replayed, [], "dashboard:fork"))

        assert twice == once
        assert (twice.cursor, twice.through, twice.messages) == (2, "w", 1)
        assert [r["content"] for r in twice.rows] == ["q2"]

    def test_the_digest_follows_the_messages_text_not_only_their_ids(self):
        rows = [_row("user", "try redis", "x"), _row("assistant", "redis works", "y")]
        switched = [rows[0], _row("assistant", "redis failed", "y")]
        with_a_note = [rows[0], {"role": "inject", "content": "a note"}, rows[1]]

        assert covered_digest(rows) == covered_digest([dict(row) for row in rows])
        assert covered_digest(rows) != covered_digest(switched)
        assert covered_digest(rows) != covered_digest(rows[1:])
        # Only the messages are fingerprinted: a row that is not one changes nothing.
        assert covered_digest(rows) == covered_digest(with_a_note)


class TestSanitizeMergedFrom:
    def _valid(self) -> dict:
        return {
            "session": "dashboard:f",
            "slot": "f",
            "title": "t",
            "createdAt": "2026-10-02T18:00:00+00:00",
            "after": "",
            "through": "m",
            "digest": "ab" * 32,
            "messages": 3,
        }

    def test_a_valid_block_comes_back_with_exactly_its_fields(self):
        raw = {**self._valid(), "extra": "dropped"}

        assert sanitize_merged_from(raw) == self._valid()

    def test_a_block_may_record_an_empty_creation_identity(self):
        # A fork whose metadata line records no ``created_at`` merges with an
        # empty identity; the card still parses and counts by its digest.
        raw = {**self._valid(), "createdAt": ""}

        assert sanitize_merged_from(raw) == raw

    @pytest.mark.parametrize(
        "missing",
        ["session", "slot", "title", "createdAt", "after", "through", "digest", "messages"],
    )
    def test_a_block_missing_any_field_is_refused(self, missing):
        raw = self._valid()
        raw.pop(missing)

        assert sanitize_merged_from(raw) is None

    @pytest.mark.parametrize(
        "change",
        [
            {"messages": 0},
            {"messages": True},
            {"messages": "3"},
            {"through": ""},
            {"session": None},
            {"slot": 7},
            {"title": None},
            {"title": "t" * 201},
            {"createdAt": None},
            {"createdAt": "t" * (MERGED_FROM_MAX_CREATED_AT_CHARS + 1)},
            {"through": "m" * (MERGED_FROM_MAX_KEY_CHARS + 1)},
            {"after": None},
            {"after": "m" * (MERGED_FROM_MAX_KEY_CHARS + 1)},
            {"digest": None},
            {"digest": "AB" * 32},
            {"digest": "ab" * 31},
        ],
    )
    def test_a_malformed_block_is_refused(self, change):
        assert sanitize_merged_from({**self._valid(), **change}) is None

    def test_a_non_dict_is_refused(self):
        assert sanitize_merged_from(["session"]) is None


class TestHeldMergeNote:
    def test_the_block_survives_a_restart_and_is_stamped_on_the_delivered_row(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("parent")
        block = {
            "session": "dashboard:f",
            "slot": "f",
            "title": "t",
            "createdAt": "2026-10-02T18:00:00+00:00",
            "after": "",
            "through": "m",
            "digest": "ab" * 32,
            "messages": 1,
        }
        held = {
            "id": "note-1",
            "content": "summary",
            "cls": "reconcile-note",
            "context": None,
            "session": effective_session_key(slot),
            "merged_from": block,
        }

        restored = sanitize_restored_deferred_notes(serialize_deferred_notes([held]))
        slot._deferred_notes = restored
        slot.flush_deferred_notes()

        row = slot.messages[-1]
        assert row["role"] == "inject"
        assert row["meta"][MERGED_FROM_META_KEY] == block
        assert row["meta"]["noteId"] == "note-1"

    def test_a_malformed_block_is_dropped_alone(self):
        held = {
            "id": "note-1",
            "content": "summary",
            "cls": "reconcile-note",
            "context": None,
            "session": "dashboard:parent",
            "merged_from": {"session": "dashboard:f", "messages": -1},
        }

        restored = sanitize_restored_deferred_notes(serialize_deferred_notes([held]))

        assert len(restored) == 1
        assert restored[0]["content"] == "summary"
        assert "merged_from" not in restored[0]

    @pytest.mark.asyncio
    async def test_an_uncommitted_rowless_card_keeps_its_durable_hold(self, tmp_path, monkeypatch):
        """A failed save plus window trimming must not retire the card's last copy."""
        from kiro_crew.dashboard import chat_persistence, chat_runner
        from kiro_crew.dashboard import state as dashboard_state

        state = _make_state(tmp_path)
        parent = state.get_or_create_slot("parent")
        parent.title = "Parent"
        parent._titled = True
        _seed(parent, ("user", "pick a cache"), ("assistant", "redis?"))
        state.flush_slot_now(parent)
        fork = _fork_of(state, parent)
        _seed(fork, ("user", "try redis"), ("assistant", "redis works"))
        await _place_merge_card(state, parent, fork, "merged summary")
        [context] = parent._pending_context
        note_id = context["noteId"]

        real_atomic_write = chat_persistence.atomic_write

        def fail_save(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(chat_persistence, "atomic_write", fail_save)
        with pytest.raises(OSError, match="disk full"):
            _save_slot_to_history(state, parent)

        monkeypatch.setattr(dashboard_state, "_MAX_SLOT_MESSAGES", 3)
        for index in range(3):
            parent.append("chunk", f"chunk-{index}")
        assert all(
            not isinstance(row.get("meta"), dict) or row["meta"].get("noteId") != note_id
            for row in parent.messages
        )

        turn_context = chat_runner.take_turn_context(
            state, parent, transcript_identity=parent._disk_meta_created_at
        )
        assert turn_context.consumed == [context]
        await chat_runner._record_consumed_merge_contexts(state, turn_context.consumed)

        monkeypatch.setattr(chat_persistence, "atomic_write", real_atomic_write)
        _save_slot_to_history(state, parent)
        held = state.conversation_log.get_metadata(slot_history_key(parent))["deferred_notes"]
        assert any(entry.get("id") == note_id for entry in held)

    @pytest.mark.asyncio
    async def test_a_committed_rowless_card_retires_from_its_durable_proof(
        self, tmp_path, monkeypatch
    ):
        """The atomic delivered stamp permits retirement after the row leaves memory."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard import state as dashboard_state

        state = _make_state(tmp_path)
        parent = state.get_or_create_slot("parent")
        parent.title = "Parent"
        parent._titled = True
        _seed(parent, ("user", "pick a cache"), ("assistant", "redis?"))
        state.flush_slot_now(parent)
        fork = _fork_of(state, parent)
        _seed(fork, ("user", "try redis"), ("assistant", "redis works"))
        await _place_merge_card(state, parent, fork, "merged summary")
        [context] = parent._pending_context
        note_id = context["noteId"]
        state.flush_slot_now(parent)

        monkeypatch.setattr(dashboard_state, "_MAX_SLOT_MESSAGES", 3)
        for index in range(3):
            parent.append("chunk", f"chunk-{index}")
        assert all(
            not isinstance(row.get("meta"), dict) or row["meta"].get("noteId") != note_id
            for row in parent.messages
        )

        turn_context = chat_runner.take_turn_context(
            state, parent, transcript_identity=parent._disk_meta_created_at
        )
        await chat_runner._record_consumed_merge_contexts(state, turn_context.consumed)
        _save_slot_to_history(state, parent)

        metadata = state.conversation_log.get_metadata(slot_history_key(parent))
        assert not metadata.get("deferred_notes")


# --- routes ---------------------------------------------------------------


def _seed(slot, *messages: tuple[str, str]) -> None:
    for role, content in messages:
        slot.append(role, content, "msg msg-u" if role == "user" else "msg msg-a", broadcast=False)


def _fork_of(state, parent, name: str = "fork", *, upto: int | None = None):
    """A fork made the way the fork route makes one: the parent's visible rows
    copied with their meta, so they keep the parent's message ids."""
    fork = state.get_or_create_slot(name)
    visible = [m for m in parent.messages if m.get("role") in ("user", "assistant")]
    for m in visible[:upto]:
        fork.append(
            m["role"],
            m["content"],
            m.get("cls", ""),
            ts=m.get("ts", ""),
            meta=dict(m["meta"]),
            broadcast=False,
        )
    fork.forked_from = effective_session_key(parent)
    fork.title = "↳ Fork of Parent"
    fork._titled = True
    state.flush_slot_now(fork)
    return fork


_CHANNEL_STEM = "slack_1700000000.000001"
_CHANNEL_KEY = "slack:1700000000.000001"


def _channel_parent_and_fork(state, stem: str, *, linked: bool):
    """A channel-born parent named by its transcript stem, and a fork of it.

    *linked* binds it to the channel's own session key, as the dashboard does
    once the session map answers for the stem; unbound, it writes the same file
    under the stem alone.
    """
    parent = state.get_or_create_slot(stem, channel_origin=True)
    if linked:
        parent.linked_session_key = _CHANNEL_KEY
    parent.title = "Thread"
    parent._titled = True
    _seed(parent, ("user", "pick a cache"), ("assistant", "memcached or redis?"))
    state.flush_slot_now(parent)
    fork = _fork_of(state, parent, "channel-fork")
    _seed(fork, ("user", "try redis"), ("assistant", "redis works, TTL 300s"))
    return parent, fork


@pytest.fixture
def model(monkeypatch):
    """The background model call, recorded and answered with a fixed note."""
    calls: list[dict] = []
    replies = ["Tried Redis as the cache. It works; the TTL is 300s."]

    async def fake(sessions, prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        reply = replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    cfg = SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: f"model-for-{role}"))
    monkeypatch.setattr(chat_merge_back, "run_bg_oneliner", fake)
    monkeypatch.setattr(chat_merge_back, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    return SimpleNamespace(calls=calls, replies=replies)


@pytest.fixture
def audit(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        chat_merge_back,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kwargs: calls.append(kwargs)),
    )
    return calls


def _assert_privacy_denial(
    calls: list[dict], operation: str, *, resources: str, transcript: str
) -> None:
    assert calls == [
        {
            "caller": "dashboard",
            "operation": operation,
            "outcome": "denied",
            "source": "dashboard",
            "resources": resources,
            "error": f"{transcript} transcript is restricted",
        }
    ]


@pytest.fixture
def chats(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    parent.title = "Parent"
    parent._titled = True
    _seed(parent, ("user", "pick a cache"), ("assistant", "memcached or redis?"))
    state.flush_slot_now(parent)
    fork = _fork_of(state, parent)
    _seed(fork, ("user", "try redis"), ("assistant", "redis works, TTL 300s"))
    return SimpleNamespace(state=state, parent=parent, fork=fork)


def _app(state, *, request_app: str = "") -> web.Application:
    app = _make_app(state)

    @web.middleware
    async def _as_caller(request, handler):
        request["is_dashboard_user"] = not bool(request_app)
        if request_app:
            request["app"] = request_app
        return await handler(request)

    app.middlewares.insert(0, _as_caller)
    app.router.add_post("/api/chat/slots/{slot}/merge-back/draft", api_chat_slot_merge_back_draft)
    app.router.add_post("/api/chat/slots/{slot}/merge-back", api_chat_slot_merge_back)
    app.router.add_delete("/api/chat/slots/{slot}", chat_handlers.api_chat_slot_delete)
    app.router.add_post("/api/chat/slots/{slot}/resume", chat_handlers.api_chat_slot_resume)
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    app.router.add_delete("/api/sessions/{key}", api_session_delete)
    return app


@asynccontextmanager
async def _client(state, **kwargs):
    async with TestClient(TestServer(_app(state, **kwargs))) as client:
        yield client


async def _draft(client, slot: str = "fork"):
    resp = await client.post(f"/api/chat/slots/{slot}/merge-back/draft", json={})
    return resp.status, await resp.json()


async def _merge(client, summary: str, draft: dict, slot: str = "fork"):
    """Merge *summary* against *draft*: its ``through`` and ``digest``."""
    resp = await client.post(
        f"/api/chat/slots/{slot}/merge-back",
        json={"summary": summary, "through": draft["through"], "digest": draft["digest"]},
    )
    return resp.status, await resp.json()


async def _close_route(client, slot: str = "parent") -> tuple[int, dict]:
    response = await client.delete(f"/api/chat/slots/{slot}")
    return response.status, await response.json()


async def _resume_route(client, history_key: str, slot: str = "parent") -> tuple[int, dict]:
    response = await client.post(f"/api/chat/slots/{slot}/resume", json={"key": history_key})
    return response.status, await response.json()


def _workflow_result_for(session: str, run_id: str) -> dict:
    """A finished run's terminal snapshot, as the registry hands it to ``on_done``."""
    return {
        "run_id": run_id,
        "name": "nightly",
        "status": "finished",
        "result": {"answer": 42},
        "session_key": session,
        "memory_mode": "persistent",
    }


def _slots_on_session(state, session: str) -> list:
    """Every live slot whose turns run on *session*, in registry order."""
    return [slot for slot in state._slots.values() if effective_session_key(slot) == session]


def _ends_at(through: str) -> dict:
    """A merge point with no draft behind it, for refusals decided before the digest."""
    return {"through": through, "digest": "0" * 64}


async def _run_turn(
    state,
    slot,
    message: str,
    *,
    outcome: str = "complete",
    started: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
    events=None,
    cancel_when: asyncio.Event | None = None,
    yolo: bool = False,
) -> list[str]:
    """Run one real turn on *slot* through ``_run_chat``; the prompts the model got.

    *events*, when given, is an async-generator factory whose events replace the
    default reply (one text chunk, then ``end_turn``); it may block or signal
    *cancel_when*. When *cancel_when* is set the turn is cancelled as soon as
    it fires, wherever the turn is at that moment.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.acp.types import STOP_REASON_END_TURN
    from kiro_crew.dashboard.chat_runner import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    prompts: list[str] = []
    stream_started = asyncio.Event()

    async def stream(prompt):
        prompts.append(prompt)
        stream_started.set()
        if started is not None:
            started.set()
        if release is not None:
            await asyncio.wait_for(release.wait(), timeout=10)
        if outcome == "error":
            raise RuntimeError("provider failed after accepting the prompt")
        if outcome == "cancel":
            await asyncio.wait_for(asyncio.Future(), timeout=10)
        if events is not None:
            async for event in events():
                yield event
            return
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="noted")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    client = MagicMock(is_kiro_backend=True, stream=stream, stream_command=stream)
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_success = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=yolo)
    state.context_builder = MagicMock()
    state.context_builder.conversation_log = None
    state.context_builder.build_message.side_effect = lambda text, *_a, **_k: (text, None)
    state._background_tasks = set()
    slot.append("user", message, "msg msg-u")
    with patch(
        "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
        new=AsyncMock(return_value=False),
    ):
        turn = asyncio.create_task(_run_chat(state, slot, message))
        if outcome == "cancel":
            await asyncio.wait_for(stream_started.wait(), timeout=10)
            turn.cancel()
        elif cancel_when is not None:
            await asyncio.wait_for(cancel_when.wait(), timeout=10)
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
    tasks = list(state._background_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return prompts


def _drop_ids_on_disk(state, slot) -> None:
    """Rewrite *slot*'s transcript as it was before rows carried ids."""
    log = state.conversation_log
    key = slot_history_key(slot)
    path = log._path(key)
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line) if line.strip() else None
        if isinstance(row, dict) and isinstance(row.get("meta"), dict):
            row["meta"].pop("mid", None)
        lines.append(line if row is None else json.dumps(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log._invalidate_cache(key)


def _cards(parent) -> list[dict]:
    return [
        m
        for m in parent.messages
        if isinstance(m.get("meta"), dict) and MERGED_FROM_META_KEY in m["meta"]
    ]


def _change_at_source_check(chats, monkeypatch, change) -> list[bool]:
    """Apply *change* at the merge source check and prove the hold encloses it."""
    log = chats.state.conversation_log
    fork_key = slot_history_key(chats.fork)
    real_hold = log.publication_hold
    real_check = chat_merge_back._source_range_matches
    source_hold_active = False
    changed = False
    observations: list[bool] = []

    @contextmanager
    def tracked_hold(key, **kwargs):
        nonlocal source_hold_active
        with real_hold(key, **kwargs):
            was_active = source_hold_active
            if key == fork_key:
                source_hold_active = True
            try:
                yield
            finally:
                source_hold_active = was_active

    def change_then_check(*args, **kwargs):
        nonlocal changed
        observations.append(source_hold_active)
        if not changed:
            change()
            changed = True
        return real_check(*args, **kwargs)

    monkeypatch.setattr(log, "publication_hold", tracked_hold)
    monkeypatch.setattr(chat_merge_back, "_source_range_matches", change_then_check)
    return observations


async def _place_merge_card(state, parent, fork, content: str):
    visible = [row for row in fork.messages if row.get("role") in ("user", "assistant")]
    covered = visible[-2:]
    merged_from = sanitize_merged_from(
        {
            "session": effective_session_key(fork),
            "slot": fork.key,
            "title": fork.title,
            "createdAt": fork._disk_meta_created_at,
            "after": message_key(visible[-3]) or "",
            "through": message_key(covered[-1]),
            "digest": covered_digest(covered),
            "messages": len(covered),
        }
    )
    assert merged_from is not None
    delivery = await chat_handlers.deliver_note(
        state,
        parent,
        content=content,
        source=MERGE_NOTE_SOURCE,
        max_age=None,
        merged_from=merged_from,
        durable=True,
    )
    assert not isinstance(delivery, web.Response)
    return delivery


def _queued_merge_context(index: int) -> dict:
    return {
        "content": f"queued merge {index}",
        "source": MERGE_NOTE_SOURCE,
        "ephemeral": True,
        "injectedAt": 1e12,
        "noteId": f"queued-note-{index}",
        "noteTranscript": "dashboard:parent",
    }


def _restrict_parent_chain(chats, mode: str = "temporary") -> str:
    """Restrict the parent's requested member of its real transcript chain."""
    from kiro_crew import history as history_mod

    log = chats.state.conversation_log
    parent_key = slot_history_key(chats.parent)
    with history_mod.allow_on_loop_persist():
        log.update_metadata(parent_key, {"memory_mode": mode})
    assert log.get_metadata(parent_key)["memory_mode"] == mode
    return parent_key


async def _move_older_fork_rows_to_rotated_archive(chats) -> None:
    """Leave the fork's copied rows and first fork-only turn only in its archive head."""
    from kiro_crew import history as history_mod

    log = chats.state.conversation_log
    key = slot_history_key(chats.fork)
    await asyncio.to_thread(chats.state.flush_slot_now, chats.fork)
    path = log._path(key)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    messages = [row for row in rows if "_type" not in row]
    controls = [row for row in rows if "_type" in row]
    assert len(messages) >= 6
    archived_messages, live_messages = messages[:-2], messages[-2:]
    archive_dir = history_mod._archive_dir(log._dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / (
        history_mod._safe_key(key) + history_mod.ARCHIVE_SEGMENT_DELIMITER + "20261001-000000.jsonl"
    )
    archived = [{"_type": "archive", "reason": "rotate"}, *archived_messages]
    archive.write_text("\n".join(json.dumps(row) for row in archived) + "\n", encoding="utf-8")
    live = [*controls, *live_messages]
    path.write_text("\n".join(json.dumps(row) for row in live) + "\n", encoding="utf-8")
    log._invalidate_cache(key)


def _move_parent_rows_to_rotated_archive(chats) -> None:
    """Leave the persistent parent's copied rows only in its archive head."""
    from kiro_crew import history as history_mod

    log = chats.state.conversation_log
    key = slot_history_key(chats.parent)
    path = log._path(key)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    messages = [row for row in rows if "_type" not in row]
    controls = [row for row in rows if "_type" in row]
    assert messages
    archive_dir = history_mod._archive_dir(log._dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / (
        history_mod._safe_key(key) + history_mod.ARCHIVE_SEGMENT_DELIMITER + "20261001-000000.jsonl"
    )
    archived = [{"_type": "archive", "reason": "rotate"}, *messages]
    archive.write_text("\n".join(json.dumps(row) for row in archived) + "\n", encoding="utf-8")
    path.write_text("\n".join(json.dumps(row) for row in controls) + "\n", encoding="utf-8")
    log._invalidate_cache(key)
    chats.parent.messages.clear()


class TestDraft:
    @pytest.mark.asyncio
    async def test_the_draft_summarizes_only_the_new_messages(self, chats, model):
        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert body["summary"] == "Tried Redis as the cache. It works; the TTL is 300s."
        assert body["messages"] == 2
        assert body["parent"] == "parent"
        assert "parent_unseen" not in body
        assert body["through"] == chats.fork.messages[-1]["meta"]["mid"]
        # The fingerprint covers exactly the two new messages.
        assert body["digest"] == covered_digest(chats.fork.messages[2:])
        prompt = model.calls[0]["prompt"]
        branch = prompt.split("FORK MESSAGES")[1]
        assert "try redis" in branch and "redis works, TTL 300s" in branch
        assert "pick a cache" not in branch
        # The copied messages are shown only as context, ahead of the branch.
        assert "pick a cache" in prompt.split("FORK MESSAGES")[0]
        assert model.calls[0]["crew_log_session_key"] == effective_session_key(chats.fork)
        assert model.calls[0]["model"] == "model-for-background"

    @pytest.mark.asyncio
    async def test_a_draft_covers_only_the_prefix_its_prompt_reads_in_full(
        self, chats, model, monkeypatch
    ):
        _seed(chats.fork, ("user", "third message must wait"))
        covered = chats.fork.messages[2:4]
        monkeypatch.setattr(
            chat_merge_back,
            "_MAX_SUMMARY_INPUT_CHARS",
            len(chat_merge_back._full_branch_input(covered)),
        )

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert body["messages"] == 2
        assert body["remaining"] == 1
        assert body["through"] == covered[-1]["meta"]["mid"]
        assert body["digest"] == covered_digest(covered)
        branch = model.calls[0]["prompt"].split("FORK MESSAGES")[1]
        assert all(row["content"] in branch for row in covered)
        assert "third message must wait" not in branch
        assert "omitted" not in branch
        assert "[excerpt]" not in branch

    @pytest.mark.asyncio
    async def test_a_long_assistant_reply_under_the_bound_reaches_the_prompt_whole(
        self, chats, model, monkeypatch
    ):
        reply = ("complete assistant detail " * 140).strip()
        _seed(chats.fork, ("assistant", reply))
        monkeypatch.setattr(chat_merge_back, "_MAX_SUMMARY_INPUT_CHARS", 10_000)

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert body["remaining"] == 0
        branch = model.calls[0]["prompt"].split("FORK MESSAGES")[1]
        assert reply in branch
        assert "[excerpt]" not in branch
        assert "omitted" not in branch

    @pytest.mark.asyncio
    async def test_a_first_message_over_the_bound_is_the_only_message_covered(
        self, chats, model, monkeypatch
    ):
        monkeypatch.setattr(chat_merge_back, "_MAX_SUMMARY_INPUT_CHARS", 20)

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert body["messages"] == 1
        assert body["remaining"] == 1
        assert body["through"] == chats.fork.messages[2]["meta"]["mid"]
        assert body["digest"] == covered_digest(chats.fork.messages[2:3])

    @pytest.mark.asyncio
    async def test_merging_a_short_draft_leaves_the_rest_for_the_next_draft(
        self, chats, model, monkeypatch
    ):
        first = chats.fork.messages[2]
        monkeypatch.setattr(
            chat_merge_back,
            "_MAX_SUMMARY_INPUT_CHARS",
            len(chat_merge_back._full_branch_input([first])),
        )

        async with _client(chats.state) as client:
            first_status, first_draft = await _draft(client)
            merge_status, merged = await _merge(client, "First result.", first_draft)
            second_status, second_draft = await _draft(client)

        assert first_status == merge_status == second_status == 200
        assert first_draft["messages"] == 1
        assert first_draft["remaining"] == 1
        assert merged["messages"] == 1
        assert second_draft["messages"] == 1
        assert second_draft["remaining"] == 0
        assert second_draft["through"] == chats.fork.messages[-1]["meta"]["mid"]
        assert second_draft["digest"] == covered_digest(chats.fork.messages[3:])
        second_branch = model.calls[1]["prompt"].split("FORK MESSAGES")[1]
        assert "redis works, TTL 300s" in second_branch
        assert "try redis" not in second_branch

    @pytest.mark.asyncio
    async def test_a_draft_never_ends_on_an_ambiguous_legacy_message_key(
        self, chats, model, monkeypatch
    ):
        duplicate_rows = chats.fork.messages[2:4]
        for row in duplicate_rows:
            row["role"] = "user"
            row["content"] = "same legacy message"
            row["ts"] = "T-duplicate"
            row.pop("meta", None)
        _seed(chats.fork, ("assistant", "message after duplicates"))
        chats.state.flush_slot_now(chats.fork)
        monkeypatch.setattr(
            chat_merge_back,
            "_MAX_SUMMARY_INPUT_CHARS",
            len(chat_merge_back._full_branch_input(duplicate_rows[:1])),
        )

        async with _client(chats.state) as client:
            first_status, first_draft = await _draft(client)
            first_merge_status, first_merge = await _merge(
                client, "Duplicates covered.", first_draft
            )
            second_status, second_draft = await _draft(client)
            second_merge_status, second_merge = await _merge(
                client, "Remaining message covered.", second_draft
            )

        assert first_status == first_merge_status == second_status == second_merge_status == 200
        positions = chat_merge_back._message_positions(chats.fork.messages)
        assert first_draft["messages"] == 2
        assert first_draft["remaining"] == 1
        assert positions[first_draft["through"]] == 3
        assert first_draft["digest"] == covered_digest(chats.fork.messages[2:4])
        assert first_merge["messages"] == 2
        assert second_draft["messages"] == 1
        assert second_draft["remaining"] == 0
        assert positions[second_draft["through"]] == 4
        assert second_draft["digest"] == covered_digest(chats.fork.messages[4:5])
        assert second_merge["messages"] == 1
        blocks = [card["meta"][MERGED_FROM_META_KEY] for card in _cards(chats.parent)]
        assert [block["messages"] for block in blocks] == [2, 1]
        assert [block["digest"] for block in blocks] == [
            covered_digest(chats.fork.messages[2:4]),
            covered_digest(chats.fork.messages[4:5]),
        ]

    def test_a_draft_steps_back_to_the_last_message_its_key_names(self, monkeypatch):
        rows = [
            {"role": "user", "content": "first question", "meta": {"mid": "m-1"}},
            {"role": "assistant", "content": "same legacy reply", "ts": "T-duplicate"},
            {"role": "assistant", "content": "same legacy reply", "ts": "T-duplicate"},
            {"role": "user", "content": "last question", "meta": {"mid": "m-4"}},
        ]
        plan = MergePlan(cursor=-1, rows=rows, context=[], messages=4, through="m-4")
        monkeypatch.setattr(
            chat_merge_back,
            "_MAX_SUMMARY_INPUT_CHARS",
            len(chat_merge_back._full_branch_input(rows[:2])),
        )

        fitted, remaining = chat_merge_back._fit_draft_prefix(plan)

        assert (fitted.rows, fitted.messages, fitted.through, remaining) == (rows[:1], 1, "m-1", 3)

    @pytest.mark.asyncio
    async def test_a_chat_that_is_not_a_fork_is_refused(self, chats, model):
        async with _client(chats.state) as client:
            status, body = await _draft(client, slot="parent")

        assert (status, body["code"]) == (409, "not_a_fork")

    @pytest.mark.asyncio
    async def test_a_closed_parent_is_refused(self, chats, model):
        chats.state._slots.pop("parent")

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (409, "parent_not_open")
        assert model.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parent_session", ["slack:1700000000.000001", "cron:daily"])
    async def test_a_parent_session_outside_the_dashboard_is_refused_before_reads(
        self, chats, model, parent_session
    ):
        chats.fork.forked_from = parent_session
        parent_messages = list(chats.parent.messages)
        fork_messages = list(chats.fork.messages)
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft["code"]) == (409, "parent_not_dashboard")
        assert (merge_status, merged["code"]) == (409, "parent_not_dashboard")
        assert chats.parent.messages == parent_messages
        assert chats.fork.messages == fork_messages
        assert chats.parent._deferred_notes == []
        assert chats.fork._deferred_notes == []
        assert model.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("complete_binding", [True, False])
    async def test_a_remote_executor_dashboard_parent_is_refused_before_reads(
        self, chats, model, complete_binding
    ):
        chats.parent.executor = "remote"
        if complete_binding:
            chats.parent.instance_id = "peer-instance"
            chats.parent.remote_slot = "parent"
        assert chats.parent.executor == "remote"
        parent_messages = list(chats.parent.messages)
        fork_messages = list(chats.fork.messages)
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft["code"]) == (409, "parent_not_dashboard")
        assert (merge_status, merged["code"]) == (409, "parent_not_dashboard")
        assert chats.parent.messages == parent_messages
        assert chats.fork.messages == fork_messages
        assert chats.parent._deferred_notes == []
        assert chats.fork._deferred_notes == []
        assert model.calls == []

    @pytest.mark.asyncio
    async def test_a_parent_made_remote_bound_after_draft_is_refused_at_merge(self, chats, model):
        parent_messages = list(chats.parent.messages)
        fork_messages = list(chats.fork.messages)

        async with _client(chats.state) as client:
            draft_status, draft = await _draft(client)
            chats.parent.executor = "remote"
            chats.parent.instance_id = "peer-instance"
            chats.parent.remote_slot = "parent"
            merge_status, merged = await _merge(client, "Redis works.", draft)

        assert draft_status == 200, draft
        assert (merge_status, merged["code"]) == (409, "parent_not_dashboard")
        assert chats.parent.messages == parent_messages
        assert chats.fork.messages == fork_messages
        assert chats.parent._deferred_notes == []
        assert chats.fork._deferred_notes == []
        assert len(model.calls) == 1

    @pytest.mark.asyncio
    async def test_an_unbound_channel_parent_is_refused_before_reads(self, chats, model):
        state = chats.state
        parent, fork = _channel_parent_and_fork(state, _CHANNEL_STEM, linked=False)
        parent_messages = list(parent.messages)
        fork_messages = list(fork.messages)
        through = fork.messages[-1]["meta"]["mid"]

        async with _client(state) as client:
            draft_status, draft = await _draft(client, fork.key)
            merge_status, merged = await _merge(client, "Redis works.", _ends_at(through), fork.key)

        assert (draft_status, draft["code"]) == (409, "parent_not_dashboard")
        assert (merge_status, merged["code"]) == (409, "parent_not_dashboard")
        assert parent.messages == parent_messages
        assert fork.messages == fork_messages
        assert parent._deferred_notes == []
        assert fork._deferred_notes == []
        assert model.calls == []

    def test_parent_identity_accepts_exact_and_legacy_older_transcripts_only(self, chats):
        state, fork = chats.state, chats.fork
        twin = state.get_or_create_slot("twin")
        twin.linked_session_key = effective_session_key(chats.parent)
        fork.forked_from_created_at = chats.parent._disk_meta_created_at
        twin._disk_meta_created_at = chats.parent._disk_meta_created_at

        assert chat_merge_back._parent_slots(state, fork) == [chats.parent, twin]
        twin._disk_meta_created_at = "2099-01-01T00:00:00+00:00"
        assert chat_merge_back._parent_slots(state, fork) == [chats.parent]
        fork.forked_from_created_at = ""
        fork._disk_meta_created_at = "2026-10-01T12:00:00+00:00"
        chats.parent._disk_meta_created_at = "2026-10-01T11:00:00+00:00"
        assert chat_merge_back._parent_slots(state, fork) == [chats.parent]
        chats.parent._disk_meta_created_at = ""
        assert chat_merge_back._parent_slots(state, fork) == []

    def test_parent_identity_tells_another_transcript_from_an_unprovable_one(self, chats):
        fork, parent = chats.fork, chats.parent
        identity = chat_merge_back._parent_identity
        holds, another, unproven = chat_merge_back._ParentIdentity

        fork.forked_from_created_at = parent._disk_meta_created_at
        assert identity(fork, parent) is holds
        parent._disk_meta_created_at = "2099-01-01T00:00:00+00:00"
        assert identity(fork, parent) is another
        parent._disk_meta_created_at = ""
        assert identity(fork, parent) is unproven

        fork.forked_from_created_at = ""
        fork._disk_meta_created_at = "2026-10-01T12:00:00+00:00"
        parent._disk_meta_created_at = "2026-10-01T12:00:00+00:00"
        assert identity(fork, parent) is holds
        parent._disk_meta_created_at = "2026-10-01T12:00:01+00:00"
        assert identity(fork, parent) is another
        for unorderable in ("", "not-a-time", "2026-10-01T11:00:00"):
            parent._disk_meta_created_at = unorderable
            assert identity(fork, parent) is unproven, unorderable
        parent._disk_meta_created_at = "2026-10-01T11:00:00+00:00"
        fork._disk_meta_created_at = "2026-10-01T12:00:00"
        assert identity(fork, parent) is unproven

    @pytest.mark.asyncio
    async def test_a_fork_mid_turn_is_refused(self, chats, model):
        chats.fork.task = asyncio.get_running_loop().create_future()

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (409, "fork_running")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("turn_ended", [False, True])
    async def test_a_turn_started_while_the_fork_is_read_is_refused(
        self, chats, model, monkeypatch, turn_ended
    ):
        read = chat_merge_back._read_inputs

        async def send_during_read(state, fork, parent, operation):
            # The send route writes the row and starts the turn in one step.
            _seed(fork, ("user", "now try memcached"))
            turn = asyncio.get_running_loop().create_future()
            if turn_ended:
                turn.set_result(None)
            fork.task = turn
            return await read(state, fork, parent, operation)

        monkeypatch.setattr(chat_merge_back, "_read_inputs", send_during_read)

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (409, "fork_running")
        assert model.calls == []

    @pytest.mark.asyncio
    async def test_a_merge_of_a_fork_mid_turn_is_refused(self, chats, model):
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            chats.fork.task = asyncio.get_running_loop().create_future()
            status, body = await _merge(client, "Redis works.", draft)

        assert (status, body.get("code")) == (409, "fork_running")
        assert _cards(chats.parent) == []
        assert chats.parent._pending_context == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("turn_ended", [False, True])
    async def test_a_turn_started_while_the_merge_reads_is_a_running_fork_not_a_stale_draft(
        self, chats, model, monkeypatch, turn_ended
    ):
        read = chat_merge_back._read_inputs

        async def send_during_read(state, fork, parent, operation):
            _seed(fork, ("user", "now try memcached"))
            turn = asyncio.get_running_loop().create_future()
            if turn_ended:
                turn.set_result(None)
            fork.task = turn
            return await read(state, fork, parent, operation)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(chat_merge_back, "_read_inputs", send_during_read)
            status, body = await _merge(client, "Redis works.", draft)

        assert (status, body.get("code")) == (409, "fork_running")
        assert _cards(chats.parent) == []
        assert chats.parent._pending_context == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_turn_streaming_at_the_source_check_is_a_running_fork_not_a_stale_draft(
        self, chats, model, monkeypatch
    ):
        state = chats.state
        loop = asyncio.get_running_loop()
        flush = state.flush_slot_now

        def flush_then_stream_a_chunk(slot):
            # A chunk landing right after the flush dirties the fork again, as a
            # turn streaming through the source check does.
            flush(slot)
            if slot is chats.fork:
                _seed(slot, ("assistant", "memcached: "))

        def start_streaming():
            chats.fork.task = loop.create_future()
            monkeypatch.setattr(state, "flush_slot_now", flush_then_stream_a_chunk)

        observations = _change_at_source_check(chats, monkeypatch, start_streaming)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "Redis works.", draft)

        assert (status, body.get("code")) == (409, "fork_running")
        assert observations == [True]
        assert _cards(chats.parent) == []
        assert chats.parent._pending_context == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["draft", "merge"])
    async def test_an_incognito_fork_is_refused_and_audited_once(self, chats, model, audit, route):
        chats.fork.memory_mode = "incognito"

        async with _client(chats.state) as client:
            if route == "draft":
                status, body = await _draft(client)
                operation = chat_merge_back._AUDIT_DRAFT
            else:
                through = chats.fork.messages[-1]["meta"]["mid"]
                status, body = await _merge(client, "summary", _ends_at(through))
                operation = chat_merge_back._AUDIT_MERGE

        assert (status, body["code"]) == (409, "merge_back_restricted")
        assert model.calls == []
        _assert_privacy_denial(audit, operation, resources="from=fork", transcript="fork")

    @pytest.mark.asyncio
    async def test_an_app_token_gets_the_uniform_not_found(self, chats, model):
        async with _client(chats.state, request_app="some-app") as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (404, "slot_not_found")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raised", "status", "code"),
        [
            (TranscriptBusy("lock timeout"), 503, "merge_history_busy"),
            (TranscriptWithheld("incognito line"), 409, "merge_back_restricted"),
        ],
    )
    async def test_a_fork_transcript_the_seam_will_not_read_is_refused(
        self, chats, model, monkeypatch, raised, status, code
    ):
        def refuse(key):
            raise raised

        monkeypatch.setattr(
            chats.state.conversation_log, "derive_messages_chained_full_with_keys", refuse
        )

        async with _client(chats.state) as client:
            got_status, body = await _draft(client)

        # A busy transcript is a retry, never the claim that the chat is private.
        assert (got_status, body["code"]) == (status, code)
        assert model.calls == []

    @pytest.mark.asyncio
    async def test_a_restricted_parent_chain_publishes_no_draft(self, chats, model, audit):
        _restrict_parent_chain(chats)

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (409, "merge_back_restricted")
        assert model.calls == []
        assert _cards(chats.parent) == []
        assert not chats.state.conversation_log.get_metadata(slot_history_key(chats.parent)).get(
            "deferred_notes"
        )
        _assert_privacy_denial(
            audit,
            chat_merge_back._AUDIT_DRAFT,
            resources="from=fork,to=parent",
            transcript="parent",
        )

    @pytest.mark.asyncio
    async def test_an_unreadable_parent_line_is_retryable(self, chats, model, monkeypatch):
        log = chats.state.conversation_log
        parent_key = slot_history_key(chats.parent)
        read_status = type(log)._read_metadata_status

        def unreadable_parent(self, key):
            if key == parent_key:
                return {}, False
            return read_status(self, key)

        monkeypatch.setattr(type(log), "_read_metadata_status", unreadable_parent)
        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (503, "merge_history_busy")
        assert body["error"] == "the fork or its parent is being written to; please retry"
        assert model.calls == []

    @pytest.mark.asyncio
    async def test_a_persistent_parent_archive_head_still_drafts(self, chats, model):
        _move_parent_rows_to_rotated_archive(chats)

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert body["messages"] == 2
        branch = model.calls[-1]["prompt"].split("FORK MESSAGES")[1]
        assert "try redis" in branch
        assert "pick a cache" not in branch

    @pytest.mark.asyncio
    async def test_a_persistent_fork_archive_head_still_drafts(self, chats, model):
        _seed(
            chats.fork,
            ("user", "now compare sqlite"),
            ("assistant", "sqlite is slower for this workload"),
        )
        await _move_older_fork_rows_to_rotated_archive(chats)

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert body["messages"] == 4
        assert body["digest"] == covered_digest(chats.fork.messages[2:])
        branch = model.calls[-1]["prompt"].split("FORK MESSAGES")[1]
        assert "try redis" in branch
        assert "redis works, TTL 300s" in branch
        assert "now compare sqlite" in branch

    @pytest.mark.asyncio
    async def test_a_failed_summary_is_a_retryable_error(self, chats, model):
        model.replies[0] = RuntimeError("bg session unavailable")

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (502, "merge_summary_failed")

    @pytest.mark.asyncio
    async def test_a_note_over_the_limit_comes_back_within_it_and_marked_trimmed(
        self, chats, model
    ):
        model.replies[0] = ("redis detail " * 400).strip()
        assert len(model.replies[0]) > chat_merge_back.MAX_MERGE_SUMMARY_CHARS

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert body["trimmed"] is True
        assert len(body["summary"]) <= chat_merge_back.MAX_MERGE_SUMMARY_CHARS
        # Cut at a word boundary, so the dialog opens on whole words.
        assert body["summary"].endswith("…")
        assert body["summary"][:-1].rsplit(" ", 1)[-1] in {"redis", "detail"}

    @pytest.mark.asyncio
    async def test_a_note_within_the_limit_is_not_marked_trimmed(self, chats, model):
        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert body["trimmed"] is False
        assert body["summary"] == "Tried Redis as the cache. It works; the TTL is 300s."

    @pytest.mark.asyncio
    async def test_a_fork_made_private_while_its_summary_is_written_gets_no_draft(
        self, chats, monkeypatch
    ):
        log = chats.state.conversation_log
        key = slot_history_key(chats.fork)

        async def private_meanwhile(sessions, prompt, **kwargs):
            # Another tab makes the fork incognito while the model writes.
            await asyncio.to_thread(log.update_metadata, key, {"memory_mode": "incognito"})
            return "Tried Redis."

        cfg = SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "m"))
        monkeypatch.setattr(chat_merge_back, "run_bg_oneliner", private_meanwhile)
        monkeypatch.setattr(chat_merge_back, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (409, "merge_back_restricted")
        assert "summary" not in body

    @pytest.mark.asyncio
    async def test_a_fork_closed_while_its_summary_is_written_gets_no_draft(
        self, chats, monkeypatch
    ):
        async def closed_meanwhile(sessions, prompt, **kwargs):
            chats.state._slots.pop("fork")
            return "Tried Redis."

        cfg = SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "m"))
        monkeypatch.setattr(chat_merge_back, "run_bg_oneliner", closed_meanwhile)
        monkeypatch.setattr(chat_merge_back, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (404, "slot_not_found")

    @pytest.mark.asyncio
    async def test_a_parent_made_private_while_its_summary_is_written_gets_no_draft(
        self, chats, monkeypatch, audit
    ):
        log = chats.state.conversation_log
        key = slot_history_key(chats.parent)

        async def private_meanwhile(sessions, prompt, **kwargs):
            await asyncio.to_thread(log.update_metadata, key, {"memory_mode": "incognito"})
            return "Tried Redis."

        cfg = SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "m"))
        monkeypatch.setattr(chat_merge_back, "run_bg_oneliner", private_meanwhile)
        monkeypatch.setattr(chat_merge_back, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (409, "merge_back_restricted")
        assert "summary" not in body
        _assert_privacy_denial(
            audit,
            chat_merge_back._AUDIT_DRAFT,
            resources="from=fork,to=parent",
            transcript="fork or parent",
        )

    @pytest.mark.asyncio
    async def test_a_parent_chain_that_changes_before_draft_publication_is_retryable(
        self, chats, model, monkeypatch
    ):
        log = chats.state.conversation_log
        parent_key = slot_history_key(chats.parent)
        chained_keys = log.chained_keys
        calls = 0

        def grow_at_publication(key):
            nonlocal calls
            keys = chained_keys(key)
            if key != parent_key:
                return keys
            calls += 1
            return keys if calls <= 2 else [*keys, "dashboard:chat-late"]

        monkeypatch.setattr(log, "chained_keys", grow_at_publication)
        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (503, "merge_history_busy")
        assert model.calls

    @pytest.mark.asyncio
    async def test_a_second_draft_while_the_first_is_written_is_refused(self, chats, monkeypatch):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(sessions, prompt, **kwargs):
            started.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            return "note"

        cfg = SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "m"))
        monkeypatch.setattr(chat_merge_back, "run_bg_oneliner", slow)
        monkeypatch.setattr(chat_merge_back, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))

        async with _client(chats.state) as client:
            first = asyncio.create_task(_draft(client))
            await asyncio.wait_for(started.wait(), timeout=10)
            second = await _draft(client)
            release.set()
            first_status, _ = await first

        assert (second[0], second[1]["code"]) == (409, "merge_draft_in_flight")
        assert first_status == 200

    @pytest.mark.asyncio
    async def test_a_credential_in_the_note_is_redacted_before_it_is_returned(self, chats, model):
        model.replies[0] = "Redis works with the key AKIAIOSFODNN7EXAMPLE."

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert "AKIAIOSFODNN7EXAMPLE" not in body["summary"]
        assert "[REDACTED: credential]" in body["summary"]

    @pytest.mark.asyncio
    async def test_an_overlong_note_is_cut_to_the_merge_limit(self, chats, model):
        model.replies[0] = "word " * 2000

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert len(body["summary"]) <= MAX_DEFERRED_NOTE_CHARS
        assert body["summary"].endswith("…")


class TestMerge:
    @pytest.mark.asyncio
    async def test_a_merge_waiting_behind_one_whose_write_fails_still_merges(
        self, chats, model, monkeypatch
    ):
        # Two tabs merge the same draft. The first note's durable write is held
        # open and then fails, which takes the note out of the hold again.
        entered, release = threading.Event(), threading.Event()
        write = chat_handlers.persist_deferred_notes_sync
        writes = 0

        def fail_the_first_write(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 1:
                entered.set()
                assert release.wait(10), "the first merge write was not released"
                raise OSError("disk full")
            return write(*args, **kwargs)

        parked = asyncio.Event()
        merge_lock = chat_merge_back._merge_lock

        class Parking:
            def __init__(self, lock):
                self.lock = lock

            async def __aenter__(self):
                if self.lock.locked():
                    parked.set()
                return await self.lock.__aenter__()

            async def __aexit__(self, *exc):
                return await self.lock.__aexit__(*exc)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(chat_handlers, "persist_deferred_notes_sync", fail_the_first_write)
            monkeypatch.setattr(
                chat_merge_back, "_merge_lock", lambda key: Parking(merge_lock(key))
            )
            first = asyncio.create_task(_merge(client, "Redis works.", draft))
            assert await asyncio.to_thread(
                entered.wait, 10
            ), "the first merge write never entered its hold"
            second = asyncio.create_task(_merge(client, "Redis works.", draft))
            await asyncio.wait_for(parked.wait(), timeout=10)
            release.set()
            first_status, first_body = await first
            second_status, second_body = await second

        assert (first_status, first_body["code"]) == (503, "deferred_note_persist_failed")
        # The second merge read the parent once the first had failed, so it is
        # not told the fork was already merged.
        assert second_status == 200, second_body
        assert len(_cards(chats.parent)) == 1

    @pytest.mark.asyncio
    async def test_a_fork_whose_rows_carry_no_ids_merges_once(self, chats, model):
        # Both chats as written before rows carried ids. A restore leaves such a
        # row without one in the window too.
        chats.state.flush_slot_now(chats.fork)
        for slot in (chats.parent, chats.fork):
            _drop_ids_on_disk(chats.state, slot)
            for row in slot.messages:
                if isinstance(row.get("meta"), dict):
                    row["meta"].pop("mid", None)

        async with _client(chats.state) as client:
            status, draft = await _draft(client)
            assert status == 200, draft
            status, body = await _merge(client, "Redis works.", draft)
            assert status == 200, body
            again, refused = await _draft(client)

        assert draft["messages"] == 2
        (card,) = _cards(chats.parent)
        block = card["meta"][MERGED_FROM_META_KEY]
        assert (block["after"], block["through"]) == (
            message_key(chats.fork.messages[1]),
            message_key(chats.fork.messages[-1]),
        )
        assert (again, refused["code"]) == (409, "nothing_to_merge")

    @pytest.mark.asyncio
    async def test_a_restricted_parent_chain_writes_no_merge_state(self, chats, model, audit):
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            audit.clear()
            _restrict_parent_chain(chats)
            status, body = await _merge(client, "Redis works.", draft)

        assert (status, body["code"]) == (409, "merge_back_restricted")
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []
        assert not chats.state.conversation_log.get_metadata(slot_history_key(chats.parent)).get(
            "deferred_notes"
        )
        _assert_privacy_denial(
            audit,
            chat_merge_back._AUDIT_MERGE,
            resources="from=fork,to=parent",
            transcript="parent",
        )

    @pytest.mark.asyncio
    async def test_a_persistent_parent_archive_head_still_merges(self, chats, model):
        _move_parent_rows_to_rotated_archive(chats)

        async with _client(chats.state) as client:
            status, draft = await _draft(client)
            assert status == 200, draft
            status, body = await _merge(client, "Redis works.", draft)

        assert status == 200, body
        assert body["messages"] == 2
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]

    @pytest.mark.asyncio
    async def test_a_persistent_fork_archive_head_still_merges(self, chats, model):
        _seed(
            chats.fork,
            ("user", "now compare sqlite"),
            ("assistant", "sqlite is slower for this workload"),
        )
        await _move_older_fork_rows_to_rotated_archive(chats)

        async with _client(chats.state) as client:
            status, draft = await _draft(client)
            assert status == 200, draft
            status, body = await _merge(client, "Redis and SQLite were compared.", draft)

        assert status == 200, body
        assert body["messages"] == 4
        (card,) = _cards(chats.parent)
        block = card["meta"][MERGED_FROM_META_KEY]
        covered = chats.fork.messages[2:]
        assert block["messages"] == 4
        assert block["digest"] == covered_digest(covered)

    @pytest.mark.asyncio
    async def test_a_fork_rewound_inside_its_source_hold_makes_the_draft_stale(
        self, chats, model, monkeypatch
    ):
        def rewind_fork():
            rewound = list(chats.fork.messages[:-1])
            saved = _save_slot_to_history(
                chats.state,
                chats.fork,
                rewound,
                expected_history_key=slot_history_key(chats.fork),
            )
            assert saved
            chats.fork.messages[:] = rewound
            chats.fork._dirty = False

        observations = _change_at_source_check(chats, monkeypatch, rewind_fork)
        parent_key = slot_history_key(chats.parent)
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "Redis works.", draft)
            assert (status, body["code"]) == (409, "merge_draft_stale")
            assert _cards(chats.parent) == []
            assert chats.parent._deferred_notes == []
            assert not chats.state.conversation_log.get_metadata(parent_key).get("deferred_notes")
            fresh_status, fresh = await _draft(client)
            merged_status, merged = await _merge(client, "Redis retry.", fresh)

        assert (fresh_status, merged_status) == (200, 200), (fresh, merged)
        assert observations and all(observations)
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis retry."]
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_variant_switch_inside_the_source_hold_makes_the_draft_stale(
        self, chats, model, monkeypatch
    ):
        reply = chats.fork.messages[-1]
        switched = {"content": "redis failed: no TTL support", "ts": "variant-ts"}

        def switch_variant():
            adopt_variant_text(reply, switched)
            reply["variant_idx"] = 0
            chats.fork._dirty = True

        observations = _change_at_source_check(chats, monkeypatch, switch_variant)
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "Redis works.", draft)

        assert (status, body["code"]) == (409, "merge_draft_stale")
        assert observations == [True]
        assert _cards(chats.parent) == []
        assert chats.parent._pending_context == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    async def test_an_append_after_through_inside_the_source_hold_still_merges(
        self, chats, model, monkeypatch
    ):
        observations = _change_at_source_check(
            chats, monkeypatch, lambda: _seed(chats.fork, ("user", "later question"))
        )
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "Redis works.", draft)

        assert status == 200, body
        assert observations == [True]
        (card,) = _cards(chats.parent)
        assert card["meta"][MERGED_FROM_META_KEY]["through"] == draft["through"]
        assert chats.fork.messages[-1]["content"] == "later question"

    @pytest.mark.asyncio
    async def test_a_source_key_without_a_source_check_keeps_plain_note_delivery(self, chats):
        log = chats.state.conversation_log
        fork_key = slot_history_key(chats.fork)
        _, fork_keys = log.derive_messages_chained_full_with_keys(fork_key)

        delivery = await chat_handlers.deliver_note(
            chats.state,
            chats.parent,
            content="plain source-backed note",
            source="plain note",
            durable=True,
            source_key=fork_key,
            source_expected_keys=fork_keys,
            source_expected_created_at=chats.fork._disk_meta_created_at,
        )

        assert not isinstance(delivery, web.Response)
        assert delivery.deferred is False
        assert chats.parent.messages[-1]["content"] == "plain source-backed note"

    @pytest.mark.asyncio
    async def test_a_fork_made_private_before_its_card_is_written_merges_nothing(
        self, chats, model, monkeypatch
    ):
        log = chats.state.conversation_log
        read = chat_merge_back._read_inputs

        async def private_after_the_read(state, fork, parent, operation):
            inputs = await read(state, fork, parent, operation)
            await asyncio.to_thread(
                log.update_metadata, slot_history_key(fork), {"memory_mode": "incognito"}
            )
            return inputs

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(chat_merge_back, "_read_inputs", private_after_the_read)
            status, body = await _merge(client, "Redis works.", draft)

        assert (status, body["code"]) == (409, "merge_back_restricted")
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("change", "expected"),
        [
            ("deleted", (404, "slot_not_found")),
            ("replaced", (404, "slot_not_found")),
            ("chain", (503, "merge_history_busy")),
        ],
    )
    async def test_a_fork_deleted_or_replaced_after_read_publishes_nothing(
        self, chats, model, monkeypatch, change, expected
    ):
        state = chats.state
        log = state.conversation_log
        fork_key = slot_history_key(chats.fork)
        read = chat_merge_back._read_inputs

        async def change_after_the_read(state, fork, parent, operation):
            inputs = await read(state, fork, parent, operation)
            if change == "chain":
                chained_keys = log.chained_keys

                def replaced_chain(key):
                    keys = chained_keys(key)
                    return [*keys, "dashboard:late-chain"] if key == fork_key else keys

                monkeypatch.setattr(log, "chained_keys", replaced_chain)
            else:
                await asyncio.to_thread(log.delete_session, fork_key)
                if change == "replaced":
                    await asyncio.to_thread(
                        log.update_metadata,
                        fork_key,
                        {"created_at": "2099-01-01T00:00:00+00:00"},
                    )
            return inputs

        async with _client(state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(chat_merge_back, "_read_inputs", change_after_the_read)
            status, body = await _merge(client, "Redis works.", draft)

        assert (status, body["code"]) == expected
        assert _cards(chats.parent) == []
        assert chats.parent._pending_context == []
        assert chats.parent._deferred_notes == []
        assert not log.get_metadata(slot_history_key(chats.parent)).get("deferred_notes")

    @pytest.mark.asyncio
    async def test_a_parent_made_private_before_its_card_is_written_merges_nothing(
        self, chats, model, monkeypatch, audit
    ):
        log = chats.state.conversation_log
        read = chat_merge_back._read_inputs

        async def private_after_the_read(state, fork, parent, operation):
            inputs = await read(state, fork, parent, operation)
            await asyncio.to_thread(
                log.update_metadata, slot_history_key(parent), {"memory_mode": "incognito"}
            )
            return inputs

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            audit.clear()
            monkeypatch.setattr(chat_merge_back, "_read_inputs", private_after_the_read)
            status, body = await _merge(client, "Redis works.", draft)

        assert (status, body["code"]) == (409, "merge_back_restricted")
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []
        _assert_privacy_denial(
            audit,
            chat_merge_back._AUDIT_MERGE,
            resources="from=fork,to=parent",
            transcript="fork or parent",
        )

    @pytest.mark.asyncio
    async def test_a_turn_ending_before_a_private_forks_card_is_checked_shows_nothing(
        self, chats, model, monkeypatch
    ):
        # The fork turns private after the merge read it, and the parent's turn
        # ends before the card's write checks the fork's privacy line. The card
        # waits for that check, which refuses it.
        log = chats.state.conversation_log
        hold = log.publication_hold
        paused, resume = threading.Event(), threading.Event()

        @contextmanager
        def check_after_the_turn_ends(key, **kwargs):
            if chats.parent._deferred_notes and not paused.is_set():
                paused.set()
                assert resume.wait(10), "the private-fork check was not released"
            with hold(key, **kwargs):
                yield

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            chats.parent.task = asyncio.get_running_loop().create_future()
            monkeypatch.setattr(log, "publication_hold", check_after_the_turn_ends)
            merging = asyncio.create_task(_merge(client, "Redis works.", draft))
            assert await asyncio.to_thread(
                paused.wait, 10
            ), "the private-fork check never entered its hold"
            log.update_metadata(slot_history_key(chats.fork), {"memory_mode": "incognito"})
            chats.parent.task = None
            chats.parent.flush_deferred_notes()
            resume.set()
            status, body = await merging

        assert (status, body["code"]) == (409, "merge_back_restricted")
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_save_while_a_private_forks_card_waits_puts_nothing_on_disk(
        self, chats, model, monkeypatch
    ):
        # The parent is saved while the card waits for its write, then the fork
        # turns private and that write's hold refuses the card. A restart must not
        # replay a card the save wrote down meanwhile.
        log = chats.state.conversation_log
        hold = log.publication_hold
        paused, resume = threading.Event(), threading.Event()

        @contextmanager
        def check_after_a_save(key, **kwargs):
            if chats.parent._deferred_notes and not paused.is_set():
                paused.set()
                assert resume.wait(10), "the save check was not released"
            with hold(key, **kwargs):
                yield

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            chats.parent.task = asyncio.get_running_loop().create_future()
            monkeypatch.setattr(log, "publication_hold", check_after_a_save)
            merging = asyncio.create_task(_merge(client, "Redis works.", draft))
            assert await asyncio.to_thread(paused.wait, 10), "the save check never entered its hold"
            chats.parent._dirty = True
            chats.state.flush_slot_now(chats.parent)
            log.update_metadata(slot_history_key(chats.fork), {"memory_mode": "incognito"})
            resume.set()
            status, body = await merging

        assert (status, body["code"]) == (409, "merge_back_restricted")
        assert not log.get_metadata(slot_history_key(chats.parent)).get("deferred_notes")

    @pytest.mark.asyncio
    async def test_a_merge_goes_to_the_parent_slot_running_a_turn_and_counts_there(
        self, chats, model
    ):
        # A channel-linked chat and its dashboard twin are two slots on one session.
        twin = chats.state.get_or_create_slot("twin")
        twin.linked_session_key = effective_session_key(chats.parent)
        twin._disk_meta_created_at = chats.parent._disk_meta_created_at
        assert effective_session_key(twin) == chats.fork.forked_from

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            twin.task = asyncio.get_running_loop().create_future()
            status, body = await _merge(client, "Redis works.", draft)
            assert status == 200, body
            # Held for the twin's turn, which owns the transcript's tail.
            assert (body["parent"], body["deferred"]) == ("twin", True)
            assert _cards(chats.parent) == []
            # The twin's turn ends; its window has the card, not yet on disk.
            twin.task = None
            twin.flush_deferred_notes()
            again, refused = await _draft(client)

        assert [c["meta"][MERGED_FROM_META_KEY]["slot"] for c in _cards(twin)] == ["fork"]
        assert (again, refused["code"]) == (409, "nothing_to_merge")

    @pytest.mark.asyncio
    async def test_the_merge_reaches_the_next_turn_whichever_parent_slot_runs_it(
        self, chats, model
    ):
        # Two idle slots on the parent's session. The card goes to the first, and
        # the person's next message arrives in the other one.
        twin = chats.state.get_or_create_slot("twin")
        twin.linked_session_key = effective_session_key(chats.parent)
        twin._disk_meta_created_at = chats.parent._disk_meta_created_at
        twin._titled = True

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "Redis works.", draft)
        assert (status, body["parent"], body["deferred"]) == (200, "parent", False), body

        prompts = await _run_turn(chats.state, twin, "carry on")

        assert "Redis works." in prompts[0]
        # Delivered once: the first slot does not keep it for a later turn.
        assert [c for c in chats.parent._pending_context if c["source"] == MERGE_NOTE_SOURCE] == []

    @pytest.mark.asyncio
    async def test_a_plain_note_is_delivered_by_the_slot_it_was_queued_on(self, chats):
        # The parent's session is open in two slots and no merge card is present.
        # A /note on the first slot is that slot's own: a turn on the other slot
        # does not carry it, and the first slot's next turn delivers it once.
        twin = chats.state.get_or_create_slot("twin")
        twin.linked_session_key = effective_session_key(chats.parent)
        twin._titled = True
        delivery = await chat_handlers.deliver_note(
            chats.state, chats.parent, content="plain note for parent", source="note"
        )
        assert not isinstance(delivery, web.Response)
        assert chats.parent._pending_context[-1]["noteSession"] == effective_session_key(twin)

        twin_prompts = await _run_turn(chats.state, twin, "carry on")
        parent_prompts = await _run_turn(chats.state, chats.parent, "and here")
        again = await _run_turn(chats.state, chats.parent, "once more")

        assert "plain note for parent" not in twin_prompts[0]
        assert parent_prompts[0].count("plain note for parent") == 1
        assert "plain note for parent" not in again[0]
        assert chats.parent._pending_context == []

    @pytest.mark.asyncio
    async def test_a_turn_takes_no_more_card_context_than_one_slot_can_hold(self, chats):
        # Each of the parent session's two slots holds a full source's worth of cards.
        twin = chats.state.get_or_create_slot("twin")
        twin.linked_session_key = effective_session_key(chats.parent)
        twin._titled = True
        session = effective_session_key(chats.parent)
        for slot in (chats.parent, twin):
            slot._pending_context = [
                {
                    **_queued_merge_context(n),
                    "content": f"{slot.key} card {n}",
                    "source": "watch",
                    "noteSession": session,
                    "noteId": f"{slot.key}-card-{n}",
                }
                for n in range(_MAX_CONTEXT_PER_SOURCE)
            ]

        prompts = await _run_turn(chats.state, twin, "carry on")

        assert prompts[0].count('[Background context from "watch"]') == _MAX_CONTEXT_PER_SOURCE
        # The rest waits on the slot it was queued on, for a later turn.
        assert [entry["content"] for entry in chats.parent._pending_context] == [
            f"parent card {n}" for n in range(_MAX_CONTEXT_PER_SOURCE)
        ]

    @pytest.mark.asyncio
    async def test_the_summary_lands_in_the_parent_as_a_note_with_its_card(self, chats, model):
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "Redis works. TTL 300s.", draft)

        assert status == 200, body
        assert body == {"ok": True, "parent": "parent", "messages": 2, "deferred": False}
        (card,) = _cards(chats.parent)
        assert card["role"] == "inject"
        assert card["content"] == "Redis works. TTL 300s."
        assert card["meta"]["noteSession"] == effective_session_key(chats.parent)
        assert card["meta"][MERGED_FROM_META_KEY] == {
            "session": effective_session_key(chats.fork),
            "slot": "fork",
            "title": "↳ Fork of Parent",
            "createdAt": chats.fork._disk_meta_created_at,
            # The range starts after the last message the fork copied.
            "after": chats.fork.messages[1]["meta"]["mid"],
            "through": draft["through"],
            "digest": draft["digest"],
            "messages": 2,
        }
        recorded_created_at = card["meta"][MERGED_FROM_META_KEY]["createdAt"]
        del chats.state._slots["fork"]
        restored_fork = _rehydrate_slot_from_history(chats.state, "fork")
        assert restored_fork is not None
        assert restored_fork.created_at == recorded_created_at
        # The parent's agent receives it on its next turn, and it does not expire.
        (context,) = [c for c in chats.parent._pending_context if c["source"] == MERGE_NOTE_SOURCE]
        assert context["content"] == "Redis works. TTL 300s."
        assert "maxAge" not in context

    @pytest.mark.asyncio
    async def test_a_draft_during_a_failed_card_write_offers_the_range_again(
        self, chats, model, monkeypatch
    ):
        durable_persist = chat_handlers._persist_deferred_note_hold
        write_started = asyncio.Event()
        release_write = asyncio.Event()

        async def persist_after_release(*args, **kwargs):
            write_started.set()
            await asyncio.wait_for(release_write.wait(), timeout=10)
            return await durable_persist(*args, **kwargs)

        def fail_write(*_args, **_kwargs):
            raise OSError("card write failed")

        async with _client(chats.state) as client:
            _, original = await _draft(client)
            monkeypatch.setattr(chat_handlers, "_persist_deferred_note_hold", persist_after_release)
            monkeypatch.setattr(chat_handlers, "persist_deferred_notes_sync", fail_write)
            merging = asyncio.create_task(_merge(client, "first attempt", original))
            await asyncio.wait_for(write_started.wait(), timeout=10)
            [waiting] = chats.parent._deferred_notes
            assert waiting[AWAITING_DURABLE_WRITE] is True
            try:
                draft_status, retry = await _draft(client)
            finally:
                release_write.set()
            merge_status, failed = await merging
            after_status, after = await _draft(client)

        assert (draft_status, retry["through"], retry["digest"]) == (
            200,
            original["through"],
            original["digest"],
        )
        assert (merge_status, failed["code"]) == (503, "deferred_note_persist_failed")
        assert chats.parent._deferred_notes == []
        assert (after_status, after["through"], after["digest"]) == (
            200,
            original["through"],
            original["digest"],
        )

    @pytest.mark.asyncio
    async def test_a_draft_during_a_successful_card_write_cannot_merge_twice(
        self, chats, model, monkeypatch
    ):
        durable_persist = chat_handlers._persist_deferred_note_hold
        write_started = asyncio.Event()
        release_write = asyncio.Event()

        async def persist_after_release(*args, **kwargs):
            write_started.set()
            await asyncio.wait_for(release_write.wait(), timeout=10)
            return await durable_persist(*args, **kwargs)

        async with _client(chats.state) as client:
            _, original = await _draft(client)
            monkeypatch.setattr(chat_handlers, "_persist_deferred_note_hold", persist_after_release)
            merging = asyncio.create_task(_merge(client, "first attempt", original))
            await asyncio.wait_for(write_started.wait(), timeout=10)
            [waiting] = chats.parent._deferred_notes
            assert waiting[AWAITING_DURABLE_WRITE] is True
            try:
                draft_status, concurrent = await _draft(client)
            finally:
                release_write.set()
            merge_status, merged = await merging
            duplicate_status, duplicate = await _merge(client, "duplicate attempt", concurrent)

        assert (draft_status, concurrent["through"], concurrent["digest"]) == (
            200,
            original["through"],
            original["digest"],
        )
        assert (merge_status, merged["ok"]) == (200, True)
        assert (duplicate_status, duplicate["code"]) == (409, "already_merged")
        assert len(_cards(chats.parent)) == 1

    @pytest.mark.asyncio
    async def test_an_idle_merge_is_on_disk_before_it_is_acknowledged(self, chats, model):
        log = chats.state.conversation_log
        key = slot_history_key(chats.parent)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "Redis works.", draft)
            # Before any save: the durable hold already carries the card.
            held = log._read_metadata(key).get("deferred_notes") or []

        assert (status, body["deferred"]) == (200, False)
        assert [n["merged_from"]["through"] for n in held] == [draft["through"]]
        (card,) = _cards(chats.parent)
        assert card["meta"]["noteId"] == held[0]["id"]
        # Saving the row does not retire a merge card while its context is unread.
        chats.state.flush_slot_now(chats.parent)
        assert log._read_metadata(key).get("deferred_notes")
        assert [
            row["meta"][MERGED_FROM_META_KEY]["through"]
            for row in log.read_messages_chained_full(key)
            if isinstance(row.get("meta"), dict) and MERGED_FROM_META_KEY in row["meta"]
        ] == [draft["through"]]
        persisted_rows = log.read_messages_chained(key)
        persisted_cards = merge_cards(persisted_rows, [], effective_session_key(chats.fork))
        assert [card["through"] for card in persisted_cards] == [draft["through"]]
        prompts = await _run_turn(chats.state, chats.parent, "continue")
        assert prompts[0].count("Redis works.") == 1
        chats.state.flush_slot_now(chats.parent)
        assert not log._read_metadata(key).get("deferred_notes")

    @pytest.mark.asyncio
    async def test_an_idle_cards_context_survives_save_and_restart_until_drained(
        self, chats, model
    ):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "durable summary", draft)
        assert (status, body["deferred"]) == (200, False)

        state.flush_slot_now(chats.parent)
        assert state.conversation_log._read_metadata(key).get("deferred_notes")
        del state._slots["parent"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert len(_cards(restored)) == 1
        assert restored._deferred_notes == []
        assert [entry["content"] for entry in restored._pending_context] == ["durable summary"]

        prompts = await _run_turn(state, restored, "continue")
        assert prompts[0].count("durable summary") == 1
        state.flush_slot_now(restored)
        del state._slots["parent"]
        again = _rehydrate_slot_from_history(state, "parent")
        assert again is not None
        assert again._pending_context == []
        assert again._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_running_parents_card_restores_only_undrained_context(self, chats, model):
        state = chats.state
        chats.parent.task = asyncio.get_running_loop().create_future()
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "held durable summary", draft)
        assert (status, body["deferred"]) == (200, True)
        chats.parent.task = None
        assert chats.parent.flush_deferred_notes() == 1
        state.flush_slot_now(chats.parent)

        del state._slots["parent"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert len(_cards(restored)) == 1
        assert restored._deferred_notes == []
        assert [entry["content"] for entry in restored._pending_context] == ["held durable summary"]
        prompts = await _run_turn(state, restored, "continue")
        assert prompts[0].count("held durable summary") == 1
        state.flush_slot_now(restored)
        del state._slots["parent"]
        again = _rehydrate_slot_from_history(state, "parent")
        assert again is not None
        assert again._pending_context == []

    @pytest.mark.asyncio
    async def test_history_resume_restores_one_committed_card_context(self, chats, model):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            merge_status, merged = await _merge(client, "resume summary", draft)
            close_status, closed = await _close_route(client)
            # Closed BEFORE the resume begins, whatever the clock's resolution.
            close_before_resume(state.conversation_log, key)
            resume_status, resumed = await _resume_route(client, key)

        assert (merge_status, merged["deferred"]) == (200, False)
        assert (close_status, closed) == (200, {"ok": True})
        assert resume_status == 200, resumed
        restored = state._slots["parent"]
        assert len(_cards(restored)) == 1
        assert restored._deferred_notes == []
        assert [entry["content"] for entry in restored._pending_context] == ["resume summary"]

        prompts = await _run_turn(state, restored, "continue")
        assert prompts[0].count("resume summary") == 1
        state.flush_slot_now(restored)
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")

        state._slots.pop("parent")
        boot_restored = _rehydrate_slot_from_history(state, "parent")
        assert boot_restored is not None
        assert len(_cards(boot_restored)) == 1
        assert boot_restored._pending_context == []
        assert boot_restored._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_parent_closed_during_the_card_write_restores_the_card_once(
        self, chats, model, monkeypatch
    ):
        state = chats.state
        key = slot_history_key(chats.parent)
        durable_write = chat_handlers.persist_deferred_notes_sync
        written = threading.Event()
        release = threading.Event()

        def write_then_wait(*args, **kwargs):
            outcome = durable_write(*args, **kwargs)
            written.set()
            assert release.wait(10), "the card write was not released"
            return outcome

        async with _client(state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(chat_handlers, "persist_deferred_notes_sync", write_then_wait)
            merging = asyncio.create_task(_merge(client, "write-race summary", draft))
            assert await asyncio.to_thread(written.wait, 10)
            closing = asyncio.create_task(_close_route(client))
            try:
                for _ in range(1000):
                    if "parent" not in state._slots:
                        break
                    await asyncio.sleep(0.01)
                assert "parent" not in state._slots
            finally:
                release.set()
            (close_status, closed), (merge_status, merged) = await asyncio.gather(closing, merging)
            # Closed BEFORE the resume begins, whatever the clock's resolution.
            close_before_resume(state.conversation_log, key)
            resume_status, resumed = await _resume_route(client, key)

        assert (close_status, closed) == (200, {"ok": True})
        assert (merge_status, merged["deferred"]) == (200, True)
        assert resume_status == 200, resumed
        restored = state._slots["parent"]
        assert _cards(restored) == []
        assert [entry["content"] for entry in restored._deferred_notes] == ["write-race summary"]
        assert restored.flush_deferred_notes() == 1
        assert [card["content"] for card in _cards(restored)] == ["write-race summary"]
        assert [entry["content"] for entry in restored._pending_context] == ["write-race summary"]
        prompts = await _run_turn(state, restored, "continue")
        assert prompts[0].count("write-race summary") == 1
        state.flush_slot_now(restored)
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")

        state._slots.pop("parent")
        boot_restored = _rehydrate_slot_from_history(state, "parent")
        assert boot_restored is not None
        assert [card["content"] for card in _cards(boot_restored)] == ["write-race summary"]
        assert boot_restored._pending_context == []
        assert boot_restored._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_plain_note_held_at_close_is_written_once_after_history_resume(self, chats):
        state = chats.state
        key = slot_history_key(chats.parent)
        chats.parent.task = asyncio.get_running_loop().create_future()
        delivery = await chat_handlers.deliver_note(
            state, chats.parent, content="held plain note", source="plain note"
        )
        assert not isinstance(delivery, web.Response)
        assert delivery.deferred is True

        async with _client(state) as client:
            close_status, closed = await _close_route(client)
            # Closed BEFORE the resume begins, whatever the clock's resolution.
            close_before_resume(state.conversation_log, key)
            resume_status, resumed = await _resume_route(client, key)

        assert (close_status, closed) == (200, {"ok": True})
        assert resume_status == 200, resumed
        restored = state._slots["parent"]
        assert [entry["content"] for entry in restored._deferred_notes] == ["held plain note"]
        assert restored.flush_deferred_notes() == 1
        prompts = await _run_turn(state, restored, "continue")
        assert prompts[0].count("held plain note") == 1
        assert sum(row.get("content") == "held plain note" for row in restored.messages) == 1
        state.flush_slot_now(restored)
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")

        state._slots.pop("parent")
        boot_restored = _rehydrate_slot_from_history(state, "parent")
        assert boot_restored is not None
        assert sum(row.get("content") == "held plain note" for row in boot_restored.messages) == 1
        assert boot_restored._pending_context == []
        assert boot_restored._deferred_notes == []

    @pytest.mark.asyncio
    async def test_history_resume_returns_the_live_slot_for_the_same_transcript(self, chats):
        state = chats.state
        parent, fork = _channel_parent_and_fork(state, _CHANNEL_STEM, linked=False)
        delivery = await _place_merge_card(state, parent, fork, "live summary")
        assert delivery.deferred is False
        assert effective_session_key(parent) != f"dashboard:{_CHANNEL_KEY}"
        assert transcripts_share_file(slot_history_key(parent), _CHANNEL_KEY)
        before_messages = list(parent.messages)
        before_context = list(parent._pending_context)
        before_hold = list(parent._deferred_notes)

        outcome = await chat_handlers.resume_slot_from_history(
            state, name="second-channel-parent", history_key=_CHANNEL_KEY
        )

        assert outcome.already_live is True
        assert outcome.slot is parent
        assert "second-channel-parent" not in state._slots
        assert parent.messages == before_messages
        assert parent._pending_context == before_context
        assert parent._deferred_notes == before_hold

    @pytest.mark.asyncio
    async def test_history_resume_of_a_chat_held_by_a_workflow_slot_returns_that_slot(self, chats):
        # A workflow launched from the parent finishes after the parent was closed,
        # so its result lands in a ``workflow-<run_id>`` slot bound to the parent's
        # session (``workflow_inject``). A History resume of the parent finds that
        # slot by its session and opens nothing beside it.
        state = chats.state
        session = effective_session_key(chats.parent)
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            close_status, closed = await _close_route(client)
            assert (close_status, closed) == (200, {"ok": True})
            assert workflow_inject.inject_workflow_result(
                state, "wf-1", _workflow_result_for(session, "wf-1")
            )
            held = state._slots["workflow-wf-1"]
            assert effective_session_key(held) == session
            resume_status, resumed = await _resume_route(client, key)

        assert resume_status == 200, resumed
        assert resumed["key"] == "workflow-wf-1"
        assert "parent" not in state._slots
        assert _slots_on_session(state, session) == [held]

    @pytest.mark.asyncio
    async def test_a_named_create_opens_the_chat_beside_its_workflow_slot_and_the_card_reaches_either_turn(
        self, chats, model
    ):
        # ``POST /api/chat/slots`` with the chat's own key dedups by name alone
        # (``api_chat_slot_create``), so it mints the chat beside the
        # ``workflow-<run_id>`` slot bound to its session: two live slots on one
        # dashboard session, neither ``channel_origin``. Once each has saved, both
        # hold the forked transcript, the merge lands on one, and the person's
        # next message in the other carries the card.
        state = chats.state
        session = effective_session_key(chats.parent)
        async with _client(state) as client:
            close_status, closed = await _close_route(client)
            assert (close_status, closed) == (200, {"ok": True})
            assert workflow_inject.inject_workflow_result(
                state, "wf-1", _workflow_result_for(session, "wf-1")
            )
            create = await client.post("/api/chat/slots", json={"name": "parent"})
            created = await create.json()
            assert (create.status, created["key"]) == (200, "parent"), created
            twins = _slots_on_session(state, session)
            assert [slot.key for slot in twins] == ["workflow-wf-1", "parent"]
            assert [slot.channel_origin for slot in twins] == [False, False]
            for slot in twins:
                await _run_turn(state, slot, f"a turn in {slot.key}")
                state.flush_slot_now(slot)
            assert len({slot._disk_meta_created_at for slot in twins}) == 1
            _, draft = await _draft(client)
            status, body = await _merge(client, "Redis works.", draft)
        assert (status, body["parent"], body["deferred"]) == (200, "workflow-wf-1", False), body
        carrier = state._slots["workflow-wf-1"]
        assert [entry["source"] for entry in carrier._pending_context] == [MERGE_NOTE_SOURCE]

        prompts = await _run_turn(state, state._slots["parent"], "carry on")

        assert "Redis works." in prompts[0]
        assert carrier._pending_context == []

    @pytest.mark.asyncio
    async def test_a_chat_minted_beside_its_workflow_slot_carries_the_card_on_its_first_turn(
        self, chats, model
    ):
        # The create that dedups by name mints the chat without hydrating its
        # transcript, so the minted slot has observed no identity when the person
        # sends their first message. The transcript is intact on disk, so the turn
        # reads its identity there and the card the workflow slot holds reaches
        # that first message rather than the one after it.
        state = chats.state
        session = effective_session_key(chats.parent)
        key = slot_history_key(chats.parent)
        original_created_at = chats.parent._disk_meta_created_at
        async with _client(state) as client:
            close_status, closed = await _close_route(client)
            assert (close_status, closed) == (200, {"ok": True})
            assert workflow_inject.inject_workflow_result(
                state, "wf-first", _workflow_result_for(session, "wf-first")
            )
            carrier = state._slots["workflow-wf-first"]
            await _run_turn(state, carrier, "workflow result acknowledged")
            state.flush_slot_now(carrier)
            assert carrier._disk_meta_created_at == original_created_at

            created = await client.post("/api/chat/slots", json={"name": "parent"})
            created_body = await created.json()
            assert (created.status, created_body["key"]) == (200, "parent"), created_body
            minted = state._slots["parent"]
            assert minted._disk_meta_created_at == ""
            assert state.conversation_log.thread_transcript_identity(key) == original_created_at

            _, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Redis works.", draft)
            assert (merge_status, merged["parent"], merged["deferred"]) == (
                200,
                "workflow-wf-first",
                False,
            )
            [card] = carrier._pending_context
            assert card["noteSession"] == session
            assert minted._disk_meta_created_at == ""

        prompts = await _run_turn(state, minted, "carry on")

        assert "Redis works." in prompts[0]
        assert carrier._pending_context == []
        assert minted._pending_context == []

    @pytest.mark.asyncio
    async def test_a_recreated_chat_does_not_take_a_deleted_chats_card_from_a_surviving_alias(
        self, chats, model
    ):
        state = chats.state
        session = effective_session_key(chats.parent)
        key = slot_history_key(chats.parent)
        original_created_at = chats.parent._disk_meta_created_at
        async with _client(state) as client:
            close_status, closed = await _close_route(client)
            assert (close_status, closed) == (200, {"ok": True})
            assert workflow_inject.inject_workflow_result(
                state, "wf-recycled", _workflow_result_for(session, "wf-recycled")
            )
            carrier = state._slots["workflow-wf-recycled"]
            await _run_turn(state, carrier, "workflow result acknowledged")
            state.flush_slot_now(carrier)
            assert carrier._disk_meta_created_at == original_created_at

            _, draft = await _draft(client)
            merge_status, merged = await _merge(client, "Deleted chat summary.", draft)
            assert (merge_status, merged["parent"], merged["deferred"]) == (
                200,
                "workflow-wf-recycled",
                False,
            )
            [stale_card] = carrier._pending_context
            # The log's metadata memo is warm for the file the delete removes.
            assert state.conversation_log.thread_transcript_identity(key) == original_created_at

            deleted = await client.delete(f"/api/sessions/{key}")
            assert (deleted.status, await deleted.json()) == (200, {"ok": True})
            assert state._slots["workflow-wf-recycled"] is carrier
            # No file, so the log reports no identity, and the memo did not keep
            # the deleted file's.
            assert state.conversation_log.thread_transcript_identity(key) is None

            created = await client.post(
                "/api/chat/slots",
                json={"name": "parent", "title": "Replacement chat"},
            )
            replacement_body = await created.json()
            assert (created.status, replacement_body["key"]) == (200, "parent")
            replacement = state._slots["parent"]
            assert replacement._disk_meta_created_at == ""
            assert carrier._disk_meta_created_at == original_created_at
            assert state.conversation_log.thread_transcript_identity(key) is None

        prompts = await _run_turn(state, replacement, "new conversation")

        assert "Deleted chat summary." not in prompts[0]
        assert carrier._pending_context == [stale_card]
        assert replacement._pending_context == []

        # The first turn's own write minted the replacement's file with a fresh
        # `created_at`, which the slot had observed by its drain, so both turns fence
        # on two observed identities; the read for a slot that has observed nothing
        # is pinned by test_a_turn_slot_whose_transcript_was_deleted_takes_nothing.
        # After a save the replacement holds the identity a recycled key threatens.
        state.flush_slot_now(replacement)
        assert replacement._disk_meta_created_at
        assert replacement._disk_meta_created_at != original_created_at
        assert (
            state.conversation_log.thread_transcript_identity(key)
            == replacement._disk_meta_created_at
        )
        assert carrier._disk_meta_created_at == original_created_at

        prompts = await _run_turn(state, replacement, "and another")

        assert "Deleted chat summary." not in prompts[0]
        assert carrier._pending_context == [stale_card]
        assert replacement._pending_context == []
        assert stale_card["noteId"] not in replacement._dropped_note_ids

    @pytest.mark.asyncio
    async def test_a_hooked_resume_publishes_the_chat_beside_a_workflow_slot_its_window_admitted(
        self, chats
    ):
        # Session control's revive resumes with a containment hook, which holds the
        # built slot RETRACTED while it awaits. A workflow result landing in that
        # window finds no live chat and mints the ``workflow-<run_id>`` slot on the
        # chat's session; the resume then publishes the chat beside it.
        state = chats.state
        session = effective_session_key(chats.parent)
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            close_status, closed = await _close_route(client)
            assert (close_status, closed) == (200, {"ok": True})
        # Closed BEFORE the resume begins, whatever the clock's resolution.
        close_before_resume(state.conversation_log, key)

        async def containment(slot):
            assert "parent" not in state._slots
            assert workflow_inject.inject_workflow_result(
                state, "wf-2", _workflow_result_for(session, "wf-2")
            )
            return None

        outcome = await chat_handlers.resume_slot_from_history(
            state, name="parent", history_key=key, containment=containment
        )

        assert outcome.refusal is None and outcome.slot is state._slots["parent"]
        twins = _slots_on_session(state, session)
        assert [slot.key for slot in twins] == ["workflow-wf-2", "parent"]
        assert [slot.channel_origin for slot in twins] == [False, False]

    @pytest.mark.asyncio
    async def test_a_rows_only_card_save_marks_the_carried_hold_delivered(self, chats, model):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, first_draft = await _draft(client)
            first_status, first_body = await _merge(client, "rows-only summary", first_draft)
            _seed(chats.fork, ("user", "follow up"), ("assistant", "follow-up answer"))
            _, second_draft = await _draft(client)
            second_status, second_body = await _merge(
                client, "second rows-only summary", second_draft
            )
        assert first_status == 200, first_body
        assert second_status == 200, second_body
        hold_before = state.conversation_log._read_metadata(key)["deferred_notes"]
        hold_ids = [entry["id"] for entry in hold_before]
        assert [entry.get("delivered") for entry in hold_before] == [None, None]

        chats.parent._tab_id = "rows-only-writer"
        assert _save_slot_to_history(state, chats.parent, force=True, rows_only=True)

        hold_after = state.conversation_log._read_metadata(key)["deferred_notes"]
        assert [entry["id"] for entry in hold_after] == hold_ids
        assert [entry["delivered"] for entry in hold_after] == [True, True]

    @pytest.mark.asyncio
    async def test_a_flush_refusal_restores_the_merge_context_later(
        self, chats, model, monkeypatch, caplog
    ):
        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        append_pending_context = type(parent).append_pending_context
        refused_once = False

        def refuse_the_flush_once(slot, entry):
            nonlocal refused_once
            if slot is parent and entry.get("content") == "late summary" and not refused_once:
                refused_once = True
                return False
            return append_pending_context(slot, entry)

        monkeypatch.setattr(type(parent), "append_pending_context", refuse_the_flush_once)
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.state"):
            async with _client(state) as client:
                _, draft = await _draft(client)
                status, body = await _merge(client, "late summary", draft)

        assert status == 200, body
        assert refused_once is True
        assert [card["content"] for card in _cards(parent)] == ["late summary"]
        assert all(entry["content"] != "late summary" for entry in parent._pending_context)
        assert any("without its context" in record.message for record in caplog.records)

        state.flush_slot_now(parent)
        [durable] = state.conversation_log.get_metadata(key)["deferred_notes"]
        assert durable["delivered"] is True
        assert durable["context"]["content"] == "late summary"

        del state._slots["parent"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert restored._deferred_notes == []
        assert [entry["content"] for entry in restored._pending_context] == ["late summary"]

    @pytest.mark.asyncio
    async def test_a_rows_only_card_rotated_out_of_live_rows_is_not_replayed(self, chats, model):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "rows-only rotated summary", draft)
        assert status == 200, body
        chats.parent._tab_id = "rows-only-writer"
        assert _save_slot_to_history(state, chats.parent, force=True, rows_only=True)
        _move_parent_rows_to_rotated_archive(chats)

        del state._slots["parent"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert _cards(restored) == []
        assert restored._deferred_notes == []
        assert [entry["content"] for entry in restored._pending_context] == [
            "rows-only rotated summary"
        ]

        restored.flush_deferred_notes()
        state.flush_slot_now(restored)
        rows = state.conversation_log.read_messages_chained_full(key)
        assert (
            sum(
                1
                for row in rows
                if isinstance(row.get("meta"), dict) and MERGED_FROM_META_KEY in row["meta"]
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_a_rows_only_card_save_restores_only_undrained_context(self, chats, model):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "rows-only summary", draft)
        assert status == 200, body
        chats.parent._tab_id = "rows-only-writer"
        assert _save_slot_to_history(state, chats.parent, force=True, rows_only=True)
        assert state.conversation_log._read_metadata(key).get("deferred_notes")

        del state._slots["parent"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert len(_cards(restored)) == 1
        assert restored._deferred_notes == []
        assert [entry["content"] for entry in restored._pending_context] == ["rows-only summary"]
        prompts = await _run_turn(state, restored, "continue")
        assert prompts[0].count("rows-only summary") == 1
        state.flush_slot_now(restored)
        del state._slots["parent"]
        again = _rehydrate_slot_from_history(state, "parent")
        assert again is not None
        assert again._pending_context == []

    @pytest.mark.asyncio
    async def test_a_twin_save_keeps_an_uncommitted_parent_card_for_restart(self, chats, model):
        state = chats.state
        key = slot_history_key(chats.parent)
        twin = state.get_or_create_slot("twin")
        twin.linked_session_key = effective_session_key(chats.parent)
        twin._disk_meta_created_at = chats.parent._disk_meta_created_at
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "restart-safe twin summary", draft)
        assert status == 200, body
        note_id = _cards(chats.parent)[0]["meta"]["noteId"]
        assert all(
            not isinstance(row.get("meta"), dict) or row["meta"].get("noteId") != note_id
            for row in state.conversation_log.read_messages_chained_full(key)
        )

        prompts = await _run_turn(state, twin, "continue")
        assert prompts[0].count("restart-safe twin summary") == 1
        state.flush_slot_now(twin)
        assert state.conversation_log._read_metadata(key).get("deferred_notes")

        del state._slots["parent"]
        del state._slots["twin"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert [entry["content"] for entry in restored._deferred_notes] == [
            "restart-safe twin summary"
        ]
        assert restored.flush_deferred_notes() == 1
        assert [card["content"] for card in _cards(restored)] == ["restart-safe twin summary"]
        assert [entry["content"] for entry in restored._pending_context] == [
            "restart-safe twin summary"
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("holder_saved_first", [False, True])
    async def test_a_twin_drain_marks_only_the_card_holder_for_retirement(
        self, chats, model, holder_saved_first
    ):
        state = chats.state
        key = slot_history_key(chats.parent)
        twin = state.get_or_create_slot("twin")
        twin.linked_session_key = effective_session_key(chats.parent)
        twin._disk_meta_created_at = chats.parent._disk_meta_created_at
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "twin summary", draft)
        assert status == 200, body
        if holder_saved_first:
            # The holder already wrote the card's row and is clean, so only the
            # consumption's own dirty mark can get its retirement saved.
            state.flush_slot_now(chats.parent)
            assert not chats.parent._dirty
            assert state.conversation_log._read_metadata(key).get("deferred_notes")

        prompts = await _run_turn(state, twin, "continue")
        assert prompts[0].count("twin summary") == 1
        note_id = _cards(chats.parent)[0]["meta"]["noteId"]
        assert note_id in chats.parent._dropped_note_ids
        assert chats.parent._dirty
        assert note_id not in twin._dropped_note_ids

        state._flush_dirty_slots()

        assert not state.conversation_log._read_metadata(key).get("deferred_notes")
        assert any(
            isinstance(row.get("meta"), dict) and row["meta"].get("noteId") == note_id
            for row in state.conversation_log.read_messages_chained_full(key)
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["error", "cancel"])
    async def test_an_uncompleted_turn_returns_merge_context_and_keeps_its_hold(
        self, chats, model, outcome
    ):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "retry summary", draft)
        assert status == 200, body

        await _run_turn(state, chats.parent, "continue", outcome=outcome)

        assert [entry["content"] for entry in chats.parent._pending_context] == ["retry summary"]
        assert chats.parent._dropped_note_ids == set()
        state.flush_slot_now(chats.parent)
        assert state.conversation_log.get_metadata(key).get("deferred_notes")

    @staticmethod
    async def _merged_card(chats, summary: str) -> str:
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, summary, draft)
        assert status == 200, body
        [context] = chats.parent._pending_context
        return context["noteId"]

    @staticmethod
    def _kept_reply(*events_before_end):
        """A reply kiro-cli keeps: *events_before_end*, then a normal ``end_turn``."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        async def events():
            for event in events_before_end:
                yield event
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        return events

    @staticmethod
    def _text(text: str):
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        return LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)

    async def _assert_card_reached_the_model_once(
        self, chats, summary: str, note_id: str, first_turn: list[str]
    ) -> None:
        """The failed turn sent the frame; the next turn does not send it again."""
        state = chats.state
        assert first_turn[0].count(summary) == 1
        assert chats.parent._pending_context == []
        following = await _run_turn(state, chats.parent, "and then")
        assert summary not in following[0]
        state.flush_slot_now(chats.parent)
        key = slot_history_key(chats.parent)
        held = state.conversation_log.get_metadata(key).get("deferred_notes") or []
        held_ids = {entry.get("id") for entry in held}
        assert note_id not in held_ids
        assert not held_ids

    @pytest.mark.asyncio
    async def test_a_promise_only_turn_retires_the_card_its_prompt_carried(self, chats, model):
        """The continuation runs on the session that already holds the frame."""
        note_id = await self._merged_card(chats, "promise summary")
        first = await _run_turn(
            chats.state,
            chats.parent,
            "continue",
            events=self._kept_reply(self._text("I'll go ahead and run the gate now.")),
        )
        assert chats.parent._promise_only_retries == 1
        await self._assert_card_reached_the_model_once(chats, "promise summary", note_id, first)

    @pytest.mark.asyncio
    async def test_a_promise_only_turn_under_auto_approve_retires_the_card(self, chats, model):
        """No continuation is queued, so the next USER turn must not re-send the frame."""
        note_id = await self._merged_card(chats, "yolo promise summary")
        first = await _run_turn(
            chats.state,
            chats.parent,
            "continue",
            events=self._kept_reply(self._text("I'll go ahead and run the gate now.")),
            yolo=True,
        )
        assert any(
            "Auto-continue is skipped" in row.get("content", "")
            for row in chats.parent.messages
            if row.get("role") == "notice"
        )
        await self._assert_card_reached_the_model_once(
            chats, "yolo promise summary", note_id, first
        )

    @pytest.mark.asyncio
    async def test_a_leaked_tool_call_turn_retires_the_card_its_prompt_carried(self, chats, model):
        leak = (
            "call <" + 'invoke name="spawn_run"> <' + 'parameter name="task">reconcile</'
            "parameter> </" + "invoke>"
        )
        note_id = await self._merged_card(chats, "leak summary")
        first = await _run_turn(
            chats.state, chats.parent, "continue", events=self._kept_reply(self._text(leak))
        )
        assert any(
            "A tool call leaked" in row.get("content", "")
            for row in chats.parent.messages
            if row.get("role") == "notice"
        )
        await self._assert_card_reached_the_model_once(chats, "leak summary", note_id, first)

    @pytest.mark.asyncio
    async def test_a_turn_cut_by_a_completed_compaction_retires_the_card(self, chats, model):
        """The summarized session was built from a prompt that carried the frame."""
        from kiro_crew.providers.base import EVENT_COMPACTION_STATUS, LLMEvent

        note_id = await self._merged_card(chats, "compaction summary")
        first = await _run_turn(
            chats.state,
            chats.parent,
            "continue",
            events=self._kept_reply(
                LLMEvent(kind=EVENT_COMPACTION_STATUS, text="started"),
                LLMEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="summary"),
            ),
        )
        assert chats.parent._compaction_continue_retries == 1
        await self._assert_card_reached_the_model_once(chats, "compaction summary", note_id, first)

    @pytest.mark.asyncio
    async def test_a_stop_after_the_first_token_returns_the_card_for_the_next_turn(
        self, chats, model
    ):
        """kiro-cli discards a cancelled prompt, so the frame goes out once more, and only once."""
        state = chats.state
        note_id = await self._merged_card(chats, "stopped summary")
        streamed = asyncio.Event()

        async def events():
            yield self._text("partial")
            streamed.set()
            await asyncio.wait_for(asyncio.Future(), timeout=10)

        stopped = await _run_turn(
            state, chats.parent, "continue", events=events, cancel_when=streamed
        )
        assert stopped[0].count("stopped summary") == 1
        assert [entry["noteId"] for entry in chats.parent._pending_context] == [note_id]
        assert chats.parent._dropped_note_ids == set()

        resent = await _run_turn(state, chats.parent, "continue")
        assert resent[0].count("stopped summary") == 1
        assert chats.parent._pending_context == []
        later = await _run_turn(state, chats.parent, "and then")
        assert "stopped summary" not in later[0]

    @pytest.mark.asyncio
    async def test_a_stop_during_the_retirement_proof_read_puts_no_card_back(
        self, chats, model, monkeypatch
    ):
        """The turn landed; a card whose proof did not finish keeps its durable hold only."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard import state as dashboard_state

        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        note_id = await self._merged_card(chats, "proof summary")
        state.flush_slot_now(parent)
        # Leave the card row-less, so retirement needs the durable proof read.
        monkeypatch.setattr(dashboard_state, "_MAX_SLOT_MESSAGES", 3)
        for index in range(3):
            parent.append("chunk", f"chunk-{index}")
        assert all(
            not isinstance(row.get("meta"), dict) or row["meta"].get("noteId") != note_id
            for row in parent.messages
        )

        loop = asyncio.get_running_loop()
        proof_started = asyncio.Event()
        proof_may_finish = threading.Event()

        def blocked_proof(*_args):
            loop.call_soon_threadsafe(proof_started.set)
            proof_may_finish.wait(10)
            return False

        # The retirement reads the proof through the runner's namespace.
        monkeypatch.setattr(chat_runner, "_merge_card_row_committed", blocked_proof)
        try:
            landed = await _run_turn(state, parent, "continue", cancel_when=proof_started)
        finally:
            proof_may_finish.set()

        assert landed[0].count("proof summary") == 1
        assert parent._pending_context == []
        assert parent._dropped_note_ids == set()
        assert parent._inflight_merge_contexts == []
        assert any(
            entry.get("id") == note_id
            for entry in state.conversation_log.get_metadata(key)["deferred_notes"]
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("row_in_window", [True, False], ids=["held", "rowless"])
    async def test_a_backend_error_after_output_retires_a_card_only_through_its_live_row(
        self, chats, model, monkeypatch, row_in_window
    ):
        """kiro-cli kept the prompt, so nothing is re-queued and the finally reads no proof.

        A card whose row the parent's window holds retires at turn end; a row-less
        card keeps its durable hold, since the proof read is an await the turn's
        ``finally`` must not run ahead of its other teardown steps.
        """
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard import state as dashboard_state

        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        note_id = await self._merged_card(chats, "error summary")
        state.flush_slot_now(parent)
        if not row_in_window:
            monkeypatch.setattr(dashboard_state, "_MAX_SLOT_MESSAGES", 3)
            for index in range(3):
                parent.append("chunk", f"chunk-{index}")
        row_held = any(
            isinstance(row.get("meta"), dict) and row["meta"].get("noteId") == note_id
            for row in parent.messages
        )
        assert row_held is row_in_window

        proof_reads: list[tuple] = []

        def recorded_proof(*args):
            proof_reads.append(args)
            return False

        monkeypatch.setattr(chat_runner, "_merge_card_row_committed", recorded_proof)

        async def events():
            yield self._text("partial")
            raise RuntimeError("provider failed after output streamed")

        failed = await _run_turn(state, parent, "continue", events=events)

        assert failed[0].count("error summary") == 1
        assert parent._pending_context == []
        assert parent._inflight_merge_contexts == []
        assert proof_reads == []
        if not row_in_window:
            assert parent._dropped_note_ids == set()
        state.flush_slot_now(parent)
        held = [
            entry.get("id")
            for entry in state.conversation_log.get_metadata(key).get("deferred_notes") or []
        ]
        # A held card's retirement rode the save that committed its row; a
        # row-less card's hold stays for a restart to re-deliver.
        assert held == ([] if row_in_window else [note_id])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("death", ["typed", "pipe"])
    async def test_a_process_death_after_output_returns_the_card_for_the_recovery_turn(
        self, chats, model, death
    ):
        """kiro-cli logs a prompt only once the model answered it (``replay.py``).

        The reloaded session lacks the prompt that carried the frame, the
        recovery requeue is a bare continuation, so the card must go back on the
        queue and out again exactly once, with its durable hold intact meanwhile.
        """
        from kiro_crew.acp.transport_errors import AcpError, AcpProcessDied

        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        note_id = await self._merged_card(chats, "death summary")
        state.flush_slot_now(parent)
        failure = (
            AcpProcessDied("kiro-cli exited (code 137)")
            if death == "typed"
            else AcpError("ACP process exited before the turn completed")
        )

        async def events():
            yield self._text("partial")
            raise failure

        died = await _run_turn(state, parent, "continue", events=events)

        assert died[0].count("death summary") == 1
        assert [entry["noteId"] for entry in parent._pending_context] == [note_id]
        assert parent._dropped_note_ids == set()
        assert parent._inflight_merge_contexts == []
        [recovery] = parent._queue
        assert "death summary" not in recovery["content"]
        state.flush_slot_now(parent)
        held = [
            entry.get("id")
            for entry in state.conversation_log.get_metadata(key).get("deferred_notes") or []
        ]
        assert held == [note_id]

        parent._queue.clear()
        recovered = await _run_turn(state, parent, recovery["content"])
        await self._assert_card_reached_the_model_once(chats, "death summary", note_id, recovered)

    @pytest.mark.asyncio
    async def test_a_retained_image_discard_restores_the_full_merge_card(self, chats, model):
        """A discarded native conversation cannot retain its drained card prompt."""
        from unittest.mock import AsyncMock

        from kiro_crew.acp.client import AcpError

        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        summary = "retained-image merge " + "0123456789" * 220
        assert len(summary) > 2000
        note_id = await self._merged_card(chats, summary)
        state.flush_slot_now(parent)
        failure = AcpError("The model could not process an image", transient=False)
        failure.structural_terminal = True
        failure.image_format_unsupported = True
        state.sessions.discard_conversation = AsyncMock()

        async def events():
            yield self._text("partial")
            raise failure

        rejected = await _run_turn(state, parent, "continue", events=events)

        assert rejected[0].count(summary) == 1
        state.sessions.discard_conversation.assert_awaited_once()
        assert [entry["noteId"] for entry in parent._pending_context] == [note_id]
        assert parent._dropped_note_ids == set()
        assert parent._inflight_merge_contexts == []
        [recovery] = parent._queue
        assert summary not in recovery["content"]
        state.flush_slot_now(parent)
        held = [
            entry.get("id")
            for entry in state.conversation_log.get_metadata(key).get("deferred_notes") or []
        ]
        assert held == [note_id]

        parent._queue.clear()
        recovered = await _run_turn(state, parent, recovery["content"])
        await self._assert_card_reached_the_model_once(chats, summary, note_id, recovered)

    @pytest.mark.asyncio
    async def test_a_compaction_failure_after_output_returns_the_card_for_the_next_turn(
        self, chats, model
    ):
        """The completion is synthetic and the turn was never answered, so the
        reset session lacks the prompt that carried the frame: the card goes back
        on the queue, keeps its durable hold, and the next turn carries it once."""
        from kiro_crew.acp.types import STOP_REASON_COMPACTION_FAILED
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        note_id = await self._merged_card(chats, "compaction-failed summary")
        state.flush_slot_now(parent)

        async def events():
            yield self._text("partial")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_COMPACTION_FAILED)

        abandoned = await _run_turn(state, parent, "continue", events=events)

        assert abandoned[0].count("compaction-failed summary") == 1
        state.sessions.reset.assert_awaited_once()
        assert parent._queue == []
        assert [entry["noteId"] for entry in parent._pending_context] == [note_id]
        assert parent._dropped_note_ids == set()
        assert parent._inflight_merge_contexts == []
        state.flush_slot_now(parent)
        held = [
            entry.get("id")
            for entry in state.conversation_log.get_metadata(key).get("deferred_notes") or []
        ]
        assert held == [note_id]

        resent = await _run_turn(state, parent, "continue")
        await self._assert_card_reached_the_model_once(
            chats, "compaction-failed summary", note_id, resent
        )

    @pytest.mark.asyncio
    async def test_a_runtime_death_reported_as_a_stop_reason_returns_the_card(self, chats, model):
        """The ``error:`` stop class replaces the runtime on the next claim with no
        reset flag set, so the card's return must follow from the terminal itself."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        note_id = await self._merged_card(chats, "unacked summary")
        state.flush_slot_now(parent)

        async def events():
            yield self._text("partial")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="error: cancel unacked")

        died = await _run_turn(state, parent, "continue", events=events)

        assert died[0].count("unacked summary") == 1
        state.sessions.reset.assert_not_awaited()
        assert [entry["noteId"] for entry in parent._pending_context] == [note_id]
        assert parent._dropped_note_ids == set()
        assert parent._inflight_merge_contexts == []
        [recovery] = parent._queue
        assert "unacked summary" not in recovery["content"]
        state.flush_slot_now(parent)
        held = [
            entry.get("id")
            for entry in state.conversation_log.get_metadata(key).get("deferred_notes") or []
        ]
        assert held == [note_id]

        parent._queue.clear()
        recovered = await _run_turn(state, parent, recovery["content"])
        await self._assert_card_reached_the_model_once(chats, "unacked summary", note_id, recovered)

    @pytest.mark.asyncio
    async def test_a_card_held_through_a_restart_and_a_failed_turn_is_delivered_once(
        self, chats, model
    ):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "restart retry summary", draft)
        assert (status, body["deferred"]) == (200, False)
        state.flush_slot_now(chats.parent)
        del state._slots["parent"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert [entry["content"] for entry in restored._pending_context] == [
            "restart retry summary"
        ]

        failed = await _run_turn(state, restored, "continue", outcome="error")
        assert failed[0].count("restart retry summary") == 1
        assert [entry["content"] for entry in restored._pending_context] == [
            "restart retry summary"
        ]
        assert restored._dropped_note_ids == set()
        assert state.conversation_log.get_metadata(key).get("deferred_notes")

        succeeded = await _run_turn(state, restored, "continue again")
        assert succeeded[0].count("restart retry summary") == 1
        assert restored._pending_context == []
        state.flush_slot_now(restored)
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")

        later = await _run_turn(state, restored, "one more")
        assert "restart retry summary" not in later[0]
        del state._slots["parent"]
        again = _rehydrate_slot_from_history(state, "parent")
        assert again is not None
        assert again._pending_context == []
        assert again._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_running_turns_drained_cards_still_fill_the_merge_cap(self, chats, model):
        for index in range(_MAX_CONTEXT_PER_SOURCE):
            chats.parent.append_pending_context(_queued_merge_context(index))
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            started, release = asyncio.Event(), asyncio.Event()
            turn = asyncio.create_task(
                _run_turn(chats.state, chats.parent, "continue", started=started, release=release)
            )
            chats.parent.task = turn
            await asyncio.wait_for(started.wait(), timeout=10)
            status, body = await _merge(client, "one too many", draft)
            release.set()
            await turn

        assert (status, body["code"]) == (429, "merge_queue_full")
        assert _cards(chats.parent) == []

    @pytest.mark.asyncio
    async def test_a_completed_turns_cards_free_the_merge_cap_before_it_unwinds(
        self, chats, model, monkeypatch
    ):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_handlers import _source_cap_reached

        for index in range(_MAX_CONTEXT_PER_SOURCE):
            chats.parent.append_pending_context(_queued_merge_context(index))
        cap_reached_once_landed: list[bool] = []
        monkeypatch.setattr(
            chat_runner,
            "record_interaction_event",
            lambda *_args: cap_reached_once_landed.append(
                _source_cap_reached(chats.parent, MERGE_NOTE_SOURCE)
            ),
        )

        await _run_turn(chats.state, chats.parent, "continue")

        assert cap_reached_once_landed == [False]

    @pytest.mark.asyncio
    async def test_a_failed_turn_restores_cards_accepted_up_to_the_cap(self, chats, model):
        drained = 6
        accepted = _MAX_CONTEXT_PER_SOURCE - drained
        for index in range(drained):
            chats.parent.append_pending_context(_queued_merge_context(index))
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            started, release = asyncio.Event(), asyncio.Event()
            turn = asyncio.create_task(
                _run_turn(
                    chats.state,
                    chats.parent,
                    "continue",
                    outcome="error",
                    started=started,
                    release=release,
                )
            )
            chats.parent.task = turn
            await asyncio.wait_for(started.wait(), timeout=10)
            for index in range(accepted):
                status, body = await _merge(client, f"accepted merge {index}", draft)
                assert status == 200, body
                if index + 1 < accepted:
                    _seed(
                        chats.fork,
                        ("user", f"follow-up {index}"),
                        ("assistant", f"answer {index}"),
                    )
                    _, draft = await _draft(client)
            release.set()
            await turn

        assert len(chats.parent._pending_context) == _MAX_CONTEXT_PER_SOURCE
        assert all(entry.get("noteId") for entry in chats.parent._pending_context)
        from kiro_crew.dashboard.chat_runner import drain_pending_context

        next_prefix = drain_pending_context(chats.parent)
        assert next_prefix.count(f'[Background context from "{MERGE_NOTE_SOURCE}"]') == (
            _MAX_CONTEXT_PER_SOURCE
        )

    @pytest.mark.asyncio
    async def test_a_failed_turn_restores_cards_without_overfilling_the_queue(self, chats, model):
        card_count = 4
        for index in range(card_count):
            assert chats.parent.append_pending_context(_queued_merge_context(index)) is True
        started, release = asyncio.Event(), asyncio.Event()
        turn = asyncio.create_task(
            _run_turn(
                chats.state,
                chats.parent,
                "continue",
                outcome="error",
                started=started,
                release=release,
            )
        )
        chats.parent.task = turn
        await asyncio.wait_for(started.wait(), timeout=10)
        ordinary_count = _MAX_PENDING_CONTEXT - card_count
        for index in range(_MAX_PENDING_CONTEXT):
            seated = chats.parent.append_pending_context(
                {
                    "content": f"ordinary {index}",
                    "source": f"other {index}",
                    "ephemeral": True,
                    "injectedAt": 1e12,
                }
            )
            assert seated is (index < ordinary_count)
        release.set()
        await turn

        assert [entry["content"] for entry in chats.parent._pending_context] == [
            *(f"queued merge {index}" for index in range(card_count)),
            *(f"ordinary {index}" for index in range(ordinary_count)),
        ]

    @pytest.mark.asyncio
    async def test_a_completed_turn_retires_the_merge_context_in_its_own_save(
        self, chats, model, monkeypatch
    ):
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "durable completed summary", draft)
        assert status == 200, body
        [held] = state.conversation_log.get_metadata(key)["deferred_notes"]

        saved_holds: list[list[dict]] = []
        save_slot_off_loop = chat_runner.save_slot_off_loop

        async def capture_turn_save(saved_state, saved_slot, *args, **kwargs):
            saved = await save_slot_off_loop(saved_state, saved_slot, *args, **kwargs)
            saved_holds.append(
                saved_state.conversation_log.get_metadata(key).get("deferred_notes") or []
            )
            return saved

        monkeypatch.setattr(chat_runner, "save_slot_off_loop", capture_turn_save)
        await _run_turn(state, chats.parent, "continue")

        assert saved_holds and saved_holds[0] == []
        assert held["id"] not in chats.parent._dropped_note_ids
        del state._slots["parent"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert restored._pending_context == []

    @pytest.mark.asyncio
    async def test_a_completed_turn_retires_the_merge_context_hold(self, chats, model):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "completed summary", draft)
        assert status == 200, body

        await _run_turn(state, chats.parent, "continue")
        state.flush_slot_now(chats.parent)

        assert chats.parent._pending_context == []
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")

    @pytest.mark.asyncio
    async def test_a_delivered_card_rotated_out_of_the_live_file_is_not_replayed(
        self, chats, model
    ):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "rotated summary", draft)
        assert status == 200, body
        state.flush_slot_now(chats.parent)
        [held] = state.conversation_log.get_metadata(key)["deferred_notes"]
        assert held["delivered"] is True
        _move_parent_rows_to_rotated_archive(chats)

        del state._slots["parent"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert _cards(restored) == []
        assert restored._deferred_notes == []
        assert [entry["content"] for entry in restored._pending_context] == ["rotated summary"]
        restored.flush_deferred_notes()
        state.flush_slot_now(restored)
        rows = state.conversation_log.read_messages_chained_full(key)
        assert (
            sum(
                1
                for row in rows
                if isinstance(row.get("meta"), dict) and MERGED_FROM_META_KEY in row["meta"]
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_a_channel_linked_parents_card_retires_after_a_restart(self, chats, model):
        state = chats.state
        parent, fork = _channel_parent_and_fork(state, _CHANNEL_STEM, linked=True)
        key = slot_history_key(parent)
        delivery = await _place_merge_card(state, parent, fork, "channel summary")
        assert delivery.deferred is False
        state.flush_slot_now(parent)
        assert len(state.conversation_log.get_metadata(key)["deferred_notes"]) == 1

        del state._slots[_CHANNEL_STEM]
        restored = _rehydrate_slot_from_history(state, _CHANNEL_STEM)
        assert restored is not None
        assert restored.linked_session_key == _CHANNEL_KEY
        [context] = restored._pending_context
        assert (context["content"], context["noteTranscript"]) == ("channel summary", _CHANNEL_STEM)

        prompts = await _run_turn(state, restored, "continue")
        assert prompts[0].count("channel summary") == 1
        state.flush_slot_now(restored)
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")
        del state._slots[_CHANNEL_STEM]
        again = _rehydrate_slot_from_history(state, _CHANNEL_STEM)
        assert again is not None
        assert again._pending_context == []

    @pytest.mark.asyncio
    async def test_a_channel_parent_restored_as_a_recent_session_retires_its_card(
        self, chats, model
    ):
        state = chats.state
        parent, fork = _channel_parent_and_fork(state, _CHANNEL_STEM, linked=True)
        key = slot_history_key(parent)
        delivery = await _place_merge_card(state, parent, fork, "recent channel summary")
        assert delivery.deferred is False
        state.flush_slot_now(parent)
        log = state.conversation_log
        meta = log.get_metadata(key)
        assert len(meta["deferred_notes"]) == 1
        messages = log.read_messages_chained(key)

        del state._slots[_CHANNEL_STEM]
        # The stem is the key the restore reads the file by; the dispatcher's own
        # skip of channel keys is bypassed to reach the apply half directly.
        _apply_recent_session(
            state,
            _CHANNEL_STEM,
            _CHANNEL_STEM,
            {},
            meta,
            messages,
            conv_log=log,
            kiro_model_map={},
            restore_cfg=None,
        )
        restored = state._slots[_CHANNEL_STEM]
        assert restored.linked_session_key == _CHANNEL_KEY
        [context] = restored._pending_context
        assert context["noteTranscript"] == _CHANNEL_STEM

        prompts = await _run_turn(state, restored, "continue")
        assert prompts[0].count("recent channel summary") == 1
        state.flush_slot_now(restored)
        assert not log.get_metadata(key).get("deferred_notes")

    @pytest.mark.asyncio
    async def test_an_unbound_channel_parent_bound_later_retires_its_card(self, chats, model):
        from kiro_crew.dashboard.channel_slots import _rebind_unbound_channel_slot

        state = chats.state
        parent, fork = _channel_parent_and_fork(state, _CHANNEL_STEM, linked=False)
        assert slot_history_key(parent) == _CHANNEL_STEM
        delivery = await _place_merge_card(state, parent, fork, "unbound summary")
        assert delivery.deferred is False
        [context] = parent._pending_context
        assert context["noteTranscript"] == _CHANNEL_STEM
        state.flush_slot_now(parent)
        assert len(state.conversation_log.get_metadata(_CHANNEL_STEM)["deferred_notes"]) == 1

        parent._channel_runtime_origin = True
        assert _rebind_unbound_channel_slot(state, parent, _CHANNEL_KEY)
        assert slot_history_key(parent) == _CHANNEL_KEY

        await _run_turn(state, parent, "continue")
        assert parent._pending_context == []
        state.flush_slot_now(parent)
        assert not state.conversation_log.get_metadata(_CHANNEL_KEY).get("deferred_notes")
        del state._slots[_CHANNEL_STEM]
        again = _rehydrate_slot_from_history(state, _CHANNEL_STEM)
        assert again is not None
        assert again._pending_context == []

    @pytest.mark.asyncio
    async def test_a_card_dropped_as_foreign_authorized_at_the_drain_retires(self, chats, model):
        state = chats.state
        parent, fork = _channel_parent_and_fork(state, _CHANNEL_STEM, linked=False)
        delivery = await _place_merge_card(state, parent, fork, "foreign summary")
        assert delivery.deferred is False
        [context] = parent._pending_context
        assert (
            context["noteSession"] == effective_session_key(parent) == f"dashboard:{_CHANNEL_STEM}"
        )
        state.flush_slot_now(parent)
        assert len(state.conversation_log.get_metadata(_CHANNEL_STEM)["deferred_notes"]) == 1

        parent.linked_session_key = _CHANNEL_KEY
        prompts = await _run_turn(state, parent, "continue")

        # The drain dropped the context as another session's, so the agent never
        # read it; the drop is this turn's consumption of the card all the same.
        assert "foreign summary" not in prompts[0]
        assert parent._pending_context == []
        state.flush_slot_now(parent)
        assert not state.conversation_log.get_metadata(_CHANNEL_KEY).get("deferred_notes")

    def test_a_returned_card_is_not_queued_twice_under_another_spelling_of_its_file(self, chats):
        from kiro_crew.dashboard.chat_runner import _restore_consumed_merge_contexts

        parent = chats.parent
        parent.channel_origin = True
        queued = {**_queued_merge_context(1), "noteTranscript": _CHANNEL_STEM}
        parent.append_pending_context(queued)
        consumed = {**_queued_merge_context(1), "noteTranscript": _CHANNEL_KEY}

        _restore_consumed_merge_contexts(parent, [consumed])

        assert parent._pending_context == [queued]

    @pytest.mark.asyncio
    async def test_the_routes_scan_the_full_corpora_off_the_event_loop(
        self, chats, model, monkeypatch
    ):
        archived_mids = {row["meta"]["mid"] for row in chats.parent.messages}
        _move_parent_rows_to_rotated_archive(chats)
        loop_thread = threading.get_ident()
        on_loop_scans: list[str] = []

        def watched(name: str):
            original = getattr(chat_merge_back, name)

            def wrapper(*args, **kwargs):
                if threading.get_ident() == loop_thread:
                    on_loop_scans.append(name)
                return original(*args, **kwargs)

            monkeypatch.setattr(chat_merge_back, name, wrapper)

        watched("_pre_id_pool")
        watched("_message_positions")
        merge_cards_ = chat_merge_back.merge_cards

        def cards_watched(rows, held_notes, fork_session):
            rows = list(rows)
            # Scanning the live window on the loop is bounded work; a row the
            # archive holds can only reach the loop through the full corpus.
            if threading.get_ident() == loop_thread and any(
                isinstance(row, dict)
                and isinstance(row.get("meta"), dict)
                and row["meta"].get("mid") in archived_mids
                for row in rows
            ):
                on_loop_scans.append("merge_cards")
            return merge_cards_(rows, held_notes, fork_session)

        monkeypatch.setattr(chat_merge_back, "merge_cards", cards_watched)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "summary", draft)

        assert status == 200, body
        assert len(_cards(chats.parent)) == 1
        assert on_loop_scans == []

    def test_an_inflight_merge_card_holds_a_pending_context_seat(self, chats):
        for index in range(_MAX_PENDING_CONTEXT - 1):
            assert (
                chats.parent.append_pending_context(
                    {
                        "content": f"plain-{index}",
                        "source": f"plain-{index}",
                        "injectedAt": 1e12,
                    }
                )
                is True
            )
        assert chats.parent.has_pending_context_seat() is True

        chats.parent._inflight_merge_contexts = [_queued_merge_context(0)]

        assert chats.parent.has_pending_context_seat() is False
        chats.parent._inflight_merge_contexts = []
        assert chats.parent.has_pending_context_seat() is True

    @pytest.mark.asyncio
    async def test_a_turn_ending_while_the_cards_write_fails_shows_nothing(
        self, chats, model, monkeypatch
    ):
        # The parent is mid-turn, so the card is held, and that turn ends while the
        # hold is written. The card waits for the write, which then fails.
        entered, release, failing = threading.Event(), threading.Event(), threading.Event()
        failing.set()
        write = chat_handlers.persist_deferred_notes_sync

        def fail_after_the_turn_ends(*args, **kwargs):
            if not failing.is_set():
                return write(*args, **kwargs)
            entered.set()
            assert release.wait(10), "the failed card write was not released"
            raise OSError("disk full")

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            chats.parent.task = asyncio.get_running_loop().create_future()
            monkeypatch.setattr(
                chat_handlers, "persist_deferred_notes_sync", fail_after_the_turn_ends
            )
            merging = asyncio.create_task(_merge(client, "Redis works.", draft))
            assert await asyncio.to_thread(
                entered.wait, 10
            ), "the failed card write never entered its hold"
            chats.parent.task = None
            chats.parent.flush_deferred_notes()
            release.set()
            status, body = await merging
            shown, held = _cards(chats.parent), list(chats.parent._deferred_notes)
            # Once writes work again the same draft merges, once.
            failing.clear()
            retry_status, retry_body = await _merge(client, "Redis works.", draft)

        assert (status, body["code"]) == (503, "deferred_note_persist_failed")
        assert (shown, held) == ([], [])
        assert retry_status == 200, retry_body
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]

    @pytest.mark.asyncio
    async def test_a_card_its_parents_turn_end_kept_back_is_shown_once_written(
        self, chats, model, monkeypatch
    ):
        entered, release = threading.Event(), threading.Event()
        write = chat_handlers.persist_deferred_notes_sync

        def written_after_the_turn_ends(*args, **kwargs):
            entered.set()
            assert release.wait(10), "the successful card write was not released"
            return write(*args, **kwargs)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            chats.parent.task = asyncio.get_running_loop().create_future()
            monkeypatch.setattr(
                chat_handlers, "persist_deferred_notes_sync", written_after_the_turn_ends
            )
            merging = asyncio.create_task(_merge(client, "Redis works.", draft))
            assert await asyncio.to_thread(
                entered.wait, 10
            ), "the successful card write never entered its hold"
            chats.parent.task = None
            chats.parent.flush_deferred_notes()
            kept_back = _cards(chats.parent)
            release.set()
            status, body = await merging

        assert kept_back == []
        # The turn is over, so the card is shown as soon as it is written.
        assert (status, body["deferred"]) == (200, False), body
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("deleted", ["before the hold is written", "after it is written"])
    async def test_a_parent_deleted_and_replaced_while_its_card_is_written_gets_nothing(
        self, chats, model, monkeypatch, deleted
    ):
        # The person permanently deletes the parent while the card is written, and
        # a new chat is made on its key before the merge answers.
        state = chats.state
        original_created_at = "2026-10-01T12:00:00+00:00"
        chats.parent.created_at = original_created_at
        state.conversation_log.update_metadata(
            slot_history_key(chats.parent), {"created_at": original_created_at}
        )
        chats.parent._disk_meta_created_at = original_created_at
        chats.fork.forked_from_created_at = original_created_at
        paused, resume = threading.Event(), threading.Event()
        write = chat_handlers.persist_deferred_notes_sync

        def pause_around_the_write(*args, **kwargs):
            if deleted.startswith("before"):
                paused.set()
                assert resume.wait(10), "the parent write was not released"
                return write(*args, **kwargs)
            outcome = write(*args, **kwargs)
            paused.set()
            assert resume.wait(10), "the parent write was not released"
            return outcome

        async with _client(state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(
                chat_handlers, "persist_deferred_notes_sync", pause_around_the_write
            )
            merging = asyncio.create_task(_merge(client, "Redis works.", draft))
            assert await asyncio.to_thread(
                paused.wait, 10
            ), "the parent write never entered its hold"
            state._slots.pop("parent")
            state.conversation_log.delete_session(slot_history_key(chats.parent))
            stranger = state.get_or_create_slot("parent")
            stranger.created_at = "2026-10-01T12:00:01+00:00"
            _seed(stranger, ("user", "an unrelated chat"), ("assistant", "hello"))
            state.flush_slot_now(stranger)
            resume.set()
            status, body = await merging

        assert (status, body["code"]) == (409, "parent_deleted"), body
        # Nothing was shown under the key either: the deleted chat is not flushed.
        assert _cards(chats.parent) == []
        key = slot_history_key(stranger)
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")
        assert _cards(stranger) == []
        on_disk = state.conversation_log.read_messages_chained_full(key)
        assert not any(MERGED_FROM_META_KEY in (row.get("meta") or {}) for row in on_disk)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unverifiable", ["stat failure", "unreadable metadata"])
    async def test_a_parent_whose_transcript_cannot_be_read_after_the_write_is_merged(
        self, chats, model, monkeypatch, audit, unverifiable
    ):
        # The card is in the parent's durable hold when the parent's transcript
        # becomes unreadable. Nothing shows the parent is gone, and the card will
        # reach its agent, so the merge is acknowledged rather than refused as
        # ``parent_deleted``.
        state, log = chats.state, chats.state.conversation_log
        parent_key = slot_history_key(chats.parent)
        broken = threading.Event()
        write = chat_handlers.persist_deferred_notes_sync
        real_path, real_status = log._path, log.get_metadata_status

        class _UnstatablePath(type(real_path(parent_key))):
            def stat(self, *args, **kwargs):
                raise PermissionError("stat refused")

        def path_of(key):
            path = real_path(key)
            if broken.is_set() and key == parent_key and unverifiable == "stat failure":
                return _UnstatablePath(path)
            return path

        def status_of(key):
            if broken.is_set() and key == parent_key and unverifiable == "unreadable metadata":
                return {}, False
            return real_status(key)

        def break_the_read_after_the_write(*args, **kwargs):
            outcome = write(*args, **kwargs)
            broken.set()
            return outcome

        async with _client(state) as client:
            _, draft = await _draft(client)
            audit.clear()
            monkeypatch.setattr(log, "_path", path_of)
            monkeypatch.setattr(log, "get_metadata_status", status_of)
            monkeypatch.setattr(
                chat_handlers, "persist_deferred_notes_sync", break_the_read_after_the_write
            )
            status, body = await _merge(client, "Redis works.", draft)
            broken.clear()

        assert status == 200, body
        assert (body["ok"], body["parent"]) == (True, "parent")
        held = log.get_metadata(parent_key).get("deferred_notes") or []
        assert [note["content"] for note in held] == ["Redis works."]
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]
        assert [(event["operation"], event["outcome"]) for event in audit] == [
            (chat_merge_back._AUDIT_MERGE, "allowed")
        ]

    @pytest.mark.asyncio
    async def test_the_next_merge_covers_only_what_came_after(self, chats, model):
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            await _merge(client, "first", draft)
            status, body = await _draft(client)
            assert (status, body["code"]) == (409, "nothing_to_merge")

            _seed(chats.fork, ("user", "now add eviction"), ("assistant", "LRU, 512MB"))
            status, second = await _draft(client)

        assert status == 200, second
        assert second["messages"] == 2
        branch = model.calls[-1]["prompt"].split("FORK MESSAGES")[1]
        assert "now add eviction" in branch
        assert "try redis" not in branch

    @pytest.mark.asyncio
    async def test_the_cursor_survives_a_restart_through_the_parents_transcript(self, chats, model):
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            await _merge(client, "first", draft)
        chats.state.flush_slot_now(chats.parent)
        # A restart rebuilds the parent's window from disk: drop the in-memory rows.
        chats.parent.messages.clear()

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (409, "nothing_to_merge")

    @pytest.mark.asyncio
    async def test_a_parent_mid_turn_holds_the_card_until_the_turn_ends(self, chats, model):
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            chats.parent.task = asyncio.get_running_loop().create_future()
            status, body = await _merge(client, "held summary", draft)
            assert status == 200, body
            assert body["deferred"] is True
            assert _cards(chats.parent) == []
            # A second merge of the same messages is refused while the first is held.
            status, again = await _merge(client, "again", draft)

        assert (status, again["code"]) == (409, "already_merged")
        chats.parent.task = None
        chats.parent.flush_deferred_notes()
        (card,) = _cards(chats.parent)
        assert card["meta"][MERGED_FROM_META_KEY]["through"] == draft["through"]

    @pytest.mark.asyncio
    async def test_the_same_merge_twice_is_refused(self, chats, model):
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            await _merge(client, "first", draft)
            status, body = await _merge(client, "first again", draft)

        assert (status, body["code"]) == (409, "already_merged")
        assert len(_cards(chats.parent)) == 1

    @pytest.mark.asyncio
    async def test_a_card_replayed_by_a_downgrade_counts_as_one_card(self, chats, model):
        # An older build ignores the hold entry's ``delivered`` flag and writes
        # the card's row a second time. The two rows share one ``noteId`` and one
        # ``mergedFrom``, so the covered range is counted once: the same messages
        # stay merged, a new draft covers only what came after them, and the
        # next merge is written beside both rows.
        async with _client(chats.state) as client:
            _, first = await _draft(client)
            status, body = await _merge(client, "Redis works.", first)
            assert status == 200, body
            (card,) = _cards(chats.parent)
            chats.parent.messages.append(
                {**card, "meta": {**card["meta"], "mid": f"{card['meta']['mid']}-replay"}}
            )
            chats.state.flush_slot_now(chats.parent)
            assert len(_cards(chats.parent)) == 2

            status, again = await _merge(client, "Redis works, again.", first)
            assert (status, again["code"]) == (409, "already_merged")
            _seed(chats.fork, ("user", "now add eviction"), ("assistant", "LRU, 512MB"))
            status, draft = await _draft(client)
            assert status == 200, draft
            assert (draft["messages"], draft["remaining"]) == (2, 0)
            status, body = await _merge(client, "LRU eviction added.", draft)

        assert status == 200, body
        assert body["messages"] == 2
        cards = _cards(chats.parent)
        assert [c["meta"]["noteId"] for c in cards[:2]] == [card["meta"]["noteId"]] * 2
        assert cards[2]["meta"][MERGED_FROM_META_KEY]["after"] == first["through"]

    @pytest.mark.asyncio
    async def test_a_reply_switched_to_another_variant_makes_the_draft_stale(self, chats, model):
        reply = chats.fork.messages[-1]
        reply["variants"] = [
            {"content": "redis failed: no TTL support", "ts": "t1"},
            {"content": reply["content"], "ts": reply.get("ts", "")},
        ]
        reply["variant_idx"] = 1

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            # The switch keeps the reply's message id and changes its text.
            resp = await client.post("/api/chat/slots/fork/switch-variant", json={"index": 0})
            assert resp.status == 200, await resp.text()
            status, body = await _merge(client, "Redis works. TTL 300s.", draft)

        assert (status, body["code"]) == (409, "merge_draft_stale")
        assert _cards(chats.parent) == []

    @pytest.mark.asyncio
    async def test_a_merged_reply_switched_to_another_variant_can_be_merged_again(
        self, chats, model
    ):
        reply = chats.fork.messages[-1]
        reply["variants"] = [
            {"content": "redis failed: no TTL support", "ts": "t1"},
            {"content": reply["content"], "ts": reply.get("ts", "")},
        ]
        reply["variant_idx"] = 1

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, _ = await _merge(client, "Redis works.", draft)
            assert status == 200
            resp = await client.post("/api/chat/slots/fork/switch-variant", json={"index": 0})
            assert resp.status == 200, await resp.text()
            status, again = await _draft(client)
            assert status == 200, again
            status, body = await _merge(client, "Redis failed after all.", again)
            assert status == 200, body
            status, after = await _draft(client)

        assert again["messages"] == 2
        assert "redis failed: no TTL support" in model.calls[-1]["prompt"].split("FORK MESSAGES")[1]
        assert [c["content"] for c in _cards(chats.parent)] == [
            "Redis works.",
            "Redis failed after all.",
        ]
        # The second card covers the range as it reads now.
        assert (status, after["code"]) == (409, "nothing_to_merge")

    @pytest.mark.asyncio
    async def test_a_draft_whose_start_another_merge_covered_is_stale(self, chats, model):
        async with _client(chats.state) as client:
            _, first = await _draft(client)
            _seed(chats.fork, ("user", "now add eviction"), ("assistant", "LRU, 512MB"))
            _, both = await _draft(client)
            status, _ = await _merge(client, "Redis works.", first)
            assert status == 200
            status, body = await _merge(client, "Redis works, with LRU eviction.", both)

        assert (status, body["code"]) == (409, "merge_draft_stale")
        assert len(_cards(chats.parent)) == 1

    @pytest.mark.asyncio
    async def test_the_last_check_reads_merges_the_transcript_read_missed(self, chats, model):
        async with _client(chats.state) as client:
            _, first = await _draft(client)
            _seed(chats.fork, ("user", "now add eviction"), ("assistant", "LRU, 512MB"))
            _, both = await _draft(client)
            # A card only the parent's window holds, with no message id, so the
            # transcript read cannot match it to a disk row.
            block = {
                "session": effective_session_key(chats.fork),
                "slot": "fork",
                "title": "",
                "createdAt": chats.fork._disk_meta_created_at,
                "after": chats.fork.messages[1]["meta"]["mid"],
                "through": first["through"],
                "digest": first["digest"],
                "messages": 2,
            }
            card = {
                "role": "inject",
                "content": "Redis works.",
                "meta": {"noteId": "note-other-tab", MERGED_FROM_META_KEY: block},
            }
            chats.parent.messages.append(card)
            status, body = await _merge(client, "Redis works, with LRU eviction.", both)

        assert (status, body["code"]) == (409, "merge_draft_stale")
        assert _cards(chats.parent) == [card]

    @pytest.mark.asyncio
    async def test_a_through_the_fork_no_longer_has_is_refused(self, chats, model):
        async with _client(chats.state) as client:
            status, body = await _merge(client, "summary", _ends_at("no-such-message"))

        assert (status, body["code"]) == (409, "merge_point_missing")

    @pytest.mark.asyncio
    async def test_a_through_inside_the_copied_prefix_is_already_merged(self, chats, model):
        copied = chats.fork.messages[0]["meta"]["mid"]

        async with _client(chats.state) as client:
            status, body = await _merge(client, "summary", _ends_at(copied))

        assert (status, body["code"]) == (409, "already_merged")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "code", "status"),
        [
            ({"summary": "  ", "through": "m"}, "invalid_summary", 400),
            ({"summary": 7, "through": "m"}, "invalid_summary", 400),
            (
                {"summary": "x" * (MAX_DEFERRED_NOTE_CHARS + 1), "through": "m"},
                "summary_too_long",
                413,
            ),
            ({"summary": "ok", "through": ""}, "invalid_through", 400),
            (
                {"summary": "ok", "through": "m" * (MERGED_FROM_MAX_KEY_CHARS + 1)},
                "invalid_through",
                400,
            ),
            ({"summary": "ok", "through": "m"}, "invalid_digest", 400),
            ({"summary": "ok", "through": "m", "digest": "abc"}, "invalid_digest", 400),
            ({"summary": "ok", "through": "m", "digest": "A" * 64}, "invalid_digest", 400),
        ],
    )
    async def test_a_malformed_body_is_refused(self, chats, model, payload, code, status):
        async with _client(chats.state) as client:
            resp = await client.post("/api/chat/slots/fork/merge-back", json=payload)
            body = await resp.json()

        assert (resp.status, body["code"]) == (status, code)

    @pytest.mark.asyncio
    async def test_a_parent_that_moved_on_since_the_draft_still_takes_the_merge(self, chats, model):
        # The summary never states how far the parent has moved, so nothing in
        # it can go stale when the parent carries on while the person reads it.
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            _seed(chats.parent, ("user", "went with memcached"), ("assistant", "ok"))

            status, body = await _merge(client, "Redis works.", draft)

        assert status == 200, body
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]

    @pytest.mark.asyncio
    async def test_a_full_merge_queue_is_refused_rather_than_written_unread(self, chats, model):
        for index in range(10):
            chats.parent.append_pending_context(
                {
                    "content": f"m{index}",
                    "source": MERGE_NOTE_SOURCE,
                    "ephemeral": True,
                    "injectedAt": 1e12,
                }
            )

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "summary", draft)

        assert (status, body["code"]) == (429, "merge_queue_full")
        assert _cards(chats.parent) == []

    @pytest.mark.asyncio
    async def test_a_full_context_queue_refuses_the_merge_without_a_card(self, chats, model):
        # Fifty entries from fifty sources: no per-source cap is reached, so the
        # refusal comes from the whole queue having no seat.
        for index in range(_MAX_PENDING_CONTEXT):
            assert (
                chats.parent.append_pending_context(
                    {
                        "content": f"ordinary {index}",
                        "source": f"other {index}",
                        "ephemeral": True,
                        "injectedAt": 1e12,
                    }
                )
                is True
            )

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "summary", draft)

        assert (status, body["code"]) == (429, "merge_queue_full")
        assert _cards(chats.parent) == []

    @pytest.mark.asyncio
    async def test_context_filling_during_cursor_work_refuses_without_a_card(
        self, chats, model, monkeypatch
    ):
        entered, release = threading.Event(), threading.Event()
        merge_cursor = chat_merge_back._merge_cursor

        def pause_cursor(*args):
            entered.set()
            assert release.wait(10), "the queue-fill cursor was not released"
            return merge_cursor(*args)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(chat_merge_back, "_merge_cursor", pause_cursor)
            merging = asyncio.create_task(_merge(client, "summary", draft))
            assert await asyncio.to_thread(entered.wait, 10)
            for index in range(10):
                chats.parent.append_pending_context(
                    {
                        "content": f"m{index}",
                        "source": MERGE_NOTE_SOURCE,
                        "ephemeral": True,
                        "injectedAt": 1e12,
                    }
                )
            release.set()
            status, body = await merging

        assert (status, body["code"]) == (429, "merge_queue_full")
        assert _cards(chats.parent) == []

    @pytest.mark.asyncio
    async def test_cards_changing_during_cursor_work_make_the_draft_stale(
        self, chats, model, monkeypatch
    ):
        entered, release = threading.Event(), threading.Event()
        merge_cursor = chat_merge_back._merge_cursor

        def pause_cursor(*args):
            entered.set()
            assert release.wait(10), "the concurrent-card cursor was not released"
            return merge_cursor(*args)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(chat_merge_back, "_merge_cursor", pause_cursor)
            merging = asyncio.create_task(_merge(client, "summary", draft))
            assert await asyncio.to_thread(entered.wait, 10)
            chats.parent.messages.append(
                _card(
                    chats.fork.messages[1]["meta"]["mid"],
                    draft["through"],
                    chats.fork.messages[2:],
                    "concurrent-card",
                )
            )
            release.set()
            status, body = await merging

        assert (status, body["code"]) == (409, "merge_draft_stale")
        assert len(_cards(chats.parent)) == 1

    @pytest.mark.asyncio
    async def test_a_twin_starting_during_cursor_work_receives_the_card(
        self, chats, model, monkeypatch
    ):
        twin = chats.state.get_or_create_slot("twin")
        twin.linked_session_key = effective_session_key(chats.parent)
        twin._disk_meta_created_at = chats.parent._disk_meta_created_at
        entered, release = threading.Event(), threading.Event()
        merge_cursor = chat_merge_back._merge_cursor

        def pause_cursor(*args):
            entered.set()
            assert release.wait(10), "the twin-start cursor was not released"
            return merge_cursor(*args)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(chat_merge_back, "_merge_cursor", pause_cursor)
            merging = asyncio.create_task(_merge(client, "summary", draft))
            assert await asyncio.to_thread(entered.wait, 10)
            twin.task = asyncio.get_running_loop().create_future()
            release.set()
            status, body = await merging

        assert (status, body["parent"], body["deferred"]) == (200, "twin", True)
        assert len(twin._deferred_notes) == 1
        twin.task.cancel()

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_merge(self, chats, model):
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state, request_app="some-app") as client:
            status, body = await _merge(client, "summary", _ends_at(through))

        assert (status, body["code"]) == (404, "slot_not_found")
        assert _cards(chats.parent) == []


class TestEndToEndWithTheForkRoute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("forked_from", [7, "s" * (MAX_FORK_PARENT_KEY_CHARS + 1)])
    async def test_a_restored_invalid_fork_parent_is_not_a_fork(self, chats, model, forked_from):
        chats.state.flush_slot_now(chats.fork)
        fork_history_key = slot_history_key(chats.fork)
        chats.state.conversation_log.update_metadata(
            fork_history_key,
            {
                "forked_from": forked_from,
                "forked_from_created_at": chats.parent._disk_meta_created_at,
            },
        )
        chats.state._slots.pop(chats.fork.key)
        restored = _rehydrate_slot_from_history(chats.state, chats.fork.key)
        assert restored is not None
        assert restored.forked_from is None
        assert restored.forked_from_created_at == ""
        through = restored.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft_body = await _draft(client)
            merge_status, merge_body = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft_body["code"]) == (409, "not_a_fork")
        assert (merge_status, merge_body["code"]) == (409, "not_a_fork")
        assert model.calls == []

    @pytest.mark.asyncio
    async def test_a_legacy_fork_still_drafts_and_merges_into_its_original_parent(
        self, chats, model
    ):
        chats.fork.forked_from_created_at = ""

        async with _client(chats.state) as client:
            draft_status, draft = await _draft(client)
            merge_status, body = await _merge(client, "Redis works.", draft)

        assert draft_status == 200, draft
        assert merge_status == 200, body
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("candidate_created_at", ["", "not-a-time"])
    async def test_a_legacy_fork_refuses_a_candidate_without_orderable_identity_as_unconfirmed(
        self, chats, model, candidate_created_at
    ):
        chats.fork.forked_from_created_at = ""
        chats.parent._disk_meta_created_at = candidate_created_at
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft_body = await _draft(client)
            merge_status, merge_body = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft_body["code"]) == (409, "parent_unconfirmed")
        assert (merge_status, merge_body["code"]) == (409, "parent_unconfirmed")
        assert model.calls == []
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_legacy_fork_of_a_parent_with_a_naive_created_at_is_unconfirmed_not_deleted(
        self, chats, model
    ):
        # Older builds stamped the metadata line with a naive local time, so the
        # parent's transcript is real and unchanged, only unorderable.
        naive = "2026-09-30T09:00:00"
        chats.state.conversation_log.update_metadata(
            slot_history_key(chats.parent), {"created_at": naive}
        )
        chats.parent._disk_meta_created_at = naive
        chats.fork.forked_from_created_at = ""
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft_body = await _draft(client)
            merge_status, merge_body = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft_body["code"]) == (409, "parent_unconfirmed")
        assert (merge_status, merge_body["code"]) == (409, "parent_unconfirmed")
        assert "cannot be confirmed" in draft_body["error"]
        assert model.calls == []
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []
        on_disk = chats.state.conversation_log.read_messages_chained_full(
            slot_history_key(chats.parent)
        )
        assert not any(MERGED_FROM_META_KEY in (row.get("meta") or {}) for row in on_disk)

    @pytest.mark.asyncio
    async def test_a_recorded_identity_against_a_parent_without_created_at_is_unconfirmed(
        self, chats, model
    ):
        chats.fork.forked_from_created_at = chats.parent._disk_meta_created_at
        assert chats.fork.forked_from_created_at
        chats.parent._disk_meta_created_at = ""
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft_body = await _draft(client)
            merge_status, merge_body = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft_body["code"]) == (409, "parent_unconfirmed")
        assert (merge_status, merge_body["code"]) == (409, "parent_unconfirmed")
        assert model.calls == []
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_legacy_fork_refuses_a_newer_candidate_as_deleted(self, chats, model):
        chats.fork.forked_from_created_at = ""
        chats.fork._disk_meta_created_at = "2026-10-01T12:00:00+00:00"
        chats.parent._disk_meta_created_at = "2026-10-01T13:00:00+00:00"
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state) as client:
            draft_status, draft_body = await _draft(client)
            merge_status, merge_body = await _merge(client, "Redis works.", _ends_at(through))

        assert (draft_status, draft_body["code"]) == (409, "parent_deleted")
        assert (merge_status, merge_body["code"]) == (409, "parent_deleted")
        assert model.calls == []
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_chat_made_on_a_deleted_parents_key_gets_nothing(
        self, tmp_path, monkeypatch, model
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        parent = state.get_or_create_slot("parent")
        parent.created_at = "2026-10-01T12:00:00+00:00"
        parent.title = "Parent"
        parent._titled = True
        _seed(parent, ("user", "pick a cache"), ("assistant", "memcached or redis?"))
        state.flush_slot_now(parent)
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/parent/fork", json={})
            forked = await resp.json()
            assert resp.status == 200, forked
            child = state._slots[forked["key"]]
            child.created_at = "2026-10-01T12:00:00+00:00"
            state.conversation_log.update_metadata(
                slot_history_key(child), {"created_at": child.created_at}
            )
            child._disk_meta_created_at = child.created_at
            child.forked_from_created_at = ""
            _seed(child, ("user", "try redis"), ("assistant", "redis works"))
            _, draft = await _draft(client, slot=child.key)
            # A permanent delete, then a new chat on the same key.
            state._slots.pop("parent")
            state.conversation_log.delete_session(slot_history_key(parent))
            stranger = state.get_or_create_slot("parent")
            stranger.created_at = "2026-10-01T12:00:01+00:00"
            _seed(stranger, ("user", "an unrelated chat"), ("assistant", "hello"))
            state.flush_slot_now(stranger)
            assert stranger._disk_meta_created_at not in ("", child.forked_from_created_at)
            calls_before_refusals = len(model.calls)
            draft_status, draft_body = await _draft(client, slot=child.key)
            status, body = await _merge(client, "Redis works.", draft, slot=child.key)

        assert (draft_status, draft_body["code"]) == (409, "parent_deleted")
        assert (status, body["code"]) == (409, "parent_deleted")
        assert len(model.calls) == calls_before_refusals
        assert _cards(stranger) == []
        assert stranger._deferred_notes == []
        on_disk = state.conversation_log.read_messages_chained_full(slot_history_key(stranger))
        assert not any(MERGED_FROM_META_KEY in (row.get("meta") or {}) for row in on_disk)

    @pytest.mark.asyncio
    async def test_a_fork_of_a_restored_pre_id_parent_merges_only_its_own_messages(
        self, chats, model
    ):
        # A transcript written before rows carried ids: no line on disk has one,
        # while the parent's window rows hold ids a restore minted.
        _drop_ids_on_disk(chats.state, chats.parent)

        async with _client(chats.state) as client:
            resp = await client.post("/api/chat/slots/parent/fork", json={})
            forked = await resp.json()
            assert resp.status == 200, forked
            child = chats.state._slots[forked["key"]]
            _seed(child, ("user", "a different idea"), ("assistant", "it also works"))
            status, draft = await _draft(client, slot=child.key)

        assert status == 200, draft
        assert draft["messages"] == 2
        branch = model.calls[-1]["prompt"].split("FORK MESSAGES")[1]
        assert "a different idea" in branch
        assert "pick a cache" not in branch

    @pytest.mark.asyncio
    async def test_a_fork_made_by_the_fork_route_merges_back(self, chats, model):
        async with _client(chats.state) as client:
            resp = await client.post("/api/chat/slots/parent/fork", json={})
            forked = await resp.json()
            assert resp.status == 200, forked
            child = chats.state._slots[forked["key"]]
            _seed(child, ("user", "a different idea"), ("assistant", "it also works"))

            status, draft = await _draft(client, slot=child.key)
            assert status == 200, draft
            status, body = await _merge(client, "different idea works", draft, slot=child.key)

        assert status == 200, body
        assert draft["messages"] == 2
        (card,) = [c for c in _cards(chats.parent) if c["content"] == "different idea works"]
        assert card["meta"][MERGED_FROM_META_KEY]["slot"] == child.key


class TestMergeBackDashboardAuthorization:
    @staticmethod
    def _real_auth_app(state, internal_secret: str) -> web.Application:
        from kiro_crew.dashboard.token_auth import token_auth_middleware

        app = web.Application(
            middlewares=[
                token_auth_middleware(
                    mixed_internal_paths=frozenset({"/api/chat"}),
                    internal_secret=internal_secret,
                )
            ]
        )
        app["state"] = state
        app.router.add_post(
            "/api/chat/slots/{slot}/merge-back/draft", api_chat_slot_merge_back_draft
        )
        app.router.add_post("/api/chat/slots/{slot}/merge-back", api_chat_slot_merge_back)
        return app

    @pytest.mark.asyncio
    async def test_claimless_internal_secret_cannot_draft_or_merge(self, chats, model, audit):
        secret = "test-internal-secret"
        app = self._real_auth_app(chats.state, secret)
        through = chats.fork.messages[-1]["meta"]["mid"]
        headers = {"X-Internal-Secret": secret}

        async with TestClient(TestServer(app)) as client:
            draft_response = await client.post(
                "/api/chat/slots/fork/merge-back/draft", json={}, headers=headers
            )
            merge_response = await client.post(
                "/api/chat/slots/fork/merge-back",
                json={
                    "summary": "attacker context",
                    "through": through,
                    "digest": "0" * 64,
                },
                headers=headers,
            )
            draft_body = await draft_response.json()
            merge_body = await merge_response.json()

        assert (draft_response.status, draft_body["code"]) == (
            404,
            "slot_not_found",
        )
        assert (merge_response.status, merge_body["code"]) == (
            404,
            "slot_not_found",
        )
        assert model.calls == []
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == []
        assert chats.parent._pending_context == []
        assert [call["caller"] for call in audit] == ["internal", "internal"]

    @pytest.mark.asyncio
    async def test_real_dashboard_token_still_drafts_and_merges(self, chats, model):
        from kiro_crew.dashboard.token_auth import generate_token

        app = self._real_auth_app(chats.state, "test-internal-secret")
        token = generate_token("local-app", ttl_seconds=300)
        params = {"token": token}

        async with TestClient(TestServer(app)) as client:
            draft_response = await client.post(
                "/api/chat/slots/fork/merge-back/draft",
                params=params,
                json={},
            )
            draft = await draft_response.json()
            assert draft_response.status == 200, draft
            merge_response = await client.post(
                "/api/chat/slots/fork/merge-back",
                json={
                    "summary": "Redis works.",
                    "through": draft["through"],
                    "digest": draft["digest"],
                },
                params=params,
            )
            body = await merge_response.json()

        assert merge_response.status == 200, body
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]
