"""The ``subagents`` fold -- one test per property it promises.

The fold answers one question from one savepoint read: which children this session
dispatched, and for each one its outcome, its duration and what it cost. The
properties worth pinning are the ones a reader would otherwise have to trust:

* a closer lands on its own row, and nowhere else;
* a closer with NO row is still counted, because crash-repair writes one for a child
  whose ``spawned`` fell past the retention cap, and dropping it would under-report
  what the session actually spent;
* ``totals.spawned`` counts every dispatch, while ``by_id`` holds only the retained
  ones -- so the two disagree by ``omitted``, on purpose;
* absent credits are never read as zero, which is the same posture ``usage`` takes.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard.handlers import crew_log as routes

SESSION = "s-subagents"
GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _log(unit_id: str = SESSION, slot: str = "dashboard:1") -> CrewLog:
    return CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)


def _opened(handle: CrewLog) -> None:
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": "dashboard:1",
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
        },
        src=GATEWAY,
    )


def _spawned(
    handle: CrewLog,
    agent_id: str,
    *,
    turn: int = 1,
    agent: str = "kirocrew-worker",
    model: str = "opus",
    scope: dict | None = None,
) -> None:
    data: dict = {"agent_id": agent_id, "turn": turn, "agent": agent, "model": model}
    if scope is not None:
        data["scope"] = scope
    handle.append("subagent/spawned", data, src=GATEWAY)


def _steered(handle: CrewLog, agent_id: str, *, mode: str = "interrupt") -> None:
    handle.append("subagent/steered", {"agent_id": agent_id, "mode": mode}, src=GATEWAY)


def _completed(
    handle: CrewLog, agent_id: str, *, ms: int = 1000, credits: float | None = None
) -> None:
    data: dict = {"agent_id": agent_id, "ms": ms}
    if credits is not None:
        data["credits"] = credits
    handle.append("subagent/completed", data, src=GATEWAY)


def _failed(
    handle: CrewLog,
    agent_id: str,
    *,
    outcome: str = "failed",
    ms: int = 500,
    reason: str = "boom",
    credits: float | None = None,
) -> None:
    data: dict = {"agent_id": agent_id, "outcome": outcome, "ms": ms, "reason": reason}
    if credits is not None:
        data["credits"] = credits
    handle.append("subagent/failed", data, src=GATEWAY)


def _fold(unit_id: str = SESSION) -> dict:
    """The ``subagents`` value for *unit_id*, folded from the start of its log."""
    return crew_log.fold_session(unit_id, names=("subagents",)).projection("subagents").value


# --- the six the plan asks for --------------------------------------------- #


def test_a_child_that_finished_lands_its_outcome_duration_and_cost_on_its_own_row():
    handle = _log()
    _opened(handle)
    _spawned(handle, "a-1", agent="kirocrew-worker", model="opus")
    _completed(handle, "a-1", ms=1200, credits=2.0)

    value = _fold()
    row = value["by_id"]["a-1"]
    assert row["outcome"] == "completed"
    assert row["ms"] == 1200
    assert row["credits"] == 2.0
    assert row["agent"] == "kirocrew-worker"
    assert row["model"] == "opus"
    assert value["totals"]["completed"] == 1
    # Nothing is still open, so the count reads 0 rather than inheriting a floor.
    assert value["running"] == 0


def test_a_stopped_child_counts_as_stopped_and_not_as_a_failure():
    """The runtime's three-way outcome exists so 'not success' is not read as 'failed'."""
    handle = _log()
    _opened(handle)
    _spawned(handle, "a-1")
    _failed(handle, "a-1", outcome="stopped", ms=300)

    value = _fold()
    assert value["by_id"]["a-1"]["outcome"] == "stopped"
    assert value["totals"]["stopped"] == 1
    assert value["totals"]["failed"] == 0


def test_a_steer_moves_nothing_because_this_fold_does_not_read_them():
    """``subagent/steered`` is declared and deliberately unread.

    A steer is an event ABOUT a child rather than a state of one, and nothing this fold
    answers for is a count of them -- so it is left out of ``affects`` too, since a type the
    step ignores would otherwise cost a copy per entry for a value that never changes.
    """
    handle = _log()
    _opened(handle)
    _spawned(handle, "a-1")
    before = _fold()
    _steered(handle, "a-1")
    _steered(handle, "a-1", mode="follow_up")

    after = _fold()
    assert after["by_id"] == before["by_id"]
    assert after["totals"] == before["totals"]
    assert "subagent/steered" not in crew_log._FOLDS["subagents"].affects


