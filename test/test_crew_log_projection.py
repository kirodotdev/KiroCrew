"""Crew-log projections -- one test per property the folds promise.

The load-bearing one is :func:`test_incremental_matches_from_scratch_at_every_split`:
a projection resumed from a checkpoint must equal the same projection folded from
the start of the file, at EVERY split point. That is what makes a checkpoint safe
to store and a push safe to send incrementally, and it is the property a second
batch implementation would be free to break.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, CrewLogError, Ref
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store

SESSION = "s-fold"
GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _log(unit_id: str = SESSION, **fields) -> CrewLog:
    fields.setdefault("owner", "raymond")
    fields.setdefault("agent", "kirocrew")
    return CrewLog.create(lg.KIND_SESSION, unit_id, **fields)


def _opened(handle: CrewLog, *, resumed: bool = False, model: str = "opus") -> None:
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": "dashboard:1",
            "model": model,
            "cwd": "/w",
            "owner": "raymond",
            "resumed": resumed,
        },
        src=GATEWAY,
    )


def _turn(
    handle: CrewLog,
    turn: int,
    *,
    credits: float | None = 0.5,
    tokens: dict[str, int] | None = None,
    stop_reason: str = "end_turn",
    attempt: int | None = None,
    model: str = "opus",
) -> None:
    """A whole turn: started, one step, completed."""
    for item in _turn_items(
        turn,
        credits=credits,
        tokens=tokens,
        stop_reason=stop_reason,
        attempt=attempt,
        model=model,
    ):
        handle.append(item["type"], item["data"], src=GATEWAY)


def _tool(
    handle: CrewLog, turn: int, call_id: str, name: str, *, status: str = "completed"
) -> None:
    for item in _tool_items(turn, call_id, name, status=status):
        handle.append(item["type"], item["data"], src=GATEWAY)


# --- writing a fixture that has to be LONG --------------------------------- #
#
# A bound in this module is a bound on retained state, so reaching one costs as
# many entries as the bound itself, and ``FOLD_CHUNK_ENTRIES`` is 1024 -- 2049
# entries to cross it twice. One ``append`` per entry is one cross-process lock,
# one tail scan and one ``os.fsync`` EACH, which measures 76-90 ms per entry on
# the Windows CI runner: that puts a 2049-entry fixture at 156-185 s against a
# 180 s per-test cap. A breach there does not fail one test. Windows has no
# SIGALRM, so pytest-timeout falls back to the thread method and terminates the
# whole xdist worker, and the shard runs ``--max-worker-restart=0`` deliberately,
# so one fixture can fail a shard of 1600 tests in a file its author never opened.
#
# So a fixture past a few hundred entries is written with ``append_many``, the
# production group write: the same validation, the same consecutive seqs, the same
# file, one fsync per group. A group shares one ``time`` -- which is what a
# production group write does as well -- and no bound here is a bound on wall
# clock. ``test_a_grouped_fixture_is_the_log_one_append_at_a_time_writes`` pins
# that equivalence, so this stays a change in what the fixture COSTS.

_GROUP_ENTRIES = 512


def _append_grouped(
    handle: CrewLog, items: list[dict[str, Any]], *, group: int = _GROUP_ENTRIES
) -> None:
    """Append *items* with one lock and one fsync per bounded group.

    Bounded rather than one write of everything, so a long fixture still crosses
    the tail scan and the ``needs_newline`` path more than once. *group* is only
    for the test that pins this helper, which must cross a group boundary without
    paying for a long fixture to do it.
    """
    for start in range(0, len(items), group):
        handle.append_many(items[start : start + group], src=GATEWAY)


def _turn_items(
    turn: int,
    *,
    credits: float | None = 0.5,
    tokens: dict[str, int] | None = None,
    stop_reason: str = "end_turn",
    attempt: int | None = None,
    model: str = "opus",
) -> list[dict[str, Any]]:
    """The four entries of one whole turn, unwritten."""
    start: dict[str, Any] = {"turn": turn, "actor": "user", "depth": 0}
    if attempt is not None:
        start["attempt"] = attempt
    done: dict[str, Any] = {
        "turn": turn,
        "stop_reason": stop_reason,
        "depth": 0,
        "duration_ms": 900,
        "model": model,
        "provider": "kiro",
    }
    if credits is not None:
        done["credits"] = credits
        done["tokens"] = tokens or {
            "input": 100,
            "output": 20,
            "cache_read": 5,
            "cache_write": 1,
        }
    return [
        {"type": "turn/started", "data": start},
        {"type": "step/started", "data": {"turn": turn, "step": 1}},
        {"type": "step/completed", "data": {"turn": turn, "step": 1, "ms": 120}},
        {"type": "turn/completed", "data": done},
    ]


def _tool_items(
    turn: int, call_id: str, name: str, *, status: str = "completed"
) -> list[dict[str, Any]]:
    """The call and the completion of one tool frame, unwritten."""
    return [
        {
            "type": "tool/called",
            "data": {
                "turn": turn,
                "call_id": call_id,
                "name": name,
                "server": "core",
                "kind": "mcp",
            },
        },
        {
            "type": "tool/completed",
            "data": {
                "turn": turn,
                "call_id": call_id,
                "name": name,
                "server": "core",
                "status": status,
                "elapsed_ms": 42,
            },
        },
    ]


def _busy_log() -> CrewLog:
    """A session with something for every fold to see."""
    handle = _log()
    _opened(handle)
    handle.append("model/selected", {"model": "opus", "source": "config"}, src=GATEWAY)
    handle.append(
        "request/configured",
        {"turn": 1, "model": "opus", "provider": "kiro", "context_window": 200000},
        src=GATEWAY,
    )
    handle.append(
        "context/composed",
        {
            "turn": 1,
            "step": 1,
            "sources": [
                {"kind": "system", "chars": 400, "tokens": 100},
                {"kind": "memory", "chars": 800, "tokens": 200},
            ],
            "chars": 1200,
            "tokens": 300,
            "tokens_estimated": True,
        },
        src=GATEWAY,
    )
    _turn(handle, 1)
    _tool(handle, 1, "c1", "fs_read")
    _tool(handle, 1, "c2", "fs_write", status="refused")
    handle.append(
        "approval/requested",
        {"turn": 1, "approval_id": "a1", "tool": "shell", "reason": "run tests"},
        src=GATEWAY,
    )
    handle.append(
        "approval/decided",
        {"turn": 1, "approval_id": "a1", "decision": "allow", "by": "raymond"},
        src=GATEWAY,
    )
    handle.append(
        "compaction/applied",
        {"pct_before": 82.0, "pct_after": 41.0, "freed_pct": 41.0},
        src=GATEWAY,
    )
    _turn(handle, 2, credits=None, stop_reason="interrupted")
    handle.append("write/dropped", {"dropped_count": 3, "dropped_bytes": 900}, src=GATEWAY)
    handle.append("session/closed", {"reason": "reset"}, src=GATEWAY)
    return handle


def _entries(handle: CrewLog) -> tuple:
    return tuple(handle.iter_from(1))


# --- the contract ---------------------------------------------------------


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


# --- reading a session ----------------------------------------------------


def test_a_session_with_no_crew_log_folds_to_the_empty_projections():
    bundle = crew_log.fold_session("s-absent")
    assert bundle.last_seq == 0
    assert set(bundle.checkpoints) == set(crew_log.PROJECTION_NAMES)
    assert bundle.projection("status").value["lifecycle"] == "unknown"
    assert crew_log.read_projection("s-absent", "usage").seq == 0


def test_fold_session_serves_every_projection_from_one_pass():
    _busy_log()
    bundle = crew_log.fold_session(SESSION)
    assert set(bundle.checkpoints) == set(crew_log.PROJECTION_NAMES)
    for name in crew_log.PROJECTION_NAMES:
        assert bundle.projection(name).value == crew_log.read_projection(SESSION, name).value


def test_fold_session_continues_from_an_earlier_bundle():
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    first = crew_log.fold_session(SESSION)
    _turn(handle, 2, credits=2.0)
    second = crew_log.fold_session(SESSION, since=first)
    assert second.last_seq > first.last_seq
    assert second.projection("usage").value == crew_log.fold_usage(_entries(handle))


def test_fold_session_rebuilds_when_the_log_is_shorter_than_the_bundle():
    """A recreated unit restarts seq, so continuing would swallow the new log.

    The stale bundle carries a DIFFERENT session's state under this session's id
    and a seq beyond this file's end, which is the shape a cache holds after the
    unit it was folded from is removed and recreated. Reusing it returns the other
    session's totals and reports them at this file's seq.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)

    busier = _log("s-busier")
    _opened(busier)
    for turn in (1, 2, 3):
        _turn(busier, turn, credits=5.0)
    foreign = crew_log.fold_session("s-busier")
    ahead = handle.last_seq + 100
    stale = crew_log.SessionProjections(
        session_id=SESSION,
        last_seq=ahead,
        checkpoints={
            name: crew_log.Checkpoint(name=name, last_seq=ahead, state=checkpoint.state)
            for name, checkpoint in foreign.checkpoints.items()
        },
    )

    rebuilt = crew_log.fold_session(SESSION, since=stale)
    honest = crew_log.fold_usage(_entries(handle))
    assert rebuilt.last_seq == handle.last_seq
    assert rebuilt.projection("usage").value == honest
    assert honest["turns"]["completed"] == 1


