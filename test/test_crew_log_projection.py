"""Crew-log projections -- one test per property the folds promise.

The load-bearing one is :func:`test_incremental_matches_from_scratch_at_every_split`:
a projection resumed from a checkpoint must equal the same projection folded from
the start of the file, at EVERY split point. That is what makes a checkpoint safe
to store and a push safe to send incrementally, and it is the property a second
batch implementation would be free to break.

This module pins the fold contract and the status, usage, timeline, tools and approvals
folds. Reading a session (``fold_session``, bundle reuse, the order a slot's units fold in)
lives in ``test_crew_log_projection_session_reads.py``, the bounds on what a fold retains in
``test_crew_log_projection_bounds.py``, and the ``class`` fold in
``test_crew_log_projection_class.py``; the shared logs, entries and fixture writers in
``crew_log_projection_helpers.py``.
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

import pytest
from crew_log_projection_helpers import (  # noqa: F401 -- fixtures are used by name
    GATEWAY,
    SESSION,
    _append_grouped,
    _busy_log,
    _entries,
    _isolated_home,
    _log,
    _opened,
    _tool,
    _tool_items,
    _turn,
)

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLogError
from kiro_crew.crew_log import projection as crew_log

# --- the contract ---------------------------------------------------------


def test_the_class_fold_is_registered_but_not_advertised():
    """MUTATION-SENSITIVE: the class fold is machinery, not a panel value.

    It must stay REGISTERED, because its one caller asks the registry for it by name
    and that is what gives it the shared checkpoint, the incremental reuse and the
    recreated-log guard. It must stay OUT of the advertised set, because nothing on a
    client draws it: naming it there makes the growth push ship a frame per log growth
    to every owner socket for no reader, and advertises a projection with no consumer.
    Both halves are asserted, so neither drifts without this failing.
    """
    from kiro_crew import mcp_crew_log

    assert "class" in crew_log.FOLD_NAMES
    assert "class" in crew_log.INTERNAL_PROJECTION_NAMES
    assert "class" not in crew_log.PROJECTION_NAMES
    assert "class" not in mcp_crew_log.PROJECTION_NAMES


@pytest.mark.parametrize("name", crew_log.PROJECTION_NAMES)
def test_incremental_matches_from_scratch_at_every_split(name):
    """Resuming from a checkpoint equals folding the whole file, at every split."""
    entries = _entries(_busy_log())
    whole = crew_log.fold(name, entries)
    for cut in range(len(entries) + 1):
        first = crew_log.advance(crew_log.initial(name), entries[:cut])
        resumed = crew_log.advance(first, entries[cut:])
        assert crew_log.projection_of(resumed).value == whole, f"{name} disagrees at cut {cut}"
        assert resumed.last_seq == (entries[-1].seq if entries else 0)


@pytest.mark.parametrize("name", crew_log.SESSION_FOLD_NAMES)
def test_the_kernel_definition_folds_to_what_advance_folds(name):
    """The wrapper the kernel drives and the ``advance`` surface reach one value.

    ``advance`` deep-copies once and steps the whole span; the kernel drives one entry
    at a time through a copy the fold declared. Two ways of arriving at the same fold,
    so they are pinned against each other -- a copier or an ``affects`` set that was
    wrong would show up here as a different number.
    """
    entries = _entries(_busy_log())
    definition = crew_log._SessionFold(crew_log._FOLDS[name])
    state = definition.init()
    for entry in entries:
        state = definition.apply(state, entry)

    assert definition.view(state) == crew_log.fold(name, entries)
    # The version the kernel is handed is THIS fold's, not the module maximum: a bump
    # to one fold must retire that fold's savepoints and leave the others standing, and
    # comparing against the maximum here would pass while the wrapper published the
    # wrong number for every fold below it.
    assert definition.state_version == crew_log.fold_state_version(name)


@pytest.mark.parametrize("name", crew_log.SESSION_FOLD_NAMES)
def test_a_fold_never_reaches_into_the_state_it_was_handed(name):
    """MUTATION-SENSITIVE: ``apply`` copies before it steps, so its input is intact.

    This is what keeps each fold's declared ``copy_state`` honest. A copier that misses
    a nested container still produces the RIGHT value -- the step edits the object it
    meant to edit -- but it edits the caller's state along with it, and a caller holding
    an earlier bundle would then watch its value move underneath it. Nothing raises
    either way, so this test is the only thing that catches the miss.

    Checked at every position, because the container a copier misses often does not
    exist until the fold has something in it: ``tools`` has no per-name row to share
    until a tool has been called.
    """
    entries = _entries(_busy_log())
    definition = crew_log._SessionFold(crew_log._FOLDS[name])
    state = definition.init()
    for entry in entries:
        before = copy.deepcopy(state)
        grown = definition.apply(state, entry)
        assert state == before, f"{name} edited the state it was handed at seq {entry.seq}"
        state = grown


@pytest.mark.parametrize("name", crew_log.SESSION_FOLD_NAMES)
def test_an_entry_a_fold_declares_untouched_really_moves_nothing(name):
    """MUTATION-SENSITIVE: ``affects`` may be wider than the truth, never narrower.

    Returning the state unchanged is how ``apply`` tells the kernel this entry moved
    nothing, and the kernel believes it: a type wrongly left out of ``affects`` drops a
    real change and serves a stale value for as long as the fold sits there. So for
    every entry the wrapper skipped, the step is run on a copy to prove it would indeed
    have changed nothing.

    The opposite error is not a bug and is not asserted against: a type wrongly
    INCLUDED costs a copy, and a spurious frame to a client watching the change feed.
    """
    entries = _entries(_busy_log())
    fold_spec = crew_log._FOLDS[name]
    definition = crew_log._SessionFold(fold_spec)
    state = definition.init()
    skipped = 0
    for entry in entries:
        grown = definition.apply(state, entry)
        if grown is state:
            skipped += 1
            probe = fold_spec.copied(state)
            fold_spec.step(probe, entry)
            assert probe == state, (
                f"{name} treats {entry.type} as untouched, but its step moves the "
                f"state at seq {entry.seq}"
            )
        state = grown
    # Only a NARROWED set can skip anything. ``status`` and ``class`` declare the whole
    # vocabulary -- ``KNOWN_TYPES`` -- because every entry moves them, and that is a
    # statement about them rather than a set left undeclared: a fold whose ``affects`` is
    # the vocabulary skips nothing, and there is nothing for it to prove here.
    if fold_spec.affects is not None and fold_spec.affects != crew_log.KNOWN_TYPES:
        assert skipped > 0, f"{name} declares a narrowed type set but skipped nothing"


@pytest.mark.parametrize("name", crew_log.PROJECTION_NAMES)
def test_checkpoint_survives_a_json_round_trip_and_keeps_folding(name):
    """A checkpoint written down and read back continues to the same answer."""
    entries = _entries(_busy_log())
    cut = len(entries) // 2
    part = crew_log.advance(crew_log.initial(name), entries[:cut])
    assert crew_log.state_is_serializable(part)
    stored = json.loads(json.dumps(part.to_dict()))
    restored = crew_log.Checkpoint.from_dict(stored)
    assert crew_log.projection_of(crew_log.advance(restored, entries[cut:])).value == crew_log.fold(
        name, entries
    )


@pytest.mark.parametrize("name", crew_log.PROJECTION_NAMES)
def test_advance_leaves_its_input_checkpoint_untouched(name):
    """The older checkpoint still renders the value it was handed."""
    entries = _entries(_busy_log())
    cut = len(entries) // 2
    part = crew_log.advance(crew_log.initial(name), entries[:cut])
    before = crew_log.projection_of(part).value
    crew_log.advance(part, entries[cut:])
    assert crew_log.projection_of(part).value == before
    assert part.last_seq == entries[cut - 1].seq


@pytest.mark.parametrize("name", crew_log.PROJECTION_NAMES)
def test_refolding_an_entry_the_checkpoint_already_saw_is_refused(name):
    """A replayed entry raises rather than double-counting or being skipped."""
    entries = _entries(_busy_log())
    part = crew_log.advance(crew_log.initial(name), entries[:4])
    with pytest.raises(CrewLogError) as excinfo:
        crew_log.advance(part, entries[2:])
    assert excinfo.value.code == lg.CODE_BAD_DATA


def test_projection_seq_is_the_seq_it_folded_through():
    entries = _entries(_busy_log())
    result = crew_log.projection_of(crew_log.advance(crew_log.initial("usage"), entries))
    assert result.seq == entries[-1].seq
    assert result.to_dict()["name"] == "usage"


def test_an_unknown_projection_name_is_refused():
    for call in (
        lambda: crew_log.initial("board"),
        lambda: crew_log.fold("board", ()),
        lambda: crew_log.read_projection(SESSION, "board"),
    ):
        with pytest.raises(CrewLogError) as excinfo:
            call()
        assert excinfo.value.code == lg.CODE_BAD_DATA


# --- status ---------------------------------------------------------------


def test_status_reports_an_open_turn_rather_than_closing_it():
    handle = _log()
    _opened(handle)
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src=GATEWAY)
    value = crew_log.fold_status(_entries(handle))
    assert value["turn_open"] is True
    assert value["turn"]["turn"] == 1
    assert value["turns_completed"] == 0
    assert value["last_stop_reason"] is None
    assert value["lifecycle"] == "open"


def test_status_carries_the_attempt_from_the_start_entry():
    """A rerun at one ordinal is told apart by ``attempt``, which only the start carries."""
    handle = _log()
    _opened(handle)
    handle.append(
        "turn/started", {"turn": 7, "actor": "user", "depth": 0, "attempt": 2}, src=GATEWAY
    )
    assert crew_log.fold_status(_entries(handle))["turn"]["attempt"] == 2


def test_status_defaults_a_missing_attempt_to_one():
    handle = _log()
    _opened(handle)
    handle.append("turn/started", {"turn": 7, "actor": "user", "depth": 0}, src=GATEWAY)
    assert crew_log.fold_status(_entries(handle))["turn"]["attempt"] == 1


def test_status_reports_a_reopened_session_as_open_again():
    handle = _log()
    _opened(handle)
    first_open = crew_log.fold_status(_entries(handle))["opened_at"]
    handle.append("session/closed", {"reason": "reset"}, src=GATEWAY)
    assert crew_log.fold_status(_entries(handle))["lifecycle"] == "closed"
    _opened(handle, resumed=True, model="sonnet")
    value = crew_log.fold_status(_entries(handle))
    assert value["lifecycle"] == "open"
    assert value["resumed"] is True
    assert value["close_reason"] is None
    assert value["model"] == "sonnet"
    assert value["opened_at"] == first_open


def test_status_without_an_opener_is_unknown_not_open():
    """Retention can remove the segment that carried ``session/opened``."""
    handle = _log()
    handle.append(
        "turn/refused",
        {"turn": 1, "actor": "cron", "reason": "not_authorized", "depth": 0},
        src=GATEWAY,
    )
    value = crew_log.fold_status(_entries(handle))
    assert value["lifecycle"] == "unknown"
    assert value["turns_refused"] == 1


def test_status_totals_dropped_writes():
    handle = _log()
    _opened(handle)
    handle.append("write/dropped", {"dropped_count": 2, "dropped_bytes": 10}, src=GATEWAY)
    handle.append("write/dropped", {"dropped_count": 3, "dropped_bytes": 20}, src=GATEWAY)
    assert crew_log.fold_status(_entries(handle))["dropped"] == {"count": 5, "bytes": 30}


# --- usage ----------------------------------------------------------------


def test_usage_does_not_read_absent_credits_as_zero():
    """A synthesized closer reports no cost; a total must not claim it measured one."""
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=0.25)
    _turn(handle, 2, credits=None)
    value = crew_log.fold_usage(_entries(handle))
    assert value["turns"]["completed"] == 2
    assert value["turns"]["credits_reported"] == 1
    assert value["credits"] == 0.25
    assert value["turns"]["tokens_reported"] == 1


def test_usage_sums_every_token_dimension_and_its_total():
    handle = _log()
    _opened(handle)
    _turn(handle, 1, tokens={"input": 10, "output": 2, "cache_read": 3, "cache_write": 4})
    _turn(handle, 2, tokens={"input": 20, "output": 5, "cache_read": 0, "cache_write": 1})
    tokens = crew_log.fold_usage(_entries(handle))["tokens"]
    assert tokens == {
        "input": 30,
        "output": 7,
        "cache_read": 3,
        "cache_write": 5,
        "total": 45,
    }


def test_usage_does_not_count_an_all_zero_tokens_block_as_reported():
    """A block of four zeros is an absence written as a block.

    Logs on disk carry it on every turn whose provider sent no counts, so the fold --
    not only the writer -- has to know that a completed turn cannot have cost zero
    tokens. The credits beside it were genuinely billed and stay counted, which is
    the difference a reader sees: a real bill beside ``tokens_reported: 0``, and the
    panel dashes the tokens instead of printing a measured ``0``.
    """
    handle = _log()
    _opened(handle)
    for turn in range(1, 5):
        _turn(
            handle,
            turn,
            credits=0.3725,
            tokens={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        )
    value = crew_log.fold_usage(_entries(handle))
    assert value["turns"]["completed"] == 4
    assert value["turns"]["credits_reported"] == 4
    assert value["credits"] == 1.49
    assert value["turns"]["tokens_reported"] == 0
    assert value["tokens"]["total"] == 0


def test_usage_counts_a_block_reported_once_any_dimension_is_above_zero():
    """One measured dimension is a report; the rule is not "input above zero"."""
    handle = _log()
    _opened(handle)
    _turn(handle, 1, tokens={"input": 0, "output": 0, "cache_read": 7, "cache_write": 0})
    _turn(handle, 2, tokens={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
    _turn(handle, 3, tokens={"input": 5, "output": 1, "cache_read": 0, "cache_write": 0})
    value = crew_log.fold_usage(_entries(handle))
    assert value["turns"]["completed"] == 3
    assert value["turns"]["tokens_reported"] == 2
    assert value["tokens"]["total"] == 13
    assert value["by_model"]["opus"] == {
        "turns": 3,
        "credits": 1.5,
        "credits_reported": 3,
        "tokens": 13,
    }


def test_usage_still_counts_a_recorded_zero_credit_charge_as_reported():
    """Pins what the token rule deliberately leaves alone.

    ``credits: 0.0`` on a closer is counted as a report, and the fold keeps doing so:
    the writer omits an unbilled charge, but a zero already on disk cannot be told
    from a turn a provider genuinely billed at nothing, and ``_bill_credits`` lets a
    zero through on purpose. Tokens are different -- a completed turn cannot cost
    zero of them -- which is why only ``tokens_reported`` carries the rule.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=0.0)
    value = crew_log.fold_usage(_entries(handle))
    assert value["turns"]["credits_reported"] == 1
    assert value["credits_by_source"]["turn"]["reported"] == 1
    assert value["credits"] == 0.0


