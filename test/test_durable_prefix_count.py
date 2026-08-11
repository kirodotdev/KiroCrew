"""Absolute message cursors need a durable-only count of the frozen prefix.

``_disk_older_count`` counts every trimmed on-disk line. Transient rows
(``chunk``/``done``/``streaming``/``queued``/``permission``) are written to disk
but skipped on read-back, so as soon as one is trimmed that counter advances with
no durable row behind it. A cursor of ``_disk_older_count + <durable index>``
then names a row the consumer already received.

``_disk_older_durable_count`` counts the same prefix, durable rows only.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.state import (
    _MAX_SLOT_MESSAGES,
    TRANSIENT_ROLES,
    _ChatSlot,
    durable_row_count,
)


def _rows(*roles: str) -> list[dict]:
    return [{"role": r, "content": f"{r}-{i}"} for i, r in enumerate(roles)]


class TestDurableRowCount:
    def test_transient_rows_do_not_count(self):
        rows = _rows("user", "chunk", "assistant", "done", "streaming", "user")
        assert durable_row_count(rows) == 3
        assert len(rows) == 6, "the raw count is what _disk_older_count would use"

    @pytest.mark.parametrize("role", sorted(TRANSIENT_ROLES))
    def test_every_transient_role_is_excluded(self, role):
        """Pinned against the set itself so a new transient role cannot be added
        without either being counted here or failing this test."""
        assert durable_row_count(_rows(role)) == 0

    def test_durable_roles_count(self):
        assert durable_row_count(_rows("user", "assistant", "system")) == 3

    def test_a_row_with_no_role_counts_as_durable(self):
        """``_build_message_entry`` defaults a missing role to ``assistant``, so
        the count must default the same way rather than silently dropping it."""
        assert durable_row_count([{"content": "x"}]) == 1

    def test_empty_prefix(self):
        assert durable_row_count([]) == 0


class TestCursorSkew:
    """The defect itself, stated as arithmetic over one concrete history."""

    def test_a_trimmed_transient_row_shifts_a_raw_cursor_but_not_a_durable_one(self):
        # A history whose frozen prefix holds 2 durable rows and 1 transient one.
        prefix = _rows("user", "chunk", "assistant")
        window = _rows("user", "assistant")

        disk_older_count = len(prefix)
        disk_older_durable_count = durable_row_count(prefix)

        # A consumer paging over DURABLE rows asks for "the row after the 2 I
        # already read from the prefix" — i.e. durable index 0 of the window.
        durable_index_of_first_window_row = 0

        skewed = disk_older_count + durable_index_of_first_window_row
        correct = disk_older_durable_count + durable_index_of_first_window_row

        # Absolute positions over the durable-only view of the whole history:
        # [user, assistant] from the prefix, then [user, assistant] from the window.
        durable_history = [r for r in prefix + window if r["role"] not in TRANSIENT_ROLES]

        assert durable_history[correct]["content"] == window[0]["content"]
        assert skewed == correct + 1, "one trimmed transient row, one position of skew"
        assert durable_history[skewed]["content"] != window[0]["content"], (
            "the skewed cursor skips a row the consumer never saw"
        )

    def test_no_transient_rows_means_the_two_counters_agree(self):
        """Control: the skew appears only because transient rows are on disk."""
        prefix = _rows("user", "assistant", "user")
        assert durable_row_count(prefix) == len(prefix)


class TestLiveTrimAdvancesBothCounters:
    """The frozen prefix also grows at runtime, not only at restore.

    ``_ChatSlot.append`` trims the window past ``_MAX_SLOT_MESSAGES`` and credits
    ``_disk_older_count``. The real invariant is not "every restore site sets
    both counters" but **every frozen-prefix mutation advances both, each by its
    own semantics** — an increment site that moves only the raw counter reopens
    exactly the skew this exists to close.
    """

    def _slot_at_cap(self, *, disk_window_len: int) -> _ChatSlot:
        slot = _ChatSlot(key="trim-slot")
        slot.messages = [
            {"role": "user", "content": f"m{i}"} for i in range(_MAX_SLOT_MESSAGES)
        ]
        slot._disk_window_len = disk_window_len
        slot._disk_older_count = 0
        slot._disk_older_durable_count = 0
        return slot

    def test_a_trim_credits_durable_rows_only(self):
        """Prefix gains 3 lines but only 2 durable messages."""
        slot = self._slot_at_cap(disk_window_len=_MAX_SLOT_MESSAGES)
        # Make the three rows that will be trimmed durable, transient, durable.
        slot.messages[0] = {"role": "user", "content": "a"}
        slot.messages[1] = {"role": "chunk", "content": "partial"}
        slot.messages[2] = {"role": "assistant", "content": "b"}

        for _ in range(3):
            slot.append("user", "new", "msg msg-u", broadcast=False)

        assert slot._disk_older_count == 3
        assert slot._disk_older_durable_count == 2

    def test_unpersisted_overflow_enters_neither_counter(self):
        """Only the persisted subset joins the frozen prefix.

        With 5 rows leaving memory but only 3 on disk, the last 2 are gone from
        the window and were never flushed — crediting them to either counter
        would claim history that cannot be read back.
        """
        slot = self._slot_at_cap(disk_window_len=3)
        # First 3 (the persisted ones) are durable, transient, durable.
        slot.messages[0] = {"role": "user", "content": "a"}
        slot.messages[1] = {"role": "queued", "content": "waiting"}
        slot.messages[2] = {"role": "assistant", "content": "b"}
        # The 2 that overflow unpersisted are durable, and must NOT be counted.
        slot.messages[3] = {"role": "user", "content": "c"}
        slot.messages[4] = {"role": "assistant", "content": "d"}

        for _ in range(5):
            slot.append("user", "new", "msg msg-u", broadcast=False)

        assert slot._disk_older_count == 3, "raw counter credits only the persisted subset"
        assert slot._disk_older_durable_count == 2, "durable counter, same subset"

    def test_with_no_transient_rows_both_counters_move_together(self):
        """Control: the two diverge only because transient rows occupy lines."""
        slot = self._slot_at_cap(disk_window_len=_MAX_SLOT_MESSAGES)

        for _ in range(4):
            slot.append("user", "new", "msg msg-u", broadcast=False)

        assert slot._disk_older_count == 4
        assert slot._disk_older_durable_count == 4