def test_fold_session_ignores_a_bundle_belonging_to_another_session():
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    other = _log("s-other")
    _opened(other)
    foreign = crew_log.fold_session("s-other")
    folded = crew_log.fold_session(SESSION, since=foreign)
    assert folded.projection("usage").value == crew_log.fold_usage(_entries(handle))


def test_fold_session_rebuilds_when_the_bundle_lacks_a_name_it_is_asked_for():
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    partial = crew_log.fold_session(SESSION, ("status",))
    full = crew_log.fold_session(SESSION, crew_log.PROJECTION_NAMES, since=partial)
    assert full.projection("usage").value == crew_log.fold_usage(_entries(handle))


def test_fold_session_refuses_an_entry_type_it_cannot_interpret():
    """A required unknown type stops the fold rather than skewing a total."""
    handle = _log()
    _opened(handle)
    path = handle.path
    line = json.dumps(
        {
            "type": "turn/teleported",
            "seq": handle.last_seq + 1,
            "time": 1789000000000,
            "src": GATEWAY,
            "data": {"turn": 1},
        }
    )
    with path.open("a", encoding="utf-8") as sink:
        sink.write(line + "\n")
    with pytest.raises(CrewLogError) as excinfo:
        crew_log.fold_session(SESSION)
    assert excinfo.value.code == lg.CODE_UNKNOWN_ENTRY_TYPE