def test_usage_bills_injected_context_per_source():
    handle = _log()
    _opened(handle)
    handle.append(
        "context/composed",
        {
            "turn": 1,
            "sources": [
                {"kind": "system", "chars": 100, "tokens": 25},
                {"kind": "memory", "chars": 40, "tokens": 10},
            ],
            "chars": 140,
            "tokens": 35,
            "tokens_estimated": True,
        },
        src=GATEWAY,
    )
    handle.append(
        "context/composed",
        {
            "turn": 2,
            "sources": [{"kind": "memory", "chars": 60, "tokens": 15}],
            "chars": 60,
            "tokens": 15,
            "tokens_estimated": False,
        },
        src=GATEWAY,
    )
    context = crew_log.fold_usage(_entries(handle))["context"]
    assert context["tokens"] == 50
    assert context["estimated_turns"] == 1
    assert context["by_source"]["memory"] == {"blocks": 2, "tokens": 25, "chars": 100}
    assert context["by_source"]["system"] == {"blocks": 1, "tokens": 25, "chars": 100}
    assert context["sources_omitted"] == 0


def test_usage_counts_the_sources_a_row_leaves_out():
    """A row past the per-turn source cap says so, and keeps adding to labels it holds.

    Two NEW labels past the cap are left out and counted. A label the row already
    holds, arriving again at the cap, still adds to that label rather than being
    dropped, so a kept figure is never cut short.
    """
    limit = crew_log.CONTEXT_SOURCES_PER_TURN_LIMIT
    sources = [{"kind": f"src{i}", "chars": 1, "tokens": 0} for i in range(limit + 2)]
    sources.append({"kind": "src0", "chars": 5, "tokens": 0})
    handle = _log()
    _opened(handle)
    handle.append(
        "context/composed",
        {"turn": 1, "sources": sources, "chars": limit + 7, "tokens": 0, "tokens_estimated": True},
        src=GATEWAY,
    )
    context = crew_log.fold_usage(_entries(handle))["context"]
    (row,) = context["turns"]
    assert len(row["sources"]) == limit
    assert row["sources"]["src0"] == 6
    assert context["sources_omitted"] == 2


