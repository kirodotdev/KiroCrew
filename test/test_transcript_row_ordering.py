"""One transcript's rows must sort in the order they were written.

A turn writes the message a person sent and the reply to it microseconds apart.
Where the system clock ticks coarsely -- Windows advances it in ~15.6 ms steps --
both writes read the same instant and the two rows land carrying an identical
``ts``. Ordering by ``ts`` is then ambiguous and the dashboard can show the
answer above the question, which is the defect the question-before-answer work
set out to remove.

The writer tests run under ``windows_sim.colliding_clock``, the repo's simulator
for exactly that condition: every ``datetime.now()`` in the module under test
returns one instant, so nothing but the write order can separate the rows. They
fail before the change on every platform, including the Linux CI where the real
clock happens to be fine.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from chat_test_helpers import _make_state
from windows_sim import colliding_clock

from kiro_crew.history import (
    ConversationLog,
    _parse_transcript_ts,
    monotonic_transcript_ts,
    transcript_sort_key,
)

_INSTANT = datetime(2026, 8, 5, 17, 54, 8, 435769)


def _sorts_strictly_ascending(rows: list[dict]) -> bool:
    keys = [transcript_sort_key(r["ts"]) for r in rows]
    return all(a < b for a, b in zip(keys, keys[1:]))


class TestTheStamperItself:
    """``monotonic_transcript_ts`` only ever moves a row forward."""

    def test_an_advancing_clock_is_left_alone(self):
        later = _INSTANT.replace(microsecond=999999)

        stamped = monotonic_transcript_ts(_INSTANT.isoformat(), later)

        # Same instant, not the same text: the stamp now carries its offset.
        assert _parse_transcript_ts(stamped) == later.astimezone()

    def test_a_stalled_clock_still_advances_the_row(self):
        stamped = monotonic_transcript_ts(_INSTANT.isoformat(), _INSTANT)

        assert transcript_sort_key(stamped) > transcript_sort_key(_INSTANT.isoformat())

    def test_a_clock_that_went_backwards_still_advances_the_row(self):
        # NTP correction, or a resumed VM: the row must not sort before the one
        # it followed just because the clock moved the other way.
        earlier = _INSTANT.replace(microsecond=1)

        stamped = monotonic_transcript_ts(_INSTANT.isoformat(), earlier)

        assert transcript_sort_key(stamped) > transcript_sort_key(_INSTANT.isoformat())

    @pytest.mark.parametrize("previous", [None, "", "not a timestamp"])
    def test_nothing_to_order_against_yields_the_clock(self, previous):
        assert _parse_transcript_ts(monotonic_transcript_ts(previous, _INSTANT)) == (
            _INSTANT.astimezone()
        )

    def test_the_two_stored_formats_are_compared_in_one_domain(self):
        # The dashboard writes offset-aware values and the channel path writes
        # naive local ones into the SAME file. Comparing them as text orders
        # them by their spelling, so each has to be read the way its writer
        # meant it -- the reading transcript_sort_key already uses.
        aware_previous = _INSTANT.astimezone(timezone.utc).isoformat()

        stamped = monotonic_transcript_ts(aware_previous, _INSTANT)

        assert transcript_sort_key(stamped) > transcript_sort_key(aware_previous)

    def test_a_naive_previous_orders_an_aware_row(self):
        stamped = monotonic_transcript_ts(_INSTANT.isoformat(), _INSTANT.astimezone(timezone.utc))

        assert transcript_sort_key(stamped) > transcript_sort_key(_INSTANT.isoformat())

    def test_the_row_after_a_dst_fold_row_does_not_sort_an_hour_early(self):
        # When daylight saving ends the local wall clock repeats for an hour, and
        # isoformat does not record which pass a naive value belongs to. 01:30
        # local on 2026-11-01 in Los Angeles happens twice: at 08:30 UTC and
        # again at 09:30 UTC. A row stamped as a bare wall clock during the
        # SECOND pass reads back as the first -- an hour before the offset-aware
        # row it actually followed. Resolving the clock to an instant removes it.
        second_pass = datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc).astimezone(
            ZoneInfo("America/Los_Angeles")
        )
        previous = second_pass.isoformat()

        stamped = monotonic_transcript_ts(previous, second_pass.replace(tzinfo=None))

        assert transcript_sort_key(stamped) > transcript_sort_key(previous)

    def test_the_stamp_always_carries_an_offset(self):
        # A value without one is not orderable against the offset-aware rows the
        # dashboard writes into the same file.
        assert _parse_transcript_ts(monotonic_transcript_ts(None, _INSTANT)).tzinfo is not None


class TestTheChannelWriter:
    """``ConversationLog.append`` -- the durable write both Slack rows take."""

    def test_a_turns_two_rows_stay_ordered_on_a_stalled_clock(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)

        with colliding_clock("kiro_crew.history", at=_INSTANT):
            log.append("s1", "user", "the question")
            log.append("s1", "assistant", "the reply")

        rows = log.read_messages("s1")
        assert [r["content"] for r in rows] == ["the question", "the reply"]
        assert _sorts_strictly_ascending(rows)

    def test_a_long_run_of_rows_stays_ordered(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)

        with colliding_clock("kiro_crew.history", at=_INSTANT):
            for i in range(12):
                log.append("s1", "user" if i % 2 == 0 else "assistant", f"m{i}")

        rows = log.read_messages("s1")
        assert [r["content"] for r in rows] == [f"m{i}" for i in range(12)]
        assert _sorts_strictly_ascending(rows)

    def test_a_reply_longer_than_the_tail_window_does_not_hide_its_predecessor(self, tmp_path):
        # The floor comes from a bounded read of the file's tail. A single
        # message bigger than that window would leave the next row with nothing
        # to order against unless the reader grows its window.
        log = ConversationLog(base_dir=tmp_path)

        with colliding_clock("kiro_crew.history", at=_INSTANT):
            log.append("s1", "user", "the question")
            log.append("s1", "assistant", "x" * (ConversationLog._TAIL_MIN_BYTES * 2))
            log.append("s1", "user", "the follow-up")

        assert _sorts_strictly_ascending(log.read_messages("s1"))

    def test_a_row_written_by_another_process_is_still_ordered_against(self, tmp_path):
        # Two log objects on one file stand in for two processes. The floor is
        # read from the file under the cross-process lock, not remembered in
        # memory, so a row this object never wrote still orders the next one.
        a = ConversationLog(base_dir=tmp_path)
        b = ConversationLog(base_dir=tmp_path)

        with colliding_clock("kiro_crew.history", at=_INSTANT):
            b.append("s1", "user", "b first")
            a.append("s1", "user", "a second")
            b.append("s1", "user", "b third")

        rows = a.read_messages("s1")
        assert [r["content"] for r in rows] == ["b first", "a second", "b third"]
        assert _sorts_strictly_ascending(rows)

    def test_a_brand_new_session_does_not_read_the_file_it_just_created(self, tmp_path):
        # Several Slack command handlers call append inline on the event loop, so
        # the first message of a session should not pay a read to discover what
        # this call already knows: the file it just created holds no rows.
        log = ConversationLog(base_dir=tmp_path)
        reads: list[str] = []
        real = log._read_tail_messages

        def counting(path, max_messages, roles):
            reads.append(str(path))
            return real(path, max_messages, roles)

        log._read_tail_messages = counting  # type: ignore[method-assign]

        with colliding_clock("kiro_crew.history", at=_INSTANT):
            log.append("s1", "user", "the first message")

        assert reads == []

    def test_a_rewritten_file_is_still_ordered_against(self, tmp_path):
        # Rotation, consolidation and the slot save all rewrite the file. The
        # floor is read at stamp time rather than remembered, so a row that
        # arrived by a rewrite still orders the next append.
        log = ConversationLog(base_dir=tmp_path)
        with colliding_clock("kiro_crew.history", at=_INSTANT):
            log.append("s1", "user", "first")

        path = log._path("s1")
        kept = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        later = monotonic_transcript_ts(None, _INSTANT.astimezone() + timedelta(hours=1))
        rewritten = json.loads(kept[-1])
        rewritten["ts"] = later
        path.write_text("\n".join([kept[0], json.dumps(rewritten)]) + "\n", encoding="utf-8")

        with colliding_clock("kiro_crew.history", at=_INSTANT):
            log.append("s1", "user", "second")

        rows = log.read_messages("s1")
        assert _sorts_strictly_ascending(rows)
        assert transcript_sort_key(rows[-1]["ts"]) > transcript_sort_key(later)


class TestTheDashboardWriter:
    """``_ChatSlot.append`` -- the window re-serialized into the same file."""

    def test_two_appends_stay_ordered_on_a_stalled_clock(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        with colliding_clock("kiro_crew.dashboard.state", at=_INSTANT):
            slot.append("user", "the question")
            slot.append("assistant", "the reply")

        assert _sorts_strictly_ascending(slot.messages)

    def test_a_replayed_row_keeps_the_timestamp_it_arrived_with(self, tmp_path, monkeypatch):
        # A row replayed from a channel transcript carries the ts it was
        # originally written with. Restamping it would reorder the replay.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        original = "2026-01-01T00:00:00+00:00"

        with colliding_clock("kiro_crew.dashboard.state", at=_INSTANT):
            slot.append("user", "replayed", ts=original)

        assert slot.messages[-1]["ts"] == original


class TestTheDashboardWriterAndAForeignRow:
    """A row the slot never observed must still be ordered against.

    ``ConversationLog.append`` floors on the on-disk tail under the cross-process
    flock, so it sees every committed row. ``_ChatSlot.append`` runs on the event
    loop and may only read in-process state -- a ``stat`` plus a file read per
    append is what ``AUTOSDE.yaml``'s ``no-blocking-call-on-event-loop`` rule
    forbids -- so it floors on its in-memory window.

    Those are not the same set. ``_save_slot_to_history`` deliberately preserves a
    genuinely foreign on-disk row without folding it into ``slot.messages``, so a
    row can live in the file forever and never become a floor candidate. Under a
    colliding clock the slot's next append then produces a ``ts`` that TIES the
    foreign row, which is exactly the ambiguity the stamper exists to prevent.

    The fix caches the last known disk tail on the slot and floors on the later of
    the two, refreshed at the save boundary where the lock is already held.
    """

    def test_a_foreign_disk_row_is_ordered_against(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        with colliding_clock("kiro_crew.dashboard.state", at=_INSTANT):
            slot.append("user", "run the job")
            window_tail = slot.messages[-1]["ts"]
            # A subagent wrote this under the flock one microsecond later. It is
            # on disk and will be preserved as a foreign line; the slot never
            # observed it, so before the fix it was not a floor candidate.
            foreign = _parse_transcript_ts(window_tail) + timedelta(microseconds=1)
            slot._disk_tail_ts = foreign.isoformat()

            slot.append("assistant", "acknowledged")

        assert transcript_sort_key(slot.messages[-1]["ts"]) > transcript_sort_key(
            slot._disk_tail_ts
        ), "the slot's row ties or precedes a foreign on-disk row"

    def test_a_stale_cached_tail_does_not_drag_a_row_backwards(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        with colliding_clock("kiro_crew.dashboard.state", at=_INSTANT):
            slot.append("user", "later row", ts=(_INSTANT + timedelta(seconds=5)).isoformat())
            slot._disk_tail_ts = _INSTANT.isoformat()  # older than the window tail
            slot.append("assistant", "reply")

        assert _sorts_strictly_ascending(slot.messages)

    def test_a_replayed_row_still_keeps_its_timestamp(self, tmp_path, monkeypatch):
        """The extra floor candidate must not restamp an explicit ts."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._disk_tail_ts = "2099-01-01T00:00:00.000000+00:00"  # far in the future
        original = "2026-01-01T00:00:00+00:00"

        with colliding_clock("kiro_crew.dashboard.state", at=_INSTANT):
            slot.append("user", "replayed", ts=original)

        assert slot.messages[-1]["ts"] == original

    def test_no_cached_tail_behaves_exactly_as_before(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        assert slot._disk_tail_ts is None

        with colliding_clock("kiro_crew.dashboard.state", at=_INSTANT):
            slot.append("user", "the question")
            slot.append("assistant", "the reply")

        assert _sorts_strictly_ascending(slot.messages)

    def test_the_save_boundary_records_a_foreign_rows_ts(self, tmp_path, monkeypatch):
        """The save is the ONLY place the slot can learn a foreign row's ts.

        Without this the cache would never populate and the floor above would be
        permanently ``None`` -- the fix would be inert.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log = ConversationLog(tmp_path / "history")
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        _save_slot_to_history(state, slot, force=True)

        # Another process appends to the same transcript. The slot never sees it.
        state.conversation_log.append(slot.key, "assistant", "from a subagent")
        _save_slot_to_history(state, slot, force=True)

        assert slot._disk_tail_ts, "the save recorded no disk tail at all"

    def test_the_cached_tail_never_moves_backwards(self, tmp_path, monkeypatch):
        """A save must not regress the floor -- that would re-open the tie."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log = ConversationLog(tmp_path / "history")
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")

        future = "2099-01-01T00:00:00.000000+00:00"
        slot._disk_tail_ts = future
        _save_slot_to_history(state, slot, force=True)

        assert slot._disk_tail_ts == future, "a save regressed the ordering floor"
