"""Merging a fork back into its parent.

The planner decides which fork messages the parent does not have yet, the
draft route asks the background model to summarize them, and the merge route
writes the person's text into the parent as a note whose row carries a
``mergedFrom`` block. These tests drive the routes against a real conversation
log, with only the model call replaced. The merge route's own cases are in
``test_chat_merge_back_merge.py`` and ``test_chat_merge_back_merge_hold.py``,
the sidebar, fork-route and authorization cases in
``test_chat_merge_back_routes.py``; the shared fixtures and helpers are in
``chat_merge_back_helpers.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from chat_merge_back_helpers import (  # noqa: F401
    _CHANNEL_STEM,
    _assert_privacy_denial,
    _card,
    _cards,
    _change_at_source_check,
    _channel_parent_and_fork,
    _client,
    _draft,
    _ends_at,
    _fork_of,
    _merge,
    _move_older_fork_rows_to_rotated_archive,
    _move_parent_rows_to_rotated_archive,
    _place_merge_card,
    _restrict_parent_chain,
    _row,
    _seed,
    audit_fixture,
    chats_fixture,
    model_fixture,
)
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_merge_back, slot_buffers
from kiro_crew.dashboard.chat_merge_back import (
    MergePlan,
    covered_digest,
    merge_cards,
    message_key,
    plan_merge,
)
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.slot_buffers import (
    MAX_DEFERRED_NOTE_CHARS,
    MAX_SLOT_KEY_CHARS,
    MERGED_FROM_MAX_KEY_CHARS,
    MERGED_FROM_META_KEY,
    sanitize_merged_from,
    sanitize_restored_deferred_notes,
    serialize_deferred_notes,
)
from kiro_crew.dashboard.state import _normalize_slot_key
from kiro_crew.history import TranscriptBusy, TranscriptWithheld
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.security import redact as baseline_redact
from kiro_crew.testing.wait import await_until


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
            "after": "",
            "through": "m",
            "digest": "ab" * 32,
            "messages": 3,
        }

    def test_a_valid_block_comes_back_with_exactly_its_fields(self):
        raw = {**self._valid(), "extra": "dropped"}

        assert sanitize_merged_from(raw) == self._valid()

    @pytest.mark.parametrize(
        "missing",
        ["session", "slot", "title", "after", "through", "digest", "messages"],
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

    def test_the_session_key_takes_the_note_holds_session_bound(self, monkeypatch):
        monkeypatch.setattr(slot_buffers, "MAX_DEFERRED_NOTE_SESSION_CHARS", 8)

        assert sanitize_merged_from({**self._valid(), "session": "s" * 8}) is not None
        assert sanitize_merged_from({**self._valid(), "session": "s" * 9}) is None

    def test_the_slot_key_takes_the_bound_every_slot_key_is_folded_to(self):
        # The slot is ``fork.key``, which ``_normalize_slot_key`` folded, so the
        # block's bound is the fold's own: a key at it survives the fold verbatim
        # and the block accepts it, and one past it is shortened by the fold and
        # refused by the block. Both read one constant, so the bounds cannot drift,
        # and the constant is the filename limit the fold exists for.
        at_bound, past_bound = "s" * MAX_SLOT_KEY_CHARS, "s" * (MAX_SLOT_KEY_CHARS + 1)
        assert len(f"{at_bound}.jsonl") <= 255 < len(f"{past_bound}.jsonl")
        assert _normalize_slot_key(at_bound) == at_bound
        assert len(_normalize_slot_key(past_bound)) <= MAX_SLOT_KEY_CHARS

        assert sanitize_merged_from({**self._valid(), "slot": at_bound}) is not None
        assert sanitize_merged_from({**self._valid(), "slot": past_bound}) is None


class TestHeldMergeNote:
    def test_the_block_survives_a_restart_and_is_stamped_on_the_delivered_row(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("parent")
        block = {
            "session": "dashboard:f",
            "slot": "f",
            "title": "t",
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
    async def test_a_two_way_mirrored_dashboard_parent_is_refused_before_reads(
        self, chats, model, monkeypatch
    ):
        key = chats.fork.forked_from
        assert key
        chats.state.sessions.set_mirror_link(
            key,
            ChannelLink(channel_type="discord", channel_id="mirror-channel"),
            accepts_inbound=True,
        )
        parent_messages = list(chats.parent.messages)
        fork_messages = list(chats.fork.messages)
        through = chats.fork.messages[-1]["meta"]["mid"]

        def parent_lookup_must_not_run(*_args):
            raise AssertionError("the two-way mirror must be refused before parent lookup")

        monkeypatch.setattr(chat_merge_back, "_live_parent", parent_lookup_must_not_run)
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
    async def test_a_two_way_mirror_bound_after_draft_is_refused_at_merge(
        self, chats, model, monkeypatch
    ):
        parent_messages = list(chats.parent.messages)
        fork_messages = list(chats.fork.messages)
        live_parent = chat_merge_back._live_parent

        async with _client(chats.state) as client:
            draft_status, draft = await _draft(client)

            def bind_during_parent_lookup(state, fork):
                parent = live_parent(state, fork)
                state.sessions.set_mirror_link(
                    fork.forked_from,
                    ChannelLink(channel_type="discord", channel_id="mirror-channel"),
                    accepts_inbound=True,
                )
                return parent

            async def transcript_read_must_not_start(*_args):
                raise AssertionError("the new two-way mirror must be refused before reads")

            monkeypatch.setattr(chat_merge_back, "_live_parent", bind_during_parent_lookup)
            monkeypatch.setattr(chat_merge_back, "_read_inputs", transcript_read_must_not_start)
            merge_status, merged = await _merge(client, "Redis works.", draft)

        assert draft_status == 200, draft
        assert (merge_status, merged["code"]) == (409, "parent_not_dashboard")
        assert chats.parent.messages == parent_messages
        assert chats.fork.messages == fork_messages
        assert chats.parent._deferred_notes == []
        assert chats.fork._deferred_notes == []
        assert len(model.calls) == 1

    @pytest.mark.asyncio
    async def test_an_outbound_only_mirrored_dashboard_parent_is_admitted(self, chats, model):
        key = chats.fork.forked_from
        assert key
        chats.state.sessions.set_mirror_link(
            key,
            ChannelLink(channel_type="discord", channel_id="mirror-channel"),
            accepts_inbound=False,
        )

        async with _client(chats.state) as client:
            draft_status, draft = await _draft(client)

        assert draft_status == 200, draft
        assert draft["ok"] is True
        assert chats.state.sessions.mirror_accepts_inbound(key) is False
        assert len(model.calls) == 1

    @pytest.mark.asyncio
    async def test_a_two_way_mirror_bound_during_merge_is_refused_before_delivery(
        self, chats, model, monkeypatch
    ):
        parent_messages = list(chats.parent.messages)
        fork_messages = list(chats.fork.messages)
        landed_cursor = chat_merge_back._landed_cursor

        async with _client(chats.state) as client:
            draft_status, draft = await _draft(client)

            def bind_after_cursor_snapshot(*args, **kwargs):
                result = landed_cursor(*args, **kwargs)
                chats.state.sessions.set_mirror_link(
                    chats.fork.forked_from,
                    ChannelLink(channel_type="discord", channel_id="mirror-channel"),
                    accepts_inbound=True,
                )
                return result

            monkeypatch.setattr(chat_merge_back, "_landed_cursor", bind_after_cursor_snapshot)
            merge_status, merged = await _merge(client, "Redis works.", draft)

        assert draft_status == 200, draft
        assert (merge_status, merged["code"]) == (409, "parent_not_dashboard")
        assert chats.parent.messages == parent_messages
        assert chats.fork.messages == fork_messages
        assert chats.parent._pending_context == []
        assert chats.parent._deferred_notes == []
        assert chats.fork._deferred_notes == []
        assert len(model.calls) == 1

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
            await await_until(release, what="the test's release")
            return "note"

        cfg = SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "m"))
        monkeypatch.setattr(chat_merge_back, "run_bg_oneliner", slow)
        monkeypatch.setattr(chat_merge_back, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))

        async with _client(chats.state) as client:
            first = asyncio.create_task(_draft(client))
            await await_until(started, what="the stub's start")
            second = await _draft(client)
            release.set()
            first_status, _ = await await_until(first, what="the first draft completion")

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
    async def test_a_credential_split_by_a_url_end_is_redacted_from_the_note(self, chats, model):
        model.replies[0] = f"https://collector.example/?pad={'a' * 40}&token=)opaque-value-xyz"

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert "opaque-value" not in body["summary"]

    @pytest.mark.asyncio
    async def test_the_active_credential_policy_redacts_the_draft_note(
        self, chats, model, monkeypatch
    ):
        companion_secret = "COMPANIONONLYTOKEN-do-not-leak"
        model.replies[0] = f"Redis works with companion_key={companion_secret}."

        def fake_context_redact(text: str) -> str:
            scrubbed = text.replace(companion_secret, "[REDACTED: credential]")
            return baseline_redact(scrubbed)

        monkeypatch.setattr(chat_merge_back, "redact_via_context", fake_context_redact)

        assert companion_secret in baseline_redact(model.replies[0])
        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert companion_secret not in body["summary"]
        assert "[REDACTED: credential]" in body["summary"]

    @pytest.mark.asyncio
    async def test_an_overlong_note_is_cut_to_the_merge_limit(self, chats, model):
        model.replies[0] = "word " * 2000

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert status == 200, body
        assert len(body["summary"]) <= MAX_DEFERRED_NOTE_CHARS
        assert body["summary"].endswith("…")
