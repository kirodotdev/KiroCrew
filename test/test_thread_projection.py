"""What a thread's model sees of its parent, proven without a model.

Every rule in ``thread_projection`` is a budget, a cursor or a band boundary, and
all three are decidable from rows alone -- so the summarizer is a lambda here and
the assertions are about which rows reached which band, not about what a model
said of them. The one exception is the degrade path, where a summarizer that
RAISES is the input under test.
"""

from __future__ import annotations

import asyncio

import pytest

import kiro_crew.dashboard.thread_projection as tp
from kiro_crew.crew_log import declaration_for
from kiro_crew.dashboard.thread_projection import (
    ANCHOR_SEQ_UNKNOWN,
    BLOCK_BUDGET_CHARS,
    N_BEFORE,
    PARENT_INTENTS_SHOWN,
    PROJECTION_VERSION,
    Projection,
    ThreadHandle,
    ThreadState,
    Window,
    build_projection,
    digest_row,
    find_anchor,
    fold_chunks,
    loop_bridged_summarizer,
    parent_log_position,
    parse_ts,
    project_for_turn,
    projected_rows,
    read_thread_state,
    render_context_entry,
    render_parent_summary,
    resolve_anchor_log_seq,
    select_window,
)


def row(seq: int, kind: str = "message/received", **data: object) -> dict:
    return {"type": kind, "seq": seq, "time": 1, "src": "t", "data": dict(data)}


def stamped(seq: int, when_ms: int) -> dict:
    return {"type": "message/received", "seq": seq, "time": when_ms, "src": "t", "data": {}}


def echo(prompt: str) -> str:
    """A summarizer that proves which rows reached it, by counting their lines."""
    lines = [line for line in prompt.splitlines() if line.startswith("[")]
    return f"folded {len(lines)} rows: " + " ".join(
        line.split("]")[0].lstrip("[") for line in lines
    )


HANDLE = ThreadHandle(
    parent_slot_key="slot-1",
    anchor_mid="mid-abc",
    parent_turn_id="turn-7",
    parent_session_id="sess-9",
    parent_log_seq=100,
)


# ── joining the two clocks ──


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1970-01-01T00:00:01Z", 1_000),
        ("1970-01-01T00:00:01+00:00", 1_000),
        ("1970-01-01T00:00:01", 1_000),  # naive reads as UTC, as the writer produces
        ("1970-01-01T00:00:01.500Z", 1_500),
        ("1970-01-01T01:00:01+01:00", 1_000),  # an offset is honoured, not stripped
    ],
)
def test_transcript_timestamps_convert_to_the_logs_own_unit(text, expected):
    assert parse_ts(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "yesterday", "2026-13-45T99:99:99Z"])
def test_an_unusable_timestamp_is_none_rather_than_a_guess(text):
    assert parse_ts(text) is None


def test_the_anchor_resolves_to_the_last_entry_at_or_before_its_timestamp():
    rows = [stamped(10, 1_000), stamped(11, 2_000), stamped(12, 3_000)]
    assert resolve_anchor_log_seq(rows, "1970-01-01T00:00:02Z") == 11
    assert resolve_anchor_log_seq(rows, "1970-01-01T00:00:02.500Z") == 11
    assert resolve_anchor_log_seq(rows, "1970-01-01T00:00:09Z") == 12


def test_an_anchor_older_than_the_whole_log_is_unknown_and_not_seq_one():
    rows = [stamped(10, 5_000), stamped(11, 6_000)]
    assert resolve_anchor_log_seq(rows, "1970-01-01T00:00:01Z") == ANCHOR_SEQ_UNKNOWN


def test_an_unparseable_anchor_timestamp_resolves_to_unknown():
    rows = [stamped(10, 1_000)]
    assert resolve_anchor_log_seq(rows, "whenever") == ANCHOR_SEQ_UNKNOWN


def test_rows_missing_a_seq_or_a_time_cannot_place_the_anchor():
    rows = [
        {"type": "message/received", "seq": None, "time": 1_000, "src": "t", "data": {}},
        {"type": "message/received", "seq": 11, "time": None, "src": "t", "data": {}},
    ]
    assert resolve_anchor_log_seq(rows, "1970-01-01T00:00:09Z") == ANCHOR_SEQ_UNKNOWN


def test_out_of_order_entries_still_resolve_to_the_highest_eligible_seq():
    rows = [stamped(12, 3_000), stamped(10, 1_000), stamped(11, 2_000)]
    assert resolve_anchor_log_seq(rows, "1970-01-01T00:00:02Z") == 11


# ── reaching the one record ──


def test_the_open_anchor_naming_this_slot_gives_its_mid_and_position():
    anchors = {
        "m-aaa": {"thread_slot": "chat-other", "closed_at": None, "parent_log_seq": 1},
        "m-bbb": {"thread_slot": "chat-77", "closed_at": None, "parent_log_seq": 412},
    }
    assert find_anchor(anchors, "chat-77") == ("m-bbb", 412)


def test_an_open_anchor_wins_over_a_closed_one_carrying_the_same_slot():
    """A retracted mint followed by a successful one can leave both, and the live
    thread is the one asking."""
    anchors = {
        "m-old": {
            "thread_slot": "chat-77",
            "closed_at": "2026-01-01T00:00:00Z",
            "parent_log_seq": 9,
        },
        "m-new": {"thread_slot": "chat-77", "closed_at": None, "parent_log_seq": 412},
    }
    assert find_anchor(anchors, "chat-77") == ("m-new", 412)


def test_a_closed_anchor_is_still_found_when_it_is_the_only_one():
    anchors = {
        "m-old": {
            "thread_slot": "chat-77",
            "closed_at": "2026-01-01T00:00:00Z",
            "parent_log_seq": 9,
        }
    }
    assert find_anchor(anchors, "chat-77") == ("m-old", 9)


def test_no_anchor_naming_this_slot_is_unknown_rather_than_a_guess():
    anchors = {"m-aaa": {"thread_slot": "chat-other", "closed_at": None, "parent_log_seq": 1}}
    assert find_anchor(anchors, "chat-77") == ("", ANCHOR_SEQ_UNKNOWN)
    assert find_anchor({}, "chat-77") == ("", ANCHOR_SEQ_UNKNOWN)


def test_a_bad_position_on_a_matching_anchor_still_finds_the_mid():
    """The mid is what makes the thread reachable; the position is only the
    window's start, so a bad one degrades the window and not the lookup."""
    anchors = {"m-bbb": {"thread_slot": "chat-77", "closed_at": None, "parent_log_seq": True}}
    assert find_anchor(anchors, "chat-77") == ("m-bbb", 0)


def test_a_thread_with_no_readable_log_has_no_parent_and_no_cursor():
    state = read_thread_state("")
    assert state.parent_slot_key == ""
    assert state.cursor is None
    assert state.first_turn is True


def test_a_thread_with_no_recorded_projection_is_on_its_first_turn():
    assert ThreadState(parent_slot_key="chat-1").first_turn is True
    assert ThreadState(parent_slot_key="chat-1", cursor=0).first_turn is False


# ── window selection ──


def test_first_turn_reaches_back_over_the_anchor_and_forward_to_now():
    window = select_window(anchor_log_seq=100, latest_log_seq=140, cursor=None)
    assert (window.start, window.end) == (100 - N_BEFORE, 140)
    assert window.first_turn is True
    assert window.rows == 141 - (100 - N_BEFORE)