def test_usage_a_retry_attempts_reading_does_not_stamp_the_first_attempts_rows():
    """MUTATION-SENSITIVE: a rerun of one turn ordinal is a separate attempt.

    A regenerate or rewind reruns a turn the ordinal already names, so one ordinal can
    carry two attempts, each with its own compositions and its own closer. Matching a
    reading to rows by TURN NUMBER alone conflates them: attempt 2's closer walking the
    tail back would reach attempt 1's rows -- same ordinal -- and stamp them with
    attempt 2's occupancy. It bites hardest when attempt 1 reported NO occupancy, so
    its rows are unstamped and nothing else marks them closed. Each closer sealing its
    own run is what keeps the two apart, whether or not attempt 1 measured anything.
    """

    def _completed(reading: dict[str, int] | None) -> dict[str, Any]:
        done: dict[str, Any] = {
            "turn": 1,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 100,
            "model": "opus",
            "provider": "kiro",
            "credits": 0.1,
            "tokens": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0},
        }
        if reading is not None:
            done["context"] = reading
        return done

    def _composed(kind: str, chars: int) -> dict[str, Any]:
        return {
            "turn": 1,
            "sources": [{"kind": kind, "chars": chars, "tokens": chars // 4}],
            "chars": chars,
            "tokens": chars // 4,
            "tokens_estimated": True,
        }

    handle = _log()
    _opened(handle)
    # Attempt 1 at ordinal 1 composes, then completes WITHOUT an occupancy reading.
    handle.append("context/composed", _composed("memory", 40), src=GATEWAY)
    handle.append("turn/completed", _completed(None), src=GATEWAY)
    # Attempt 2 reruns ordinal 1, composes fresh rows, and completes WITH a reading.
    handle.append("context/composed", _composed("system", 90), src=GATEWAY)
    handle.append(
        "turn/started", {"turn": 1, "actor": "user", "depth": 0, "attempt": 2}, src=GATEWAY
    )
    handle.append("turn/completed", _completed({"used": 8_000, "window": 200_000}), src=GATEWAY)
    turns = crew_log.fold_usage(_entries(handle))["context"]["turns"]
    # Attempt 1's row keeps NO reading; only attempt 2's row carries the 8_000. The
    # reading did not walk back across the attempt boundary onto the earlier run.
    assert [("used" in row) for row in turns] == [False, True]
    assert [row.get("used") for row in turns] == [None, 8_000]


def test_usage_each_attempt_of_one_turn_keeps_its_own_reading():
    """MUTATION-SENSITIVE: two attempts that BOTH measured keep their own readings.

    When each attempt reports occupancy the seal still matters: attempt 2's closer
    must not overwrite attempt 1's already-stamped reading, and attempt 1's must not be
    left for attempt 2 to claim.
    """
    handle = _log()
    _opened(handle)
    handle.append(
        "context/composed",
        {
            "turn": 1,
            "sources": [{"kind": "memory", "chars": 40, "tokens": 10}],
            "chars": 40,
            "tokens": 10,
            "tokens_estimated": True,
        },
        src=GATEWAY,
    )
    _turn(handle, 1)  # the helper's closer carries a used=4_200 reading
    handle.append(
        "context/composed",
        {
            "turn": 1,
            "sources": [{"kind": "system", "chars": 90, "tokens": 22}],
            "chars": 90,
            "tokens": 22,
            "tokens_estimated": True,
        },
        src=GATEWAY,
    )
    handle.append(
        "turn/started", {"turn": 1, "actor": "user", "depth": 0, "attempt": 2}, src=GATEWAY
    )
    handle.append(
        "turn/completed",
        {
            "turn": 1,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 100,
            "model": "opus",
            "provider": "kiro",
            "credits": 0.1,
            "tokens": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0},
            "context": {"used": 8_000, "window": 200_000},
        },
        src=GATEWAY,
    )
    turns = crew_log.fold_usage(_entries(handle))["context"]["turns"]
    assert [row.get("used") for row in turns] == [4_200, 8_000]


def test_usage_snapshot_row_is_not_stamped_by_a_later_completion():
    """MUTATION-SENSITIVE: a copied snapshot's rows are independent of the base's.

    The ``turn/completed`` closer stamps the provider's occupancy reading onto the
    rows of the turn it closes, AFTER those rows were appended. ``copy_state`` ships a
    snapshot of the fold to socket owners, so if that snapshot shared the row objects
    a later completion would stamp a reading into a snapshot a reader is still holding
    -- an occupancy from a turn the snapshot was taken BEFORE. The copy must be deep
    enough at the row level that the base can be stamped without touching it.
    """
    from kiro_crew.crew_log.schema import Entry

    state = crew_log._usage_start()
    crew_log._usage_step(
        state,
        Entry(
            seq=1,
            time=1000,
            type="request/configured",
            src=GATEWAY,
            data={"turn": 1, "context_window": 200_000},
        ),
    )
    crew_log._usage_step(
        state,
        Entry(
            seq=2,
            time=1001,
            type="context/composed",
            src=GATEWAY,
            data={"turn": 1, "sources": [{"kind": "memory", "chars": 40}], "chars": 40},
        ),
    )
    snapshot = crew_log._usage_copy(state)
    assert "used" not in snapshot["context_turns"][0]
    # The turn now closes with a reading. It must land on the base state's row only.
    crew_log._usage_step(
        state,
        Entry(
            seq=3,
            time=1002,
            type="turn/completed",
            src=GATEWAY,
            data={"turn": 1, "context": {"used": 9_000, "window": 200_000}},
        ),
    )
    assert state["context_turns"][0]["used"] == 9_000
    assert "used" not in snapshot["context_turns"][0]


def test_usage_the_ordinal_memo_stays_bounded_over_a_long_session():
    """MUTATION-SENSITIVE: the (unit, turn) memo must not grow without bound.

    The memo lets a rewound turn reuse its ordinal, but it is checkpointed state, so it
    is capped at ``CONTEXT_ORDINAL_MEMO_LIMIT`` keys and evicts oldest-first. Driving
    more distinct turns than the cap must leave it at the cap, never larger -- while the
    counter keeps advancing so each new turn still gets its true session-global ordinal
    (the cap bounds the memory, not the numbering).
    """
    state = crew_log._usage_start()
    limit = crew_log.CONTEXT_ORDINAL_MEMO_LIMIT
    overflow = limit + 50
    for turn in range(1, overflow + 1):
        ordinal = crew_log._count_turn_ordinal(state, "acp-1", turn)
        # Each distinct turn advances the counter, so its ordinal is its true position.
        assert ordinal == turn
    keys = state["context_turn_keys"]
    # The memo never exceeds its cap, no matter how many turns passed through it.
    assert keys.count(",") - 1 == limit
    # The retained keys are the MOST RECENT ones; the oldest turns were evicted.
    assert f",acp-1:{overflow}," in keys
    assert f",acp-1:{overflow - limit + 1}," in keys
    assert f",acp-1:{overflow - limit}," not in keys
    # Retained turns at both ends still read back their own ordinal after eviction,
    # and reading one back does not advance the counter.
    assert crew_log._count_turn_ordinal(state, "acp-1", overflow) == overflow
    assert crew_log._count_turn_ordinal(state, "acp-1", overflow - limit + 1) == (
        overflow - limit + 1
    )
    assert state["context_turns_seq"] == overflow


def test_usage_a_rewind_past_400_turns_keeps_the_turns_ordinal():
    """MUTATION-SENSITIVE: a rewind deep into a long session reuses the turn's ordinal.

    ``chat_rewind`` can re-run any user message in the slot's live list, which holds far
    more than the fold's 200 rows. The memo once kept 400 keys, so rewinding to a turn
    older than that counted it again: its label and every later ordinal moved up by one,
    for good. A rewind to turn 10 after 1,000 turns must read ordinal 10 and advance
    nothing.
    """
    state = crew_log._usage_start()
    for turn in range(1, 1_001):
        crew_log._count_turn_ordinal(state, 7, turn)
    assert crew_log._count_turn_ordinal(state, 7, 10) == 10
    assert state["context_turns_seq"] == 1_000
    # The next NEW turn still takes the next ordinal.
    assert crew_log._count_turn_ordinal(state, 7, 1_001) == 1_001
    # The same turn number in another unit is a different turn.
    assert crew_log._count_turn_ordinal(state, 8, 10) == 1_002


def test_usage_a_regenerate_after_a_reattach_keeps_the_turns_ordinal():
    """MUTATION-SENSITIVE: a re-attachment is the same unit, so its key space continues.

    A gateway restart re-attaches to the same crew log and writes another
    ``session/opened`` with ``resumed`` true. The conversation and its turn numbers are
    the same unit's, so a regenerate of turn 1 after it is still turn 1: it must read
    back ordinal 1 and count no extra turn. Keyed by the per-attachment counter, the
    regenerate missed its key and took ordinal 2.
    """

    def _composed(turn: int) -> dict[str, Any]:
        return {
            "turn": turn,
            "sources": [{"kind": "memory", "chars": 40, "tokens": 10}],
            "chars": 40,
            "tokens": 10,
            "tokens_estimated": True,
        }

    def _completed(turn: int) -> dict[str, Any]:
        return {
            "turn": turn,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 100,
            "model": "opus",
            "provider": "kiro",
            "credits": 0.1,
            "tokens": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0},
        }

    handle = _log()
    _opened(handle)
    handle.append("context/composed", _composed(1), src=GATEWAY)
    handle.append("turn/completed", _completed(1), src=GATEWAY)
    # The gateway process died and a new one re-attached to the same crew log.
    _opened(handle, resumed=True)
    handle.append(
        "turn/started", {"turn": 1, "actor": "user", "depth": 0, "attempt": 2}, src=GATEWAY
    )
    handle.append("context/composed", _composed(1), src=GATEWAY)
    handle.append("turn/completed", _completed(1), src=GATEWAY)
    handle.append("context/composed", _composed(2), src=GATEWAY)

    state = crew_log._usage_start()
    for entry in _entries(handle):
        crew_log._usage_step(state, entry)
    assert [row["ordinal"] for row in state["context_turns"]] == [1, 1, 2]
    assert state["context_turns_seq"] == 2