def test_past_the_retention_cap_a_further_dispatch_is_counted_and_not_retained():
    handle = _log()
    _opened(handle)
    for index in range(crew_log.OPEN_RETAIN_LIMIT + 1):
        _spawned(handle, f"a-{index}")

    value = _fold()
    assert len(value["by_id"]) == crew_log.OPEN_RETAIN_LIMIT
    assert value["omitted"] == 1
    # The cap itself is not in the value: nothing reads it, and a reader that needs to know
    # its rows are a window is told that by ``omitted``.
    assert "limit" not in value


def test_a_closer_with_no_row_is_counted_and_builds_none():
    """crash-repair closes a child whose ``spawned`` this fold never retained."""
    handle = _log()
    _opened(handle)
    _failed(handle, "ghost", outcome="unknown", ms=700, credits=1.5)

    value = _fold()
    assert value["by_id"] == {}
    totals = value["totals"]
    assert totals["unknown"] == 1
    # And it is legible as unmatched rather than looking like a retained child.
    assert totals["closed_unmatched"] == 1


@pytest.mark.asyncio
async def test_the_route_serves_the_fold_with_no_route_change():
    """``GET /api/sessions/{id}/crew-log/projection/subagents`` on the existing door."""
    handle = _log()
    _opened(handle)
    _spawned(handle, "a-1")
    _completed(handle, "a-1", ms=900, credits=0.5)

    request = make_mocked_request("GET", f"/api/sessions/{SESSION}/crew-log/projection/subagents")
    request.match_info["id"] = SESSION
    request.match_info["name"] = "subagents"
    with patch(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        return_value=True,
    ):
        response = await routes.api_session_crew_log_projection(request)
    assert response.status == 200
    value = json.loads(response.body)["value"]
    assert value["by_id"]["a-1"]["outcome"] == "completed"
    assert value["by_id"]["a-1"]["credits"] == 0.5


# --- the conductor's two amendments ---------------------------------------- #


def test_totals_spawned_counts_every_dispatch_while_by_id_holds_the_retained_ones():
    """The two disagree by ``omitted``, and the render says so rather than hiding it."""
    handle = _log()
    _opened(handle)
    for index in range(crew_log.OPEN_RETAIN_LIMIT + 3):
        _spawned(handle, f"a-{index}")

    value = _fold()
    assert value["totals"]["spawned"] == crew_log.OPEN_RETAIN_LIMIT + 3
    assert len(value["by_id"]) == crew_log.OPEN_RETAIN_LIMIT
    assert value["omitted"] == 3
    # The identity a reader can check: every dispatch is either retained or omitted.
    assert value["totals"]["spawned"] == len(value["by_id"]) + value["omitted"]


def test_an_orphan_closer_still_bills_its_duration_and_cost_into_the_totals():
    """Amendment (b): a closer is never silently dropped for want of a row."""
    handle = _log()
    _opened(handle)
    _spawned(handle, "kept")
    _completed(handle, "kept", ms=100, credits=1.0)
    # Two closers whose spawn this fold never saw.
    _completed(handle, "ghost-1", ms=200, credits=2.0)
    _failed(handle, "ghost-2", outcome="stopped", ms=300, credits=3.0)

    totals = _fold()["totals"]
    assert totals["completed"] == 2
    assert totals["stopped"] == 1
    assert totals["closed_unmatched"] == 2


# --- absent is not zero ---------------------------------------------------- #


def test_a_closer_that_reported_no_duration_leaves_the_row_without_a_number():
    """An absent ``ms`` is not a measured instant, the same rule ``credits`` follows.

    Two closers write none: the emitter only sets ``ms`` when it measured a duration above
    zero, and crash-repair's closer writes just an ``agent_id`` and ``unknown``. A 0 on the
    row would render as "0.0s" -- a child that finished instantly, which is a different
    claim from one whose duration nobody recorded.
    """
    handle = _log()
    _opened(handle)
    _spawned(handle, "a-1")
    # The shape crash-repair writes: a closer carrying no duration at all.
    handle.append("subagent/failed", {"agent_id": "a-1", "outcome": "unknown"}, src=GATEWAY)

    value = _fold()
    assert value["by_id"]["a-1"]["ms"] is None
    # And no summed duration to read a zero out of: the row carries the only duration this
    # fold keeps, so "nobody said" is the row's `None` rather than a total that reads as 0.
    assert "ms" not in value["totals"]
    assert "ms_reported" not in value["totals"]
    # The child still closed.
    assert value["totals"]["unknown"] == 1