def test_fold_session_skips_an_ignorable_type_it_cannot_interpret():
    handle = _log()
    _opened(handle)
    line = json.dumps(
        {
            "type": "sample/taken",
            "seq": handle.last_seq + 1,
            "time": 1789000000000,
            "src": GATEWAY,
            "ignorable": True,
            "data": {},
        }
    )
    with handle.path.open("a", encoding="utf-8") as sink:
        sink.write(line + "\n")
    assert crew_log.fold_session(SESSION).projection("status").value["lifecycle"] == "open"


def test_open_session_log_reads_without_claiming_write_ownership():
    """A read must not refuse just because the live writer holds the lease."""
    writer = _log()
    _opened(writer)
    reader = crew_log.open_session_log(SESSION)
    assert reader is not None
    assert reader.last_seq == writer.last_seq
    # The writer still owns its unit: the read took nothing away from it.
    writer.append("session/closed", {"reason": "reset"}, src=GATEWAY)


def test_open_session_log_is_none_for_a_session_that_has_none():
    assert crew_log.open_session_log("s-nothing") is None


def test_known_types_is_the_declared_session_vocabulary():
    assert crew_log.KNOWN_TYPES == frozenset(lg.SESSION_ENTRY_TYPES)


def test_the_fold_registry_and_the_public_name_list_agree():
    assert tuple(crew_log._FOLDS) == crew_log.FOLD_NAMES


def test_a_ref_on_a_session_entry_is_left_to_the_page_path():
    """A fold reads its own crew log only (FR-4), so a ref changes no fold's value."""
    handle = _log()
    _opened(handle)
    handle.append(
        "subagent/spawned",
        {"turn": 1, "agent_id": "sub-1", "agent": "worker", "model": "opus"},
        src=GATEWAY,
        ref=Ref(unit=lg.KIND_SESSION, id="s-child", from_seq=1, to_seq=4),
    )
    value = crew_log.fold_timeline(_entries(handle))
    assert value["moments"][-1]["type"] == "subagent/spawned"
    assert "ref" not in value["moments"][-1]


# --------------------------------------------------------------------------- #
# bounded state (a-bound-bounds-every-field-it-retains)
# --------------------------------------------------------------------------- #


def test_tools_open_map_is_bounded_when_calls_are_never_completed():
    """Never-matched open tool calls do not grow the checkpoint without bound.

    A session that opens far more distinct calls than it completes must keep the
    retained ``open`` map bounded and count the ones it dropped, the same way the
    render already caps the listed window.
    """
    handle = _log()
    _opened(handle)
    overflow = crew_log.OPEN_RETAIN_LIMIT + 25
    _append_grouped(
        handle,
        [
            {
                "type": "tool/called",
                "data": {
                    "turn": 1,
                    "call_id": f"open-{index}",
                    "name": "fs_read",
                    "server": "core",
                    "kind": "mcp",
                },
            }
            for index in range(overflow)
        ],
    )
    bundle = crew_log.fold_session(SESSION, ("tools",))
    state = bundle.checkpoints["tools"].state
    assert len(state["open"]) == crew_log.OPEN_RETAIN_LIMIT
    assert state["open_omitted"] == overflow - crew_log.OPEN_RETAIN_LIMIT
    value = bundle.projection("tools").value
    assert value["open"] == crew_log.OPEN_RETAIN_LIMIT
    assert value["open_dropped"] == overflow - crew_log.OPEN_RETAIN_LIMIT


def test_approvals_pending_map_is_bounded_when_requests_are_never_decided():
    """Never-decided approval requests keep the retained ``pending`` map bounded."""
    handle = _log()
    _opened(handle)
    overflow = crew_log.OPEN_RETAIN_LIMIT + 10
    _append_grouped(
        handle,
        [
            {
                "type": "approval/requested",
                "data": {"turn": 1, "approval_id": f"ap-{index}", "tool": "shell", "reason": "x"},
            }
            for index in range(overflow)
        ],
    )
    bundle = crew_log.fold_session(SESSION, ("approvals",))
    state = bundle.checkpoints["approvals"].state
    assert len(state["pending"]) == crew_log.OPEN_RETAIN_LIMIT
    assert state["pending_omitted"] == overflow - crew_log.OPEN_RETAIN_LIMIT
    assert bundle.projection("approvals").value["pending_dropped"] == (
        overflow - crew_log.OPEN_RETAIN_LIMIT
    )