def test_usage_the_ordinal_memo_covers_the_rewindable_range():
    """The memo bound is the slot's live message cap, the range a rewind can reach.

    ``crew_log`` cannot import the dashboard, so the figure is restated there and pinned
    here: if the live cap grows, a rewind could again reach a turn the memo evicted.
    """
    from kiro_crew.dashboard import state as dashboard_state

    assert crew_log.CONTEXT_ORDINAL_MEMO_LIMIT == dashboard_state._MAX_SLOT_MESSAGES


def test_usage_an_interrupted_units_open_row_is_not_stamped_by_the_next_unit():
    """MUTATION-SENSITIVE: the seal alone cannot close the unit boundary.

    A slot's window merges every UNIT it ran under and turn ordinals restart in each
    one, so a matching ordinal is not proof a row belongs to the turn now closing. The
    ``_closed`` seal tells two FINISHED runs of one ordinal apart, because each closer
    marks its own tail run. It cannot see a unit cut off mid-turn: that unit composed a
    row and then died, so no closer ever sealed it, and the next unit's turn 1 walks
    straight onto it and stamps an unmeasured turn with its own reading.
    """
    handle_a = _log("unit-a")
    _opened(handle_a)
    handle_a.append(
        "context/composed",
        {
            "turn": 1,
            "sources": [{"kind": "memory", "chars": 400, "tokens": 100}],
            "chars": 400,
            "tokens": 100,
            "tokens_estimated": True,
        },
        src=GATEWAY,
    )
    # No closer: the gateway died mid-turn, so nothing seals that row.
    handle_b = _log("unit-b")
    _opened(handle_b)
    handle_b.append(
        "context/composed",
        {
            "turn": 1,
            "sources": [{"kind": "memory", "chars": 800, "tokens": 200}],
            "chars": 800,
            "tokens": 200,
            "tokens_estimated": True,
        },
        src=GATEWAY,
    )
    handle_b.append(
        "turn/completed",
        {
            "turn": 1,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 100,
            "model": "opus",
            "provider": "kiro",
            "credits": 0.1,
            "tokens": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0},
            "context": {"used": 90_000, "window": 200_000},
        },
        src=GATEWAY,
    )
    turns = crew_log.fold_slot("usage", ["unit-a", "unit-b"], slot="dashboard:1").value["context"][
        "turns"
    ]
    assert [row.get("used") for row in turns] == [None, 90_000]
    # The counter is the fold's own bookkeeping and never reaches a reader.
    assert all("unit" not in row for row in turns)