def test_a_running_child_has_no_duration_either():
    handle = _log()
    _opened(handle)
    _spawned(handle, "a-1")

    assert _fold()["by_id"]["a-1"]["ms"] is None


def test_the_state_keeps_no_open_list_for_the_render_to_disagree_with():
    """No ``open`` list, in the state OR the render -- nothing reads one.

    It had writes on every spawn and closer plus a copy, and nothing read it -- state kept in
    step by hand for no consumer, which is the class this fold already deleted ``scope`` for.
    The render then carried the same list for the same non-reader: a surface listing the open
    children filters ``by_id`` for an absent outcome, which is the filter the list was built
    from, so shipping both is one fact spelled twice.
    """
    fold = crew_log._FOLDS["subagents"]
    assert "open" not in fold.start()
    handle = _log()
    _opened(handle)
    _spawned(handle, "a-1")
    assert "open" not in _fold()


def test_a_closer_that_reported_no_credits_leaves_the_row_without_a_number():
    """Absent credits are not a measurement of zero, the posture ``usage`` already takes."""
    handle = _log()
    _opened(handle)
    _spawned(handle, "a-1")
    _completed(handle, "a-1", ms=400)

    value = _fold()
    assert value["by_id"]["a-1"]["credits"] is None
    # And there is no aggregate to read a zero out of: ``usage`` owns the total over these
    # same closers, so this fold publishes a charge per ROW and nothing summed.
    assert "credits" not in value["totals"]
    assert "credits_reported" not in value["totals"]


def test_an_integer_too_large_to_be_a_float_does_not_crash_the_fold():
    """Python's ``int`` has no magnitude limit; ``float()`` raises past about 1.8e308.

    Such a value passes the numeric type check, so the conversion is where it bites -- and
    the line stays on disk, so an escaping ``OverflowError`` would turn EVERY later read of
    this session's fold into a crash rather than costing one number.
    """
    fold = crew_log._FOLDS["subagents"]
    state = fold.start()
    fold.step(
        state,
        crew_log.Entry(
            type="subagent/spawned", seq=1, time=1, src=GATEWAY, data={"agent_id": "a-1"}
        ),
    )
    fold.step(
        state,
        crew_log.Entry(
            type="subagent/completed",
            seq=2,
            time=2,
            src=GATEWAY,
            data={"agent_id": "a-1", "ms": 10, "credits": 10**400},
        ),
    )

    value = fold.render(state)
    # The child still closed -- only its unusable charge was dropped.
    assert value["by_id"]["a-1"]["outcome"] == "completed"
    assert value["by_id"]["a-1"]["credits"] is None
    assert "credits" not in value["totals"]


def test_two_charges_a_running_total_could_not_hold_both_land_on_their_own_rows():
    """No total, no overflow on the way up -- and that is the point of not keeping one.

    While this fold summed its own credits, two FINITE charges could push that sum past
    what a float holds, and the screen had to refuse the second one. ``usage`` owns the
    aggregate now, so each charge is only ever a term on its own row: both are
    representable, both land, and neither is refused for what the other cost.
    """
    fold = crew_log._FOLDS["subagents"]
    state = fold.start()
    for index, charge in enumerate((1.5e308, 1.5e308), start=1):
        fold.step(
            state,
            crew_log.Entry(
                type="subagent/spawned",
                seq=index * 2 - 1,
                time=1,
                src=GATEWAY,
                data={"agent_id": f"a-{index}"},
            ),
        )
        fold.step(
            state,
            crew_log.Entry(
                type="subagent/completed",
                seq=index * 2,
                time=1,
                src=GATEWAY,
                data={"agent_id": f"a-{index}", "ms": 1, "credits": charge},
            ),
        )

    value = fold.render(state)
    assert value["by_id"]["a-1"]["credits"] == pytest.approx(1.5e308)
    assert value["by_id"]["a-2"]["credits"] == pytest.approx(1.5e308)
    assert value["totals"]["completed"] == 2
    assert "credits" not in value["totals"]