def test_usage_by_model_is_bounded_when_many_models_appear():
    """Many distinct models keep the retained ``by_model`` map bounded while the
    whole-session totals stay exact."""
    handle = _log()
    _opened(handle)
    overflow = crew_log.MODEL_LIMIT + 15
    _append_grouped(
        handle,
        [
            item
            for turn in range(1, overflow + 1)
            for item in _turn_items(turn, credits=1.0, model=f"model-{turn}")
        ],
    )
    value = crew_log.fold_session(SESSION, ("usage",)).projection("usage").value
    assert len(value["by_model"]) == crew_log.MODEL_LIMIT
    assert value["models_omitted"] == overflow - crew_log.MODEL_LIMIT
    # The whole-session totals count every turn, budget or not.
    assert value["turns"]["completed"] == overflow
    assert value["turns"]["credits_reported"] == overflow
    assert value["credits"] == float(overflow)


def test_tool_row_servers_are_bounded_per_name():
    """One tool called through many distinct servers bounds its ``servers`` list."""
    handle = _log()
    _opened(handle)
    overflow = crew_log.SERVERS_PER_TOOL_LIMIT + 8
    for index in range(overflow):
        handle.append(
            "tool/called",
            {
                "turn": 1,
                "call_id": f"c{index}",
                "name": "fs_read",
                "server": f"srv-{index}",
                "kind": "mcp",
            },
            src=GATEWAY,
        )
    row = crew_log.fold_session(SESSION, ("tools",)).projection("tools").value["by_name"]["fs_read"]
    assert len(row["servers"]) == crew_log.SERVERS_PER_TOOL_LIMIT
    assert row["servers_omitted"] == overflow - crew_log.SERVERS_PER_TOOL_LIMIT


def test_servers_omitted_counts_distinct_servers_not_repeated_calls():
    """Repeated calls through one over-budget server count as one omitted server."""
    handle = _log()
    _opened(handle)
    # Fill the listed budget with distinct servers, then call ONE extra server
    # many times: it is one omitted server, not many.
    for index in range(crew_log.SERVERS_PER_TOOL_LIMIT):
        handle.append(
            "tool/called",
            {
                "turn": 1,
                "call_id": f"c{index}",
                "name": "fs_read",
                "server": f"srv-{index}",
                "kind": "mcp",
            },
            src=GATEWAY,
        )
    for repeat in range(5):
        handle.append(
            "tool/called",
            {
                "turn": 1,
                "call_id": f"x{repeat}",
                "name": "fs_read",
                "server": "overflow",
                "kind": "mcp",
            },
            src=GATEWAY,
        )
    row = crew_log.fold_session(SESSION, ("tools",)).projection("tools").value["by_name"]["fs_read"]
    assert row["servers_omitted"] == 1


# --------------------------------------------------------------------------- #
# recreated-then-grown reuse (residual/crash-data-loss-corruption)
# --------------------------------------------------------------------------- #


def test_fold_session_rebuilds_when_the_bundle_origin_does_not_match_the_file():
    """A bundle whose origin differs from the current file is not reused.

    This is the removed-and-recreated case that has already grown PAST the cached
    seq: the seq guard (``since.last_seq <= last_seq``) passes because the file is
    longer than the stale bundle, so without the file-identity check the old
    file's state would be folded over the new file's bytes. A recreated log has a
    different origin (a fresh ``created_at`` and/or inode), so the reuse is
    refused and the fold rebuilds from the start. Filesystem-free so it behaves
    the same on every platform.
    """
    handle = _log()
    _opened(handle)
    for turn in range(1, 6):
        _turn(handle, turn, credits=1.0)

    # A stale bundle from a DIFFERENT file: real checkpoints (so a wrong reuse
    # would fold visibly wrong totals) but a foreign origin and a seq below the
    # current head, the exact recreated-then-grown shape.
    other = _log("s-other-origin")
    _opened(other)
    for turn in (1, 2):
        _turn(other, turn, credits=99.0)
    foreign = crew_log.fold_session("s-other-origin")
    stale = crew_log.SessionProjections(
        session_id=SESSION,
        last_seq=foreign.last_seq,
        checkpoints=foreign.checkpoints,
        origin="not-this-file",
    )
    assert stale.last_seq < handle.last_seq  # seq guard alone would pass

    rebuilt = crew_log.fold_session(SESSION, since=stale)
    honest = crew_log.fold_usage(_entries(handle))
    assert rebuilt.projection("usage").value == honest
    assert rebuilt.origin is not None and rebuilt.origin != stale.origin


def test_fold_session_reuses_the_bundle_when_the_file_is_the_same():
    """The origin guard does not defeat a legitimate incremental reuse."""
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    first = crew_log.fold_session(SESSION, log=handle)
    _turn(handle, 2, credits=1.0)
    second = crew_log.fold_session(SESSION, since=first, log=handle)
    assert second.origin == first.origin
    assert second.projection("usage").value == crew_log.fold_usage(_entries(handle))


def _rewrite_in_place(target, lines: list[str]) -> None:
    """Replace *target*'s whole content, keeping its device and inode.

    A removal and a create would hand the new file a new inode on most runs, and
    then device and inode refuse the reuse on their own -- so a test written that
    way is decided by whether the filesystem recycled the inode, and passes
    without the identity it means to exercise. Truncating in place is the
    recycled-inode case made deterministic: same device, same inode, different
    file. The assertion below is what keeps it that way.
    """
    before = target.stat()
    with target.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")
    after = target.stat()
    assert (after.st_dev, after.st_ino) == (
        before.st_dev,
        before.st_ino,
    ), "the rewrite moved the file, so device and inode alone would refuse it"