def test_usage_bills_every_source_that_spends_and_says_which_spent_what():
    """A session's bill is its turns PLUS its children PLUS its helpers.

    A total that billed turns alone would read a session that spent most of its
    budget on subagents as cheap, and would fold none of the credits
    ``background/completed`` carries. The split beside the total is what keeps the
    total readable: one number cannot say whether the spend was the user's turns or
    a wave of children.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=2.0)
    handle.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src=GATEWAY)
    handle.append("subagent/completed", {"agent_id": "sub-1", "ms": 7, "credits": 1.0}, src=GATEWAY)
    handle.append(
        "background/completed", {"kind": "title", "model": "haiku", "credits": 0.5}, src=GATEWAY
    )

    value = crew_log.fold_usage(_entries(handle))

    assert value["credits"] == 3.5
    assert value["credits_by_source"] == {
        "turn": {"credits": 2.0, "reported": 1},
        "subagent": {"credits": 1.0, "reported": 1},
        "background": {"credits": 0.5, "reported": 1},
    }
    # The turn-scoped count still answers for turns alone: it is what tells a reader
    # how many of the session's turns the turn bucket covers.
    assert value["turns"]["credits_reported"] == 1


def test_usage_bills_a_child_that_failed_against_the_subagent_bucket():
    """A run the user stopped still billed for the turns it attempted."""
    handle = _log()
    _opened(handle)
    handle.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src=GATEWAY)
    handle.append(
        "subagent/failed",
        {"agent_id": "sub-1", "outcome": "stopped", "ms": 3, "credits": 0.75},
        src=GATEWAY,
    )

    value = crew_log.fold_usage(_entries(handle))

    assert value["credits"] == 0.75
    assert value["credits_by_source"]["subagent"] == {"credits": 0.75, "reported": 1}


#: Every charge shape the fold refuses, and why each one is not a measurement of
#: spend. They are one list because they are one rule: a charge is billed only when
#: it is a real number this fold can add to a running total and defend afterwards.
#: ``nan`` and the infinities cannot be added -- every later sum is non-finite too,
#: ``round`` keeps it so, and the savepoint persists it, so the total is poisoned for
#: the life of the unit. A negative can be added, and that is the problem: it REDUCES
#: a total, so a session reads as having spent less than it did, and no later entry
#: corrects it. ``json`` round-trips all five without complaint.
_UNUSABLE_CHARGES = [float("nan"), float("inf"), float("-inf"), -0.5, -1.0e9]


@pytest.mark.parametrize("source", ["subagent/completed", "subagent/failed"])
@pytest.mark.parametrize("bad", _UNUSABLE_CHARGES)
def test_usage_refuses_an_unusable_credit_charge_from_a_child(bad, source):
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=2.0)
    handle.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src=GATEWAY)
    handle.append(source, {"agent_id": "sub-1", "credits": bad}, src=GATEWAY)

    value = crew_log.fold_usage(_entries(handle))

    assert value["credits"] == 2.0
    assert value["credits_by_source"]["subagent"] == {"credits": 0.0, "reported": 0}


@pytest.mark.parametrize("bad", _UNUSABLE_CHARGES)
def test_usage_refuses_an_unusable_credit_charge_from_a_background_helper(bad):
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=2.0)
    handle.append("background/completed", {"kind": "title", "credits": bad}, src=GATEWAY)

    value = crew_log.fold_usage(_entries(handle))

    assert value["credits"] == 2.0
    assert value["credits_by_source"]["background"] == {"credits": 0.0, "reported": 0}


@pytest.mark.parametrize("bad", _UNUSABLE_CHARGES)
def test_usage_refuses_an_unusable_credit_charge_from_a_turn(bad):
    """One rule for all three spenders, including the one that predates the buckets.

    The turn path is where a charge has always been read, so leaving it out would
    make the rule depend on which spender wrote the line -- and the poisoned total it
    produces is the same total.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=2.0)
    _turn(handle, 2, credits=bad)

    value = crew_log.fold_usage(_entries(handle))

    assert value["credits"] == 2.0
    assert value["credits_by_source"]["turn"] == {"credits": 2.0, "reported": 1}
    assert value["turns"]["credits_reported"] == 1
    assert value["by_model"]["opus"]["credits"] == 2.0


