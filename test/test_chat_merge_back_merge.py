"""Merging a fork back into its parent: the merge route, first half.

Continues in ``test_chat_merge_back_merge_hold.py``; the shared fixtures and
helpers are in ``chat_merge_back_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager

import pytest
from aiohttp import web
from chat_merge_back_helpers import (  # noqa: F401
    _CHANNEL_KEY,
    _CHANNEL_STEM,
    _assert_privacy_denial,
    _cards,
    _change_at_source_check,
    _channel_parent_and_fork,
    _client,
    _close_route,
    _draft,
    _drop_ids_on_disk,
    _merge,
    _move_older_fork_rows_to_rotated_archive,
    _move_parent_rows_to_rotated_archive,
    _place_merge_card,
    _queued_merge_context,
    _restrict_parent_chain,
    _resume_route,
    _run_turn,
    _seed,
    _slots_on_session,
    _workflow_result_for,
    audit_fixture,
    chats_fixture,
    model_fixture,
)
from chat_test_helpers import close_before_resume

from kiro_crew.dashboard import chat_handlers, chat_merge_back, chat_persistence, workflow_inject
from kiro_crew.dashboard.chat_merge_back import (
    MERGE_NOTE_SOURCE,
    covered_digest,
    merge_cards,
    message_key,
)
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history, _save_slot_to_history
from kiro_crew.dashboard.chat_utils import (
    adopt_variant_text,
    effective_session_key,
    slot_history_key,
    transcripts_share_file,
)
from kiro_crew.dashboard.slot_buffers import AWAITING_DURABLE_WRITE, MERGED_FROM_META_KEY
from kiro_crew.dashboard.state import _MAX_CONTEXT_PER_SOURCE
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout


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
            await async_wait_until(
                entered.is_set, describe=lambda: "the first merge write never entered its hold"
            )
            second = asyncio.create_task(_merge(client, "Redis works.", draft))
            await await_until(parked, what="the second merge parking")
            release.set()
            first_status, first_body = await await_until(first, what="the failed first merge")
            second_status, second_body = await await_until(second, what="the waiting second merge")

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
            await async_wait_until(
                paused.is_set, describe=lambda: "the private-fork check never entered its hold"
            )
            log.update_metadata(slot_history_key(chats.fork), {"memory_mode": "incognito"})
            chats.parent.task = None
            chats.parent.flush_deferred_notes()
            resume.set()
            status, body = await await_until(merging, what="the private-fork merge completion")

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
            await async_wait_until(
                paused.is_set, describe=lambda: "the save check never entered its hold"
            )
            chats.parent._dirty = True
            chats.state.flush_slot_now(chats.parent)
            log.update_metadata(slot_history_key(chats.fork), {"memory_mode": "incognito"})
            resume.set()
            status, body = await await_until(merging, what="the saved-parent merge completion")

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
            # The range starts after the last message the fork copied.
            "after": chats.fork.messages[1]["meta"]["mid"],
            "through": draft["through"],
            "digest": draft["digest"],
            "messages": 2,
        }
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
            await await_until(release_write, what="the write's release")
            return await durable_persist(*args, **kwargs)

        def fail_write(*_args, **_kwargs):
            raise OSError("card write failed")

        async with _client(chats.state) as client:
            _, original = await _draft(client)
            monkeypatch.setattr(chat_handlers, "_persist_deferred_note_hold", persist_after_release)
            monkeypatch.setattr(chat_handlers, "persist_deferred_notes_sync", fail_write)
            merging = asyncio.create_task(_merge(client, "first attempt", original))
            await await_until(write_started, what="the card write starting")
            [waiting] = chats.parent._deferred_notes
            assert waiting[AWAITING_DURABLE_WRITE] is True
            try:
                draft_status, retry = await _draft(client)
            finally:
                release_write.set()
            merge_status, failed = await await_until(
                merging, what="the failed card-write merge completion"
            )
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
            await await_until(release_write, what="the write's release")
            return await durable_persist(*args, **kwargs)

        async with _client(chats.state) as client:
            _, original = await _draft(client)
            monkeypatch.setattr(chat_handlers, "_persist_deferred_note_hold", persist_after_release)
            merging = asyncio.create_task(_merge(client, "first attempt", original))
            await await_until(write_started, what="the card write starting")
            [waiting] = chats.parent._deferred_notes
            assert waiting[AWAITING_DURABLE_WRITE] is True
            try:
                draft_status, concurrent = await _draft(client)
            finally:
                release_write.set()
            merge_status, merged = await await_until(
                merging, what="the successful card-write merge completion"
            )
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
            await async_wait_until(written.is_set, describe=lambda: "the card write never ran")
            closing = asyncio.create_task(_close_route(client))
            try:
                await async_wait_until(
                    lambda: "parent" not in state._slots, describe=lambda: sorted(state._slots)
                )
            finally:
                release.set()
            (close_status, closed), (merge_status, merged) = await await_until(
                asyncio.gather(closing, merging),
                what="the parent close and card-write completion",
            )
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
        assert stale_card["noteId"] not in state._consumed_merge_cards

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
    async def test_a_flush_refusal_keeps_the_merge_card_held_until_a_seat_frees(
        self, chats, model, monkeypatch, caplog
    ):
        state = chats.state
        parent = chats.parent
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

        assert (status, body["deferred"]) == (200, True), body
        assert refused_once is True
        assert _cards(parent) == []
        [held] = parent._deferred_notes
        assert held["context"]["content"] == "late summary"
        assert any("kept a merge card held" in record.message for record in caplog.records)

        assert parent.flush_deferred_notes() == 1
        [card] = _cards(parent)
        assert card["content"] == "late summary"
        queued = [e for e in parent._pending_context if e.get("noteId") == card["meta"]["noteId"]]
        assert [e["content"] for e in queued] == ["late summary"]
        assert parent._deferred_notes == []

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
        assert restored._pending_context == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("holder_saved_first", [False, True])
    async def test_a_twin_drain_stamps_disk_and_the_next_full_save_retires_the_card(
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
            state.flush_slot_now(chats.parent)

        prompts = await _run_turn(state, twin, "continue")
        assert prompts[0].count("twin summary") == 1
        note_id = _cards(chats.parent)[0]["meta"]["noteId"]
        assert state._consumed_merge_cards[note_id] == (key, True)
        held_entries = state.conversation_log._read_metadata(key).get("deferred_notes") or []
        if holder_saved_first:
            assert held_entries == []
        else:
            assert held_entries[0]["id"] == note_id
            assert held_entries[0]["consumed"] is True

        state.flush_slot_now(chats.parent)

        assert not state.conversation_log._read_metadata(key).get("deferred_notes")
        assert any(
            isinstance(row.get("meta"), dict) and row["meta"].get("noteId") == note_id
            for row in state.conversation_log.read_messages_chained_full(key)
        )

    @pytest.mark.asyncio
    async def test_a_stale_save_defers_consumed_card_retirement_until_it_writes_the_row(
        self, chats, monkeypatch
    ):
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        await _place_merge_card(state, chats.parent, chats.fork, "stale save summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]
        card_index = next(
            index
            for index, row in enumerate(chats.parent._messages)
            if isinstance(row.get("meta"), dict) and row["meta"].get("noteId") == note_id
        )
        card_row = chats.parent._messages.pop(card_index)
        assert [
            entry.get("id")
            for entry in state.conversation_log._read_metadata(key)["deferred_notes"]
        ] == [note_id]
        await chat_runner._record_consumed_merge_contexts(state, [context])

        lock_acquired = threading.Event()
        release_lock = threading.Event()
        snapshot_taken = threading.Event()

        def hold_transcript_lock():
            with state.conversation_log._locked(key):
                lock_acquired.set()
                assert release_lock.wait(
                    default_timeout()
                ), "test did not release the transcript lock"

        holder = threading.Thread(target=hold_transcript_lock)
        holder.start()
        await async_wait_until(
            lock_acquired.is_set, describe=lambda: "transcript lock was not acquired"
        )

        real_snapshot = chat_persistence._write_guards.paired_window_snapshot

        def record_snapshot(slot, *args, **kwargs):
            snapshot = real_snapshot(slot, *args, **kwargs)
            snapshot_taken.set()
            return snapshot

        monkeypatch.setattr(
            chat_persistence._write_guards, "paired_window_snapshot", record_snapshot
        )
        stale_save = asyncio.create_task(
            asyncio.to_thread(_save_slot_to_history, state, chats.parent, force=True)
        )
        try:
            await async_wait_until(
                snapshot_taken.is_set, describe=lambda: "save did not take its snapshot"
            )
            chats.parent._messages.insert(card_index, card_row)
        finally:
            release_lock.set()
        assert await await_until(stale_save, what="the stale save")
        holder.join(default_timeout())
        assert not holder.is_alive(), "transcript-lock holder did not exit"

        held = state.conversation_log._read_metadata(key).get("deferred_notes") or []
        assert [entry.get("id") for entry in held] == [note_id]
        assert held[0]["consumed"] is True
        assert state._consumed_merge_cards[note_id] == (key, True)

        assert await asyncio.to_thread(_save_slot_to_history, state, chats.parent, force=True)

        assert not state.conversation_log._read_metadata(key).get("deferred_notes")
        assert state._consumed_merge_cards[note_id] == (key, True)
        assert any(
            isinstance(row.get("meta"), dict) and row["meta"].get("noteId") == note_id
            for row in state.conversation_log.read_messages_chained_full(key)
        )

    @pytest.mark.asyncio
    async def test_a_clean_restart_does_not_queue_consumed_delivered_context(self, chats):
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        await _place_merge_card(state, chats.parent, chats.fork, "restart consumed summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]
        state.flush_slot_now(chats.parent)
        await chat_runner._record_consumed_merge_contexts(state, [context])
        assert state._consumed_merge_cards[note_id].stamped is True
        # A restart starts with an empty registry, so the durable stamp is the
        # only thing left to suppress the card's context.
        state._consumed_merge_cards.clear()
        state._slots.pop("parent")

        restored = _rehydrate_slot_from_history(state, "parent")

        assert restored is not None
        assert restored._pending_context == []

    def test_union_carries_consumed_and_delivered_from_disk(self):
        from kiro_crew.dashboard.slot_buffers import union_deferred_notes

        disk = [{"id": "card", "merged_from": {}, "consumed": True, "delivered": True}]
        live = [{"id": "card", "merged_from": {}}]

        [merged] = union_deferred_notes(disk, live)

        assert merged["consumed"] is True
        assert merged["delivered"] is True

    @pytest.mark.asyncio
    async def test_stamp_failure_logs_and_the_process_guard_stops_replay(
        self, chats, monkeypatch, caplog
    ):
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        await _place_merge_card(state, chats.parent, chats.fork, "failed stamp summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]

        async def fail_stamp(*_args):
            raise OSError("stamp failed")

        monkeypatch.setattr(chat_runner, "_stamp_consumed_merge_contexts", fail_stamp)
        with caplog.at_level(logging.WARNING):
            await chat_runner._record_consumed_merge_contexts(state, [context])
        twin = state.get_or_create_slot("failed-stamp-twin")
        twin._pending_context.append(dict(context))
        drained = chat_runner.take_turn_context(
            state, twin, transcript_identity=twin._disk_meta_created_at
        )

        assert state._consumed_merge_cards[note_id] == (context["noteTranscript"], False)
        assert "failed stamp summary" not in drained.prefix
        assert any("Could not stamp consumed merge cards" in row.message for row in caplog.records)

    @staticmethod
    def _full_line(slot, existing_meta, window):
        from kiro_crew.dashboard.slot_persistence import metadata_line

        meta_line, _mode, _queue, retired, _tab_id = metadata_line.build_full_line(
            slot,
            existing_meta,
            live_session="",
            closed=False,
            closed_at=None,
            window=window,
            queue_snapshot=[],
            queue_candidates=0,
            rewrite=False,
            rows_only=False,
        )
        return [entry.get("id") for entry in meta_line.get("deferred_notes") or []], retired

    @pytest.mark.parametrize(
        "delivered, row_in_window, retires",
        [(False, False, False), (False, True, True), (True, False, True)],
    )
    def test_a_full_save_retires_a_consumed_card_only_with_proof_of_its_row(
        self, chats, delivered, row_in_window, retires
    ):
        card = {
            "id": "card",
            "content": "summary",
            "merged_from": {"key": "fork"},
            "consumed": True,
        }
        if delivered:
            card["delivered"] = True
        window = [{"role": "user", "content": "summary", "meta": {"noteId": "card"}}]

        held, retired = self._full_line(
            chats.parent, {"deferred_notes": [card]}, window if row_in_window else []
        )

        assert held == ([] if retires else ["card"])
        assert retired == set()

    @pytest.mark.parametrize(
        "on_disk_hold,retires",
        [
            (7, False),
            (True, False),
            ("held", False),
            ([["card"], {"id": ["card"], "merged_from": {}, "delivered": True}], False),
            ([{"id": "card", "merged_from": {}, "delivered": True}], True),
        ],
    )
    def test_only_a_well_formed_on_disk_hold_supplies_the_delivered_stamp(
        self, chats, on_disk_hold, retires
    ):
        """A malformed hold supplies no ``delivered`` stamp, so it retires nothing.

        The live card is consumed, its row is not in this save's window and it
        is not ``delivered``, so the only thing that could retire it is a
        stamp read off the disk hold — and a malformed one supplies none. The
        last parameter is the positive control: a well-formed disk entry for
        the same id DOES fold its ``delivered`` onto the live card, which with
        the live ``consumed`` retires the hold. Without it every malformed
        parameter would pass for a reason unrelated to malformedness.
        """
        chats.parent._deferred_notes = [
            {
                "id": "card",
                "content": "summary",
                "merged_from": {"key": "fork"},
                "consumed": True,
            }
        ]

        held, retired = self._full_line(chats.parent, {"deferred_notes": on_disk_hold}, [])

        assert held == ([] if retires else ["card"])
        assert retired == set()

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
        [context] = chats.parent._pending_context
        note_id = context["noteId"]

        await _run_turn(state, chats.parent, "continue", outcome=outcome)

        assert [entry["content"] for entry in chats.parent._pending_context] == ["retry summary"]
        assert note_id not in state._consumed_merge_cards
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
            await await_until(asyncio.Future(), what="the cancel that ends this stub")

        stopped = await _run_turn(
            state, chats.parent, "continue", events=events, cancel_when=streamed
        )
        assert stopped[0].count("stopped summary") == 1
        assert [entry["noteId"] for entry in chats.parent._pending_context] == [note_id]
        assert note_id not in state._consumed_merge_cards

        resent = await _run_turn(state, chats.parent, "continue")
        assert resent[0].count("stopped summary") == 1
        assert chats.parent._pending_context == []
        later = await _run_turn(state, chats.parent, "and then")
        assert "stopped summary" not in later[0]

    @pytest.mark.asyncio
    async def test_empty_response_give_up_returns_the_card_for_the_next_turn(self, chats, model):
        """An unanswered prompt is not a delivery even when recovery gives up."""
        state = chats.state
        note_id = await self._merged_card(chats, "empty response summary")

        prompts = await _run_turn(
            state,
            chats.parent,
            "continue",
            events=self._kept_reply(),
            queued_events=self._kept_reply(),
        )

        assert len(prompts) == 3
        assert all(prompt.count("empty response summary") == 1 for prompt in prompts)
        # The queued verbatim replay and empty-response continuation are the
        # episode's second and third attempts.
        assert "[Empty response — automatic recovery]" not in prompts[1]
        assert "[Empty response — automatic recovery]" in prompts[2]

        assert [entry["noteId"] for entry in chats.parent._pending_context] == [note_id]
        assert chats.parent._inflight_merge_contexts == []
        assert note_id not in state._consumed_merge_cards
        hold = state.conversation_log._read_metadata(slot_history_key(chats.parent)).get(
            "deferred_notes"
        )
        assert [entry.get("id") for entry in hold] == [note_id]
        assert hold[0].get("consumed") is not True

        resent = await _run_turn(state, chats.parent, "continue")
        assert resent[0].count("empty response summary") == 1
        assert chats.parent._pending_context == []
        later = await _run_turn(state, chats.parent, "and then")
        assert "empty response summary" not in later[0]

    @pytest.mark.asyncio
    async def test_an_empty_continuation_after_productive_work_returns_the_card(self, chats, model):
        """A give-up reads only its own attempt, so the card comes back once."""
        from kiro_crew.providers.base import EVENT_TOOL_CALL, LLMEvent

        state = chats.state
        note_id = await self._merged_card(chats, "productive empty summary")

        prompts = await _run_turn(
            state,
            chats.parent,
            "continue",
            events=self._kept_reply(
                self._text("visible answer"),
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="read-0",
                ),
            ),
            queued_events=self._kept_reply(),
        )

        assert len(prompts) == 2
        assert prompts[0].count("productive empty summary") == 1
        assert prompts[1].count("productive empty summary") == 1
        assert "[Empty response — automatic recovery]" in prompts[1]
        assert [entry["noteId"] for entry in chats.parent._pending_context] == [note_id]
        assert note_id not in state._consumed_merge_cards
        following = await _run_turn(state, chats.parent, "and then")
        assert following[0].count("productive empty summary") == 1
        later = await _run_turn(state, chats.parent, "and later")
        assert "productive empty summary" not in later[0]

    @pytest.mark.asyncio
    async def test_a_productive_empty_give_up_consumes_the_card(self, chats, model):
        """Work in the attempt that gives up answers its prompt, so the card is spent."""
        from kiro_crew.providers.base import EVENT_TOOL_CALL, LLMEvent

        def productive_empty_reply(call_id):
            return self._kept_reply(
                self._text("visible answer"),
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id=call_id,
                ),
            )

        state = chats.state
        note_id = await self._merged_card(chats, "productive give-up summary")

        prompts = await _run_turn(
            state,
            chats.parent,
            "continue",
            events=productive_empty_reply("read-first"),
            queued_events=productive_empty_reply("read-second"),
        )

        assert len(prompts) == 2
        assert prompts[1].count("productive give-up summary") == 1
        assert chats.parent._pending_context == []
        assert state._consumed_merge_cards[note_id] == (
            slot_history_key(chats.parent),
            True,
        )
        following = await _run_turn(state, chats.parent, "and then")
        assert "productive give-up summary" not in following[0]

    @pytest.mark.asyncio
    async def test_stale_episode_productivity_does_not_consume_an_app_turn_card(self, chats, model):
        """A discarded recovery gives an unrelated synthetic turn no productivity."""
        from kiro_crew.providers.base import EVENT_TOOL_CALL, LLMEvent

        state = chats.state
        await _run_turn(
            state,
            chats.parent,
            "do some work",
            events=self._kept_reply(
                self._text("visible answer"),
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="read-stale",
                ),
            ),
        )
        assert chats.parent._empty_episode_productive is True
        assert chats.parent._queue
        # A hard Stop takes and clears the queued continuation before settling
        # it; clearing here reproduces that slot-state effect without stopping
        # the test's mocked provider session.
        chats.parent._queue.clear()

        note_id = await self._merged_card(chats, "app turn summary")
        prompts = await _run_turn(
            state,
            chats.parent,
            "[MCP app turn]",
            events=self._kept_reply(),
            synthetic_payload=True,
        )

        assert prompts[0].count("app turn summary") == 1
        assert [entry["noteId"] for entry in chats.parent._pending_context] == [note_id]
        assert chats.parent._inflight_merge_contexts == []
        assert note_id not in state._consumed_merge_cards
