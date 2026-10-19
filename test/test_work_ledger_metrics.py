"""The four ``kirocrew.work.*`` counters: one per successful ledger write, never more.

Two halves. The bucket and enum functions are exercised directly, on their own
edges and on every shape a stored record can hold that is not a value at all. The
call sites are exercised through the routes, which is the only way to prove the
property that matters: a refused write counts nothing, a committed write counts
exactly once, and a rebuild of the board from the crew log re-counts nothing.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import work_ledger_projection_helpers
from work_ledger_projection_helpers import (
    _SLOTS,
    ACCEPTANCE,
    CONDUCTOR,
    WORKER,
    _board,
    _real_units,
    _rebuild,
    _record,
    _report,
    _Slot,
)

from kiro_crew import work_ledger as wl
from kiro_crew.crew_log.entry_types import CREW_REPORT_STATUSES, CREW_TARGET_KINDS
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.metrics import work as work_metrics
from kiro_crew.work_vocab import WORK_BLOCKED_REASONS, WORK_ITEM_STATES, WORK_WORKER_STATUSES

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated = work_ledger_projection_helpers._isolated
recorded = work_ledger_projection_helpers.recorded


@pytest.fixture
def counted(monkeypatch) -> list[tuple[str, dict]]:
    """Every ``emit_counter`` the work-ledger instruments make, captured in order.

    Patched at the metrics module's own seam rather than at the route's, so the
    enum mapping and the bucketing are inside what each test asserts on.
    """
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        work_metrics,
        "emit_counter",
        lambda name, attrs: calls.append((name, dict(attrs))),
    )
    return calls


def _of(calls: list[tuple[str, dict]], name: str) -> list[dict]:
    return [attrs for emitted, attrs in calls if emitted == name]


def _age_item(item_id: str, **delta) -> None:
    """Move the stored item's ``created_at`` back, so a close spans a chosen duration.

    The route reads the committed record, so this is the only way to produce a
    span the test chooses without sleeping for it. The new stamp is the store's
    OWN ``created_at`` shifted back, never a fresh clock read: the close stamps
    ``closed_at`` moments later from that same clock, so the span is the delta
    asked for, in whatever zone the store writes, on any host.
    """
    path = wl.item_path(CONDUCTOR, item_id)
    record = json.loads(path.read_text())
    stamped = wl.parse_stamp(record["created_at"])
    assert stamped is not None, record["created_at"]
    record["created_at"] = (stamped - timedelta(**delta)).isoformat()
    path.write_text(json.dumps(record))


# -- buckets, on their own edges -------------------------------------------------


def test_depth_buckets_cover_zero_one_and_the_whole_tail():
    assert work_metrics.depth_bucket(0) == "0"
    assert work_metrics.depth_bucket(1) == "1"
    assert work_metrics.depth_bucket(2) == "2+"
    assert work_metrics.depth_bucket(9) == "2+"


def test_fails_buckets_sit_on_each_of_their_edges():
    assert [work_metrics.fails_bucket(n) for n in (0, 1, 2, 3, 4, 99)] == [
        "0",
        "1",
        "2",
        "3+",
        "3+",
        "3+",
    ]


def test_a_depth_or_fail_count_that_is_not_a_count_is_other():
    """A negative, a bool, a numeric string and an absent value each map to ``other``.

    ``True`` is the one worth pinning: it is an ``int`` subclass, so a bucket that
    only compared magnitudes would report it as depth 1 and the series would carry
    a depth nobody wrote.
    """
    for value in (-1, True, False, "1", 1.0, None, object()):
        assert work_metrics.depth_bucket(value) == "other", value
        assert work_metrics.fails_bucket(value) == "other", value


def test_duration_buckets_are_inclusive_of_their_own_edges():
    """600 s is ``le_10m`` and 601 s is ``le_1h``; the same at each later edge."""
    now = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)

    def bucket(seconds: float) -> str:
        return work_metrics.duration_bucket(
            now.isoformat(), (now + timedelta(seconds=seconds)).isoformat()
        )

    assert [bucket(s) for s in (0, 599, 600)] == ["le_10m", "le_10m", "le_10m"]
    assert [bucket(s) for s in (601, 3599, 3600)] == ["le_1h", "le_1h", "le_1h"]
    assert [bucket(s) for s in (3601, 14399, 14400)] == ["le_4h", "le_4h", "le_4h"]
    assert [bucket(s) for s in (14401, 86400)] == ["gt_4h", "gt_4h"]


def test_a_duration_with_nothing_to_measure_is_other():
    """An absent, empty or unparseable stamp at either end yields ``other``.

    Never the smallest bucket: an item whose stamps cannot be read was not open
    for under ten minutes, and a plausible-looking reading is worse than none.
    """
    good = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    for bad in (None, "", "not a stamp", 0, {}):
        assert work_metrics.duration_bucket(bad, good) == "other", bad
        assert work_metrics.duration_bucket(good, bad) == "other", bad


def test_a_close_stamped_before_its_own_create_reads_as_the_smallest_bucket():
    """Both stamps are the store's own, so a backwards span is the host's clock
    moving between two writes -- an item open for approximately no time."""
    now = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    assert (
        work_metrics.duration_bucket(now.isoformat(), (now - timedelta(hours=9)).isoformat())
        == "le_10m"
    )


def test_a_stamp_without_an_offset_still_measures_against_one_that_has_it():
    """The store wrote offsetless stamps once, so a long-lived item's span can
    have a naive end and an aware one. Subtracting across that raises, and a pair
    that is naive at BOTH ends must measure without being shifted at all."""
    naive = datetime(2026, 10, 19, 12, 0, 0)
    aware = naive.astimezone() + timedelta(minutes=30)
    assert work_metrics.duration_bucket(naive.isoformat(), aware.isoformat()) == "le_1h"
    assert work_metrics.duration_bucket(aware.isoformat(), naive.isoformat()) == "le_10m"
    both_naive = naive + timedelta(hours=5)
    assert work_metrics.duration_bucket(naive.isoformat(), both_naive.isoformat()) == "gt_4h"


def test_the_metrics_module_reaches_nothing_in_the_work_ledger_store():
    """Its stamp reading is the stdlib's, not the store's.

    The store is reachable through two allowlisted modules only, and that
    allowlist guards which principal may name a ledger. This module has no
    identity concern and must not appear in it, so it imports nothing from the
    family -- a regression here is what `test_work_ledger.py` and
    `test_work_ledger_facade.py` would also catch, named locally so the reason
    sits beside the code that must honour it.
    """
    source = Path(work_metrics.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not [name for name in imported if "work_ledger" in name], sorted(imported)


# -- the closed enums ------------------------------------------------------------


def test_the_closed_states_are_the_ledgers_own_terminal_states():
    """Derived from the ledger's vocabulary, so a new state reddens here rather
    than quietly sending every close of it to ``other``."""
    assert work_metrics.CLOSED_STATES == ("accepted", "rejected", "abandoned")
    assert set(work_metrics.CLOSED_STATES) == set(WORK_ITEM_STATES) - {"open"}


def test_the_report_enums_match_the_crew_logs_own_report_contract():
    """``failed`` is declared because the crew log's report type names it, and the
    worker half can commit only the other four -- so the route never emits it."""
    assert work_metrics.REPORT_STATUSES == ("progress", "done", "blocked", "question", "failed")
    assert set(work_metrics.REPORT_STATUSES) == set(CREW_REPORT_STATUSES)
    assert "failed" not in WORK_WORKER_STATUSES
    assert work_metrics.REPORT_REASONS == WORK_BLOCKED_REASONS + ("none",)


def test_the_target_kinds_match_the_crew_logs_own_dispatch_contract():
    assert work_metrics.TARGET_KINDS == CREW_TARGET_KINDS


def test_every_declared_attribute_value_is_a_short_closed_token():
    """No value carries an id, a path, a name or free text: each is a short
    lower-case token, which is what keeps the series bounded."""
    declared = (
        work_metrics.CREATED_KINDS
        + work_metrics.DEPTH_BUCKETS
        + work_metrics.CLOSED_STATES
        + work_metrics.FAILS_BUCKETS
        + work_metrics.DURATION_BUCKETS
        + work_metrics.TARGET_KINDS
        + work_metrics.REPORT_STATUSES
        + work_metrics.REPORT_REASONS
        + (work_metrics.OTHER,)
    )
    for value in declared:
        assert value == value.lower(), value
        assert len(value) <= 12, value
        assert set(value) <= set("abcdefghijklmnopqrstuvwxyz0123456789_+"), value


def test_every_bucket_function_answers_only_inside_its_own_enum():
    now = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    allowed_depth = set(work_metrics.DEPTH_BUCKETS) | {work_metrics.OTHER}
    allowed_fails = set(work_metrics.FAILS_BUCKETS) | {work_metrics.OTHER}
    allowed_duration = set(work_metrics.DURATION_BUCKETS) | {work_metrics.OTHER}
    for value in (0, 1, 2, 3, 17, -4, True, None, "2", 2.5):
        assert work_metrics.depth_bucket(value) in allowed_depth
        assert work_metrics.fails_bucket(value) in allowed_fails
    for seconds in (-9999, 0, 600, 601, 3600, 14400, 14401, 10**7):
        assert (
            work_metrics.duration_bucket(
                now.isoformat(), (now + timedelta(seconds=seconds)).isoformat()
            )
            in allowed_duration
        )
    assert work_metrics.duration_bucket("x", "y") in allowed_duration


def test_every_record_function_is_total_over_hostile_input(counted):
    """No `record_*` function can raise, whatever it is handed.

    This is the guarantee that stands in place of a try/except around each emit.
    `emit_counter` already swallows every failure of the emit itself, so the only
    code a wrapper here would cover is the bucketing -- and the assertion is that
    the bucketing maps everything, rather than that something catches it when it
    does not. Delete any `_closed` or `_is_count` guard and this test raises.
    """
    hostile = (None, "", 0, -1, True, 2.5, [], {}, object(), "not a value")
    for value in hostile:
        work_metrics.record_goal_created(value)
        work_metrics.record_item_created(value)
        work_metrics.record_dispatch(value)
        work_metrics.record_report(value, value)
        work_metrics.record_item_closed(value, value, value, value)
    assert len(counted) == len(hostile) * 5
    allowed = {
        "kind": set(work_metrics.CREATED_KINDS),
        "depth": set(work_metrics.DEPTH_BUCKETS) | {work_metrics.OTHER},
        "state": set(work_metrics.CLOSED_STATES) | {work_metrics.OTHER},
        "fails": set(work_metrics.FAILS_BUCKETS) | {work_metrics.OTHER},
        "duration": set(work_metrics.DURATION_BUCKETS) | {work_metrics.OTHER},
        "target_kind": set(work_metrics.TARGET_KINDS) | {work_metrics.OTHER},
        "status": set(work_metrics.REPORT_STATUSES) | {work_metrics.OTHER},
        "reason": set(work_metrics.REPORT_REASONS) | {work_metrics.OTHER},
    }
    for _name, attrs in counted:
        for key, got in attrs.items():
            assert got in allowed[key], (key, got)


# -- created: the board-opening goal, and each item ------------------------------


@pytest.mark.asyncio
async def test_the_goal_that_opens_a_board_counts_once_at_its_depth(counted, recorded):
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
    assert status == 200, body
    assert _of(counted, work_metrics.WORK_CREATED) == [{"kind": "goal", "depth": "0"}]


@pytest.mark.asyncio
async def test_a_reworded_or_restarted_goal_counts_nothing(counted, recorded):
    """Only the ledger-opening goal write is a board. A reword, a round bump, a cap
    raise and an explicit ``new_goal`` are all later writes on the same board, and
    counting them would make the series read as goal writes under a name that
    claims boards."""
    assert (await _record(CONDUCTOR, {"action": "goal", "goal": "ship it"}))[0] == 200
    assert len(_of(counted, work_metrics.WORK_CREATED)) == 1
    for body in (
        {"action": "goal", "goal": "ship it, but better"},
        {"action": "goal", "round": 4},
        {"action": "goal", "item_cap": 7},
        {"action": "goal", "goal": "a different goal", "new_goal": True},
    ):
        status, payload = await _record(CONDUCTOR, body)
        assert status == 200, payload
    assert len(_of(counted, work_metrics.WORK_CREATED)) == 1


@pytest.mark.asyncio
async def test_each_create_counts_one_item_at_the_boards_depth(counted, recorded):
    assert (await _record(CONDUCTOR, {"action": "goal", "goal": "ship it"}))[0] == 200
    for title in ("one", "two"):
        status, body = await _record(
            CONDUCTOR, {"action": "create", "title": title, "acceptance": ACCEPTANCE}
        )
        assert status == 200, body
    assert _of(counted, work_metrics.WORK_CREATED) == [
        {"kind": "goal", "depth": "0"},
        {"kind": "item", "depth": "0"},
        {"kind": "item", "depth": "0"},
    ]


@pytest.mark.asyncio
async def test_a_second_level_boards_creates_count_at_depth_one(counted, recorded):
    """The worker bound to the first board's item opens its own ledger, which
    ``_bootstrap`` puts one past its parent."""
    await _board()
    counted.clear()
    status, body = await _record(WORKER, {"action": "goal", "goal": "my own slice"})
    assert status == 200, body
    status, body = await _record(
        WORKER, {"action": "create", "title": "sub item", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    assert _of(counted, work_metrics.WORK_CREATED) == [
        {"kind": "goal", "depth": "1"},
        {"kind": "item", "depth": "1"},
    ]


@pytest.mark.asyncio
async def test_a_stored_header_past_the_depth_cap_counts_in_the_tail_bucket(counted, recorded):
    """``ensure_conductor`` admits no depth past the cap, but a rebuild keeps the
    depth a stored header holds, so the ``2+`` bucket is reachable and must not
    fall into ``other``."""
    wl.ensure_conductor(CONDUCTOR, goal="")
    path = wl.conductor_dir(CONDUCTOR) / wl._CONDUCTOR_FILE
    record = json.loads(path.read_text())
    record["depth"] = 2
    path.write_text(json.dumps(record))
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "deep board"})
    assert status == 200, body
    assert _of(counted, work_metrics.WORK_CREATED) == [{"kind": "goal", "depth": "2+"}]


# -- refusals count nothing ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_create_refused_past_the_goals_concurrency_cap_counts_nothing(counted, recorded):
    assert (await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "concurrency_cap": 1}))[
        0
    ] == 200
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "one", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    counted.clear()
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "two", "acceptance": ACCEPTANCE}
    )
    assert status == 409, body
    assert body["code"] == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED
    assert counted == []


@pytest.mark.asyncio
async def test_a_validation_refused_write_counts_nothing(counted, recorded):
    """A cap outside its range and a title past its cap: each refused before the
    first store write, so neither reaches a counter."""
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "item_cap": 0})
    assert status == 400, body
    status, body = await _record(
        CONDUCTOR,
        {"action": "create", "title": "t" * (wl.MAX_TITLE_CHARS + 1), "acceptance": ACCEPTANCE},
    )
    assert status == 400, body
    assert counted == []


@pytest.mark.asyncio
async def test_closing_an_already_closed_item_counts_nothing_the_second_time(counted, recorded):
    item_id = await _board()
    counted.clear()
    status, body = await _record(
        CONDUCTOR, {"action": "close", "item_id": item_id, "state": "accepted"}
    )
    assert status == 200, body
    assert len(_of(counted, work_metrics.WORK_ITEM_CLOSED)) == 1
    status, body = await _record(
        CONDUCTOR, {"action": "close", "item_id": item_id, "state": "abandoned"}
    )
    assert status == 409, body
    assert body["code"] == wl.CODE_ITEM_CLOSED
    assert len(_of(counted, work_metrics.WORK_ITEM_CLOSED)) == 1


@pytest.mark.asyncio
async def test_a_write_whose_record_did_not_land_counts_nothing(counted, monkeypatch):
    """The cache commit is undone when the crew log will not take the entry, and
    the route answers a refusal -- so the counter must sit after that, not before.

    The ``recorded`` fixture is deliberately not used here: it is what makes the
    append succeed.
    """
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: False)
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it"})
    assert status != 200, body
    assert counted == []


# -- item_closed: state, fails and duration --------------------------------------


@pytest.mark.asyncio
async def test_a_close_counts_its_state_fails_and_duration(counted, recorded):
    item_id = await _board()
    assert (
        await _record(
            CONDUCTOR, {"action": "verdict", "item_id": item_id, "verdict": "fail", "fails": 2}
        )
    )[0] == 200
    counted.clear()
    status, body = await _record(
        CONDUCTOR, {"action": "close", "item_id": item_id, "state": "rejected"}
    )
    assert status == 200, body
    assert _of(counted, work_metrics.WORK_ITEM_CLOSED) == [
        {"state": "rejected", "fails": "2", "duration": "le_10m"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fails,bucket", [(0, "0"), (1, "1"), (2, "2"), (3, "3+"), (11, "3+")])
async def test_each_fails_bucket_edge_is_reachable_through_a_verdict(
    counted, recorded, fails, bucket
):
    item_id = await _board()
    if fails:
        assert (
            await _record(
                CONDUCTOR,
                {"action": "verdict", "item_id": item_id, "verdict": "fail", "fails": fails},
            )
        )[0] == 200
    counted.clear()
    assert (await _record(CONDUCTOR, {"action": "close", "item_id": item_id, "state": "accepted"}))[
        0
    ] == 200
    assert _of(counted, work_metrics.WORK_ITEM_CLOSED)[0]["fails"] == bucket


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "age,bucket",
    [
        ({"minutes": 5}, "le_10m"),
        ({"minutes": 42}, "le_1h"),
        ({"hours": 3}, "le_4h"),
        ({"hours": 9}, "gt_4h"),
    ],
)
async def test_each_duration_bucket_is_reachable_from_the_committed_stamps(
    counted, recorded, age, bucket
):
    item_id = await _board()
    _age_item(item_id, **age)
    counted.clear()
    assert (await _record(CONDUCTOR, {"action": "close", "item_id": item_id, "state": "accepted"}))[
        0
    ] == 200
    assert _of(counted, work_metrics.WORK_ITEM_CLOSED)[0]["duration"] == bucket


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["accepted", "rejected", "abandoned"])
async def test_each_terminal_state_counts_under_its_own_name(counted, recorded, state):
    item_id = await _board()
    counted.clear()
    assert (await _record(CONDUCTOR, {"action": "close", "item_id": item_id, "state": state}))[
        0
    ] == 200
    assert _of(counted, work_metrics.WORK_ITEM_CLOSED)[0]["state"] == state


# -- dispatches, and the actions that count nothing -------------------------------


@pytest.mark.asyncio
async def test_a_bind_counts_one_dispatch_to_a_session(counted, recorded):
    assert (await _record(CONDUCTOR, {"action": "goal", "goal": "ship it"}))[0] == 200
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "one", "acceptance": ACCEPTANCE}
    )
    item_id = body["item"]["item_id"]
    _SLOTS[WORKER] = _Slot(created_by=CONDUCTOR)
    counted.clear()
    status, body = await _record(
        CONDUCTOR, {"action": "bind", "item_id": item_id, "worker_session_key": WORKER}
    )
    assert status == 200, body
    assert _of(counted, work_metrics.WORK_DISPATCHES) == [{"target_kind": "session"}]


@pytest.mark.asyncio
async def test_a_second_bind_of_the_same_item_counts_no_second_dispatch(counted, recorded):
    item_id = await _board()
    counted.clear()
    status, body = await _record(
        CONDUCTOR, {"action": "bind", "item_id": item_id, "worker_session_key": WORKER}
    )
    assert status == 409, body
    assert counted == []


@pytest.mark.asyncio
async def test_decide_and_verdict_count_nothing_of_their_own(counted, recorded):
    """Neither carries a number this RFC asks for: a decision's text is not a
    metric, and an item's fail count is read off the close it leads to."""
    item_id = await _board()
    counted.clear()
    assert (
        await _record(CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "go on"})
    )[0] == 200
    assert (
        await _record(CONDUCTOR, {"action": "verdict", "item_id": item_id, "verdict": "pending"})
    )[0] == 200
    assert counted == []


