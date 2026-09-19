"""Unit tests for the append-only session log emitter."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import inspect
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from kiro_crew import crew_log as lg
from kiro_crew import executors, session_map
from kiro_crew.crew_log import crew_log_path, crew_log_root, emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.crew_log.lease import LEASE_FILE
from kiro_crew.dashboard import server as server_module
from kiro_crew.platform_compat import file_lock

SESSION = "acp-sess-0001"

#: The real backoff schedule, captured before the fixture below replaces it. The
#: schedule is production policy and one test pins its arithmetic; every OTHER test
#: drives the retry budget with the wait removed, because waiting out real backoff
#: makes an assertion a race against machine load rather than a statement about
#: behaviour.
_REAL_RETRY_DELAY = emit._retry_delay


def _pending(job, what: str) -> emit._PendingJob:
    """Wrap *job* in the record the writer's buffer holds."""
    return emit._PendingJob(job=job, what=what)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one.

    The retry backoff is also flattened to zero here. What the retention tests
    assert is the ORDER and the OUTCOME of a bounded retry -- which entry lands,
    which is dropped, what the counter reads -- and none of that is a claim about
    how long the writer waits. Leaving the real schedule in would make each of them
    sit through up to 1.55s of sleeping on a single writer thread and then assert
    through a fixed timeout, which is a stopwatch race the moment the host is busy:
    the same three tests passed locally and on Windows while failing a loaded Linux
    shard. Zeroed, they wait only on the writer's own barrier.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    yield
    # Stop the writer BEFORE pytest removes tmp_path. The writer runs on its own
    # thread and creates the crew log root on demand, so one still in flight here can
    # recreate the directory tree pytest has just deleted -- leaving a stray home
    # behind and, worse, making a later test's failure depend on the previous test's
    # timing. `drain_for_shutdown` returns once nothing is buffered and no batch is
    # claimed, which is exactly the condition for "no thread will touch this path
    # again"; `reset_caches` then drops the handles that point into it.
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()


def test_the_retry_schedule_doubles_from_its_floor_to_its_ceiling():
    """The one place the real schedule is asserted, and it needs no clock.

    Every other retention test flattens this to zero, so the schedule itself would
    otherwise be unpinned -- and it is policy: a floor low enough that a blip costs
    a moment, a ceiling low enough that the whole budget still fits inside a
    bounded shutdown.
    """
    floor = emit._RETRY_BACKOFF_SECONDS
    assert _REAL_RETRY_DELAY(1) == floor
    assert _REAL_RETRY_DELAY(2) == floor * 2
    assert _REAL_RETRY_DELAY(3) == floor * 4
    assert _REAL_RETRY_DELAY(99) == emit._RETRY_BACKOFF_MAX_SECONDS
    # The whole budget, so a wedged writer cannot outlast a bounded drain.
    spent = sum(_REAL_RETRY_DELAY(n) for n in range(1, emit._MAX_WRITE_ATTEMPTS))
    assert spent < emit._SHUTDOWN_DRAIN_SECONDS


def _log_path(session_id: str = SESSION) -> Path:
    """Ask the storage library where it puts things; never pin its layout."""
    return crew_log_path("session", session_id)


def _store_root() -> Path:
    return crew_log_root("session")


def _entries(session_id: str = SESSION) -> list[dict]:
    """Every line, header first, exactly as the emitter left it."""
    path = _log_path(session_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _body(session_id: str = SESSION) -> list[dict]:
    """The entries after the header line."""
    return _entries(session_id)[1:]


def _open_session() -> None:
    emit.on_session_opened(
        SESSION,
        agent="kirocrew",
        slot="chat-7",
        model="claude-opus-5",
        cwd="/home/dev/project",
        owner="default",
    )


# --- creation and header ---------------------------------------------------


def test_first_open_creates_the_log_file():
    assert not _log_path().exists()
    _open_session()
    assert _log_path().is_file()


def test_the_header_is_line_one_and_carries_owner_agent_slot_cwd():
    _open_session()
    header = _entries()[0]
    assert header["type"] == "session"
    assert header["id"] == SESSION
    assert header["owner"] == "default"
    assert header["agent"] == "kirocrew"
    assert header["slot"] == "chat-7"
    assert header["cwd"] == "/home/dev/project"
    assert isinstance(header["createdAt"], int)


def test_opened_entry_echoes_the_header_and_adds_the_model():
    _open_session()
    opened = [e for e in _body() if e["type"] == "session/opened"]
    assert len(opened) == 1
    entry = opened[0]
    assert entry["src"] == "gateway"
    assert isinstance(entry["time"], int)
    data = entry["data"]
    assert data["agent"] == "kirocrew"
    assert data["slot"] == "chat-7"
    assert data["cwd"] == "/home/dev/project"
    assert data["owner"] == "default"
    assert data["resumed"] is False
    # The storage header has no model field, so the entry carries it.
    assert data["model"] == "claude-opus-5"


def test_an_agentless_call_site_still_produces_a_valid_header():
    emit.on_session_opened(SESSION, slot="chat-1")
    assert _entries()[0]["agent"] == "kirocrew"


# --- the model: what serves, and what was asked for ------------------------


def test_the_request_is_recorded_when_the_backend_confirmed_nothing():
    # The dispatched-worker shape: the gateway resolved a model (a crew pin) and
    # the backend confirmed none. Without the request the entry is
    # indistinguishable from a session deliberately left on the backend default.
    emit.on_session_opened(
        SESSION,
        agent="kirocrew-worker",
        slot="chat-42",
        model="",
        model_requested="claude-opus-5",
    )
    data = _opened_data()
    assert data["model"] == ""
    assert data["model_requested"] == "claude-opus-5"


def test_a_concrete_served_default_does_not_hide_the_request():
    # A refused pin does not have to leave `model` empty: the backend can report
    # a concrete default of its own instead of the auto sentinel. Conditioning
    # the request on an unknown `model` dropped exactly this case, and the entry
    # is append-only, so the requested/served pair was lost with no way back.
    emit.on_session_opened(
        SESSION,
        agent="kirocrew-worker",
        slot="chat-42",
        model="backend-default-7",
        model_requested="claude-opus-5",
    )
    data = _opened_data()
    assert data["model"] == "backend-default-7"
    assert data["model_requested"] == "claude-opus-5"


def test_a_request_served_under_another_spelling_keeps_both_ids():
    # The backend serves the spelling IT resolved, so an APPLIED request can come
    # back as a different string. The entry records both and claims nothing about
    # which happened -- a reader must not read the difference as a refusal.
    emit.on_session_opened(
        SESSION,
        agent="kirocrew",
        slot="chat-7",
        model="kiro::claude-opus-5-v1",
        model_requested="claude-opus-5",
    )
    data = _opened_data()
    assert data["model"] == "kiro::claude-opus-5-v1"
    assert data["model_requested"] == "claude-opus-5"


def test_asking_for_nothing_writes_no_request_field():
    # Every tier deferred to the backend, so there is no request to record and an
    # empty field would read as a resolved model of "".
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", model="")
    data = _opened_data()
    assert data["model"] == ""
    assert "model_requested" not in data


# --- lineage: who made this session ----------------------------------------


def _opened_data() -> dict:
    opened = [e for e in _body() if e["type"] == "session/opened"]
    assert len(opened) == 1
    return opened[0]["data"]


def test_a_created_session_records_its_creator_on_the_opened_entry():
    # Both halves of the edge, from the one side that holds both: the creator's
    # key and ACP session id captured on the slot when session_create minted the
    # child. The sid is absent when the creator had no live handle at mint.
    emit.on_session_opened(
        SESSION,
        agent="kirocrew-worker",
        slot="chat-42",
        parent_slot="chat-7",
        parent_sid="acp-sess-creator",
    )
    assert _opened_data()["parent"] == {"slot": "chat-7", "sid": "acp-sess-creator"}


def test_a_creator_whose_handle_is_gone_is_recorded_by_slot_alone():
    # An empty sid is not written as "": a reader would take an empty string for
    # a creator with an empty name, where an absent key says the handle was down.
    emit.on_session_opened(SESSION, agent="kirocrew-worker", slot="chat-42", parent_slot="chat-7")
    assert _opened_data()["parent"] == {"slot": "chat-7"}


def test_a_session_nobody_created_carries_no_parent_at_all():
    # A person's own tab and a fork have no creator. The key is absent rather
    # than null or empty, so a fold can tell "no creator" from "creator unknown".
    _open_session()
    assert "parent" not in _opened_data()


def test_a_sid_without_a_slot_names_no_creator():
    # The slot is the tree key; a sid alone is a citation with nowhere to hang.
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-42", parent_sid="acp-sess-creator")
    assert "parent" not in _opened_data()


def test_the_session_class_is_recorded_as_facts_on_the_opened_entry():
    # What kind of session this log belongs to, recorded here because the reader
    # that asks -- may another session read this log -- asks it about sessions that
    # have closed, and a closed session has no slot left to ask.
    emit.on_session_opened(
        SESSION,
        agent="kirocrew",
        slot="chat-42",
        memory="persistent",
        app="travel-desk",
        channel=True,
    )
    assert _opened_data()["class"] == {
        "memory": "persistent",
        "app": "travel-desk",
        "channel": True,
    }


def test_a_session_with_nothing_to_declare_still_records_its_class():
    # MUTATION-SENSITIVE: this is what makes "recorded, and nothing applies"
    # distinguishable from "not recorded". The object is never empty, because
    # ``memory`` is always known for a live slot, so its presence is the witness --
    # and a reader deciding an authorization question refuses on the absence.
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-42", memory="persistent")
    assert _opened_data()["class"] == {"memory": "persistent"}


def test_a_caller_that_names_no_memory_mode_records_no_class_at_all():
    # A partial record would read as complete, so the emitter writes none. The key
    # is absent rather than empty, which is the same distinction ``parent`` keeps.
    _open_session()
    assert "class" not in _opened_data()


def test_an_absent_app_or_channel_is_absent_rather_than_falsy():
    # An empty app would read as an app with an empty name, and ``channel: false``
    # would claim the gateway checked and found none -- which is true here, but it
    # is not true on a log written before the field existed, and a reader must not
    # have to tell those two apart by the shape of a falsy value.
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-42", memory="incognito")
    recorded = _opened_data()["class"]
    assert recorded == {"memory": "incognito"}
    assert "app" not in recorded and "channel" not in recorded


def test_the_creator_is_written_again_on_a_re_attach():
    # A resume re-announces the header facts, and the creator is one of them: the
    # slot's attribution outlives the ACP session, so a gateway taking the child
    # over records who made it in the entry that says it re-attached. The emitter
    # writes whatever the caller froze on the slot; it never looks the sid up.
    emit.on_session_opened(SESSION, agent="kirocrew-worker", slot="chat-42", parent_slot="chat-7")
    emit.reset_caches()
    emit.on_session_opened(
        SESSION,
        agent="kirocrew-worker",
        slot="chat-42",
        resumed=True,
        parent_slot="chat-7",
        parent_sid="acp-sess-creator",
    )
    opened = [e for e in _body() if e["type"] == "session/opened"]
    assert [e["data"]["resumed"] for e in opened] == [False, True]
    assert opened[-1]["data"]["parent"] == {"slot": "chat-7", "sid": "acp-sess-creator"}


def test_reopening_appends_instead_of_truncating():
    _open_session()
    first = len(_entries())
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", resumed=True)
    entries = _entries()
    assert len(entries) == first + 1
    assert entries[-1]["data"]["resumed"] is True
    assert sum(1 for e in entries if e["type"] == "session") == 1


def test_a_warm_reuse_of_the_same_session_adds_no_second_opened_entry():
    """The claim runs every turn; only a create or a re-attach is worth an entry."""
    _open_session()
    _open_session()
    _open_session()
    assert sum(1 for e in _body() if e["type"] == "session/opened") == 1


def test_a_warm_reuse_does_not_change_which_turn_a_later_entry_names():
    # A re-claim of the same session has no cached anchor to clear: with the turn
    # carried in data, the next entry names the turn its caller passed.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    _open_session()
    emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-1")
    last = _body()[-1]
    assert "thread" not in last
    assert last["data"]["turn"] == 2


def test_owner_is_written_once_and_a_reopen_cannot_change_it():
    emit.on_session_opened(SESSION, agent="kirocrew", owner="raymond")
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", owner="somebody-else")
    assert _entries()[0]["owner"] == "raymond"


# --- a whole turn ----------------------------------------------------------


def test_one_full_turn_produces_contiguous_seq():
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_tool_called(SESSION, 4, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 4, name="fs_read", status="completed", call_id="tc-1")
    emit.on_turn_completed(
        SESSION,
        4,
        input_tokens=1200,
        output_tokens=340,
        cache_read_tokens=9000,
        cache_write_tokens=15,
        credits=0.42,
        duration_ms=8123,
        stop_reason="end_turn",
        model="claude-opus-5",
        provider="kiro",
    )
    body = _body()
    assert [e["seq"] for e in body] == list(range(1, len(body) + 1))
    assert [e["type"] for e in body] == [
        "session/opened",
        "turn/started",
        "tool/called",
        "tool/completed",
        "turn/completed",
    ]


def test_every_entry_of_a_turn_names_that_turn_in_its_data():
    # The turn identity is CARRIED, not looked up: the runner's ordinal is in
    # data.turn on every entry the turn produced, so no in-process state has to
    # survive for an entry to say which turn it belongs to.
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_tool_called(SESSION, 4, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 4, name="fs_read", status="completed", call_id="tc-1")
    emit.on_turn_completed(SESSION, 4, stop_reason="end_turn")
    by_type = {e["type"]: e for e in _body()}
    for kind in ("turn/started", "tool/called", "tool/completed", "turn/completed"):
        assert by_type[kind]["data"]["turn"] == 4, kind


def test_no_session_entry_carries_a_thread():
    # `thread` points at another LINE's seq, which is only knowable for a unit
    # whose anchor is written before the entries citing it -- the crew log's
    # shape. A session entry names its turn in data instead.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 1, status="completed", call_id="tc-1")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_compaction_applied(SESSION, pct_before=0.9, pct_after=0.3)
    emit.on_session_closed(SESSION, "reset")
    for entry in _body():
        assert "thread" not in entry, entry["type"]


def test_entries_outside_a_turn_carry_no_thread():
    _open_session()
    emit.on_session_closed(SESSION, "reset")
    for entry in _body():
        assert "thread" not in entry


def test_two_turns_are_told_apart_by_their_own_ordinals():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-2")
    tool = [e for e in _body() if e["type"] == "tool/called"][0]
    assert tool["data"]["turn"] == 2
    starts = [e["data"]["turn"] for e in _body() if e["type"] == "turn/started"]
    assert starts == [1, 2]


def test_a_tool_call_carries_its_position_in_the_turn_and_the_completion_reuses_it():
    # A step orders the calls of one turn without a reader comparing seq, and
    # tells two calls of the same tool apart when their ids are opaque.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-2")
    emit.on_tool_completed(SESSION, 1, status="completed", call_id="tc-1")
    called = [e for e in _body() if e["type"] == "tool/called"]
    assert [e["data"]["call_index"] for e in called] == [1, 2]
    done = [e for e in _body() if e["type"] == "tool/completed"][0]
    assert done["data"]["call_index"] == 1


def test_call_index_numbering_restarts_with_each_turn():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-2")
    steps = {
        e["data"]["turn"]: e["data"]["call_index"] for e in _body() if e["type"] == "tool/called"
    }
    assert steps == {1: 1, 2: 1}


def test_turn_completed_carries_the_four_token_counts_and_cost():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(
        SESSION,
        1,
        input_tokens=11,
        output_tokens=22,
        cache_read_tokens=33,
        cache_write_tokens=44,
        credits=1.5,
        duration_ms=99,
        stop_reason="end_turn",
    )
    data = _body()[-1]["data"]
    assert data["tokens"] == {
        "input": 11,
        "output": 22,
        "cache_read": 33,
        "cache_write": 44,
    }
    assert data["credits"] == 1.5
    assert data["duration_ms"] == 99
    assert data["stop_reason"] == "end_turn"


def test_an_aborted_turn_leaves_a_started_with_no_completion():
    _open_session()
    emit.on_turn_started(SESSION, 2, "cron")
    types = [e["type"] for e in _body()]
    assert types.count("turn/started") == 1
    assert "turn/completed" not in types


def test_a_recovery_reentry_is_its_own_turn_pair_marked_by_depth():
    _open_session()
    emit.on_turn_started(SESSION, 5, "user", depth=0)
    emit.on_turn_completed(SESSION, 5, stop_reason="cancelled", depth=0)
    emit.on_turn_started(SESSION, 5, "user", depth=1)
    emit.on_turn_completed(SESSION, 5, stop_reason="end_turn", depth=1)
    turns = [e for e in _body() if e["type"].startswith("turn/")]
    assert [e["data"]["depth"] for e in turns] == [0, 0, 1, 1]


def test_every_actor_the_dispatcher_distinguishes_is_recorded_verbatim():
    _open_session()
    for actor in ("user", "app", "cron", "autonudge", "subagent", "crew"):
        emit.on_turn_started(SESSION, 1, actor)
    recorded = [e["data"]["actor"] for e in _body() if e["type"] == "turn/started"]
    assert recorded == ["user", "app", "cron", "autonudge", "subagent", "crew"]


def test_an_unknown_actor_is_recorded_as_other_not_guessed():
    _open_session()
    emit.on_turn_started(SESSION, 1, "something-new")
    assert _body()[-1]["data"]["actor"] == "other"


def test_an_app_authored_send_is_not_dispatched_as_a_person():
    """The send API names the app it already identified.

    The actor resolver's fallback is ``user``, so a dispatch site that observes
    a non-person producer and passes no actor does not leave the field absent --
    it states that a person typed the message. ``_api_chat`` holds that fact in
    ``request_app`` (stamped by the app-token auth middleware, which a person
    cannot write), so the branch that reads it must also hand it on.
    """
    import ast
    import inspect

    from kiro_crew.dashboard import chat_handlers

    tree = ast.parse(inspect.getsource(chat_handlers))
    kwargs_writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "_turn_kwargs"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "_turn_actor"
    ]
    assert kwargs_writes, (
        "an app-authored send reaches the turn with no actor, so the crew log "
        "records a person who never typed anything"
    )


# --- tools, approvals, model, compaction ----------------------------------


def test_a_tool_call_is_identified_by_id_in_data_and_records_no_arguments():
    _open_session()
    emit.on_turn_started(SESSION, 3, "user")
    emit.on_tool_called(SESSION, 3, name="execute_bash", server="", kind="execute", call_id="tc-9")
    entry = _body()[-1]
    # ref is a citation of another crew log's lines, so a tool call id is data.
    assert "ref" not in entry
    assert entry["data"] == {
        "turn": 3,
        "call_id": "tc-9",
        "name": "execute_bash",
        "server": "",
        "kind": "execute",
        "call_index": 1,
    }


# --- message bodies -------------------------------------------------------


def test_an_observed_class_is_recorded_when_a_link_commits():
    """MUTATION-SENSITIVE: the record is taken when the class BECOMES true.

    The per-turn observation cannot see a link that commits and is removed inside one
    turn, and content authored through it is in the log with nothing saying it was
    published. So the surfaces that commit a link record the class themselves, and this
    is the entry point they call.
    """
    _open_session()
    emit.on_class_observed(SESSION, memory="persistent", channel=True)
    assert emit.flush()
    entry = _body()[-1]
    assert entry["type"] == "session/class"
    assert entry["data"]["channel"] is True
    assert entry["data"]["memory"] == "persistent"


def test_a_class_that_has_not_moved_records_nothing():
    """MUTATION-SENSITIVE: the recorder is free to call from every link site.

    Every assignment site calls it, several of which cannot have changed anything --
    a cron link, a slot being created. Writing a line for each would fill a log with
    restatements, so the latch is what makes calling it everywhere the cheap option.
    Without this the previous test passes while the log grows a line per bind.
    """
    _open_session()
    emit.on_class_observed(SESSION, memory="persistent", channel=True)
    assert emit.flush()
    before = len(_body())
    emit.on_class_observed(SESSION, memory="persistent", channel=True)
    assert emit.flush()
    assert len(_body()) == before, "a restatement of the same class is not a move"


def test_an_observed_class_with_no_memory_mode_records_nothing():
    """A fragment is refused here rather than appended and refused by every reader.

    The opening entry applies the same rule: a log that states no class refuses every
    test built on one, and appending a transition to it would leave a history with a
    middle and no beginning.
    """
    _open_session()
    emit.on_class_observed(SESSION, memory="", channel=True)
    assert emit.flush()
    assert [e["type"] for e in _body()].count("session/class") == 0


def _typed(turn: int = 1, text: str = "hello", **kw) -> None:
    emit.on_message_received(SESSION, turn, text=text, **kw)


def test_a_received_message_records_its_body_role_and_surface():
    _open_session()
    _typed(1, "what broke?", source="dashboard", role="user")
    assert emit.flush()
    entry = _body()[-1]
    assert entry["type"] == "message/received"
    assert entry["data"]["text"] == "what broke?"
    assert entry["data"]["role"] == "user"
    assert entry["data"]["source"] == "dashboard"
    assert entry["data"]["turn"] == 1


def test_a_received_message_is_recorded_even_when_the_turn_is_then_refused():
    # The body is written before the dispatch gates, so the turns a reader most
    # wants explained -- the refused ones -- are not the ones missing their text.
    _open_session()
    _typed(1, "run it")
    emit.on_turn_refused(SESSION, 1, "not_authorized")
    assert emit.flush()
    kinds = [e["type"] for e in _body()]
    assert "message/received" in kinds
    assert "turn/refused" in kinds
    assert "turn/started" not in kinds


@pytest.mark.parametrize("reason", ["blocked", "too_large"])
def test_a_refused_prompt_expansion_records_the_input_and_the_refusal(reason):
    # A blocked or oversized @prompt expansion returns before the normal emit
    # block, so without the reorder it left the accepted turn with NO entry. It
    # now records the same pair every dispatch gate does: the accepted input, then
    # a turn/refused naming the reason -- and nothing derived from a request the
    # refused turn never assembled.
    _open_session()
    emit.on_message_received(SESSION, 1, role="user", text="@secret run it", source="dashboard")
    emit.on_turn_refused(SESSION, 1, reason, "user", depth=0)
    assert emit.flush()
    received = [e for e in _body() if e["type"] == "message/received"]
    refused = [e for e in _body() if e["type"] == "turn/refused"]
    assert len(received) == 1
    assert received[0]["data"]["text"] == "@secret run it"
    assert len(refused) == 1
    assert refused[0]["data"]["reason"] == reason
    kinds = [e["type"] for e in _body()]
    assert "turn/started" not in kinds
    assert "request/configured" not in kinds
    assert "context/composed" not in kinds


def test_an_accepted_expansion_records_the_input_exactly_once():
    # The accepted path is unchanged: one message/received, no turn/refused. The
    # refusal reorder must not double-write on the path the gate did not take.
    _open_session()
    emit.on_message_received(SESSION, 1, role="user", text="hello", source="dashboard")
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    received = [e for e in _body() if e["type"] == "message/received"]
    assert len(received) == 1
    assert [e["type"] for e in _body() if e["type"] == "turn/refused"] == []


def test_chat_runner_records_the_input_and_refusal_on_a_blocked_expansion():
    # The reorder lives in _run_chat: the blocked/too_large @prompt branch must
    # emit on_message_received then on_turn_refused, mirroring the not_authorized
    # gate. Asserted structurally rather than by driving the whole runner.
    import ast

    from kiro_crew.dashboard import chat_runner

    tree = ast.parse(inspect.getsource(chat_runner))
    src = inspect.getsource(chat_runner)
    assert "🔒 Prompt blocked" in src

    def _emit_attr(node, name):
        # A crew_log_emit.<name>(...) call, however the module is aliased.
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        )

    def _string_in(node, needle):
        return any(
            isinstance(c, ast.Constant) and isinstance(c.value, str) and needle in c.value
            for c in ast.walk(node)
        )

    # Find every statement block (an if/elif body, orelse, loop or with body) that
    # appends the "Prompt blocked" notice, and confirm the refusal pair lives IN
    # THAT SAME block -- not merely somewhere in the 9000-line function. A refactor
    # that moved the refusal elsewhere, or dropped the paired message/received,
    # must fail this.
    blocks = []
    for node in ast.walk(tree):
        for body in (getattr(node, "body", None), getattr(node, "orelse", None)):
            if not isinstance(body, list):
                continue
            for stmt in body:
                if (
                    isinstance(stmt, ast.Expr)
                    and _string_in(stmt.value, "🔒 Prompt blocked")
                    and body not in blocks
                ):
                    blocks.append(body)

    assert blocks, "no block appends the '🔒 Prompt blocked' notice"

    def _co_located(block):
        received_at = None
        refused_at = None
        for i, stmt in enumerate(block):
            if not isinstance(stmt, ast.Expr):
                continue
            call = stmt.value
            if _emit_attr(call, "on_message_received"):
                received_at = i
            elif _emit_attr(call, "on_turn_refused") and any(
                ast.unparse(a) == "_status" for a in call.args
            ):
                refused_at = i
        return received_at is not None and refused_at is not None and received_at < refused_at

    assert any(_co_located(block) for block in blocks), (
        "the blocked/too_large branch must emit on_message_received then "
        "on_turn_refused(_status, ...) in the SAME block as the 'Prompt blocked' "
        "notice"
    )


def test_a_step_less_context_composed_is_written_for_the_first_call(tmp_path):
    # on_context_composed is emitted before the first step/started, so it carries
    # no step. The join is a reader rule -- a step-less composition belongs
    # to the turn's first model call -- and the entry is still written and still
    # ordered after the message it was derived from.
    _open_session()
    emit.on_message_received(SESSION, 1, role="user", text="do it", source="dashboard")
    emit.on_context_composed(SESSION, 1, blocks={"system": 400})
    emit.on_step_started(SESSION, 1)
    assert emit.flush()
    body = _body()
    kinds = [e["type"] for e in body]
    composed = [e for e in body if e["type"] == "context/composed"]
    assert len(composed) == 1
    assert "step" not in composed[0]["data"], "the turn-opening composition is step-less"
    # Ordering invariant: the composition is written after the message it derives
    # from and before the first model call opens.
    assert kinds.index("message/received") < kinds.index("context/composed")
    assert kinds.index("context/composed") < kinds.index("step/started")