def test_the_file_identity_reads_the_creation_stamp_from_disk_not_from_the_handle():
    """A handle's own header describes the file that existed when it was opened.

    The identity exists to separate a recreated log from the one a reader started
    on. Taken from the open handle, the stamp cannot do that: it is parsed once at
    open and keeps answering for the replaced file, leaving device and inode as the
    only live signal -- and those agree whenever the new file landed on the freed
    inode, which is the common case. Here they are held IDENTICAL on purpose, so
    only a stamp read from disk can tell the two files apart.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    before = crew_log.log_origin(handle)
    assert before is not None

    lines = handle.path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["createdAt"] = header["createdAt"] + 1
    _rewrite_in_place(handle.path, [json.dumps(header), *lines[1:]])

    after = crew_log.log_origin(handle)
    assert after is not None, "a readable header is a provable identity"
    assert after != before


def test_a_log_recreated_under_a_held_handle_is_folded_again_rather_than_spliced():
    """The caller passes its handle back in, so the reuse must not trust it.

    ``fold_session`` takes ``log=`` from a caller that already holds one, which is
    what makes this reachable without any race: the handle was opened before the
    recreation, so a stamp read from it still names the retired file. The seq guard
    passes because the new file is longer, so nothing else stands between the old
    file's folded state and the new file's bytes -- the totals below are the old
    turn plus part of the new ones, a number no file on disk ever held.
    """
    handle = _log()
    _opened(handle)
    _turn(handle, 1, credits=1.0)
    first = crew_log.fold_session(SESSION, log=handle)

    # A different, longer log, written over the first one's bytes: the same id, a
    # fresh creation stamp, and turns whose credits could not come from the file
    # the bundle was folded from.
    donor = _log("s-recreated")
    _opened(donor)
    for turn in (1, 2, 3):
        _turn(donor, turn, credits=99.0)
    body = donor.path.read_text(encoding="utf-8").splitlines()
    header = json.loads(body[0])
    header["id"] = SESSION
    retired = json.loads(handle.path.read_text(encoding="utf-8").splitlines()[0])
    header["createdAt"] = retired["createdAt"] + 1
    _rewrite_in_place(handle.path, [json.dumps(header), *body[1:]])

    settled = CrewLog.open(lg.KIND_SESSION, SESSION)
    assert first.last_seq <= settled.last_seq, "the seq guard alone would allow the reuse"
    honest = crew_log.fold_usage(_entries(settled))

    rebuilt = crew_log.fold_session(SESSION, since=first, log=handle)

    assert rebuilt.projection("usage").value == honest
    assert rebuilt.origin is not None and rebuilt.origin != first.origin


# --------------------------------------------------------------------------- #
# bounds on what a fold RETAINS, and on what one pass holds
# --------------------------------------------------------------------------- #


def test_a_cold_fold_crossing_the_chunk_boundary_matches_the_whole_file(monkeypatch):
    """Folding a span in chunks is the same value as folding it whole.

    ``fold_session`` reads the log one bounded chunk at a time so a cold fold of a
    long log does not hold the whole thing in memory. That is only safe if the
    chunk boundary is invisible in the result, so this drives a log well past
    ``FOLD_CHUNK_ENTRIES``, compares against the from-scratch fold, AND counts the
    passes -- otherwise the test would still pass if the chunking were removed.
    """
    handle = _log()
    _opened(handle)
    # Two entries per tool, so this clears the chunk boundary several times over.
    items: list[dict[str, Any]] = []
    for index in range(crew_log.FOLD_CHUNK_ENTRIES):
        items.extend(_tool_items(1, f"c{index}", "fs_read"))
    _append_grouped(handle, items)
    assert handle.last_seq > crew_log.FOLD_CHUNK_ENTRIES

    passes = []
    real_advance_all = crew_log._advance_all

    def counting(checkpoints, chunk):
        passes.append(len(chunk))
        return real_advance_all(checkpoints, chunk)

    monkeypatch.setattr(crew_log, "_advance_all", counting)
    chunked = crew_log.fold_session(SESSION, ("tools",)).projection("tools").value
    whole = crew_log.fold_tools(_entries(handle))
    assert chunked == whole
    assert chunked["calls"] == crew_log.FOLD_CHUNK_ENTRIES
    # More than one pass, and no pass bigger than the chunk bound.
    assert len(passes) > 1
    assert max(passes) <= crew_log.FOLD_CHUNK_ENTRIES


def test_a_grouped_fixture_is_the_log_one_append_at_a_time_writes(monkeypatch):
    """Writing a fixture in groups changes its COST, not the log a fold reads.

    The fixtures above reach bounds that are themselves in the hundreds or
    thousands, and one ``append`` per entry is one ``os.fsync`` per entry. The
    cold-fold fixture's 2049 of those measure 156-185 s on the Windows runner
    against a 180 s per-test cap, and a breach there terminates the whole xdist
    worker instead of failing one test, so a shard of 1600 tests dies on a file its
    author never opened. Substituting ``append_many`` is only SAFE if the entries
    are the same ones, and only WORTH it if the durable writes stop scaling with
    the entry count -- so this pins both halves. Without it, an edit can quietly
    restore the per-entry cost, and the only signal is an intermittent Windows
    worker death with no assertion to read.
    """
    monkeypatch.setattr(store, "now_ms", lambda: 1_700_000_000_000)
    syncs = {"n": 0}
    real_write_then_sync = store._write_then_sync

    def counting(path, blob):
        syncs["n"] += 1
        return real_write_then_sync(path, blob)

    monkeypatch.setattr(store, "_write_then_sync", counting)

    # Small, and a group size to match: this test must cross a group boundary
    # without paying the cost it exists to remove.
    tools, group = 10, 8
    items = [item for index in range(tools) for item in _tool_items(1, f"c{index}", "fs_read")]

    one_at_a_time = _log("s-one-at-a-time")
    syncs["n"] = 0
    for index in range(tools):
        _tool(one_at_a_time, 1, f"c{index}", "fs_read")
    per_entry_syncs = syncs["n"]

    grouped = _log("s-grouped")
    syncs["n"] = 0
    _append_grouped(grouped, items, group=group)
    grouped_syncs = syncs["n"]

    # The same entries, in the same order, with the same seqs and bodies.
    assert [entry.to_dict() for entry in _entries(grouped)] == [
        entry.to_dict() for entry in _entries(one_at_a_time)
    ]
    assert grouped.last_seq == one_at_a_time.last_seq == len(items)

    # One durable write per entry becomes one per group.
    assert per_entry_syncs == len(items)
    assert grouped_syncs == -(-len(items) // group)
    assert grouped_syncs < per_entry_syncs


def test_an_over_long_call_id_is_counted_and_not_retained():
    """An id too big to retain identifies nothing, exactly like an absent one.

    A ``call_id`` comes off the wire and nothing on that path caps its length, so
    retaining it raw would let a few frames outweigh the whole entry budget the
    ``open`` map is counted against. It cannot be TRUNCATED to fit -- two distinct
    ids sharing a head would become one identity and a completion would close the
    wrong frame -- so it takes the unidentified path instead.
    """
    handle = _log()
    _opened(handle)
    handle.append(
        "tool/called",
        {
            "turn": 1,
            "call_id": "x" * (crew_log.ID_LIMIT + 1),
            "name": "fs_read",
            "server": "core",
            "kind": "mcp",
        },
        src=GATEWAY,
    )
    value = crew_log.fold_tools(_entries(handle))
    assert value["calls"] == 1
    assert value["unidentified_calls"] == 1
    assert value["open"] == 0

    state = crew_log.fold_session(SESSION, ("tools",)).checkpoints["tools"].state
    assert state["open"] == {}
    # An id one character shorter is ordinary and IS retained, so the refusal is
    # the length and not the shape.
    other = _log("s-id-edge")
    _opened(other)
    other.append(
        "tool/called",
        {
            "turn": 1,
            "call_id": "x" * crew_log.ID_LIMIT,
            "name": "fs_read",
            "server": "core",
            "kind": "mcp",
        },
        src=GATEWAY,
    )
    kept = crew_log.fold_tools(_entries(other))
    assert kept["unidentified_calls"] == 0
    assert kept["open"] == 1


def test_an_over_long_call_id_on_a_completion_does_not_inflate_unmatched():
    """The completion side coerces the id the same way the call side does.

    If a completion paired on the raw id while the call retained a coerced one,
    the two would disagree about what an identity is: the same frame would be
    unbounded on one side and reported unmatched on the other.
    """
    handle = _log()
    _opened(handle)
    handle.append(
        "tool/completed",
        {
            "turn": 1,
            "call_id": "y" * (crew_log.ID_LIMIT + 1),
            "name": "fs_read",
            "server": "core",
            "status": "completed",
        },
        src=GATEWAY,
    )
    value = crew_log.fold_tools(_entries(handle))
    assert value["completed"] == 1
    assert value["unmatched_completions"] == 0


def test_an_over_long_approval_id_is_counted_and_not_retained():
    """The pending-approval map bounds its identity the same way tools do."""
    handle = _log()
    _opened(handle)
    handle.append(
        "approval/requested",
        {
            "turn": 1,
            "approval_id": "z" * (crew_log.ID_LIMIT + 1),
            "tool": "execute_bash",
            "reason": "writes a file",
        },
        src=GATEWAY,
    )
    value = crew_log.fold_approvals(_entries(handle))
    assert value["requested"] == 1
    assert value["unidentified_requests"] == 1
    assert value["pending"] == 0


def test_names_omitted_stops_counting_rather_than_counting_a_name_twice():
    """Past the dedup budget the count says it is a floor instead of inflating.

    ``names_omitted`` is a count of DISTINCT names left out of the detail, and it
    is deduplicated against a list that is itself capped. A name arriving once the
    list is full is not recognised as already-seen, so counting it again would
    count one name once per appearance -- a call and its completion both reach
    here -- and the figure would climb past the number of names that exist.
    """
    handle = _log()
    _opened(handle)
    distinct = crew_log.TOOL_NAME_LIMIT * 3
    _append_grouped(
        handle,
        [
            item
            for index in range(distinct)
            for item in _tool_items(1, f"c{index}", f"tool_{index:05d}")
        ],
    )
    value = crew_log.fold_tools(_entries(handle))

    omitted_names = distinct - crew_log.TOOL_NAME_LIMIT
    assert len(value["by_name"]) == crew_log.TOOL_NAME_LIMIT
    # The count never exceeds the number of names actually left out -- the whole
    # point -- and stops at the dedup budget, saying so.
    assert value["names_omitted"] <= omitted_names
    assert value["names_omitted"] == crew_log.TOOL_NAME_LIMIT
    assert value["names_omitted_saturated"] is True
    # Totals stay exact regardless.
    assert value["calls"] == distinct
    assert value["completed"] == distinct


def test_names_omitted_is_exact_and_unsaturated_inside_the_dedup_budget():
    """Below the budget the count is a total, not a floor."""
    handle = _log()
    _opened(handle)
    extra = 4
    _append_grouped(
        handle,
        [
            item
            for index in range(crew_log.TOOL_NAME_LIMIT + extra)
            for item in _tool_items(1, f"c{index}", f"tool_{index:05d}")
        ],
    )
    value = crew_log.fold_tools(_entries(handle))
    assert value["names_omitted"] == extra
    assert value["names_omitted_saturated"] is False


def test_a_retained_label_is_cut_to_the_text_limit():
    """A label is retained, so its SIZE is part of the bound on the state.

    This is about a label that is pure display and never distinguishes one thing
    from another -- an approval's reason -- so cutting it is safe and is the
    honest bound: the entry stays, bounded. A label used as a KEY takes the other
    path and is refused rather than cut, because two cut keys could merge.
    """
    handle = _log()
    _opened(handle)
    handle.append(
        "approval/requested",
        {
            "turn": 1,
            "approval_id": "a1",
            "tool": "execute_bash",
            "reason": "r" * (crew_log.TEXT_LIMIT * 20),
        },
        src=GATEWAY,
    )
    value = crew_log.fold_approvals(_entries(handle))
    (pending,) = value["pending_requests"]
    assert len(pending["reason"]) == crew_log.TEXT_LIMIT
    assert value["pending"] == 1


def test_two_names_sharing_a_cut_head_do_not_merge_their_totals():
    """A label at the cut length is not keyed, so distinct tools stay distinct.

    A retained label is cut to ``TEXT_LIMIT``, which means a label sitting exactly
    at that length cannot be told apart from one that was cut. Keying on it would
    put two unrelated tools in one row reporting each other's calls -- a wrong
    answer, not merely a big value -- so such a label gets no detail row and is
    counted as omitted instead.
    """
    handle = _log()
    _opened(handle)
    head = "t" * crew_log.TEXT_LIMIT
    _tool(handle, 1, "c1", head + "-alpha")
    _tool(handle, 1, "c2", head + "-beta")
    value = crew_log.fold_tools(_entries(handle))

    # Neither name is detailed, and no row claims both calls.
    assert value["by_name"] == {}
    # The two names are indistinguishable ONCE CUT, so the omitted count reports
    # one label rather than claiming two it cannot tell apart. The load-bearing
    # property is above -- no row carrying both tools' calls -- and below: the
    # whole-session totals stay exact whatever the detail can and cannot separate.
    assert value["names_omitted"] == 1
    assert value["calls"] == 2
    assert value["completed"] == 2


def test_a_name_just_under_the_cut_length_still_gets_its_row():
    """The refusal is the cut length, not long names in general."""
    handle = _log()
    _opened(handle)
    name = "u" * (crew_log.TEXT_LIMIT - 1)
    _tool(handle, 1, "c1", name)
    value = crew_log.fold_tools(_entries(handle))
    assert list(value["by_name"]) == [name]
    assert value["names_omitted"] == 0


def test_models_omitted_counts_models_not_the_turns_they_ran():
    """One omitted model running many turns is one omitted model.

    The per-model detail is capped, and the count beside it is a count of MODELS.
    Counting it per turn would make it climb past the number of models that exist,
    which is the same defect the tool-name count avoids.
    """
    handle = _log()
    _opened(handle)
    _append_grouped(
        handle,
        [
            item
            for index in range(crew_log.MODEL_LIMIT)
            for item in _turn_items(index + 1, credits=1.0, model=f"model_{index:04d}")
        ],
    )
    # One further model, run over MANY turns.
    turns = 25
    _append_grouped(
        handle,
        [
            item
            for index in range(turns)
            for item in _turn_items(
                crew_log.MODEL_LIMIT + index + 1, credits=1.0, model="one-extra"
            )
        ],
    )
    value = crew_log.fold_usage(_entries(handle))

    assert len(value["by_model"]) == crew_log.MODEL_LIMIT
    assert value["models_omitted"] == 1
    assert value["models_omitted_saturated"] is False
    # The whole-session totals still count every turn exactly.
    assert value["turns"]["completed"] == crew_log.MODEL_LIMIT + turns


def test_models_omitted_saturates_rather_than_counting_a_model_twice():
    """Past the dedup budget the model count says it is a floor."""
    handle = _log()
    _opened(handle)
    distinct = crew_log.MODEL_LIMIT * 3
    _append_grouped(
        handle,
        [
            item
            for index in range(distinct)
            for item in _turn_items(index + 1, credits=1.0, model=f"model_{index:05d}")
        ],
    )
    value = crew_log.fold_usage(_entries(handle))
    omitted = distinct - crew_log.MODEL_LIMIT
    assert value["models_omitted"] <= omitted
    assert value["models_omitted"] == crew_log.MODEL_LIMIT
    assert value["models_omitted_saturated"] is True


def test_a_server_label_is_bounded_and_not_listed_at_full_length():
    """A server name is retained per tool, so its size is part of the bound."""
    handle = _log()
    _opened(handle)
    handle.append(
        "tool/called",
        {
            "turn": 1,
            "call_id": "c1",
            "name": "fs_read",
            "server": "s" * (crew_log.TEXT_LIMIT * 20),
            "kind": "mcp",
        },
        src=GATEWAY,
    )
    row = crew_log.fold_tools(_entries(handle))["by_name"]["fs_read"]
    # Nothing retained at full length: a label at the cut length cannot be told
    # apart from a cut one, so it is counted rather than listed.
    assert all(len(server) < crew_log.TEXT_LIMIT for server in row["servers"])
    assert row["servers"] == []
    assert row["servers_omitted"] == 1

    state = crew_log.fold_session(SESSION, ("tools",)).checkpoints["tools"].state
    assert all(
        len(server) <= crew_log.TEXT_LIMIT
        for detail in state["by_name"].values()
        for server in detail["servers"] + detail["servers_over"]
    )


def test_servers_omitted_counts_distinct_servers_and_says_when_it_saturates():
    """The omitted-server count is exact to its budget, then says it is a floor."""
    handle = _log()
    _opened(handle)
    total = crew_log.SERVERS_PER_TOOL_LIMIT * 3
    for index in range(total):
        handle.append(
            "tool/called",
            {
                "turn": 1,
                "call_id": f"c{index}",
                "name": "fs_read",
                "server": f"server_{index:04d}",
                "kind": "mcp",
            },
            src=GATEWAY,
        )
    row = crew_log.fold_tools(_entries(handle))["by_name"]["fs_read"]
    omitted = total - crew_log.SERVERS_PER_TOOL_LIMIT
    assert len(row["servers"]) == crew_log.SERVERS_PER_TOOL_LIMIT
    # Never more than the servers actually left out, and it says it stopped.
    assert row["servers_omitted"] <= omitted
    assert row["servers_omitted"] == crew_log.SERVERS_PER_TOOL_LIMIT
    assert row["servers_omitted_saturated"] is True
    # Calling repeatedly through one already-counted server adds nothing.
    before = row["servers_omitted"]
    handle.append(
        "tool/called",
        {
            "turn": 1,
            "call_id": "again",
            "name": "fs_read",
            "server": "server_0100",
            "kind": "mcp",
        },
        src=GATEWAY,
    )
    assert crew_log.fold_tools(_entries(handle))["by_name"]["fs_read"]["servers_omitted"] == before


def test_a_retained_status_string_is_bounded():
    """A status echo is retained too, and a nullable one keeps its absence."""
    handle = _log()
    _opened(handle)
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src=GATEWAY)
    handle.append(
        "turn/completed",
        {
            "turn": 1,
            "stop_reason": "x" * (crew_log.TEXT_LIMIT * 20),
            "error": "e" * (crew_log.TEXT_LIMIT * 20),
            "model": "m" * (crew_log.TEXT_LIMIT * 20),
            "depth": 0,
        },
        src=GATEWAY,
    )
    value = crew_log.fold_status(_entries(handle))
    assert len(value["last_stop_reason"]) == crew_log.TEXT_LIMIT
    assert len(value["last_error"]) == crew_log.TEXT_LIMIT
    assert len(value["model"]) == crew_log.TEXT_LIMIT
    # Absence still reads as absence, not as a reason of no characters.
    assert crew_log.fold_status(_entries(_log("s-no-close")))["close_reason"] is None


def test_a_close_mid_turn_still_reports_the_turn_as_open():
    """A close is not a turn ending, and the fold must not claim it was.

    A session cut off mid-turn writes ``session/closed`` with no
    ``turn/completed``. Clearing the open turn here would assert the turn finished
    when nothing recorded it doing so, and would erase the one fact a reader wants:
    this session died with work in flight. Only ``turn/completed`` closes a turn.
    """
    handle = _log()
    _opened(handle)
    handle.append("turn/started", {"turn": 4, "actor": "user", "depth": 0}, src=GATEWAY)
    handle.append("session/closed", {"reason": "crashed"}, src=GATEWAY)

    value = crew_log.fold_status(_entries(handle))
    assert value["lifecycle"] == "closed"
    assert value["close_reason"] == "crashed"
    assert value["turn_open"] is True, "a turn nothing completed is still open"