def test_first_turn_never_reaches_below_the_first_seq():
    window = select_window(anchor_log_seq=2, latest_log_seq=9, cursor=None)
    assert window.start == 1


def test_an_unresolved_anchor_takes_a_small_window_off_the_tail_not_the_whole_log():
    window = select_window(anchor_log_seq=ANCHOR_SEQ_UNKNOWN, latest_log_seq=5_000, cursor=None)
    assert (window.start, window.end) == (5_000 - N_BEFORE, 5_000)


def test_later_turn_starts_strictly_after_the_cursor():
    window = select_window(anchor_log_seq=100, latest_log_seq=160, cursor=140)
    assert (window.start, window.end) == (141, 160)
    assert window.first_turn is False


def test_a_parent_that_appended_nothing_yields_an_empty_window():
    window = select_window(anchor_log_seq=100, latest_log_seq=140, cursor=140)
    assert window.empty is True
    assert window.rows == 0


# ── row selection ──


def test_rows_outside_the_window_and_uninteresting_types_are_dropped():
    rows = [
        row(9, text="before the window"),
        row(10, text="in"),
        row(11, "spend/recorded", cost=1),
        row(12, "tool/called", name="fs_write"),
        row(99, text="after the window"),
    ]
    kept = projected_rows(rows, Window(start=10, end=12, first_turn=True))
    assert [entry["seq"] for entry in kept] == [10, 12]


def test_an_ignorable_row_is_dropped_because_nothing_depends_on_reading_it():
    rows = [row(10, text="kept"), {**row(11, text="sampled"), "ignorable": True}]
    kept = projected_rows(rows, Window(start=10, end=11, first_turn=True))
    assert [entry["seq"] for entry in kept] == [10]


def test_a_row_without_an_integer_seq_cannot_be_placed_and_is_dropped():
    rows = [{**row(10, text="ok")}, {**row(11, text="bad"), "seq": None}]
    kept = projected_rows(rows, Window(start=1, end=99, first_turn=True))
    assert [entry["seq"] for entry in kept] == [10]


# ── digests ──


@pytest.mark.parametrize(
    "entry,expected",
    [
        (row(5, text="hello", role="user"), "[5] user: hello"),
        (row(6, "message/sent", text="hi"), "[6] assistant: hi"),
        (row(7, "tool/called", name="fs_read"), "[7] tool call fs_read"),
        (row(8, "tool/completed", name="fs_read", status="ok"), "[8] tool fs_read -> ok"),
        (row(9, "turn/started", turn=3), "[9] turn 3 started"),
        (row(10, "turn/completed", turn=3), "[10] turn 3 completed"),
        (row(11, "thread/opened"), "[11] a thread was opened on this conversation"),
        (row(12, "thread/closed"), "[12] a thread on this conversation was closed"),
    ],
)
def test_each_projected_type_digests_to_one_readable_line(entry, expected):
    assert digest_row(entry) == expected


def test_an_unknown_type_digests_to_its_name_rather_than_raising():
    assert digest_row(row(11, "something/new")) == "[11] something/new"


def test_a_huge_row_is_clipped_so_one_row_cannot_take_the_prompt():
    line = digest_row(row(12, text="x" * 10_000))
    assert len(line) < 700
    assert line.endswith("…")


# ── folding ──


def test_rows_fold_in_groups_of_the_configured_size():
    rows = [row(seq, text=f"m{seq}") for seq in range(1, 21)]
    fold = fold_chunks(rows, echo, chunk_rows=8)
    assert len(fold.summaries) == 3
    assert fold.summaries[0].startswith("folded 8 rows:")
    assert fold.summaries[2].startswith("folded 4 rows:")
    assert fold.complete
    assert fold.last_seq == 20


def test_a_failed_group_degrades_to_its_digests_and_keeps_the_band():
    def broken(prompt: str) -> str:
        raise RuntimeError("model unreachable")

    fold = fold_chunks([row(1, text="a"), row(2, text="b")], broken, chunk_rows=8)
    assert len(fold.summaries) == 1
    assert "[1] user: a" in fold.summaries[0]
    assert "[2] user: b" in fold.summaries[0]


def test_an_empty_summary_falls_back_to_digests_rather_than_a_blank_band():
    fold = fold_chunks([row(1, text="a")], lambda prompt: "   ", chunk_rows=8)
    assert "[1] user: a" in fold.summaries[0]


def test_the_fold_budget_stops_the_summarizer_instead_of_paying_for_trimmed_text():
    calls: list[str] = []

    def counting(prompt: str) -> str:
        calls.append(prompt)
        return "x" * 100

    rows = [row(seq, text=f"m{seq}") for seq in range(1, 81)]
    fold = fold_chunks(rows, counting, chunk_rows=8, budget_chars=250)
    # 10 groups are available; the budget stops it after the third call, whose
    # output is what first carries the total past 250.
    assert len(calls) == 3
    assert not fold.complete
    assert fold.last_seq == 24


def test_the_first_group_always_folds_so_a_spent_budget_cannot_stall_a_thread():
    calls: list[str] = []

    def counting(prompt: str) -> str:
        calls.append(prompt)
        return "x" * 100

    rows = [row(seq, text=f"m{seq}") for seq in range(1, 33)]
    fold = fold_chunks(rows, counting, chunk_rows=8, budget_chars=0)
    assert len(calls) == 1
    assert fold.last_seq == 8
    assert not fold.complete


def test_an_unbudgeted_fold_still_summarizes_every_group():
    rows = [row(seq, text=f"m{seq}") for seq in range(1, 41)]
    fold = fold_chunks(rows, echo, chunk_rows=8)
    assert len(fold.summaries) == 5
    assert fold.complete


# ── the block ──


def test_the_first_turn_carries_all_three_bands_when_the_parent_has_a_summary():
    rows = [row(seq, text=f"m{seq}") for seq in range(94, 111)]
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=110, cursor=None),
        rows=rows,
        summarize=echo,
        anchor_text="the message the thread hangs off",
        parent_compaction_summary="the parent had already discussed the schema",
    )
    assert len(projection.bands) == 4  # summary, anchor, before, since
    assert "the parent had already discussed the schema" in projection.text
    assert "the message the thread hangs off" in projection.text
    assert projection.cursor_seq == 110


def test_the_parent_summary_band_is_absent_when_the_parent_never_compacted():
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=104, cursor=None),
        rows=[row(seq, text=f"m{seq}") for seq in range(100, 105)],
        summarize=echo,
        anchor_text="anchor",
    )
    assert not any("parent's own summary" in band for band in projection.bands)


def test_the_anchor_band_names_the_message_when_its_text_is_unavailable():
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=100, cursor=None),
        rows=[row(100, text="m")],
        summarize=echo,
    )
    assert "mid-abc" in projection.text


def test_rows_split_at_the_anchor_so_before_and_after_are_different_bands():
    rows = [row(seq, text=f"m{seq}") for seq in range(96, 105)]
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=104, cursor=None),
        rows=rows,
        summarize=echo,
        anchor_text="anchor",
    )
    # 96..99 before, 100..104 at or after.
    assert "folded 4 rows: 96 97 98 99" in projection.text
    assert "folded 5 rows: 100 101 102 103 104" in projection.text