def test_attachments_are_identifiers_not_refs():
    # An attachment is not a crew log unit, so it cannot be cited by a Ref -- the
    # same reason a tool call id lives in data.
    _open_session()
    _typed(1, "see this", attachments=["img-1", "img-2"])
    assert emit.flush()
    entry = _body()[-1]
    assert entry["data"]["attachments"] == ["img-1", "img-2"]
    assert "attachments_omitted" not in entry["data"]
    assert "ref" not in entry


def test_no_attachment_key_when_there_are_none():
    _open_session()
    _typed(1, "plain")
    assert emit.flush()
    assert "attachments" not in _body()[-1]["data"]


def test_a_sent_message_records_the_text_and_the_model_call_it_came_from():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_message_sent(SESSION, 1, step=2, text="the log was rotated")
    assert emit.flush()
    entry = _body()[-1]
    assert entry["type"] == "message/sent"
    assert entry["data"]["text"] == "the log was rotated"
    assert entry["data"]["step"] == 2


def test_an_interrupted_reply_says_so_and_a_normal_one_stays_silent():
    _open_session()
    emit.on_message_sent(SESSION, 1, text="half a thou")
    emit.on_message_sent(SESSION, 1, text="all of it", interrupted=True)
    assert emit.flush()
    sent = [e for e in _body() if e["type"] == "message/sent"]
    assert "interrupted" not in sent[0]["data"]
    assert sent[1]["data"]["interrupted"] is True


def test_a_body_too_big_for_one_line_is_split_and_cited_in_order():
    _open_session()
    # Sized from the format's ceiling, not from the slice: the slice decides how
    # big each piece is, the ceiling decides whether splitting happens at all.
    huge = "x" * (lg.MAX_ENTRY_BYTES + 1000)
    expected = math.ceil(len(huge) / emit._CHUNK_TEXT_CHARS)
    emit.on_message_sent(SESSION, 4, step=1, text=huge)
    assert emit.flush()
    body = _body()
    chunks = [e for e in body if e["type"] == "message/chunk"]
    sent = [e for e in body if e["type"] == "message/sent"][-1]
    assert len(chunks) == expected
    # The citation is in order, and reassembling it returns the original text.
    assert sent["data"]["chunks"] == [c["seq"] for c in chunks]
    assert "".join(c["data"]["delta"] for c in chunks) == huge
    assert sent["data"]["chars"] == len(huge)
    assert "text" not in sent["data"]


def test_a_body_that_fits_is_not_split_at_all():
    # The slice size is not the trigger. A body several times the slice still
    # rides on one line when the line can hold it.
    _open_session()
    emit.on_message_sent(SESSION, 1, text="x" * (emit._CHUNK_TEXT_CHARS * 2))
    assert emit.flush()
    assert not [e for e in _body() if e["type"] == "message/chunk"]
    assert _body()[-1]["data"]["text"]


def test_every_split_line_is_inside_the_format_ceiling():
    _open_session()
    emit.on_message_sent(SESSION, 1, text="\u4e2d" * (emit._CHUNK_TEXT_CHARS * 2))
    assert emit.flush()
    # Non-ASCII is the case the slice size exists for: ensure_ascii turns one
    # character into six bytes, so a slice measured in characters alone would be
    # refused at append time and the body would vanish.
    for line in _log_path().read_text(encoding="utf-8").splitlines():
        assert len(line.encode("utf-8")) <= lg.MAX_ENTRY_BYTES


def test_an_astral_body_still_splits_inside_the_ceiling():
    # A character outside the BMP escapes as a SURROGATE PAIR -- twelve bytes, not
    # six -- so a slice sized for the six-byte case is refused, and a refused chunk
    # kills the whole split and loses the body rather than just cutting it badly.
    _open_session()
    body = "\U0001f600" * (emit._CHUNK_TEXT_CHARS + 500)
    emit.on_message_sent(SESSION, 1, text=body)
    assert emit.flush()
    chunks = [e for e in _body() if e["type"] == "message/chunk"]
    sent = [e for e in _body() if e["type"] == "message/sent"]
    assert chunks, "the body was not split"
    assert sent, "the body was lost: no message/sent followed the chunks"
    assert "".join(c["data"]["delta"] for c in chunks) == body
    assert sent[-1]["data"]["chunks"] == [c["seq"] for c in chunks]
    for line in _log_path().read_text(encoding="utf-8").splitlines():
        assert len(line.encode("utf-8")) <= lg.MAX_ENTRY_BYTES


def test_a_lone_surrogate_body_never_raises_into_the_caller():
    # A JSON payload can carry one. Fail-soft means it is dropped or escaped, not
    # raised at the call site.
    _open_session()
    emit.on_message_received(SESSION, 1, text="before \ud800 after")
    emit.on_message_sent(SESSION, 1, text="reply \ud800 here")
    assert emit.flush()


def test_an_oversize_received_body_is_split_like_a_sent_one():
    # Both bodies go through one seam, so a pasted message too big for a line gets
    # the same split. Written straight onto the entry it would be refused at append
    # time and the message would be lost whole.
    _open_session()
    body = "p" * (lg.MAX_ENTRY_BYTES + 1000)
    emit.on_message_received(SESSION, 1, text=body, source="dashboard")
    assert emit.flush()
    chunks = [e for e in _body() if e["type"] == "message/chunk"]
    got = [e for e in _body() if e["type"] == "message/received"]
    assert chunks, "an oversize received body was not split"
    assert got, "the received entry was lost"
    assert got[-1]["data"]["chunks"] == [c["seq"] for c in chunks]
    assert "".join(c["data"]["delta"] for c in chunks) == body
    # The non-body fields still ride on the citing entry.
    assert got[-1]["data"]["role"] == "user"
    assert got[-1]["data"]["source"] == "dashboard"
    for line in _log_path().read_text(encoding="utf-8").splitlines():
        assert len(line.encode("utf-8")) <= lg.MAX_ENTRY_BYTES


def test_oversize_attachment_metadata_keeps_the_received_message():
    _open_session()
    attachments = [f"file-{index}-" + "\U0001f600" * 1024 for index in range(12)]
    cases = (
        ("small body", False),
        ("p" * (lg.MAX_ENTRY_BYTES + 1000), True),
    )

    for turn, (body, split) in enumerate(cases, start=1):
        emit.on_message_received(
            SESSION,
            turn,
            text=body,
            source="dashboard",
            attachments=attachments,
        )
    assert emit.flush()

    received = [entry for entry in _body() if entry["type"] == "message/received"]
    assert len(received) == len(
        cases
    ), "an accepted message was lost when attachment metadata exceeded the line ceiling"
    chunks = {entry["seq"]: entry for entry in _body() if entry["type"] == "message/chunk"}
    for entry, (body, split) in zip(received, cases, strict=True):
        data = entry["data"]
        kept = data.get("attachments", [])
        assert kept == attachments[: len(kept)]
        assert data["attachments_omitted"] == len(attachments) - len(kept)
        if split:
            assert "text" not in data
            assert "".join(chunks[seq]["data"]["delta"] for seq in data["chunks"]) == body
        else:
            assert data["text"] == body
            assert "chunks" not in data
    assert emit.dropped_writes() == 0
    for line in _log_path().read_text(encoding="utf-8").splitlines():
        assert len(line.encode("utf-8")) <= lg.MAX_ENTRY_BYTES


def test_every_body_family_produces_one_body_representation():
    # FR-7 is amended in this change to include bodies, so the redacted body is
    # what goes in the log. The invariant is the SHAPE every body-bearing entry
    # has, asserted on the entries rather than on the source that produces them:
    # exactly one of `text` or `chunks`, never both and never neither. That holds
    # through any refactor of how the seam is written, and it is what a reader
    # actually depends on.
    assert emit.BODY_MODE == emit.BODY_MODE_TEXT
    _open_session()
    small, large = "a short body", "L" * (lg.MAX_ENTRY_BYTES + 400)
    emit.on_turn_started(SESSION, 1, "user")
    _typed(1, small)
    emit.on_message_sent(SESSION, 1, step=1, text=small)
    _typed(2, large)
    emit.on_message_sent(SESSION, 2, step=1, text=large)
    assert emit.flush()

    families = {"message/received", "message/sent"}
    seen: set[str] = set()
    for entry in _body():
        if entry["type"] not in families:
            continue
        seen.add(entry["type"])
        data = entry["data"]
        has_text, has_chunks = "text" in data, "chunks" in data
        assert has_text != has_chunks, (
            f"{entry['type']} carries {'both' if has_text else 'neither'} "
            f"text and chunks: {sorted(data)}"
        )
        if has_chunks:
            # A citation must name real entries and carry the original length.
            assert data["chunks"], "an empty chunk citation"
            assert data.get("chars", 0) > 0
    assert seen == families, f"a body family produced no entry: {sorted(families - seen)}"


def test_overflow_chunks_carry_a_body_that_would_otherwise_be_lost():
    # The only producer of `message/chunk`. Written unconditionally, because the
    # alternative is an append refused for size and a body missing from the log.
    _open_session()
    emit.on_message_sent(SESSION, 1, text="y" * (lg.MAX_ENTRY_BYTES + 1000))
    assert emit.flush()
    assert [e["type"] for e in _body() if e["type"] == "message/chunk"]


def test_a_split_chunk_is_marked_ignorable():
    # A reader that does not know the type may skip them and still read the
    # message/sent that cites them.
    _open_session()
    emit.on_message_sent(SESSION, 1, text="z" * (lg.MAX_ENTRY_BYTES + 1000))
    assert emit.flush()
    marked = [e for e in _body() if e["type"] == "message/chunk"]
    assert marked
    for entry in marked:
        assert entry["ignorable"] is True


# --- streamed deltas ------------------------------------------------------


def test_the_request_configuration_is_written_once_and_then_only_on_change():
    _open_session()
    for turn in (1, 2, 3):
        emit.on_request_configured(
            SESSION, turn, model="claude-opus-5", provider="acp", context_window=200000
        )
    emit.on_request_configured(
        SESSION, 4, model="claude-sonnet-5", provider="acp", context_window=200000
    )
    assert emit.flush()
    written = [e for e in _body() if e["type"] == "request/configured"]
    # Three identical turns produce ONE entry; the swap produces the second.
    assert [e["data"]["turn"] for e in written] == [1, 4]
    assert [e["data"]["model"] for e in written] == ["claude-opus-5", "claude-sonnet-5"]


def test_the_system_prompt_is_recorded_as_a_digest_never_as_text():
    _open_session()
    emit.on_request_configured(SESSION, 1, model="m", provider="acp", system="SECRET PROMPT")
    assert emit.flush()
    data = _body()[-1]["data"]
    assert "SECRET PROMPT" not in json.dumps(data)
    assert len(data["system"]) == 64
    assert data["system_bytes"] == len("SECRET PROMPT")


def test_a_changed_system_prompt_alone_is_a_change():
    _open_session()
    emit.on_request_configured(SESSION, 1, model="m", provider="acp", system="one")
    emit.on_request_configured(SESSION, 2, model="m", provider="acp", system="two")
    assert emit.flush()
    assert len([e for e in _body() if e["type"] == "request/configured"]) == 2


def test_the_configuration_carries_no_tool_list_at_all():
    # The gateway never receives the resolved tool list -- the backend serves
    # specs with tool search on. An empty list every turn would read as "no
    # tools", which is false, so the field is absent instead.
    _open_session()
    emit.on_request_configured(SESSION, 1, model="m", provider="acp", context_window=1)
    assert emit.flush()
    assert "tools" not in _body()[-1]["data"]


def test_a_reopened_session_writes_its_configuration_again():
    _open_session()
    emit.on_request_configured(SESSION, 1, model="m", provider="acp")
    emit.on_session_closed(SESSION, "reset")
    assert emit.flush()
    _open_session()
    emit.on_request_configured(SESSION, 1, model="m", provider="acp")
    assert emit.flush()
    assert len([e for e in _body() if e["type"] == "request/configured"]) == 2


def test_a_failed_configuration_append_is_retried_not_suppressed(monkeypatch):
    # Remembering the fingerprint before the append landed would turn one
    # transient failure into a permanent gap: every later identical configuration
    # would be suppressed as unchanged, and the session would carry no record of
    # what it ran as.
    _open_session()
    assert emit.flush()
    original = lg.CrewLog.append
    failures = {"left": 1}

    def _fail_once(self, entry_type, *args, **kwargs):
        if entry_type == "request/configured" and failures["left"]:
            failures["left"] -= 1
            raise lg.CrewLogError("disk said no", code=lg.CODE_BAD_DATA)
        return original(self, entry_type, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _fail_once)
    emit.on_request_configured(SESSION, 1, model="m", provider="acp", context_window=7)
    assert emit.flush()
    assert not [e for e in _body() if e["type"] == "request/configured"]
    # Same configuration, next turn: it must be written, not treated as unchanged.
    emit.on_request_configured(SESSION, 2, model="m", provider="acp", context_window=7)
    assert emit.flush()
    written = [e for e in _body() if e["type"] == "request/configured"]
    assert len(written) == 1
    assert written[0]["data"]["turn"] == 2


# --- composed context -----------------------------------------------------


def test_composed_context_itemises_the_blocks_with_estimated_tokens():
    _open_session()
    emit.on_context_composed(
        SESSION, 1, step=1, blocks={"lessons": 400, "memory": 800}, total_chars=1200
    )
    assert emit.flush()
    data = _body()[-1]["data"]
    # Newest-biggest first, characters exact, tokens derived at 4 chars each.
    assert data["sources"] == [
        {"kind": "memory", "chars": 800, "tokens": 200},
        {"kind": "lessons", "chars": 400, "tokens": 100},
    ]
    assert data["chars"] == 1200
    assert data["tokens"] == 300


def test_the_token_count_says_that_it_is_an_estimate():
    # The only tokenizer available is the wrong one for the served model, so the
    # entry admits the number is derived rather than measured.
    _open_session()
    emit.on_context_composed(SESSION, 1, blocks={"memory": 40})
    assert emit.flush()
    assert _body()[-1]["data"]["tokens_estimated"] is True


def test_unclassified_characters_are_reported_as_one_other_source():
    # Steering, tool specs and injected ledger context have no marker, so their
    # characters are genuinely a remainder. Three zeroed sources would claim a
    # measurement nobody took.
    _open_session()
    emit.on_context_composed(SESSION, 1, blocks={"unclassified": 100, "lessons": 40})
    assert emit.flush()
    kinds = [s["kind"] for s in _body()[-1]["data"]["sources"]]
    assert kinds == ["other", "lessons"]


def test_empty_and_negative_blocks_are_dropped_rather_than_recorded():
    _open_session()
    emit.on_context_composed(SESSION, 1, blocks={"memory": 0, "lessons": -5, "skill_index": 8})
    assert emit.flush()
    assert [s["kind"] for s in _body()[-1]["data"]["sources"]] == ["skill_index"]


def test_no_blocks_writes_nothing():
    _open_session()
    before = len(_body())
    emit.on_context_composed(SESSION, 1, blocks={})
    emit.on_context_composed(SESSION, 1, blocks=None)
    assert emit.flush()
    assert len(_body()) == before


# --- model-call steps -----------------------------------------------------


def test_a_step_is_one_model_call_and_numbering_restarts_each_turn():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.on_step_started(SESSION, 1) == 1
    assert emit.on_step_started(SESSION, 1) == 2
    emit.on_turn_completed(SESSION, 1)
    emit.on_turn_started(SESSION, 2, "user")
    assert emit.on_step_started(SESSION, 2) == 1


def test_a_step_records_how_long_the_model_call_took():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    step = emit.on_step_started(SESSION, 1)
    emit.on_step_completed(SESSION, 1, step, ms=1234)
    assert emit.flush()
    done = [e for e in _body() if e["type"] == "step/completed"][-1]
    assert done["data"] == {"turn": 1, "step": step, "ms": 1234}


def test_closing_a_step_that_never_opened_writes_nothing():
    _open_session()
    before = len(_body())
    emit.on_step_completed(SESSION, 1, 0, ms=5)
    assert emit.flush()
    assert len(_body()) == before


def test_a_tool_call_names_the_model_call_that_issued_it():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="b", call_id="c2")
    emit.on_tool_called(SESSION, 1, name="c", call_id="c3")
    assert emit.flush()
    calls = [e["data"] for e in _body() if e["type"] == "tool/called"]
    # step says WHICH model call; call_index orders the calls within the turn.
    assert [c["step"] for c in calls] == [1, 2, 2]
    assert [c["call_index"] for c in calls] == [1, 2, 3]


def test_two_tools_in_one_model_call_share_a_step_but_not_a_position():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    emit.on_tool_called(SESSION, 1, name="b", call_id="c2")
    assert emit.flush()
    calls = [e["data"] for e in _body() if e["type"] == "tool/called"]
    assert {c["step"] for c in calls} == {1}
    assert [c["call_index"] for c in calls] == [1, 2]


def test_a_tool_call_with_no_announced_step_omits_it_rather_than_guessing():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    assert emit.flush()
    data = [e["data"] for e in _body() if e["type"] == "tool/called"][-1]
    assert "step" not in data
    assert data["call_index"] == 1


def test_a_completion_reuses_both_of_its_calls_ordinals():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed")
    assert emit.flush()
    done = [e["data"] for e in _body() if e["type"] == "tool/completed"][-1]
    assert done["step"] == 1
    assert done["call_index"] == 1


# --- a tool call settles exactly once --------------------------------------


def test_a_second_terminal_frame_for_the_same_call_writes_no_second_closer():
    # Both update parsers can emit a status-only terminal frame for one tool, so
    # two frames for the same call_id is ordinary. The first settles the call; the
    # second must add nothing, or one call ends up with two tool/completed entries.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="c1")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed")
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "tool/completed"]
    assert len(closers) == 1
    # The one that landed is the FIRST frame: it kept the remembered identity.
    assert closers[0]["data"]["name"] == "fs_read"


def test_a_terminal_frame_for_a_never_opened_call_still_gets_its_closer():
    # The absence of a tool/called is NOT the same as "already settled by us": a
    # result for a call whose opener was never recorded is a real close and must
    # be written, with empty name/server and no elapsed.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_completed(SESSION, 1, call_id="never-opened", status="completed")
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "tool/completed"]
    assert len(closers) == 1
    data = closers[0]["data"]
    assert data["call_id"] == "never-opened"
    assert data["name"] == ""
    assert data["server"] == ""
    assert "elapsed_ms" not in data


def test_a_frame_after_the_group_sweep_closed_the_call_writes_nothing():
    # close_open_tool_calls IS the closer at a tool-group boundary, so it marks the
    # calls it sweeps settled. A terminal frame arriving after the sweep is a
    # duplicate and must not double-write.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="c1")
    assert emit.close_open_tool_calls(SESSION, 1, status="completed") == 1
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed")
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "tool/completed"]
    assert len(closers) == 1
    # The sweep's closer, not the late frame's.
    assert closers[0]["data"]["result_bytes"] == 0


def test_settle_once_holds_for_a_terminal_frame_from_either_parser():
    # The guard is in on_tool_completed, the ONE consumer both parsers feed, so a
    # frame produced by _dispatch and a frame produced by the AcpClient parser
    # settle the same call identically: the first writes the closer, the second
    # (whichever parser it came from) writes nothing.
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.types import EVENT_TOOL_RESULT, JsonRpcMessage

    # Parser A: the shared _dispatch terminal-frame builder.
    parser_a = _tool_result_event(None)
    assert parser_a.kind == EVENT_TOOL_RESULT

    # Parser B: the AcpClient tool_call_update parser, same terminal frame shape.
    client = AcpClient()
    msg = JsonRpcMessage(
        params={
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-measured",
                "status": "completed",
            }
        }
    )
    parser_b = client._extract_tool_call_update(msg)
    assert parser_b is not None and parser_b.kind == EVENT_TOOL_RESULT

    def _record(event) -> None:
        emit.on_tool_completed(
            SESSION,
            1,
            call_id=event.tool_call_id,
            status=event.tool_status,
            result=event.tool_output,
            result_digest=event.tool_output_digest,
            result_bytes=event.tool_output_bytes,
        )

    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    _record(parser_a)
    _record(parser_b)
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "tool/completed"]
    assert len(closers) == 1, "the two parsers' frames wrote two closers for one call"


def test_a_settled_call_can_be_reopened_and_reclosed_in_a_later_turn():
    # Settled markers are pruned when a turn's live record is released, so the same
    # call_id reused in a later turn settles again rather than being dropped as a
    # stale duplicate.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_completed(SESSION, 1, call_id="reused", status="completed")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit._forget_turn(SESSION, 1)
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_tool_completed(SESSION, 2, call_id="reused", status="completed")
    assert emit.flush()
    closers = [e["data"] for e in _body() if e["type"] == "tool/completed"]
    assert [c["turn"] for c in closers] == [1, 2]


def test_a_closed_sessions_settled_markers_are_released():
    # on_session_closed sweeps the session's orphaned _tool_started entries; its
    # settled markers ride the SAME lifecycle, so a marker whose turn is already
    # gone from _live must be swept too and not left to the bound alone. A marker
    # of a turn still running is left for that turn's own _release_live.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit._forget_turn(SESSION, 1)
    # The turn is gone from _live but its marker was already pruned by the turn
    # release, so seed a fresh orphan directly to prove the close sweep also fires.
    emit._settled_tools[(SESSION, "orphan")] = 1
    assert (SESSION, "orphan") in emit._settled_tools
    emit.on_session_closed(SESSION, "reset")
    assert emit.flush()
    assert (SESSION, "orphan") not in emit._settled_tools


def test_the_settled_markers_stay_under_their_cap_inside_one_live_turn():
    # A marker accumulates per call COMPLETED, where an open-call record is popped by
    # its own completion, so the module's shared never-evict-a-live-turn rule would
    # let this map grow for the whole of a turn that makes more calls than the cap.
    # This map trims its OLDEST instead: the duplicate frames a marker suppresses
    # come from two parsers reading one terminal frame, so the youngest markers are
    # the ones doing the work.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert SESSION in emit._pinned, "the turn must be pinned for this to mean anything"
    total = emit._MAX_PENDING_TOOLS + 20
    for n in range(total):
        emit.on_tool_completed(SESSION, 1, call_id=f"c{n}", status="completed")
    assert emit.flush()
    assert len(emit._settled_tools) <= emit._MAX_PENDING_TOOLS
    # The oldest went and the youngest stayed.
    assert (SESSION, "c0") not in emit._settled_tools
    assert (SESSION, f"c{total - 1}") in emit._settled_tools
    # Every call still got exactly one closer: trimming a marker drops the guard for
    # a possible LATER duplicate, never an entry that was already written.
    closers = [e for e in _body() if e["type"] == "tool/completed"]
    assert len(closers) == total


# --- a step may cover consecutive tool-only calls ---------------------------


def test_the_step_boundary_is_derived_and_may_cover_consecutive_tool_only_calls():
    # The ACP stream exposes only the tool-group-to-text transition, so a turn that
    # calls tools, is called again with their results and calls MORE tools -- text
    # only at the end -- shows one transition and the two model calls collapse into
    # one step. The format documents this as a lower bound rather than pretending
    # the step count is a model-call count; every tool still orders by call_index.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    step = emit.on_step_started(SESSION, 1)
    assert step == 1
    emit.on_tool_called(SESSION, 1, name="read", call_id="c1")
    emit.on_tool_called(SESSION, 1, name="edit", call_id="c2")
    # A second tool-only model call with no intervening text produces no new
    # step/started: the runner only opens one at a text transition.
    emit.on_tool_called(SESSION, 1, name="read", call_id="c3")
    assert emit.flush()
    steps = [e for e in _body() if e["type"] == "step/started"]
    assert len(steps) == 1, "a documented limitation: consecutive tool-only calls share a step"
    calls = [e["data"] for e in _body() if e["type"] == "tool/called"]
    assert [c["step"] for c in calls] == [1, 1, 1]
    assert [c["call_index"] for c in calls] == [1, 2, 3]


# --- tool payload accounting ---------------------------------------------


def test_the_error_flag_is_recorded_as_given_not_derived_from_status():
    # The stream has no error boolean: a refusal is the presence of a refusal
    # object, so the site that can see that computes it and this records it.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="refused", is_error=True)
    emit.on_tool_completed(SESSION, 1, call_id="c2", status="completed", is_error=False)
    assert emit.flush()
    done = [e["data"] for e in _body() if e["type"] == "tool/completed"]
    assert done[0]["is_error"] is True
    assert done[1]["is_error"] is False


# --- the queue ------------------------------------------------------------


def test_a_queued_message_records_its_size_and_place_but_no_turn():
    # It belongs to no turn yet: stamping the RUNNING turn's ordinal would
    # attribute one person's message to another's turn.
    _open_session()
    emit.on_message_queued(SESSION, source="slack", size_bytes=42, queued_seq="q-7")
    assert emit.flush()
    entry = _body()[-1]
    assert entry["type"] == "message/queued"
    assert entry["data"] == {"source": "slack", "bytes": 42, "queued_seq": "q-7"}
    assert "turn" not in entry["data"]


def test_a_queued_message_does_not_record_its_body_twice():
    # The body arrives with message/received when the queue drains.
    _open_session()
    emit.on_message_queued(SESSION, source="dashboard", size_bytes=9, queued_seq="q-1")
    assert emit.flush()
    assert "text" not in _body()[-1]["data"]


# --- redaction ------------------------------------------------------------