# -- reports ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,expected",
    [
        ({"status": "progress", "summary": "half way"}, {"status": "progress", "reason": "none"}),
        (
            {"status": "done", "summary": "landed", "pr": 41},
            {"status": "done", "reason": "none"},
        ),
        (
            {"status": "blocked", "summary": "waiting", "reason": "approval"},
            {"status": "blocked", "reason": "approval"},
        ),
        (
            {"status": "question", "summary": "which base", "reason": "needs_human"},
            {"status": "question", "reason": "needs_human"},
        ),
        (
            {"status": "blocked", "summary": "waiting on a build"},
            {"status": "blocked", "reason": "none"},
        ),
    ],
)
async def test_each_report_counts_once_with_its_status_and_reason(
    counted, recorded, body, expected
):
    await _board()
    counted.clear()
    status, payload = await _report(WORKER, body)
    assert status == 200, payload
    assert _of(counted, work_metrics.WORK_REPORTS) == [expected]


@pytest.mark.asyncio
async def test_a_done_report_refused_for_missing_evidence_counts_nothing(counted, recorded):
    await _board()
    counted.clear()
    status, body = await _report(WORKER, {"status": "done", "summary": "trust me"})
    assert status == 400, body
    assert body["code"] == "done_without_evidence"
    assert counted == []