def test_a_later_turn_projects_only_the_delta_and_no_anchor_band():
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=143, cursor=140),
        rows=[row(seq, text=f"m{seq}") for seq in range(130, 144)],
        summarize=echo,
        anchor_text="anchor",
        parent_compaction_summary="earlier",
    )
    assert projection.bands == ("New in the parent conversation since the last update:",)
    assert "anchor" not in projection.text
    assert "folded 3 rows: 141 142 143" in projection.text
    assert projection.cursor_seq == 143


def test_a_backlog_costs_a_bounded_number_of_summarizer_calls_per_turn():
    calls: list[str] = []

    def counting(prompt: str) -> str:
        calls.append(prompt)
        return "x" * 400

    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=1140, cursor=140),
        rows=[row(seq, text=f"m{seq}") for seq in range(141, 1141)],
        summarize=counting,
        anchor_text="anchor",
        budget_chars=1200,
    )
    # 125 groups are available. The budget stops the fold well short of them.
    assert len(calls) <= 5
    assert projection.cursor_seq < 1140


def test_a_stopped_fold_leaves_the_skipped_rows_for_the_next_turn():
    def wordy(prompt: str) -> str:
        return "x" * 400

    rows = [row(seq, text=f"m{seq}") for seq in range(141, 1141)]
    first = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=1140, cursor=140),
        rows=rows,
        summarize=wordy,
        anchor_text="anchor",
        budget_chars=1200,
    )
    # The cursor stands on a row that was actually summarized, so the next window
    # opens on the first row this turn did not account for.
    assert first.cursor_seq >= 148
    following = select_window(anchor_log_seq=100, latest_log_seq=1140, cursor=first.cursor_seq)
    assert following.start == first.cursor_seq + 1

    second = build_projection(
        handle=HANDLE,
        window=following,
        rows=rows,
        summarize=wordy,
        anchor_text="anchor",
        budget_chars=1200,
    )
    assert second.cursor_seq > first.cursor_seq


def test_a_fold_that_summarized_nothing_cannot_walk_the_cursor_backwards():
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=1140, cursor=140),
        rows=[row(seq, text=f"m{seq}") for seq in range(141, 1141)],
        summarize=lambda prompt: "x" * 4000,
        anchor_text="anchor",
        budget_chars=100,
    )
    assert projection.cursor_seq >= 140


def test_an_empty_delta_injects_nothing_at_all():
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=140, cursor=140),
        rows=[],
        summarize=echo,
    )
    assert projection.empty is True
    assert projection.text == ""
    assert projection.bands == ()


def test_a_first_turn_with_no_projectable_rows_still_names_the_anchor():
    """A parent whose crew log is off, or whose window holds only bookkeeping,
    must not cost the thread the one message it is about -- the anchor's text
    comes from the transcript, not the log."""
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=104, cursor=None),
        rows=[row(seq, "spend/recorded", cost=1) for seq in range(94, 105)],
        summarize=echo,
        anchor_text="the question the thread is about",
    )
    assert projection.empty is False
    assert "the question the thread is about" in projection.text
    assert projection.rows == 0


def test_a_window_that_produced_no_block_does_not_advance_the_cursor():
    """Otherwise a turn told nothing about those rows would consume them, and the
    next delta would start past history the thread never saw."""
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=150, cursor=140),
        rows=[row(seq, "spend/recorded", cost=1) for seq in range(141, 151)],
        summarize=echo,
    )
    assert projection.empty is True
    assert projection.cursor_seq == 140


def test_a_parent_with_no_log_tail_yet_still_gets_its_anchor_on_the_first_turn():
    """The window is empty here -- a parent whose crew log holds nothing projectable
    -- and the anchor is the one thing the thread is about. Injecting nothing would
    also publish a cursor, and any cursor makes the next window a LATER turn, so
    the anchor band becomes unreachable for the thread with the least context."""
    window = select_window(anchor_log_seq=ANCHOR_SEQ_UNKNOWN, latest_log_seq=0, cursor=None)
    assert window.empty is True
    projection = build_projection(
        handle=HANDLE,
        window=window,
        rows=[],
        summarize=echo,
        anchor_text="why is triage slow",
    )
    assert projection.empty is False
    assert "why is triage slow" in projection.text
    assert projection.bands == ("The message this thread was opened on:",)


def test_an_empty_window_on_a_later_turn_still_injects_nothing():
    """Only the FIRST turn has a preamble. A later empty delta is a no-op, so the
    admission above cannot turn every quiet turn into a repeat of the anchor."""
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=140, cursor=140),
        rows=[],
        summarize=echo,
        anchor_text="why is triage slow",
    )
    assert projection.empty is True
    assert projection.cursor_seq == 140


def test_an_unresolved_anchor_puts_every_row_on_the_since_side():
    unknown = ThreadHandle(parent_slot_key="slot-1", anchor_mid="mid-abc", parent_log_seq=0)
    projection = build_projection(
        handle=unknown,
        window=select_window(anchor_log_seq=ANCHOR_SEQ_UNKNOWN, latest_log_seq=200, cursor=None),
        rows=[row(seq, text=f"m{seq}") for seq in range(194, 201)],
        summarize=echo,
        anchor_text="anchor",
    )
    assert not any("Just before it" in band for band in projection.bands)
    assert "folded 7 rows: 194 195 196 197 198 199 200" in projection.text


def test_a_delta_of_only_uninteresting_rows_also_injects_nothing():
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=145, cursor=140),
        rows=[row(seq, "spend/recorded", cost=1) for seq in range(141, 146)],
        summarize=echo,
    )
    assert projection.empty is True


def test_a_block_over_budget_is_trimmed_and_says_so():
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=400, cursor=140),
        rows=[row(seq, text="y" * 400) for seq in range(141, 401)],
        summarize=lambda prompt: "z" * 900,
        budget_chars=1_000,
    )
    assert projection.truncated is True
    assert len(projection.text) < 1_100
    assert "trimmed" in projection.text


def test_the_default_budget_is_about_a_thousand_tokens():
    assert BLOCK_BUDGET_CHARS == 4_000


# ── provenance and injection ──


def test_provenance_is_exactly_the_declared_entry_fields():
    """The crew log's vocabulary is a contract a reader folds, so the row this
    builds carries the declared keys and nothing else -- an undeclared key in
    ``data`` would be the writer extending the schema by writing to it."""
    declaration = declaration_for("session", "thread/context_projected")
    assert declaration is not None
    projection = Projection(
        text="body", cursor_seq=143, window_start_seq=100, rows=3, bands=("a", "b")
    )
    recorded = projection.provenance()
    declared = {field.name for field in declaration.fields}
    assert set(recorded) <= declared
    # ``anchor`` is the emitter's to supply from the handle, not the projector's.
    assert declared - set(recorded) == {"anchor"}
    assert recorded["cursor_seq"] == 143
    assert recorded["window_start_seq"] == 100
    assert recorded["summary_version"] == PROJECTION_VERSION
    assert recorded["block_chars"] == 4
    assert recorded["rows"] == 3
    assert recorded["partial"] is False
    assert recorded["fold_generation"] == 0


def test_how_the_block_was_built_is_not_written_into_the_log_row():
    recorded = Projection(text="body", cursor_seq=1, bands=("a",), truncated=True).provenance()
    assert "bands" not in recorded
    assert "truncated" not in recorded