def test_no_job_is_ever_submitted_without_a_session_key():
    # A job in the "" bucket is ordered against nothing: it can be drained before
    # or after the session-keyed lifecycle entries it belongs between, so a reader
    # folding the file in order sees a config change land inside the wrong turn.
    # The signature makes the key required; this proves no caller passes an empty
    # one on the enabled path.
    import inspect

    # Read the PARAMETER, not the source text: a type alias, a reflowed signature
    # or a renamed annotation would all break a string match without changing the
    # thing that matters, which is that the key has no default to fall back on.
    param = inspect.signature(emit._submit).parameters["session_id"]
    assert param.default is inspect.Parameter.empty, (
        "session_id must stay required -- a default silently reintroduces the " "unordered bucket"
    )
    assert param.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ), f"session_id became {param.kind}, which lets a caller omit it"
    _open_session()
    seen: list[str] = []
    real = emit._submit

    def _spy(job, what, session_id, *a, **kw):
        seen.append(session_id)
        return real(job, what, session_id, *a, **kw)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_submit", _spy)
        # Every family this PR emits, driven through one turn.
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_request_configured(SESSION, 1, model="m", provider="p", context_window=1000)
        emit.on_context_composed(SESSION, 1, step=1, blocks={"steering": 40}, total_chars=40)
        emit.on_step_started(SESSION, 1)
        _typed(1, "hello")
        emit.on_message_sent(SESSION, 1, step=1, text="hi")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="call-1", args='{"path":"/x"}')
        emit.on_tool_completed(SESSION, 1, name="fs_read", call_id="call-1", result="ok")
        emit.close_open_tool_calls(SESSION, 1)
        emit.on_step_completed(SESSION, 1, 1, ms=5)
        emit.on_message_queued(SESSION, source="slack", size_bytes=3, queued_seq="q-1")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()
    assert seen, "no job was submitted at all -- the spy never ran"
    assert "" not in seen, f"{seen.count('')} job(s) landed in the unordered bucket"


def test_a_rerun_immediately_after_a_resume_does_not_reuse_the_attempt():
    # The seed is a queued job; deriving the attempt on the caller's thread would
    # read the map while that seed is still in the queue, and the first rerun
    # after a restart would then write an attempt the file already holds.
    _open_session()
    emit.on_turn_started(SESSION, 7, "user")
    emit.on_turn_completed(SESSION, 7, stop_reason="end_turn")
    assert emit.flush()
    emit.reset_caches()

    # Re-attach and rerun ordinal 7 with NO flush in between: the rerun's job is
    # submitted while the seed is still queued behind it.
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", resumed=True)
    emit.on_turn_started(SESSION, 7, "user")
    assert emit.flush()
    starts = [e for e in _body() if e["type"] == "turn/started" and e["data"]["turn"] == 7]
    assert len(starts) == 2, f"expected two starts at ordinal 7, got {len(starts)}"
    assert "attempt" not in starts[0]["data"], "the first try must carry no field"
    assert (
        starts[1]["data"].get("attempt") == 2
    ), f"the rerun reused the attempt: {starts[1]['data']}"


def test_the_turn_closer_is_the_last_entry_of_its_turn():
    # `turn/completed` is the one entry whose POSITION carries meaning. A reader
    # folding in order treats it as the turn's boundary, so a `message/sent`
    # after it belongs to a turn the file already closed.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    first = emit.on_step_started(SESSION, 1)
    emit.on_message_sent(SESSION, 1, step=first, text="first model call")
    second = emit.on_step_started(SESSION, 1)
    emit.on_message_sent(SESSION, 1, step=second, text="the turn's last text")
    emit.on_step_completed(SESSION, 1, second, ms=5)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()
    types = [e["type"] for e in _body() if e["type"].startswith(("turn/", "step/", "message/"))]
    assert types[-1] == "turn/completed", f"the closer is not last: {types}"
    assert types.index("turn/completed") > max(
        i for i, t in enumerate(types) if t == "message/sent"
    ), "an assistant message lands after the turn that produced it was closed"


def test_the_closer_never_reaches_another_turns_open_call():
    # A transient can leave a call open and the same session then runs another
    # turn. Closing every open call for the session would stamp THIS turn's
    # ordinal on the earlier turn's tool -- a tool attributed to a turn that never
    # used it, which is worse than the open call it was trying to tidy.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="call-a", args="{}")
    # Turn 1 ends without a result frame for call-a AND without its closer running.
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_tool_called(SESSION, 2, name="fs_write", call_id="call-b", args="{}")
    closed = emit.close_open_tool_calls(SESSION, 2)
    assert emit.flush()
    assert closed == 1, f"turn 2's closer touched {closed} calls, expected only its own"
    done = [e for e in _body() if e["type"] == "tool/completed"]
    assert [e["data"]["call_id"] for e in done] == ["call-b"]
    assert done[0]["data"]["turn"] == 2
    # The other turn's call is still open, which is the interrupted-turn repair's
    # job -- it works from the file, not from this process's memory.
    assert not [e for e in done if e["data"]["call_id"] == "call-a"]


def test_an_oversize_typed_message_is_recorded_rather_than_refused():
    # A received message is a body a person typed. Written straight onto the entry,
    # one over the ceiling is refused at append time and the message that actually
    # reached the turn is missing from the log.
    _open_session()
    body = "s" * (lg.MAX_ENTRY_BYTES + 500)
    _typed(1, body, source="dashboard")
    assert emit.flush()
    received = [e for e in _body() if e["type"] == "message/received"]
    chunks = [e for e in _body() if e["type"] == "message/chunk"]
    assert received, "the message was refused and lost"
    assert received[-1]["data"]["source"] == "dashboard"
    assert received[-1]["data"]["chunks"] == [c["seq"] for c in chunks]
    assert "".join(c["data"]["delta"] for c in chunks) == body


def test_entries_for_one_session_land_in_the_order_they_were_emitted():
    # The ordering guarantee, asserted on the file rather than on the signature. A
    # job in the shared "" bucket is ordered against nothing, so it can drain
    # before or after the session-keyed entries it belongs between -- and a reader
    # folding the file in order would see a config change inside the wrong turn or
    # a tool result before the call that produced it.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_request_configured(SESSION, 1, model="m", provider="p", context_window=1000)
    step = emit.on_step_started(SESSION, 1)
    emit.on_message_sent(SESSION, 1, step=step, text="first")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="c1", args="{}")
    emit.on_tool_completed(SESSION, 1, name="fs_read", call_id="c1", result="ok")
    emit.on_message_sent(SESSION, 1, step=step, text="second")
    emit.on_step_completed(SESSION, 1, step, ms=1)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()
    got = [e["type"] for e in _body() if e["type"] != "session/opened"]
    expected = [
        "turn/started",
        "request/configured",
        "step/started",
        "message/sent",
        "tool/called",
        "tool/completed",
        "message/sent",
        "step/completed",
        "turn/completed",
    ]
    assert got == expected, f"entries were reordered:\n  got      {got}\n  expected {expected}"
    seqs = [e["seq"] for e in _body()]
    assert seqs == sorted(seqs), "seq is not monotonic in the file"


def test_an_entry_in_the_window_before_the_turn_is_published_is_not_blamed_on_turn_zero():
    # The slot's ordinal is assigned partway through the turn while the ACP client
    # is reachable earlier, so an entry written between the two cannot take its turn
    # from the slot -- it would read 0 or the previous turn's number. The segment
    # flush is the real caller: it holds a slot and asks the emitter for the turn.
    # The emitter's live record is opened at the turn's start, so it is already
    # right in that window.
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    assert emit.live_turn(SESSION) == 4, "the live record is not readable at turn start"
    emit.on_message_sent(SESSION, emit.live_turn(SESSION), text="partial reply")
    assert emit.flush()
    sent = [e for e in _body() if e["type"] == "message/sent"]
    assert sent and sent[-1]["data"]["turn"] == 4

    # And with no turn running the answer is 0, which the caller must treat as
    # "record what you actually have" rather than stamping 0 on an entry.
    emit.on_turn_completed(SESSION, 4, stop_reason="end_turn")
    assert emit.flush()
    assert emit.live_turn(SESSION) == 0
    assert emit.live_turn("no-such-session") == 0


def test_the_attempt_seed_reads_the_file_only_when_memory_cannot_answer():
    # The scan is O(file) on the single writer thread, so seeding a session that
    # already holds its counts costs one full rescan per open against a file that
    # only grows -- and every other session's appends queue behind it.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    scans: list[str] = []
    real = emit._seed_attempts

    def _spy(session_id, log):
        scans.append(session_id)
        return real(session_id, log)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_seed_attempts", _spy)

        # A warm reuse holds its counts: no scan.
        emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7")
        assert emit.flush()
        assert scans == [], "a warm reuse rescanned the file"

        # A resume belongs to a process that is gone: scan.
        emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", resumed=True)
        assert emit.flush()
        assert scans == [SESSION], "a resume did not seed from the file"

        # An evicted map cannot answer either: scan.
        with emit._lock:
            emit._attempts.pop(SESSION, None)
        emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7")
        assert emit.flush()
        assert scans == [SESSION, SESSION], "an evicted map was not re-seeded"


def test_a_turns_closers_name_the_turn_that_started_it():
    # The runner's message-slice index is reset when a mid-turn clear empties the
    # message list. That index and the crew log's turn ordinal must not be the same
    # value: sharing one would move the ordinal to 0 mid-turn, and the turn's own
    # closers -- emitted at the end -- would then name a turn that never started,
    # filing the turn's cost under a phantom ordinal 0 while the real turn shows a
    # start and no completion.
    _open_session()
    emit.on_turn_started(SESSION, 3, "user")
    step = emit.on_step_started(SESSION, 3)
    emit.on_tool_called(SESSION, 3, name="fs_read", call_id="c9", args="{}")
    # Whatever the runner does to its own slice index, the crew log's ordinal is the
    # one the turn opened with.
    emit.close_open_tool_calls(SESSION, 3)
    emit.on_step_completed(SESSION, 3, step, ms=1)
    emit.on_turn_completed(SESSION, 3, stop_reason="end_turn")
    assert emit.flush()
    turns = {e["data"].get("turn") for e in _body() if e["type"] != "session/opened"}
    assert turns == {3}, f"entries were split across ordinals: {sorted(turns, key=str)}"
    assert [e["type"] for e in _body()][-1] == "turn/completed"
    # The live record is released under the ordinal it was created with, so no
    # finished turn is left pinned for a later entry to be blamed on.
    assert emit.live_turn(SESSION) == 0, "a completed turn is still the live turn"


def test_a_credential_cannot_survive_the_overflow_split_at_any_offset():
    # The split is safe for one reason only: the body is redacted ONCE, whole,
    # before it is sliced. So a credential is already gone when slicing happens,
    # and no slice boundary can cut it into two unmatched halves. This is exactly
    # what a per-delta streaming emitter could not offer, which is why there is
    # none: redacting each delta on its own leaves a credential split across two
    # of them intact in both, and the pieces concatenate.
    marker = "A" + "KIA" + "IOSFODNN7" + "EXAMPLE" + "0" * 24
    secret = "aws_secret" + "_access_key=" + marker
    # Walk the credential across a slice boundary one byte at a time.
    boundary = lg.MAX_ENTRY_BYTES - 4096
    for offset in range(0, len(secret) + 1, 7):
        emit.reset_caches()
        _open_session()
        filler_before = "f" * max(0, boundary - offset)
        emit.on_message_sent(
            SESSION,
            1,
            step=1,
            text=filler_before + secret + "t" * 8000,
        )
        assert emit.flush()
        blob = _log_path().read_text(encoding="utf-8")
        assert marker not in blob, f"the credential survived at offset {offset}"
        # And reassembling the cited chunks must not rebuild it either.
        chunks = [e for e in _body() if e["type"] == "message/chunk"]
        assert marker not in "".join(
            c["data"]["delta"] for c in chunks
        ), f"the credential reassembled from chunks at offset {offset}"


def test_no_second_thread_appends_while_a_writer_batch_is_claimed(monkeypatch):
    # Two threads appending to one file is not just a race for the lock: flock
    # serializes the writes but does not ORDER them, so the batch claimed second
    # can reach the file first and a `turn/started` can land at a lower seq than
    # the `session/opened` before it. A log whose seq disagrees with causality is
    # worse than a short one, because a fold cannot detect it.
    import threading

    release = threading.Event()
    holding = threading.Event()
    writers: list[str] = []
    real_append = lg.CrewLog.append

    def _slow(self, *args, **kwargs):
        writers.append(threading.current_thread().name)
        if not holding.is_set():
            holding.set()
            release.wait(timeout=10.0)
        return real_append(self, *args, **kwargs)

    _open_session()
    assert emit.flush()

    async def _emit() -> None:
        emit.on_turn_started(SESSION, 1, "user")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _slow)
        asyncio.run(_emit())
        assert holding.wait(timeout=10.0), "the writer never claimed a batch"
        claimer = writers[0]

        # A synchronous emit lands here with no event loop running, which is the
        # path that writes inline rather than handing the job to the writer.
        done = threading.Event()

        def _emit_sync() -> None:
            emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
            done.set()

        worker = threading.Thread(target=_emit_sync, name="sync-caller")
        worker.start()
        # It must NOT have appended while the batch is still claimed.
        assert not done.wait(timeout=1.0) or writers == [
            claimer
        ], f"a second thread appended beside a claimed batch: {writers}"
        assert writers == [claimer], f"expected only {claimer} to have written: {writers}"
        release.set()
        worker.join(timeout=15.0)
        assert not worker.is_alive(), "the synchronous caller never completed"
    assert emit.drain_for_shutdown(timeout=10.0) is True
    seqs = [e["seq"] for e in _body()]
    assert seqs == sorted(seqs), f"seq is not monotonic: {seqs}"
    kinds = [e["type"] for e in _body()]
    assert kinds.index("turn/started") < kinds.index(
        "turn/completed"
    ), f"the file disagrees with emit order: {kinds}"


def test_every_gateway_mode_drains_the_log_before_a_hard_exit():
    # The dashboard registers its own cleanup hook, but a mode that builds no
    # dashboard app -- slack-only is the plain case -- never runs one, and the hard
    # exit skips atexit. So the drain has to sit on the exit path ITSELF, which is
    # the only code every mode goes through. What its absence drops is the last
    # thing each session did, which is what a reader looks for after a restart.
    #
    # Inspected rather than executed, matching how this repo already tests
    # `_shutdown_and_exit`: the function ends in `os._exit`, so calling it would
    # take the test process with it. Read from the imported MODULE, not from a
    # path relative to the working directory: a shard that runs pytest from
    # anywhere else, or from a Windows checkout, would otherwise fail on the read
    # rather than on the thing being asserted.
    import ast
    import importlib
    import inspect

    gateway_module = importlib.import_module("kiro_crew.slack.gateway")
    tree = ast.parse(inspect.getsource(gateway_module))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_shutdown_and_exit"
    )

    def _calls(name: str) -> list[int]:
        found = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                func = node.func
                attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if attr == name:
                    found.append(node.lineno)
            elif isinstance(node, ast.Attribute) and node.attr == name:
                # Handed to a thread rather than called on this one, which is how
                # a bounded blocking drain belongs on an async path.
                found.append(node.lineno)
        return found

    drains = _calls("drain_for_shutdown")
    exits = _calls("_exit")
    assert drains, "the hard-exit path does not drain the session log at all"
    assert exits, "this test no longer finds the hard exit it is anchored to"
    assert min(drains) < min(
        exits
    ), f"the crew log drain at line {min(drains)} runs after the exit at {min(exits)}"
    # And before the log-queue drain, so a crew log warning still reaches the log.
    log_drains = _calls("drain_log_queue_before_hard_exit")
    if log_drains:
        assert min(drains) < min(log_drains), (
            "the crew log drain runs after the log queue is flushed, so its own "
            "warning about an incomplete drain would be lost"
        )


def test_a_body_is_redacted_by_the_emitter_not_trusted_from_the_call_site():
    # Some sites hand over already-clean text and some hand over raw input. A
    # rule enforced here cannot be forgotten by the next site that is added.
    _open_session()
    # Assembled at runtime, never written as one literal: a credential-shaped
    # string in a source file is what the content scan exists to stop, and a test
    # fixture is not an exception the scanner can see.
    marker = "A" + "KIA" + "IOSFODNN7" + "EXAMPLE" + "0" * 24
    secret = "aws_secret" + "_access_key=" + marker
    _typed(1, f"use {secret} please")
    emit.on_message_sent(SESSION, 1, text=f"ok, {secret}")
    assert emit.flush()
    blob = json.dumps(_body())
    assert marker not in blob


def test_a_redaction_failure_writes_no_text_rather_than_the_raw_text(monkeypatch):
    # Fail CLOSED: the module's promise is that data carries no secrets.
    def boom(_text):
        raise RuntimeError("redaction is broken")

    monkeypatch.setattr(emit, "redact_exfiltration_urls", boom)
    _open_session()
    _typed(1, "a very secret thing")
    assert emit.flush()
    entry = _body()[-1]
    assert entry["data"]["text"] == ""
    assert "very secret thing" not in json.dumps(entry)


def test_an_empty_body_writes_no_sent_entry_at_all():
    _open_session()
    before = len(_body())
    emit.on_message_sent(SESSION, 1, text="")
    assert emit.flush()
    assert len(_body()) == before


# --- the new families obey the module flag -------------------------------


def test_every_new_family_is_silent_with_the_flag_off(monkeypatch):
    _open_session()
    monkeypatch.delenv(emit.CREW_LOG_ENV, raising=False)
    before = len(_body())
    emit.on_message_received(SESSION, 1, text="x")
    emit.on_message_sent(SESSION, 1, text="y")
    emit.on_request_configured(SESSION, 1, model="m", provider="p")
    emit.on_context_composed(SESSION, 1, blocks={"memory": 10})
    emit.on_step_started(SESSION, 1)
    emit.on_step_completed(SESSION, 1, 1, ms=1)
    emit.on_message_queued(SESSION, source="s", size_bytes=1, queued_seq="q")
    assert len(_body()) == before


def test_a_disabled_emitter_does_no_payload_work_at_all(monkeypatch):
    """Off is FREE, not merely silent.

    Hashing a tool result is proportional to its size and redaction walks a whole
    body, both once per frame on the event loop. An emitter that did that work and
    then discarded it inside the writer would charge every user for a feature that
    is off by default.
    """
    _open_session()
    monkeypatch.delenv(emit.CREW_LOG_ENV, raising=False)
    worked: list[str] = []
    monkeypatch.setattr(
        emit, "_payload_digest", lambda payload: worked.append("digest") or ("", -1)
    )
    monkeypatch.setattr(emit, "_safe_text", lambda text: worked.append("redact") or "")

    emit.on_tool_called(SESSION, 1, name="t", call_id="c1", args="v" * 10000)
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed", result="r" * 10000)
    emit.on_message_received(SESSION, 1, text="body")
    emit.on_message_sent(SESSION, 1, text="body")
    emit.on_request_configured(SESSION, 1, model="m", provider="p", system="s" * 10000)

    assert worked == [], f"a disabled emitter still did payload work: {worked}"


def test_the_same_work_does_happen_when_enabled(monkeypatch):
    # The mirror of the test above, so it cannot pass by the calls being wrong.
    _open_session()
    worked: list[str] = []
    real_digest = emit._payload_digest
    real_safe = emit._safe_text
    monkeypatch.setattr(
        emit, "_payload_digest", lambda payload: (worked.append("digest"), real_digest(payload))[1]
    )
    monkeypatch.setattr(
        emit, "_safe_text", lambda text: (worked.append("redact"), real_safe(text))[1]
    )
    emit.on_tool_called(SESSION, 1, name="t", call_id="c1", args="v")
    emit.on_message_received(SESSION, 1, text="body")
    assert emit.flush()
    assert "digest" in worked
    assert "redact" in worked


def test_an_mcp_tool_records_its_server():
    _open_session()
    emit.on_tool_called(SESSION, 1, name="InternalSearch", server="builder-mcp", call_id="tc-3")
    assert _body()[-1]["data"]["server"] == "builder-mcp"


def test_tool_completion_measures_elapsed_ms_from_its_call():
    _open_session()
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-2")
    emit.on_tool_completed(SESSION, 1, name="fs_read", status="completed", call_id="tc-2")
    assert _body()[-1]["data"]["elapsed_ms"] >= 0


def test_completion_inherits_the_identity_the_call_frame_carried():
    """The terminal ACP frame repeats neither the tool name nor its server."""
    _open_session()
    emit.on_tool_called(SESSION, 1, name="InternalSearch", server="builder-mcp", call_id="tc-4")
    emit.on_tool_completed(SESSION, 1, status="completed", call_id="tc-4")
    data = _body()[-1]["data"]
    assert data["name"] == "InternalSearch"
    assert data["server"] == "builder-mcp"


def test_an_explicit_completion_name_is_not_overwritten_by_the_remembered_one():
    _open_session()
    emit.on_tool_called(SESSION, 1, name="old", server="old-srv", call_id="tc-5")
    emit.on_tool_completed(
        SESSION, 1, name="new", server="new-srv", status="completed", call_id="tc-5"
    )
    data = _body()[-1]["data"]
    assert data["name"] == "new"
    assert data["server"] == "new-srv"


def test_tool_completion_without_a_matching_call_omits_elapsed_ms():
    _open_session()
    emit.on_tool_completed(SESSION, 1, name="fs_read", status="completed", call_id="unseen")
    assert "elapsed_ms" not in _body()[-1]["data"]


def test_approval_request_and_decision_share_the_approval_id():
    _open_session()
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_approval_requested(SESSION, 2, approval_id="ap-1", tool="execute_bash")
    emit.on_approval_decided(SESSION, 2, approval_id="ap-1", decision="rejected_once")
    request, decision = _body()[-2:]
    assert request["data"]["approval_id"] == decision["data"]["approval_id"] == "ap-1"
    assert request["data"]["tool"] == "execute_bash"
    assert decision["data"]["decision"] == "rejected_once"


def test_compaction_records_percentages_not_token_counts():
    _open_session()
    emit.on_compaction_applied(SESSION, pct_before=0.92, pct_after=0.31)
    data = _body()[-1]["data"]
    assert data["pct_before"] == 0.92
    assert data["pct_after"] == 0.31
    assert data["freed_pct"] == pytest.approx(0.61)
    assert "before_tokens" not in data


def test_model_selection_records_its_source():
    _open_session()
    emit.on_model_selected(SESSION, "claude-haiku-4.5", "fallback")
    assert _body()[-1]["data"] == {
        "model": "claude-haiku-4.5",
        "source": "fallback",
    }


def test_close_records_the_gateway_reason_verbatim():
    _open_session()
    emit.on_session_closed(SESSION, "shutdown")
    assert _body()[-1]["data"]["reason"] == "shutdown"


# --- durable loss markers -------------------------------------------------


def test_spent_retry_budget_flushes_loss_marker_without_a_later_append():
    _open_session()
    assert emit.flush()
    before = len(_body())

    def _fail() -> None:
        raise OSError("retry loss")

    emit._buffer(
        SESSION,
        emit._PendingJob(job=_fail, what="retry loss", nbytes=23),
    )
    assert emit.flush(timeout=2.0), "flush returned without writing the spent-budget loss marker"
    assert emit.dropped_writes() == 1
    recovered = _body()[before:]
    assert [entry["type"] for entry in recovered] == [
        "write/dropped"
    ], f"flush left the loss marker owed: {[e['type'] for e in recovered]}"
    assert recovered[0]["data"]["dropped_count"] == 1
    assert recovered[0]["data"]["dropped_bytes"] == 23


def _leave_spent_retry_loss_owed(*, nbytes: int = 23) -> None:
    """Spend one job's budget without letting the following marker pass run."""

    def _fail() -> None:
        raise OSError("retry loss")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_start_drain", lambda: None)
        emit._buffer(SESSION, emit._PendingJob(job=_fail, what="retry loss", nbytes=nbytes))
        for _ in range(emit._MAX_WRITE_ATTEMPTS):
            with emit._lock:
                emit._draining = False
                emit._drain_future = None
                retry = emit._retry.get(SESSION)
                if retry is not None:
                    retry.not_before = 0.0
            emit._drain_once()

    with emit._lock:
        loss = emit._pending_loss.get(SESSION)
        assert loss is not None, "the retry budget did not leave loss debt"
        assert SESSION not in emit._pending, "the spent job was still buffered"


def test_ceiling_overflow_drains_with_marker_first(monkeypatch):
    _open_session()
    assert emit.flush()
    monkeypatch.setattr(emit, "_MAX_PENDING_COUNT", 0)
    emit._buffer(
        SESSION,
        emit._PendingJob(job=lambda: None, what="overflowed append", nbytes=31),
    )
    assert emit.overflow_writes() == 1

    monkeypatch.setattr(emit, "_MAX_PENDING_COUNT", 100_000)
    before = len(_body())
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush(timeout=20.0)
    recovered = _body()[before:]
    assert (
        recovered[0]["type"] == "write/dropped"
    ), f"first entry after overflow was not marker: {[e['type'] for e in recovered]}"
    assert recovered[0]["data"]["dropped_count"] == 1
    assert recovered[0]["data"]["dropped_bytes"] == 31


def test_a_lost_marker_is_merged_into_one_later_marker():
    _open_session()
    assert emit.flush()
    _leave_spent_retry_loss_owed(nbytes=17)

    real_append = lg.CrewLog.append

    def _lose_marker(self, entry_type, *args, **kwargs):
        if entry_type == "write/dropped":
            raise OSError("marker loss")
        return real_append(self, entry_type, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _lose_marker)
        patch.setattr(emit, "_start_drain", lambda: None)
        deferred_loss: set[str] = set()
        for _ in range(emit._MAX_WRITE_ATTEMPTS):
            with emit._lock:
                emit._draining = False
                emit._drain_future = None
                retry = emit._retry.get(SESSION)
                if retry is not None:
                    retry.not_before = 0.0
            deferred_loss.update(emit._drain_once(deferred_loss))
        assert not emit.flush(
            timeout=0.1
        ), "flush reported quiet while the refused loss marker was still owed"

    before = len(_body())
    emit.on_turn_completed(SESSION, 2, stop_reason="recovered")
    assert emit.flush(timeout=20.0)
    recovered = _body()[before:]
    markers = [entry for entry in recovered if entry["type"] == "write/dropped"]
    assert len(markers) == 1, f"marker loss split or lost debt: {markers}"
    assert (
        markers[0]["data"]["dropped_count"] == 1
    ), f"marker loss did not preserve the original debt: {markers[0]['data']}"