@pytest.mark.asyncio
async def test_a_failed_status_is_refused_by_the_route_and_counts_nothing(counted, recorded):
    """``failed`` is in the metric's enum because the crew log's report contract
    names it, but the ledger's worker half cannot commit it -- so no report
    through this route ever emits it."""
    await _board()
    counted.clear()
    status, body = await _report(WORKER, {"status": "failed", "summary": "it broke"})
    assert status == 400, body
    assert counted == []


@pytest.mark.asyncio
async def test_a_report_on_a_closed_item_counts_nothing(counted, recorded):
    item_id = await _board()
    assert (await _record(CONDUCTOR, {"action": "close", "item_id": item_id, "state": "accepted"}))[
        0
    ] == 200
    counted.clear()
    status, body = await _report(WORKER, {"status": "progress", "summary": "still going"})
    assert status == 409, body
    assert counted == []


# -- the rebuild, and the telemetry gate -----------------------------------------


@pytest.mark.asyncio
async def test_a_rebuild_from_the_crew_log_re_counts_nothing(counted, monkeypatch):
    """The ledger files are a cache of the crew log, and the rebuild re-materialises
    every item and event from it. Nothing here is instrumented, so a board rebuilt
    after a goal, a create, a bind, a report and a close adds no count at all."""
    _real_units(monkeypatch)
    item_id = await _board()
    assert (await _report(WORKER, {"status": "progress", "summary": "half way"}))[0] == 200
    assert (await _record(CONDUCTOR, {"action": "close", "item_id": item_id, "state": "accepted"}))[
        0
    ] == 200
    before = list(counted)
    assert before, "the writes above should have counted"
    counted.clear()
    status, body = await _rebuild(CONDUCTOR)
    assert status == 200, body
    assert body["items"] >= 1
    assert counted == []