def test_usage_survives_a_charge_too_large_to_be_a_float():
    """A JSON integer is unbounded, so a charge can be unrepresentable, not just wrong.

    ``float()`` raises ``OverflowError`` on a 400-digit int, and nothing between
    ``_usage_step`` and ``fold_session`` catches it, so the whole fold would fail on
    one bad entry rather than skipping it. The fold's contract is that a line it
    cannot interpret costs that line, never the projection.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=2.0)
    handle.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src=GATEWAY)
    handle.append("subagent/completed", {"agent_id": "sub-1", "credits": 10**400}, src=GATEWAY)

    value = crew_log.fold_usage(_entries(handle))

    assert value["credits"] == 2.0
    assert value["credits_by_source"]["subagent"] == {"credits": 0.0, "reported": 0}


def test_usage_refuses_a_charge_that_would_overflow_the_running_total():
    """Two charges that are each finite can still sum to infinity.

    This is the case an input check cannot see: both values pass every test that
    looks at them alone. Only the RESULT of the addition is wrong, and once it is
    stored the savepoint keeps it, so the invariant has to be checked after the add
    rather than before it.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0e308)
    handle.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src=GATEWAY)
    handle.append("subagent/completed", {"agent_id": "sub-1", "credits": 1.0e308}, src=GATEWAY)

    value = crew_log.fold_usage(_entries(handle))

    assert math.isfinite(value["credits"])
    assert value["credits"] == 1.0e308
    assert value["credits_by_source"]["turn"] == {"credits": 1.0e308, "reported": 1}
    assert value["credits_by_source"]["subagent"] == {"credits": 0.0, "reported": 0}
    assert math.isfinite(value["by_model"]["opus"]["credits"])