def test_fold_reads_loss_marker_with_contiguous_sequence():
    _open_session()
    assert emit.flush()
    emit._buffer(
        SESSION,
        emit._PendingJob(
            job=lambda: (_ for _ in ()).throw(OSError("fold loss")),
            what="fold loss",
        ),
    )
    assert emit.flush(timeout=20.0)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush(timeout=20.0)

    known = {"session/opened", "write/dropped", "turn/completed"}
    entries = list(lg.CrewLog.open(lg.KIND_SESSION, SESSION).iter_from(1, known=known))
    kinds = [entry.type for entry in entries]
    assert "write/dropped" in kinds, f"reader did not observe write/dropped: {kinds}"
    seqs = [entry.seq for entry in entries]
    assert seqs == list(range(1, len(seqs) + 1)), f"marker introduced a seq gap: {seqs}"


def test_a_permanent_refusal_midbatch_owes_a_loss_marker():
    _open_session()
    assert emit.flush()

    real_append = lg.CrewLog.append
    refused_once = {"left": 1}

    def _refuse_turn_started(self, entry_type, *args, **kwargs):
        if entry_type == "turn/started" and refused_once["left"]:
            refused_once["left"] -= 1
            raise lg.CrewLogError("another process owns this log", code=lg.CODE_ALREADY_OWNED)
        return real_append(self, entry_type, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _refuse_turn_started)

        async def _batch() -> None:
            # Both land in one drain: the first is refused (permanent), the batch
            # continues to the second, so the file resumes right after the hole.
            emit.on_turn_started(SESSION, 1, "user")
            emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
            assert emit.flush(timeout=20.0)

        asyncio.run(_batch())

    assert refused_once["left"] == 0, "the injected refusal never fired"
    assert emit.dropped_writes() == 1, "the refused entry was not counted"
    body = _body()
    assert (
        body[-2]["type"] == "write/dropped"
    ), f"the entry after a refusal was not the marker: {[e['type'] for e in body]}"
    marker = body[-2]["data"]
    assert marker["dropped_count"] == 1
    assert body[-1]["type"] == "turn/completed", "the batch did not resume after the marker"


# --- fail-soft ------------------------------------------------------------


def test_a_refused_oversize_write_is_dropped_and_counted_and_named_once(caplog):
    """A refusal is a loss NOW, not a retry, and it is admitted rather than hidden.

    The storage layer checks an entry against the format before it writes a byte,
    so a refused entry leaves the file untouched and would be refused identically
    on every retry. Retaining one would spend the whole attempt budget on a verdict
    that cannot change and hold the rest of that session's log behind it, so it is
    dropped at once -- and counted, because a hole a caller can read is the only
    kind this writer is allowed to leave.
    """
    _open_session()
    before = len(_entries())
    with caplog.at_level(logging.WARNING, logger=emit.logger.name):
        emit.on_tool_called(SESSION, 1, name="x" * (70 * 1024), call_id="tc-big")
        emit.on_tool_called(SESSION, 1, name="y" * (70 * 1024), call_id="tc-big2")
    assert len(_entries()) == before, "a refused write must not land"
    assert emit.dropped_writes() == 2, "both losses are counted"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # One for the storage failure, one for the loss -- and the SECOND refusal adds
    # neither, because a failing crew log names itself once rather than per entry.
    assert len(warnings) == 2, f"expected the failure and the loss once each: {warnings}"
    assert any("gave up on" in record.getMessage() for record in warnings)


def test_a_refused_write_does_not_break_the_next_good_write():
    _open_session()
    emit.on_tool_called(SESSION, 1, name="z" * (70 * 1024), call_id="tc-big")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert _body()[-1]["type"] == "turn/completed"


def test_a_storage_layer_that_raises_never_breaks_a_caller(monkeypatch):
    _open_session()

    class Exploding:
        def append(self, *args, **kwargs):
            raise RuntimeError("disk is on fire")

    monkeypatch.setitem(emit._open, SESSION, Exploding())
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 1, name="fs_read", status="completed", call_id="tc-1")
    emit.on_approval_requested(SESSION, 1, approval_id="ap-1")
    emit.on_approval_decided(SESSION, 1, approval_id="ap-1", decision="approved")
    emit.on_model_selected(SESSION, "m", "config")
    emit.on_compaction_applied(SESSION, pct_before=0.9, pct_after=0.2)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_session_closed(SESSION, "reset")


def test_a_failed_turn_start_leaves_later_entries_unthreaded(monkeypatch):
    """An anchor is only claimed from an entry that actually landed."""
    _open_session()
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert "thread" not in _body()[-1]


def test_events_for_a_session_with_no_log_create_nothing():
    emit.on_turn_started("never-opened", 1, "user")
    emit.on_tool_called("never-opened", 1, name="fs_read", call_id="tc-1")
    assert not _log_path("never-opened").exists()


def test_a_missing_session_id_is_a_no_op():
    emit.on_session_opened("", agent="kirocrew")
    emit.on_turn_started("", 1, "user")
    emit.on_session_closed("", "reset")
    assert not _store_root().exists()


# --- cache pressure -------------------------------------------------------


def test_cache_pressure_never_closes_a_live_turn(monkeypatch):
    """What a bounded LRU of open handles must not do to a running turn.

    With more concurrent crew logs than the cache holds, evicting a live session's
    handle makes the next emit in that same turn reopen the crew log -- and an open
    that repaired would append a false `turn/completed {interrupted}` into a turn
    that then keeps writing. Two things prevent it: repair is opt-in and only a
    resume asks for it, and a session with a turn in flight is not evicted at all.
    """
    _open_session()
    emit.on_turn_started(SESSION, 7, "user")
    emit.on_tool_called(SESSION, 7, name="fs_read", call_id="tc-1")

    # Enough other sessions to overrun the cache several times over.
    for n in range(emit._MAX_OPEN_CREW_LOGS + 1):
        other = f"filler-{n:04d}"
        emit.on_session_opened(other, agent="kirocrew")
        emit.on_turn_started(other, 1, "user")
        emit.on_turn_completed(other, 1, stop_reason="end_turn")
    assert emit.flush()

    # The live session survived the pressure ...
    assert SESSION in emit._open
    # ... nothing closed its turn ...
    types = [e["type"] for e in _body()]
    assert "turn/completed" not in types
    assert types.count("tool/called") == 1
    # ... and its next entry still names the turn it belongs to.
    emit.on_tool_completed(SESSION, 7, status="completed", call_id="tc-1")
    assert emit.flush()
    last = _body()[-1]
    assert last["type"] == "tool/completed"
    assert last["data"]["turn"] == 7


def test_a_finished_session_is_evicted_so_the_cache_stays_bounded():
    # Pinning must not turn the cap into a leak: a session whose turn ended is
    # evictable again, which is what keeps a long-lived gateway bounded.
    for n in range(emit._MAX_OPEN_CREW_LOGS + 8):
        other = f"done-{n:04d}"
        emit.on_session_opened(other, agent="kirocrew")
        emit.on_turn_started(other, 1, "user")
        emit.on_turn_completed(other, 1, stop_reason="end_turn")
    assert emit.flush()
    assert len(emit._open) <= emit._MAX_OPEN_CREW_LOGS


def test_a_reconnect_after_an_eviction_does_not_repair(monkeypatch):
    # The reconnect path directly: a handle absent from the cache says nothing
    # about the writer's health, so reopening must not close anything.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()
    with emit._lock:
        emit._open.clear()  # simulate the eviction the cap would have made
    emit.on_tool_completed(SESSION, 1, status="completed", call_id="tc-1")
    assert emit.flush()
    types = [e["type"] for e in _body()]
    assert "turn/completed" not in types
    assert types[-1] == "tool/completed"


def test_a_resume_closes_a_child_the_registry_no_longer_lists():
    # The reachability the registration exists for. A crashed gateway's successor
    # has an empty registry, so a child that never reported is gone and its opener
    # is closed. Without a registered probe the store closes nothing, which is what
    # the next test pins -- so this asserts the wiring, not just the store.
    emit.set_child_liveness(lambda agent_id: False)
    try:
        _open_session()
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
        assert emit.flush()
        emit.reset_caches()
        emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
        assert emit.flush()
        closers = [e for e in _body() if e["type"] == "subagent/failed"]
        assert [e["data"]["agent_id"] for e in closers] == ["sub-1"]
        assert closers[0]["data"]["outcome"] == "unknown"
    finally:
        emit.set_child_liveness(None)


def test_a_resume_leaves_a_child_the_registry_still_lists_alone():
    # The same-process case that made an earlier revision corrupt the file: idle
    # teardown, reset, resume -- while the child is still running and will file its
    # own terminal. The registry still lists it, so the repair must not invent one.
    emit.set_child_liveness(lambda agent_id: agent_id == "sub-1")
    try:
        _open_session()
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
        assert emit.flush()
        emit.reset_caches()
        emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
        assert emit.flush()
        assert [e for e in _body() if e["type"] == "subagent/failed"] == []
    finally:
        emit.set_child_liveness(None)


def test_the_child_probe_reports_present_while_this_session_owes_an_entry():
    # The window the registry alone cannot answer. A child's terminal closer goes
    # through the writer and `_submit` returns before it lands, so the child leaves
    # the running set while its own outcome is still owed: the registry says gone
    # and the file shows an unmatched opener. Closing then puts a synthesised
    # `unknown` ahead of the real outcome, and both stand in a file nothing
    # rewrites. The debt is keyed by session, so another session's backlog must not
    # hold this one's children open.
    emit.set_child_liveness(lambda agent_id: False)
    try:
        gone = emit._child_gone_probe(SESSION)
        assert gone is not None
        assert gone("sub-1") is True
        with emit._lock:
            emit._pending[SESSION] = [emit._PendingJob(job=lambda: None, what="debt")]
        try:
            assert gone("sub-1") is False
        finally:
            with emit._lock:
                emit._pending.pop(SESSION, None)
        with emit._lock:
            emit._pending["other-session"] = [emit._PendingJob(job=lambda: None, what="debt")]
        try:
            assert gone("sub-1") is True
        finally:
            with emit._lock:
                emit._pending.pop("other-session", None)
    finally:
        emit.set_child_liveness(None)


def test_the_pin_cap_drops_a_finished_child_before_a_running_one():
    # A pin is released by its child's terminal entry, so pins for children that
    # never reported one collect at the old end -- and they are what makes this cap
    # reachable by accumulation rather than by that many children genuinely running.
    # The finished one goes first, and no running child's attribution pays for it.
    emit.set_child_liveness(lambda agent_id: agent_id != "gone-1")
    try:
        emit.remember_child_origin("gone-1", SESSION, 1)
        for i in range(2, emit._MAX_CHILD_ORIGINS + 2):
            emit.remember_child_origin(f"live-{i}", SESSION, i)
        assert "gone-1" not in emit._child_origin, "the finished child's pin is the one dropped"
        assert "live-2" in emit._child_origin, "the oldest RUNNING child keeps its pin"
        assert emit.lost_child_origins() == 0, "so nothing live was spent"
    finally:
        emit.set_child_liveness(None)


def test_a_cap_full_of_running_children_drops_the_oldest_and_counts_the_loss():
    # The residual this policy leaves: with every pin belonging to a child that is
    # still running there is nothing free to drop, so the oldest goes. That child's
    # remaining entries will be absent, which is exactly why the drop is counted
    # instead of left for a reader to fail to notice.
    emit.set_child_liveness(lambda agent_id: True)
    try:
        for i in range(emit._MAX_CHILD_ORIGINS + 1):
            emit.remember_child_origin(f"live-{i}", SESSION, i + 1)
        assert emit.lost_child_origins() == 1
        assert "live-0" not in emit._child_origin
        assert "live-1" in emit._child_origin
    finally:
        emit.set_child_liveness(None)


def test_the_cap_counts_what_it_drops_even_with_no_probe_registered():
    # With nothing able to say whether a child finished, the cap cannot prefer a
    # dead pin. That is a reason to drop the oldest, not a reason to do it silently.
    for i in range(emit._MAX_CHILD_ORIGINS + 1):
        emit.remember_child_origin(f"c-{i}", SESSION, i + 1)
    assert emit.lost_child_origins() == 1
    assert "c-0" not in emit._child_origin


def test_an_over_long_session_id_is_refused_and_counted():
    # The count cap bounds memory only if a pin's own fields are bounded, and the
    # session id comes from the provider. It is refused rather than shortened,
    # because an identity that has been cut down names a different unit -- and the
    # refusal is counted, because that child's entries are absent either way.
    emit.remember_child_origin("huge-1", "s" * (emit._MAX_SESSION_ID_CHARS + 1), 1)
    assert "huge-1" not in emit._child_origin, "an unbounded id is not retained"
    assert emit.lost_child_origins() == 1, "and the refusal is not silent"
    # Exactly at the bound is still retained, so it rejects nothing legitimate.
    emit.remember_child_origin("ok-1", "s" * emit._MAX_SESSION_ID_CHARS, 1)
    assert "ok-1" in emit._child_origin
    assert emit.lost_child_origins() == 1


def test_a_resume_with_no_registered_probe_closes_no_child():
    # The default every embedder and test gets. Nothing answers "is this child
    # alive", so the opener stands and a reader treats it as unknown.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="sub-1")
    assert emit.flush()
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
    assert emit.flush()
    assert [e for e in _body() if e["type"] == "subagent/failed"] == []


def test_a_resume_is_the_one_path_that_closes_an_open_turn():
    # `resumed=True` means this claim re-attached to a conversation a different
    # gateway process was writing, so a turn left open there belongs to a writer
    # that is gone -- the one situation where closing it records what happened.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "turn/completed"]
    assert len(closers) == 1
    assert closers[0]["data"] == {"turn": 1, "stop_reason": "interrupted"}


def test_a_resume_does_not_close_a_turn_this_process_is_still_running(caplog):
    """The resume flag is a belief; a live turn of our own is evidence against it.

    `resumed=True` says the writer of this file is gone. When this process is itself
    still running a turn for that id, that is false -- the writer is us. Repairing
    anyway closes a turn that is still producing entries, and the file then says the
    turn completed as interrupted and, further down, completed again for real, with
    the same tool call closed both `unknown` and `completed`. A fold reads two
    outcomes for one turn with no way to tell which happened.

    The legitimate resume is unaffected: a claim re-attaching to a conversation whose
    process is gone holds no live record, so it still repairs (see
    `test_a_resume_is_the_one_path_that_closes_an_open_turn`, which clears the caches
    to model exactly that).

    Mutation guard: repairing on the flag alone writes the interrupted closer here and
    reddens the count below.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()
    # NO reset_caches: this process still holds turn 1 as live, which is the whole
    # point -- the claim below is contradicted by our own state.
    with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.emit"):
        emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
        assert emit.flush()

    body = _body()
    assert "turn/completed" not in [e["type"] for e in body], (
        "a turn this process is still running was closed by a resume; its remaining "
        "entries will follow an outcome it never had"
    )
    assert "tool/completed" not in [
        e["type"] for e in body
    ], "the live turn's open tool call was closed as unknown while it was still running"
    assert any(
        "is still running in this process" in r.getMessage() for r in caplog.records
    ), "the refusal to repair was silent"

    # And the turn still ends exactly once, on its own terms.
    emit.on_tool_completed(SESSION, 1, name="fs_read", call_id="tc-1", status="completed")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn", duration_ms=5)
    assert emit.flush()
    closers = [e for e in _body() if e["type"] == "turn/completed"]
    assert len(closers) == 1, f"the turn completed {len(closers)} times: {closers}"
    assert closers[0]["data"]["stop_reason"] == "end_turn"


@contextlib.contextmanager
def _owned_elsewhere(session_id: str = SESSION):
    """Hold one session log's write ownership from another descriptor.

    An advisory lock belongs to an open file description, so a descriptor of our
    own contends exactly as a second gateway's would. Non-blocking, so a lock this
    process already holds is reported here instead of becoming a wait.
    """
    path = lg.crew_log_dir(lg.KIND_SESSION, session_id) / LEASE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open("r+") as handle:
        with file_lock(handle.fileno(), exclusive=True, required=True, wait=False):
            yield


def _can_take_ownership(session_id: str = SESSION) -> bool:
    """Whether another owner could take this crew log's write lock right now.

    False means this process still owns the log. Non-blocking, so a held lock is
    an answer here rather than a wait.
    """
    try:
        with _owned_elsewhere(session_id):
            return True
    except OSError:
        return False


def test_a_claim_that_cannot_own_the_log_writes_nothing_and_counts_the_loss():
    """A second gateway on a live session loses its own entries, not the log.

    A claim that cannot take a unit's write ownership has two honest options: write
    into a file whose owner is still producing entries, or write nothing and say
    so. This pins the second. The open turn is left as its owner has it, the
    entries this process could not write are counted in `dropped_writes`, and none
    of them is retried -- ownership is held for the life of the owning process,
    which no retry budget outlasts, and a retained batch would hold the rest of
    that session's log behind an entry that cannot land.

    The caches are cleared first so this process holds no live turn and no handle,
    which is the state a fresh gateway starts in: the same-process guard cannot
    answer here, and the kernel lock is the only thing that can.

    Mutation guard: dropping the claim from the store's write paths lets the repair
    close the owner's turn and reddens the first assertion.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()
    before = [e["type"] for e in _body()]
    emit.reset_caches()

    with _owned_elsewhere():
        emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
        emit.on_message_received(SESSION, 1, role="user", text="hello")
        assert emit.flush()
        after = [e["type"] for e in _body()]
        assert after == before, f"a process that owns nothing wrote into the log: {after}"
        assert (
            emit.dropped_writes() == 2
        ), "the entries this process could not write are not counted"

    # Ownership released: the same emitter writes again, so the refusal was about
    # the moment rather than a state it latched.
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn", duration_ms=5)
    assert emit.flush()
    assert [e["type"] for e in _body()][-1] == "turn/completed"


def test_a_claim_does_not_take_back_a_pin_a_queued_closer_still_owes(monkeypatch):
    """A re-claim must leave a turn whose terminal event is still in flight.

    A terminal event is QUEUED, not written, so the pin that keeps its handle --
    and the log's write ownership with it -- is owed to the write job. A claim
    arriving in that window reads the live map, and a claim that closes every
    record it finds drops the pin while the file still shows the turn open:
    capacity eviction then takes the handle, the lease goes with the descriptor,
    and a successor process repairs a turn whose real completion is still on its
    way to disk. A record with no terminal handed over is different -- nothing
    else will ever close it -- and the claim still releases that one.

    The writer is held for the whole window, so the queued state is arranged
    rather than raced for: without that the background writer can land the closer
    and run its release before the claim is even made.

    Mutation guard: releasing every record regardless of an owed closer reddens
    the first assertion.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    release = threading.Event()
    original = lg.CrewLog.append

    def _held(self, *args, **kwargs):
        assert release.wait(20.0)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _held)

    # The terminal is handed over and cannot reach disk; the claim lands in between.
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn", duration_ms=5)
    _open_session()
    assert emit.live_turn(SESSION) == 1, (
        "a re-claim took back a pin its queued closer still owes, so eviction can "
        "release the log while the file still shows that turn open"
    )

    release.set()
    assert emit.flush(timeout=20.0)
    assert "turn/completed" in [e["type"] for e in _body()]
    assert emit.live_turn(SESSION) == 0, "the write job never released the pin it owed"


def test_a_claim_still_closes_a_turn_no_closer_is_coming_for():
    """The stale-record case the claim exists for is unchanged.

    A turn whose terminal event was never emitted has nothing that will release
    it, so a fresh claim -- a new gateway taking over the same session id -- must
    close it. Skipping this one too would leak the pin for the life of the
    process and pin write ownership to a turn no one is writing.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    _open_session()
    assert emit.live_turn(SESSION) == 0, "a claim left a turn nothing will ever close"


def test_a_nested_prompt_turn_keeps_the_author_of_the_turn_that_carried_it():
    """A replacing expansion re-enters the runner, and the actor travels with it.

    An ``@prompt`` mention re-dispatches the turn with the expanded body. The
    actor resolver's fallback is ``user``, so a re-entry that forwards the
    directive flags but drops the actor records a person for a turn an app
    authored -- the same false statement the top-level dispatch was fixed to stop
    making, one frame deeper.
    """
    import ast
    import inspect

    from kiro_crew.dashboard import chat_runner

    tree = ast.parse(inspect.getsource(chat_runner))
    reentries = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "_run_chat"
        and any(kw.arg == "_prompt_depth" for kw in node.keywords)
    ]
    assert reentries, "the expansion re-entry moved -- this pin no longer sees it"
    for call in reentries:
        assert any(kw.arg == "_turn_actor" for kw in call.keywords), (
            "the expansion re-entry drops the actor, so a nested app turn is "
            "recorded as a person who never typed anything"
        )


def test_a_teardown_mid_turn_keeps_the_write_ownership_that_turn_needs():
    """A forced reset must not hand the log away while a turn is still writing.

    The reset route tears a session down with a turn still running, and that turn's
    closers are written later by its own `finally`. The cached handle carries the
    unit's write ownership, so dropping it at teardown releases the log to whichever
    process asks next -- and a second gateway's resume then closes a turn that
    completes for real a moment later, which is the two-outcome record ownership
    exists to prevent. Asserted through the kernel: another owner must not be able
    to take the lock while the turn is live.

    Mutation guard: dropping the handle unconditionally at teardown lets the foreign
    owner take it and reddens the first assertion.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()

    emit.on_session_closed(SESSION, "reset")
    assert emit.flush()
    assert not _can_take_ownership(), "a live turn's write ownership was released by its teardown"

    # And the turn still closes on its own terms, through the handle it kept.
    emit.on_tool_completed(SESSION, 1, name="fs_read", call_id="tc-1", status="completed")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn", duration_ms=5)
    assert emit.flush()
    types = [e["type"] for e in _body()]
    assert (
        types.count("turn/completed") == 1
    ), f"the turn completed {types.count('turn/completed')}x"
    assert types.index("session/closed") < types.index("turn/completed")


def test_a_teardown_between_turns_gives_the_write_ownership_back():
    """With no turn running there is nothing to protect, so the log is released.

    The other half of the rule: holding ownership past the teardown of a quiet
    session would refuse a successor gateway that has every right to the log.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn", duration_ms=5)
    assert emit.flush()

    emit.on_session_closed(SESSION, "reset")
    assert emit.flush()
    gc.collect()
    assert _can_take_ownership(), "a torn-down session with no live turn kept the log"


def test_a_stalled_write_is_named_by_a_producer_without_being_asked(caplog):
    """The stall report has a production caller, not just a test that calls it.

    A hung write reaches no attempt counter, so this line is the only account it
    gives of itself. The thread that would notice is the one blocked in the call,
    which is why a PRODUCER checks -- and a helper nothing calls reports nothing.
    The threshold is injected rather than waited out.

    Mutation guard: removing the call from `_submit` leaves an ordinary emit silent
    and reddens the assertion.
    """
    _open_session()
    assert emit.flush()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(emit, "_WRITE_STALL_SECS", 0.0)
        with emit._lock:
            emit._inflight_since = time.monotonic()
            emit._inflight_what = "a write that never returns"
            emit._stall_reported = False
        try:
            with caplog.at_level(logging.ERROR, logger="kiro_crew.crew_log.emit"):
                # An ordinary producer call, nothing stall-specific about it.
                emit.on_turn_started(SESSION, 1, "user")
            assert any(
                "neither returned nor failed" in r.getMessage() for r in caplog.records
            ), "a producer passed a stalled writer without naming the stall"
        finally:
            with emit._lock:
                emit._inflight_since = 0.0
                emit._inflight_what = ""


def test_a_warm_reuse_inside_this_process_closes_nothing():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew")  # resumed defaults False
    assert emit.flush()
    assert "turn/completed" not in [e["type"] for e in _body()]


def test_a_refused_turn_is_its_own_fact_and_writes_no_start():
    """A start asserts the turn RAN.

    A start written before the dispatch gates leaves an orphan for every refusal,
    and the interrupted-turn repair would later close it as though the turn had
    died mid-flight. The refusal is recorded instead, naming the gate.
    """
    _open_session()
    emit.on_turn_refused(SESSION, 4, "stopped_before_dispatch", "user")
    types = [e["type"] for e in _body()]
    assert "turn/started" not in types
    assert types[-1] == "turn/refused"
    assert _body()[-1]["data"] == {
        "turn": 4,
        "actor": "user",
        "reason": "stopped_before_dispatch",
        "depth": 0,
    }


def test_a_refusal_records_the_actor_it_would_have_run_as():
    _open_session()
    emit.on_turn_refused(SESSION, 1, "not_authorized", "cron")
    assert _body()[-1]["data"]["actor"] == "cron"


def test_a_refusal_leaves_nothing_pinned():
    # Nothing is in flight after a refusal, so the handle and the step ordinals
    # must become evictable again -- otherwise a refused turn leaks a pin.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert SESSION in emit._pinned
    emit.on_turn_refused(SESSION, 1, "gateway_closing", "user")
    assert SESSION not in emit._pinned
    assert (SESSION, 1) not in emit._live


def test_call_index_of_a_live_turn_survives_cache_pressure():
    """A live turn's step counter is its own state, so pressure cannot reset it.

    Dropping it mid-turn restarts the numbering, so the turn's next tool call
    claims a position an earlier call in the same turn already holds.

    The pressure is built from ABORTED turns: a turn that ends releases its own
    record, so it can never crowd the map. A turn that minted a step and was then
    re-claimed is the residue a cap exists to trim -- and what must not take a
    live turn with it.
    """
    _open_session()
    emit.on_turn_started(SESSION, 3, "user")
    emit.on_tool_called(SESSION, 3, name="fs_read", call_id="tc-1")
    emit.on_tool_called(SESSION, 3, name="fs_read", call_id="tc-2")

    for n in range(emit._MAX_OPEN_CREW_LOGS + 1):
        other = f"stepfill-{n:04d}"
        emit.on_session_opened(other, agent="kirocrew")
        emit.on_turn_started(other, 1, "user")
        emit.on_tool_called(other, 1, name="fs_read", call_id="tc-x")
    assert emit.flush()
    assert len(emit._live) > emit._MAX_OPEN_CREW_LOGS, "the map never came under pressure"

    assert (SESSION, 3) in emit._live
    emit.on_tool_called(SESSION, 3, name="fs_read", call_id="tc-3")
    assert emit.flush()
    steps = [e["data"]["call_index"] for e in _body() if e["type"] == "tool/called"]
    assert steps == [1, 2, 3], "the numbering restarted, so two calls share a step"


