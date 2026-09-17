"""Crew-log projections -- one test per property the folds promise.

The load-bearing one is :func:`test_incremental_matches_from_scratch_at_every_split`:
a projection resumed from a checkpoint must equal the same projection folded from
the start of the file, at EVERY split point. That is what makes a checkpoint safe
to store and a push safe to send incrementally, and it is the property a second
batch implementation would be free to break.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import Ledger, LedgerError, Ref
from kiro_crew.crew_log import projection as crew_log

SESSION = "s-fold"
GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _log(unit_id: str = SESSION, **fields) -> Ledger:
    fields.setdefault("owner", "raymond")
    fields.setdefault("agent", "kirocrew")
    return Ledger.create(lg.KIND_SESSION, unit_id, **fields)


def _opened(handle: Ledger, *, resumed: bool = False, model: str = "opus") -> None:
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
    handle: Ledger,
    turn: int,
    *,
    credits: float | None = 0.5,
    tokens: dict[str, int] | None = None,
    stop_reason: str = "end_turn",
    attempt: int | None = None,
    model: str = "opus",
) -> None:
    """A whole turn: started, one step, completed."""
    start: dict[str, object] = {"turn": turn, "actor": "user", "depth": 0}
    if attempt is not None:
        start["attempt"] = attempt
    handle.append("turn/started", start, src=GATEWAY)
    handle.append("step/started", {"turn": turn, "step": 1}, src=GATEWAY)
    handle.append("step/completed", {"turn": turn, "step": 1, "ms": 120}, src=GATEWAY)
    done: dict[str, object] = {
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
    handle.append("turn/completed", done, src=GATEWAY)


def _tool(handle: Ledger, turn: int, call_id: str, name: str, *, status: str = "completed") -> None:
    handle.append(
        "tool/called",
        {"turn": turn, "call_id": call_id, "name": name, "server": "core", "kind": "mcp"},
        src=GATEWAY,
    )
    handle.append(
        "tool/completed",
        {
            "turn": turn,
            "call_id": call_id,
            "name": name,
            "server": "core",
            "status": status,
            "elapsed_ms": 42,
        },
        src=GATEWAY,
    )


def _busy_log() -> Ledger:
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


def _entries(handle: Ledger) -> tuple:
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
    with pytest.raises(LedgerError) as excinfo:
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
        with pytest.raises(LedgerError) as excinfo:
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


def test_status_tracks_a_lost_remote_placement():
    handle = _log()
    _opened(handle)
    handle.append("remote/placed", {"provider": "fargate", "id": "task-1"}, src=GATEWAY)
    handle.append("remote/lost", {"reason": "evicted"}, src=GATEWAY)
    remote = crew_log.fold_status(_entries(handle))["remote"]
    assert remote == {"provider": "fargate", "id": "task-1", "lost_reason": "evicted"}


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
    for turn in range(1, wanted + 1):
        handle.append("turn/started", {"turn": turn, "actor": "user", "depth": 0}, src=GATEWAY)
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
    for index in range(crew_log.TOOL_NAME_LIMIT + extra):
        _tool(handle, 1, f"c{index}", f"tool_{index:04d}")
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
    with pytest.raises(LedgerError) as excinfo:
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
    assert tuple(crew_log._FOLDS) == crew_log.PROJECTION_NAMES


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