def test_the_injected_entry_says_it_is_a_summary_and_points_at_the_read_tool():
    entry = render_context_entry(Projection(text="body", cursor_seq=7), HANDLE)
    assert "never its transcript" in entry["content"]
    assert "thread_context_read" in entry["content"]
    assert entry["cursor_seq"] == 7
    assert entry["thread_handle"]["anchor_mid"] == "mid-abc"
    assert entry["thread_handle"]["parent_log_seq"] == 100


def test_the_injected_entry_uses_the_queues_own_key_contract():
    """The drain reads ``content`` and ``source`` and frames them; a producer that
    spelled either differently is dropped with a KeyError at drain time, which is
    a turn's whole context lost far from the write that caused it."""
    from kiro_crew.dashboard.chat_runner import drain_pending_context

    entry = render_context_entry(Projection(text="body", cursor_seq=7), HANDLE)
    assert entry["source"] and entry["content"]

    class _Slot:
        _pending_context = [entry]

        def drop_foreign_authorized_notes(self):
            return 0

    framed = drain_pending_context(_Slot())
    assert 'Background context from "the parent conversation"' in framed
    assert "body" in framed
    assert "[End of background context]" in framed


def test_the_injected_entry_is_not_marked_authorized():
    entry = render_context_entry(Projection(text="body", cursor_seq=7), HANDLE)
    assert "authorized" not in entry


def test_the_injected_entry_carries_no_expiry():
    """The queue expires entries so a stale app payload cannot surface turns later.
    This one is written immediately before the turn that consumes it, and a person
    typing slowly must not lose their thread's whole first context to a timer."""
    entry = render_context_entry(Projection(text="body", cursor_seq=7), HANDLE)
    assert "maxAge" not in entry


# ── bridging the sync projector to an async model call ──


def test_the_bridge_refuses_to_run_on_the_event_loop_thread():
    """It blocks on a coroutine submitted to that same loop, so running it ON the
    loop would deadlock. Refusing is a traceback in a warning; hanging is a turn
    that never answers."""
    import asyncio

    summarize = loop_bridged_summarizer(object(), object())

    async def _on_loop():
        with pytest.raises(RuntimeError, match="off the event loop"):
            summarize("anything")

    asyncio.run(_on_loop())


@pytest.mark.asyncio
async def test_a_slot_with_no_acp_session_is_not_a_thread_and_costs_one_lookup():
    class _Slot:
        key = "chat-1"
        _acp_client = None

    assert await project_for_turn(object(), _Slot()) is None


@pytest.mark.asyncio
async def test_a_session_whose_log_names_no_parent_is_not_a_thread(monkeypatch):
    """Every ordinary chat reaches this line, so the answer must be None rather
    than an attempt to find an anchor for a conversation that hangs off nothing."""
    import kiro_crew.dashboard.thread_projection as tp

    monkeypatch.setattr(tp, "read_thread_state", lambda sid: ThreadState())
    monkeypatch.setattr("kiro_crew.crew_log.emit.session_id_of", lambda client: "sess-ordinary")

    class _Slot:
        key = "chat-1"
        _acp_client = object()

    assert await project_for_turn(object(), _Slot()) is None


# ── the first band reuses the parent's own summary ──


def test_the_parents_stored_intents_become_the_first_band():
    payload = {
        "intents": [
            {"title": "Ship the threads epic", "status": "active"},
            {"title": "Fix the Windows shard", "status": "done"},
        ]
    }
    text = render_parent_summary(payload)
    assert "Ship the threads epic (active)" in text
    assert "Fix the Windows shard (done)" in text


def test_a_stale_summary_is_used_and_labelled_stale():
    """The parent moved on since it was generated, which is what the window and
    the delta cover. An empty band because the summary is one append behind would
    be worse than a slightly old one that says so."""
    text = render_parent_summary({"intents": [{"title": "Ship it"}]}, stale=True)
    assert "moved on since" in text
    assert "Ship it" in text


def test_only_the_head_of_the_intent_list_reaches_the_band():
    payload = {"intents": [{"title": f"t{n}"} for n in range(20)]}
    text = render_parent_summary(payload)
    assert text.count("\n- ") == PARENT_INTENTS_SHOWN
    assert "t0" in text and "t19" not in text


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"intents": "not a list"}, {"intents": []}, {"intents": [{"title": "  "}]}],
)
def test_a_parent_with_no_usable_summary_contributes_no_band(payload):
    assert render_parent_summary(payload) == ""


def test_an_intent_without_a_title_is_skipped_not_rendered_blank():
    payload = {"intents": [{"status": "active"}, {"title": "real one"}]}
    assert render_parent_summary(payload) == "What that conversation has been about:\n- real one"


# ── a window the caller asked for and did not get ──


def test_a_failed_window_read_publishes_nothing_and_holds_the_cursor():
    """A read that FAILED is not a window that held nothing. Advancing the cursor
    over rows nobody read skips them silently, and because every later window
    starts after the cursor there is no delta that could ever recover them."""
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=160, cursor=140),
        rows=[],
        summarize=echo,
        window_unread=True,
    )
    assert projection.empty is True
    assert projection.cursor_seq == 140


def test_a_failed_window_read_on_the_FIRST_turn_also_holds_the_cursor():
    """The dangerous case: a first turn has an anchor band to render, so it would
    otherwise look productive while committing cursor=latest over nothing."""
    window = select_window(anchor_log_seq=100, latest_log_seq=160, cursor=None)
    projection = build_projection(
        handle=HANDLE,
        window=window,
        rows=[],
        summarize=echo,
        anchor_text="the anchored question",
        window_unread=True,
    )
    assert projection.empty is True
    assert "the anchored question" not in projection.text
    # Held BEFORE the window, so the next turn asks for this same window again.
    assert projection.cursor_seq == window.start - 1
    assert (
        select_window(anchor_log_seq=100, latest_log_seq=160, cursor=projection.cursor_seq).start
        == window.start
    )


def test_an_empty_window_that_was_read_fine_still_renders_the_first_turn_preamble():
    """The contrast that makes the flag meaningful: rows absent because the window
    held none is not rows absent because the read failed."""
    projection = build_projection(
        handle=HANDLE,
        window=select_window(anchor_log_seq=100, latest_log_seq=104, cursor=None),
        rows=[row(seq, "spend/recorded", cost=1) for seq in range(100, 105)],
        summarize=echo,
        anchor_text="the anchored question",
        window_unread=False,
    )
    assert projection.empty is False
    assert "the anchored question" in projection.text


# ── reading the two logs ──
#
# Everything below crosses a seam the projector does not own: the parent's crew
# log, the thread's own log, the parent's transcript, its live chunk rows, the
# model call. Each is faked at ITS boundary rather than by patching the projector's
# own helpers, because the rules under test here are precisely what the projector
# does with what those seams answer -- including when they answer by raising.


class _Entry:
    """One crew-log entry, in the shape ``iter_from`` yields."""

    def __init__(self, seq: int, *, time: int = 0, type: str = "message/sent", **data: object):
        self.seq = seq
        self.time = time
        self.type = type
        self.data = dict(data)


class _Log:
    def __init__(self, entries):
        self._entries = list(entries)

    def iter_from(self, start, strict_seq=True):
        for entry in self._entries:
            if entry.seq >= start:
                yield entry