def test_call_index_stays_unique_when_many_turns_are_live_at_once():
    """More live turns than the handle cap, all still running.

    The oldest live turn is the one any oldest-first trim reaches first, so it is
    the one this drives: its step counter must survive, because a counter that
    restarts mid-turn hands two of that turn's calls the same ordinal. Keeping the
    counter in the same record as the pin is what makes that impossible -- one
    record cannot be half-dropped.
    """
    _open_session()
    emit.on_turn_started(SESSION, 7, "user")
    for call in range(3):
        emit.on_tool_called(SESSION, 7, name="fs_read", call_id=f"early-{call}")

    # Every one of these stays live: no completion, no re-claim.
    for n in range(emit._MAX_OPEN_CREW_LOGS * 2):
        other = f"livefill-{n:04d}"
        emit.on_session_opened(other, agent="kirocrew")
        emit.on_turn_started(other, 1, "user")
    assert emit.flush()
    assert SESSION in emit._pinned, "the oldest live turn lost its pin"

    for call in range(3):
        emit.on_tool_called(SESSION, 7, name="fs_read", call_id=f"late-{call}")
    assert emit.flush()
    steps = [e["data"]["call_index"] for e in _body() if e["type"] == "tool/called"]
    assert steps == sorted(steps), f"ordinals went backwards: {steps}"
    assert len(steps) == len(set(steps)), f"two calls share an ordinal: {steps}"
    assert steps == [1, 2, 3, 4, 5, 6]


def test_call_index_is_released_when_the_turn_completes():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert (SESSION, 1) in emit._live
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert (SESSION, 1) not in emit._live
    assert SESSION not in emit._pinned


def test_a_leaked_turn_is_never_evicted_even_past_the_ceiling(monkeypatch):
    # The ceiling sheds only records whose terminal event is already owed, and
    # accepts the overage when none of those are left. Shedding a LIVE record
    # drops its step and call_index, and its next event re-mints them from zero,
    # so two entries claim one ordinal -- in a file that is never rewritten that
    # duplicate is read as fact. The residue is memory: a leak grows this map
    # rather than corrupting the log, which is the recoverable half of the trade.
    #
    # The cap is lowered rather than reached: every turn started here costs a
    # synchronous append, so opening the real ceiling's worth of them spends
    # thousands of disk syncs to assert a property that a handful demonstrates.
    monkeypatch.setattr(emit, "_MAX_LIVE_TURNS", 6)
    _open_session()
    for n in range(emit._MAX_LIVE_TURNS + 5):
        emit.on_turn_started(SESSION, n + 1, "user")
    assert emit.flush()
    assert len(emit._live) == emit._MAX_LIVE_TURNS + 5, "a live turn was evicted"


# --- the flag ------------------------------------------------------------


def test_a_tool_call_records_a_digest_of_its_arguments_never_the_arguments():
    """A hash and a size answer real questions; the bytes would be a liability.

    "Did this call run the same command as the last one" and "how big was this
    payload" are both answerable from a digest. Keeping the payload itself would
    make the crew log a place shell arguments and file contents accumulate.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(
        SESSION, 1, name="execute_bash", call_id="tc-1", args='{"command": "ls /tmp"}'
    )
    assert emit.flush()

    entry = next(e for e in _body() if e["type"] == "tool/called")
    assert entry["data"]["args_hash"] == hashlib.sha256(b'{"command": "ls /tmp"}').hexdigest()
    assert entry["data"]["args_bytes"] == 22
    assert "ls /tmp" not in json.dumps(entry), "the arguments themselves reached the file"


def test_a_repeated_call_is_recognisable_by_its_digest():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="a", args='{"path": "x"}')
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="b", args='{"path": "x"}')
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="c", args='{"path": "y"}')
    assert emit.flush()

    hashes = [e["data"]["args_hash"] for e in _body() if e["type"] == "tool/called"]
    assert hashes[0] == hashes[1] != hashes[2]


def test_a_call_with_no_arguments_records_no_digest_fields():
    # -1 would be a size, and 0 is a real size. Absent is the honest answer for
    # "nothing was handed over to digest".
    _open_session()
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert emit.flush()

    data = next(e for e in _body() if e["type"] == "tool/called")["data"]
    assert "args_hash" not in data and "args_bytes" not in data


def test_a_tool_completion_records_the_error_flag_and_a_result_digest():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(
        SESSION, 1, call_id="tc-1", status="refused", is_error=True, result="denied by policy"
    )
    assert emit.flush()

    data = next(e for e in _body() if e["type"] == "tool/completed")["data"]
    assert data["is_error"] is True
    assert data["result_hash"] == hashlib.sha256(b"denied by policy").hexdigest()
    assert data["result_bytes"] == 16


def _tool_result_event(output: str | None):
    from kiro_crew.acp._dispatch import parse_session_update
    from kiro_crew.acp.types import EVENT_TOOL_RESULT

    raw_output = {"items": [] if output is None else [{"Text": output}]}
    events = parse_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-measured",
            "status": "completed",
            "rawOutput": raw_output,
        }
    )
    results = [event for event in events if event.kind == EVENT_TOOL_RESULT]
    assert len(results) == 1
    return results[0]


def _record_tool_result_event(event, *, call_id: str = "tc-measured") -> dict:
    emit.on_tool_completed(
        SESSION,
        1,
        call_id=call_id,
        status=event.tool_status,
        result=event.tool_output,
        result_digest=event.tool_output_digest,
        result_bytes=event.tool_output_bytes,
    )
    assert emit.flush()
    return [e["data"] for e in _body() if e["type"] == "tool/completed"][-1]


def test_a_long_result_records_the_full_redacted_output_measurement():
    from kiro_crew import session_directive

    full_output = "x" * session_directive.MAX_TOOL_RESULT_CHARS + "distinct suffix"
    event = _tool_result_event(full_output)
    displayed = full_output[: session_directive.MAX_TOOL_RESULT_CHARS]
    assert event.tool_output == displayed, "the display truncation changed"

    _open_session()
    data = _record_tool_result_event(event)

    assert data["result_bytes"] == len(
        full_output.encode("utf-8")
    ), "the crew log measured the displayed prefix instead of the full redacted output"
    assert data["result_hash"] == hashlib.sha256(full_output.encode("utf-8")).hexdigest()
    assert (
        data["result_hash"] != hashlib.sha256(displayed.encode("utf-8")).hexdigest()
    ), "the crew log hashed the displayed prefix instead of the full redacted output"


def test_equal_display_prefixes_record_different_full_result_hashes():
    from kiro_crew import session_directive

    prefix = "x" * session_directive.MAX_TOOL_RESULT_CHARS
    _open_session()
    first = _record_tool_result_event(_tool_result_event(prefix + "a"), call_id="tc-a")
    second = _record_tool_result_event(_tool_result_event(prefix + "b"), call_id="tc-b")

    assert (
        first["result_hash"] != second["result_hash"]
    ), "two full outputs with the same displayed prefix recorded the same hash"


def test_a_status_only_terminal_frame_records_no_result_measurement():
    event = _tool_result_event(None)
    assert event.tool_output_bytes == -1, "a status-only frame invented a byte count"
    assert event.tool_output_digest == "", "a status-only frame invented a digest"

    _open_session()
    data = _record_tool_result_event(event)

    assert "result_bytes" not in data, "a status-only terminal frame recorded a false byte count"
    assert "result_hash" not in data, "a status-only terminal frame recorded a false hash"


def test_chat_runner_forwards_the_full_result_measurement_to_the_emitter():
    import ast

    from kiro_crew.dashboard import chat_runner

    tree = ast.parse(inspect.getsource(chat_runner))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "on_tool_completed"
    ]
    assert len(calls) == 1
    keywords = {kw.arg: ast.unparse(kw.value) for kw in calls[0].keywords if kw.arg}
    assert (
        keywords.get("result_digest") == "event.tool_output_digest"
    ), "chat_runner dropped the full result digest"
    assert (
        keywords.get("result_bytes") == "event.tool_output_bytes"
    ), "chat_runner dropped the full result byte count"


def test_an_unstated_error_flag_is_left_off_rather_than_recorded_as_success():
    # Tri-state on purpose: "nobody said" is not the same claim as "it worked".
    _open_session()
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 1, call_id="tc-1", status="completed")
    assert emit.flush()

    data = next(e for e in _body() if e["type"] == "tool/completed")["data"]
    assert "is_error" not in data


def test_a_rerun_of_the_same_turn_ordinal_is_distinguishable_by_its_attempt():
    """Regenerate and rewind reuse a turn ordinal the log already named.

    Without a discriminator two starts at the same ordinal are indistinguishable,
    so a fold cannot tell a deliberate rerun from a duplicate write -- and those
    call for opposite handling.
    """
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_turn_completed(SESSION, 4, stop_reason="end_turn")
    emit.on_turn_started(SESSION, 4, "user", attempt=2)
    emit.on_turn_completed(SESSION, 4, stop_reason="end_turn")
    emit.on_turn_started(SESSION, 4, "user", attempt=3)
    assert emit.flush()

    starts = [e["data"] for e in _body() if e["type"] == "turn/started"]
    assert [d["turn"] for d in starts] == [4, 4, 4]
    assert "attempt" not in starts[0], "attempt 1 is every turn and must cost no field"
    assert starts[1]["attempt"] == 2
    assert starts[2]["attempt"] == 3


def test_a_turn_start_carries_the_message_it_answers_when_the_caller_knows_it():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user", message_seq=7)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_turn_started(SESSION, 2, "user")
    assert emit.flush()

    starts = [e["data"] for e in _body() if e["type"] == "turn/started"]
    assert starts[0]["message_seq"] == 7
    assert "message_seq" not in starts[1], "0 is not a seq and must not be recorded as one"


def test_flag_off_writes_no_file_at_all(monkeypatch):
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    assert emit.enabled() is False
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_session_closed(SESSION, "reset")
    assert not _store_root().exists()


def test_flag_unset_writes_no_file_at_all(monkeypatch):
    monkeypatch.delenv(emit.CREW_LOG_ENV, raising=False)
    assert emit.enabled() is False
    _open_session()
    assert not _store_root().exists()


def test_flag_off_allocates_no_state_either(monkeypatch):
    """Off means off: no file AND nothing held in memory.

    Writing is guarded by the entry points, but live-turn state is allocated
    before the write is handed over, so an unguarded allocation would accumulate a
    record per turn on every gateway running with the emitter off -- invisible,
    because no file appears to give it away.
    """
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    assert emit.enabled() is False

    for turn in range(1, 6):
        emit.on_session_opened(SESSION, agent="kirocrew")
        emit.on_turn_started(SESSION, turn, "user")
        emit.on_tool_called(SESSION, turn, name="fs_read", call_id=f"tc-{turn}")

    assert emit._live == {}, f"live-turn state allocated with the flag off: {emit._live}"
    assert emit._pinned == {}, f"pins allocated with the flag off: {emit._pinned}"
    assert emit._open == {}
    assert emit._tool_started == {}
    assert emit.buffered_writes() == 0
    assert not _store_root().exists()


def test_a_flag_turned_off_mid_turn_still_releases_what_it_allocated(monkeypatch):
    # The release paths stay unguarded for exactly this: state allocated while the
    # flag was on must not be stranded by turning it off.
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert (SESSION, 1) in emit._live

    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert (SESSION, 1) not in emit._live
    assert SESSION not in emit._pinned


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_the_flag_accepts_the_repo_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv(emit.CREW_LOG_ENV, value)
    assert emit.enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "2"])
def test_the_flag_rejects_everything_else(monkeypatch, value):
    monkeypatch.setenv(emit.CREW_LOG_ENV, value)
    assert emit.enabled() is False


def test_the_flag_is_read_per_call_not_at_import(monkeypatch):
    monkeypatch.setenv(emit.CREW_LOG_ENV, "0")
    _open_session()
    assert not _store_root().exists()
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _open_session()
    assert _log_path().is_file()


# --- off the event loop ----------------------------------------------------


def _spy_on_append(monkeypatch, seen: list[int]) -> None:
    """Record which thread each storage append actually runs on."""
    original = lg.CrewLog.append

    def _spy(self, *args, **kwargs):
        seen.append(threading.get_ident())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _spy)


def test_an_append_never_runs_on_the_event_loop_thread(monkeypatch):
    """Why this module queues at all.

    The storage call takes the unit's lock, reads a bounded tail to assign seq
    and fsyncs the line. On the gateway that call site is inside the async chat
    path, so running it inline blocks the one loop that drives every session's
    turns and the liveness heartbeat.
    """
    _open_session()
    assert emit.flush()
    seen: list[int] = []
    _spy_on_append(monkeypatch, seen)

    async def _turn() -> int:
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        assert emit.flush()
        return threading.get_ident()

    loop_thread = asyncio.run(_turn())
    assert seen, "no append was observed"
    assert loop_thread not in seen


def test_a_write_from_a_thread_with_no_loop_runs_inline(monkeypatch):
    """There is no loop to keep the call off, so it is not deferred.

    What makes a synchronous caller -- and this file -- deterministic: the entry
    point returns with the entry already on disk.
    """
    _open_session()
    seen: list[int] = []
    _spy_on_append(monkeypatch, seen)
    emit.on_turn_started(SESSION, 1, "user")
    assert seen == [threading.get_ident()]


def test_a_turn_boundary_crossing_a_full_queue_cannot_mis_attribute_an_entry():
    """The property the carried identity buys.

    Two turns' entries sit in the queue at once and the writer drains them after
    both turns have already started on the loop. Each entry still names the turn
    that produced it, because it was built with that ordinal in it -- there is no
    shared cell for a later turn to overwrite and no cache entry to evict.
    """
    _open_session()
    assert emit.flush()

    async def _two_turns() -> None:
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
        emit.on_turn_started(SESSION, 2, "user")
        emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-2")
        emit.on_turn_completed(SESSION, 2, stop_reason="end_turn")
        assert emit.flush()

    asyncio.run(_two_turns())
    body = _body()
    assert [(e["type"], e["data"]["turn"]) for e in body if e["type"] != "session/opened"] == [
        ("turn/started", 1),
        ("tool/called", 1),
        ("turn/completed", 1),
        ("turn/started", 2),
        ("tool/called", 2),
        ("turn/completed", 2),
    ]
    assert all("thread" not in e for e in body)


def test_a_slow_writer_costs_memory_not_a_record_and_not_the_loop(monkeypatch):
    """Both halves of the rule at once, which is why they are one test.

    A hole in an append-only log is permanent and silent -- a reader cannot tell a
    turn that never completed from one whose completion was discarded -- so no
    record is dropped. And a turn must not wait on the filesystem, so the producer
    neither writes nor waits. What gives instead is memory, which is visible.
    """
    _open_session()
    assert emit.flush()
    original = lg.CrewLog.append
    release = threading.Event()

    def _held(self, *args, **kwargs):
        # The writer is stuck for the whole burst, so this measures the producer
        # alone rather than a race with a writer that might be keeping up.
        assert release.wait(20.0)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _held)

    async def _flood() -> None:
        started = time.monotonic()
        for n in range(40):
            emit.on_tool_called(SESSION, 1, name="fs_read", call_id=f"tc-{n}")
        elapsed = time.monotonic() - started
        # The writer holds the very first append for 20s. A producer that waited
        # on it, or wrote for itself, could not get through 40 emits in under a
        # second.
        assert elapsed < 1.0, f"the producer blocked for {elapsed:.2f}s"
        assert emit.buffered_writes() > 0, "nothing was buffered -- the writer kept up"
        release.set()
        assert emit.flush(timeout=20.0)

    asyncio.run(_flood())

    assert emit.dropped_writes() == 0
    calls = [e for e in _body() if e["type"] == "tool/called"]
    assert len(calls) == 40, f"only {len(calls)} of 40 entries reached the file"
    assert [e["data"]["call_id"] for e in calls] == [f"tc-{n}" for n in range(40)]
    assert [e["data"]["call_index"] for e in calls] == list(range(1, 41))
    assert [e["seq"] for e in calls] == sorted(e["seq"] for e in calls)


def test_the_backlog_is_reported_rather_than_shed(monkeypatch, caplog):
    """The high-water mark is a report, not a cap.

    Crossing it must not shed anything -- that is what the durability half of the
    rule forbids -- so the only correct response is to say so.
    """
    _open_session()
    assert emit.flush()
    monkeypatch.setattr(emit, "_PENDING_HIGH_WATER", 4)
    original = lg.CrewLog.append
    release = threading.Event()

    def _held(self, *args, **kwargs):
        assert release.wait(20.0)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _held)

    async def _flood() -> None:
        for n in range(12):
            emit.on_tool_called(SESSION, 1, name="fs_read", call_id=f"tc-{n}")
        release.set()
        assert emit.flush(timeout=20.0)

    with caplog.at_level(logging.WARNING):
        asyncio.run(_flood())

    assert "buffered" in caplog.text and "rather than" in caplog.text
    assert emit.peak_buffered_writes() >= 4
    assert emit.dropped_writes() == 0
    assert [e["type"] for e in _body()].count("tool/called") == 12


def test_shutdown_drains_what_the_writer_has_not_written_yet():
    """The quiescence barrier a restart needs.

    Entries live in memory until the writer takes them, so a process that exits
    without draining loses the last thing each session did -- which is precisely
    what a reader goes looking for after a restart.
    """
    _open_session()
    assert emit.flush()

    async def _turn() -> None:
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
        # No flush: this is the state a restart interrupts.
        assert emit.buffered_writes() > 0 or emit._draining

    asyncio.run(_turn())

    assert emit.drain_for_shutdown(timeout=20.0)
    assert emit.buffered_writes() == 0
    types = [e["type"] for e in _body()]
    assert types == ["session/opened", "turn/started", "tool/called", "turn/completed"]


def test_shutdown_writes_spent_retry_loss_without_a_later_append():
    _open_session()
    assert emit.flush()
    before = len(_body())
    _leave_spent_retry_loss_owed(nbytes=29)

    assert emit.drain_for_shutdown(
        timeout=20.0
    ), "shutdown returned without writing the spent-budget loss marker"
    recovered = _body()[before:]
    assert [entry["type"] for entry in recovered] == [
        "write/dropped"
    ], f"shutdown left the loss marker owed: {[e['type'] for e in recovered]}"
    assert recovered[0]["data"] == {"dropped_count": 1, "dropped_bytes": 29}


def test_shutdown_reports_owed_loss_when_its_marker_cannot_land(monkeypatch, caplog):
    """A shutdown whose retry budget is spent folds the debt forward and names it.

    The split asserted below -- nothing buffered, one marker owed -- is the state a
    SPENT budget leaves. Reaching it on the real budget of six takes six PACED
    attempts inside *timeout*: `_drain_inline_until` sleeps a slice of what is left
    between passes, so how many attempts a window buys is a property of the host
    rather than of the emitter. A Linux runner fits six. The macOS runner fits fewer,
    and there the same assertion reads a DIFFERENT lifecycle point -- the marker job
    still retained, its debt riding inside that job -- which is how one assertion
    reddens a shard for changes that never touch this code. Pinning the budget to one
    attempt settles which point is reached: the first failed append spends it on any
    host.

    Two other repairs are weaker. A longer timeout buys margin on a fast host and
    loses it again on a slow one, leaving the assertion resting on the same
    stopwatch. Asserting that the append is reported as EITHER buffered or owed holds
    in both states, and one of those states prints `0 loss marker(s) owed` while a
    marker is genuinely owed, so the weaker form agrees with a wrong number instead
    of describing a state.

    That count belongs to the code that reads it rather than to this test: the debt
    travels inside the retained job, so a count taken from `_pending_loss` alone
    reads zero while a marker waits. What is pinned here is the spent budget, which
    is the state this test names.

    Mutation guard: removing the pin makes the assertion host-dependent again. It
    still passes on a host that fits six paced attempts, which is why the failure
    surfaces only on the slower runner.
    """
    _open_session()
    assert emit.flush()
    _leave_spent_retry_loss_owed()

    def _fail_marker(self, *args, **kwargs):
        raise OSError("filesystem still unavailable")

    monkeypatch.setattr(lg.CrewLog, "append", _fail_marker)
    # Patched AFTER the helper above, which spends a whole budget of its own.
    monkeypatch.setattr(emit, "_MAX_WRITE_ATTEMPTS", 1)
    with caplog.at_level(logging.WARNING, logger=emit.logger.name):
        drained = emit.drain_for_shutdown(timeout=0.5)

    assert drained is False, "shutdown reported success with 1 loss marker still owed"
    assert "0 append(s) buffered, 1 loss marker(s) owed" in caplog.text, (
        "the warning did not describe a spent budget, so the drain stopped at a "
        f"different lifecycle point: {caplog.text}"
    )
    with emit._lock:
        loss = emit._pending_loss.get(SESSION)
        assert loss is not None, "the failed marker's debt disappeared"
        assert loss.dropped_count == 1, "the original counted loss was not folded forward"


def test_shutdown_names_a_marker_still_waiting_to_be_retried(monkeypatch, caplog):
    """A marker held for retry is owed, and the warning has to say so.

    The other lifecycle point. A budget that is not spent leaves the marker JOB at
    the front of its session's bucket, and the debt rides inside that job: the map
    the count reads is empty while a marker is very much owed. The warning is the
    only record of how short the log's tail is at exit, so a zero there tells an
    operator nothing is missing when something is.

    This is the state a bounded shutdown reaches whenever the retry budget outlasts
    the window, which is a property of the host. Constructed here instead of raced
    for: a budget of 10,000 attempts cannot be spent, so the marker is retained
    however many passes the window buys.

    Mutation guard: counting only ``_pending_loss`` reports ``0 loss marker(s)
    owed`` and reddens the assertion below.
    """
    _open_session()
    assert emit.flush()
    _leave_spent_retry_loss_owed()

    def _fail_marker(self, *args, **kwargs):
        raise OSError("filesystem still unavailable")

    monkeypatch.setattr(lg.CrewLog, "append", _fail_marker)
    monkeypatch.setattr(emit, "_MAX_WRITE_ATTEMPTS", 10_000)
    # Both windows bounded to a hair: the point is which state the warning
    # describes, not how long the drain spins before describing it.
    monkeypatch.setattr(emit, "_SECOND_CHANCE_DRAIN_SECONDS", 0.01)
    with caplog.at_level(logging.WARNING, logger=emit.logger.name):
        drained = emit.drain_for_shutdown(timeout=0.01)

    assert drained is False, "shutdown reported success with a marker still retained"
    with emit._lock:
        assert emit._pending_loss == {}, "the debt is meant to be riding in the retained job"
        jobs = emit._pending.get(SESSION) or []
        assert jobs and jobs[0].loss is not None, "the marker job was not retained"
        assert jobs[0].loss.dropped_count == 1, "the retained marker lost its count"
    assert "1 loss marker(s) owed" in caplog.text


def test_a_close_under_a_live_turn_leaves_that_turn_its_open_calls():
    """A teardown may not erase state the running turn still needs.

    A forced reset tears a session down while a turn is running. The teardown's
    cleanup drops only what a successor must not inherit, and of the open tool calls
    only those whose turn is already gone -- because the running turn closes its own
    calls later, from its `finally`, and a cleanup that took them would leave them
    open for the life of the file.

    The terminal itself is written at once and late entries may follow it; see
    `on_session_closed` for why holding it was abandoned. What is asserted here is
    that nothing the live turn needs was taken away.

    Mutation guard: dropping every `_tool_started` key for the session, as before,
    makes `close_open_tool_calls` return 0 and reddens this.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_session_closed(SESSION, "reset")
    assert emit.flush()

    types = [e["type"] for e in _body()]
    assert "session/closed" in types, "the terminal is written, not held"

    # The turn ends the way the runner ends one, after its session already closed.
    assert emit.close_open_tool_calls(SESSION, 1) == 1, "the live turn lost its open call"
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()

    after = [e["type"] for e in _body()]
    assert after.index("tool/completed") > after.index("session/closed")
    assert after.index("tool/completed") < after.index("turn/completed")


def test_a_close_with_no_live_turn_is_written_at_once():
    """The deferral is for a live turn only, not a general delay.

    Every ordinary teardown -- an idle session evicted, a shutdown, a reset between
    turns -- has nothing to wait for, and holding its terminal would leave the log
    without one.
    """
    _open_session()
    assert emit.flush()
    emit.on_session_closed(SESSION, "shutdown")
    assert emit.flush()
    assert _body()[-1]["type"] == "session/closed"
    assert _body()[-1]["data"]["reason"] == "shutdown"


def test_shutdown_drains_even_when_the_writer_pool_is_gone():
    """A shutdown that already tore the pool down must still not lose records.

    The executor registers its own exit hook, so the pool can be gone before this
    runs. Blocking the caller here is correct -- it is the exit path -- and it is
    the only remaining way to keep the entries.

    The state is CONSTRUCTED rather than hoped for. What a pool shutdown cancels is
    a QUEUED future, so the single writer thread is occupied first to make the drain
    pass queue behind it; whether the pass had already started is precisely the
    timing this test must not depend on. The cancelled pass never runs, so nothing
    releases the writer claim -- and reading that claim as "a batch is in flight"
    wedges every later barrier for the life of the process: the inline write is
    refused on the belief another thread holds the batch, and ``flush`` answers
    False forever. So the claim is derived from the future, not from a flag nothing
    will clear.
    """
    _open_session()
    assert emit.flush()

    occupied = threading.Event()
    release = threading.Event()

    def _occupy() -> None:
        occupied.set()
        assert release.wait(20.0)

    executors.crew_log_executor().submit(_occupy)
    # Waited on the worker's OWN signal, so the queue state below is a fact rather
    # than a guess about scheduling.
    assert occupied.wait(20.0), "the writer thread was never occupied"

    async def _turn() -> None:
        emit.on_turn_started(SESSION, 2, "user")
        emit.on_turn_completed(SESSION, 2, stop_reason="end_turn")

    asyncio.run(_turn())
    assert emit.buffered_writes() == 2, "the entries were written instead of queueing"

    # Cancels the queued drain pass. The occupier is RUNNING, so it survives and is
    # released next -- only the pass that would have cleared the claim is lost.
    executors.shutdown_maintenance_executor()
    release.set()

    assert emit.drain_for_shutdown(timeout=20.0)
    assert emit.buffered_writes() == 0
    assert [e["type"] for e in _body()].count("turn/completed") == 1
    # The claim was released with the pass it was made for, so the next caller is
    # not answered False forever.
    assert emit.flush(timeout=20.0), "the writer claim outlived the pass it was made for"
    emit.on_turn_started(SESSION, 3, "user")
    assert emit.flush(timeout=20.0)
    assert [e["data"]["turn"] for e in _body() if e["type"] == "turn/started"] == [2, 3]


