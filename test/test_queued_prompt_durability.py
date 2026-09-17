"""Durability of the busy-slot queue: a queued user prompt survives a restart.

A prompt typed while a turn is running is accepted with ``{"queued": true}`` and
held in ``slot._queue``. Its transcript row is written by the DRAIN, not by the
enqueue, so before this the only copy lived in process memory: a gateway restart
(an auto-update, a watchdog exit) dropped it with no row, no error card, and an
empty queue card list after reload.

Pinned here, per layer:

- **Selection** — only a plain user prompt is durable. A cron notification, a
  subagent completion, a synthetic recovery continuation and any entry carrying
  a retry callback are process-bound and must NOT be replayed.
- **Persist** — the metadata line carries the queue, is cleared by absence once
  the drain consumes it, and is deferred (never clobbered) by a rows-only
  handover save.
- **Drift** — the periodic flush saves on queue drift as well as ``_dirty``, so
  durability does not depend on each queue mutation site marking the slot dirty.
- **Restore** — both restore paths hand the entries back as queue cards, and a
  tampered metadata value cannot smuggle a system entry (a ``kind``, a
  ``payload``, a callback key) into the queue.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
    restore_recent_sessions,
)
from kiro_crew.dashboard.slot_queue_repository import (
    EMPTY_QUEUE_SIGNATURE,
    MAX_DURABLE_QUEUE_BYTES,
    MAX_DURABLE_QUEUE_ENTRIES,
    MAX_DURABLE_QUEUE_SCAN,
    count_durable_candidates,
    durable_queue_entries,
    durable_queue_view,
    queue_persist_signature,
    sanitize_restored_queue,
    warn_if_not_durable,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import (
    ROWS_ONLY_DEFERRED_META_KEYS,
    SLOT_OWNED_META_KEYS,
    ConversationLog,
)

#: The logger the durable-queue warning is emitted on, so a caplog assertion
#: names the real emitter rather than the root logger.
_QUEUE_LOGGER = "kiro_crew.dashboard.slot_queue_repository"


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _busy_slot(state, name: str = "s1"):
    """A slot with a persisted user row, i.e. the shape a queue is held on."""
    slot = state.get_or_create_slot(name)
    slot.append("user", "the running turn")
    slot.drain()
    return slot


def _meta(state, name: str = "s1") -> dict:
    return state.conversation_log._read_metadata(f"dashboard:{name}")


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)


class TestDurableSelection:
    def test_plain_user_prompt_is_durable(self) -> None:
        entries = durable_queue_entries([{"id": "q1", "content": "hello", "kind": ""}])
        assert [e["content"] for e in entries] == ["hello"]

    @pytest.mark.parametrize(
        "entry",
        [
            # An injection whose producer is gone: a restart is not the event
            # happening again.
            {"id": "q1", "content": "[Cron notification]", "kind": "cron_notification"},
            {"id": "q1", "content": "[Subagent completion event]", "kind": "subagent_completion"},
            # A synthetic recovery continuation dispatches an action a dead turn
            # announced.
            {"id": "q1", "content": "continue", "kind": "", "payload": "resume"},
            # Retry callbacks do not survive the process, so replaying the text
            # would acknowledge nothing.
            {"id": "q1", "content": "retry", "kind": "", "_on_consumed": lambda ok: None},
            {
                "id": "q1",
                "content": "retry",
                "kind": "",
                "_on_irreversibly_consumed": lambda: None,
            },
            # Shape guards.
            {"id": "q1", "content": "", "kind": ""},
            {"id": "q1", "kind": ""},
            {"content": "no id", "kind": ""},
            "not a dict",
        ],
    )
    def test_non_user_entries_are_not_durable(self, entry) -> None:
        assert durable_queue_entries([entry]) == []

    def test_meta_rides_along_but_provenance_does_not(self) -> None:
        # ``meta`` carries the admission-time containment snapshot the drain
        # re-validates against, so it must survive. Provenance must NOT: see
        # TestRestoredEntriesCarryNoHumanAuthority -- a flag read back from a
        # writable file is authority granted to whoever edited it.
        entries = durable_queue_entries(
            [
                {
                    "id": "q1",
                    "content": "hi",
                    "kind": "",
                    "meta": {"queued_containment": {"linked": False}, "sendId": "s-1"},
                    "_directive_user_origin": True,
                }
            ]
        )
        assert entries[0]["meta"]["queued_containment"] == {"linked": False}
        assert entries[0]["meta"]["sendId"] == "s-1"
        assert "_directive_user_origin" not in entries[0]

    def test_unserializable_meta_keeps_the_prompt(self) -> None:
        # The prompt is the part that cannot be reconstructed; losing only the
        # metadata degrades the drain's re-check to fail-closed, which is safe.
        entries = durable_queue_entries(
            [{"id": "q1", "content": "hi", "kind": "", "meta": {"fn": lambda: None}}]
        )
        assert entries[0]["content"] == "hi"
        assert "meta" not in entries[0]

    def test_durable_copy_does_not_alias_the_live_entry(self) -> None:
        live = {"id": "q1", "content": "hi", "kind": "", "meta": {"sendId": "s-1"}}
        entries = durable_queue_entries([live])
        live["meta"]["sendId"] = "mutated"
        assert entries[0]["meta"]["sendId"] == "s-1"

    def test_entry_count_is_capped_front_first(self) -> None:
        queue = [
            {"id": f"q{i}", "content": f"m{i}", "kind": ""}
            for i in range(MAX_DURABLE_QUEUE_ENTRIES + 5)
        ]
        entries = durable_queue_entries(queue)
        assert len(entries) == MAX_DURABLE_QUEUE_ENTRIES
        # Front-first: the front of the queue is what runs first.
        assert entries[0]["id"] == "q0"

    def test_byte_budget_bounds_the_metadata_line(self) -> None:
        big = "x" * (MAX_DURABLE_QUEUE_BYTES // 2)
        queue = [{"id": f"q{i}", "content": big, "kind": ""} for i in range(4)]
        entries = durable_queue_entries(queue)
        assert 0 < len(entries) < 4
        assert len(json.dumps(entries)) <= MAX_DURABLE_QUEUE_BYTES

    def test_an_oversized_prompt_is_dropped_not_truncated(self) -> None:
        # A shortened prompt replayed as the user's own words is worse than one
        # reported as not carried.
        queue = [{"id": "q1", "content": "y" * (MAX_DURABLE_QUEUE_BYTES + 10), "kind": ""}]
        assert durable_queue_entries(queue) == []


class TestPersist:
    def test_queued_prompt_reaches_the_metadata_line(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("my follow-up", meta={"sendId": "s-1"})

        _save_slot_to_history(state, slot, closed=False)

        persisted = _meta(state)["queued_prompts"]
        assert [e["content"] for e in persisted] == ["my follow-up"]
        assert persisted[0]["meta"]["sendId"] == "s-1"

    def test_a_drained_queue_is_cleared_by_absence(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        qid = slot.queue_append("my follow-up")
        _save_slot_to_history(state, slot, closed=False)
        assert _meta(state).get("queued_prompts")

        # What the drain does: remove the entry, append its row.
        slot.queue_remove_by_id(qid)
        slot.append("user", "my follow-up")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)

        assert "queued_prompts" not in _meta(state)

    def test_the_key_is_absent_when_nothing_is_queued(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        assert "queued_prompts" not in _meta(state)

    def test_an_empty_window_save_still_persists_the_queue(self, tmp_path) -> None:
        # The forced empty-window save is a metadata mutation; its field
        # enumeration must mirror the full save's or a route that persists only
        # through it silently drops the queue.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "seed")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        slot.messages.clear()
        slot.queue_append("queued while empty")

        _save_slot_to_history(state, slot, force=True)

        assert [e["content"] for e in _meta(state)["queued_prompts"]] == ["queued while empty"]
        assert slot.queue_persist_pending is False

    def test_the_key_is_slot_owned_and_rows_only_defers_it(self) -> None:
        # Owned, so absence clears it. Deferred on a rows-only write, so the
        # handover drain cannot revert the live holder's own queue.
        assert "queued_prompts" in SLOT_OWNED_META_KEYS
        assert "queued_prompts" in ROWS_ONLY_DEFERRED_META_KEYS

    def test_rows_only_leaves_the_holders_queue_and_owes_its_own(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        holder = _busy_slot(state, "s1")
        holder.queue_append("the holder's prompt")
        _save_slot_to_history(state, holder, closed=False)

        # A popped slot writing its rows onto a line another slot published.
        popped = state.get_or_create_slot("s2")
        popped._tab_id = "otherslot"
        popped.linked_session_key = "dashboard:s1"
        popped.append("user", "handover row")
        popped.drain()
        popped.queue_append("the popped slot's prompt")

        _save_slot_to_history(state, popped, rows_only=True)

        assert [e["content"] for e in _meta(state)["queued_prompts"]] == ["the holder's prompt"]
        # Not credited: the popped slot's own entries are still owed, which is
        # the conservative side of the deferral.
        assert popped.queue_persist_pending is True


class TestDrift:
    def test_an_enqueue_makes_the_queue_owed_and_a_save_settles_it(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        assert slot.queue_persist_pending is False

        slot.queue_append("my follow-up")
        assert slot.queue_persist_pending is True

        _save_slot_to_history(state, slot, closed=False)
        assert slot.queue_persist_pending is False

    @pytest.mark.parametrize("mutate", ["clear", "reorder", "in_place_filter"])
    def test_an_in_place_mutation_is_detected_without_a_dirty_mark(self, tmp_path, mutate) -> None:
        # A reorder, a plan-approval filter and a force-stop clear all rewrite
        # the list directly, bypassing the repository. The signature is what
        # keeps them durable-correct anyway.
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("first")
        slot.queue_append("second")
        _save_slot_to_history(state, slot, closed=False)
        assert slot.queue_persist_pending is False

        if mutate == "clear":
            slot._queue.clear()
        elif mutate == "reorder":
            slot._queue[:] = list(reversed(slot._queue))
        else:
            slot._queue[:] = [e for e in slot._queue if e["content"] != "first"]

        assert slot.queue_persist_pending is True

    def test_a_system_entry_does_not_make_the_queue_owed(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)

        slot.queue_append('[Cron notification from "job"]', kind="cron_notification")

        assert slot.queue_persist_pending is False

    def test_the_periodic_flush_saves_a_clean_slot_that_owes_a_prompt(self, tmp_path) -> None:
        # The enqueue writes no row, so the transcript is not dirty. Without the
        # drift signal the flush skips the slot and the prompt is never durable.
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        slot._dirty = False
        slot.queue_append("my follow-up")
        slot._dirty = False

        state.flush_slot_now(slot)

        assert [e["content"] for e in _meta(state)["queued_prompts"]] == ["my follow-up"]

    def test_the_flush_still_skips_a_slot_that_owes_nothing(self, tmp_path, monkeypatch) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        slot._dirty = False
        saver = MagicMock()
        monkeypatch.setattr(
            "kiro_crew.dashboard.dashboard_persistence._current_slot_saver",
            lambda: saver,
        )

        state.flush_slot_now(slot)

        saver.assert_not_called()

    def test_a_resumed_slot_that_owes_a_prompt_is_not_skipped(self, tmp_path) -> None:
        # The no-op guard for a resumed slot compares window length; a queued
        # prompt lives on the metadata line, so the guard has to read it too.
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        slot._resumed_count = len(slot.messages)
        slot._dirty = False
        slot.queue_append("my follow-up")
        slot._dirty = False

        assert _save_slot_to_history(state, slot) is True
        assert [e["content"] for e in _meta(state)["queued_prompts"]] == ["my follow-up"]


class TestRestore:
    def test_rehydrate_hands_the_prompt_back_as_a_queue_card(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("my follow-up", meta={"sendId": "s-1"})
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")

        assert restored is not None
        assert [e["content"] for e in restored._queue] == ["my follow-up"]
        assert restored._queue[0]["meta"]["sendId"] == "s-1"
        # Handed back, not dispatched: the slot is idle and nothing drains it.
        assert restored.running is False
        # Nothing changed, so the first flush after the restart owes nothing.
        assert restored.queue_persist_pending is False

    def test_bulk_restore_hands_the_prompt_back(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("my follow-up")
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        assert restore_recent_sessions(state, window_minutes=0) >= 1

        assert [e["content"] for e in state._slots["s1"]._queue] == ["my follow-up"]
        assert state._slots["s1"].queue_persist_pending is False

    def test_a_restored_entry_stays_addressable(self, tmp_path) -> None:
        # Every queue gesture the user can make (promote, edit, delete) is keyed
        # by id, so a restored entry the user cannot name is a dead card.
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("my follow-up")
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]
        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None

        qid = restored._queue[0]["id"]
        assert restored.queue_edit_by_id(qid, "edited") is True
        assert restored.queue_remove_by_id(qid) == "edited"

    def test_an_entry_with_no_id_gets_one(self) -> None:
        entries = sanitize_restored_queue([{"content": "hi"}])
        assert entries[0]["id"]
        assert entries[0]["content"] == "hi"

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "not a list",
            {"content": "hi"},
            [None, 3, "x"],
            [{"content": ""}],
            [{"content": 5}],
            [{}],
        ],
    )
    def test_a_malformed_value_restores_nothing(self, raw) -> None:
        assert sanitize_restored_queue(raw) == []

    def test_restore_is_capped(self) -> None:
        raw = [{"id": f"q{i}", "content": "hi"} for i in range(MAX_DURABLE_QUEUE_ENTRIES + 5)]
        assert len(sanitize_restored_queue(raw)) == MAX_DURABLE_QUEUE_ENTRIES

    @pytest.mark.parametrize(
        "smuggled",
        [
            {"kind": "cron_notification"},
            {"payload": "resume"},
            {"_on_consumed": "x"},
            {"_on_irreversibly_consumed": "x"},
        ],
    )
    def test_a_tampered_entry_cannot_smuggle_a_system_entry(self, smuggled) -> None:
        # The history file is tamperable with disk access, and each of these
        # keys changes what the drain DOES with the entry.
        entry = {"id": "q1", "content": "hi", **smuggled}
        restored = sanitize_restored_queue([entry])
        assert restored[0]["kind"] == ""
        assert "payload" not in restored[0]
        assert "_on_consumed" not in restored[0]
        assert "_on_irreversibly_consumed" not in restored[0]

    def test_a_tampered_metadata_line_restores_a_plain_prompt_only(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        path = state.conversation_log._path("dashboard:s1")
        lines = path.read_text(encoding="utf-8").splitlines()
        meta = json.loads(lines[0])
        meta["queued_prompts"] = [
            {"id": "q1", "content": "hi", "kind": "cron_notification", "payload": "resume"},
            "junk",
        ]
        lines[0] = json.dumps(meta)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        restored = _rehydrate_slot_from_history(state, "s1")

        assert restored is not None
        assert [e["content"] for e in restored._queue] == ["hi"]
        assert restored._queue[0]["kind"] == ""
        assert "payload" not in restored._queue[0]

    def test_a_restored_prompt_is_not_a_human_directive(self, tmp_path) -> None:
        # The flag decides whether a drained turn may act on the message as an
        # authenticated human's directive, and the line it would come back from
        # is writable. So the restored prompt is deliberately downgraded: it is
        # replayed as the user's words, never as the user's authority.
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("do the thing", directive_user_origin=True)
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")

        assert restored is not None
        assert restored._queue[0]["content"] == "do the thing"
        assert restored._queue[0].get("_directive_user_origin") is None


class TestSignature:
    def test_order_is_part_of_the_identity(self) -> None:
        a = [{"id": "q1", "content": "one"}, {"id": "q2", "content": "two"}]
        assert queue_persist_signature(a) != queue_persist_signature(list(reversed(a)))

    def test_equal_values_share_one_signature(self) -> None:
        a = [{"id": "q1", "content": "one"}]
        b = [{"id": "q1", "content": "one"}]
        assert queue_persist_signature(a) == queue_persist_signature(b)

    def test_an_empty_queue_owes_nothing_on_a_fresh_slot(self, tmp_path) -> None:
        # A save rewrites the transcript, and an unnecessary one invalidates
        # every cache keyed on the file's mtime (the session-intent summary
        # among them), so a slot with nothing queued must not report a debt.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        assert slot._queue_persisted_sig == EMPTY_QUEUE_SIGNATURE
        assert slot.queue_persist_pending is False

    def test_emptying_a_persisted_queue_still_owes_the_clear(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("my follow-up")
        _save_slot_to_history(state, slot, closed=False)

        slot._queue.clear()

        assert slot.queue_persist_pending is True


class TestQueueWindowPairing:
    """The window and the queue value must be ONE observation.

    The drain pops an entry and appends its user row in a single event-loop
    step. The save runs in the flush executor thread, so reading the two halves
    separately lets a committed file show NEITHER — window frozen before the
    drain, queue read after it — which is the prompt vanishing with no row.
    """

    def _moving_queue(self, monkeypatch, slot, values: list[list[dict]]) -> list[int]:
        """Make each queue read return the next *values* entry.

        Both doors are stubbed from ONE sequence — the paired snapshot reads
        through ``durable_queue_view`` and the agreement check reads through
        ``durable_queue_entries`` — so a read is a read whichever name performs
        it, and the sequence still models a queue moving under the save.
        """
        calls = [0]
        remaining = list(values)

        def _read(self):  # noqa: ANN001 - bound through the class, __slots__ blocks instances
            calls[0] += 1
            return remaining.pop(0) if remaining else []

        def _view(self):  # noqa: ANN001 - same binding
            entries = _read(self)
            return entries, len(entries)

        monkeypatch.setattr(type(slot), "durable_queue_entries", _read)
        monkeypatch.setattr(type(slot), "durable_queue_view", _view)
        return calls

    def test_a_queue_that_settles_is_written_from_the_settled_pair(
        self, tmp_path, monkeypatch
    ) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("my follow-up")
        entry = [{"id": "q1", "content": "my follow-up"}]
        # Read 1 disagrees with read 2 (a drain landed mid-snapshot); reads 3
        # and 4 agree, so the pair the save writes is the settled one.
        self._moving_queue(monkeypatch, slot, [entry, [], [], []])

        assert _save_slot_to_history(state, slot, closed=False) is True
        assert _meta(state).get("queued_prompts", []) == []

    def test_a_queue_that_never_settles_refuses_the_save(self, tmp_path, monkeypatch) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("my follow-up")
        entry = [{"id": "q1", "content": "my follow-up"}]
        # Every paired read disagrees, so no pair is proven. Refusing leaves the
        # prompt owed by the drift check instead of committing a file that may
        # show neither the entry nor its row.
        self._moving_queue(monkeypatch, slot, [entry, [], entry, [], entry, [], entry, [], entry])

        assert _save_slot_to_history(state, slot, closed=False) is False

    def test_the_written_value_is_never_a_second_unpaired_read(self, tmp_path, monkeypatch) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("my follow-up")
        entry = [{"id": "q1", "content": "my follow-up"}]
        # Reads 1 and 2 agree, so the snapshot is the pair. A later re-read
        # returning something else must not reach the file.
        self._moving_queue(monkeypatch, slot, [entry, entry])

        assert _save_slot_to_history(state, slot, closed=False) is True
        assert [e["content"] for e in _meta(state)["queued_prompts"]] == ["my follow-up"]


class TestOverCapIsReportedNotSilent:
    def test_a_shortfall_names_the_slot_in_one_warning(self, tmp_path, caplog) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        for i in range(MAX_DURABLE_QUEUE_ENTRIES + 3):
            slot.queue_append(f"prompt {i}")

        with caplog.at_level("WARNING"):
            assert _save_slot_to_history(state, slot, closed=False) is True

        warnings = [r for r in caplog.records if "exceed the durable queue" in r.getMessage()]
        assert len(warnings) == 1
        assert "s1" in warnings[0].getMessage()
        assert "3 queued prompt(s)" in warnings[0].getMessage()
        # The send is NOT refused for it: everything that fits is still carried.
        assert len(_meta(state)["queued_prompts"]) == MAX_DURABLE_QUEUE_ENTRIES

    def test_a_within_cap_queue_warns_about_nothing(self, tmp_path, caplog) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("my follow-up")

        with caplog.at_level("WARNING"):
            _save_slot_to_history(state, slot, closed=False)

        assert not [r for r in caplog.records if "exceed the durable queue" in r.getMessage()]

    def test_counting_candidates_ignores_system_entries(self) -> None:
        queue = [
            {"id": "q1", "content": "mine", "kind": ""},
            {"id": "q2", "content": "a subagent finished", "kind": "subagent_completion"},
        ]
        assert count_durable_candidates(queue) == 1


class TestRestoreIsBounded:
    """The line read back is a trust boundary: the writer's bounds bound only
    what THIS gateway wrote, and whatever the restore admits is retained live,
    re-projected to every client and re-serialized by every later save."""

    def test_a_tampered_line_cannot_exceed_the_byte_budget(self) -> None:
        huge = "x" * (MAX_DURABLE_QUEUE_BYTES // 4)
        raw = [{"id": f"q{i}", "content": huge} for i in range(MAX_DURABLE_QUEUE_ENTRIES)]

        entries = sanitize_restored_queue(raw)

        assert len(entries) < MAX_DURABLE_QUEUE_ENTRIES
        assert len(json.dumps(entries)) <= MAX_DURABLE_QUEUE_BYTES

    def test_one_oversized_prompt_is_dropped_whole_not_truncated(self) -> None:
        raw = [
            {"id": "q1", "content": "x" * (MAX_DURABLE_QUEUE_BYTES + 1)},
            {"id": "q2", "content": "keep me"},
        ]

        entries = sanitize_restored_queue(raw)

        # Dropped, never shortened: a truncated prompt handed back as the user's
        # own words is worse than one reported as not carried.
        assert [e["content"] for e in entries] == ["keep me"]

    def test_meta_that_cannot_be_re_emitted_drops_the_entry(self) -> None:
        # A value json cannot re-emit would break every later save of this slot.
        raw = [
            {"id": "q1", "content": "mine", "meta": {"bad": {1, 2}}},
            {"id": "q2", "content": "ok"},
        ]

        assert [e["content"] for e in sanitize_restored_queue(raw)] == ["ok"]

    def test_the_count_cap_still_holds_for_small_prompts(self) -> None:
        raw = [{"id": f"q{i}", "content": "s"} for i in range(MAX_DURABLE_QUEUE_ENTRIES + 5)]

        assert len(sanitize_restored_queue(raw)) == MAX_DURABLE_QUEUE_ENTRIES


class TestEnqueueStartsTheWriteImmediately:
    """Waiting for the periodic flush leaves the loss window as wide as the
    flush interval. The accept therefore STARTS the durable write at once."""

    @pytest.mark.asyncio
    async def test_a_queued_prompt_reaches_disk_without_the_periodic_flush(self, tmp_path) -> None:
        import asyncio as _asyncio

        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)

        queue_for_next_turn(state, slot, "my follow-up", directive_user_origin=True)
        # Only the executor hand-off is awaited here; no flush loop is running.
        for _ in range(50):
            if _meta(state).get("queued_prompts"):
                break
            await _asyncio.sleep(0.02)

        assert [e["content"] for e in _meta(state)["queued_prompts"]] == ["my follow-up"]

    @pytest.mark.asyncio
    async def test_the_write_never_runs_on_the_event_loop(self, tmp_path) -> None:
        import asyncio as _asyncio
        import threading

        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real_flush = state.flush_slot_now

        def _record(target):  # noqa: ANN001 - test double
            seen.append(threading.get_ident())
            real_flush(target)

        state.flush_slot_now = _record

        queue_for_next_turn(state, slot, "my follow-up", directive_user_origin=True)
        for _ in range(50):
            if seen:
                break
            await _asyncio.sleep(0.02)

        assert seen and loop_thread not in seen

    def test_no_running_loop_leaves_the_write_to_the_flush(self, tmp_path) -> None:
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        state.flush_slot_now = MagicMock()

        # A synchronous caller (a tool call, a test) must not raise here.
        queue_for_next_turn(state, slot, "my follow-up", directive_user_origin=True)

        state.flush_slot_now.assert_not_called()
        assert slot.queue_persist_pending is True

    @pytest.mark.asyncio
    async def test_a_failed_background_write_is_logged_not_raised(self, tmp_path, caplog) -> None:
        import asyncio as _asyncio

        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        state.flush_slot_now = MagicMock(side_effect=OSError("disk gone"))

        with caplog.at_level("WARNING"):
            queue_for_next_turn(state, slot, "my follow-up", directive_user_origin=True)
            for _ in range(50):
                if any("Queued-prompt persist failed" in r.getMessage() for r in caplog.records):
                    break
                await _asyncio.sleep(0.02)

        assert any("Queued-prompt persist failed" in r.getMessage() for r in caplog.records)
        # Still owed, so the periodic flush retries it.
        assert slot.queue_persist_pending is True


class TestOneWriterPerSlot:
    """Two immediate writers snapshot the queue independently, and the
    transcript's file lock orders their COMMITS, not their reads. The older
    snapshot could therefore land last and put back a value that is missing an
    acknowledged prompt, which a restart in that interval would lose."""

    @pytest.mark.asyncio
    async def test_a_send_during_a_write_does_not_start_a_second_writer(self, tmp_path) -> None:
        import asyncio as _asyncio
        import threading

        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        release = threading.Event()
        entered = threading.Event()
        concurrent: list[int] = []
        live = 0
        guard = threading.Lock()
        real_flush = state.flush_slot_now

        def _slow_flush(target):  # noqa: ANN001 - test double
            nonlocal live
            with guard:
                live += 1
                concurrent.append(live)
            entered.set()
            release.wait(5)
            try:
                real_flush(target)
            finally:
                with guard:
                    live -= 1

        state.flush_slot_now = _slow_flush

        queue_for_next_turn(state, slot, "first", directive_user_origin=True)
        await _asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
        # Arrives while the first write holds the single-flight.
        queue_for_next_turn(state, slot, "second", directive_user_origin=True)
        assert slot._queue_persist_owed is True
        release.set()

        for _ in range(100):
            if len(_meta(state).get("queued_prompts") or []) == 2:
                break
            await _asyncio.sleep(0.02)

        # One writer at a time, and the prompt that arrived mid-write is still
        # carried: the debt is settled by a follow-up pass, not dropped.
        assert max(concurrent) == 1
        assert [e["content"] for e in _meta(state)["queued_prompts"]] == ["first", "second"]
        assert slot.queue_persist_pending is False

    @pytest.mark.asyncio
    async def test_the_single_flight_is_released_after_a_failed_write(self, tmp_path) -> None:
        import asyncio as _asyncio

        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        state.flush_slot_now = MagicMock(side_effect=OSError("disk gone"))

        queue_for_next_turn(state, slot, "mine", directive_user_origin=True)
        for _ in range(50):
            if not slot._queue_persist_inflight:
                break
            await _asyncio.sleep(0.02)

        # A failure that left the flag set would silence every later immediate
        # write for this slot's whole lifetime.
        assert slot._queue_persist_inflight is False
        assert slot.queue_persist_pending is True

    @pytest.mark.asyncio
    async def test_a_settled_debt_does_not_start_an_endless_chain(self, tmp_path) -> None:
        import asyncio as _asyncio

        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _busy_slot(state)
        calls: list[int] = []
        real_flush = state.flush_slot_now

        def _count(target):  # noqa: ANN001 - test double
            calls.append(1)
            real_flush(target)

        state.flush_slot_now = _count

        queue_for_next_turn(state, slot, "mine", directive_user_origin=True)
        for _ in range(50):
            if _meta(state).get("queued_prompts"):
                break
            await _asyncio.sleep(0.02)
        await _asyncio.sleep(0.1)

        # The follow-up is conditional on the queue still differing from disk, so
        # one send is one write.
        assert len(calls) == 1
        assert slot._queue_persist_owed is False


class TestTheShortfallIsOneObservation:
    """The over-cap warning subtracts two numbers, so both must come from the
    same read of the queue. Taken separately, a prompt that merely ARRIVED
    between them is reported as one the bounds refused."""

    def test_the_view_pairs_the_entries_with_their_count(self) -> None:
        queue = [
            {"id": "q1", "content": "mine", "kind": ""},
            {"id": "q2", "content": "a subagent finished", "kind": "subagent_completion"},
        ]

        entries, candidates = durable_queue_view(queue)

        assert [e["content"] for e in entries] == ["mine"]
        assert candidates == 1

    def test_the_view_is_unaffected_by_a_later_append(self) -> None:
        queue: list[dict] = [{"id": "q1", "content": "mine", "kind": ""}]

        entries, candidates = durable_queue_view(queue)
        queue.append({"id": "q2", "content": "arrived after", "kind": ""})

        # The pair describes the queue as it was READ, so the shortfall it feeds
        # stays 0 rather than blaming the bounds for a later arrival.
        assert candidates - len(entries) == 0

    def test_the_reported_shortfall_comes_from_the_paired_read(
        self, tmp_path, caplog, monkeypatch
    ) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("first")
        for i in range(9):
            slot.queue_append(f"also queued {i}")

        # The pair says two candidates did not fit. Reading the live queue for the
        # count instead would make this number drift with every send that lands
        # during the save.
        monkeypatch.setattr(
            type(slot),
            "durable_queue_view",
            lambda self: (self.durable_queue_entries(), len(self.durable_queue_entries()) + 2),
        )

        with caplog.at_level("WARNING"):
            assert _save_slot_to_history(state, slot, closed=False) is True

        messages = [
            r.getMessage() for r in caplog.records if "exceed the durable queue" in r.getMessage()
        ]
        assert len(messages) == 1
        assert "2 queued prompt(s)" in messages[0]


class TestRestoreBoundsTheReadNotOnlyTheRetention:
    """``sanitize_restored_queue`` is called from the loop-affine apply phase, so
    a hand-edited line must not buy work proportional to its own length."""

    def test_a_huge_tampered_list_is_not_walked_end_to_end(self) -> None:
        raw = [{"id": f"q{i}", "content": "s"} for i in range(MAX_DURABLE_QUEUE_SCAN * 10)]
        seen = 0

        class _Counting(list):
            def __getitem__(self, item):  # noqa: ANN001, ANN204 - test double
                nonlocal seen
                if isinstance(item, slice):
                    seen += 1
                return super().__getitem__(item)

        entries = sanitize_restored_queue(_Counting(raw))

        assert len(entries) == MAX_DURABLE_QUEUE_ENTRIES
        # The scan is bounded by a slice, not by iterating the whole value.
        assert seen == 1

    def test_the_unscanned_tail_is_reported_not_ignored(self, caplog) -> None:
        raw = [{"id": f"q{i}", "content": "s"} for i in range(MAX_DURABLE_QUEUE_SCAN + 7)]

        with caplog.at_level("WARNING"):
            sanitize_restored_queue(raw)

        messages = [
            r.getMessage() for r in caplog.records if "exceed the durable queue" in r.getMessage()
        ]
        assert len(messages) == 1
        # Every prompt not handed back is counted, including the ones past the
        # scan bound: an under-count would understate the loss.
        assert f"{len(raw) - MAX_DURABLE_QUEUE_ENTRIES} persisted queued prompt(s)" in messages[0]

    def test_the_scan_bound_is_above_the_retention_bound(self) -> None:
        # An ordinary line with some dropped entries must still restore
        # everything it should, so the read bound cannot pinch the keep bound.
        assert MAX_DURABLE_QUEUE_SCAN > MAX_DURABLE_QUEUE_ENTRIES


class TestHandoverDoesNotSilentlyDropAQueuedPrompt:
    """A queued prompt changes neither the window length nor ``_dirty``, so the
    hand-over's own "nothing owed" test must not answer True over it: the popped
    slot would take the prompt's only copy with it."""

    @pytest.mark.asyncio
    async def test_an_owed_prompt_alone_still_attempts_the_write(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        slot._dirty = False
        slot._disk_window_len = len(slot.messages)
        slot.queue_append("my follow-up")
        assert slot.queue_persist_pending is True

        saved = AsyncMock(return_value=True)
        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", saved)

        assert await chat_handlers._persist_handover_tail(state, "s1", slot) is True

        saved.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_deferred_queue_is_reported_with_its_count(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        slot._dirty = False
        slot._disk_window_len = len(slot.messages)
        slot.queue_append("first")
        slot.queue_append("second")

        # Commits, but carries the replacement's line: the rows-only path defers
        # every slot-owned field, so the queue is still owed afterwards.
        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", AsyncMock(return_value=True))

        with caplog.at_level("WARNING"):
            assert await chat_handlers._persist_handover_tail(state, "s1", slot) is True

        messages = [r.getMessage() for r in caplog.records if "were not carried" in r.getMessage()]
        assert len(messages) == 1
        assert "2 queued prompt(s)" in messages[0]
        assert "s1" in messages[0]

    @pytest.mark.asyncio
    async def test_a_carried_queue_is_not_reported(self, tmp_path, monkeypatch, caplog) -> None:
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("mine")

        async def _real_save(*_args, **_kwargs):
            _save_slot_to_history(state, slot, closed=False)
            return True

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _real_save)

        with caplog.at_level("WARNING"):
            assert await chat_handlers._persist_handover_tail(state, "s1", slot) is True

        # The entries reached disk, so there is nothing to report.
        assert not [r for r in caplog.records if "were not carried" in r.getMessage()]
        assert slot.queue_persist_pending is False

    @pytest.mark.asyncio
    async def test_a_truly_idle_slot_still_writes_nothing(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        slot._dirty = False
        slot._disk_window_len = len(slot.messages)

        saved = AsyncMock(return_value=True)
        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", saved)

        assert await chat_handlers._persist_handover_tail(state, "s1", slot) is True

        # Nothing owed is still nothing written: the early return is narrowed, not
        # removed, so a hand-over does not rewrite every idle transcript.
        saved.assert_not_awaited()


class TestAnAcceptedButUnpersistedPromptIsReported:
    """An entry the ceilings refuse is still accepted, so the refusal has to be
    told somewhere. It is told at WARNING in the gateway log, naming the slot and
    the counts. Reporting a bare "queued" and stopping there is the same silence
    this change exists to end, moved from the prompt to its acknowledgment.

    Pinned on the WARNING and on the observable durable effect rather than on a
    receipt field: a caller-visible ``durable`` boolean on the acknowledgments
    has no reader, so it is not shipped and the on-screen marker belongs with its
    consumer.
    """

    def test_an_ordinary_prompt_is_carried_and_warns_nothing(self, caplog) -> None:
        queue = [{"id": "q1", "content": "hello", "kind": ""}]
        with caplog.at_level(logging.WARNING, logger=_QUEUE_LOGGER):
            assert warn_if_not_durable(queue, "q1", "s1") is True
        assert caplog.records == []

    def test_a_prompt_past_the_count_cap_warns_and_names_the_cap(self, caplog) -> None:
        queue = [
            {"id": f"q{i}", "content": "x", "kind": ""}
            for i in range(MAX_DURABLE_QUEUE_ENTRIES + 1)
        ]
        last = queue[-1]["id"]
        with caplog.at_level(logging.WARNING, logger=_QUEUE_LOGGER):
            assert warn_if_not_durable(queue, queue[0]["id"], "s1") is True
            assert warn_if_not_durable(queue, last, "s1") is False
        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "s1" in msg and last in msg
        assert "NOT persisted" in msg
        assert str(MAX_DURABLE_QUEUE_ENTRIES) in msg
        # The counts, so the operator can see how much is uncarried.
        assert "{} candidate(s)".format(MAX_DURABLE_QUEUE_ENTRIES + 1) in msg

    def test_a_prompt_past_the_byte_budget_names_the_budget(self, caplog) -> None:
        queue = [
            {"id": "q1", "content": "x" * (MAX_DURABLE_QUEUE_BYTES - 200), "kind": ""},
            {"id": "q2", "content": "y" * 500, "kind": ""},
        ]
        # Position 2 of 2 and still refused: the two ceilings interact, which is
        # why the verdict and the reason are read off the writer's own output.
        with caplog.at_level(logging.WARNING, logger=_QUEUE_LOGGER):
            assert warn_if_not_durable(queue, "q1", "s1") is True
            assert warn_if_not_durable(queue, "q2", "s1") is False
        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "durable budget" in msg
        assert str(MAX_DURABLE_QUEUE_BYTES) in msg

    def test_a_system_entry_is_never_durable(self, caplog) -> None:
        queue = [{"id": "q1", "content": "cron fired", "kind": "cron"}]
        with caplog.at_level(logging.WARNING, logger=_QUEUE_LOGGER):
            assert warn_if_not_durable(queue, "q1", "s1") is False

    def test_an_unknown_or_empty_id_is_not_durable(self, caplog) -> None:
        queue = [{"id": "q1", "content": "hello", "kind": ""}]
        with caplog.at_level(logging.WARNING, logger=_QUEUE_LOGGER):
            assert warn_if_not_durable(queue, "nope", "s1") is False
            # An empty id names no entry, so there is nothing to report on.
            assert warn_if_not_durable(queue, "", "s1") is False
        assert len(caplog.records) == 1

    def test_neither_receipt_carries_a_durable_field(self) -> None:
        # The field was removed for want of a reader. Pinned so it cannot come
        # back without its consumer. Matched as a dict KEY (with the colon), so
        # an unrelated mention of the word in prose does not fail this.
        import inspect

        from kiro_crew.dashboard import chat_delivery, chat_handlers

        assert '"durable":' not in inspect.getsource(chat_delivery)
        assert '"durable":' not in inspect.getsource(chat_handlers)

    def test_an_ordinary_enqueue_emits_no_warning_and_reaches_disk(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        frames: list[tuple[str, dict]] = []
        monkeypatch.setattr(state, "broadcast_ws", lambda kind, data: frames.append((kind, data)))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_delivery.start_queue_persist", lambda *_a: None
        )

        queue_for_next_turn(state, slot, "my follow-up", directive_user_origin=True)

        pushes = [d for k, d in frames if k == "queue_push"]
        assert len(pushes) == 1
        assert "durable" not in pushes[0]
        # The observable effect the receipt field only described.
        _save_slot_to_history(state, slot, closed=False)
        assert [e["content"] for e in _meta(state)["queued_prompts"]] == ["my follow-up"]

    def test_an_over_cap_enqueue_warns_and_is_still_accepted(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        for i in range(MAX_DURABLE_QUEUE_ENTRIES):
            slot.queue_append(f"earlier {i}")
        monkeypatch.setattr(state, "broadcast_ws", lambda *_a: None)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_delivery.start_queue_persist", lambda *_a: None
        )

        with caplog.at_level(logging.WARNING, logger=_QUEUE_LOGGER):
            queue_for_next_turn(state, slot, "the 33rd", directive_user_origin=True)

        assert any("NOT persisted" in r.getMessage() for r in caplog.records)
        # Accepted all the same: refusing the send would take the words away at
        # the one moment they cannot be re-read from the transcript.
        assert slot._queue[-1]["content"] == "the 33rd"
        # And the entry genuinely does not reach disk, which is what the warning
        # is about.
        _save_slot_to_history(state, slot, closed=False)
        persisted = [e["content"] for e in _meta(state)["queued_prompts"]]
        assert "the 33rd" not in persisted
        assert len(persisted) == MAX_DURABLE_QUEUE_ENTRIES


class TestRestoredEntriesCarryNoHumanAuthority:
    """A restored entry must not claim authenticated-human provenance.

    The drain reduces the consumed entries' ``_directive_user_origin`` flags into
    ``producer_is_user_facing``, which admits user-surface and self-arming
    directives and exempts them from the LINKED containment constraint. The
    metadata line these entries come back from is an ordinary writable file in
    the crew home, so a carried flag is authority handed to whoever can write it.
    Neither half of the round trip may move it: the writer must not emit it, and
    the reader must not accept it even when it is there.
    """

    def test_the_writer_does_not_persist_the_directive_flags(self) -> None:
        queue = [
            {
                "id": "q1",
                "content": "arm a monitor",
                "kind": "",
                "_directive_user_origin": True,
                "_directive_channel_origin": True,
            }
        ]
        entries = durable_queue_entries(queue)
        assert entries == [{"id": "q1", "content": "arm a monitor"}]

    @pytest.mark.parametrize("flag", ["_directive_user_origin", "_directive_channel_origin"])
    def test_a_hand_added_flag_is_dropped_on_restore(self, flag) -> None:
        restored = sanitize_restored_queue([{"id": "q1", "content": "arm a monitor", flag: True}])
        assert restored[0]["content"] == "arm a monitor"
        assert flag not in restored[0]

    def test_a_restored_prompt_reaches_the_drain_as_non_directive(self, tmp_path) -> None:
        # End to end through the real metadata line: the flag is absent from the
        # written value, and a hand-added one does not survive the read back.
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("arm a monitor", directive_user_origin=True)
        _save_slot_to_history(state, slot, closed=False)

        persisted = _meta(state)["queued_prompts"]
        assert persisted == [{"id": persisted[0]["id"], "content": "arm a monitor"}]

        tampered = [{**persisted[0], "_directive_user_origin": True}]
        restored = sanitize_restored_queue(tampered)
        assert restored[0]["content"] == "arm a monitor"
        assert restored[0].get("_directive_user_origin") is None
        assert restored[0].get("_directive_channel_origin") is None


class TestTheTwoByteBudgetsAgree:
    """A durably-written queue must survive its own round trip.

    The reader's budget is charged against the same key projection the writer
    admits. Charging the reader for a key the writer never emitted makes the
    reader's budget the smaller of the two, so a queue persisted just under the
    ceiling drops its tail on the way back in -- losing a prompt that WAS
    written, which is the one outcome this value exists to prevent.
    """

    def test_a_queue_written_at_the_ceiling_restores_whole(self) -> None:
        # Sized from the two costs rather than from magic numbers, so the test
        # stays on the boundary if json's separators ever change. The head fills
        # the budget to within a few bytes of the tail's WRITER cost, leaving the
        # tail admissible to the writer and inadmissible to a reader that bills
        # itself for the ``kind`` it adds.
        tail = {"id": "q2", "content": "y" * 40}
        writer_cost = len(json.dumps(tail))
        reader_cost = len(json.dumps({**tail, "kind": ""}))
        assert reader_cost > writer_cost

        budget_left = writer_cost + (reader_cost - writer_cost) // 2
        overhead = len(json.dumps({"id": "q1", "content": ""}))
        head = {"id": "q1", "content": "x" * (MAX_DURABLE_QUEUE_BYTES - budget_left - overhead)}
        assert len(json.dumps(head)) == MAX_DURABLE_QUEUE_BYTES - budget_left

        written = durable_queue_entries([{**head, "kind": ""}, {**tail, "kind": ""}])
        assert [e["id"] for e in written] == ["q1", "q2"]

        # Both were durably written, so both must come back. Billing the reader
        # for its own added key drops "q2" here.
        restored = sanitize_restored_queue(written)
        assert [e["id"] for e in restored] == ["q1", "q2"]

    def test_the_restored_entry_still_wears_the_empty_kind(self) -> None:
        # The kind is excluded from the COST, not from the entry: it is what
        # keeps a restored prompt out of the system-injection paths.
        restored = sanitize_restored_queue([{"id": "q1", "content": "z" * 200}])
        assert restored[0]["kind"] == ""


class TestTheHoldBranchStartsTheWrite:
    """A prompt held for running sub-agents is written, not merely acknowledged.

    That branch holds an IDLE slot, so no drain is coming to write the prompt's
    row and no turn-end flush is scheduled. Its receipt already says whether the
    entry is durable, so the write has to start from the same place; otherwise
    the only record is memory until the last sub-agent finishes, which is
    unbounded.
    """

    def test_the_hold_branch_imports_the_shared_persist_seam(self) -> None:
        from kiro_crew.dashboard import chat_delivery, chat_handlers

        assert chat_handlers.start_queue_persist is chat_delivery.start_queue_persist

    def test_the_hold_branch_starts_a_persist_for_its_queued_prompt(
        self, tmp_path, monkeypatch
    ) -> None:
        import inspect

        from kiro_crew.dashboard import chat_handlers

        # The branch is inside the send handler behind a sub-agent probe; pin the
        # call at the source so the receipt and the write cannot drift apart.
        src = inspect.getsource(chat_handlers)
        hold = src.split("warn_if_not_durable(slot._queue, qid, slot.key)", 1)[1]
        hold = hold.split("return web.json_response(", 1)[0]
        assert "start_queue_persist(state, slot)" in hold

    def test_both_accept_paths_use_one_persist_function(self) -> None:
        from kiro_crew.dashboard import chat_delivery

        assert callable(chat_delivery.start_queue_persist)
        # Public, because two modules now depend on it: a private name would
        # invite the second caller to re-implement the single-flight.
        assert not chat_delivery.start_queue_persist.__name__.startswith("_")


class TestRestoredEntriesCarryNoAdmissionSnapshot:
    """A restored entry is re-checked against the constraints that hold NOW.

    The drain's re-check treats a constraint the entry recorded as already-held
    at admission as "not a change", so the two failure directions are opposite:
    an ABSENT snapshot is checked against every currently-held constraint and
    fails closed, while a FORGED all-True one reports nothing newly held and
    fails open. A hand-written entry would then drain into a linked or mirrored
    slot and republish to an audience its admission never contemplated.
    """

    def test_the_containment_snapshot_is_stripped_on_restore(self) -> None:
        from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY

        forged = {
            "linked": True,
            "mirrored": True,
            "app": True,
            "unattended": True,
            "ephemeral": True,
        }
        restored = sanitize_restored_queue(
            [
                {
                    "id": "q1",
                    "content": "post this to the channel",
                    "meta": {QUEUED_CONTAINMENT_META_KEY: forged, "sendId": "s-1"},
                }
            ]
        )
        assert QUEUED_CONTAINMENT_META_KEY not in restored[0]["meta"]

    def test_the_rest_of_meta_survives(self) -> None:
        # sendId and attachment lists decide nothing about audience; they bind
        # the sender's pre-send composer state to the entry.
        from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY

        restored = sanitize_restored_queue(
            [
                {
                    "id": "q1",
                    "content": "hi",
                    "meta": {
                        QUEUED_CONTAINMENT_META_KEY: {"linked": True},
                        "sendId": "s-1",
                        "attachments": ["a.png"],
                    },
                }
            ]
        )
        assert restored[0]["meta"] == {"sendId": "s-1", "attachments": ["a.png"]}

    def test_a_forged_snapshot_does_not_survive_the_real_round_trip(self, tmp_path) -> None:
        from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("hi", meta={QUEUED_CONTAINMENT_META_KEY: {"linked": True}})
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")

        assert restored is not None
        assert restored._queue[0]["content"] == "hi"
        assert QUEUED_CONTAINMENT_META_KEY not in restored._queue[0].get("meta", {})

    def test_the_drain_recheck_reports_a_held_constraint_after_restore(self) -> None:
        # The point of stripping: the re-check must SEE the constraint as newly
        # held. With the snapshot carried, this list is empty and the entry
        # drains unchecked.
        from kiro_crew.dashboard.session_control import (
            QUEUED_CONTAINMENT_META_KEY,
            newly_held_constraints,
        )

        now = {"linked": True, "mirrored": False, "app": False, "unattended": False}
        restored = sanitize_restored_queue(
            [
                {
                    "id": "q1",
                    "content": "hi",
                    "meta": {QUEUED_CONTAINMENT_META_KEY: {"linked": True, "mirrored": True}},
                }
            ]
        )
        assert newly_held_constraints(now, restored[0].get("meta")) == ["linked"]


class TestAStaleQueueSnapshotIsNotCommitted:
    """A writer holding an older queue value must not put it back on disk.

    The transcript's file lock orders the queue writers' COMMITS, not their
    reads. The immediate write, the periodic flush pass and ``chat_summary``'s
    own flush each take their own paired snapshot off-loop, so the one holding
    the older queue can acquire the lock second. The drift check leaves the
    newer value owed, so the next pass repairs disk — but a restart inside that
    interval loses an acknowledged prompt, which is the whole window the durable
    queue exists to close.
    """

    def _during_the_locked_write(self, state, hook) -> None:
        """Run *hook* once, inside the save's ``_locked`` region.

        ``get_metadata_status`` is the save's first read after it takes the
        lock, so a hook there lands exactly where a concurrent writer that
        committed while this save waited for the lock would have left its mark.
        """
        conv_log = state.conversation_log
        original = conv_log.get_metadata_status
        fired = {"done": False}

        def _patched(key):
            if not fired["done"]:
                fired["done"] = True
                hook()
            return original(key)

        conv_log.get_metadata_status = _patched

    def test_a_concurrent_writers_commit_refuses_the_older_snapshot(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("first")
        assert _save_slot_to_history(state, slot, closed=False) is True

        # A second writer commits a queue value this save never read, so the
        # value this one holds is older than what is already durable.
        slot.queue_append("second")
        slot.append("user", "a row this save would have carried")
        self._during_the_locked_write(
            state, lambda: setattr(slot, "_queue_persisted_sig", "another-writers-value")
        )

        assert _save_slot_to_history(state, slot, closed=False) is False
        assert [e["content"] for e in _meta(state)["queued_prompts"]] == ["first"]

    def test_a_queue_that_merely_moved_still_commits(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("first")

        # The drain pops an entry and appends its row while this save waits for
        # the lock. Ordinary: the pair was taken while it held, so writing it
        # commits a consistent past state and must not be refused.
        self._during_the_locked_write(state, lambda: slot.queue_append("arrived later"))

        assert _save_slot_to_history(state, slot, closed=False) is True
        assert [e["content"] for e in _meta(state)["queued_prompts"]] == ["first"]

    def test_a_deferring_rows_only_write_is_exempt(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        holder = _busy_slot(state, "s1")
        holder.queue_append("the holder's prompt")
        assert _save_slot_to_history(state, holder, closed=False) is True

        # A popped slot writing its rows onto another holder's line does not
        # decide the queue key at all, so the guard must not refuse it — its own
        # entries stay owed, which the hand-over reports.
        popped = state.get_or_create_slot("s2")
        popped._tab_id = "otherslot"
        popped.linked_session_key = "dashboard:s1"
        popped.append("user", "handover row")
        popped.drain()
        popped.queue_append("the popped slot's prompt")
        self._during_the_locked_write(
            state, lambda: setattr(popped, "_queue_persisted_sig", "another-writers-value")
        )

        assert _save_slot_to_history(state, popped, rows_only=True) is True
        assert [e["content"] for e in _meta(state)["queued_prompts"]] == ["the holder's prompt"]

    def test_the_refused_pass_leaves_the_prompt_owed(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("first")
        self._during_the_locked_write(
            state, lambda: setattr(slot, "_queue_persisted_sig", "another-writers-value")
        )

        assert _save_slot_to_history(state, slot, closed=False) is False
        # Owed, not dropped: the drift check still reports it, so the next pass
        # writes it against the state that actually exists.
        assert slot.queue_persist_pending is True

    def test_a_refused_pass_does_not_clear_the_window_dirty_bit(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("first")
        assert _save_slot_to_history(state, slot, closed=False) is True

        # Rows that have never reached disk, and a queue signal the overtaking
        # writer has already satisfied — so ``_dirty`` is the ONLY thing that can
        # bring the next pass back.
        slot.append("assistant", "a reply that has never reached disk")
        self._during_the_locked_write(
            state, lambda: setattr(slot, "_queue_persisted_sig", "another-writers-value")
        )

        state.flush_slot_now(slot)

        assert slot._dirty is True
        # And the next pass actually writes the rows the refusal did not.
        slot._queue_persisted_sig = queue_persist_signature(slot.durable_queue_entries())
        state.flush_slot_now(slot)
        assert slot._dirty is False
        rows = state.conversation_log.read_messages_chained("dashboard:s1")
        assert any("a reply that has never reached disk" in str(row) for row in rows)