class _FaultingLog:
    def __init__(self, at: int = 0):
        self._at = at

    def iter_from(self, start, strict_seq=True):
        for seq in range(start, start + self._at):
            yield _Entry(seq, time=1)
        raise OSError("the log faulted mid-scan")


def _logs(monkeypatch, mapping):
    monkeypatch.setattr(
        "kiro_crew.crew_log.projection.open_session_log", lambda sid: mapping.get(sid)
    )


# ── the anchor's position in the parent's crew log ──


def test_the_anchors_position_is_the_last_entry_written_at_or_before_it(monkeypatch):
    _logs(monkeypatch, {"sess-p": _Log([_Entry(1, time=500), _Entry(2, time=1500)])})
    # The anchor was delivered at 1s, so entry 2 (1.5s) is after it and entry 1 is not.
    assert parent_log_position("sess-p", "1970-01-01T00:00:01Z") == 1


def test_an_anchor_newer_than_every_entry_positions_at_the_tail(monkeypatch):
    _logs(monkeypatch, {"sess-p": _Log([_Entry(seq, time=seq * 10) for seq in range(1, 6)])})
    assert parent_log_position("sess-p", "1970-01-01T00:01:00Z") == 5


@pytest.mark.parametrize(
    "session_id,anchor_ts",
    [
        ("", "1970-01-01T00:00:01Z"),  # no parent session to read
        ("sess-p", "yesterday"),  # a timestamp that is not one
    ],
)
def test_a_position_that_cannot_be_asked_for_is_unknown(monkeypatch, session_id, anchor_ts):
    """UNKNOWN rather than a guess, and never a raise: ``select_window`` reads this
    as 'we do not know where the anchor is' and takes a small recent window."""
    _logs(monkeypatch, {"sess-p": _Log([_Entry(1, time=1)])})
    assert parent_log_position(session_id, anchor_ts) == ANCHOR_SEQ_UNKNOWN


def test_a_parent_with_no_log_at_all_positions_unknown_and_the_thread_still_opens(monkeypatch):
    _logs(monkeypatch, {})
    assert parent_log_position("sess-p", "1970-01-01T00:00:01Z") == ANCHOR_SEQ_UNKNOWN


def test_a_log_that_faults_mid_scan_positions_unknown_rather_than_raising(monkeypatch):
    """A thread that refused to open because its parent could not be positioned
    would trade the whole feature for the band it was missing."""
    _logs(monkeypatch, {"sess-p": _FaultingLog(at=3)})
    assert parent_log_position("sess-p", "1970-01-01T00:00:01Z") == ANCHOR_SEQ_UNKNOWN


# ── what the thread's own log says about itself ──


@pytest.fixture(autouse=True)
def _forget_non_threads():
    """The not-a-thread set is process-wide and keyed by session id, and these tests
    reuse ``sess-t`` for both a thread and an ordinary chat. Cleared around each one
    so a cached answer cannot decide a later test's."""
    tp._NOT_A_THREAD.clear()
    yield
    tp._NOT_A_THREAD.clear()


class _Slot:
    def __init__(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)


class _Client:
    def __init__(self, session_id=""):
        self.session_id = session_id


def test_the_parents_log_is_named_from_its_live_handle_when_it_has_one():
    slot = _Slot(_acp_client=_Client("sess-live"), _crew_log_opened_sid="sess-recorded")
    assert tp.slot_log_sid(slot) == "sess-live"


def test_a_parent_between_turns_is_named_from_its_own_record():
    """The defect this closes: a handle carries a session id only while a turn is
    running on that client, and a thread reads its parent BETWEEN the parent's turns.
    Asking the handle alone reads a conversation that has a log as having none, so a
    correctly anchored thread is told its parent keeps nothing readable."""
    slot = _Slot(_acp_client=_Client(""), _crew_log_opened_sid="sess-recorded")
    assert tp.slot_log_sid(slot) == "sess-recorded"


def test_a_slot_whose_client_was_replaced_still_names_its_log():
    """A cold start hands the slot a fresh client with no session id yet. The slot's
    own record of the store it writes is untouched by that."""
    slot = _Slot(_acp_client=_Client("sess-live"), _crew_log_opened_sid="sess-recorded")
    slot._acp_client = _Client("")  # the replacement, before its first turn
    assert tp.slot_log_sid(slot) == "sess-recorded"


def test_the_store_a_slot_was_on_before_a_switch_still_answers():
    """A provider switch moves the slot to a new store; the rows a reader wants are
    the same conversation either way, so the earlier store beats answering nothing."""
    slot = _Slot(_acp_client=None, _crew_log_opened_sid="", _crew_log_previous_sid="sess-earlier")
    assert tp.slot_log_sid(slot) == "sess-earlier"


def test_a_slot_with_no_handle_and_no_record_names_nothing():
    """Every source here is in memory, so a restart that has not re-run this slot's
    turn answers nothing and leaves the store lookup to the caller."""
    assert tp.slot_log_sid(_Slot(_acp_client=None)) == ""


def test_a_chat_that_is_not_a_thread_is_asked_once_and_not_walked_again(monkeypatch):
    """Every dashboard turn asks this, so an ordinary chat would otherwise walk its
    whole log every turn forever, and pay more the longer it lives. Thread-ness is
    fixed at mint, so the first answer is the final one."""
    opens: list[str] = []
    log = _Log([_Entry(1, type="session/opened", parent=None)])

    def counting(sid):
        opens.append(sid)
        return log if sid == "sess-plain" else None

    monkeypatch.setattr("kiro_crew.crew_log.projection.open_session_log", counting)

    assert read_thread_state("sess-plain") == ThreadState()
    assert read_thread_state("sess-plain") == ThreadState()
    assert read_thread_state("sess-plain") == ThreadState()
    assert len(opens) == 1


def test_a_header_that_has_not_landed_is_unknown_rather_than_not_a_thread(monkeypatch):
    """The lineage write races the thread's own first turn -- the settle wait exists
    for exactly that. A log with no ``session/opened`` yet must stay askable, or a
    real thread whose header is one moment behind would be stranded for the life of
    the process."""
    _logs(monkeypatch, {"sess-t": _Log([_Entry(1, type="turn/started", turn=1)])})
    assert read_thread_state("sess-t") == ThreadState()
    assert "sess-t" not in tp._NOT_A_THREAD


def test_a_thread_is_never_cached_away(monkeypatch):
    _logs(
        monkeypatch,
        {"sess-t": _Log([_Entry(1, type="session/opened", parent={"slot": "chat-1"})])},
    )
    assert read_thread_state("sess-t").parent_slot_key == "chat-1"
    assert "sess-t" not in tp._NOT_A_THREAD


def test_the_not_a_thread_set_is_bounded(monkeypatch):
    """A gateway outlives thousands of sessions, so this must not grow without end.
    Overflow forgets everything, which costs a walk and nothing else."""
    tp._NOT_A_THREAD.update(f"sess-{n}" for n in range(tp._NOT_A_THREAD_MAX))
    _logs(monkeypatch, {"sess-new": _Log([_Entry(1, type="session/opened", parent=None)])})
    read_thread_state("sess-new")
    assert len(tp._NOT_A_THREAD) == 1
    assert "sess-new" in tp._NOT_A_THREAD