def test_usage_counts_a_charge_of_exactly_zero_as_measured():
    """Zero is the one falsy charge that IS a measurement, so it stays counted.

    A provider that reports ``0.0`` explicitly measured zero, which is a different
    fact from a closer that reported nothing -- and ``reported`` is the field that
    tells those two apart. The refusals above are values that cannot be summed or
    that move a total the wrong way; zero is neither.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=0.0)

    value = crew_log.fold_usage(_entries(handle))

    assert value["credits"] == 0.0
    assert value["credits_by_source"]["turn"] == {"credits": 0.0, "reported": 1}


def test_usage_does_not_read_an_unmetered_child_as_a_free_one():
    """A closer with no ``credits`` key measured nothing, which is not zero."""
    handle = _log()
    _opened(handle)
    handle.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src=GATEWAY)
    handle.append("subagent/completed", {"agent_id": "sub-1", "ms": 7}, src=GATEWAY)

    value = crew_log.fold_usage(_entries(handle))

    assert value["credits"] == 0.0
    assert value["credits_by_source"]["subagent"] == {"credits": 0.0, "reported": 0}


def test_timeline_keeps_a_child_run_duration():
    """``ms`` is what subagent and step entries call their measured duration.

    ``duration_ms`` is the turn entry's spelling, so a copied-key list carrying only
    that one keeps a child's run time out of the timeline entirely.
    """
    handle = _log()
    _opened(handle)
    handle.append("subagent/spawned", {"turn": 1, "agent_id": "sub-1"}, src=GATEWAY)
    handle.append("subagent/completed", {"agent_id": "sub-1", "ms": 7, "credits": 1.0}, src=GATEWAY)

    moments = crew_log.fold_timeline(_entries(handle))["moments"]
    closer = [m for m in moments if m["type"] == "subagent/completed"]
    assert len(closer) == 1
    assert closer[0]["ms"] == 7
    assert closer[0]["credits"] == 1.0


def test_usage_splits_cost_by_model():
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0, model="opus")
    _turn(handle, 2, credits=0.5, model="sonnet")
    by_model = crew_log.fold_usage(_entries(handle))["by_model"]
    assert by_model["opus"]["credits"] == 1.0
    assert by_model["sonnet"]["credits"] == 0.5
    assert by_model["opus"]["turns"] == 1


def test_usage_counts_compactions_and_the_context_they_freed():
    handle = _log()
    _opened(handle)
    handle.append(
        "compaction/applied",
        {"pct_before": 90.0, "pct_after": 40.0, "freed_pct": 50.0},
        src=GATEWAY,
    )
    handle.append(
        "compaction/applied",
        {"pct_before": 60.0, "pct_after": 65.0, "freed_pct": -5.0},
        src=GATEWAY,
    )
    assert crew_log.fold_usage(_entries(handle))["compactions"] == {
        "count": 2,
        "freed_pct": 45.0,
    }


# --- timeline -------------------------------------------------------------


def test_timeline_keeps_the_newest_moments_and_says_how_many_it_dropped():
    handle = _log()
    _opened(handle)
    wanted = crew_log.TIMELINE_LIMIT + 5
    _append_grouped(
        handle,
        [
            {"type": "turn/started", "data": {"turn": turn, "actor": "user", "depth": 0}}
            for turn in range(1, wanted + 1)
        ],
    )
    value = crew_log.fold_timeline(_entries(handle))
    assert len(value["moments"]) == crew_log.TIMELINE_LIMIT
    assert value["dropped"] == wanted + 1 - crew_log.TIMELINE_LIMIT
    assert value["moments"][-1]["turn"] == wanted


def test_timeline_leaves_out_the_bulk_types_the_page_route_serves():
    handle = _log()
    _opened(handle)
    _tool(handle, 1, "c1", "fs_read")
    handle.append(
        "message/received",
        {"turn": 1, "role": "user", "source": "dashboard", "text": "hi"},
        src=GATEWAY,
    )
    kinds = {moment["type"] for moment in crew_log.fold_timeline(_entries(handle))["moments"]}
    assert kinds == {"session/opened"}


def test_timeline_moments_are_oldest_first_and_carry_their_seq():
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    moments = crew_log.fold_timeline(_entries(handle))["moments"]
    seqs = [moment["seq"] for moment in moments]
    assert seqs == sorted(seqs)
    assert [moment["type"] for moment in moments] == [
        "session/opened",
        "turn/started",
        "turn/completed",
    ]


def test_timeline_folds_one_session_unaffected_by_the_single_unit_guard():
    """The reachable path -- one session's log -- renders every moment as before.

    One session holds exactly one succession unit, so a seq that only ever climbs is
    the normal case the guard leaves untouched.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1)
    _turn(handle, 2)
    value = crew_log.fold_timeline(_entries(handle))
    assert [moment["type"] for moment in value["moments"]] == [
        "session/opened",
        "turn/started",
        "turn/completed",
        "turn/started",
        "turn/completed",
    ]
    seqs = [moment["seq"] for moment in value["moments"]]
    assert seqs == sorted(seqs)
    assert value["first_seq"] == seqs[0]
    assert value["last_seq"] == seqs[-1]


def test_timeline_refuses_rows_from_a_second_succession_unit():
    """A slot-wide fold of two units crosses a seq restart, and the guard names it.

    ``fold_slot_checkpoint`` folds the units a slot ran under back to back, re-basing
    the seq per unit so the second starts at 1 again. Nothing reads ``timeline`` over a
    slot today; this pins that the day one does, it fails loudly at the unit boundary
    rather than concatenating two units into one timestamp-scrambled timeline.
    """
    first = _log("u-first", slot="chat-tl")
    _opened(first)
    _turn(first, 1)
    second = _log("u-second", slot="chat-tl")
    _opened(second)
    _turn(second, 1)

    # One unit folds cleanly, the hazard is only the join of a second.
    one = crew_log.fold_slot_checkpoint("timeline", ["u-first"], slot="chat-tl")
    one_seqs = [moment["seq"] for moment in one.state["moments"]]
    assert one_seqs == sorted(one_seqs) and len(one_seqs) == 3

    with pytest.raises(crew_log.CrewLogError) as excinfo:
        crew_log.fold_slot_checkpoint("timeline", ["u-first", "u-second"], slot="chat-tl")
    assert excinfo.value.field == "seq"
    assert "one succession unit" in str(excinfo.value)