def test_the_dashboard_cleanup_hook_actually_drains_off_the_loop():
    """The barrier is only worth having if something calls it.

    The registered hook is INVOKED here rather than pattern-matched, so what is
    asserted is that it drains and that it does so off the event loop -- the two
    things that matter. A substring match on the source proves neither, and breaks
    on an honest rewrite of the same behaviour.
    """
    import asyncio as _asyncio

    source = inspect.getsource(server_module)
    assert (
        "app.on_cleanup.append(_crew_log_drain)" in source
    ), "the server no longer registers a crew log drain on cleanup"

    drained: list[str] = []

    def _record() -> bool:
        drained.append(threading.current_thread().name)
        return True

    async def _drive() -> None:
        loop_thread = threading.current_thread().name
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(emit, "drain_for_shutdown", _record)
            await _asyncio.to_thread(emit.drain_for_shutdown)
        assert drained, "the drain never ran"
        assert drained[0] != loop_thread, (
            "the drain ran ON the event loop thread; it blocks while the writer "
            "finishes, so a slow disk would stall everything else shutting down"
        )

    _asyncio.run(_drive())


def test_a_dropped_turn_start_leaves_its_turn_unthreaded_not_folded_into_the_last(
    monkeypatch,
):
    """The failure a single mutable anchor would have got wrong.

    A turn whose start was REFUSED has no seq to thread under. Its followers must
    carry no thread at all -- folding them into the PREVIOUS turn would attribute
    one turn's tool calls to another, which is worse than recording no grouping.

    A refusal is also the reason the followers still land. It is decided before a
    byte is written and would be decided the same way on every retry, so the entry
    is simply gone and the pass continues past it; holding the rest of the turn
    behind an entry that can never land would turn one refused line into a missing
    turn.
    """
    _open_session()
    assert emit.flush()

    async def _first_turn() -> None:
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        assert emit.flush()

    asyncio.run(_first_turn())
    first_start = next(e for e in _body() if e["type"] == "turn/started")

    original = lg.CrewLog.append

    def _refuse_turn_starts(self, entry_type, *args, **kwargs):
        if entry_type == "turn/started":
            raise lg.CrewLogError("too big", code=lg.CODE_ENTRY_TOO_LARGE)
        return original(self, entry_type, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _refuse_turn_starts)

    async def _second_turn() -> None:
        emit.on_turn_started(SESSION, 2, "user")
        emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-2")
        assert emit.flush(timeout=20.0)

    asyncio.run(_second_turn())
    orphan = [e for e in _body() if e["type"] == "tool/called" and e["data"]["turn"] == 2]
    assert len(orphan) == 1
    assert "thread" not in orphan[0]
    assert orphan[0].get("thread") != first_start["seq"]
    assert emit.dropped_writes() == 1, "the refused start is the only loss"


def test_a_log_failure_on_the_writer_never_reaches_the_caller(monkeypatch):
    """Fail-soft holds across the thread boundary too, and the loss is admitted.

    The job runs where nothing is awaiting it, so an exception that escaped would
    surface as an unretrieved future rather than at the call site -- worse than the
    inline case, not better. The entry is retried and then given up on, and
    ``dropped_writes`` is where that shows: a hole nobody can count is the failure
    this counter exists to rule out.
    """
    _open_session()
    assert emit.flush()

    def _boom(self, *args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(lg.CrewLog, "append", _boom)

    async def _turn() -> None:
        emit.on_turn_started(SESSION, 1, "user")
        assert not emit.flush(
            timeout=0.5
        ), "flush reported quiet while the failed filesystem still owed a loss marker"

    asyncio.run(_turn())
    with emit._drained:
        writer_counted_loss = emit._drained.wait_for(
            lambda: emit.dropped_writes() == 2 and emit.buffered_writes() == 0,
            timeout=20.0,
        )
    assert writer_counted_loss, "writer never finished counting the entry and failed marker"
    assert emit.dropped_writes() == 2, "the entry and its failed marker were not counted"
    assert emit.buffered_writes() == 0


def test_a_failed_append_is_retained_and_lands_on_the_next_pass(monkeypatch):
    """The hole a swallowed exception leaves.

    One transient filesystem error must cost latency, not a record: the batch goes
    back to the front of its session's bucket and the next pass writes it. The
    entries keep the order they were emitted in, and nothing is dropped -- a retry
    that reordered the file, or gave up on the first error, would each be worse
    than the delay.
    """
    _open_session()
    assert emit.flush()
    original = lg.CrewLog.append
    failures = {"left": 1}

    def _fail_once(self, *args, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("input/output error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _fail_once)

    async def _turn() -> None:
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
        assert emit.flush(timeout=20.0)

    asyncio.run(_turn())
    assert failures["left"] == 0, "the injected failure never fired"
    body = _body()
    assert [e["type"] for e in body] == [
        "session/opened",
        "turn/started",
        "tool/called",
        "turn/completed",
    ]
    assert [e["seq"] for e in body] == sorted(e["seq"] for e in body)
    assert emit.dropped_writes() == 0
    assert emit.buffered_writes() == 0


def test_a_later_write_during_retention_queues_behind_the_retained_batch(monkeypatch):
    """Why the retained batch goes to the FRONT of its session's bucket.

    While a session owes a retained batch, its inline fast path is off and every
    later write joins the same bucket behind it. Without that, the follower would
    reach the file first and the log would state an order that never happened --
    which a fold reads as fact, and cannot detect.
    """
    _open_session()
    assert emit.flush()
    original = lg.CrewLog.append
    refused = threading.Event()
    seen = {"starts": 0}

    def _fail_the_first_starts(self, entry_type, *args, **kwargs):
        if entry_type == "turn/started":
            seen["starts"] += 1
            if seen["starts"] <= 2:
                refused.set()
                raise OSError("input/output error")
        return original(self, entry_type, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _fail_the_first_starts)

    async def _turn() -> None:
        emit.on_turn_started(SESSION, 1, "user")
        # Emitted only once the start has actually been refused, so the follower is
        # produced INSIDE the retention window rather than racing it.
        assert refused.wait(20.0), "the start never reached the writer"
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        assert emit.flush(timeout=20.0)

    asyncio.run(_turn())
    assert seen["starts"] == 3, "the start was not retried twice before it landed"
    assert [e["type"] for e in _body()] == ["session/opened", "turn/started", "tool/called"]
    assert emit.dropped_writes() == 0


def test_a_reopen_that_fails_is_retried_rather_than_discarding_the_entry():
    """The same hole one layer up from the append.

    A handle can go missing from the bounded cache, and the next entry reopens the
    crew log. If that reopen error were swallowed the job would SUCCEED with nothing
    written -- no retry, and nothing in ``dropped_writes()`` either -- which is
    exactly the silent hole the retention exists to close.
    """
    _open_session()
    assert emit.flush()
    original = lg.CrewLog.open
    failures = {"left": 1}

    def _fail_first_open(kind, unit_id, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("input/output error")
        return original(kind, unit_id, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "open", _fail_first_open)
        with emit._lock:
            emit._open.clear()  # the eviction the cap would have made

        async def _turn() -> None:
            emit.on_turn_started(SESSION, 1, "user")
            assert emit.flush(timeout=20.0)

        asyncio.run(_turn())

    assert failures["left"] == 0, "the injected reopen failure never fired"
    assert [e["type"] for e in _body()] == ["session/opened", "turn/started"]
    assert emit.dropped_writes() == 0


def test_a_retried_session_open_still_writes_the_entry_it_owes():
    """A retry must not lose the decision the first attempt made.

    The job decides whether to announce from filesystem state it changes itself:
    when the header lands and the entry behind it does not, the retry finds the
    crew log already there and would read "nothing new to say" -- skipping
    ``session/opened`` for good, and not counting it either. The decision is
    latched on the first attempt for exactly that reason.
    """
    original = lg.CrewLog.append
    failures = {"left": 1}

    def _fail_the_first_open_entry(self, entry_type, *args, **kwargs):
        if entry_type == "session/opened" and failures["left"]:
            failures["left"] -= 1
            raise OSError("input/output error")
        return original(self, entry_type, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _fail_the_first_open_entry)
        _open_session()
        assert emit.flush(timeout=20.0)

    assert failures["left"] == 0, "the injected failure never fired"
    body = _body()
    assert [e["type"] for e in body] == ["session/opened"]
    assert body[0]["data"]["model"] == "claude-opus-5", "the entry kept its payload"
    assert emit.dropped_writes() == 0


def test_a_wedged_writer_gives_up_after_the_cap_so_a_bounded_drain_finishes(caplog):
    """Why there is a cap at all, and what it costs.

    Entries live in memory until they are written, so a filesystem that never
    answers would hold them forever and turn every bounded caller into a timeout --
    ``flush`` and ``drain_for_shutdown`` would both report failure while the writer
    retried a batch nothing could ever accept. So the batch is given up on, counted,
    and named once. A reported loss can be investigated; a hang cannot.
    """
    _open_session()
    assert emit.flush()

    def _wedged(self, *args, **kwargs):
        raise OSError("input/output error")

    async def _turn() -> None:
        emit.on_turn_started(SESSION, 3, "user")
        emit.on_tool_called(SESSION, 3, name="fs_read", call_id="tc-1")
        emit.on_turn_completed(SESSION, 3, stop_reason="end_turn")

    # A scoped patch, NOT this test's ``monkeypatch``: the autouse fixture pins
    # KIROCREW_HOME and the feature flag through that same object, so undoing it
    # here would send the rest of the test at the real data home with the emitter
    # switched off.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _wedged)
        with caplog.at_level(logging.WARNING, logger=emit.logger.name):
            asyncio.run(_turn())
            assert not emit.flush(
                timeout=0.5
            ), "flush reported quiet while the wedged filesystem still owed a loss marker"

        with emit._drained:
            writer_counted_loss = emit._drained.wait_for(
                lambda: emit.dropped_writes() == 4 and emit.buffered_writes() == 0,
                timeout=20.0,
            )
        assert writer_counted_loss, "writer never finished counting the batch and failed marker"
        assert emit.buffered_writes() == 0
        # At least the three entries and one failed marker pass. Not an exact
        # total: every drain pass that runs while the filesystem is wedged fails
        # the marker again and counts another loss, so the number reached by the
        # time this line runs is a property of how many passes the machine managed,
        # not of the emitter. The exact facts are asserted where they are exact --
        # the marker's own ``dropped_count`` in the file below, and that recovery
        # adds nothing further.
        assert emit.dropped_writes() >= 4, "the batch and its failed marker were not counted"
        assert not emit.drain_for_shutdown(
            timeout=0.5
        ), "shutdown reported quiet while the wedged filesystem still owed a loss marker"
        assert [e["type"] for e in _body()] == ["session/opened"]
        assert "gave up on" in caplog.text
        dropped_while_wedged = emit.dropped_writes()

    # Recovery first admits the loss into the file, then resumes ordinary
    # appends. The counter remains process-lifetime cumulative.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=emit.logger.name):
        emit.on_turn_completed(SESSION, 3, stop_reason="end_turn")
        assert emit.flush(timeout=20.0)
    assert "landing again" in caplog.text
    body = _body()
    assert [e["type"] for e in body] == [
        "session/opened",
        "write/dropped",
        "turn/completed",
    ]
    assert body[1]["data"]["dropped_count"] == 3
    assert emit.dropped_writes() == dropped_while_wedged, (
        "recovery counted a further loss: the marker landing and the appends after it "
        "are not losses"
    )


# --- provenance ------------------------------------------------------------


def test_a_typed_automation_banner_cannot_attribute_a_turn_to_automation():
    """The actor is structural or it is ``user``.

    A turn's actor is a claim a reader takes as fact, and the banner these
    injections wrap their text in is a string the user can type into the composer.
    So the runner reads the enqueue-time ``kind`` tag -- stamped by the producer,
    absent from anything a user writes -- and never the message.
    """
    from kiro_crew.dashboard.chat_runner import _actor_for_queue_items
    from kiro_crew.dashboard.chat_utils import (
        CRON_NOTIFICATION_KIND,
        SUBAGENT_COMPLETION_KIND,
    )
    from kiro_crew.dashboard.state import (
        CRON_NOTIFY_PREFIX,
        SUBAGENT_COMPLETION_PREFIXES,
    )

    typed = [
        {"content": f'{CRON_NOTIFY_PREFIX}"payroll"]\nrun it', "kind": ""},
        {"content": f"{SUBAGENT_COMPLETION_PREFIXES[0]} done", "kind": ""},
    ]
    for item in typed:
        assert _actor_for_queue_items([item]) == ""

    assert _actor_for_queue_items([{"content": "x", "kind": CRON_NOTIFICATION_KIND}]) == "cron"
    assert (
        _actor_for_queue_items([{"content": "x", "kind": SUBAGENT_COMPLETION_KIND}]) == "subagent"
    )


def test_a_recovery_is_a_second_turn_of_the_same_actor():
    """A stall or watchdog re-entry does not change who caused the turn.

    The recovery carries a fresh queue id and a kind that names no producer, so
    without the stamp its actor would fall back to the default and record an
    autonudge's retry as a user turn.
    """
    from kiro_crew.dashboard.chat_runner import TURN_ACTOR_META_KEY, _actor_for_queue_items
    from kiro_crew.dashboard.chat_utils import SYNTHETIC_RECOVERY_KIND

    recovery = [
        {
            "content": "replayed verbatim",
            "kind": SYNTHETIC_RECOVERY_KIND,
            "meta": {TURN_ACTOR_META_KEY: "autonudge"},
        }
    ]
    assert _actor_for_queue_items(recovery) == "autonudge"


def test_a_stamped_actor_outside_the_vocabulary_is_ignored():
    # The stamp is gateway-authored, but a value the emitter does not recognise
    # would be recorded as `other`, which says less than the honest default.
    from kiro_crew.dashboard.chat_runner import TURN_ACTOR_META_KEY, _actor_for_queue_items
    from kiro_crew.dashboard.chat_utils import SYNTHETIC_RECOVERY_KIND

    bogus = [{"content": "x", "kind": SYNTHETIC_RECOVERY_KIND, "meta": {TURN_ACTOR_META_KEY: "??"}}]
    assert _actor_for_queue_items(bogus) == ""


def test_a_queue_kind_beats_a_stamped_actor():
    # A cron notification that also carries a stamp is still a cron turn: the kind
    # is set by the producer minting the entry, the stamp by whoever retried one.
    from kiro_crew.dashboard.chat_runner import TURN_ACTOR_META_KEY, _actor_for_queue_items
    from kiro_crew.dashboard.chat_utils import CRON_NOTIFICATION_KIND

    both = [
        {
            "content": "x",
            "kind": CRON_NOTIFICATION_KIND,
            "meta": {TURN_ACTOR_META_KEY: "subagent"},
        }
    ]
    assert _actor_for_queue_items(both) == "cron"


# --- shutdown reports the truth about what landed -------------------------


def test_shutdown_is_not_reported_drained_while_a_batch_is_still_writing():
    """A claimed batch is out of the buffer but not yet on disk.

    The writer takes a batch OUT of `_pending` before it writes it, so an empty
    buffer says nothing about whether those entries reached the file. Reporting
    drained there is the worst answer available: the caller exits believing the
    record is complete, and the entries in that batch are the last thing the
    session did.
    """
    _open_session()
    assert emit.flush()
    holding = threading.Event()
    release = threading.Event()
    original = lg.CrewLog.append

    def _slow(self, *args, **kwargs):
        holding.set()
        release.wait(timeout=10.0)
        return original(self, *args, **kwargs)

    verdicts: list[bool] = []
    elapsed: list[float] = []

    async def _emit_then_shutdown() -> None:
        emit.on_turn_started(SESSION, 1, "user")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _slow)
        asyncio.run(_emit_then_shutdown())
        assert holding.wait(timeout=10.0), "the writer never claimed the batch"
        # Mid-batch: the buffer is empty, the batch is in flight. A short budget
        # must NOT come back True.
        began = time.monotonic()
        verdicts.append(emit.drain_for_shutdown(timeout=0.2))
        elapsed.append(time.monotonic() - began)
        release.set()
    assert emit.drain_for_shutdown(timeout=10.0) is True
    assert verdicts == [False], "shutdown claimed drained while a batch was in flight"
    # Bounded, not retried: reaching the fallback means the timed wait already
    # failed, so the writer is wedged rather than slow and looping there would
    # spend an unbounded part of the exit on a filesystem that stopped answering.
    assert elapsed[0] < 5.0, f"the failed drain took {elapsed[0]:.1f}s -- it is retrying"
    assert [e["type"] for e in _body() if e["type"] == "turn/started"]


# --- attempts at one turn ordinal -----------------------------------------


def test_rerunning_one_ordinal_increments_the_attempt():
    # A regenerate or a rewind reruns a turn the ordinal already names. Without
    # the discriminator the two starts are identical lines and a fold cannot tell
    # a retry from a duplicate write.
    _open_session()
    for _ in range(3):
        emit.on_turn_started(SESSION, 7, "user")
    assert emit.flush()
    starts = [e["data"] for e in _body() if e["type"] == "turn/started"]
    assert [d.get("attempt", 1) for d in starts] == [1, 2, 3]


def test_the_first_attempt_carries_no_field():
    # The common case is a turn that was never rerun; it should not pay a field.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    assert "attempt" not in [e for e in _body() if e["type"] == "turn/started"][-1]["data"]


def test_attempts_are_counted_per_ordinal_not_per_session():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    starts = [
        (e["data"]["turn"], e["data"].get("attempt", 1))
        for e in _body()
        if e["type"] == "turn/started"
    ]
    assert starts == [(1, 1), (2, 1), (1, 2)]


def test_a_restart_between_two_retries_still_increments():
    """The map cannot survive the restart that makes the collision possible.

    So it is rebuilt from the file on resume. Without that, a rerun after a
    restart writes attempt 1 at an ordinal the file already has an attempt 1 for
    -- exactly the collision the field exists to prevent.
    """
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_turn_started(SESSION, 4, "user")
    assert emit.flush()
    # A new gateway process: caches gone, the file is all that is left.
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", resumed=True)
    assert emit.flush()
    emit.on_turn_started(SESSION, 4, "user")
    assert emit.flush()
    attempts = [e["data"].get("attempt", 1) for e in _body() if e["type"] == "turn/started"]
    assert attempts == [1, 2, 3], f"a restart reset the attempt count: {attempts}"


def test_an_explicit_attempt_overrides_the_count():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user", attempt=9)
    assert emit.flush()
    assert [e for e in _body() if e["type"] == "turn/started"][-1]["data"]["attempt"] == 9


# --- a closer states only what the site observed --------------------------


def test_a_turn_end_closer_does_not_call_an_unfinalised_tool_completed():
    """At turn end nothing observed the tool's outcome.

    The stream may have raised, the process may have died, or the call may simply
    never have been finalised. `completed` there states a success no site saw, in a
    file nothing rewrites, so the default is `unknown` -- the same word
    `repair_interrupted_turn` writes for an unmatched `tool/called`, which is the
    identical claim reached from the file instead of from memory.

    `is_error` stays absent rather than being taken from the turn's own failure: a
    turn that raised says so in its own terminal, and stamping the TOOL as errored
    would be a second unobserved claim.

    Mutation guard: restoring `completed` as the default reddens this.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_write", call_id="c1")
    assert emit.close_open_tool_calls(SESSION, 1) == 1
    emit.on_turn_failed(SESSION, 1, error="RuntimeError")
    assert emit.flush()

    done = [e["data"] for e in _body() if e["type"] == "tool/completed"]
    assert len(done) == 1
    assert done[0]["status"] == "unknown", "an unobserved outcome was recorded as success"
    assert "is_error" not in done[0], "the tool was not observed to fail either"


# --- an outputless tool still gets its closer -----------------------------


def test_a_tool_that_produced_no_output_is_still_closed():
    """A tool with no output sends no result frame at all.

    So `tool_final` never arrives and the completion path never runs. Its
    `tool/called` would stay open for the life of the file, and a fold counting
    open calls would report a turn that never finished using a tool it had in
    fact finished with.

    `completed` is the caller's claim, not the default: the tool-group boundary
    passes it because the model went on to produce text, which means the tools it
    was waiting on finished.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="fs_write", call_id="c1")
    assert emit.close_open_tool_calls(SESSION, 1, status="completed") == 1
    assert emit.flush()
    done = [e["data"] for e in _body() if e["type"] == "tool/completed"]
    assert len(done) == 1
    assert done[0]["call_id"] == "c1"
    assert done[0]["status"] == "completed"
    # Zero bytes, not an absent field: the tool genuinely produced none, which is
    # a different claim from "the payload was not recorded".
    assert done[0]["result_bytes"] == 0
    assert "result_hash" not in done[0]
    assert done[0]["call_index"] == 1
    assert done[0]["step"] == 1


def test_closing_open_calls_leaves_an_already_completed_one_alone():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    emit.on_tool_called(SESSION, 1, name="b", call_id="c2")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed", result="out")
    assert emit.close_open_tool_calls(SESSION, 1) == 1
    assert emit.flush()
    done = [e["data"] for e in _body() if e["type"] == "tool/completed"]
    assert [d["call_id"] for d in done] == ["c1", "c2"]
    assert done[0]["result_bytes"] == 3
    assert done[1]["result_bytes"] == 0


def test_closing_open_calls_is_a_no_op_when_none_are_open():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    before = len(_body())
    assert emit.close_open_tool_calls(SESSION, 1) == 0
    assert emit.flush()
    assert len(_body()) == before


def test_a_turn_never_completes_with_an_open_call_inside_it():
    # The shape the interrupted-turn repair exists to fix. A LIVE turn must not
    # produce it in the first place.
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="a", call_id="c1")
    emit.close_open_tool_calls(SESSION, 1)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()
    kinds = [e["type"] for e in _body()]
    assert kinds.index("tool/completed") < kinds.index("turn/completed")


# --- every entry matches the shape its type documents --------------------


def test_every_emitted_type_matches_the_documented_shape():
    """The spec's data-shape table and the writer must not drift.

    One test over every family this module emits, rather than one assertion per
    field scattered across the file: a row that stops matching the writer is how a
    reader ends up trusting a field that is not there.
    """
    required = {
        "session/opened": {"agent", "slot", "model", "cwd", "owner", "resumed"},
        "session/closed": {"reason"},
        "turn/started": {"turn", "actor", "depth"},
        "turn/refused": {"turn", "actor", "reason", "depth"},
        "turn/completed": {"turn", "depth", "stop_reason", "duration_ms", "credits", "tokens"},
        "step/started": {"turn", "step"},
        "step/completed": {"turn", "step", "ms"},
        "tool/called": {"turn", "call_id", "name", "server", "kind"},
        "tool/completed": {"turn", "call_id", "name", "server", "status"},
        "message/received": {"turn", "role", "text", "source"},
        "message/sent": {"turn"},
        "message/chunk": {"turn", "delta"},
        "message/queued": {"source", "bytes", "queued_seq"},
        "request/configured": {"turn", "model", "provider", "context_window"},
        "context/composed": {"turn", "sources", "chars", "tokens", "tokens_estimated"},
        "model/selected": {"model", "source"},
        "compaction/applied": {"pct_before", "pct_after", "freed_pct"},
        "approval/requested": {"turn", "approval_id", "tool"},
        "approval/decided": {"turn", "approval_id", "decision"},
        "plan/updated": {"turn", "items"},
        "background/completed": {"kind"},
        "subagent/spawned": {"turn", "agent_id"},
        "subagent/steered": {"agent_id"},
        "subagent/completed": {"agent_id"},
        "subagent/failed": {"agent_id"},
    }
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    step = emit.on_step_started(SESSION, 1)
    emit.on_request_configured(SESSION, 1, model="m", provider="acp", context_window=9)
    emit.on_context_composed(SESSION, 1, step=step, blocks={"memory": 40})
    emit.on_message_received(SESSION, 1, text="hi", source="dashboard")
    emit.on_message_sent(SESSION, 1, step=step, text="there")
    emit.on_tool_called(SESSION, 1, name="t", call_id="c1", args="{}")
    emit.on_tool_completed(SESSION, 1, call_id="c1", status="completed", result="out")
    emit.on_step_completed(SESSION, 1, step, ms=5)
    emit.on_model_selected(SESSION, "m", "fallback", turn=1)
    emit.on_compaction_applied(SESSION, pct_before=0.8, pct_after=0.4)
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="shell", reason="ls")
    emit.on_approval_decided(SESSION, 1, approval_id="r1", decision="approved")
    emit.on_plan_updated(SESSION, 1, items=[{"id": "a", "text": "t", "completed": False}])
    emit.on_subagent_spawned(SESSION, 1, agent_id="ab12", agent="kirocrew", scope={"memory": True})
    emit.on_subagent_steered(SESSION, agent_id="ab12", mode="interrupt")
    emit.on_subagent_completed(SESSION, agent_id="ab12", duration_ms=7)
    emit.on_subagent_failed(SESSION, agent_id="cd34", reason="boom", outcome="failed")
    emit.on_background_completed(SESSION, kind="title", model="m", credits=0.1)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_message_queued(SESSION, source="slack", size_bytes=3, queued_seq="q1")
    emit.on_turn_refused(SESSION, 2, "not_authorized")
    emit.on_session_closed(SESSION, "reset")
    emit.on_message_sent(SESSION, 1, step=step, text="o" * (lg.MAX_ENTRY_BYTES + 200))
    assert emit.flush()
    seen: dict[str, int] = {}
    for entry in _body():
        kind = entry["type"]
        seen[kind] = seen.get(kind, 0) + 1
        assert kind in required, f"{kind} is emitted but has no documented shape here"
        missing = required[kind] - set(entry["data"])
        assert not missing, f"{kind} is missing documented field(s): {sorted(missing)}"
    unexercised = sorted(set(required) - set(seen))
    assert not unexercised, f"documented but not exercised: {unexercised}"