@pytest.mark.asyncio
async def test_a_board_purged_and_reopened_on_the_same_slot_counts_again(counted, recorded):
    """A purge takes the header with it, so the reopened board's own first goal
    write carries ``goal_version == 1`` from a fresh record and counts.

    The opposite reading would under-count: a slot reused after its board was
    deleted holds a genuinely new board, and reporting one where two existed is
    the error the `goal_version == 1` test could plausibly have made.
    """
    item_id = await _board()
    assert _of(counted, work_metrics.WORK_CREATED) == [
        {"kind": "goal", "depth": "0"},
        {"kind": "item", "depth": "0"},
    ]
    # An empty board is not finished, so the purge needs the item closed first.
    assert (await _record(CONDUCTOR, {"action": "close", "item_id": item_id, "state": "accepted"}))[
        0
    ] == 200
    assert wl.purge_conductor(CONDUCTOR, allow_unreadable=True, idle_for=timedelta(0)) is True
    counted.clear()
    assert (await _record(CONDUCTOR, {"action": "goal", "goal": "a new board here"}))[0] == 200
    assert _of(counted, work_metrics.WORK_CREATED) == [{"kind": "goal", "depth": "0"}]


@pytest.mark.asyncio
async def test_nothing_is_recorded_when_telemetry_is_off(monkeypatch, tmp_path, recorded):
    """Not patched at this module's seam: the real ``emit_counter`` runs, and the
    recorder the provider hands it is what decides. With consent the five writes
    below create their instruments; without it the meter is never touched."""
    from kiro_crew.metrics import provider
    from kiro_crew.metrics.recorder import MetricsRecorder

    seen: list[str] = []

    class _Instrument:
        def add(self, value, attributes=None):
            seen.append("add")

    class _Meter:
        def create_counter(self, name, unit="1", description=""):
            seen.append(name)
            return _Instrument()

    async def _drive() -> None:
        item_id = await _board()
        assert (await _report(WORKER, {"status": "progress", "summary": "half way"}))[0] == 200
        assert (
            await _record(CONDUCTOR, {"action": "close", "item_id": item_id, "state": "accepted"})
        )[0] == 200

    monkeypatch.setattr(provider, "get_recorder", lambda: MetricsRecorder(_Meter()))
    await _drive()
    assert work_metrics.WORK_CREATED in seen
    assert work_metrics.WORK_REPORTS in seen

    # A second data home, so the same five writes run again on a fresh board
    # rather than being refused as repeats of the first run's.
    seen.clear()
    _SLOTS.clear()
    monkeypatch.setattr(provider, "get_recorder", lambda: MetricsRecorder(None))
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home-telemetry-off"))
    await _drive()
    assert seen == []