@pytest.mark.parametrize("planted", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_an_unusable_charge_shape_never_reaches_a_row(planted):
    fold = crew_log._FOLDS["subagents"]
    state = fold.start()
    fold.step(
        state,
        crew_log.Entry(
            type="subagent/spawned", seq=1, time=1, src=GATEWAY, data={"agent_id": "a-1"}
        ),
    )
    fold.step(
        state,
        crew_log.Entry(
            type="subagent/completed",
            seq=2,
            time=2,
            src=GATEWAY,
            data={"agent_id": "a-1", "ms": 1, "credits": planted},
        ),
    )

    value = fold.render(state)
    assert value["by_id"]["a-1"]["credits"] is None
    assert "credits" not in value["totals"]


def test_the_running_count_stays_exact_when_retention_drops_a_dispatch():
    """``open`` lists retained rows; ``running`` is derived from the totals.

    A dropped dispatch has no row to be missing an outcome from, so a reader counting
    ``open`` would report a session with more children in flight than the cap as having at
    most the cap -- understating, silently, exactly when the number matters most.
    """
    handle = _log()
    _opened(handle)
    for index in range(crew_log.OPEN_RETAIN_LIMIT + 20):
        _spawned(handle, f"a-{index}")
    # Close two of the retained ones, so running is not simply "everything dispatched".
    _completed(handle, "a-0", ms=5)
    _failed(handle, "a-1", outcome="stopped", ms=5)

    value = _fold()
    assert value["omitted"] == 20
    # The retained rows still open are capped by what was retained; the answer is not.
    retained_open = [row for row in value["by_id"].values() if row["outcome"] is None]
    assert len(retained_open) == crew_log.OPEN_RETAIN_LIMIT - 2
    assert value["running"] == crew_log.OPEN_RETAIN_LIMIT + 20 - 2
    assert value["running"] > len(retained_open)


def test_more_closers_than_openers_reads_as_none_running_not_a_negative():
    handle = _log()
    _opened(handle)
    _completed(handle, "ghost-1", ms=1)
    _failed(handle, "ghost-2", outcome="failed", ms=1)

    assert _fold()["running"] == 0


def test_running_never_reads_below_the_rows_this_fold_still_shows_as_open():
    """A closer whose opener never reached the file must not zero out a visible child.

    ``spawned - closed`` alone can go NEGATIVE without any damaged input on this fold's part:
    an append dropped once its attempt budget was spent, or a record ``_iter_segments`` skips,
    leaves a closer whose ``subagent/spawned`` the reader never sees. It bills into the
    closers having never bumped ``spawned``. Clamping that at 0 then reports no children
    running above a row drawn wearing the running pill -- the panel's header and its own table
    contradicting each other, with nothing naming the gap.
    """
    handle = _log()
    _opened(handle)
    # One child dispatched and still going, so the fold has a row to show as open.
    _spawned(handle, "a-1")
    # Two closers whose openers are absent from the readable file: ``closed`` reaches 2 while
    # ``spawned`` is 1, so the arithmetic floor is -1.
    _completed(handle, "ghost-1", ms=1)
    _failed(handle, "ghost-2", outcome="failed", ms=1)

    value = _fold()
    assert value["totals"]["spawned"] == 1
    assert value["totals"]["closed_unmatched"] == 2
    still_open = [row for row in value["by_id"].values() if row["outcome"] is None]
    assert len(still_open) == 1
    # Not 0: the fold is still showing one child as open, so that is the floor.
    assert value["running"] == 1


def test_a_repeated_agent_id_is_counted_in_omitted_so_the_identity_holds():
    """``spawned == len(by_id) + omitted`` is advertised as checkable, so it must be.

    A second dispatch naming an id a row already has is counted in ``spawned`` and gets no row
    of its own -- the same shape as a dispatch past the cap or one with an unusable id. It used
    to return without touching ``omitted``, which left the identity short by one with nothing
    on the value saying why, so a reader auditing it would have read the gap as an arithmetic
    bug in the fold.
    """
    handle = _log()
    _opened(handle)
    _spawned(handle, "a-1")
    _spawned(handle, "a-1")

    value = _fold()
    assert value["totals"]["spawned"] == 2
    assert len(value["by_id"]) == 1
    assert value["omitted"] == 1
    assert value["totals"]["spawned"] == len(value["by_id"]) + value["omitted"]


def test_running_is_marked_inexact_when_both_truncations_are_in_play():
    """``running`` is a FLOOR when an omitted dispatch and an orphan closer coexist.

    Rows are never evicted, so once a session passes the cap its still-open children ARE the
    omitted ones -- ``still_open`` stops covering them and the count falls back on
    ``spawned - closed``. Every unmatched closer is subtracted there, but the fold cannot tell
    whether it closed one of those omitted dispatches or a child whose ``subagent/spawned``
    never reached the file, and only the first should reduce the count. So the number can be
    short by up to ``closed_unmatched``, and a surface drawing it as exact would state a
    measurement the record cannot support.
    """
    handle = _log()
    _opened(handle)
    # Past the cap by one, so exactly one dispatch is omitted and every retained row is open.
    for index in range(crew_log.OPEN_RETAIN_LIMIT + 1):
        _spawned(handle, f"a-{index}")
    # One closer for a child this log never opened.
    _completed(handle, "ghost-1", ms=1)

    value = _fold()
    assert value["omitted"] == 1
    assert value["totals"]["closed_unmatched"] == 1
    # The floor, which is one short: 513 were dispatched and none of them closed.
    assert value["running"] == crew_log.OPEN_RETAIN_LIMIT
    assert value["totals"]["spawned"] == crew_log.OPEN_RETAIN_LIMIT + 1
    # And the value says so, which is the whole remedy: the reader is told "at least".
    assert value["running_exact"] is False


def test_an_omitted_dispatch_alone_leaves_running_exact():
    """Neither truncation ALONE makes the count a floor, so neither alone may claim it is.

    Omitted dispatches with no orphan closer: every closer found its row, so nothing was
    subtracted that might have belonged to an omitted child.
    """
    handle = _log()
    _opened(handle)
    for index in range(crew_log.OPEN_RETAIN_LIMIT + 1):
        _spawned(handle, f"a-{index}")
    _completed(handle, "a-0", ms=1)

    value = _fold()
    assert value["omitted"] == 1
    assert value["totals"]["closed_unmatched"] == 0
    assert value["running_exact"] is True


def test_an_orphan_closer_alone_leaves_running_exact():
    """The other half: nothing omitted, so every open child has a visible row.

    ``still_open`` then covers them exactly and is the floor that wins, so the orphan closer
    cannot pull the answer below the truth.
    """
    handle = _log()
    _opened(handle)
    _spawned(handle, "b-1")
    _completed(handle, "ghost-1", ms=1)

    value = _fold()
    assert value["omitted"] == 0
    assert value["totals"]["closed_unmatched"] == 1
    assert value["running"] == 1
    assert value["running_exact"] is True


def test_a_row_retains_only_fields_something_reads():
    """A field kept against a reader that does not exist is state paid for on every copy.

    The panel draws agent, model, outcome, ms, credits and -- for a child that did not
    finish -- reason; the render orders by ``seq_spawned``. Nothing reads the child's
    inherited scope, its spawn time, the turn that asked or a steer count, so none of those
    is retained.
    """
    handle = _log()
    _opened(handle)
    _spawned(handle, "a-1", scope={"memory": True, "lessons": True, "project": False})

    row = _fold()["by_id"]["a-1"]
    assert set(row) == {
        "agent_id",
        "seq_spawned",
        "agent",
        "model",
        "outcome",
        "ms",
        "credits",
        "reason",
    }
    # And the state carries no container the render does not read.
    assert set(crew_log._FOLDS["subagents"].start()) == {"by_id", "omitted", "totals"}


@pytest.mark.parametrize("planted", [True, "2.0", None, {"n": 1}, [2.0]])
def test_a_credit_charge_that_is_not_a_number_is_refused_rather_than_coerced(planted):
    """Driven at the FOLD, because the writer's declaration already refuses these.

    A declaration binds the writer; a damaged or planted line is exactly the input
    that ignores it, so the coercion has to hold on the read side too. ``True`` is the
    one that matters most: it is an ``int`` in Python, so a check that forgot to exclude
    ``bool`` would bill a boolean as one credit.
    """
    fold = crew_log._FOLDS["subagents"]
    state = fold.start()
    fold.step(
        state,
        crew_log.Entry(
            type="subagent/spawned", seq=1, time=1, src=GATEWAY, data={"agent_id": "a-1"}
        ),
    )
    fold.step(
        state,
        crew_log.Entry(
            type="subagent/completed",
            seq=2,
            time=2,
            src=GATEWAY,
            data={"agent_id": "a-1", "ms": 10, "credits": planted},
        ),
    )

    value = fold.render(state)
    assert value["by_id"]["a-1"]["credits"] is None
    assert "credits" not in value["totals"]


# --- registration ---------------------------------------------------------- #


def test_the_fold_is_registered_and_advertised():
    assert "subagents" in crew_log.PROJECTION_NAMES
    assert "subagents" in crew_log.FOLD_NAMES
    assert crew_log.require_name("subagents") == "subagents"


def test_the_fold_is_lazy_because_eager_folding_is_slot_keyed():
    """Pinned so the reason survives, rather than reading as an oversight.

    The plan asked for this fold EAGER. It cannot be, and the constraint is not this
    fold's: eager folding continues the warm SLOT memo -- ``_fold_batch`` resolves a
    slot from the unit header and calls ``read_slot_projection`` -- and ``subagents`` is
    keyed by one SESSION. ``projection.py`` states the rule at import
    (``EAGER_FOLD_NAMES <= SLOT_PROJECTION_NAMES``) and ``eager._WAKE_TYPES`` is a fixed
    three-type set that no ``subagent/*`` type is in, so declaring this fold eager would
    register a mode the process cannot honour. Making it eager means building a warm
    SESSION path, which is a change to the eager module's contract rather than a fold.
    """
    assert crew_log._FOLDS["subagents"].mode == "lazy"
    assert "subagents" not in crew_log.EAGER_FOLD_NAMES
    assert set(crew_log.EAGER_FOLD_NAMES) <= set(crew_log.SLOT_PROJECTION_NAMES)


def test_the_fold_declares_the_three_subagent_types_it_reads():
    fold = crew_log._FOLDS["subagents"]
    assert fold.affects == frozenset(
        {
            "subagent/spawned",
            "subagent/completed",
            "subagent/failed",
        }
    )


def test_the_copier_covers_every_container_the_step_reaches():
    """A shared nested container would let a step edit the state it was handed."""
    fold = crew_log._FOLDS["subagents"]
    state = fold.start()
    state["by_id"]["a-1"] = {"ms": None, "credits": None}
    copied = fold.copied(state)
    copied["by_id"]["a-1"]["ms"] = 9
    copied["totals"]["spawned"] = 9
    assert state["by_id"]["a-1"]["ms"] is None
    assert state["totals"]["spawned"] == 0


def test_an_entry_this_fold_does_not_read_moves_nothing():
    """``affects`` is not the only guard, so the step's own branching has to be total.

    ``affects`` spares this fold the entries it does not read on the KERNEL's path, but
    ``advance`` and ``fold`` call ``step`` for every entry in the file. A step that
    reached its closer branch by falling through would bill every ``turn/completed`` in
    the log as a child that closed -- which is what the savepoint suite caught: 293
    phantom children, and the session's own turn credits billed as theirs.
    """
    fold = crew_log._FOLDS["subagents"]
    state = fold.start()
    for seq, (entry_type, data) in enumerate(
        (
            ("turn/completed", {"turn": 1, "credits": 0.5, "duration_ms": 900, "ms": 120}),
            ("step/completed", {"turn": 1, "step": 1, "ms": 120}),
            ("background/completed", {"kind": "title", "credits": 0.5, "ms": 20}),
            ("tool/completed", {"name": "fs_read", "call_id": "c-1", "status": "ok"}),
            ("session/opened", {"agent": "kirocrew", "model": "opus"}),
        ),
        start=1,
    ):
        fold.step(state, crew_log.Entry(type=entry_type, seq=seq, time=seq, src=GATEWAY, data=data))

    assert fold.render(state) == fold.render(fold.start())


def test_rows_render_in_dispatch_order():
    handle = _log()
    _opened(handle)
    _spawned(handle, "z-first")
    _spawned(handle, "a-second")

    assert list(_fold()["by_id"]) == ["z-first", "a-second"]