# --- shutdown finishes a batch that is inside its backoff -----------------


def test_the_shutdown_clamp_pulls_a_far_backoff_inside_the_deadline():
    """The clamp itself, with no clock and no threads.

    A retry schedule cannot outlive the process it is scheduled in: a batch parked
    beyond a bounded drain's deadline is not retried later, it is lost. So during a
    shutdown the ready-time is pulled back to within one slice of the remaining
    budget -- and CLAMPED, never collapsed, because spending every remaining attempt
    at once against a filesystem that needed a moment turns a delay into a
    guaranteed drop.

    Asserting the pure function is what makes this deterministic: the integration
    tests below prove the batch lands, and this proves the rule they depend on.
    """
    state = emit._RetryState(attempts=1, not_before=time.monotonic() + 3600.0)
    with emit._lock:
        # Outside a shutdown the schedule is its own.
        assert emit._backoff_ready_at_locked(state) == state.not_before

    with pytest.MonkeyPatch.context() as mp:
        started = time.monotonic()
        mp.setattr(emit, "_draining_for_shutdown", True)
        mp.setattr(emit, "_shutdown_started", started)
        mp.setattr(emit, "_shutdown_deadline", started + 2.0)
        with emit._lock:
            ready = emit._backoff_ready_at_locked(state)
        # Inside the budget, and by a slice rather than at the very end, so the
        # remaining attempts fit rather than expiring unused.
        assert ready < emit._shutdown_deadline
        assert ready <= time.monotonic() + (2.0 / emit._MAX_WRITE_ATTEMPTS) + 0.05
        assert ready < state.not_before


def test_shutdown_writes_a_batch_that_had_been_failing():
    """A retained batch is written at shutdown, not abandoned.

    Event-driven: the store refuses until the test says otherwise, and the assertion
    is on the drain's own answer plus the file, never on how long anything took. The
    fixture's flattened backoff keeps this to the mechanism under test -- the clamp
    that makes a LONG backoff reachable is pinned above, without threads.
    """
    allow = threading.Event()
    real_append = lg.CrewLog.append

    def _gated(self, *a, **kw):
        if not allow.is_set():
            raise OSError("store is refusing")
        return real_append(self, *a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        _open_session()
        assert emit.flush()
        # A backoff long enough that the batch is still PARKED when the assertions
        # below run: with the fixture's flattened schedule it would burn its whole
        # attempt budget in microseconds and be dropped before the drain is asked.
        mp.setattr(emit, "_retry_delay", lambda _attempts: 5.0)
        mp.setattr(lg.CrewLog, "append", _gated)
        emit.on_turn_started(SESSION, 1, "user")
        # Refused, so the batch is retained rather than written. The wait only has to
        # be shorter than that backoff, which no machine speed changes.
        assert not emit.flush(timeout=0.5), "the gated store did not retain a batch"
        assert emit.buffered_writes() >= 1

        allow.set()
        assert emit.drain_for_shutdown(timeout=20.0), "shutdown abandoned a retained batch"

    assert "turn/started" in [e["type"] for e in _body()]
    assert emit.dropped_writes() == 0, "a batch that could be written was counted lost"


def test_a_shutdown_wakes_a_writer_parked_on_a_backoff():
    """The clamp is useless if the thread that honours it never rereads it.

    The writer computes its inter-pass pause BEFORE a shutdown is requested, so a
    plain sleep would hold it for the whole backoff no matter what the deadline
    became. This drives that directly: park the writer on a long backoff, then ask
    for a shutdown and wait on the writer's OWN next pass rather than on a clock.
    """
    passes = threading.Event()
    real_drain_once = emit._drain_once

    def _spy(deferred_loss: set[str] | None = None) -> set[str]:
        passes.set()
        return real_drain_once(deferred_loss)

    real_append = lg.CrewLog.append
    calls = {"n": 0}

    def _fail_first(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real_append(self, *a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        _open_session()
        assert emit.flush()
        mp.setattr(emit, "_retry_delay", lambda _attempts: 3600.0)
        mp.setattr(lg.CrewLog, "append", _fail_first)
        emit.on_turn_started(SESSION, 1, "user")
        assert not emit.flush(timeout=2.0), "the injected failure did not park the writer"
        passes.clear()
        mp.setattr(emit, "_drain_once", _spy)

        assert emit.drain_for_shutdown(timeout=20.0), "the parked writer never woke"

    assert passes.is_set(), "no pass ran after the shutdown asked for one"
    assert "turn/started" in [e["type"] for e in _body()]


# --- a hung write is bounded by a ceiling, not by the attempt counter ------


def test_a_hung_write_grows_the_buffer_and_sheds_nothing(caplog):
    """A write that HANGS never advances the attempt counter that bounds a failure.

    Every other loss here is bounded by attempts: an append raises, the batch is
    retained, and a fixed number of failed passes drops it. A call that never returns
    and never raises reaches none of that, so the buffer grows for as long as
    producers keep appending.

    It is allowed to grow. This log's holes may not be VOLUNTARY: several subsystems
    read it to decide what happened, and an entry discarded while the process is
    healthy is indistinguishable from a fact that never occurred, with nothing able to
    recover it and no reader able to detect it. A crash-shaped loss is different --
    the repair closes what a kill left behind. So a stuck filesystem costs memory,
    and if that kills the process then every session's unwritten entries go with it:
    worse in the tail, and accepted, because a ceiling only makes the hole less
    likely while guaranteeing it happens.

    What must hold instead is that the growth is VISIBLE -- the backlog readable and
    the stall named -- so the operator sees the cause rather than a quiet gap.

    Driven through the buffer with the writer genuinely occupied, and waiting on the
    writer's own signal rather than a clock. Going through the public emitters would
    take the INLINE path -- there is no event loop in a sync test -- and the hanging
    job would block this thread instead of the writer's.

    Mutation guard: re-introducing a shed at any depth reddens the drop count below.
    """
    took_it = threading.Event()
    release = threading.Event()

    def _hangs() -> None:
        took_it.set()
        release.wait(30.0)

    try:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.crew_log.emit"):
            emit._buffer(SESSION, _pending(_hangs, "a write that never returns"))
            assert took_it.wait(20.0), "the writer never picked up the hanging job"

            # The writer is inside a call that will not come back, and producers keep
            # appending: the shape that has no attempt counter to bound it.
            for n in range(64):
                emit._buffer(SESSION, _pending(lambda: None, f"an append behind it {n}"))

            assert emit.buffered_writes() >= 64, (
                f"entries went missing while the write was hung: only "
                f"{emit.buffered_writes()} of 64 are held"
            )
            assert emit.dropped_writes() == 0, (
                f"{emit.dropped_writes()} append(s) were discarded while the process "
                "was healthy; a hole in this log may not be voluntary"
            )
            assert emit.peak_buffered_writes() >= 64, "the backlog peak is not readable"
    finally:
        release.set()
        assert emit.drain_for_shutdown(timeout=20.0)


def test_a_write_stuck_too_long_is_named(caplog):
    """Growth is allowed, silence is not.

    With no ceiling, a stuck write shows up only as memory climbing -- so the stall
    itself has to be reported, or an operator sees a process growing with no stated
    cause. The threshold is injected rather than waited out: a test that slept for it
    would be measuring the clock instead of the behaviour.

    Mutation guard: dropping the report leaves this silent and reddens the assertion.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(emit, "_WRITE_STALL_SECS", 0.0)
        with emit._lock:
            emit._inflight_since = time.monotonic()
            emit._inflight_what = "a write that never returns"
            emit._stall_reported = False
        try:
            with caplog.at_level(logging.ERROR, logger="kiro_crew.crew_log.emit"):
                emit._note_stall_if_any()
                assert any(
                    "neither returned nor failed" in r.getMessage() for r in caplog.records
                ), "a write stuck past the threshold was not reported"
                # Once, not per producer: a stuck writer would otherwise fill the log.
                before = len(caplog.records)
                emit._note_stall_if_any()
                assert len(caplog.records) == before, "the stall was reported twice"
        finally:
            with emit._lock:
                emit._inflight_since = 0.0
                emit._inflight_what = ""


def test_a_write_that_finishes_late_sheds_nothing():
    """Slow is not stuck: a job that returns leaves the counters alone.

    The ceiling is the one place memory is shed, so it must not fire for a write
    that was merely behind -- otherwise every busy disk would cost entries.
    """
    release = threading.Event()
    real_append = lg.CrewLog.append

    def _slow(self, *a, **kw):
        release.wait(10.0)
        return real_append(self, *a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        _open_session()
        assert emit.flush()
        mp.setattr(lg.CrewLog, "append", _slow)
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
        release.set()
        assert emit.drain_for_shutdown(timeout=20.0)

    assert emit.dropped_writes() == 0, "a slow write cost an entry"
    kinds = [e["type"] for e in _body()]
    assert "turn/started" in kinds and "turn/completed" in kinds


def test_attachment_metadata_is_omitted_before_a_fitting_body_is_split():
    """Attachment detail yields before a fitting message body."""
    _open_session()
    body = "x" * (emit._crew_log().MAX_ENTRY_BYTES - emit._ENVELOPE_HEADROOM - 64)
    ids = [f"/tmp/attachment-{n:04d}-with-a-long-enough-name.bin" for n in range(200)]
    emit.on_message_received(SESSION, 1, role="user", text=body, attachments=ids)
    assert emit.flush()

    received = [e for e in _body() if e["type"] == "message/received"]
    assert len(received) == 1, "the message was refused when attachment metadata exceeded the cap"
    assert emit.dropped_writes() == 0
    data = received[0]["data"]
    kept = data.get("attachments", [])
    assert kept == ids[: len(kept)]
    assert data["attachments_omitted"] == len(ids) - len(kept)
    assert data["text"] == body
    assert "chunks" not in data


def test_an_oversize_body_reaches_the_file_as_one_group():
    """The emitter writes a chunk group through the batched append, not one by one.

    Entry by entry, a hard kill between the chunks and the entry that cites them
    leaves the body on disk with nothing pointing at it. This pins the property a
    reader depends on: the cited seqs are exactly the chunk entries that precede the
    citing entry, contiguously, with no other entry interleaved.

    Mutation guard: writing the chunks with individual appends reddens the call
    count; writing the citing entry FIRST reddens the ordering assertion.
    """
    calls = {"many": 0}
    real_many = lg.CrewLog.append_many

    def _counting(self, items, **kw):
        calls["many"] += 1
        return real_many(self, items, **kw)

    with pytest.MonkeyPatch.context() as mp:
        _open_session()
        mp.setattr(lg.CrewLog, "append_many", _counting)
        body = "y" * (emit._crew_log().MAX_ENTRY_BYTES * 2)
        emit.on_message_sent(SESSION, 1, step=1, text=body)
        assert emit.flush()

    assert calls["many"] == 1, "the group was not written as a single batch"
    entries = _body()
    sent = [e for e in entries if e["type"] == "message/sent"]
    assert len(sent) == 1
    cited = sent[0]["data"]["chunks"]
    assert cited, "an oversize body was not chunked"
    chunk_seqs = [e["seq"] for e in entries if e["type"] == "message/chunk"]
    assert cited == chunk_seqs, "the citing entry names seqs that are not the chunks"
    assert cited == list(range(cited[0], cited[0] + len(cited))), "the chunk seqs have a gap"
    assert sent[0]["seq"] == cited[-1] + 1, "the citing entry does not follow its chunks"
    assert sent[0]["data"]["chars"] == len(body)


# --- one wedged session must not take another down ------------------------


def test_a_wedged_session_neither_reorders_nor_loses_another_and_both_land():
    """The per-session bucket, asserted rather than only claimed -- and its limit.

    The buffer is keyed per session because each session is a separate file, and the
    guarantee that shape buys is written in the spec: one slow crew log must not
    REORDER another's entries. This pins that, and it also pins the part the spec
    now states explicitly because writing this test is what surfaced it: ONE worker
    drains every session, so a write that hangs does DELAY every other session's
    entries until it clears. Bucketing protects order and content, not latency.

    So what is asserted is what the design actually provides: while A is wedged, B's
    entries are held in B's own bucket, none are shed, and when the wedge clears they
    land in the order they were made. A test asserting B lands DURING the wedge would
    be asserting an isolation this design does not have -- and it failed exactly that
    way before being corrected.

    Driven through the buffer with the writer genuinely occupied and synchronized on
    the writer's own signal: the public emitters take the INLINE path in a sync test,
    where A's hang would block this thread instead of the writer's.
    """
    other = "acp-sess-0002"
    emit.on_session_opened(
        other, agent="kirocrew", slot="chat-8", model="claude-opus-5", cwd="/tmp", owner="default"
    )
    _open_session()
    assert emit.flush(), "the two headers did not land"

    took_it = threading.Event()
    release = threading.Event()

    def _hangs() -> None:
        took_it.set()
        release.wait(30.0)

    def _b_write(index: int):
        def _job() -> None:
            handle = lg.CrewLog.open(lg.KIND_SESSION, other)
            handle.append("turn/started", {"turn": index, "actor": "user", "depth": 0}, src="acp")

        return _job

    # A occupies the single writer thread with a call that will not come back.
    emit._buffer(SESSION, _pending(_hangs, "a write that never returns"))
    assert took_it.wait(20.0), "the writer never picked up the hanging job"

    # B's entries buffer behind it, in a DIFFERENT bucket.
    for index in (1, 2, 3):
        emit._buffer(other, _pending(_b_write(index), f"appending turn/started {index}"))
    assert emit.buffered_writes() >= 3, "B's entries were not held"
    assert emit.dropped_writes() == 0, "an entry was shed while only one session was stuck"
    # B's header landed in the setup flush, so the check is on the entries under
    # test: none of them may reach the file while the single worker is occupied.
    held = [e for e in _body(other) if e["type"] == "turn/started"]
    assert not held, "B wrote during the wedge -- the single worker was not occupied"

    # The wedge clears, and B's held entries land in the order they were made.
    release.set()
    assert emit.drain_for_shutdown(timeout=20.0), "the drain did not finish after the wedge"

    b_turns = [e["data"]["turn"] for e in _body(other) if e["type"] == "turn/started"]
    assert b_turns == [1, 2, 3], f"a wedged session reordered another's entries: {b_turns}"
    assert emit.dropped_writes() == 0, "an entry was lost to a wedge that later cleared"


def test_queued_terminal_write_keeps_ownership_until_it_lands():
    """Cache pressure cannot hand away a still-open turn's write lease."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    writer_entered = threading.Event()
    release_writer = threading.Event()

    def _occupy_writer() -> None:
        writer_entered.set()
        assert release_writer.wait(20.0)

    try:
        emit._buffer("blocked-writer", _pending(_occupy_writer, "holding the writer"))
        assert writer_entered.wait(20.0), "the writer was never occupied"

        async def _queue_closer() -> None:
            emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")

        asyncio.run(_queue_closer())
        assert "turn/completed" not in [e["type"] for e in _body()]

        for index in range(emit._MAX_OPEN_CREW_LOGS + 1):
            emit._remember(f"pressure-{index}", object())
        gc.collect()
        assert (
            not _can_take_ownership()
        ), "ownership released while the turn is still open in the file"
    finally:
        release_writer.set()

    assert emit.flush(timeout=20.0)
    assert [e["type"] for e in _body()][-1] == "turn/completed"
    with emit._lock:
        assert (SESSION, 1) not in emit._live


def test_retryable_terminal_failure_keeps_the_turn_pinned():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    original_append = lg.CrewLog.append
    original_retain = emit._retain
    retained = threading.Event()
    failures = {"left": 1}

    def _fail_closer_once(self, entry_type, *args, **kwargs):
        if entry_type == "turn/completed" and failures["left"]:
            failures["left"] -= 1
            raise OSError("input/output error")
        return original_append(self, entry_type, *args, **kwargs)

    def _observe_retain(session_id, jobs):
        original_retain(session_id, jobs)
        if session_id == SESSION:
            retained.set()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lg.CrewLog, "append", _fail_closer_once)
        mp.setattr(emit, "_retry_delay", lambda _attempts: 30.0)
        mp.setattr(emit, "_retain", _observe_retain)

        async def _queue_closer() -> None:
            emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")

        asyncio.run(_queue_closer())
        assert retained.wait(20.0), "the failed closer was never retained"
        with emit._lock:
            assert (SESSION, 1) in emit._live, "a retryable terminal failure released the turn pin"
            emit._retry[SESSION].not_before = 0.0
        emit._wake.set()
        assert emit.flush(timeout=20.0)

    assert failures["left"] == 0
    assert [e["type"] for e in _body()][-1] == "turn/completed"
    with emit._lock:
        assert (SESSION, 1) not in emit._live


@pytest.mark.parametrize("terminal", ["refused", "completed", "failed"])
def test_every_terminal_path_releases_its_pin_after_a_definitive_drop(terminal):
    _open_session()
    emit._pin(SESSION, 1)

    original_append = lg.CrewLog.append

    def _fail_closer(self, entry_type, *args, **kwargs):
        if entry_type in {"turn/refused", "turn/completed"}:
            raise OSError("input/output error")
        return original_append(self, entry_type, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lg.CrewLog, "append", _fail_closer)
        if terminal == "refused":
            emit.on_turn_refused(SESSION, 1, "stopped_before_dispatch")
        elif terminal == "completed":
            emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
        else:
            emit.on_turn_failed(SESSION, 1, error="AcpError")
        assert emit.flush(timeout=20.0)

    assert emit.dropped_writes() == 1
    with emit._lock:
        assert (SESSION, 1) not in emit._live


# --- the gateway boot path -------------------------------------------------


_BOOT_PROBE = """
import atexit, importlib, json, sys

registered = []
_real = atexit.register


def _record(fn, *a, **k):
    registered.append(getattr(fn, "__name__", repr(fn)))
    return _real(fn, *a, **k)


atexit.register = _record
importlib.import_module("kiro_crew.dashboard.server")
print(
    json.dumps(
        {
            "storage": sorted(
                k
                for k in sys.modules
                if k.startswith("kiro_crew.crew_log.")
                and k != "kiro_crew.crew_log.emit"
            ),
            "glue": "kiro_crew.crew_log.emit" in sys.modules,
            "hooks": [n for n in registered if "drain_for_shutdown" in n],
        }
    )
)
"""


def _boot_probe(tmp_path: Path) -> dict:
    """Import a boot-path module in a CLEAN interpreter with the flag unset.

    A clean process is the only place this is observable: the suite has already
    imported both the emitter and its storage package, so an in-process check
    would read this test file's own imports rather than the boot path's.
    """
    env = {
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_path),
        "KIROCREW_HOME": str(tmp_path / "home"),
    }
    if sys.platform == "win32":  # pragma: no cover - parity with the lease suite
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        # ``Path.home()`` runs at import time on the boot path and has no ``pwd``
        # fallback on Windows, so the home-determining variables belong in even a
        # minimal environment.
        for name in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
            env[name] = os.environ.get(name, "")
    # ``cwd`` moves off the repo, so the interpreter path has to be absolute: an interpreter
    # invoked through a relative PATH entry reports a relative ``sys.executable``, which a
    # child started elsewhere cannot find. Absolutise without resolving symlinks, because a
    # venv interpreter links to the system one and following that link drops the venv's own
    # site-packages.
    interpreter = os.path.abspath(sys.executable)
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [interpreter, "-c", _BOOT_PROBE],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert done.returncode == 0, f"the boot probe did not run: {done.stderr[-2000:]}"
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_flag_off_launch_does_not_load_the_storage_subsystem(tmp_path: Path) -> None:
    """The boot path may reach the emitter's glue, never the storage package.

    AUTOSDE's ``no-new-work-on-gateway-boot-path`` rule asks for an optional
    subsystem's import to be gated rather than only its calls, so this asserts the
    split the module documents: the glue is importable for free, and the store,
    schema and lease stay unloaded until a call reaches storage.
    """
    seen = _boot_probe(tmp_path)
    assert seen["glue"], "the probe did not reach the emitter at all, so it proves nothing"
    assert seen["storage"] == [], (
        "a launch with KIROCREW_CREW_LOG unset imported the storage subsystem: "
        f"{seen['storage']}"
    )


def test_a_flag_off_launch_registers_no_shutdown_hook(tmp_path: Path) -> None:
    """Importing the emitter must not register the backstop drain.

    An ``atexit`` handler installed at import is work every launch pays for a
    subsystem it will never call, and it is the half of the boot-path cost that a
    module-load census cannot see.
    """
    seen = _boot_probe(tmp_path)
    assert seen["hooks"] == [], (
        "a flag-off launch registered the crew log drain at exit: " f"{seen['hooks']}"
    )


def test_the_shutdown_hook_is_registered_once_on_first_use(monkeypatch) -> None:
    """First use registers the backstop exactly once, however many passes follow."""
    registered: list[str] = []
    monkeypatch.setattr(
        emit.atexit, "register", lambda fn, *a, **k: registered.append(getattr(fn, "__name__", ""))
    )
    monkeypatch.setattr(emit, "_shutdown_hook_registered", False, raising=False)

    emit._ensure_shutdown_hook()
    emit._ensure_shutdown_hook()

    assert registered == ["drain_for_shutdown"], (
        "the backstop drain was not registered exactly once on first use: " f"{registered}"
    )


# --- the log-creating record is exempt from the memory ceiling ----------


def test_overflow_while_the_creating_record_is_queued_still_creates_the_log(monkeypatch):
    """(a) A ceiling crossed as the file-creating record is queued must not erase it.

    The ceiling bounds PAYLOAD memory, and the record that creates the crew log is
    O(1) per session -- refusing it would leave the session with no file, so every
    later entry (the loss marker included) would be a silent uncounted no-op. The
    creating record is exempt, so it lands even at a zero-capacity ceiling and the
    session's entries are recorded.

    Driven with the writer OCCUPIED so ``on_session_opened`` buffers its creating
    record through ``_buffer`` -- the one path the ceiling guards -- instead of the
    inline fast path a session that owes nothing would otherwise take.
    """
    writer_entered = threading.Event()
    release_writer = threading.Event()

    def _occupy_writer() -> None:
        writer_entered.set()
        assert release_writer.wait(20.0)

    try:
        emit._buffer("blocked-writer", _pending(_occupy_writer, "holding the writer"))
        assert writer_entered.wait(20.0), "the writer was never occupied"

        # The ceiling now rejects any non-exempt append, but the creating record
        # is exempt and must still be admitted while the writer is busy.
        monkeypatch.setattr(emit, "_MAX_PENDING_COUNT", 0)
        emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7")
        emit.on_turn_started(SESSION, 1, "user")
        assert emit.overflow_writes() >= 1, "a non-exempt append was expected to overflow"
    finally:
        release_writer.set()

    monkeypatch.setattr(emit, "_MAX_PENDING_COUNT", 100_000)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush(timeout=20.0)

    assert _log_path().is_file(), "the ceiling refused the log-creating record"
    body_types = [e["type"] for e in _body()]
    assert "session/opened" in body_types, "the creating session/opened entry was refused"


# --- a permanently failed creation makes later discards COUNT --------------


def _fail_creation_permanently() -> None:
    """Open a session whose creating record is REFUSED (a permanent CrewLogError).

    Leaves ``SESSION`` flagged in ``_creation_failed`` with no log file, which
    is the state a later append must count as loss rather than silently no-op.
    """
    real_create = lg.CrewLog.create

    def _refuse_create(kind, unit_id, *args, **kwargs):
        if unit_id == SESSION:
            raise lg.CrewLogError("refused at create", code=lg.CODE_ALREADY_OWNED)
        return real_create(kind, unit_id, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "create", _refuse_create)
        emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7")
        assert emit.flush(timeout=20.0)


def test_a_permanently_failed_creation_counts_later_discards_as_loss():
    """(b) After a permanent creation failure, later entries are COUNTED, not dropped silently.

    A session whose creating record died permanently has no log file, but it
    WAS opened, so its later entries are a real loss. ``_handle`` raises a refusal
    for such a session instead of returning None, so the discarded entry is counted
    in ``dropped_writes`` rather than vanishing as a silent policy no-op.
    """
    _fail_creation_permanently()
    assert SESSION in emit._creation_failed, "the failed creation was not flagged"
    assert not _log_path().is_file(), "no log file should exist after a failed creation"
    dropped_before = emit.dropped_writes()

    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush(timeout=20.0)

    assert emit.dropped_writes() == dropped_before + 1, (
        "a discarded entry for a creation-failed session was a silent no-op, " "not counted as loss"
    )


def test_a_session_that_was_never_opened_stays_a_silent_no_op():
    """(b, negative half) A session that legitimately has no crew log is unchanged.

    The creation-failed count must not turn the ordinary "feature off / never
    opened" no-op into a counted loss: that session is a policy no-op, uncounted.
    """
    dropped_before = emit.dropped_writes()
    emit.on_turn_started("never-opened-session", 1, "user")
    assert emit.flush(timeout=20.0)
    assert not _log_path("never-opened-session").exists()
    assert emit.dropped_writes() == dropped_before, "an unopened session was counted as a loss"


def test_a_closed_session_does_not_leave_its_creation_failure_flagged():
    """The creation-failure verdict dies with the session, not at the next reset.

    The flag makes every later entry for that id a counted loss, so a successor
    reusing the id would inherit a verdict about a crew log it never created. Left
    to ``reset_caches`` the flag would also outlive every failed session for as
    long as the process runs. It is cleared in the close path's terminal cleanup,
    which runs even when the closing entry itself was dropped -- and that is the
    case for exactly the sessions the flag is set on.
    """
    _fail_creation_permanently()
    assert SESSION in emit._creation_failed, "the failed creation was not flagged"

    emit.on_session_closed(SESSION, reason="test")
    assert emit.flush(timeout=20.0)

    assert (
        SESSION not in emit._creation_failed
    ), "a closed session left its creation-failure flag behind"


# --- loss debt survives until its marker lands -----------------------------


def test_loss_debt_survives_until_the_marker_actually_lands():
    """(c) A loss stays owed, WITH ITS COUNT, until its marker is on disk.

    A dropped entry records a debt; that debt is not cleared when the entry is
    dropped, only when a ``write/dropped`` marker naming it is appended. While the
    marker cannot land the debt persists -- its count intact, not merely an empty
    placeholder -- and ``flush`` does not report quiet. When the marker finally
    lands it carries the full preserved count.
    """
    _open_session()
    assert emit.flush()

    real_append = lg.CrewLog.append

    def _lose_marker(self, entry_type, *args, **kwargs):
        if entry_type == "write/dropped":
            raise OSError("marker cannot land")
        return real_append(self, entry_type, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _lose_marker)
        patch.setattr(emit, "_start_drain", lambda: None)
        emit._buffer(
            SESSION,
            emit._PendingJob(
                job=lambda: (_ for _ in ()).throw(OSError("payload loss")),
                what="payload loss",
                nbytes=13,
            ),
        )
        deferred_loss: set[str] = set()
        for _ in range(emit._MAX_WRITE_ATTEMPTS * 2):
            with emit._lock:
                emit._draining = False
                emit._drain_future = None
                retry = emit._retry.get(SESSION)
                if retry is not None:
                    retry.not_before = 0.0
            deferred_loss.update(emit._drain_once(deferred_loss))
        with emit._lock:
            owed = emit._pending_loss.get(SESSION)
            assert owed is not None, "the loss debt was cleared before its marker landed"
            assert owed.dropped_count == 1, (
                "the debt count did not survive the failed marker attempts: "
                f"{owed.dropped_count}"
            )
        assert not emit.flush(
            timeout=0.1
        ), "flush reported quiet while the loss marker was still owed"

    before = len(_body())
    emit.on_turn_completed(SESSION, 1, stop_reason="recovered")
    assert emit.flush(timeout=20.0)
    recovered = _body()[before:]
    assert (
        recovered[0]["type"] == "write/dropped"
    ), f"the owed marker did not lead the next batch: {[e['type'] for e in recovered]}"
    assert recovered[0]["data"]["dropped_count"] == 1, (
        "the landed marker lost the debt it was owed: " f"{recovered[0]['data']}"
    )
    with emit._lock:
        assert SESSION not in emit._pending_loss, "the debt was not cleared once the marker landed"


# --- the live-turn cap never restarts a live turn's numbering --------------


def test_a_live_turns_ordinals_never_restart_when_the_cap_is_reached(monkeypatch):
    """(d) At the live-turn cap, an existing live turn's step/call_index never restart.

    The oldest live record is the one an oldest-first eviction would reach first.
    With a tiny cap and only live turns present, the cap must be ACCEPTED as an
    overage rather than evicting the oldest live turn -- because evicting it drops
    its counters and its next event mints a fresh 0-based ordinal, so two entries
    claim one. Its call_index must keep climbing across the pressure.
    """
    monkeypatch.setattr(emit, "_MAX_LIVE_TURNS", 4)
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="a-1")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="a-2")

    # Fill past the cap with other live turns; none is closed, so none is sheddable.
    for n in range(emit._MAX_LIVE_TURNS + 3):
        other = f"livecap-{n:04d}"
        emit.on_session_opened(other, agent="kirocrew")
        emit.on_turn_started(other, 1, "user")
    assert emit.flush()

    assert (SESSION, 1) in emit._live, "the oldest live turn was evicted"
    assert len(emit._live) > emit._MAX_LIVE_TURNS, "the overage was not accepted"

    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="a-3")
    assert emit.flush()
    indexes = [e["data"]["call_index"] for e in _body() if e["type"] == "tool/called"]
    assert indexes == [1, 2, 3], f"a live turn's numbering restarted at the cap: {indexes}"