def test_one_walk_reads_both_the_parent_edge_and_the_projection_cursor(monkeypatch):
    _logs(
        monkeypatch,
        {
            "sess-t": _Log(
                [
                    _Entry(1, type="session/opened", parent={"slot": "chat-1", "sid": "sess-p"}),
                    _Entry(2, type="thread/context_projected", cursor_seq=140, fold_generation=2),
                ]
            )
        },
    )
    state = read_thread_state("sess-t")
    assert (state.parent_slot_key, state.parent_session_id) == ("chat-1", "sess-p")
    assert (state.cursor, state.fold_generation) == (140, 2)
    assert state.first_turn is False


def test_the_last_recorded_cursor_wins_and_not_the_highest(monkeypatch):
    """A rebuild may legitimately move the cursor BACK, and the next window must
    start from where the projector last chose rather than from the high-water mark."""
    _logs(
        monkeypatch,
        {
            "sess-t": _Log(
                [
                    _Entry(1, type="session/opened", parent={"slot": "chat-1"}),
                    _Entry(2, type="thread/context_projected", cursor_seq=200),
                    _Entry(3, type="thread/context_projected", cursor_seq=150),
                ]
            )
        },
    )
    assert read_thread_state("sess-t").cursor == 150


@pytest.mark.parametrize("cursor", [True, False, "140", 1.5, None])
def test_a_cursor_that_is_not_an_integer_seq_leaves_the_thread_on_its_first_turn(
    monkeypatch, cursor
):
    """``True`` is the one that matters: it IS an int to Python, and taken as a
    cursor it would make the first turn project the single row after seq 1."""
    _logs(
        monkeypatch,
        {
            "sess-t": _Log(
                [
                    _Entry(1, type="session/opened", parent={"slot": "chat-1"}),
                    _Entry(2, type="thread/context_projected", cursor_seq=cursor),
                ]
            )
        },
    )
    assert read_thread_state("sess-t").first_turn is True


@pytest.mark.parametrize("parent", [None, "chat-1", [], {"slot": 7, "sid": 9}])
def test_a_lineage_edge_of_the_wrong_shape_reads_as_no_parent(monkeypatch, parent):
    _logs(monkeypatch, {"sess-t": _Log([_Entry(1, type="session/opened", parent=parent)])})
    state = read_thread_state("sess-t")
    assert (state.parent_slot_key, state.parent_session_id) == ("", "")


def test_a_thread_whose_own_log_faults_reads_as_having_no_parent(monkeypatch):
    """The caller's answer to a state with no parent is to inject nothing, which is
    the right answer to 'this log is unreadable' as well as to 'not a thread'."""
    _logs(monkeypatch, {"sess-t": _FaultingLog(at=1)})
    assert read_thread_state("sess-t") == ThreadState()


def test_no_session_id_reads_no_state_without_touching_a_log(monkeypatch):
    def _boom(sid):
        raise AssertionError("should not open a log for an empty session id")

    monkeypatch.setattr("kiro_crew.crew_log.projection.open_session_log", _boom)
    assert read_thread_state("") == ThreadState()


# ── the sync/async bridge, from the side that works ──


def test_the_bridge_runs_the_model_call_on_the_loop_it_was_given(monkeypatch):
    """The projector is sync so its rules are decidable without a loop; the model
    call is async. This is the one place the two meet, and it meets them by
    submitting to a loop that is free to service it because the projector runs in
    a worker thread."""
    import asyncio
    import threading

    seen: dict[str, object] = {}

    async def _fake(sessions, prompt, **kwargs):
        seen.update(prompt=prompt, **kwargs)
        return "one line about it"

    monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", _fake)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        summarize = loop_bridged_summarizer(object(), loop, crew_log_session_key="chat-9")
        assert summarize("fold these rows") == "one line about it"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()

    assert seen["prompt"] == "fold these rows"
    # Attributed to the thread's own slot, so the spend lands on the conversation
    # that caused it rather than on the parent.
    assert seen["crew_log_session_key"] == "chat-9"
    assert seen["sel_source"] == "thread_context_projection"


# ── the parent's stored summary, read through the seam ──


@pytest.mark.asyncio
async def test_the_first_band_reads_the_parents_stored_intent_summary():
    class _Log:
        def read_intent_summary(self, key):
            return {"intents": [{"title": "ship the epic", "status": "open"}]}, False

    class _State:
        conversation_log = _Log()

    band = await tp._parent_summary_band(_State(), "chat-1")
    assert "ship the epic (open)" in band


@pytest.mark.asyncio
async def test_a_stale_stored_summary_is_still_used_and_says_so():
    class _Log:
        def read_intent_summary(self, key):
            return {"intents": [{"title": "ship the epic"}]}, True

    class _State:
        conversation_log = _Log()

    assert "it has moved on since" in await tp._parent_summary_band(_State(), "chat-1")


@pytest.mark.asyncio
async def test_an_unreadable_stored_summary_costs_the_band_and_not_the_turn():
    class _Log:
        def read_intent_summary(self, key):
            raise OSError("summary file is gone")

    class _State:
        conversation_log = _Log()

    assert await tp._parent_summary_band(_State(), "chat-1") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_key", ["chat-1", ""])
async def test_no_conversation_log_or_no_parent_key_contributes_no_band(parent_key):
    class _State:
        conversation_log = None

    assert await tp._parent_summary_band(_State(), parent_key) == ""


# ── the parent's reply as it is being written ──


@pytest.mark.parametrize(
    "snapshot,running,expected",
    [
        ("half an answer", True, ("half an answer", True)),
        # Mid-turn with nothing readable yet: still PARTIAL, because reporting
        # partial=False here would claim the parent had finished.
        ("", True, ("", True)),
        ("", False, ("", False)),
    ],
)
def test_a_streaming_parent_is_read_without_consuming_its_chunks(
    monkeypatch, snapshot, running, expected
):
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_threads.in_flight_snapshot", lambda slot: snapshot
    )

    class _Slot:
        pass

    slot = _Slot()
    slot.running = running
    assert tp._parent_live_text(slot) == expected


def test_live_text_that_cannot_be_read_reports_an_idle_parent(monkeypatch):
    def _boom(slot):
        raise RuntimeError("slot messages are locked")

    monkeypatch.setattr("kiro_crew.dashboard.chat_threads.in_flight_snapshot", _boom)
    assert tp._parent_live_text(object()) == ("", False)


# ── the anchor message's own text, from the parent's transcript ──


@pytest.mark.asyncio
async def test_the_anchor_band_text_comes_from_the_row_carrying_that_mid(monkeypatch):
    rows = [
        {"content": "an earlier line", "meta": {"mid": "mid-1"}},
        {"content": "the anchored question", "meta": {"mid": "mid-2"}},
    ]

    async def _transcript(state, slot):
        return rows

    monkeypatch.setattr("kiro_crew.dashboard.chat_threads._transcript", _transcript)
    assert await tp._anchor_text(object(), object(), "mid-2") == "the anchored question"


@pytest.mark.asyncio
async def test_an_anchor_whose_row_is_gone_yields_no_text_and_the_mid_is_named_instead(
    monkeypatch,
):
    async def _transcript(state, slot):
        return [{"content": "something else", "meta": {"mid": "mid-1"}}]

    monkeypatch.setattr("kiro_crew.dashboard.chat_threads._transcript", _transcript)
    assert await tp._anchor_text(object(), object(), "mid-gone") == ""


