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

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
    restore_recent_sessions,
)
from kiro_crew.dashboard.chat_utils import CRON_NOTIFICATION_KIND
from kiro_crew.dashboard.queue_generation_store import STALE_INCARNATION
from kiro_crew.dashboard.queue_origin_token import (
    queue_provenance_proof,
    queue_record_seal,
    queue_record_seal_matches,
)
from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY
from kiro_crew.dashboard.slot_queue_repository import (
    EMPTY_QUEUE_SIGNATURE,
    MAX_DURABLE_QUEUE_BYTES,
    MAX_DURABLE_QUEUE_ENTRIES,
    MAX_DURABLE_QUEUE_SCAN,
    ORIGIN_GENERATION_KEY,
    ORIGIN_PROOF_KEY,
    REJECTED_SEAL_CONSTRAINT,
    UNRECORDED_GENERATION_CONSTRAINT,
    SlotQueueRepository,
    count_durable_candidates,
    dashboard_origin_proven,
    durable_queue_entries,
    durable_queue_view,
    forget_provenance,
    provenance_rejected,
    provenance_unrecorded,
    queue_persist_signature,
    restore_queue_provenance,
    sanitize_restored_queue,
    warn_if_not_durable,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import (
    ROWS_ONLY_DEFERRED_META_KEYS,
    SLOT_OWNED_META_KEYS,
    ConversationLog,
)
from kiro_crew.messaging.link import ChannelLink

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


def _inc(state, name: str = "s1") -> str:
    """The transcript incarnation the generation store binds its record to: the
    metadata line's ``created_at``."""
    return str(_meta(state, name).get("created_at") or "")


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
        monkeypatch.setattr(state, "notify", MagicMock())

        result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        assert result.rows_committed is True
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
        notify = MagicMock()
        monkeypatch.setattr(state, "notify", notify)

        with caplog.at_level("WARNING"):
            result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        assert result == chat_handlers._HandoverDrainResult(rows_committed=True, prompts_lost=2)

        messages = [r.getMessage() for r in caplog.records if "were not carried" in r.getMessage()]
        assert len(messages) == 1
        assert "2 queued prompt(s)" in messages[0]
        assert "s1" in messages[0]

        # The log is not reachable by the person whose words were dropped, so the
        # same fact goes to the notification feed — as a COUNT, never the text:
        # the entries may belong to a restricted session.
        assert notify.call_count == 1
        args = notify.call_args.args
        body = args[2]
        assert "2 queued prompt(s)" in body
        assert "s1" in body
        assert "first" not in body and "second" not in body
        assert notify.call_args.kwargs["meta"]["count"] == 2

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
        notify = MagicMock()
        monkeypatch.setattr(state, "notify", notify)

        with caplog.at_level("WARNING"):
            result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        assert result == chat_handlers._HandoverDrainResult(rows_committed=True, prompts_lost=0)

        # The entries reached disk, so there is nothing to report.
        assert not [r for r in caplog.records if "were not carried" in r.getMessage()]
        notify.assert_not_called()
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

        result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        assert result == chat_handlers._HandoverDrainResult(rows_committed=True, prompts_lost=0)

        # Nothing owed is still nothing written: the early return is narrowed, not
        # removed, so a hand-over does not rewrite every idle transcript.
        saved.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_declined_write_counts_the_pending_queue_as_lost(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        slot._dirty = False
        slot._disk_window_len = len(slot.messages)
        slot.queue_append("mine")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", AsyncMock(return_value=False))
        notify = MagicMock()
        monkeypatch.setattr(state, "notify", notify)

        result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        # The save wrote nothing, so a pending queue dies with the slot along
        # with the rows — and both halves of the answer say so.
        assert result == chat_handlers._HandoverDrainResult(rows_committed=False, prompts_lost=1)
        assert notify.call_count == 1
        assert notify.call_args.kwargs["meta"]["count"] == 1

    @pytest.mark.asyncio
    async def test_a_raising_write_counts_the_pending_queue_as_lost(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        slot._dirty = False
        slot._disk_window_len = len(slot.messages)
        slot.queue_append("mine")

        monkeypatch.setattr(
            chat_handlers, "save_slot_off_loop", AsyncMock(side_effect=OSError("disk wedged"))
        )
        notify = MagicMock()
        monkeypatch.setattr(state, "notify", notify)

        result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        assert result == chat_handlers._HandoverDrainResult(rows_committed=False, prompts_lost=1)
        assert notify.call_count == 1

    @pytest.mark.asyncio
    async def test_an_already_durable_queue_is_not_counted_lost_by_a_failed_write(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("mine")
        # A committed save carries the queue onto the durable line; a LATER
        # failed hand-over write leaves that line in place, so the entries
        # survive the popped object and must not be reported as lost.
        _save_slot_to_history(state, slot, closed=False)
        assert slot.queue_persist_pending is False
        slot.append("user", "an unsaved row")

        monkeypatch.setattr(
            chat_handlers, "save_slot_off_loop", AsyncMock(side_effect=OSError("disk wedged"))
        )
        notify = MagicMock()
        monkeypatch.setattr(state, "notify", notify)

        result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        assert result == chat_handlers._HandoverDrainResult(rows_committed=False, prompts_lost=0)
        notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_notification_does_not_fail_the_drain(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        slot._dirty = False
        slot._disk_window_len = len(slot.messages)
        slot.queue_append("mine")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", AsyncMock(return_value=True))
        monkeypatch.setattr(state, "notify", MagicMock(side_effect=RuntimeError("bus down")))

        # The hand-over has to complete for the replacement holding the key, so
        # a notice that cannot be delivered is logged and the answer still comes
        # back whole.
        result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        assert result == chat_handlers._HandoverDrainResult(rows_committed=True, prompts_lost=1)

    def test_the_result_is_truthy_even_when_the_write_failed(self) -> None:
        from kiro_crew.dashboard import chat_handlers

        # A NamedTuple is a non-empty tuple, so ``if not result:`` passes over a
        # failed drain. This pin makes the hazard explicit: callers must read
        # ``rows_committed``, and a future refactor back to truthiness testing
        # fails here first.
        failed = chat_handlers._HandoverDrainResult(rows_committed=False, prompts_lost=1)
        assert bool(failed) is True
        assert failed.rows_committed is False

    @pytest.mark.asyncio
    async def test_a_replacement_clearing_the_line_is_reported_as_loss(
        self, tmp_path, monkeypatch
    ) -> None:
        """The slot's own persistence signature says "my queue is durable", but
        the shared line is the entries' only copy — and a same-key recreate
        persists at birth, rebuilding that line without them. Survival must be
        read from the line, or exactly this hand-over reports zero loss."""
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("mine")
        # The original durably commits its queue: persist-pending goes False.
        _save_slot_to_history(state, slot, closed=False)
        assert slot.queue_persist_pending is False
        slot.append("user", "an unsaved row")

        # A same-key recreate takes the key and persists at birth: its full
        # save rebuilds the shared line, clearing ``queued_prompts`` by absence.
        state._slots.pop("s1")
        replacement = _busy_slot(state)
        _save_slot_to_history(state, replacement, closed=False)
        assert "queued_prompts" not in _meta(state)

        # The drain's rows-only write commits but never re-carries the queue.
        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", AsyncMock(return_value=True))
        notify = MagicMock()
        monkeypatch.setattr(state, "notify", notify)

        result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        assert result == chat_handlers._HandoverDrainResult(rows_committed=True, prompts_lost=1)
        assert notify.call_count == 1
        assert notify.call_args.kwargs["meta"]["count"] == 1

    @pytest.mark.asyncio
    async def test_a_live_replacement_dooms_line_entries_it_does_not_carry(
        self, tmp_path, monkeypatch
    ) -> None:
        """A read that finds the entries still on the line proves nothing while
        a transcript-sharing holder is alive: that holder's next full save
        rebuilds ``queued_prompts`` from its own queue. Survival is the
        holder's queue, not a lucky snapshot of the line."""
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("mine")
        _save_slot_to_history(state, slot, closed=False)
        assert slot.queue_persist_pending is False
        slot._dirty = False
        slot._disk_window_len = len(slot.messages)

        # The recreate has taken the key but not yet saved: the line still
        # shows the original's entries.
        state._slots.pop("s1")
        _busy_slot(state)
        assert "queued_prompts" in _meta(state)

        saved = AsyncMock(return_value=True)
        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", saved)
        notify = MagicMock()
        monkeypatch.setattr(state, "notify", notify)

        result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        assert result == chat_handlers._HandoverDrainResult(rows_committed=True, prompts_lost=1)
        assert notify.call_count == 1

    @pytest.mark.asyncio
    async def test_a_cleared_line_is_reported_even_when_nothing_needs_writing(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("mine")
        _save_slot_to_history(state, slot, closed=False)
        assert slot.queue_persist_pending is False
        slot._dirty = False
        slot._disk_window_len = len(slot.messages)

        state._slots.pop("s1")
        replacement = _busy_slot(state)
        _save_slot_to_history(state, replacement, closed=False)
        assert "queued_prompts" not in _meta(state)

        saved = AsyncMock(return_value=True)
        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", saved)
        notify = MagicMock()
        monkeypatch.setattr(state, "notify", notify)

        result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        # The no-write exit still answers for the entries: nothing here owes a
        # row, but the line holds none of the user's words and this frame is
        # their last reader.
        assert result == chat_handlers._HandoverDrainResult(rows_committed=True, prompts_lost=1)
        saved.assert_not_awaited()
        assert notify.call_count == 1

    @pytest.mark.asyncio
    async def test_entries_the_replacement_restored_are_not_reported_lost(
        self, tmp_path, monkeypatch
    ) -> None:
        """The line is the survival test in BOTH directions: entries a
        replacement's own save kept on the shared line live on as queue cards,
        so counting them lost would over-report."""
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("mine")
        _save_slot_to_history(state, slot, closed=False)
        assert slot.queue_persist_pending is False
        slot.append("user", "an unsaved row")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", AsyncMock(return_value=True))
        notify = MagicMock()
        monkeypatch.setattr(state, "notify", notify)

        # No clobber: the line still holds the entry this slot committed.
        result = await chat_handlers._persist_handover_tail(state, "s1", slot)

        assert result == chat_handlers._HandoverDrainResult(rows_committed=True, prompts_lost=0)
        notify.assert_not_called()


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
        # written value, and a hand-added one does not survive the read back. What
        # IS written beside the words is the gateway's own record seal, under the
        # write's generation -- the one provenance an editor cannot produce, which
        # is why it may ride along.
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("arm a monitor", directive_user_origin=True)
        _save_slot_to_history(state, slot, closed=False)

        persisted = _meta(state)["queued_prompts"]
        generation = slot._queue_generation
        assert persisted == [
            {
                "id": persisted[0]["id"],
                "content": "arm a monitor",
                ORIGIN_PROOF_KEY: queue_record_seal(
                    slot.key,
                    generation,
                    persisted[0]["id"],
                    "arm a monitor",
                    channel_address=None,
                    admission=None,
                ),
                ORIGIN_GENERATION_KEY: generation,
            }
        ]

        tampered = [{**persisted[0], "_directive_user_origin": True}]
        restored = sanitize_restored_queue(tampered)
        assert restored[0]["content"] == "arm a monitor"
        assert restored[0].get("_directive_user_origin") is None
        assert restored[0].get("_directive_channel_origin") is None

    @pytest.mark.asyncio
    async def test_a_restored_channel_entry_still_drains_as_channel_text(
        self, tmp_path, monkeypatch
    ) -> None:
        """The flags never reach disk, so a restored entry drains with none -- and
        text a CHANNEL handed off would then regain the composer's command word:
        `/workflow deploy` typed into a linked conversation, queued behind a busy
        turn, would run on the dashboard owner's authority after a restart. A
        restored entry has that authority only with the gateway's ADDRESS-LESS
        proof on it: channel text with no conversation stamped on it (this entry)
        gets no proof at all, and a placed hand-off's proof is over its address."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _busy_slot(state)
        queue_for_next_turn(
            state, slot, "/workflow deploy", directive_user_origin=True, channel_origin=True
        )
        assert slot._queue[0]["id"] not in slot._origin_proofs, "unplaced channel text was stamped"
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert [q["content"] for q in restored._queue] == ["/workflow deploy"]
        assert restored._queue[0].get("_directive_channel_origin") is None
        runs: list[dict] = []

        def _run(state_, slot_, message, **kwargs):
            runs.append({"message": message, **kwargs})
            return MagicMock()

        monkeypatch.setattr(cr, "_run_chat", _run)
        monkeypatch.setattr(cr, "spawn_guarded_turn", lambda *a, **kw: MagicMock())

        assert await cr._start_next_queued_turn(state, restored) is True

        assert runs and runs[0]["message"] == "/workflow deploy"
        assert runs[0]["_turn_provenance_restored"] is True
        assert (
            runs[0]["_directive_channel_origin"] is True
        ), "a restored channel entry regained the composer's command word"
        assert runs[0]["_directive_user_origin"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("edit", ["seal_and_meta"])
    async def test_an_editor_cannot_give_a_persisted_channel_command_the_composers_authority(
        self, tmp_path, monkeypatch, edit
    ) -> None:
        """The persisted line is an ordinary writable file. An editor removes a
        channel hand-off's seal and meta, leaving the record naming the write's
        generation -- the shape the writer itself emits for an entry it held no
        attestation for -- and the entry comes back WITHOUT the composer's command
        word: authority after a restart is granted only by a proof the gateway
        stamped and can verify, never by the absence of a marker. And having no
        conversation on it, it is not named a channel message either
        (``_channel_message`` false: no "linked conversation" refusal for words no
        conversation sent). Stripping the GENERATION too is a different case: the
        slot's committed generation is on record outside the line, so a line that
        names none is older than the store and is rejected
        (``TestTheLineMustNameTheCommittedGeneration``)."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _busy_slot(state)
        queue_for_next_turn(
            state, slot, "/workflow deploy", directive_user_origin=True, channel_origin=True
        )
        _save_slot_to_history(state, slot, closed=False)
        persisted = _meta(state)["queued_prompts"]
        edited = {"id": persisted[0]["id"], "content": persisted[0]["content"], "meta": {}}
        if edit == "seal_and_meta":
            edited[ORIGIN_GENERATION_KEY] = persisted[0][ORIGIN_GENERATION_KEY]
        state.conversation_log.update_metadata("dashboard:s1", {"queued_prompts": [edited]})
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert [q["content"] for q in restored._queue] == ["/workflow deploy"]
        assert restored._origin_proofs == {}
        assert not provenance_rejected(restored, restored._queue[0])
        runs: list[dict] = []

        def _run(state_, slot_, message, **kwargs):
            runs.append({"message": message, **kwargs})
            return MagicMock()

        monkeypatch.setattr(cr, "_run_chat", _run)
        monkeypatch.setattr(cr, "spawn_guarded_turn", lambda *a, **kw: MagicMock())

        assert await cr._start_next_queued_turn(state, restored) is True

        assert runs and runs[0]["message"] == "/workflow deploy"
        assert (
            runs[0]["_directive_channel_origin"] is True
        ), "an edited persisted channel command ran with the composer's authority after a restart"
        assert runs[0]["_channel_message"] is False, "words no conversation sent were named its own"

    @pytest.mark.asyncio
    async def test_a_dashboard_entry_keeps_its_command_word_through_a_restart(
        self, tmp_path, monkeypatch
    ) -> None:
        """The other half of default-deny: a composer-queued command is written with
        the gateway's proof, the proof survives the round trip, and the restored
        entry drains with the composer's command word it was accepted with."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _busy_slot(state)
        queue_for_next_turn(state, slot, "/workflow deploy", directive_user_origin=True)
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert dashboard_origin_proven(restored, restored._queue[0])
        runs: list[dict] = []

        def _run(state_, slot_, message, **kwargs):
            runs.append({"message": message, **kwargs})
            return MagicMock()

        monkeypatch.setattr(cr, "_run_chat", _run)
        monkeypatch.setattr(cr, "spawn_guarded_turn", lambda *a, **kw: MagicMock())

        assert await cr._start_next_queued_turn(state, restored) is True

        assert runs and runs[0]["message"] == "/workflow deploy"
        assert runs[0]["_turn_provenance_restored"] is True
        assert (
            runs[0]["_directive_channel_origin"] is False
        ), "a proven dashboard entry lost its command word across the restart"
        # Pruning on drain: the proof was read for the reduction above and is gone.
        assert restored._origin_proofs == {}


class TestTheOriginProof:
    """The gateway's provenance proof grants nothing an editor can produce.

    It is an HMAC over the slot key, the queue id, the content, the entry's channel
    address (none for dashboard text) and its admission snapshot, under a key
    derived from the fenced signing secret, so every way of rewriting the persisted
    line -- new words, a new address or snapshot, a proof moved between entries or
    slots, a hand-written tag -- verifies as nothing. It lives BESIDE the queue
    (``_origin_proofs``, keyed by id), never on the entry. An address-less proof is
    the DASHBOARD proof, the one that grants the composer's command word; a placed
    hand-off's proof is over its address and never verifies as that.
    """

    def _slot(self, key: str = "s1"):
        return SimpleNamespace(
            key=key, _queue=[], _last_enqueue_ts=None, _note_enqueue=lambda: None
        )

    def test_the_key_load_happens_in_the_warm_up_not_under_a_loop_bound_caller(
        self, monkeypatch
    ) -> None:
        """The first derivation of a proof key loads the signing secret -- file I/O
        that must not run on the event loop, where every caller of the proof sits
        (the enqueue stamp, the durable seal, the slot restore). ``warm_proof_keys``
        is the one-time load ``token_auth.warm_auth_singletons`` runs in a worker
        thread at startup; after it, no stamp, seal or restore reaches the loader.
        The module imports ``token_secret`` at module scope and calls its loader
        lazily, so importing it writes nothing."""
        from kiro_crew.dashboard import queue_origin_token as qot
        from kiro_crew.dashboard import token_secret

        monkeypatch.setattr(qot, "_DERIVED_KEYS", {})
        loads: list[str] = []
        real = token_secret._get_secret

        def _loader() -> bytes:
            loads.append("load")
            return real()

        monkeypatch.setattr(token_secret, "_get_secret", _loader)

        qot.warm_proof_keys()

        assert set(qot._DERIVED_KEYS) == {qot._ORIGIN_PROOF_DOMAIN, qot._RECORD_SEAL_DOMAIN}
        assert loads, "the warm-up did not load the secret"
        loads.clear()
        monkeypatch.setattr(
            token_secret, "_get_secret", lambda: pytest.fail("key file read on the loop")
        )
        repo = SlotQueueRepository()
        slot = self._slot()
        qid = repo.queue_append(slot, "arm a monitor", directive_user_origin=True)
        (record,) = durable_queue_entries(
            slot._queue, slot._origin_proofs, slot_key="s1", generation="g1"
        )
        assert record[ORIGIN_PROOF_KEY]
        owner = SimpleNamespace(key="s1")
        entries = sanitize_restored_queue([record])
        restore_queue_provenance(owner, entries, [record], committed_generation="g1")
        assert set(owner._origin_proofs) == {qid}
        # Idempotent: a second warm-up derives nothing again.
        qot.warm_proof_keys()
        assert loads == []

    def test_a_dashboard_entry_is_stamped_and_unplaced_channel_text_is_not(self) -> None:
        repo = SlotQueueRepository()
        slot = self._slot()
        repo.queue_append(slot, "arm a monitor", directive_user_origin=True)
        repo.queue_append(slot, "/workflow deploy", directive_channel_origin=True)
        dashboard, channel = slot._queue
        assert dashboard_origin_proven(slot, dashboard)
        # Channel text with no conversation stamped on it: the proof of a hand-off
        # is the proof of its address, so this one gets none and restores as prose.
        assert channel["id"] not in slot._origin_proofs
        assert not dashboard_origin_proven(slot, channel)
        # Neither entry carries the proof itself: the entry is exactly what was enqueued
        # (the process-local directive flag included), with no ``meta`` created for it.
        assert dashboard == {
            "id": dashboard["id"],
            "content": "arm a monitor",
            "kind": "",
            "_directive_user_origin": True,
        }

    def test_a_placed_hand_off_is_stamped_with_a_channel_proof(self) -> None:
        from kiro_crew.dashboard.channel_busy import CHANNEL_ORIGIN_META_KEY
        from kiro_crew.dashboard.queue_origin_token import queue_provenance_proof

        repo = SlotQueueRepository()
        slot = self._slot()
        address = {"channel_type": "discord", "channel_id": "c1", "thread_id": None}
        admitted = {"linked": False, "mirrored": True, "mirror_identity": "discord:c1:"}
        meta = {CHANNEL_ORIGIN_META_KEY: address, QUEUED_CONTAINMENT_META_KEY: admitted}
        qid = repo.queue_append(
            slot,
            "/workflow deploy",
            meta=meta,
            directive_user_origin=True,
            directive_channel_origin=True,
        )
        assert slot._origin_proofs == {
            qid: queue_provenance_proof(
                "s1", qid, "/workflow deploy", channel_address=address, admission=admitted
            )
        }
        # A channel proof is not a dashboard proof: no composer command word.
        assert not dashboard_origin_proven(slot, slot._queue[0])
        # And the proof is over the stamps: either rewritten fails it.
        assert (
            queue_provenance_proof(
                "s1", qid, "/workflow deploy", channel_address=None, admission=admitted
            )
            != slot._origin_proofs[qid]
        )
        assert (
            queue_provenance_proof(
                "s1",
                qid,
                "/workflow deploy",
                channel_address=address,
                admission={**admitted, "app": True},
            )
            != slot._origin_proofs[qid]
        )

    def test_rewritten_words_outlive_their_proof(self) -> None:
        repo = SlotQueueRepository()
        slot = self._slot()
        repo.queue_append(slot, "summarise this", directive_user_origin=True)
        entry = dict(slot._queue[0], content="/workflow deploy")
        assert not dashboard_origin_proven(slot, entry)

    def test_a_proof_does_not_move_between_entries_or_slots(self) -> None:
        repo = SlotQueueRepository()
        slot = self._slot()
        repo.queue_append(slot, "/workflow deploy", directive_user_origin=True)
        repo.queue_append(slot, "/workflow deploy", directive_channel_origin=True)
        proven, channel = slot._queue
        proof = slot._origin_proofs[proven["id"]]
        slot._origin_proofs[channel["id"]] = proof
        assert not dashboard_origin_proven(slot, channel), "a proof moved between entries verified"
        other = self._slot("s2")
        other._origin_proofs = {proven["id"]: proof}
        assert not dashboard_origin_proven(other, proven), "a proof verified against another slot"
        slot._origin_proofs[proven["id"]] = "deadbeef"
        assert not dashboard_origin_proven(slot, proven)
        slot._origin_proofs[proven["id"]] = 1
        assert not dashboard_origin_proven(slot, proven)
        del slot._origin_proofs[proven["id"]]
        assert not dashboard_origin_proven(slot, proven)
        assert not dashboard_origin_proven(SimpleNamespace(key="s1"), proven), "no sidecar at all"

    def test_an_edit_re_signs_the_words_and_an_unplaced_channel_edit_drops_the_proof(
        self,
    ) -> None:
        repo = SlotQueueRepository()
        slot = self._slot()
        qid = repo.queue_append(slot, "summarise this", directive_user_origin=True)
        assert repo.queue_edit_by_id(slot, qid, "/workflow deploy", directive_user_origin=True)
        assert dashboard_origin_proven(
            slot, slot._queue[0]
        ), "the edited words carry no valid proof"
        # Channel text with no conversation on the entry: no proof (see above).
        assert repo.queue_edit_by_id(slot, qid, "/workflow deploy", directive_channel_origin=True)
        assert qid not in slot._origin_proofs

    def test_the_store_is_bounded_by_the_durable_window(self) -> None:
        """A busy slot's follow-ups can outgrow the durable window; the proof store
        does not. Only the first ``MAX_ORIGIN_PROOFS`` durable entries in queue order
        hold a proof -- the only records a writer will ever seal -- so a queue of
        twice that many entries holds exactly that many proofs, the tail is unstamped,
        and a queue that keeps growing grows the store by nothing. Red on the previous
        head, where every queued entry held one."""
        from kiro_crew.dashboard.slot_queue_repository import MAX_ORIGIN_PROOFS

        repo = SlotQueueRepository()
        slot = self._slot()
        ids = [
            repo.queue_append(slot, f"follow-up {n}", directive_user_origin=True)
            for n in range(2 * MAX_ORIGIN_PROOFS)
        ]
        assert len(slot._queue) == 2 * MAX_ORIGIN_PROOFS
        assert set(slot._origin_proofs) == set(ids[:MAX_ORIGIN_PROOFS])
        assert all(dashboard_origin_proven(slot, e) for e in slot._queue[:MAX_ORIGIN_PROOFS])
        assert not any(dashboard_origin_proven(slot, e) for e in slot._queue[MAX_ORIGIN_PROOFS:])
        # What the writer seals is exactly what the store holds.
        sealed = durable_queue_entries(
            slot._queue, slot._origin_proofs, slot_key="s1", generation="g"
        )
        assert [r["id"] for r in sealed if ORIGIN_PROOF_KEY in r] == ids[:MAX_ORIGIN_PROOFS]
        # A system entry in the window is not durable and takes no place in it.
        repo.queue_insert(slot, 0, "cron", kind=CRON_NOTIFICATION_KIND)
        assert set(slot._origin_proofs) == set(ids[:MAX_ORIGIN_PROOFS])

    def test_the_proof_set_follows_the_window_into_which_the_head_drains(self) -> None:
        """GPT (``MAX_ORIGIN_PROOFS``): the tail entry that becomes the 32nd durable
        entry when the head drains held no proof, so the writer sealed nothing for
        it. ``reattest_durable_window`` -- what ``begin_durable_queue_write`` runs
        before the snapshot -- stamps exactly the live entries the window now
        holds without a proof; a promote to the front is the same case."""
        from kiro_crew.dashboard.slot_queue_repository import (
            MAX_ORIGIN_PROOFS,
            reattest_durable_window,
        )

        repo = SlotQueueRepository()
        slot = self._slot()
        ids = [
            repo.queue_append(slot, f"follow-up {n}", directive_user_origin=True)
            for n in range(MAX_ORIGIN_PROOFS + 2)
        ]
        assert set(slot._origin_proofs) == set(ids[:MAX_ORIGIN_PROOFS])
        assert reattest_durable_window(slot) == 0, "a full window re-stamped something"
        # The drain: pop the head and forget it, as the drain does.
        assert repo.queue_pop(slot, 0)["id"] == ids[0]
        forget_provenance(slot, ids[0])
        assert ids[MAX_ORIGIN_PROOFS] not in slot._origin_proofs, "stamped before the save"
        sealed = durable_queue_entries(
            slot._queue, slot._origin_proofs, slot_key="s1", generation="g"
        )
        assert ORIGIN_PROOF_KEY not in sealed[-1] and sealed[-1]["id"] == ids[MAX_ORIGIN_PROOFS]
        assert reattest_durable_window(slot) == 1
        assert set(slot._origin_proofs) == set(ids[1 : MAX_ORIGIN_PROOFS + 1])
        assert dashboard_origin_proven(slot, slot._queue[MAX_ORIGIN_PROOFS - 1])
        sealed = durable_queue_entries(
            slot._queue, slot._origin_proofs, slot_key="s1", generation="g"
        )
        assert [r["id"] for r in sealed if ORIGIN_PROOF_KEY in r] == ids[1 : MAX_ORIGIN_PROOFS + 1]
        assert reattest_durable_window(slot) == 0
        # A promote moves the unstamped tail to the front and pushes the last
        # window entry out; the save stamps the one and the next stamp prunes the
        # other.
        assert repo.queue_promote_by_id(slot, ids[-1])
        assert ids[-1] not in slot._origin_proofs
        assert reattest_durable_window(slot) == 1
        assert ids[-1] in slot._origin_proofs and dashboard_origin_proven(slot, slot._queue[0])
        assert len(slot._origin_proofs) == MAX_ORIGIN_PROOFS
        assert (
            ids[MAX_ORIGIN_PROOFS] not in slot._origin_proofs
        ), "the pushed-out entry kept a proof"

    def test_reattestation_stamps_only_what_this_process_accepted(self) -> None:
        """Never a restored entry (its fields came off the line; the seal vouched for
        them once or not at all), never channel text with no address (withheld at
        the enqueue, withheld here), never an entry already proven or rejected."""
        from kiro_crew.dashboard.channel_busy import CHANNEL_ORIGIN_META_KEY
        from kiro_crew.dashboard.slot_queue_repository import (
            RESTORED_QUEUE_KEY,
            reattest_durable_window,
        )

        repo = SlotQueueRepository()
        slot = self._slot()
        # Two restored entries at the head, as the reader hands them back: one
        # proven at the restore (proof in the sidecar), one unproven, one rejected.
        for n, content in enumerate(("/workflow deploy", "hello", "and the weather?")):
            slot._queue.append(
                {"id": f"r{n}", "content": content, "kind": "", RESTORED_QUEUE_KEY: True}
            )
        slot._origin_proofs = {
            "r0": queue_provenance_proof(
                "s1", "r0", "/workflow deploy", channel_address=None, admission=None
            )
        }
        slot._rejected_provenance = {"r2"}
        address = {
            "channel_type": "discord",
            "channel_id": "c1",
            "thread_id": None,
            "principal": "u1",
        }
        placed = repo.queue_append(
            slot, "placed", meta={CHANNEL_ORIGIN_META_KEY: address}, directive_channel_origin=True
        )
        unplaced = repo.queue_append(slot, "unplaced", directive_channel_origin=True)
        composer = repo.queue_append(slot, "composer", directive_user_origin=True)
        assert set(slot._origin_proofs) == {"r0", placed, composer}
        # Strip the live proofs as a drained head would have left them and re-attest.
        del slot._origin_proofs[placed]
        del slot._origin_proofs[composer]
        assert reattest_durable_window(slot) == 2
        assert set(slot._origin_proofs) == {"r0", placed, composer}
        assert slot._rejected_provenance == {"r2"}
        assert not dashboard_origin_proven(slot, slot._queue[1]), "a restored line gained a proof"
        assert unplaced not in slot._origin_proofs
        assert dashboard_origin_proven(slot, slot._queue[-1])
        assert reattest_durable_window(slot) == 0
        assert reattest_durable_window(SimpleNamespace(key="s1")) == 0, "a bare double"

    def test_a_restored_proof_is_kept_while_its_entry_is_queued(self) -> None:
        """A restored entry's proof cannot be minted again, so the window keeps it
        even when a promote pushes the entry past the first 32 durable entries;
        the store stays bounded by the reader's cap on restored entries plus the
        window."""
        from kiro_crew.dashboard.slot_queue_repository import (
            MAX_ORIGIN_PROOFS,
            RESTORED_QUEUE_KEY,
            reattest_durable_window,
        )

        repo = SlotQueueRepository()
        slot = self._slot()
        restored_ids = [f"r{n}" for n in range(MAX_ORIGIN_PROOFS)]
        for rid in restored_ids:
            slot._queue.append({"id": rid, "content": rid, "kind": "", RESTORED_QUEUE_KEY: True})
        slot._origin_proofs = {
            rid: queue_provenance_proof("s1", rid, rid, channel_address=None, admission=None)
            for rid in restored_ids
        }
        live = [repo.queue_append(slot, f"live {n}", directive_user_origin=True) for n in range(3)]
        assert set(slot._origin_proofs) == set(restored_ids), "a live tail entry was stamped"
        assert repo.queue_promote_by_id(slot, live[0])
        assert reattest_durable_window(slot) == 1
        # The last restored entry is now the 33rd durable entry and keeps its proof.
        assert set(slot._origin_proofs) == set(restored_ids) | {live[0]}
        assert all(dashboard_origin_proven(slot, e) for e in slot._queue[: MAX_ORIGIN_PROOFS + 1])
        repo.queue_append(slot, "another", directive_user_origin=True)
        assert set(slot._origin_proofs) == set(restored_ids) | {live[0]}, "a stamp pruned it"
        assert len(slot._origin_proofs) == MAX_ORIGIN_PROOFS + 1
        assert len(slot._origin_proofs) <= 2 * MAX_ORIGIN_PROOFS

    def test_a_removed_entry_leaves_the_store_at_once_and_a_pop_leaves_it_to_the_drain(
        self,
    ) -> None:
        """Pruning on drain, repository half: a card's removal forgets the entry's
        attestation and rejection the moment it leaves the queue; a pop deliberately
        does not -- the drain reads the popped entry's proof to reduce the turn's
        authority and forgets it right after (pinned at the drain in
        ``TestRestoredEntriesCarryNoHumanAuthority``). Red on the previous head (the
        removal left the proof until a later stamp)."""
        repo = SlotQueueRepository()
        slot = self._slot()
        first = repo.queue_append(slot, "one", directive_user_origin=True)
        second = repo.queue_append(slot, "two", directive_user_origin=True)
        third = repo.queue_append(slot, "three", directive_user_origin=True)
        slot._rejected_provenance = {second}
        assert set(slot._origin_proofs) == {first, second, third}

        assert repo.queue_remove_by_id(slot, second) == "two"
        assert set(slot._origin_proofs) == {first, third} and slot._rejected_provenance == set()
        assert repo.queue_pop(slot, 0)["id"] == first
        assert set(slot._origin_proofs) == {first, third}
        forget_provenance(slot, first)
        assert set(slot._origin_proofs) == {third}

    def test_a_dashboard_edit_re_authors_a_hand_off(self) -> None:
        """An edit that is not the conversation's (the dashboard's PATCH of a queued
        card) drops the conversation stamp with the channel flag: both halves of
        channel provenance move together, so the drain neither grants the entry the
        composer's word while naming it a channel message nor the reverse. The edited
        words are composer text with the composer's proof. Red on the previous head
        (the address stayed, the flag went: the owner's edited ``/compact`` was refused
        as "not available from a linked conversation" into the conversation)."""
        from kiro_crew.dashboard.channel_busy import CHANNEL_ORIGIN_META_KEY, channel_origin_address

        repo = SlotQueueRepository()
        slot = self._slot()
        address = {
            "channel_type": "discord",
            "channel_id": "c1",
            "thread_id": None,
            "principal": "u1",
        }
        admitted = {"mirrored": True, "mirror_identity": "discord:c1:"}
        qid = repo.queue_append(
            slot,
            "and the weather?",
            meta={CHANNEL_ORIGIN_META_KEY: address, QUEUED_CONTAINMENT_META_KEY: admitted},
            directive_user_origin=True,
            directive_channel_origin=True,
        )
        assert not dashboard_origin_proven(slot, slot._queue[0])

        assert repo.queue_edit_by_id(slot, qid, "/compact", directive_user_origin=True)

        (entry,) = slot._queue
        assert entry["content"] == "/compact"
        assert entry.get("_directive_channel_origin") is None
        assert channel_origin_address(entry.get("meta")) is None
        assert CHANNEL_ORIGIN_META_KEY not in entry["meta"]
        # The admission snapshot is the slot's, not the conversation's, and stays.
        assert entry["meta"][QUEUED_CONTAINMENT_META_KEY] == admitted
        assert dashboard_origin_proven(slot, entry)
        # A conversation's own edit keeps both halves.
        qid2 = repo.queue_append(
            slot,
            "again",
            meta={CHANNEL_ORIGIN_META_KEY: dict(address)},
            directive_user_origin=True,
            directive_channel_origin=True,
        )
        assert repo.queue_edit_by_id(
            slot, qid2, "again, please", directive_user_origin=True, directive_channel_origin=True
        )
        assert channel_origin_address(slot._queue[1]["meta"]) is not None
        assert slot._queue[1]["_directive_channel_origin"] is True

    def test_a_system_entry_keeps_its_shape_and_gets_no_proof(self) -> None:
        repo = SlotQueueRepository()
        slot = self._slot()
        repo.queue_append(slot, "[cron] nightly", kind="cron_notification")
        assert "meta" not in slot._queue[0]
        assert slot._origin_proofs == {}

    def test_a_consumed_entry_leaves_no_proof_behind(self) -> None:
        repo = SlotQueueRepository()
        slot = self._slot()
        first = repo.queue_append(slot, "one", directive_user_origin=True)
        repo.queue_pop(slot, 0)
        second = repo.queue_append(slot, "two", directive_user_origin=True)
        assert set(slot._origin_proofs) == {second}, first

    def test_the_proof_is_beside_the_queue_and_rides_the_record_not_the_entry(
        self, tmp_path
    ) -> None:
        """Red-first on the r5 head (the proof sat in the entry's ``meta``): the live
        entry and every board-facing projection carry no proof, the durable record
        carries the gateway's seal as its own field (with the write's generation
        beside it), and the restore path mints the entry's attestation back into
        the slot's sidecar so the restored entry verifies while still carrying no
        proof."""
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        meta = {"sendId": "send-1"}
        qid = slot.queue_append("/workflow deploy", meta=meta, directive_user_origin=True)
        # The entry is what was enqueued, the caller's own meta object included.
        assert slot._queue == [
            {
                "id": qid,
                "content": "/workflow deploy",
                "kind": "",
                "meta": {"sendId": "send-1"},
                "_directive_user_origin": True,
            }
        ]
        assert slot._queue[0]["meta"] is meta
        assert ORIGIN_PROOF_KEY not in json.dumps(slot.to_dict())
        assert ORIGIN_GENERATION_KEY not in json.dumps(slot.to_dict())
        # The sidecar holds the ATTESTATION; the durable record carries the SEAL
        # over the same fields plus the write's generation, beside id, content
        # and meta -- two different strings under two different keys.
        attestation = queue_provenance_proof(
            slot.key, qid, "/workflow deploy", channel_address=None, admission=None
        )
        assert slot._origin_proofs == {qid: attestation}
        record = slot.durable_queue_entries()
        generation = slot._queue_generation
        assert record == [
            {
                "id": qid,
                "content": "/workflow deploy",
                "meta": {"sendId": "send-1"},
                ORIGIN_PROOF_KEY: queue_record_seal(
                    slot.key,
                    generation,
                    qid,
                    "/workflow deploy",
                    channel_address=None,
                    admission=None,
                ),
                ORIGIN_GENERATION_KEY: generation,
            }
        ]
        assert record[0][ORIGIN_PROOF_KEY] != attestation
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert restored._queue[0]["meta"] == {"sendId": "send-1"}
        assert ORIGIN_PROOF_KEY not in restored._queue[0]
        assert ORIGIN_GENERATION_KEY not in restored._queue[0]
        assert ORIGIN_PROOF_KEY not in json.dumps(restored.to_dict())
        # The seal verified, so the sidecar holds the attestation this gateway
        # would have minted itself -- the same string as before the restart.
        assert restored._origin_proofs == {qid: attestation}
        assert dashboard_origin_proven(restored, restored._queue[0])
        # A reader handed no sidecar restores the entry unproven -- the fail-closed reading.
        bare = sanitize_restored_queue(_meta(state)["queued_prompts"])
        assert bare[0]["content"] == "/workflow deploy" and ORIGIN_PROOF_KEY not in bare[0]
        assert not dashboard_origin_proven(SimpleNamespace(key="s1"), bare[0])


class TestTheRecordSealIsPerWrite:
    """The durable record's seal names one entry in one durable write.

    Red-first on the r7 head, where the record carried the sidecar's attestation
    verbatim: a record captured off the line verified for as long as the signing
    key lived -- put back after its entry was consumed, it drained again. The
    writer seals each attested record under the slot's current GENERATION, a
    nonce minted whenever the durable value moves, and the seal is a different
    string from the attestation, under a different derived key.
    """

    def _slot(self, key: str = "s1"):
        return SimpleNamespace(
            key=key, _queue=[], _last_enqueue_ts=None, _note_enqueue=lambda: None
        )

    def test_the_writer_seals_an_attested_record_under_the_given_generation(self) -> None:
        repo = SlotQueueRepository()
        slot = self._slot()
        qid = repo.queue_append(slot, "hello", directive_user_origin=True)
        # No generation, no seal: the record is the bare projection.
        assert durable_queue_entries(slot._queue, slot._origin_proofs) == [
            {"id": qid, "content": "hello"}
        ]
        (record,) = durable_queue_entries(
            slot._queue, slot._origin_proofs, slot_key="s1", generation="g1"
        )
        assert record == {
            "id": qid,
            "content": "hello",
            ORIGIN_PROOF_KEY: queue_record_seal(
                "s1", "g1", qid, "hello", channel_address=None, admission=None
            ),
            ORIGIN_GENERATION_KEY: "g1",
        }
        # Two shapes: the seal is not the attestation, and another write's seal
        # over the same entry is another string.
        assert record[ORIGIN_PROOF_KEY] != slot._origin_proofs[qid]
        (again,) = durable_queue_entries(
            slot._queue, slot._origin_proofs, slot_key="s1", generation="g2"
        )
        assert again[ORIGIN_PROOF_KEY] != record[ORIGIN_PROOF_KEY]
        assert again[ORIGIN_GENERATION_KEY] == "g2"

    def test_an_entry_whose_attestation_no_longer_verifies_is_written_unsealed(self) -> None:
        # The seal says what the sidecar says: words rewritten without a re-stamp
        # are unproven live (``dashboard_origin_proven``) and unsealed on the line
        # -- under the write's generation, which every record of the write names,
        # so the restore can tell this record from one the write never emitted.
        repo = SlotQueueRepository()
        slot = self._slot()
        qid = repo.queue_append(slot, "summarise this", directive_user_origin=True)
        slot._queue[0]["content"] = "/workflow deploy"
        assert not dashboard_origin_proven(slot, slot._queue[0])
        (record,) = durable_queue_entries(
            slot._queue, slot._origin_proofs, slot_key="s1", generation="g1"
        )
        assert record == {"id": qid, "content": "/workflow deploy", ORIGIN_GENERATION_KEY: "g1"}

    def test_the_seal_is_over_every_field_it_names(self) -> None:
        address = {"channel_type": "discord", "channel_id": "c1", "thread_id": None}
        admission = {"mirrored": True, "mirror_identity": "discord:c1:"}
        seal = queue_record_seal(
            "s1", "g1", "q1", "hello", channel_address=address, admission=admission
        )
        assert queue_record_seal_matches("s1", "g1", "q1", "hello", address, admission, seal)
        for args in (
            ("s2", "g1", "q1", "hello", address, admission),  # another slot
            ("s1", "g2", "q1", "hello", address, admission),  # another write
            ("s1", "g1", "q2", "hello", address, admission),  # another entry
            ("s1", "g1", "q1", "hello!", address, admission),  # rewritten words
            ("s1", "g1", "q1", "hello", {**address, "channel_id": "c2"}, admission),
            ("s1", "g1", "q1", "hello", None, admission),  # address removed
            ("s1", "g1", "q1", "hello", address, {**admission, "app": True}),
            ("s1", "g1", "q1", "hello", address, None),  # snapshot removed
        ):
            assert not queue_record_seal_matches(*args, seal), args
        # Shapes off an untrusted line that are not even candidates.
        assert not queue_record_seal_matches("s1", "", "q1", "hello", address, admission, seal)
        assert not queue_record_seal_matches("s1", None, "q1", "hello", address, admission, seal)
        assert not queue_record_seal_matches("s1", "g1", "q1", "hello", address, admission, None)
        assert not queue_record_seal_matches("s1", "g1", "q1", "hello", address, admission, 7)
        assert not queue_record_seal_matches("s1", "g1", "q1", "hello", "c1", admission, seal)
        assert not queue_record_seal_matches("s1", "g1", "q1", "hello", address, [True], seal)
        assert not queue_record_seal_matches("s1", "g1", None, "hello", address, admission, seal)
        assert not queue_record_seal_matches("s1", "g1", "q1", None, address, admission, seal)
        # A mapping json cannot emit is no candidate either -- and never raises.
        assert not queue_record_seal_matches(
            "s1", "g1", "q1", "hello", {"x": object()}, admission, seal
        )

    def test_a_save_mints_a_generation_only_when_the_value_moved(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("one")
        _save_slot_to_history(state, slot, closed=False)
        (record,) = _meta(state)["queued_prompts"]
        first_generation = record[ORIGIN_GENERATION_KEY]
        assert first_generation == slot._queue_generation
        assert not slot.queue_persist_pending
        # An unchanged value written again keeps its generation: identical records
        # cannot be a replay, and the drift check stays settled.
        _save_slot_to_history(state, slot, closed=False, force=True)
        (record,) = _meta(state)["queued_prompts"]
        assert record[ORIGIN_GENERATION_KEY] == first_generation
        assert not slot.queue_persist_pending
        # A value that moved is written under a fresh one, shared by every record.
        slot.queue_append("two")
        assert slot.queue_persist_pending
        _save_slot_to_history(state, slot, closed=False)
        records = _meta(state)["queued_prompts"]
        assert [r["content"] for r in records] == ["one", "two"]
        assert {r[ORIGIN_GENERATION_KEY] for r in records} == {slot._queue_generation}
        assert slot._queue_generation != first_generation
        assert not slot.queue_persist_pending
        # A fresh slot starts with a generation of its own, so nothing here reads "".
        assert len(first_generation) == 32

    def test_the_enqueue_costing_sees_the_sealed_record(self) -> None:
        # ``warn_if_not_durable`` must bill the entry as the write will: an entry
        # that fits unsealed but not sealed is reported as NOT kept.
        repo = SlotQueueRepository()
        slot = self._slot()
        tail_id = repo.queue_append(slot, "y" * 40, directive_user_origin=True)
        (bare,) = durable_queue_entries(slot._queue, slot._origin_proofs)
        (sealed,) = durable_queue_entries(
            slot._queue, slot._origin_proofs, slot_key="s1", generation="g1"
        )
        bare_cost, sealed_cost = len(json.dumps(bare)), len(json.dumps(sealed))
        assert sealed_cost > bare_cost
        room = bare_cost + (sealed_cost - bare_cost) // 2
        overhead = len(json.dumps({"id": "head", "content": ""}))
        head = {"id": "head", "content": "x" * (MAX_DURABLE_QUEUE_BYTES - room - overhead)}
        queue = [head, slot._queue[0]]
        assert warn_if_not_durable(queue, tail_id, "s1", slot._origin_proofs) is True
        assert warn_if_not_durable(queue, tail_id, "s1", slot._origin_proofs, "g1") is False

    def test_the_writer_reads_its_sidecars_off_untrusted_plumbing(self) -> None:
        """Every writer call site takes *proofs* and *generation* off the slot with
        ``getattr``, so a bare double (the channel handlers' ``MagicMock`` slots)
        hands over attributes of any type. Anything but the shapes this module
        writes is "none": nothing sealed, nothing named, the record json-safe --
        the ``TypeError: Object of type MagicMock is not JSON serializable`` that
        broke ``test_handler_link_intercept`` on the r10 head, pinned."""
        entry = {"id": "q1", "content": "hello", "meta": {"sendId": "x"}}
        double = MagicMock()
        (record,) = durable_queue_entries(
            [entry], double._origin_proofs, slot_key="s1", generation=double._queue_generation
        )
        assert ORIGIN_GENERATION_KEY not in record and ORIGIN_PROOF_KEY not in record
        json.dumps(record)
        assert warn_if_not_durable([entry], "q1", "s1", double._origin_proofs, double._gen) is True


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

    def test_a_sealed_queue_written_at_the_ceiling_restores_whole(self) -> None:
        # The same boundary with the seal and generation on the records: the
        # reader bills both fields exactly as the writer emitted them, so a tail
        # admitted sealed is admitted back.
        repo = SlotQueueRepository()
        slot = SimpleNamespace(
            key="s1", _queue=[], _last_enqueue_ts=None, _note_enqueue=lambda: None
        )
        repo.queue_append(slot, "x", directive_user_origin=True)
        tail_id = repo.queue_append(slot, "y" * 40, directive_user_origin=True)
        head, tail = slot._queue
        (sealed_tail,) = durable_queue_entries(
            [tail], slot._origin_proofs, slot_key="s1", generation="g1"
        )
        assert ORIGIN_PROOF_KEY in sealed_tail and ORIGIN_GENERATION_KEY in sealed_tail
        writer_cost = len(json.dumps(sealed_tail))
        reader_cost = len(json.dumps({**sealed_tail, "kind": ""}))
        budget_left = writer_cost + (reader_cost - writer_cost) // 2
        (sealed_head,) = durable_queue_entries(
            [head], slot._origin_proofs, slot_key="s1", generation="g1"
        )
        overhead = len(json.dumps(sealed_head)) - len("x")
        head["content"] = "x" * (MAX_DURABLE_QUEUE_BYTES - budget_left - overhead)
        repo.queue_edit_by_id(slot, head["id"], head["content"], directive_user_origin=True)

        written = durable_queue_entries(
            slot._queue, slot._origin_proofs, slot_key="s1", generation="g1"
        )
        assert [e["id"] for e in written] == [head["id"], tail_id]
        assert all(ORIGIN_PROOF_KEY in e for e in written)
        assert len(json.dumps(written[0])) == MAX_DURABLE_QUEUE_BYTES - budget_left

        restored = sanitize_restored_queue(written)
        assert [e["id"] for e in restored] == [head["id"], tail_id]

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
        hold = src.split("warn_if_not_durable(slot._queue, qid, slot.key, ", 1)[1]
        hold = hold.split("return web.json_response(", 1)[0]
        assert "start_queue_persist(state, slot)" in hold

    def test_both_accept_paths_use_one_persist_function(self) -> None:
        from kiro_crew.dashboard import chat_delivery

        assert callable(chat_delivery.start_queue_persist)
        # Public, because two modules now depend on it: a private name would
        # invite the second caller to re-implement the single-flight.
        assert not chat_delivery.start_queue_persist.__name__.startswith("_")


class TestRestoredEntriesCarryNoAdmissionSnapshot:
    """The pure reader re-checks a restored entry against the constraints that hold NOW.

    The drain's re-check treats a constraint the entry recorded as already-held
    at admission as "not a change", so the two failure directions are opposite:
    an ABSENT snapshot is checked against every currently-held constraint and
    fails closed, while a FORGED all-True one reports nothing newly held and
    fails open. A hand-written entry would then drain into a linked or mirrored
    slot and republish to an audience its admission never contemplated. So
    ``sanitize_restored_queue`` strips the key, and only the second step
    (``restore_queue_provenance``) puts back a snapshot the gateway's own proof
    verifies -- the round-trip test below is the boundary between the two.
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
        """Red-first on the r6 head, in the other direction: there the gateway's OWN
        snapshot was stripped too, and a restart inside a turn on a mirrored slot
        dropped every queued prompt at the first drain. The snapshot the gateway
        recorded at admission rides its proof and comes back; the one an editor
        writes over it does not."""
        from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("hi", meta={QUEUED_CONTAINMENT_META_KEY: {"linked": True}})
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert restored._queue[0]["content"] == "hi"
        assert restored._queue[0]["meta"] == {QUEUED_CONTAINMENT_META_KEY: {"linked": True}}

        # The editor: the same record with the snapshot widened to every constraint.
        persisted = _meta(state)["queued_prompts"]
        forged = [
            {
                **persisted[0],
                "meta": {
                    QUEUED_CONTAINMENT_META_KEY: {
                        "linked": True,
                        "mirrored": True,
                        "app": True,
                        "unattended": True,
                        "ephemeral": True,
                    }
                },
            }
        ]
        state.conversation_log.update_metadata("dashboard:s1", {"queued_prompts": forged})
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert restored._queue[0]["content"] == "hi"
        assert QUEUED_CONTAINMENT_META_KEY not in restored._queue[0].get("meta", {})
        assert restored._origin_proofs == {}

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


class TestRestoredEntriesCarryNoSenderStamp:
    """A restored entry names no sender, so the drop notice has nowhere to go.

    The stamp differs from the two keys above in what it DOES: those are read,
    this one names a write target. The drop resolves the recipient of its notice
    from the stamp alone and appends the entry's own text there, so a stamp
    carried back off the metadata line would write attacker-chosen text into a
    session the editor does not own. Stripping costs one notice and keeps the
    delivery.
    """

    def test_the_sender_stamp_is_stripped_on_restore(self) -> None:
        from kiro_crew.dashboard.session_control import SEND_ORIGIN_META_KEY

        restored = sanitize_restored_queue(
            [
                {
                    "id": "q1",
                    "content": "write this into the victim transcript",
                    "meta": {
                        SEND_ORIGIN_META_KEY: {"slot": "victim-slot", "tab": "forged-tab"},
                        "sendId": "s-1",
                    },
                }
            ]
        )
        assert SEND_ORIGIN_META_KEY not in restored[0]["meta"]
        assert restored[0]["meta"] == {"sendId": "s-1"}

    def test_a_forged_stamp_resolves_to_no_recipient(self) -> None:
        # The end of the chain the strip breaks: with the key carried, this reads
        # "victim-slot" and the drop appends the entry's text there. The forged
        # tab is included because an editor can write both fields, so the strip
        # -- not the identity check -- is what has to stop this one.
        from kiro_crew.dashboard.session_control import (
            SEND_ORIGIN_META_KEY,
            send_origin_slot,
            send_origin_tab,
        )

        restored = sanitize_restored_queue(
            [
                {
                    "id": "q1",
                    "content": "hi",
                    "meta": {SEND_ORIGIN_META_KEY: {"slot": "victim-slot", "tab": "forged-tab"}},
                }
            ]
        )
        assert send_origin_slot(restored[0].get("meta")) == ""
        assert send_origin_tab(restored[0].get("meta")) == ""

    def test_the_stamp_does_not_survive_the_real_round_trip(self, tmp_path) -> None:
        from kiro_crew.dashboard.session_control import SEND_ORIGIN_META_KEY

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append(
            "hi", meta={SEND_ORIGIN_META_KEY: {"slot": "victim-slot", "tab": "forged-tab"}}
        )
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")

        assert restored is not None
        assert restored._queue[0]["content"] == "hi"
        assert SEND_ORIGIN_META_KEY not in restored._queue[0].get("meta", {})

    def test_an_in_process_stamp_still_reads(self, tmp_path) -> None:
        # The strip bounds the RESTORE path only: a stamp this process admitted
        # is what the notice is for, so removing it everywhere would delete the
        # feature rather than bound it.
        from kiro_crew.dashboard.session_control import (
            send_origin_meta,
            send_origin_slot,
            send_origin_tab,
        )

        state = _make_state(tmp_path)
        sender = state.get_or_create_slot("sender-slot")
        stamp = send_origin_meta(state, "sender-slot")

        assert send_origin_slot(stamp) == "sender-slot"
        assert send_origin_tab(stamp) == sender._tab_id


class TestARestoredHandOffKeepsItsBindingUnderTheGatewaysProof:
    """A channel hand-off comes back from a restart exactly as it was queued -- or not at all.

    Red-first on the r6 head, where this was the fenced GPT block: the restore
    stripped the hand-off's admission snapshot and channel stamp, the drain then
    read the slot's existing mirror as NEWLY added and deleted the prompt, and the
    channel notice was gated on the stamp the restore had removed -- so one gateway
    restart inside one dashboard turn on a bound session lost the sender's message
    with the sender told nothing. Now the gateway's proof covers the address and
    the snapshot, ``restore_queue_provenance`` puts both back BEFORE the drain
    re-validates, and the three outcomes the design promises hold across the
    restart: an unchanged binding drains, a changed binding drops WITH the notice
    to the conversation, and a stamp the gateway never signed -- an editor's --
    is stripped and reaches no send.
    """

    _ORIGIN = ChannelLink("discord", channel_id="c1")
    _VICTIM = {"channel_type": "discord", "channel_id": "victim-c1", "thread_id": None}

    def _mirror(self, state, link) -> None:
        # The binding, from the slot's side: the conversation resumes the session
        # through an inbound-capable mirror link the session store answers with.
        state.sessions.get_mirror_link = MagicMock(return_value=link)

    async def _restart_with_a_hand_off_queued(self, state, text: str = "and the weather?"):
        """Queue a hand-off behind a running dashboard turn on a mirrored slot, then
        lose the process: save, drop the slot, rehydrate from the line."""
        from kiro_crew.dashboard.channel_busy import HANDOFF_QUEUED, hand_to_dashboard_turn

        self._mirror(state, self._ORIGIN)
        slot = _busy_slot(state)
        pending = asyncio.get_running_loop().create_future()
        slot.task = pending
        try:
            outcome = hand_to_dashboard_turn(
                state, "dashboard:s1", text, origin=self._ORIGIN, principal="u1"
            )
        finally:
            pending.cancel()
        assert outcome == HANDOFF_QUEUED
        qid = slot._queue[0]["id"]
        admitted = slot._queue[0]["meta"][QUEUED_CONTAINMENT_META_KEY]
        assert admitted["mirrored"] is True and admitted["mirror_identity"] == "discord:c1:"
        _save_slot_to_history(state, slot, closed=False)
        del state._slots["s1"]
        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        return restored, qid, admitted

    def _watch_the_notice(self, monkeypatch) -> list[tuple[str, dict, str, str]]:
        from kiro_crew.dashboard import channel_busy

        told: list[tuple[str, dict, str, str]] = []

        async def _notify(
            st, session_key: str, origin, *, reason: str, principal: str = ""
        ) -> bool:
            told.append((session_key, origin.to_dict(), reason, principal))
            return True

        monkeypatch.setattr(channel_busy, "notify_channel_origin_dropped", _notify)
        return told

    @pytest.mark.asyncio
    async def test_the_hand_off_comes_back_as_it_was_queued(self, tmp_path) -> None:
        from kiro_crew.dashboard.channel_busy import (
            CHANNEL_ORIGIN_META_KEY,
            channel_origin_address,
            channel_origin_principal,
        )

        state = _make_state(tmp_path)
        restored, qid, admitted = await self._restart_with_a_hand_off_queued(state)

        (entry,) = restored._queue
        assert entry["content"] == "and the weather?"
        # Both signed parts are back on the entry, byte-for-byte what was admitted.
        assert entry["meta"][QUEUED_CONTAINMENT_META_KEY] == admitted
        assert entry["meta"][CHANNEL_ORIGIN_META_KEY] == {
            "channel_type": "discord",
            "channel_id": "c1",
            "thread_id": None,
            "principal": "u1",
        }
        link = channel_origin_address(entry["meta"])
        assert link is not None and (link.channel_type, link.channel_id) == ("discord", "c1")
        assert channel_origin_principal(entry["meta"]) == "u1"
        # The seal rides the record (with the write's generation); what is back in
        # the sidecar is the entry's ATTESTATION over the address and snapshot the
        # seal proved -- never on the entry or the board; and it is a CHANNEL
        # proof, so no composer command word.
        record = _meta(state)["queued_prompts"][0]
        assert record[ORIGIN_GENERATION_KEY] and record[ORIGIN_PROOF_KEY]
        assert restored._origin_proofs == {
            qid: queue_provenance_proof(
                "s1",
                qid,
                "and the weather?",
                channel_address=entry["meta"][CHANNEL_ORIGIN_META_KEY],
                admission=admitted,
            )
        }
        assert restored._origin_proofs[qid] != record[ORIGIN_PROOF_KEY]
        assert ORIGIN_PROOF_KEY not in entry
        assert ORIGIN_GENERATION_KEY not in entry
        assert ORIGIN_PROOF_KEY not in json.dumps(restored.to_dict())
        assert not dashboard_origin_proven(restored, entry)
        # The flags still never round-trip: the drain derives channel authority
        # from the proof, not from a marker on the line.
        assert entry.get("_directive_channel_origin") is None
        assert entry.get("_directive_user_origin") is None

    @pytest.mark.asyncio
    async def test_the_tail_hand_off_that_enters_the_window_restores_proven(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT's sequence (``MAX_ORIGIN_PROOFS``): 33 channel hand-offs queued behind a
        dashboard turn, the head drained once, the queue persisted, the process
        lost. On the previous head the 33rd -- just inside the durable window --
        was written unsealed: it restored as unproven prose with neither snapshot
        nor address, containment read every held constraint as newly held and
        dropped it, and with no address the drop told nobody. The save now attests
        it before the snapshot, so it comes back as it was queued and the drain
        keeps it."""
        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard.channel_busy import (
            CHANNEL_ORIGIN_META_KEY,
            HANDOFF_QUEUED,
            channel_origin_address,
            hand_to_dashboard_turn,
        )
        from kiro_crew.dashboard.slot_queue_repository import (
            MAX_ORIGIN_PROOFS,
            forget_provenance,
        )

        state = _make_state(tmp_path)
        self._mirror(state, self._ORIGIN)
        # The test IS the save: the immediate single-flight writer a queued send
        # kicks off (``chat_delivery.start_queue_persist``) would otherwise snapshot
        # the first entries on the default executor and commit them under the
        # direct save below, whose pass is then refused as stale (same idiom as
        # test_channel_busy.py).
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_delivery.start_queue_persist", lambda st, sl: None
        )
        slot = _busy_slot(state)
        pending = asyncio.get_running_loop().create_future()
        slot.task = pending
        try:
            for n in range(MAX_ORIGIN_PROOFS + 1):
                outcome = hand_to_dashboard_turn(
                    state, "dashboard:s1", f"follow-up {n}", origin=self._ORIGIN, principal="u1"
                )
                assert outcome == HANDOFF_QUEUED
        finally:
            pending.cancel()
        ids = [entry["id"] for entry in slot._queue]
        tail = ids[MAX_ORIGIN_PROOFS]
        admitted = slot._queue[-1]["meta"][QUEUED_CONTAINMENT_META_KEY]
        assert tail not in slot._origin_proofs, "the tail entry was stamped on arrival"
        # The drain consumes the head (pop, read, forget), as ``_start_next_queued_turn``
        # does with merging off; the 33rd is now the 32nd durable entry.
        slot.queue_pop(0)
        forget_provenance(slot, ids[0])
        assert slot._queue[-1]["id"] == tail and tail not in slot._origin_proofs

        _save_slot_to_history(state, slot, closed=False)
        records = _meta(state)["queued_prompts"]
        assert [r["id"] for r in records] == ids[1:]
        assert all(ORIGIN_PROOF_KEY in r for r in records), "a window record was written unsealed"
        assert tail in slot._origin_proofs, "the save did not attest the entry it sealed"
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert [e["id"] for e in restored._queue] == ids[1:]
        last = restored._queue[-1]
        assert last["id"] == tail and last["content"] == f"follow-up {MAX_ORIGIN_PROOFS}"
        assert last["meta"][QUEUED_CONTAINMENT_META_KEY] == admitted
        link = channel_origin_address(last["meta"])
        assert link is not None and (link.channel_type, link.channel_id) == ("discord", "c1")
        assert set(restored._origin_proofs) == set(ids[1:])
        assert restored._origin_proofs[tail] == queue_provenance_proof(
            "s1",
            tail,
            last["content"],
            channel_address=last["meta"][CHANNEL_ORIGIN_META_KEY],
            admission=admitted,
        )
        told = self._watch_the_notice(monkeypatch)
        cr._drop_stale_admissions(state, restored)
        assert [e["id"] for e in restored._queue] == ids[1:], "containment dropped a restored entry"
        assert told == []

    @pytest.mark.asyncio
    async def test_an_unchanged_binding_drains_and_runs_after_the_restart(
        self, tmp_path, monkeypatch
    ) -> None:
        """(a) Restart inside a dashboard turn with a bound hand-off queued: after
        the restore the prompt drains and runs, as channel text, in the session the
        conversation is still bound to. On the r6 head the drain deleted it here."""
        from kiro_crew.dashboard import chat_runner as cr

        state = _make_state(tmp_path)
        restored, _qid, _admitted = await self._restart_with_a_hand_off_queued(state)
        told = self._watch_the_notice(monkeypatch)

        cr._drop_stale_admissions(state, restored)
        await asyncio.sleep(0)

        assert [q["content"] for q in restored._queue] == ["and the weather?"]
        assert told == []
        assert not any(
            "Queued message dropped" in (m.get("content") or "")
            for m in restored.messages
            if isinstance(m, dict)
        )
        runs: list[dict] = []

        def _run(state_, slot_, message, **kwargs):
            runs.append({"message": message, **kwargs})
            return MagicMock()

        monkeypatch.setattr(cr, "_run_chat", _run)
        monkeypatch.setattr(cr, "spawn_guarded_turn", lambda *a, **kw: MagicMock())

        assert await cr._start_next_queued_turn(state, restored) is True

        assert runs and runs[0]["message"] == "and the weather?"
        assert runs[0]["_turn_provenance_restored"] is True
        assert runs[0]["_directive_channel_origin"] is True
        assert runs[0]["_directive_user_origin"] is False
        assert restored._queue == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("binding_now", "phrase"),
        [
            pytest.param(None, "left the session", id="released"),
            pytest.param(ChannelLink("discord", channel_id="c2"), "retargeted", id="retargeted"),
        ],
    )
    async def test_a_changed_binding_drops_and_tells_the_conversation(
        self, tmp_path, monkeypatch, binding_now, phrase
    ) -> None:
        """(b) The binding changed across the restart -- the conversation left the
        session, or the mirror moved to another one -- so the entry is dropped, and
        the conversation that sent it is told, at the address the restore proved and
        with the principal the dispatcher admitted."""
        from kiro_crew.dashboard import chat_runner as cr

        state = _make_state(tmp_path)
        restored, _qid, _admitted = await self._restart_with_a_hand_off_queued(state)
        self._mirror(state, binding_now)
        told = self._watch_the_notice(monkeypatch)

        cr._drop_stale_admissions(state, restored)
        await asyncio.sleep(0)  # the notice is fire-and-forget

        assert restored._queue == []
        assert len(told) == 1, told
        session_key, address, reason, principal = told[0]
        assert session_key == "dashboard:s1"
        assert (address["channel_type"], address["channel_id"]) == ("discord", "c1")
        assert phrase in reason
        assert principal == "u1"
        # The slot keeps its own notice too: both records exist.
        assert any(
            "Queued message dropped" in (m.get("content") or "")
            for m in restored.messages
            if isinstance(m, dict)
        )

    def test_an_editors_stamp_is_stripped_and_resolves_to_no_address(self) -> None:
        # The pure reader alone: a stamp on the line proves nothing.
        from kiro_crew.dashboard.channel_busy import (
            CHANNEL_ORIGIN_META_KEY,
            channel_binding_released,
            channel_origin_address,
        )

        restored = sanitize_restored_queue(
            [
                {
                    "id": "q1",
                    "content": "hi",
                    "meta": {CHANNEL_ORIGIN_META_KEY: dict(self._VICTIM), "sendId": "s-1"},
                }
            ]
        )
        assert restored[0]["meta"] == {"sendId": "s-1"}
        assert channel_origin_address(restored[0].get("meta")) is None
        # And without the stamp the entry is not a channel hand-off: an unmirrored
        # slot does not release it.
        assert channel_binding_released({"mirrored": False}, restored[0].get("meta")) is False

    @pytest.mark.asyncio
    async def test_a_stamp_the_gateway_never_signed_reaches_no_send_at_the_drain(
        self, tmp_path, monkeypatch
    ) -> None:
        """The editor adds a conversation to a composer prompt's record. Restored, the
        stamp is gone: rejected at the drain (the slot is linked and the unproven
        entry fails closed), the entry tells the conversation the editor named
        nothing -- the dashboard notice only, as before the proof existed."""
        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard.channel_busy import CHANNEL_ORIGIN_META_KEY

        state = _make_state(tmp_path)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _busy_slot(state)
        slot.queue_append("hi", directive_user_origin=True)
        _save_slot_to_history(state, slot, closed=False)
        persisted = _meta(state)["queued_prompts"]
        edited = [{**persisted[0], "meta": {CHANNEL_ORIGIN_META_KEY: dict(self._VICTIM)}}]
        state.conversation_log.update_metadata("dashboard:s1", {"queued_prompts": edited})
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert [q["content"] for q in restored._queue] == ["hi"]
        assert CHANNEL_ORIGIN_META_KEY not in restored._queue[0].get("meta", {})
        assert restored._origin_proofs == {}, "an edited record kept its proof"
        restored.linked_session_key = "discord:elsewhere:gen0"
        told = self._watch_the_notice(monkeypatch)

        cr._drop_stale_admissions(state, restored)
        await asyncio.sleep(0)

        assert restored._queue == [], "the unproven restored entry should have been rejected"
        assert told == [], "a stamp the gateway never signed reached a send"
        assert any(
            "Queued message dropped" in (m.get("content") or "")
            for m in restored.messages
            if isinstance(m, dict)
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("edit", ["address", "snapshot"])
    async def test_a_rewritten_address_or_snapshot_outlives_the_proof(
        self, tmp_path, monkeypatch, edit
    ) -> None:
        """A real hand-off's record, edited: the address pointed at another
        conversation, or the snapshot widened. Either edit fails the proof, so BOTH
        parts are stripped, the entry fails closed against the slot's live mirror,
        and neither the victim nor the original conversation is sent anything."""
        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard.channel_busy import CHANNEL_ORIGIN_META_KEY

        state = _make_state(tmp_path)
        restored, _qid, _admitted = await self._restart_with_a_hand_off_queued(state)
        persisted = _meta(state)["queued_prompts"]
        meta = dict(persisted[0]["meta"])
        if edit == "address":
            meta[CHANNEL_ORIGIN_META_KEY] = dict(self._VICTIM)
        else:
            meta[QUEUED_CONTAINMENT_META_KEY] = {**meta[QUEUED_CONTAINMENT_META_KEY], "app": True}
        state.conversation_log.update_metadata(
            "dashboard:s1", {"queued_prompts": [{**persisted[0], "meta": meta}]}
        )
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        (entry,) = restored._queue
        assert CHANNEL_ORIGIN_META_KEY not in entry["meta"]
        assert QUEUED_CONTAINMENT_META_KEY not in entry["meta"]
        assert restored._origin_proofs == {}
        told = self._watch_the_notice(monkeypatch)

        cr._drop_stale_admissions(state, restored)
        await asyncio.sleep(0)

        # The slot is still mirrored, which the stripped entry never recorded.
        assert restored._queue == []
        assert told == []

    # -- A seal is good for one entry in one durable record -----------------------

    async def _two_hand_offs_queued(self, state) -> tuple[Any, str, str]:
        """Two hand-offs from the bound conversation, queued behind one running turn."""
        from kiro_crew.dashboard.channel_busy import HANDOFF_QUEUED, hand_to_dashboard_turn

        self._mirror(state, self._ORIGIN)
        slot = _busy_slot(state)
        pending = asyncio.get_running_loop().create_future()
        slot.task = pending
        try:
            for text in ("first", "second"):
                outcome = hand_to_dashboard_turn(
                    state, "dashboard:s1", text, origin=self._ORIGIN, principal="u1"
                )
                assert outcome == HANDOFF_QUEUED
        finally:
            pending.cancel()
        first_id, second_id = (q["id"] for q in slot._queue)
        # Each accept started an immediate background write; let both land before
        # the explicit save below, which would otherwise be refused as the older
        # snapshot (``_queue_snapshot_is_stale``) and leave the line short.
        for _ in range(200):
            if not slot._queue_persist_inflight and not slot.queue_persist_pending:
                break
            await asyncio.sleep(0.01)
        return slot, first_id, second_id

    def _dashboard_notices(self, slot) -> list[str]:
        return [
            m.get("content") or ""
            for m in slot.messages
            if isinstance(m, dict) and "Queued message dropped" in (m.get("content") or "")
        ]

    async def _restore_and_drain(self, state, monkeypatch, line: list[dict]) -> tuple[Any, list]:
        """Put *line* on the transcript, lose the process, restore and re-validate."""
        from kiro_crew.dashboard import chat_runner as cr

        state.conversation_log.update_metadata("dashboard:s1", {"queued_prompts": line})
        del state._slots["s1"]
        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        told = self._watch_the_notice(monkeypatch)
        cr._drop_stale_admissions(state, restored)
        await asyncio.sleep(0)
        return restored, told

    @pytest.mark.asyncio
    async def test_a_seal_copied_onto_another_record_proves_nothing_for_it(
        self, tmp_path, monkeypatch
    ) -> None:
        """(a) The editor copies the first hand-off's seal onto the second's record.
        The seal is recomputed over the record it sits on -- the second's id and
        words -- so it fails: the second comes back stripped, fails closed against
        the mirror and is dropped with the dashboard notice, told to nobody; the
        first, whose seal is its own, drains. Held on the r7 head too (the seal was
        already over the id and content); pinned here beside the cases it did not
        hold for."""
        state = _make_state(tmp_path)
        slot, first_id, second_id = await self._two_hand_offs_queued(state)
        _save_slot_to_history(state, slot, closed=False)
        first, second = _meta(state)["queued_prompts"]
        assert (first["id"], second["id"]) == (first_id, second_id)

        restored, told = await self._restore_and_drain(
            state, monkeypatch, [first, {**second, ORIGIN_PROOF_KEY: first[ORIGIN_PROOF_KEY]}]
        )

        assert [q["content"] for q in restored._queue] == ["first"]
        assert set(restored._origin_proofs) == {first_id}
        assert told == [], "a record wearing another record's seal reached a send"
        assert len(self._dashboard_notices(restored)) == 1

    @pytest.mark.asyncio
    async def test_a_record_carried_twice_is_one_entry(self, tmp_path, monkeypatch) -> None:
        """(a), the copy that DID verify on the r7 head: the same record twice --
        id, words, stamps and seal all copied -- restored as two entries, both
        proven, and the one hand-off drained and ran twice. One record under an id
        is one entry: the second copy is not restored at all (it would also make
        every id-keyed queue mutation address the wrong entry), so the hand-off
        drains once."""
        state = _make_state(tmp_path)
        restored, qid, _admitted = await self._restart_with_a_hand_off_queued(state)
        (record,) = _meta(state)["queued_prompts"]
        assert record["id"] == qid

        restored, told = await self._restore_and_drain(state, monkeypatch, [record, dict(record)])

        assert [q["content"] for q in restored._queue] == ["and the weather?"]
        assert restored._origin_proofs.keys() == {qid}
        assert told == [] and self._dashboard_notices(restored) == []

    @pytest.mark.asyncio
    async def test_a_record_kept_from_a_superseded_write_is_a_replay_and_drops(
        self, tmp_path, monkeypatch
    ) -> None:
        """(b) Red-first on the r7 head. The first hand-off's record is captured
        off the line, the gateway consumes that hand-off and writes the queue again
        -- a new generation -- and the editor puts the old record back beside the
        current one. On r7 its seal still verified and the consumed prompt ran
        again with its channel authority. Now the line carries two generations,
        which no write of this gateway produces, so nothing on it is proven: the
        replayed entry is stripped, fails closed against the mirror and is dropped
        with the dashboard notice -- and so is the current one, the cost of a line
        this gateway did not write -- and neither reaches a send. In either
        order."""
        state = _make_state(tmp_path)
        slot, first_id, second_id = await self._two_hand_offs_queued(state)
        _save_slot_to_history(state, slot, closed=False)
        older = _meta(state)["queued_prompts"]
        assert [r["id"] for r in older] == [first_id, second_id]
        stale = older[0]
        # The gateway drains "first" and the next save writes the value that moved.
        slot.queue_remove_by_id(first_id)
        _save_slot_to_history(state, slot, closed=False)
        (current,) = _meta(state)["queued_prompts"]
        assert current["id"] == second_id
        assert current[ORIGIN_GENERATION_KEY] != stale[ORIGIN_GENERATION_KEY]

        for line in ([current, stale], [stale, current]):
            restored, told = await self._restore_and_drain(state, monkeypatch, line)
            assert restored._queue == [], line
            assert restored._origin_proofs == {}
            assert told == [], "a replayed record reached a send"
            assert len(self._dashboard_notices(restored)) == 2

    def test_the_reader_admits_one_record_per_id(self, caplog) -> None:
        # The pure reader alone: the second record under an id is not handed back,
        # whatever it says, and the fact is logged rather than silent.
        line = [
            {"id": "q1", "content": "one"},
            {"id": "q1", "content": "one again"},
            {"id": "q2", "content": "two"},
        ]
        with caplog.at_level(logging.WARNING, logger=_QUEUE_LOGGER):
            restored = sanitize_restored_queue(line)
        assert [(e["id"], e["content"]) for e in restored] == [("q1", "one"), ("q2", "two")]
        assert any("repeat an id already restored" in r.getMessage() for r in caplog.records)

    def test_the_restore_honours_no_seal_on_a_line_of_two_generations(self) -> None:
        # The pure verifier alone, for both orders and for a lone stale record.
        from kiro_crew.dashboard.slot_queue_repository import restore_queue_provenance

        def _sealed(generation: str, qid: str, text: str) -> dict:
            return {
                "id": qid,
                "content": text,
                "meta": {QUEUED_CONTAINMENT_META_KEY: {"mirrored": True}},
                ORIGIN_PROOF_KEY: queue_record_seal(
                    "s1",
                    generation,
                    qid,
                    text,
                    channel_address=None,
                    admission={"mirrored": True},
                ),
                ORIGIN_GENERATION_KEY: generation,
            }

        current = _sealed("gen-2", "q2", "two")
        stale = _sealed("gen-1", "q1", "one")
        for line in ([current, stale], [stale, current]):
            owner = SimpleNamespace(key="s1")
            entries = sanitize_restored_queue(line)
            restore_queue_provenance(owner, entries, line, committed_generation="gen-2")
            assert getattr(owner, "_origin_proofs", {}) == {}, line
            assert all(QUEUED_CONTAINMENT_META_KEY not in e.get("meta", {}) for e in entries)
            # Provenance present and false is a rejection, not "unproven".
            assert owner._rejected_provenance == {"q1", "q2"}, line
        # One generation, one write, the committed one: the same current record
        # alone is proven.
        owner = SimpleNamespace(key="s1")
        entries = sanitize_restored_queue([current])
        restore_queue_provenance(owner, entries, [current], committed_generation="gen-2")
        assert set(owner._origin_proofs) == {"q2"}
        assert getattr(owner, "_rejected_provenance", set()) == set()
        assert entries[0]["meta"][QUEUED_CONTAINMENT_META_KEY] == {"mirrored": True}
        # And a seal is over the generation it names: the same record re-labelled
        # with another generation fails -- and is rejected -- even when that label
        # is the committed one.
        relabelled = {**current, ORIGIN_GENERATION_KEY: "gen-3"}
        owner = SimpleNamespace(key="s1")
        entries = sanitize_restored_queue([relabelled])
        restore_queue_provenance(owner, entries, [relabelled], committed_generation="gen-3")
        assert getattr(owner, "_origin_proofs", {}) == {}
        assert owner._rejected_provenance == {"q2"}


class TestARestoredLineIsReadByWhatItsRecordsCarry:
    """Three readings of a restored record, told apart by what the line carries.

    Red-first on the r8 head, where "no verifiable seal" was one reading: a
    composer-queued ``/compact`` restored from a line written before records
    carried a seal drained with channel authority, was refused as "not available
    from a linked conversation" and reported so in the transcript and the SEL --
    while a record whose seal had been REWRITTEN was merely unproven, and drained
    as prose in any slot where no constraint happened to hold. Now a record whose
    provenance is present and false is rejected at the restore and dropped at the
    drain with the dashboard notice, whatever holds; an unsealed record that names
    the line's own write, and every record of a line with no seal or generation
    anywhere, stays unproven prose -- the narrower authority, and not a channel
    message unless a conversation's address is on it (see the editor test in
    ``TestRestoredEntriesCarryNoHumanAuthority``). Proof is minted only by a seal
    that verifies: an agent-writable line stripped of every seal buys nothing.
    """

    def _notices(self, slot) -> list[str]:
        return [
            m.get("content") or ""
            for m in slot.messages
            if isinstance(m, dict) and "Queued message dropped" in (m.get("content") or "")
        ]

    def _capture_runs(self, monkeypatch) -> list[dict]:
        from kiro_crew.dashboard import chat_runner as cr

        runs: list[dict] = []

        def _run(state_, slot_, message, **kwargs):
            runs.append({"message": message, **kwargs})
            return MagicMock()

        monkeypatch.setattr(cr, "_run_chat", _run)
        monkeypatch.setattr(cr, "spawn_guarded_turn", lambda *a, **kw: MagicMock())
        return runs

    @pytest.mark.asyncio
    async def test_a_line_from_before_seals_is_unproven_prose_not_a_channel_message(
        self, tmp_path, monkeypatch
    ) -> None:
        """(a) A pre-seal line: the composer's ``/compact``, persisted by a gateway
        that stamped nothing. Nothing on it is proven -- the file is agent-writable,
        so a line stripped of every seal cannot buy more than one that never had
        them -- and nothing on it is rejected: the entry drains with the narrower
        authority (no command word; the model gets the text as prose) and, having no
        conversation on it, is NOT named a channel message: ``_channel_message`` is
        False, so no "linked conversation" refusal is written for words no
        conversation sent. Red on the r8 head, where that refusal keyed on the
        authority flag alone."""
        from kiro_crew.dashboard import chat_runner as cr

        state = _make_state(tmp_path)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _busy_slot(state)
        _save_slot_to_history(state, slot, closed=False)
        line = [{"id": "q-legacy", "content": "/compact", "meta": {"sendId": "s-1"}}]
        state.conversation_log.update_metadata("dashboard:s1", {"queued_prompts": line})
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        (entry,) = restored._queue
        assert entry["content"] == "/compact" and entry["meta"] == {"sendId": "s-1"}
        assert restored._origin_proofs == {}
        assert not dashboard_origin_proven(restored, entry)
        assert not provenance_rejected(restored, entry)
        runs = self._capture_runs(monkeypatch)

        assert await cr._start_next_queued_turn(state, restored) is True

        assert runs and runs[0]["message"] == "/compact"
        assert runs[0]["_directive_channel_origin"] is True
        assert runs[0]["_channel_message"] is False
        assert self._notices(restored) == []

    @pytest.mark.asyncio
    async def test_a_record_whose_seal_does_not_verify_is_dropped_whatever_holds(
        self, tmp_path, monkeypatch
    ) -> None:
        """(b) The composer's entry, sealed by this gateway, its words rewritten
        under the seal. The slot is unlinked and unmirrored, so no containment
        constraint holds at the drain -- on the r8 head this entry drained as prose.
        Now the seal is present and false: the entry is rejected at the restore and
        the drain drops it with the dashboard notice naming the rejected record,
        retracts the card and runs nothing."""
        from kiro_crew.dashboard import chat_runner as cr

        state = _make_state(tmp_path)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        slot = _busy_slot(state)
        qid = slot.queue_append("summarise this", directive_user_origin=True)
        _save_slot_to_history(state, slot, closed=False)
        (record,) = _meta(state)["queued_prompts"]
        assert record[ORIGIN_PROOF_KEY] and record[ORIGIN_GENERATION_KEY]
        state.conversation_log.update_metadata(
            "dashboard:s1", {"queued_prompts": [{**record, "content": "/workflow deploy"}]}
        )
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        (entry,) = restored._queue
        assert entry["content"] == "/workflow deploy"
        assert restored._origin_proofs == {}
        assert provenance_rejected(restored, entry) and restored._rejected_provenance == {qid}
        assert not dashboard_origin_proven(restored, entry)
        popped: list[dict] = []
        monkeypatch.setattr(
            state,
            "broadcast_ws",
            lambda kind, payload: popped.append(payload) if kind == "queue_pop" else None,
        )
        runs = self._capture_runs(monkeypatch)

        assert await cr._start_next_queued_turn(state, restored) is False

        assert runs == [], "a record with a false seal ran"
        assert restored._queue == []
        assert popped == [{"slot": "s1", "content": "", "queue_id": qid}]
        (notice,) = self._notices(restored)
        assert (
            "its stored record was changed by something other than this gateway after it "
            "was queued, so the authorization that admitted it no longer holds." in notice
        ), notice
        assert REJECTED_SEAL_CONSTRAINT not in notice, "the raw constraint name leaked"

    def test_the_pure_reader_tells_the_three_readings_apart(self) -> None:
        """(d) The verifier alone, every reading on one slot key: proven, rejected
        (a false seal; a sealed line's record naming no generation or another
        write's; every record of a two-generation line) and unproven prose (an
        unsealed record naming the line's write); and the pre-seal line."""

        def _sealed(generation: str, qid: str, text: str) -> dict:
            return {
                "id": qid,
                "content": text,
                "meta": {QUEUED_CONTAINMENT_META_KEY: {"mirrored": True}},
                ORIGIN_PROOF_KEY: queue_record_seal(
                    "s1", generation, qid, text, channel_address=None, admission={"mirrored": True}
                ),
                ORIGIN_GENERATION_KEY: generation,
            }

        def _read(line: list[dict], committed: str | None = "gen-1") -> tuple[Any, list[dict]]:
            owner = SimpleNamespace(key="s1")
            entries = sanitize_restored_queue(line)
            restore_queue_provenance(owner, entries, line, committed_generation=committed)
            return owner, entries

        good = _sealed("gen-1", "q-good", "kept")
        false_seal = {**_sealed("gen-1", "q-false", "kept"), "content": "rewritten"}
        unsealed_same_write = {"id": "q-prose", "content": "prose", ORIGIN_GENERATION_KEY: "gen-1"}
        no_generation = {"id": "q-bare", "content": "bare"}
        other_write = {"id": "q-other", "content": "old", ORIGIN_GENERATION_KEY: "gen-0"}

        owner, entries = _read([good, false_seal, unsealed_same_write, no_generation])
        assert set(owner._origin_proofs) == {"q-good"}
        assert owner._rejected_provenance == {"q-false", "q-bare"}
        assert entries[0]["meta"][QUEUED_CONTAINMENT_META_KEY] == {"mirrored": True}
        assert all(QUEUED_CONTAINMENT_META_KEY not in e.get("meta", {}) for e in entries[1:])
        assert provenance_rejected(owner, entries[1]) and not provenance_rejected(owner, entries[2])

        # A record naming another write makes the line one of two generations:
        # nothing on it is proven and every record that names a write is rejected;
        # the unsealed record of neither write is rejected too.
        owner, entries = _read([good, other_write, unsealed_same_write])
        assert getattr(owner, "_origin_proofs", {}) == {}
        assert owner._rejected_provenance == {"q-good", "q-other", "q-prose"}

        # The pre-seal line, for a slot the store holds no record for: no record
        # names a write, so nothing is proven and nothing is rejected -- every
        # entry is unproven prose.
        owner, entries = _read(
            [
                {"id": "q-1", "content": "/compact"},
                {"id": "q-2", "content": "hello", "meta": {"sendId": "x"}},
            ],
            committed=None,
        )
        assert getattr(owner, "_origin_proofs", {}) == {}
        assert getattr(owner, "_rejected_provenance", set()) == set()
        assert not any(dashboard_origin_proven(owner, e) for e in entries)
        assert not any(provenance_rejected(owner, e) for e in entries)

    def test_an_edit_re_authors_a_rejected_entry(self, tmp_path) -> None:
        """(e) The rejection describes the record the entry was restored from. An
        edit through the authenticated dashboard re-stamps the entry with the
        composer's proof over the new words, so the drain would drop a prompt its
        user just wrote; the re-stamp clears the rejection instead."""
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        qid = slot.queue_append("summarise this", directive_user_origin=True)
        _save_slot_to_history(state, slot, closed=False)
        (record,) = _meta(state)["queued_prompts"]
        state.conversation_log.update_metadata(
            "dashboard:s1", {"queued_prompts": [{**record, "content": "rewritten"}]}
        )
        del state._slots["s1"]
        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert restored._rejected_provenance == {qid}

        assert restored.queue_edit_by_id(qid, "my own words", directive_user_origin=True)

        (entry,) = restored._queue
        assert entry["content"] == "my own words"
        assert restored._rejected_provenance == set()
        assert dashboard_origin_proven(restored, entry)


class TestTheLineMustNameTheCommittedGeneration:
    """The restore honours a queued-prompt line only when its one generation is the
    one the save committed OUTSIDE the transcript (``queue_generation_store``).

    Red-first on the r10 head, where the generation lived on the line alone: an
    agent that put an OLDER line back whole -- every record naming that older
    generation, every seal verifying under it -- had a ``/goal`` or ``/workflow``
    the gateway had already consumed restored with the authority it was queued
    with, and it ran again (GPT, ``slot_queue_repository.py:915``). The committed
    generation is now the save's word kept where the line's editor cannot follow
    -- a top-level crew-home leaf masked from every sandbox and fenced from the
    file tools, like ``tag-grants`` -- and a line that names anything else is
    rejected whole: dropped at the drain with the dashboard notice, told to
    nobody. The two costs are fail-closed and pinned here too: a process lost
    between committing the line and its generation, and a line stripped of every
    seal and generation for a slot that HAS a committed one, both drop the queue
    rather than honour it.
    """

    _r7 = TestARestoredHandOffKeepsItsBindingUnderTheGatewaysProof()

    def _notices(self, slot) -> list[str]:
        return TestARestoredLineIsReadByWhatItsRecordsCarry._notices(self, slot)

    async def _rolled_back(
        self, state, monkeypatch, line: list[dict]
    ) -> tuple[Any, list, list[dict]]:
        """Put *line* on the transcript as an editor would, lose the process, restore
        and drain; return the slot, the sender notices and the turns that ran."""
        from kiro_crew.dashboard import chat_runner as cr

        state.conversation_log.update_metadata("dashboard:s1", {"queued_prompts": line})
        del state._slots["s1"]
        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        told = self._r7._watch_the_notice(monkeypatch)
        runs = TestARestoredLineIsReadByWhatItsRecordsCarry._capture_runs(self, monkeypatch)
        cr._drop_stale_admissions(state, restored)
        await asyncio.sleep(0)
        await cr._start_next_queued_turn(state, restored)
        return restored, told, runs

    @pytest.mark.asyncio
    async def test_the_save_commits_the_lines_generation_outside_the_transcript(
        self, tmp_path
    ) -> None:
        """(c) The happy path, with the store read beside it: the line's records name
        one generation, the store names the same one for the slot, and the restored
        hand-off drains with its binding as on the r7 head."""
        from kiro_crew.dashboard import queue_generation_store as store
        from kiro_crew.dashboard.channel_busy import CHANNEL_ORIGIN_META_KEY

        state = _make_state(tmp_path)
        restored, qid, admitted = await self._r7._restart_with_a_hand_off_queued(state)
        (record,) = _meta(state)["queued_prompts"]
        assert store.read_committed_generation("s1", _inc(state)) == record[ORIGIN_GENERATION_KEY]
        assert restored._queue_generation_committed == record[ORIGIN_GENERATION_KEY]
        (entry,) = restored._queue
        assert entry["meta"][QUEUED_CONTAINMENT_META_KEY] == admitted
        assert entry["meta"][CHANNEL_ORIGIN_META_KEY]["channel_id"] == "c1"
        assert set(restored._origin_proofs) == {qid}
        # The record lives under the fenced leaf of the crew data home, not beside
        # the transcript.
        from kiro_crew.config.paths import config_dir

        record_files = list((config_dir() / store.STORE_DIR_LEAF).glob("*.json"))
        assert len(record_files) == 1
        assert json.loads(record_files[0].read_text())["slot"] == "s1"
        assert not list(tmp_path.rglob("*.json")), "a record landed beside the transcript"

    @pytest.mark.asyncio
    async def test_a_line_rolled_back_whole_to_an_older_write_is_rejected(
        self, tmp_path, monkeypatch
    ) -> None:
        """(a) Red-first on the r10 head. Two hand-offs are queued and written; the
        gateway consumes the first and writes the queue again -- a new generation,
        committed to the store. The editor puts the OLDER line back WHOLE: one
        generation, every seal verifying under it. On r10 the consumed "first"
        drained and ran again. Now the line names a generation other than the
        committed one, so both records are rejected, both dropped with the dashboard
        notice, nobody told, nothing run."""
        from kiro_crew.dashboard import queue_generation_store as store

        state = _make_state(tmp_path)
        slot, first_id, second_id = await self._r7._two_hand_offs_queued(state)
        _save_slot_to_history(state, slot, closed=False)
        older = _meta(state)["queued_prompts"]
        assert [r["id"] for r in older] == [first_id, second_id]
        older_generation = older[0][ORIGIN_GENERATION_KEY]
        assert store.read_committed_generation("s1", _inc(state)) == older_generation
        slot.queue_remove_by_id(first_id)
        _save_slot_to_history(state, slot, closed=False)
        (current,) = _meta(state)["queued_prompts"]
        assert current[ORIGIN_GENERATION_KEY] != older_generation
        assert store.read_committed_generation("s1", _inc(state)) == current[ORIGIN_GENERATION_KEY]

        restored, told, runs = await self._rolled_back(state, monkeypatch, older)

        assert restored._queue == [], "a consumed hand-off was restored from a rolled-back line"
        assert restored._origin_proofs == {}
        assert runs == [] and told == []
        notices = self._notices(restored)
        assert len(notices) == 2
        # Told against a record the store HOLDS for this transcript, the older line
        # is one this gateway did not write last: the tamper wording is the true
        # one here, and the unrecorded wording would understate it.
        for notice in notices:
            assert "its stored record was changed by something other than this gateway" in notice
            assert "kept no record of" not in notice

    @pytest.mark.asyncio
    async def test_a_line_older_than_the_store_is_rejected_whatever_it_carries(
        self, tmp_path, monkeypatch
    ) -> None:
        """The other rollback shapes, for a slot whose store holds a committed
        generation: a line stripped of every seal and generation (which on r9 read
        as a pre-seal line, unproven prose), and a line re-labelled with a
        generation the store does not name. Neither is the line this gateway wrote
        last; both are rejected whole."""
        state = _make_state(tmp_path)
        restored, qid, _admitted = await self._r7._restart_with_a_hand_off_queued(state)
        (record,) = _meta(state)["queued_prompts"]

        stripped = {"id": record["id"], "content": record["content"], "meta": {}}
        restored, told, runs = await self._rolled_back(state, monkeypatch, [stripped])
        assert restored._queue == [] and runs == [] and told == []
        assert len(self._notices(restored)) == 1

        state = _make_state(tmp_path / "again")
        restored, qid, _admitted = await self._r7._restart_with_a_hand_off_queued(state)
        (record,) = _meta(state)["queued_prompts"]
        relabelled = {**record, ORIGIN_GENERATION_KEY: "not-the-committed-one"}
        restored, told, runs = await self._rolled_back(state, monkeypatch, [relabelled])
        assert restored._queue == [] and runs == [] and told == []
        assert len(self._notices(restored)) == 1

    @pytest.mark.asyncio
    async def test_a_line_committed_without_its_generation_is_not_honoured(
        self, tmp_path, monkeypatch
    ) -> None:
        """The first stated cost: the process dies between committing the line and
        committing its generation (here: the record is gone), so the store holds
        nothing for the slot while the line names a write. The requirement cannot
        be met and the line is rejected -- the queue is dropped WITH the notice,
        never honoured on the strength of the line alone."""
        from kiro_crew.dashboard import queue_generation_store as store

        state = _make_state(tmp_path)
        restored, qid, _admitted = await self._r7._restart_with_a_hand_off_queued(state)
        (record,) = _meta(state)["queued_prompts"]
        store._record_path("s1").unlink()
        assert store.read_committed_generation("s1", _inc(state)) is None

        restored, told, runs = await self._rolled_back(state, monkeypatch, [record])

        assert restored._queue == [] and runs == [] and told == []
        assert len(self._notices(restored)) == 1
        assert restored._queue_generation_committed is None

    @pytest.mark.asyncio
    async def test_emptying_the_queue_moves_the_store_off_the_consumed_line(
        self, tmp_path, monkeypatch
    ) -> None:
        """A committed line with entries, then every entry consumed and the empty
        queue committed: the store must not keep naming the generation of the line
        that held the consumed entries, or that line put back whole would match.
        The empty commit moves it to the slot's fresh generation."""
        from kiro_crew.dashboard import queue_generation_store as store

        state = _make_state(tmp_path)
        slot, first_id, second_id = await self._r7._two_hand_offs_queued(state)
        _save_slot_to_history(state, slot, closed=False)
        full_line = _meta(state)["queued_prompts"]
        held = full_line[0][ORIGIN_GENERATION_KEY]
        assert store.read_committed_generation("s1", _inc(state)) == held
        slot.queue_remove_by_id(first_id)
        slot.queue_remove_by_id(second_id)
        _save_slot_to_history(state, slot, closed=False)
        assert _meta(state).get("queued_prompts", []) == []
        moved = store.read_committed_generation("s1", _inc(state))
        assert moved is not None and moved != held
        assert moved == slot._queue_generation

        restored, told, runs = await self._rolled_back(state, monkeypatch, full_line)
        assert restored._queue == [] and runs == [] and told == []
        assert len(self._notices(restored)) == 2

    def test_a_slot_that_never_queued_anything_gets_no_record_and_a_quiet_slot_no_write(
        self, tmp_path, monkeypatch
    ) -> None:
        """The store write is bound to the queue value moving: a slot with nothing
        queued never gets a record (its saves commit an empty list with nothing to
        move off), and a slot whose queue is unchanged between saves does not
        rewrite the record it has."""
        from kiro_crew.dashboard import queue_generation_store as store

        writes: list[tuple[str, str]] = []
        real = store.commit_queue_generation

        def _counting(slot_key: str, generation: str, incarnation: str) -> bool:
            writes.append((slot_key, generation))
            return real(slot_key, generation, incarnation)

        monkeypatch.setattr(store, "commit_queue_generation", _counting)
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.messages.append({"role": "user", "content": "hello", "ts": "t"})
        _save_slot_to_history(state, slot, closed=False)
        _save_slot_to_history(state, slot, closed=False, force=True)
        assert writes == []
        assert not (tmp_path / store.STORE_DIR_LEAF).exists()

        slot.queue_append("later", directive_user_origin=True)
        _save_slot_to_history(state, slot, closed=False)
        assert len(writes) == 1 and writes[0][0] == "s1"
        slot.messages.append({"role": "user", "content": "more", "ts": "t2"})
        _save_slot_to_history(state, slot, closed=False, force=True)
        _save_slot_to_history(state, slot, closed=False, force=True)
        assert len(writes) == 1, "an unchanged queue rewrote its committed generation"

    def test_a_refused_store_write_leaves_the_queue_owed_and_the_next_pass_retries(
        self, tmp_path, monkeypatch
    ) -> None:
        """The line is on disk but the store refused its generation (``OSError`` on
        the fenced leaf, answered as False): the save must NOT credit the queue as
        persisted -- crediting it clears the drift signal with no retry left, and
        the next restart rejects the whole line and drops the acknowledged prompt.
        Owed instead, so the periodic flush re-saves and the store write is
        retried; once it lands the queue is settled and the prompt survives a
        restart. Red with the witness advanced ahead of the commit."""
        from kiro_crew.dashboard import queue_generation_store as store

        real = store.commit_queue_generation
        refused: list[str] = []

        def _refusing(slot_key: str, generation: str, incarnation: str) -> bool:
            refused.append(generation)
            return False

        monkeypatch.setattr(store, "commit_queue_generation", _refusing)
        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("keep me", directive_user_origin=True)
        _save_slot_to_history(state, slot, closed=False)

        (record,) = _meta(state)["queued_prompts"]
        assert refused == [record[ORIGIN_GENERATION_KEY]], "the commit was not attempted"
        assert store.read_committed_generation("s1", _inc(state)) is None
        assert slot._queue_generation_committed is None
        assert slot.queue_persist_pending is True, (
            "a refused generation commit was credited as durable: the flush has no "
            "retry left and a restart drops the line"
        )

        # The store recovers; the next flush pass (drift-driven, no dirty mark
        # needed) re-saves the same queue -- sealed under a fresh per-write
        # generation, as every durable write is -- and the commit lands.
        monkeypatch.setattr(store, "commit_queue_generation", real)
        _save_slot_to_history(state, slot, closed=False)
        (retried,) = _meta(state)["queued_prompts"]
        assert retried["content"] == "keep me"
        assert store.read_committed_generation("s1", _inc(state)) == retried[ORIGIN_GENERATION_KEY]
        assert slot._queue_generation_committed == retried[ORIGIN_GENERATION_KEY]
        assert slot.queue_persist_pending is False

    def test_a_refused_store_write_on_the_empty_window_merge_is_owed_too(
        self, tmp_path, monkeypatch
    ) -> None:
        """The forced empty-window save commits the queue through the metadata
        merge, the second credit site: the same ordering holds there."""
        from kiro_crew.dashboard import queue_generation_store as store

        real = store.commit_queue_generation
        monkeypatch.setattr(store, "commit_queue_generation", lambda *_a: False)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "seed")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        slot.messages.clear()
        slot.queue_append("queued while empty", directive_user_origin=True)

        _save_slot_to_history(state, slot, force=True)

        (record,) = _meta(state)["queued_prompts"]
        assert record["content"] == "queued while empty"
        assert store.read_committed_generation("s1", _inc(state)) is None
        assert slot.queue_persist_pending is True

        monkeypatch.setattr(store, "commit_queue_generation", real)
        _save_slot_to_history(state, slot, force=True)
        (retried,) = _meta(state)["queued_prompts"]
        assert store.read_committed_generation("s1", _inc(state)) == retried[ORIGIN_GENERATION_KEY]
        assert slot.queue_persist_pending is False

    @pytest.mark.asyncio
    async def test_a_refused_move_off_the_consumed_line_is_owed_too(
        self, tmp_path, monkeypatch
    ) -> None:
        """The empty commit that moves the store off a consumed line is a write with
        the same stake (a rollback to that line would match): refused, it leaves the
        emptied queue owed, and the retry moves the store."""
        from kiro_crew.dashboard import queue_generation_store as store

        state = _make_state(tmp_path)
        slot, first_id, second_id = await self._r7._two_hand_offs_queued(state)
        _save_slot_to_history(state, slot, closed=False)
        held = _meta(state)["queued_prompts"][0][ORIGIN_GENERATION_KEY]
        assert store.read_committed_generation("s1", _inc(state)) == held
        slot.queue_remove_by_id(first_id)
        slot.queue_remove_by_id(second_id)

        real = store.commit_queue_generation
        monkeypatch.setattr(store, "commit_queue_generation", lambda *_a: False)
        _save_slot_to_history(state, slot, closed=False)
        assert _meta(state).get("queued_prompts", []) == []
        assert (
            store.read_committed_generation("s1", _inc(state)) == held
        ), "the store moved on a refusal"
        assert slot.queue_persist_pending is True

        monkeypatch.setattr(store, "commit_queue_generation", real)
        _save_slot_to_history(state, slot, closed=False)
        moved = store.read_committed_generation("s1", _inc(state))
        assert moved is not None and moved != held
        assert slot.queue_persist_pending is False

    def test_the_witness_is_credited_only_after_the_store_answers(self) -> None:
        """Structural: at both credit sites the witness advance is conditional on
        the commit's answer, never a statement that precedes it."""
        import inspect

        from kiro_crew.dashboard import chat_persistence as cp

        src = inspect.getsource(cp)
        credits = [
            i for i, line in enumerate(src.splitlines()) if "slot._queue_persisted_sig = " in line
        ]
        # The two save credit sites; the restore seeds are assignments from the
        # line just read and are excluded by the helper's name.
        lines = src.splitlines()
        sites = [i for i in credits if "queue_persist_signature(_" in lines[i]]
        assert len(sites) == 2, sites
        for i in sites:
            # The nearest statement above the credit (comments skipped) is the
            # guard that asked the store; the credit is its body. The guard may
            # wrap, so collect back to its ``if``.
            guard_lines: list[str] = []
            for j in range(i - 1, i - 12, -1):
                if lines[j].strip().startswith("#"):
                    continue
                guard_lines.append(lines[j])
                if lines[j].strip().startswith("if "):
                    break
            guard = "\n".join(guard_lines)
            assert (
                "_commit_queue_generation(" in guard
            ), f"line {i + 1}: the witness is advanced without the commit's answer"
            assert "_commit_queue_generation(" not in "\n".join(
                lines[i + 1 : i + 3]
            ), f"line {i + 1}: the commit runs after the witness was already advanced"

    async def _consumed_then_copied(self, state) -> tuple[list[dict], Any, str]:
        """A sealed hand-off queued and committed (record written), then consumed --
        and the transcript copied before anything else moves. Returns the copied
        line, the live slot and the transcript's incarnation."""
        from kiro_crew.dashboard import queue_generation_store as store

        restored, qid, _admitted = await self._r7._restart_with_a_hand_off_queued(state)
        line = _meta(state)["queued_prompts"]
        incarnation = _inc(state)
        assert store.read_committed_generation("s1", incarnation) == line[0][ORIGIN_GENERATION_KEY]
        # The drain consumes it -- before the queue rotates on the next save, so
        # the store still names the line's generation.
        restored.queue_remove_by_id(qid)
        return line, restored, incarnation

    def _restored_from_a_copy(self, state, line: list[dict], incarnation: str) -> None:
        """Put the copied transcript back under the reused key, as a backup
        restore would: the old line with the OLD ``created_at``."""
        path = state.conversation_log._path("dashboard:s1")
        meta = dict(state.conversation_log._read_metadata("dashboard:s1") or {})
        meta.update({"_type": "metadata", "queued_prompts": line, "created_at": incarnation})
        rows = [json.dumps(meta)]
        rows.append(json.dumps({"role": "user", "content": "hello", "ts": "t"}))
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_permanent_delete_tombstones_the_record_so_a_copy_cannot_replay(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """GPT's sequence: a consumed sealed command, a permanent delete before the
        queue rotates, the slot key reused, the deleted transcript put back from a
        copy. Red with a record that outlives its transcript: the copy's line names
        the generation the store still holds, every seal verifies, and the consumed
        command comes back to the queue. The delete tombstones the record first, so
        the copy's line is a sealed line with no record -- dropped whole, as the
        unrecorded write it is."""
        from kiro_crew.dashboard import queue_generation_store as store
        from kiro_crew.dashboard.handlers.sessions import (
            _capture_history_delete_claim,
            _delete_history_session,
        )

        state = _make_state(tmp_path)
        line, live, incarnation = await self._consumed_then_copied(state)
        state.sessions.destroy_if = AsyncMock(return_value=True)
        claim = _capture_history_delete_claim(state, "dashboard:s1")
        deleted, _claim = _delete_history_session(state.conversation_log, "dashboard:s1", claim)
        assert deleted is True
        assert not store._record_path("s1").exists(), "the record outlived its transcript"
        del state._slots["s1"]
        # The key is reused (a fresh slot, a fresh ``created_at``), then the deleted
        # transcript is put back from a copy; ``_rolled_back`` loses the process.
        assert state.get_or_create_slot("s1").created_at != incarnation
        self._restored_from_a_copy(state, line, incarnation)

        with caplog.at_level(logging.INFO, logger="kiro_crew.dashboard.slot_queue_repository"):
            restored, told, runs = await self._rolled_back(state, monkeypatch, line)
        assert restored._queue == [] and runs == [] and told == []
        (notice,) = self._notices(restored)
        # Nothing on the copy was altered: the line is dropped as the expected
        # state it is (Opus r17), never under the tamper wording.
        self._assert_unrecorded_not_tampered(notice)
        assert any(
            "kept no record of" in r.getMessage()
            and "no record: a save cut short" in r.getMessage()
            and r.levelno == logging.INFO
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def _assert_unrecorded_not_tampered(self, notice: str) -> None:
        assert (
            "this gateway kept no record of the durable write it was restored from after "
            "it was queued, so the authorization that admitted it no longer holds." in notice
        ), notice
        assert "changed by something other than this gateway" not in notice, notice
        assert UNRECORDED_GENERATION_CONSTRAINT not in notice, "the raw constraint name leaked"

    @pytest.mark.asyncio
    async def test_the_record_is_bound_to_the_incarnation_even_without_the_tombstone(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """The transcript removed by hand (no tombstone runs), the key reused and the
        new transcript committing a queue: its record is bound to the NEW
        ``created_at``, so the old transcript put back from a copy finds no record
        for its incarnation and is rejected -- the binding alone closes the reuse
        even when the delete path was not this gateway's."""
        from kiro_crew.dashboard import queue_generation_store as store

        state = _make_state(tmp_path)
        line, live, incarnation = await self._consumed_then_copied(state)
        del state._slots["s1"]
        state.conversation_log._path("dashboard:s1").unlink()
        assert store.read_committed_generation("s1", incarnation) is not None

        fresh = _busy_slot(state)
        assert fresh.created_at != incarnation
        fresh.queue_append("a new session's prompt", directive_user_origin=True)
        _save_slot_to_history(state, fresh, closed=False)
        new_incarnation = _inc(state)
        assert new_incarnation != incarnation
        assert store.read_committed_generation("s1", new_incarnation) not in (
            None,
            store.STALE_INCARNATION,
        )
        assert store.read_committed_generation("s1", incarnation) == store.STALE_INCARNATION
        self._restored_from_a_copy(state, line, incarnation)

        with caplog.at_level(logging.INFO, logger="kiro_crew.dashboard.slot_queue_repository"):
            restored, told, runs = await self._rolled_back(state, monkeypatch, line)
        assert restored._queue == [] and runs == [] and told == []
        (notice,) = self._notices(restored)
        self._assert_unrecorded_not_tampered(notice)
        # The stale answer is told apart from "no record" in the log, at info: it
        # is the expected shape of a copy under a reused key, not an error.
        assert any(
            "kept no record of" in r.getMessage()
            and "names another incarnation of the transcript" in r.getMessage()
            and r.levelno == logging.INFO
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    @pytest.mark.asyncio
    async def test_a_restored_transcript_retires_a_stale_record_on_its_next_save(
        self, tmp_path, monkeypatch
    ) -> None:
        """The residual the binding alone leaves: the old transcript removed by hand,
        the new one under the reused key never queuing anything -- so no save ever
        rebinds the record -- and then the old transcript put back from a copy. The
        restore of the new transcript sees a record for another incarnation
        (``STALE_INCARNATION``); it honours nothing under it AND seeds the witness,
        so the next save, empty queue included, commits a fresh record over it."""
        from kiro_crew.dashboard import queue_generation_store as store

        state = _make_state(tmp_path)
        line, live, incarnation = await self._consumed_then_copied(state)
        del state._slots["s1"]
        state.conversation_log._path("dashboard:s1").unlink()

        fresh = _busy_slot(state)
        fresh.messages.append({"role": "user", "content": "a quiet new session", "ts": "t"})
        _save_slot_to_history(state, fresh, closed=False)
        new_incarnation = _inc(state)
        assert new_incarnation != incarnation
        assert (
            store.read_committed_generation("s1", new_incarnation) == store.STALE_INCARNATION
        ), "a never-queuing session left the old record standing"
        del state._slots["s1"]

        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        assert restored._queue == []
        assert restored._queue_generation_committed == store.STALE_INCARNATION
        _save_slot_to_history(state, restored, closed=False, force=True)
        fresh_record = store.read_committed_generation("s1", new_incarnation)
        assert fresh_record not in (None, store.STALE_INCARNATION)
        assert store.read_committed_generation("s1", incarnation) == store.STALE_INCARNATION

        self._restored_from_a_copy(state, line, incarnation)
        restored, told, runs = await self._rolled_back(state, monkeypatch, line)
        assert restored._queue == [] and runs == [] and told == []
        (notice,) = self._notices(restored)
        self._assert_unrecorded_not_tampered(notice)

    def test_a_tombstone_that_cannot_be_written_refuses_the_delete_with_the_row_intact(
        self, tmp_path, monkeypatch
    ) -> None:
        import os

        from kiro_crew.dashboard import queue_generation_store as store
        from kiro_crew.dashboard.handlers.sessions import (
            _capture_history_delete_claim,
            _delete_history_session,
        )

        state = _make_state(tmp_path)
        slot = _busy_slot(state)
        slot.queue_append("keep me", directive_user_origin=True)
        _save_slot_to_history(state, slot, closed=False)
        assert store._record_path("s1").exists()
        claim = _capture_history_delete_claim(state, "dashboard:s1")
        excluded: list[str] = []

        def _refuse(src: Any, dst: Any) -> None:
            raise PermissionError("read-only leaf")

        monkeypatch.setattr(os, "replace", _refuse)
        with pytest.raises(store.QueueGenerationTombstoneError):
            _delete_history_session(
                state.conversation_log,
                "dashboard:s1",
                claim,
                exclude=lambda registry_key: excluded.append(registry_key),
            )
        monkeypatch.undo()
        assert excluded == [], "the ledger exclusion was written before the tombstone"
        assert state.conversation_log.has_log("dashboard:s1"), "the row was not left intact"
        assert store.read_committed_generation("s1", _inc(state)) is not None

    async def _sealed_hand_off_committed(self, state) -> tuple[list[dict], str, str]:
        """A hand-off queued behind a dashboard turn and committed -- the line
        sealed, the record written -- then the process lost. Returns the persisted
        line, the queue id and the incarnation; the slot is gone from the state."""
        from kiro_crew.dashboard import queue_generation_store as store

        restored, qid, _admitted = await self._r7._restart_with_a_hand_off_queued(state)
        line = _meta(state)["queued_prompts"]
        assert store.read_committed_generation("s1", _inc(state)) == line[0][ORIGIN_GENERATION_KEY]
        del state._slots["s1"]
        return line, qid, _inc(state)

    def _assert_the_queue_survived(self, state, monkeypatch, qid: str) -> None:
        """The transcript that survived its refused delete restores its hand-off
        proven, as if no delete had been attempted: no notice, nothing dropped."""
        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard import queue_generation_store as store
        from kiro_crew.dashboard.channel_busy import CHANNEL_ORIGIN_META_KEY

        assert state.conversation_log.has_log("dashboard:s1"), "the transcript was deleted"
        line = _meta(state)["queued_prompts"]
        assert store.read_committed_generation("s1", _inc(state)) == line[0][ORIGIN_GENERATION_KEY]
        store_dir = store._record_path("s1").parent
        assert [p.name for p in store_dir.iterdir()] == [store._record_path("s1").name]
        restored = _rehydrate_slot_from_history(state, "s1")
        assert restored is not None
        told = self._r7._watch_the_notice(monkeypatch)
        cr._drop_stale_admissions(state, restored)
        (entry,) = restored._queue
        assert entry["id"] == qid
        assert entry["meta"][CHANNEL_ORIGIN_META_KEY]["channel_id"] == "c1"
        assert set(restored._origin_proofs) == {qid}
        assert told == [] and not self._notices(restored), "the surviving queue was dropped"

    @pytest.mark.asyncio
    async def test_a_delete_refused_after_the_tombstone_leaves_the_queue_intact(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT (sessions.py:2483), red-first: the record was tombstoned, then the
        transcript's own unlink refused (a search-index row it cannot drop, an
        attachments directory it cannot move -- ``delete_session`` answers False).
        On the r14 head the surviving transcript's sealed line then had no record:
        the restart rejected every record and dropped the hand-off with the notice.
        The tombstone is now moved back the moment the delete refuses."""
        from kiro_crew.dashboard.handlers.sessions import (
            _capture_history_delete_claim,
            _delete_history_session,
        )

        state = _make_state(tmp_path)
        line, qid, incarnation = await self._sealed_hand_off_committed(state)
        log = state.conversation_log
        excluded: list[str] = []
        monkeypatch.setattr(type(log), "delete_session", lambda self_, key, **kw: False)
        claim = _capture_history_delete_claim(state, "dashboard:s1")
        deleted, _claim = _delete_history_session(
            log,
            "dashboard:s1",
            claim,
            exclude=lambda registry_key: excluded.append(registry_key)
            or SimpleNamespace(added=(), carry_tombstoned=False),
        )
        monkeypatch.undo()
        assert deleted is False
        self._assert_the_queue_survived(state, monkeypatch, qid)

    @pytest.mark.asyncio
    async def test_an_exclusion_that_refuses_after_the_tombstone_leaves_the_queue_intact(
        self, tmp_path, monkeypatch
    ) -> None:
        """The ledger exclusion -- the step between the tombstone and the unlink --
        refuses: the error still propagates (the callers answer 409 / the
        undeletable row), and the record is back before it does."""
        from kiro_crew.dashboard.handlers.sessions import (
            _delete_history_session,
            _HistoryDeleteClaim,
        )

        class _ExclusionRefused(Exception):
            pass

        state = _make_state(tmp_path)
        line, qid, incarnation = await self._sealed_hand_off_committed(state)
        # A claim already resolved to its slot (``slot=None`` is returned as-is by the
        # resolver), complete and path-verified: the shape the exclusion runs for.
        claim = _HistoryDeleteClaim(
            registry_key="s1",
            slot=None,
            history_key="dashboard:s1",
            session_key="s1",
            session_generation=1,
            path_match_verified=True,
            complete=True,
        )
        reached: list[str] = []

        def _refusing_exclude(registry_key: str) -> None:
            reached.append(registry_key)
            raise _ExclusionRefused(registry_key)

        with pytest.raises(_ExclusionRefused):
            _delete_history_session(
                state.conversation_log, "dashboard:s1", claim, exclude=_refusing_exclude
            )
        assert reached == ["s1"]
        self._assert_the_queue_survived(state, monkeypatch, qid)

    @pytest.mark.asyncio
    async def test_a_pinned_row_skipped_by_the_bulk_clear_keeps_its_record(
        self, tmp_path, monkeypatch
    ) -> None:
        """``delete_session(skip_pinned=True)`` answers None for a pinned row: not a
        refusal by failure, but a transcript that survives all the same."""
        from kiro_crew.dashboard.handlers.sessions import (
            _capture_history_delete_claim,
            _delete_history_session,
        )

        state = _make_state(tmp_path)
        line, qid, incarnation = await self._sealed_hand_off_committed(state)
        state.conversation_log.update_metadata("dashboard:s1", {"pinned": True})
        claim = _capture_history_delete_claim(state, "dashboard:s1")
        deleted, _claim = _delete_history_session(
            state.conversation_log, "dashboard:s1", claim, skip_pinned=True
        )
        assert deleted is None
        self._assert_the_queue_survived(state, monkeypatch, qid)

    @pytest.mark.asyncio
    async def test_a_completed_delete_leaves_neither_record_nor_copy_aside(self, tmp_path) -> None:
        """The irreversible half runs only once the transcript is gone, and it
        leaves the fenced directory empty: no record, no ``.deleting-`` copy."""
        from kiro_crew.dashboard import queue_generation_store as store
        from kiro_crew.dashboard.handlers.sessions import (
            _capture_history_delete_claim,
            _delete_history_session,
        )

        state = _make_state(tmp_path)
        line, qid, incarnation = await self._sealed_hand_off_committed(state)
        state.sessions.destroy_if = AsyncMock(return_value=True)
        claim = _capture_history_delete_claim(state, "dashboard:s1")
        deleted, _claim = _delete_history_session(state.conversation_log, "dashboard:s1", claim)
        assert deleted is True
        assert not state.conversation_log.has_log("dashboard:s1")
        assert list(store._record_path("s1").parent.iterdir()) == []
        assert store.read_committed_generation("s1", incarnation) is None

    def test_the_pure_verifier_requires_the_committed_generation(self) -> None:
        """The verifier alone, every reading of the requirement on one slot key."""

        def _sealed(generation: str, qid: str, text: str) -> dict:
            return {
                "id": qid,
                "content": text,
                "meta": {QUEUED_CONTAINMENT_META_KEY: {"mirrored": True}},
                ORIGIN_PROOF_KEY: queue_record_seal(
                    "s1", generation, qid, text, channel_address=None, admission={"mirrored": True}
                ),
                ORIGIN_GENERATION_KEY: generation,
            }

        def _read(line: list[dict], committed: str | None) -> Any:
            owner = SimpleNamespace(key="s1")
            entries = sanitize_restored_queue(line)
            restore_queue_provenance(owner, entries, line, committed_generation=committed)
            return owner

        line = [_sealed("gen-1", "q1", "one"), _sealed("gen-1", "q2", "two")]
        # Named: proven.
        owner = _read(line, "gen-1")
        assert set(owner._origin_proofs) == {"q1", "q2"}
        assert getattr(owner, "_rejected_provenance", set()) == set()
        assert getattr(owner, "_unrecorded_provenance", set()) == set()
        # An older or a newer committed generation: the line is not the write this
        # gateway committed last for the transcript -- every record REJECTED.
        for committed in ("gen-0", "gen-2"):
            owner = _read(line, committed)
            assert getattr(owner, "_origin_proofs", {}) == {}, committed
            assert owner._rejected_provenance == {"q1", "q2"}, committed
            assert getattr(owner, "_unrecorded_provenance", set()) == set(), committed
        # No record for this transcript -- none at all, or one for another
        # incarnation of it: nothing verified false, nothing to honour. Every
        # record UNRECORDED, none rejected (Opus r17: the tamper notice was a
        # false claim for a transcript put back from a copy).
        for committed in (None, STALE_INCARNATION):
            owner = _read(line, committed)
            assert getattr(owner, "_origin_proofs", {}) == {}, committed
            assert getattr(owner, "_rejected_provenance", set()) == set(), committed
            assert owner._unrecorded_provenance == {"q1", "q2"}, committed
        # A generation-less line for a slot with a committed generation: rejected.
        owner = _read([{"id": "q1", "content": "/compact"}], "gen-1")
        assert owner._rejected_provenance == {"q1"}
        # The same line for a slot with none, or with a stale record: the pre-seal
        # reading, unproven prose -- neither mark.
        for committed in (None, STALE_INCARNATION):
            owner = _read([{"id": "q1", "content": "/compact"}], committed)
            assert getattr(owner, "_rejected_provenance", set()) == set(), committed
            assert getattr(owner, "_unrecorded_provenance", set()) == set(), committed
            assert getattr(owner, "_origin_proofs", {}) == {}, committed

    def test_the_provenance_store_has_one_writer(self) -> None:
        """The bound (``MAX_ORIGIN_PROOFS``) is a property of the store, not of one
        call site: ``_record_provenance`` is the only code that puts an attestation
        or a rejection into a slot's sidecars, and nothing outside the repository
        module writes them at all. Structural, over the source: a second writer
        would re-open the growth GPT blocked twice (r9 ``:288``, r10 ``:915``)."""
        import inspect
        import re
        from pathlib import Path

        from kiro_crew.dashboard import slot_queue_repository as repo

        module_src = inspect.getsource(repo)
        writer_src = inspect.getsource(repo._record_provenance)
        attestation_writes = re.compile(
            r"^\s*(proofs|_origin_proofs_of\([^)]*\))\[.+?\]\s*=(?!=)", re.MULTILINE
        )
        rejection_writes = re.compile(
            r"\b(rejected|rejections|_rejected_provenance_of\([^)]*\))\.(add|update)\("
        )
        unrecorded_writes = re.compile(
            r"\b(unrecorded|unrecorded_ids|_unrecorded_provenance_of\([^)]*\))\.(add|update)\("
        )
        assert len(attestation_writes.findall(module_src)) == len(
            attestation_writes.findall(writer_src)
        ), "an attestation is written outside _record_provenance"
        assert len(rejection_writes.findall(module_src)) == len(
            rejection_writes.findall(writer_src)
        ), "a rejection is written outside _record_provenance"
        assert len(unrecorded_writes.findall(module_src)) == len(
            unrecorded_writes.findall(writer_src)
        ), "an unrecorded mark is written outside _record_provenance"
        assert attestation_writes.search(writer_src) and rejection_writes.search(writer_src)
        assert unrecorded_writes.search(writer_src)

        src_root = Path(repo.__file__).resolve().parents[2]
        foreign_writes = re.compile(
            r"_origin_proofs\s*\[.+?\]\s*=(?!=)|_origin_proofs\.(update|setdefault)\("
            r"|_rejected_provenance\.(add|update)\("
            r"|_unrecorded_provenance\.(add|update)\("
        )
        offenders = [
            str(path.relative_to(src_root))
            for path in src_root.rglob("*.py")
            if path.name != "slot_queue_repository.py"
            and foreign_writes.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], offenders

    def test_the_unrecorded_mark_lives_and_dies_with_the_other_two(self) -> None:
        """The third mark is one more value of the same store, not a second store:
        exclusive with a proof and a rejection for one id, pruned to the window,
        forgotten as the entry leaves, and cleared by the re-stamp an edit runs.
        Red-first: with the writer's ``unrecorded`` branch or ``forget_provenance``'s
        third discard removed, one of these fails."""
        from types import SimpleNamespace

        from kiro_crew.dashboard.slot_queue_repository import _record_provenance

        owner = SimpleNamespace(key="s1")
        window = {"a", "b"}
        _record_provenance(owner, "a", window=window, unrecorded=True)
        assert owner._unrecorded_provenance == {"a"}
        assert provenance_unrecorded(owner, {"id": "a"}) and not provenance_rejected(
            owner, {"id": "a"}
        )
        # Exclusive: a later proof or rejection for the same id replaces the mark.
        _record_provenance(owner, "a", window=window, proof="p")
        assert owner._unrecorded_provenance == set() and set(owner._origin_proofs) == {"a"}
        _record_provenance(owner, "a", window=window, rejected=True)
        assert owner._unrecorded_provenance == set() and owner._rejected_provenance == {"a"}
        # A rejection asked together with the mark is a rejection: present-and-false
        # outranks nothing-to-honour.
        _record_provenance(owner, "b", window=window, rejected=True, unrecorded=True)
        assert owner._rejected_provenance == {"a", "b"} and owner._unrecorded_provenance == set()
        # Pruned to the window like the rest, and never written outside it.
        _record_provenance(owner, "c", window={"c"}, unrecorded=True)
        assert owner._unrecorded_provenance == {"c"} and owner._rejected_provenance == set()
        _record_provenance(owner, "d", window={"c"}, unrecorded=True)
        assert owner._unrecorded_provenance == {"c"}
        # Forgotten as the entry leaves.
        forget_provenance(owner, "c")
        assert owner._unrecorded_provenance == set()
        assert not provenance_unrecorded(owner, {"id": "c"})
        assert provenance_unrecorded(owner, None) is False
        assert provenance_unrecorded(SimpleNamespace(), {"id": "c"}) is False


class TestTheQueueGenerationStore:
    """The fenced record itself: round trip, the readings that are 'no record', the
    binding to one transcript incarnation, the tombstone, and the leaf's
    registration in the sandbox mask, the precreate list and the file-tool fence --
    the three lists that make it the gateway's alone."""

    def test_round_trip_and_the_absent_record(self, tmp_path) -> None:
        from kiro_crew.dashboard import queue_generation_store as store

        assert store.read_committed_generation("s1", "inc-1") is None
        assert store.commit_queue_generation("s1", "gen-1", "inc-1") is True
        assert store.read_committed_generation("s1", "inc-1") == "gen-1"
        assert (
            store.read_committed_generation("s2", "inc-1") is None
        ), "one slot's record read for another"
        assert store.commit_queue_generation("s1", "gen-2", "inc-1") is True
        assert store.read_committed_generation("s1", "inc-1") == "gen-2"
        assert store.commit_queue_generation("", "gen-3", "inc-1") is False
        assert store.commit_queue_generation("s1", "", "inc-1") is False
        assert (
            store.commit_queue_generation("s1", "gen-3", "") is False
        ), "a line naming no incarnation has nothing for the record to bind to"
        assert store.read_committed_generation("", "inc-1") is None
        assert store.read_committed_generation("s1", "") is None

    def test_a_record_is_good_for_one_transcript_incarnation(self, tmp_path) -> None:
        """The slot key is reused: a transcript deleted and another opened under the
        same key carries a new ``created_at``. The record committed for the old
        incarnation must not answer for the new one, and once the new incarnation
        commits, the record must not answer for the OLD line put back from a copy."""
        from kiro_crew.dashboard import queue_generation_store as store

        assert store.commit_queue_generation("s1", "gen-old", "inc-old") is True
        assert store.read_committed_generation("s1", "inc-old") == "gen-old"
        # Another incarnation reads the record as STALE: no line names that answer,
        # and the restore seeds its witness from it so the next save retires it.
        assert store.read_committed_generation("s1", "inc-new") == store.STALE_INCARNATION
        assert store.commit_queue_generation("s1", "gen-new", "inc-new") is True
        assert store.read_committed_generation("s1", "inc-new") == "gen-new"
        assert (
            store.read_committed_generation("s1", "inc-old") == store.STALE_INCARNATION
        ), "the old incarnation's line still verified after the key was reused"

    def test_the_tombstone_moves_the_record_aside_and_refuses_when_it_cannot(
        self, tmp_path, monkeypatch
    ) -> None:
        import os

        from kiro_crew.dashboard import queue_generation_store as store

        store_dir = store._record_path("s1").parent
        assert store.stage_queue_generation_tombstone(["s1"]).slot_keys == ()
        assert store.commit_queue_generation("s1", "gen-1", "inc-1") is True
        # Staged: gone from where the restore reads, still in the fenced directory.
        staged = store.stage_queue_generation_tombstone(["s1", "", "s1", "s9"])
        assert staged.slot_keys == ("s1",)
        assert store.read_committed_generation("s1", "inc-1") is None
        assert not store._record_path("s1").exists()
        (aside,) = [p for p in store_dir.iterdir() if ".deleting-" in p.name]
        assert aside.name.startswith(store._record_path("s1").name)
        # Rolled back: the record answers again, nothing left aside.
        assert staged.rollback() is True
        assert store.read_committed_generation("s1", "inc-1") == "gen-1"
        assert [p.name for p in store_dir.iterdir()] == [store._record_path("s1").name]
        assert staged.slot_keys == ()
        staged.rollback()
        staged.purge()
        assert store.read_committed_generation("s1", "inc-1") == "gen-1"
        # Purged: the irreversible half leaves nothing in the directory.
        staged = store.stage_queue_generation_tombstone(["s1"])
        staged.purge()
        assert list(store_dir.iterdir()) == []
        assert store.read_committed_generation("s1", "inc-1") is None
        staged.purge()
        assert staged.rollback() is True

        assert store.commit_queue_generation("s1", "gen-1", "inc-1") is True

        def _refuse(src: Any, dst: Any) -> None:
            raise PermissionError("read-only leaf")

        monkeypatch.setattr(os, "replace", _refuse)
        with pytest.raises(store.QueueGenerationTombstoneError) as caught:
            store.stage_queue_generation_tombstone(["s1"])
        assert isinstance(caught.value, OSError)
        assert str(tmp_path) not in str(caught.value), "the refusal carries a path"
        monkeypatch.undo()
        assert store.read_committed_generation("s1", "inc-1") == "gen-1", "the record moved"

    def test_a_refused_move_puts_the_records_already_moved_back(
        self, tmp_path, monkeypatch
    ) -> None:
        """Two slot spellings, the second's move refused: the first is back before
        the error reaches the caller, so 'nothing written' holds for the row."""
        import os

        from kiro_crew.dashboard import queue_generation_store as store

        assert store.commit_queue_generation("s1", "gen-1", "inc-1") is True
        assert store.commit_queue_generation("s2", "gen-2", "inc-2") is True
        real_replace = os.replace

        def _refuse_second(src: Any, dst: Any) -> None:
            if str(src) == str(store._record_path("s2")):
                raise PermissionError("read-only leaf")
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _refuse_second)
        with pytest.raises(store.QueueGenerationTombstoneError):
            store.stage_queue_generation_tombstone(["s1", "s2"])
        monkeypatch.undo()
        assert store.read_committed_generation("s1", "inc-1") == "gen-1"
        assert store.read_committed_generation("s2", "inc-2") == "gen-2"
        assert [
            p.name for p in store._record_path("s1").parent.iterdir() if ".deleting-" in p.name
        ] == []

    def test_a_rollback_never_moves_the_committed_generation_backwards(self, tmp_path) -> None:
        """A save committed a fresh record while the delete was refusing: the
        rollback keeps the newer record and drops the copy aside."""
        from kiro_crew.dashboard import queue_generation_store as store

        assert store.commit_queue_generation("s1", "gen-1", "inc-1") is True
        staged = store.stage_queue_generation_tombstone(["s1"])
        assert store.commit_queue_generation("s1", "gen-2", "inc-1") is True
        assert staged.rollback() is True
        assert store.read_committed_generation("s1", "inc-1") == "gen-2"
        assert [p.name for p in store._record_path("s1").parent.iterdir()] == [
            store._record_path("s1").name
        ]

    def test_a_rollback_that_cannot_put_the_record_back_says_so(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        import os

        from kiro_crew.dashboard import queue_generation_store as store

        assert store.commit_queue_generation("s1", "gen-1", "inc-1") is True
        staged = store.stage_queue_generation_tombstone(["s1"])
        (aside,) = [p for p in store._record_path("s1").parent.iterdir()]

        def _refuse(src: Any, dst: Any) -> None:
            raise PermissionError("directory turned read-only")

        monkeypatch.setattr(os, "replace", _refuse)
        with caplog.at_level(logging.ERROR, logger=store.__name__):
            assert staged.rollback() is False
        monkeypatch.undo()
        (record,) = [r for r in caplog.records if "could not be put back" in r.getMessage()]
        assert record.levelno == logging.ERROR
        assert "s1" in record.getMessage()
        assert (
            str(aside) in record.getMessage()
            and str(store._record_path("s1")) in record.getMessage()
        )
        assert "NOT deleted" in record.getMessage()
        # Still aside: a second rollback once the directory is writable again works.
        assert staged.slot_keys == ("s1",)
        assert staged.rollback() is True
        assert store.read_committed_generation("s1", "inc-1") == "gen-1"

    def test_a_refused_write_names_the_slot_and_the_path(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """Opus: the docstring promises a warning naming the slot and the PATH; the
        operator on a full disk or read-only data home is directed to the file."""
        from kiro_crew.dashboard import queue_generation_store as store

        def _full(*args: Any, **kwargs: Any) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(store, "atomic_write", _full)
        with caplog.at_level(logging.WARNING, logger=store.__name__):
            assert store.commit_queue_generation("s1", "gen-1", "inc-1") is False
        (record,) = [r for r in caplog.records if "could not be recorded" in r.getMessage()]
        assert "s1" in record.getMessage()
        assert str(store._record_path("s1")) in record.getMessage()
        assert "No space left on device" in record.getMessage()

    def test_a_record_this_module_did_not_write_is_no_record(self, tmp_path) -> None:
        from kiro_crew.dashboard import queue_generation_store as store

        assert store.commit_queue_generation("s1", "gen-1", "inc-1") is True
        path = store._record_path("s1")
        for payload in (
            "not json",
            json.dumps([]),
            json.dumps({"v": 3, "slot": "s1", "incarnation": "inc-1", "generation": "gen-9"}),
            # A version-1 record is bound to the slot key alone: absent, not trusted.
            json.dumps({"v": 1, "slot": "s1", "generation": "gen-9"}),
            json.dumps({"v": 2, "slot": "s2", "incarnation": "inc-1", "generation": "gen-9"}),
            json.dumps({"v": 2, "slot": "s1", "generation": "gen-9"}),
            json.dumps({"v": 2, "slot": "s1", "incarnation": "inc-1", "generation": ""}),
            json.dumps({"v": 2, "slot": "s1", "incarnation": "inc-1", "generation": 7}),
            json.dumps({"v": 2, "slot": "s1", "incarnation": "inc-1", "generation": "x" * 5000}),
        ):
            path.write_text(payload)
            assert store.read_committed_generation("s1", "inc-1") is None, payload[:40]
        path.unlink()
        path.mkdir()
        assert store.read_committed_generation("s1", "inc-1") is None

    def test_the_leaf_is_masked_precreated_and_fenced(self) -> None:
        from kiro_crew import sandbox
        from kiro_crew.dashboard import queue_generation_store as store
        from kiro_crew.security import paths as security_paths

        assert store.STORE_DIR_LEAF in sandbox._CREW_HIDDEN_LEAVES
        assert store.STORE_DIR_LEAF in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert store.STORE_DIR_LEAF in security_paths._CREW_SECRET_LEAVES
        assert "/" not in store.STORE_DIR_LEAF, "a mask covers the leaf, not its ancestors"


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
