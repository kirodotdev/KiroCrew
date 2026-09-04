"""A fork rebuild must not append rows the fresh disk read already holds.

The mid-rotation rebuild reads the full chained corpus and appends the slot's
unflushed tail. "Unflushed" was decided against an EARLIER read inside the
snapshot loop, and two ``to_thread`` suspensions separate that read from this
one -- so a save landing in between persists those rows and appending the tail
blind duplicates them. A duplicated row makes the fork index ambiguous, which
is the failure this predicate exists to prevent.
"""

from __future__ import annotations

from kiro_crew.dashboard.chat_fork import drop_persisted_tail_prefix


def _m(ts: str, role: str = "user") -> dict:
    return {"ts": ts, "role": role, "content": "x"}


def test_tail_survives_when_disk_does_not_hold_it() -> None:
    disk = [_m("1"), _m("2", "assistant")]
    tail = [_m("3"), _m("4", "assistant")]
    assert drop_persisted_tail_prefix(disk, tail) == tail


def test_fully_persisted_tail_is_dropped_entirely() -> None:
    # The save landed between the two reads: every tail row is now on disk.
    tail = [_m("3"), _m("4", "assistant")]
    disk = [_m("1"), _m("2", "assistant"), *tail]
    assert drop_persisted_tail_prefix(disk, tail) == []


def test_partially_persisted_tail_keeps_only_the_unpersisted_remainder() -> None:
    # A save persists a PREFIX of the tail, never an interior slice.
    tail = [_m("3"), _m("4", "assistant"), _m("5")]
    disk = [_m("1"), _m("2", "assistant"), _m("3"), _m("4", "assistant")]
    assert drop_persisted_tail_prefix(disk, tail) == [_m("5")]


def test_same_timestamp_different_role_is_not_treated_as_the_same_row() -> None:
    # Identity is (ts, role): a user/assistant pair can share a second.
    tail = [_m("3", "assistant")]
    disk = [_m("1"), _m("3", "user")]
    assert drop_persisted_tail_prefix(disk, tail) == tail


def test_empty_inputs_are_returned_unchanged() -> None:
    assert drop_persisted_tail_prefix([], [_m("1")]) == [_m("1")]
    assert drop_persisted_tail_prefix([_m("1")], []) == []


def test_a_timestamp_and_role_collision_does_not_drop_a_distinct_row() -> None:
    # Two distinct rows CAN share a second and a role -- the same speaker twice
    # inside one second, or two processes stamping the same ts. A match here
    # DELETES a row, so identity must not collapse them: the tail row is
    # genuinely unpersisted and must survive into the fork.
    disk = [_m("1"), {"ts": "3", "role": "user", "content": "persisted"}]
    tail = [{"ts": "3", "role": "user", "content": "different, still in memory"}]
    assert drop_persisted_tail_prefix(disk, tail) == tail


def test_the_stable_row_id_decides_when_both_rows_carry_one() -> None:
    # `meta.mid` is what history assigns per row, so it cannot collide. It wins
    # over the field-wise fallback -- including when the content was rewritten
    # (a redaction pass on the persisted copy) and a content compare would miss.
    disk = [{"ts": "3", "role": "assistant", "content": "redacted", "meta": {"mid": "m9"}}]
    tail = [{"ts": "3", "role": "assistant", "content": "raw", "meta": {"mid": "m9"}}]
    assert drop_persisted_tail_prefix(disk, tail) == []


def test_differing_row_ids_are_never_treated_as_the_same_row() -> None:
    disk = [{"ts": "3", "role": "user", "content": "same", "meta": {"mid": "m1"}}]
    tail = [{"ts": "3", "role": "user", "content": "same", "meta": {"mid": "m2"}}]
    assert drop_persisted_tail_prefix(disk, tail) == tail
