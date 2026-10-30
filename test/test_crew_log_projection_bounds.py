"""Bounds on what a crew-log fold retains, and on what one pass holds.

Every map and label a fold keeps is bounded, the omitted counts stay exact or say
when they saturate, and a cold fold streams the log rather than holding it. Shared
logs, entries and fixture writers live in ``crew_log_projection_helpers.py``.
"""

from __future__ import annotations

from typing import Any

from crew_log_projection_helpers import (  # noqa: F401 -- fixtures are used by name
    GATEWAY,
    SESSION,
    _append_grouped,
    _entries,
    _isolated_home,
    _log,
    _opened,
    _tool,
    _tool_items,
    _turn_items,
)

from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store

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
# bounds on what a fold RETAINS, and on what one pass holds
# --------------------------------------------------------------------------- #


def test_a_cold_fold_streams_the_log_rather_than_holding_it(monkeypatch):
    """A long cold fold holds ONE entry at a time, and the value is unaffected.

    ``fold_session`` hands the projection kernel the log as a stream and the kernel
    folds each entry through every unit as it arrives, so what a cold fold of a long
    log holds is one entry rather than the file. That is only safe if consuming it
    piecemeal is invisible in the result, so this drives a log past two thousand
    entries, compares against the from-scratch fold, AND checks the interleaving --
    otherwise the test would still pass if the whole log were materialized first.

    The interleaving is the part worth pinning: at the moment of the Nth fold step,
    exactly N entries have come out of the reader. A pass that read the file into a
    list would have every entry out before the first step.
    """
    handle = _log()
    _opened(handle)
    # Two entries per tool, so this clears two thousand entries several hundred over.
    items: list[dict[str, Any]] = []
    for index in range(1024):
        items.extend(_tool_items(1, f"c{index}", "fs_read"))
    _append_grouped(handle, items)
    assert handle.last_seq > 2048

    # Folded before the instruments go in, so its own read is not counted.
    whole = crew_log.fold_tools(_entries(handle))

    yielded = [0]
    real_iter_from = CrewLog.iter_from

    def counted_iter_from(self, from_seq, **kwargs):
        for entry in real_iter_from(self, from_seq, **kwargs):
            yielded[0] += 1
            yield entry

    interleaving: list[tuple[int, int]] = []
    real_apply = crew_log._SessionFold.apply

    real_apply_owned = crew_log._SessionFold.apply_owned

    def counted_apply(self, state, entry):
        interleaving.append((yielded[0], len(interleaving) + 1))
        return real_apply(self, state, entry)

    # A cold fold steps through ``apply_owned`` (in place, one private state), so the
    # probe watches both doors; either way each step must follow its own read.
    def counted_apply_owned(self, state, entry):
        interleaving.append((yielded[0], len(interleaving) + 1))
        return real_apply_owned(self, state, entry)

    monkeypatch.setattr(CrewLog, "iter_from", counted_iter_from)
    monkeypatch.setattr(crew_log._SessionFold, "apply", counted_apply)
    monkeypatch.setattr(crew_log._SessionFold, "apply_owned", counted_apply_owned)

    streamed = crew_log.fold_session(SESSION, ("tools",)).projection("tools").value

    assert streamed == whole
    assert streamed["calls"] == 1024
    assert len(interleaving) > 2048, "the whole log was folded"
    assert [out for out, _ in interleaving] == [
        step for _, step in interleaving
    ], "the reader ran ahead of the fold, so entries were held rather than streamed"
    assert [out for out, _ in interleaving] == [
        step for _, step in interleaving
    ], "the reader ran ahead of the fold, so entries were held rather than streamed"


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