# --- tools ----------------------------------------------------------------


def test_tools_matches_a_call_to_its_completion_by_call_id():
    handle = _log()
    _opened(handle)
    _tool(handle, 1, "c1", "fs_read")
    value = crew_log.fold_tools(_entries(handle))
    assert value["calls"] == 1
    assert value["completed"] == 1
    assert value["open"] == 0
    assert value["by_name"]["fs_read"]["elapsed_ms"] == 42


def test_tools_reports_an_unmatched_call_as_open():
    handle = _log()
    _opened(handle)
    handle.append(
        "tool/called",
        {"turn": 1, "call_id": "c9", "name": "shell", "server": "", "kind": "native"},
        src=GATEWAY,
    )
    value = crew_log.fold_tools(_entries(handle))
    assert value["open"] == 1
    assert value["open_calls"][0]["call_id"] == "c9"
    assert value["completed"] == 0


def test_tools_never_pairs_two_calls_that_carry_no_call_id():
    """An empty call_id identifies nothing, so it is counted and left unpaired."""
    handle = _log()
    _opened(handle)
    for _ in range(2):
        handle.append(
            "tool/called",
            {"turn": 1, "call_id": "", "name": "shell", "server": "", "kind": "native"},
            src=GATEWAY,
        )
    value = crew_log.fold_tools(_entries(handle))
    assert value["calls"] == 2
    assert value["unidentified_calls"] == 2
    assert value["open"] == 0


def test_tools_counts_a_refused_status_and_an_asserted_error():
    handle = _log()
    _opened(handle)
    _tool(handle, 1, "c1", "a", status="refused")
    handle.append(
        "tool/called",
        {"turn": 1, "call_id": "c2", "name": "b", "server": "", "kind": "native"},
        src=GATEWAY,
    )
    handle.append(
        "tool/completed",
        {
            "turn": 1,
            "call_id": "c2",
            "name": "b",
            "server": "",
            "status": "completed",
            "is_error": True,
        },
        src=GATEWAY,
    )
    value = crew_log.fold_tools(_entries(handle))
    assert value["errors"] == 2


def test_tools_absent_is_error_is_not_a_claim_that_the_call_worked():
    handle = _log()
    _opened(handle)
    _tool(handle, 1, "c1", "a", status="unknown")
    value = crew_log.fold_tools(_entries(handle))
    assert value["errors"] == 0
    assert value["by_name"]["a"]["last_status"] == "unknown"


def test_tools_keeps_totals_exact_past_the_name_budget():
    handle = _log()
    _opened(handle)
    extra = 3
    _append_grouped(
        handle,
        [
            item
            for index in range(crew_log.TOOL_NAME_LIMIT + extra)
            for item in _tool_items(1, f"c{index}", f"tool_{index:04d}")
        ],
    )
    value = crew_log.fold_tools(_entries(handle))
    assert len(value["by_name"]) == crew_log.TOOL_NAME_LIMIT
    assert value["names_omitted"] == extra
    assert value["calls"] == crew_log.TOOL_NAME_LIMIT + extra
    assert value["completed"] == crew_log.TOOL_NAME_LIMIT + extra


def test_tools_counts_a_completion_with_no_call_before_it():
    handle = _log()
    _opened(handle)
    handle.append(
        "tool/completed",
        {"turn": 1, "call_id": "ghost", "name": "a", "server": "", "status": "completed"},
        src=GATEWAY,
    )
    assert crew_log.fold_tools(_entries(handle))["unmatched_completions"] == 1


# --- approvals ------------------------------------------------------------


def test_approvals_matches_a_request_to_its_decision():
    handle = _log()
    _opened(handle)
    handle.append(
        "approval/requested",
        {"turn": 1, "approval_id": "a1", "tool": "shell", "reason": "run"},
        src=GATEWAY,
    )
    handle.append(
        "approval/decided",
        {"turn": 1, "approval_id": "a1", "decision": "allow", "by": "raymond"},
        src=GATEWAY,
    )
    value = crew_log.fold_approvals(_entries(handle))
    assert value == {
        "requested": 1,
        "decided": 1,
        "pending": 0,
        "pending_requests": [],
        "pending_omitted": 0,
        "pending_dropped": 0,
        "unidentified_requests": 0,
        "unmatched_decisions": 0,
        "by_decision": {"allow": 1},
        "last": {
            "approval_id": "a1",
            "decision": "allow",
            "by": "raymond",
            "cause": "",
            "tool": "shell",
            "turn": 1,
            "time": value["last"]["time"],
            "seq": value["last"]["seq"],
        },
    }


def test_approvals_reports_an_undecided_request_as_pending():
    handle = _log()
    _opened(handle)
    handle.append(
        "approval/requested",
        {"turn": 1, "approval_id": "a1", "tool": "shell", "reason": "run"},
        src=GATEWAY,
    )
    value = crew_log.fold_approvals(_entries(handle))
    assert value["pending"] == 1
    assert value["pending_requests"][0]["approval_id"] == "a1"
    assert value["last"] is None


def test_approvals_counts_a_decision_with_no_request_before_it():
    handle = _log()
    _opened(handle)
    handle.append(
        "approval/decided",
        {"turn": 1, "approval_id": "a1", "decision": "deny", "by": "host"},
        src=GATEWAY,
    )
    value = crew_log.fold_approvals(_entries(handle))
    assert value["unmatched_decisions"] == 1
    assert value["by_decision"] == {"deny": 1}