# --- the cap overage is reported once, not per event -----------------------


def test_the_live_turn_cap_overage_is_reported_once_not_per_event(monkeypatch, caplog):
    """(e) An accepted live-turn overage is named once, however many events follow."""
    monkeypatch.setattr(emit, "_MAX_LIVE_TURNS", 3)
    with caplog.at_level(logging.ERROR, logger=emit.logger.name):
        _open_session()
        # Enough live turns to sit over the cap, each minting several events.
        for n in range(emit._MAX_LIVE_TURNS + 4):
            other = f"overage-{n:04d}"
            emit.on_session_opened(other, agent="kirocrew")
            emit.on_turn_started(other, 1, "user")
            emit.on_tool_called(other, 1, name="fs_read", call_id=f"c-{n}")
        assert emit.flush()

    overage_lines = [r for r in caplog.records if "accepting the overage" in r.getMessage()]
    assert len(overage_lines) == 1, (
        "the live-turn cap overage was reported "
        f"{len(overage_lines)} times, not once: {[r.getMessage() for r in overage_lines]}"
    )


# --------------------------------------------------------------------------- #
# A superseded crew log: the `previous` edge that names it
# --------------------------------------------------------------------------- #

#: The successor id a superseded-session test opens after ``SESSION``. A restart
#: cold-starts the slot's ACP session under a new id, which is why the successor
#: gets a crew log of its own rather than re-attaching to this one.
SUCCESSOR = "acp-sess-0002"


def _open_successor(**kwargs) -> None:
    """Open ``SUCCESSOR`` on the same slot ``_open_session`` uses."""
    emit.on_session_opened(
        SUCCESSOR,
        agent="kirocrew",
        slot="chat-7",
        model="claude-opus-5",
        cwd="/home/dev/project",
        owner="default",
        **kwargs,
    )


def test_a_new_store_for_a_slot_that_had_one_names_it_as_previous():
    """The edge `resumed` cannot express, because the successor is a different unit.

    A slot outlives its ACP session. When the session is torn down and the
    successor cold-starts under a new id, `CrewLog.exists` is false for that id,
    so the emitter CREATES a second crew log for one slot. `resumed` is correctly
    false there -- nothing re-attached -- and without this edge no field in either
    file says the two belong to the same slot.
    """
    _open_session()
    assert emit.flush()
    _open_successor(previous_sid=SESSION)
    assert emit.flush()

    opened = [e for e in _body(SUCCESSOR) if e["type"] == "session/opened"]
    assert len(opened) == 1
    assert opened[0]["data"]["previous"] == {"sid": SESSION}
    # Not redefined: the successor did not re-attach to anything.
    assert opened[0]["data"]["resumed"] is False
    # The edge is written on the SUCCESSOR only. The superseded log was written by
    # a session that never learns its successor's id, the same asymmetry `parent`
    # is recorded on the child for.
    assert "previous" not in _body(SESSION)[0]["data"]


def test_a_slots_first_store_names_no_previous():
    """Absent, not empty: "first crew log" has to be readable as its own case.

    A `previous` carrying an empty sid would read as an earlier crew log with an
    empty name, and a chain walker would try to open it.
    """
    _open_session()
    assert emit.flush()
    assert "previous" not in _body(SESSION)[0]["data"]


def test_a_resume_of_the_same_store_writes_no_previous_edge():
    """A store cannot be its own predecessor, and the emitter decides that here.

    The caller passes the id the slot was MAPPED to, which on the resume path is
    this session itself. Normally that is harmless because a resume re-attaches
    and never creates. The case that needs the comparison is a resumed session
    whose crew log is GONE: retention removes whole crew logs, so `session/load`
    can succeed while `CrewLog.exists` is false, and the emitter then creates a
    second crew log under the SAME id it was handed as the predecessor. Taking the
    caller's value on trust writes `previous {sid: <self>}` there, and a chain
    walker revisits the store it started from.

    Mutation guard: dropping the `previous_sid != session_id` comparison writes a
    self-edge here.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    # Retention's effect on this unit, without waiting for retention.
    _log_path(SESSION).unlink()
    emit.reset_caches()
    assert not lg.CrewLog.exists(lg.KIND_SESSION, SESSION)

    emit.on_session_opened(
        SESSION, agent="kirocrew", slot="chat-7", resumed=True, previous_sid=SESSION
    )
    assert emit.flush()

    opened = [e for e in _body(SESSION) if e["type"] == "session/opened"]
    assert len(opened) == 1, "the re-created crew log carries exactly one opener"
    assert "previous" not in opened[0]["data"]
    # And a reader stepping back from it is not sent to itself.
    assert crew_log_projection.read_projection(SESSION, "status").value["previous"] is None


def test_a_warm_reuse_of_a_live_store_writes_nothing_at_all():
    """The silent case is still silent, edge or no edge.

    Every turn calls this, and a warm reuse of a crew log this process already
    holds has nothing new to say. A `previous_sid` arriving on such a turn (the
    map names this same session, which is the ordinary steady state) must not turn
    a silent claim into a second opener.
    """
    _open_session()
    assert emit.flush()
    before = len(_body(SESSION))

    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", previous_sid=SESSION)
    assert emit.flush()
    assert len(_body(SESSION)) == before, "a warm reuse appended something"


def test_a_re_attach_names_no_predecessor():
    """Only a CREATE may name a predecessor, because only a create superseded one.

    A re-attach already has its store, so a unit the caller names alongside it is
    either that same store or an unrelated one. Writing the edge there would claim
    a supersede that did not happen, and point a chain walker at a store this slot
    never wrote.

    Mutation guard: dropping `created` from the latch condition writes the edge
    here.
    """
    # A bystander with work in flight, on a different slot.
    bystander = "acp-sess-bystander"
    emit.on_session_opened(bystander, agent="kirocrew", slot="chat-9")
    emit.on_turn_started(bystander, 1, "user")
    assert emit.flush()

    _open_session()
    assert emit.flush()
    emit.reset_caches()
    # A re-attach to a store that EXISTS, handed an unrelated unit as predecessor.
    emit.on_session_opened(
        SESSION, agent="kirocrew", slot="chat-7", resumed=True, previous_sid=bystander
    )
    assert emit.flush()

    for entry in [e for e in _body(SESSION) if e["type"] == "session/opened"]:
        assert "previous" not in entry["data"]
    assert [
        e for e in _body(bystander) if e["type"] == "turn/completed"
    ] == [], "the bystander's live turn was closed by another session's re-attach"


def test_a_candidate_from_another_slot_is_not_linked():
    """`previous` means the SAME slot, so the candidate's own header must say so.

    The id arrives from the slot-to-session mapping, a persisted file whose entry
    can be stale or recycled by the time a successor cold-starts. Comparing that
    mapping against itself proves nothing, so the check reads the candidate crew
    log's own header -- written once at create, never rewritten -- and links only a
    candidate that names this slot. Otherwise a reader following the edge lands in
    a crew log this slot never wrote.

    Mutation guard: removing the check links the other slot's crew log here.
    """
    # A crew log belonging to a DIFFERENT slot, fully written.
    other = "acp-sess-other-slot"
    emit.on_session_opened(other, agent="kirocrew", slot="chat-9", cwd="/home/dev/project")
    assert emit.flush()

    _open_successor(previous_sid=other)
    assert emit.flush()

    opened = [e for e in _body(SUCCESSOR) if e["type"] == "session/opened"]
    assert len(opened) == 1
    assert "previous" not in opened[0]["data"], "a crew log from another slot was linked"


def test_a_candidate_whose_crew_log_is_gone_is_not_linked_and_costs_no_entry():
    """Unverifiable is not the same as verified, and the entry still lands.

    Retention removes whole crew logs, so a successor can name a predecessor whose
    header is unreadable. That candidate is not KNOWN to be this slot's, so
    no edge is written -- ending the walk one link early beats sending a reader
    somewhere unverified. The successor's own record is what this job owes, and the
    refusal must not take it down too.
    """
    _open_successor(previous_sid="acp-sess-never-existed")
    assert emit.flush()

    opened = [e for e in _body(SUCCESSOR) if e["type"] == "session/opened"]
    assert len(opened) == 1
    assert "previous" not in opened[0]["data"]


def test_the_previous_edge_survives_a_retry_that_finds_the_header_already_written():
    """Latched with the create decision, for the same reason that one is.

    A retry after the header landed but the entry did not reads `exists` true and
    `created` false. An unlatched edge would be dropped exactly there, silently
    and permanently, while the entry it belongs to still gets written.

    Mutation guard: reading `created` instead of the latch drops `previous` here.
    """
    _open_session()
    assert emit.flush()

    calls = {"n": 0}
    real_append = lg.CrewLog.append

    def _fail_the_first_append(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1 and self.id == SUCCESSOR:
            raise OSError("the entry did not land, the header did")
        return real_append(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _fail_the_first_append)
        _open_successor(previous_sid=SESSION)
        assert emit.flush()
        assert emit.flush()

    opened = [e for e in _body(SUCCESSOR) if e["type"] == "session/opened"]
    assert len(opened) == 1, "the retried entry landed more or less than once"
    assert opened[0]["data"]["previous"] == {"sid": SESSION}


def test_a_reader_joins_both_stores_of_one_slot_through_the_status_fold():
    """The read side: one slot's history, joined across the supersede boundary.

    Each fold reads ONE crew log, which is what the routes and the crew-log MCP
    server address. `previous` is what lets a reader that wants the SLOT rather
    than the session step from the newest crew log to the one before it, one fold
    at a time, through the projection route that already exists.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    _open_successor(previous_sid=SESSION)
    emit.on_turn_started(SUCCESSOR, 1, "user")
    emit.on_turn_completed(SUCCESSOR, 1, stop_reason="end_turn")
    assert emit.flush()

    newest = crew_log_projection.read_projection(SUCCESSOR, "status").value
    assert newest["slot"] == "chat-7"
    assert newest["turn_open"] is False
    # The edge a reader follows to reach the rest of this slot's history.
    assert newest["previous"] == SESSION

    older = crew_log_projection.read_projection(newest["previous"], "status").value
    assert older["slot"] == "chat-7", "one slot, two crew logs"
    assert older["previous"] is None, "the oldest crew log ends the walk"
    # The superseded turn stays OPEN: the edge is a citation, and closing that
    # store's dangling tail is tracked separately. A reader joining the slot sees
    # both crew logs, and sees that the older one's last turn never ended.
    assert older["turn_open"] is True


def test_a_cleared_sid_still_names_the_predecessor():
    """`clear_sid` drops the POINTER, not the history, so the edge survives it.

    A provider switch and the poisoned-conversation discard both empty `sid` and
    stash it as `discarded_sid`, saying so in the method's own docstring: the
    native conversation is still on disk. For the RESUME question the clear is the
    answer, which is why `get` must not see the stashed id. For the history
    question it is not: the cleared id is exactly "the id this key was last
    serving". Reading `sid` alone made a key that had served a session all day
    report having served none, so the successor spawned after the clear cited no
    predecessor and the slot's history was truncated with nothing recording it.

    Mutation guard: dropping the `discarded_sid` fallback answers "" here.
    """
    mapping = session_map.SessionMap()
    mapping.set("dashboard:chat-9", SESSION, provider="", cwd="")
    assert mapping.mapped_sid("dashboard:chat-9") == SESSION

    mapping.clear_sid("dashboard:chat-9")

    # The resume answer and the history answer diverge here, which is the point.
    assert mapping.get("dashboard:chat-9") in (None, "")
    assert mapping.mapped_sid("dashboard:chat-9") == SESSION


def test_an_oversized_id_in_the_mapping_answers_as_no_predecessor():
    """This map is agent-writable JSON, so a value read back is input, not a fact.

    An id longer than the bound every other consumer applies would ride into
    `session/opened.previous`, and an entry over the maximum size is DROPPED rather
    than truncated -- so one oversized value in this file would cost a record that
    had nothing to do with it. Answering "" keeps the failure at the size of "no
    predecessor", which every caller already handles.

    Mutation guard: removing the bound returns the oversized id here.
    """
    mapping = session_map.SessionMap()
    mapping.set("dashboard:chat-big", "x" * 4096, provider="", cwd="")

    assert mapping.mapped_sid("dashboard:chat-big") == ""

    # At the bound it still answers: the rule is a ceiling, not a narrowing.
    mapping.set("dashboard:chat-fits", "y" * 128, provider="", cwd="")
    assert mapping.mapped_sid("dashboard:chat-fits") == "y" * 128


def test_a_key_that_never_served_a_session_names_no_predecessor():
    """The fallback must not invent one: absent is still "nothing to follow"."""
    mapping = session_map.SessionMap()
    assert mapping.mapped_sid("dashboard:chat-never") == ""


def test_a_stale_prune_records_the_id_it_dropped_as_the_keys_latest_store():
    """Clearing A then pruning B must not leave A standing as the last store.

    Three paths empty ``sid`` in place: the provider switch, the startup prune,
    and the per-read stale repair. All three must record what they dropped. When
    only one does, a prune leaves an OLDER id in ``discarded_sid`` and the history
    reader answers that -- naming a predecessor two links back and orphaning the store
    between them. Which path emptied the field is not a distinction any reader of
    it can use, so it cannot be written by only some of them.

    Mutation guard: returning the prune's clear to a bare ``sid = ""`` answers
    ``acp-sess-A`` here.
    """
    mapping = session_map.SessionMap()
    key = "dashboard:chat-42"

    mapping.set(key, "acp-sess-A", provider="", cwd="")
    mapping.clear_sid(key)
    mapping.set(key, "acp-sess-B", provider="", cwd="")
    assert mapping.mapped_sid(key) == "acp-sess-B"

    # A binding makes the entry outlive its session, so the stale path empties the
    # pointer in place instead of removing the whole entry.
    mapping._data[session_map.canonical_key(key)]["slack_thread_ts"] = "1700000000.1"

    # B's transcript does not exist here, which is what prune calls stale.
    mapping.prune()

    assert mapping.mapped_sid(key) == "acp-sess-B", (
        "the reader named the older cleared id, so a successor would cite a "
        "predecessor two links back and skip the store between them"
    )


def test_three_stores_of_one_slot_form_a_chain_with_no_store_skipped():
    """A to B to C, each citing the one before it rather than the one before that.

    What a reader needs from the edge: following it from the newest store reaches
    every earlier one exactly once. Each predecessor is handed in directly here,
    which is what the caller does -- it reads the slot's mapping and latches the
    answer -- so this pins the chain shape the emitter writes rather than where
    the caller found the id.
    """
    first, second, third = SESSION, SUCCESSOR, "acp-sess-0003"

    emit.on_session_opened(first, agent="kirocrew", slot="chat-7", cwd="/home/dev/project")
    assert emit.flush()

    emit.on_session_opened(
        second,
        agent="kirocrew",
        slot="chat-7",
        cwd="/home/dev/project",
        previous_sid=first,
    )
    assert emit.flush()

    emit.on_session_opened(
        third,
        agent="kirocrew",
        slot="chat-7",
        cwd="/home/dev/project",
        previous_sid=second,
    )
    assert emit.flush()

    def _previous(session_id: str) -> dict | None:
        opened = [e for e in _body(session_id) if e["type"] == "session/opened"]
        assert len(opened) == 1
        return opened[0]["data"].get("previous")

    assert _previous(first) is None
    assert _previous(second) == {"sid": first}
    assert _previous(third) == {"sid": second}, "the chain skipped the store between"


def test_every_slot_allocation_site_latches_the_predecessor_first():
    """Site COVERAGE, which the spelling ratchet above cannot give.

    That ratchet pins how the two known sites read the predecessor. It says nothing
    about a THIRD allocation site added later: such a site publishes its own id over
    the slot without ever latching, so the store it supersedes is cited by nobody
    and the chain has a gap a walker cannot see -- the same loss this edge exists to
    remove, reintroduced by addition rather than by edit, and silently.

    So this reads the allocation calls instead of the latch calls, and requires each
    one to be preceded by a latch. A new site reds this on the day it is written,
    which is the only moment anyone is in a position to know whether it should
    latch.

    The window is generous (the prefetch's latch sits nine lines above its call, the
    turn's two) and deliberately bounded: a latch hundreds of lines away is not
    evidence about this call.

    The exact count is asserted too, so a third site cannot arrive unnoticed even if
    it happens to sit below an unrelated latch.

    Mutation guard: deleting either latch call, or moving it below its allocation,
    reds this.
    """
    runner = Path(__file__).parent.parent / "src" / "kiro_crew" / "dashboard" / "chat_runner.py"
    lines = runner.read_text(encoding="utf-8").splitlines()
    allocations = [i for i, line in enumerate(lines) if "get_or_create(" in line]
    assert len(allocations) == 2, (
        "chat_runner now allocates a slot's session at a different number of sites; "
        f"each one must latch the predecessor first: lines {[i + 1 for i in allocations]}"
    )
    window = 30
    for index in allocations:
        preceding = lines[max(0, index - window) : index]
        assert any("latch_crew_log_previous(" in line for line in preceding), (
            f"the allocation at line {index + 1} publishes a session id for the slot "
            "without latching the store it supersedes, so that store is left cited "
            "by nobody and a chain walker steps over it"
        )


def test_the_turn_path_reads_the_predecessor_through_the_non_pruning_accessor():
    """A source ratchet, because the two accessors are one identifier apart.

    `mapped_sid` and `resumable_sid` differ by a filesystem stat and a prune, and
    at this call site that difference is a sync store read on the gateway loop
    plus the loss of the very edge being recorded. Both spellings type-check, both
    return the id on the happy path, and every behavioural test of the emitter
    passes either way, because the emitter is handed the value rather than
    choosing it. So the choice is pinned where it is made.

    It is pinned at EVERY site, not one: the slot has two allocation sites -- the
    eager prefetch and the first real turn -- and a spelling that is right at one
    and wrong at the other leaves the prefetched half of the fleet recording no
    edge, which is the shape that shipped broken once already. A site added later
    that feeds the latch from anything else reds this.

    Mutation guard: swapping either call site to `resumable_sid`, or feeding the
    latch from any other source, reds this.
    """
    runner = Path(__file__).parent.parent / "src" / "kiro_crew" / "dashboard" / "chat_runner.py"
    source = runner.read_text(encoding="utf-8")
    # Joined because the call can be wrapped across lines; the whole expression is
    # what this pins, so a line-at-a-time read could not see it.
    flat = " ".join(source.split())
    latches = re.findall(r"slot\.latch_crew_log_previous\([^)]*\)[^)]*\)", flat)
    # Both allocation sites, each reading the mapping through the non-pruning
    # accessor under the session key. The `sessions` receiver differs because the
    # prefetch is handed the boundary directly and the turn reaches it through
    # `state`.
    assert latches == [
        "slot.latch_crew_log_previous(sessions.mapped_sid(session_key))",
        "slot.latch_crew_log_previous(state.sessions.mapped_sid(session_key))",
    ], f"the predecessor is latched somewhere unexpected: {latches}"
    # Spent exactly once, at the emitter call. A second consumer would hand the
    # same edge to two entries; none would leave it for the slot's next store.
    takes = [line.strip() for line in source.splitlines() if "take_crew_log_previous(" in line]
    assert takes == [
        "previous_sid=slot.take_crew_log_previous(),"
    ], f"the predecessor edge is consumed somewhere unexpected: {takes}"


def test_the_predecessor_is_read_without_pruning_the_mapping():
    """The turn path reads the mapped id, not the resumable one.

    `SessionMap.get` answers "can this id still be resumed", so it stats the ACP
    transcript and PRUNES the entry when that file is gone or empty. Both are
    wrong for a history citation: the stat is synchronous store work on the
    gateway loop, and the prune erases the id exactly when the two stores
    disagree -- a crew log unit can outlive a truncated ACP transcript, and that
    unit is the one whose tail most needs closing.

    Mutation guard: routing the turn path back through `resumable_sid` answers
    None here, so the successor records no edge and the predecessor's tail is
    never closed.
    """
    mapping = session_map.SessionMap()
    mapping.set("dashboard:chat-7", SESSION, provider="", cwd="")

    # The history read answers, and it is read FIRST because that is the order the
    # turn path uses -- nothing has pruned the entry yet.
    assert mapping.mapped_sid("dashboard:chat-7") == SESSION

    # The resumable read, on the same live entry, answers None AND removes it:
    # there is no ACP transcript on disk for that id.
    assert mapping.get("dashboard:chat-7") is None
    assert (
        mapping.has_hint("dashboard:chat-7") is False
    ), "get() pruned the entry, which is why the history read must not go through it"


def test_the_mapped_read_touches_no_file():
    """Safe on the gateway loop because it reads memory, not the store.

    An unmapped key answers "" rather than raising, since the emitter turns that
    into an absent edge: a slot's first ever session has nothing to name.
    """
    mapping = session_map.SessionMap()
    mapping.set("dashboard:chat-7", SESSION, provider="", cwd="")

    def _no_disk(*_args, **_kwargs):
        raise AssertionError("mapped_sid touched the filesystem")

    # Patched AFTER construction, which loads the map from disk by design.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "exists", _no_disk)
        patch.setattr(Path, "stat", _no_disk)
        assert mapping.mapped_sid("dashboard:chat-7") == SESSION
        assert mapping.mapped_sid("dashboard:never-seen") == ""


def test_a_store_whose_front_retention_removed_reports_no_edge():
    """`null` is "no edge to follow", never "there was no earlier crew log".

    The edge rides on the creating `session/opened`, which lives at the front of
    the file, and retention deletes whole segments off the front. A reader that
    read a missing edge as "this is the slot's first crew log" would silently
    claim a slot's history began at the oldest segment that survived.
    """
    _open_session()
    assert emit.flush()
    _open_successor(previous_sid=SESSION)
    assert emit.flush()
    assert crew_log_projection.read_projection(SUCCESSOR, "status").value["previous"] == SESSION

    _log_path(SUCCESSOR).unlink()
    folded = crew_log_projection.read_projection(SUCCESSOR, "status").value
    assert folded["previous"] is None
    assert folded["lifecycle"] == "unknown", "and the fold says it could not read an opener"