@pytest.mark.asyncio
async def test_an_unreadable_parent_transcript_yields_no_anchor_text(monkeypatch):
    async def _transcript(state, slot):
        raise OSError("transcript file is gone")

    monkeypatch.setattr("kiro_crew.dashboard.chat_threads._transcript", _transcript)
    assert await tp._anchor_text(object(), object(), "mid-2") == ""


# ── the whole turn, end to end ──

THREAD_SID = "sess-thread"
PARENT_SID = "sess-parent"


class _TurnSlot:
    def __init__(self, key, sid, *, running=False):
        self.key = key
        self.running = running
        self._acp_client = sid
        self.pending: list[dict] = []

    def append_pending_context(self, entry):
        self.pending.append(entry)


class _TurnLog:
    def __init__(self, anchors, *, anchors_raise=False):
        self._anchors = anchors
        self._anchors_raise = anchors_raise
        self.anchor_keys: list[str] = []

    def read_thread_anchors(self, key):
        if self._anchors_raise:
            raise OSError("anchor index is gone")
        self.anchor_keys.append(key)
        return self._anchors

    def read_intent_summary(self, key):
        return None, False


class _TurnState:
    def __init__(self, slots, log):
        self._slots = {slot.key: slot for slot in slots}
        self.conversation_log = log
        self.sessions = object()

    def get_slot(self, key):
        return self._slots.get(key)


def _turn(
    monkeypatch,
    *,
    anchors=None,
    cursor=None,
    last_seq=200,
    rows=None,
    parent_slot_key="chat-parent",
    tail_raises=False,
    window_raises=False,
    anchors_raise=False,
    conversation_log=True,
    parent_present=True,
    live_text="",
    cursor_write="landed",
):
    """Wire every seam ``project_for_turn`` reaches, and hand back what it did."""
    thread_slot = _TurnSlot("chat-thread", THREAD_SID)
    parent_slot = _TurnSlot("chat-parent", PARENT_SID)
    anchors = (
        {"mid-2": {"thread_slot": "chat-thread", "parent_log_seq": 100}}
        if anchors is None
        else anchors
    )
    log = _TurnLog(anchors, anchors_raise=anchors_raise)
    slots = [thread_slot] + ([parent_slot] if parent_present else [])
    state = _TurnState(slots, log if conversation_log else None)
    if not conversation_log:
        state.conversation_log = None

    monkeypatch.setattr(
        "kiro_crew.crew_log.emit.session_id_of", lambda client: client if client else ""
    )
    monkeypatch.setattr(
        tp,
        "read_thread_state",
        lambda sid: ThreadState(
            parent_slot_key=parent_slot_key, parent_session_id=PARENT_SID, cursor=cursor
        ),
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.slot_history_key", lambda slot: slot.key)
    monkeypatch.setattr(tp, "loop_bridged_summarizer", lambda *a, **k: echo)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_threads.in_flight_snapshot", lambda slot: live_text
    )

    async def _transcript(state_, slot_):
        return [{"content": "the anchored question", "meta": {"mid": "mid-2"}}]

    monkeypatch.setattr("kiro_crew.dashboard.chat_threads._transcript", _transcript)

    def _read_page(sid, start, end):
        if end == 0:
            if tail_raises:
                raise OSError("parent log tail unreadable")
            return {"last_seq": last_seq}
        if window_raises:
            raise OSError("parent window unreadable")
        return {"entries": list(rows or [])}

    monkeypatch.setattr("kiro_crew.crew_log.read.read_page", _read_page)

    emitted: list[dict] = []

    def _emit(sid, **kw):
        emitted.append({"sid": sid, **kw})
        # Stand in for the writer: the entry lands, so the drop hook does not run
        # and `after` does. `cursor_write` picks a different outcome.
        if cursor_write == "dropped" and kw.get("on_permanent_drop") is not None:
            kw["on_permanent_drop"]()
        if cursor_write != "stalled" and kw.get("after") is not None:
            kw["after"]()

    monkeypatch.setattr("kiro_crew.crew_log.emit.on_thread_context_projected", _emit)
    return thread_slot, state, emitted


@pytest.mark.asyncio
async def test_a_first_turn_queues_the_block_before_the_turn_reads_the_queue(monkeypatch):
    slot, state, emitted = _turn(
        monkeypatch, last_seq=104, rows=[row(seq, text=f"line {seq}") for seq in range(100, 105)]
    )
    projection = await project_for_turn(state, slot)
    assert projection is not None and projection.empty is False
    entry = slot.pending[0]
    assert entry["source"] == tp.CONTEXT_SOURCE
    assert "the anchored question" in entry["content"]
    assert "thread_context_read" in entry["content"]
    assert entry["cursor_seq"] == 104


@pytest.mark.asyncio
async def test_the_provenance_row_names_the_anchor_and_the_cursor_it_committed(monkeypatch):
    slot, state, emitted = _turn(
        monkeypatch, last_seq=104, rows=[row(seq, text=f"line {seq}") for seq in range(100, 105)]
    )
    await project_for_turn(state, slot)
    assert len(emitted) == 1
    written = emitted[0]
    assert written["sid"] == THREAD_SID
    assert written["anchor"] == {
        "surface": "dashboard",
        "conversation": "chat-parent",
        "mid": "mid-2",
    }
    assert written["cursor_seq"] == 104
    assert written["rows"] == 5


@pytest.mark.asyncio
async def test_a_quiet_turn_still_writes_its_provenance_row(monkeypatch):
    """What makes a turn the projector ran on and found nothing distinguishable
    from a turn it never ran on at all."""
    slot, state, emitted = _turn(monkeypatch, cursor=200, last_seq=200)
    projection = await project_for_turn(state, slot)
    assert projection is not None and projection.empty is True
    assert slot.pending == []
    assert len(emitted) == 1 and emitted[0]["rows"] == 0


@pytest.mark.asyncio
async def test_a_later_turn_projects_the_delta_and_names_no_anchor_band(monkeypatch):
    slot, state, emitted = _turn(
        monkeypatch,
        cursor=140,
        last_seq=143,
        rows=[row(seq, text=f"line {seq}") for seq in range(141, 144)],
    )
    projection = await project_for_turn(state, slot)
    assert projection is not None
    assert "The message this thread was opened on:" not in projection.text
    assert projection.cursor_seq == 143


@pytest.mark.asyncio
async def test_a_window_the_projector_asked_for_and_did_not_get_publishes_nothing(monkeypatch):
    slot, state, emitted = _turn(monkeypatch, cursor=140, last_seq=160, window_raises=True)
    projection = await project_for_turn(state, slot)
    assert projection is not None and projection.empty is True
    assert slot.pending == []
    # Held, so the next turn asks for the same window again.
    assert emitted[0]["cursor_seq"] == 140


@pytest.mark.asyncio
async def test_an_unreadable_parent_tail_costs_the_delta_and_not_the_turn(monkeypatch):
    """A tail nobody could read leaves the window empty rather than guessing an
    end, and the turn runs with no block."""
    slot, state, emitted = _turn(monkeypatch, cursor=140, tail_raises=True)
    projection = await project_for_turn(state, slot)
    assert projection is not None and projection.empty is True
    assert slot.pending == []


@pytest.mark.asyncio
async def test_a_parent_still_writing_its_reply_makes_the_projection_partial(monkeypatch):
    """The log gets one ``message/sent`` when the turn ENDS, so the window cannot
    see the very reply the thread was opened beside."""
    slot, state, emitted = _turn(
        monkeypatch,
        last_seq=104,
        rows=[row(seq, text=f"line {seq}") for seq in range(100, 105)],
        live_text="the answer so far",
    )
    projection = await project_for_turn(state, slot)
    assert projection is not None and projection.partial is True
    assert "the answer so far" in slot.pending[0]["content"]
    assert emitted[0]["partial"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"parent_present": False},  # the parent conversation was deleted
        {"conversation_log": False},  # transcripts are off
        {"anchors_raise": True},  # the anchor index is unreadable
        {"anchors": {}},  # no anchor names this slot
        {"anchors": {"mid-2": {"thread_slot": "chat-other"}}},  # it names another
    ],
)
async def test_a_thread_whose_anchor_cannot_be_found_runs_its_turn_with_no_block(
    monkeypatch, kwargs
):
    slot, state, emitted = _turn(monkeypatch, **kwargs)
    assert await project_for_turn(state, slot) is None
    assert slot.pending == []
    assert emitted == []


def test_a_thread_with_no_log_of_its_own_reads_as_having_no_parent(monkeypatch):
    """A gateway that never wrote the thread's log, or one turned off entirely: the
    thread is then indistinguishable from an ordinary chat, and gets no block."""
    _logs(monkeypatch, {})
    assert read_thread_state("sess-t") == ThreadState()


def test_an_intent_of_the_wrong_shape_is_skipped_rather_than_stringified():
    payload = {"intents": ["ship the epic", None, 7, {"title": "the real one"}]}
    assert render_parent_summary(payload) == (
        "What that conversation has been about:\n- the real one"
    )


# ── the cursor is persisted before the block is published ──
#
# The cursor in `thread/context_projected` is the durable authority the next
# window starts from, and the emitter hands its append to a buffered writer that
# returns without writing. A block queued BEFORE that entry lands can therefore be
# drained into this turn while its cursor is still in the buffer -- and if the
# write is then lost, the next turn reads the old cursor and re-injects rows the
# thread has already been told. So publication rides the write's own outcome.


@pytest.mark.asyncio
async def test_the_block_is_queued_only_once_its_cursor_entry_has_landed(monkeypatch):
    rows = [row(seq, text=f"line {seq}") for seq in range(100, 105)]
    slot, state, emitted = _turn(monkeypatch, last_seq=104, rows=rows)

    # Ordering is observable: the entry is submitted with BOTH hooks, and the queue
    # is written from the hook rather than before the submit.
    projection = await project_for_turn(state, slot)
    assert projection is not None and projection.empty is False
    assert emitted[0]["after"] is not None
    assert emitted[0]["on_permanent_drop"] is not None
    assert len(slot.pending) == 1
    assert emitted[0]["cursor_seq"] == 104


@pytest.mark.asyncio
async def test_a_cursor_the_writer_gave_up_on_publishes_nothing(monkeypatch):
    """The case the ordering exists for. Publishing here would put a summary in
    front of the model against a cursor that never landed, and the next turn --
    reading the old cursor -- would summarize the same rows again."""
    rows = [row(seq, text=f"line {seq}") for seq in range(100, 105)]
    slot, state, emitted = _turn(monkeypatch, last_seq=104, rows=rows, cursor_write="dropped")

    projection = await project_for_turn(state, slot)
    # The projection itself is still real -- the block was built and the cursor was
    # offered to the log. What did not happen is the publish.
    assert projection is not None and projection.empty is False
    assert slot.pending == []


@pytest.mark.asyncio
async def test_a_writer_that_has_not_settled_does_not_hang_the_turn(monkeypatch):
    """A wedged disk costs the thread its block on THIS turn, never the turn. The
    hook still fires when the entry lands, so the block rides a later turn against
    a cursor that already accounts for those rows -- told once either way."""
    import kiro_crew.dashboard.thread_projection as tp_mod

    monkeypatch.setattr(tp_mod, "CURSOR_SETTLE_TIMEOUT_S", 0.05)
    rows = [row(seq, text=f"line {seq}") for seq in range(100, 105)]
    slot, state, emitted = _turn(monkeypatch, last_seq=104, rows=rows, cursor_write="stalled")

    projection = await project_for_turn(state, slot)
    assert projection is not None and projection.empty is False
    assert slot.pending == []
    # And the hook the writer still holds publishes when it eventually lands.
    emitted[0]["after"]()
    await asyncio.sleep(0)
    assert len(slot.pending) == 1


@pytest.mark.asyncio
async def test_a_quiet_turn_waits_on_its_cursor_and_queues_nothing(monkeypatch):
    """`rows=0` still records the cursor, so the wait is the same; there is simply
    no block for the hook to publish."""
    slot, state, emitted = _turn(monkeypatch, cursor=200, last_seq=200)
    projection = await project_for_turn(state, slot)
    assert projection is not None and projection.empty is True
    assert slot.pending == []
    assert len(emitted) == 1 and emitted[0]["rows"] == 0


# ── the parent edge is durable before the first turn reads it ──
#
# A thread's parent edge lives in `session/opened.parent.slot`, and the emitter
# hands that append to a buffered writer. `project_for_turn` reads it back off
# disk, so on a FIRST turn the read can beat the write: `read_thread_state` then
# answers with no parent, the projector reads that as "not a thread", and the
# reply carries no account of the conversation it was opened on. Silently, and a
# turn late to recover, because no cursor is recorded either.


def test_the_session_opened_settle_hook_fires_even_with_the_crew_log_off(monkeypatch):
    """The whole reason a caller may WAIT on it. `on_session_opened` returns early
    when there is no session id or the log is off, and a hook not called on those
    paths would make every such turn wait out its full bound for an entry that was
    never going to be written."""
    from kiro_crew.crew_log import emit as crew_log_emit

    monkeypatch.setattr(crew_log_emit, "enabled", lambda: False)
    fired: list[str] = []
    crew_log_emit.on_session_opened("sess-x", after=lambda: fired.append("off"))
    assert fired == ["off"]

    # And with the log on, an empty session id takes the same path.
    monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)
    crew_log_emit.on_session_opened("", after=lambda: fired.append("no-sid"))
    assert fired == ["off", "no-sid"]


def test_the_lineage_bound_is_shorter_than_the_cursor_bound():
    """They sit on opposite sides of the turn: the lineage wait blocks BEFORE any
    work is done, so it is the one that must stay small; the cursor wait happens
    after the block is built and only delays publishing it."""
    assert tp.LINEAGE_SETTLE_TIMEOUT_S < tp.CURSOR_SETTLE_TIMEOUT_S
    assert tp.LINEAGE_SETTLE_TIMEOUT_S > 0


def test_a_thread_whose_parent_edge_has_not_landed_reads_as_no_parent(monkeypatch):
    """What the wait exists to prevent, stated as the projector's own behaviour: an
    unwritten edge is indistinguishable from an ordinary chat down here, which is
    why the ordering has to be fixed at the writer rather than guessed at here."""
    _logs(monkeypatch, {"sess-t": _Log([])})  # opened, but nothing appended yet
    assert read_thread_state("sess-t") == ThreadState()
