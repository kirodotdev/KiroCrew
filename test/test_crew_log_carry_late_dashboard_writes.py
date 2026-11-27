"""A dashboard write that resolved a unit before its slot's carry lands where the carry is.

The custom-record route (``agent_panel_record.record_through_door``, shared by the cron and
the installed-app record routes) and the agentic write route resolve the crewmate's newest
DM unit (``member_dashboard._newest_unit``) before their writer job runs, and the crewmate
feed resolves its session before ``on_goal_recorded`` / ``on_waiting_changed`` queue. A
reset can open the slot's next unit and commit its carry in between. A write that still
lands in the older unit is acknowledged, then lost behind the carry: the slot's fold
replaces what the older unit gave it with the carried state, so the page keeps showing the
earlier value.

Each case runs twice: with the older unit closed ``superseded`` once the carry landed, and
with a session-map key still mapping it, so it stays open and only the carry's own record
of what it carried (``late_writes.note_carried``) can say the slot moved past it. The
closed unit's slot key is dropped with its close, so a feed write to it reads the unit's
header on the writer (``late_writes.requeue_on_slot_lane``).
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from crew_log_drain import assert_drained, settle

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import emit, emit_runtime, emit_surfaces, late_writes, projection
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store
from kiro_crew.crew_log.entry_types import (
    CUSTOM_FOLD_NAME,
    DASHBOARD_FOLD_NAME,
    GOAL_FOLD_NAME,
    MISTAKES_FOLD_NAME,
    SESSION_ENTRY_TYPES,
    WAITING_FOLD_NAME,
)
from kiro_crew.crew_log.errors import CODE_ALREADY_OWNED, CrewLogError
from kiro_crew.crew_log.projection_catalogue import CARRIED_FOLD_NAMES, CARRIED_SESSION_FOLD_NAMES
from kiro_crew.crew_log.writer import JobKind
from kiro_crew.session_ledger import _store_name
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

DASH = "member-late-dash"
DASH_SESSION_KEY = f"dashboard:{DASH}"
WRITER = "cron:late-dash"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture
def carrying(monkeypatch):
    """The live emitter and the carry folded inline (as ``test_crew_log_carry_late_board_writes``)."""
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    stp.reset_for_tests()
    monkeypatch.setattr(emit, "_submit_carry_fold", lambda job: job())
    monkeypatch.setattr(stp.SessionTreeProjection, "_schedule_checkpoint", lambda self: None)
    try:
        yield
    finally:
        emit.set_session_key_probe(None)
        settle()
        stp.reset_for_tests()


@pytest.fixture(params=["closed", "mapped"])
def older(request) -> str:
    """Whether the older unit is closed ``superseded`` after the carry or kept by a key."""
    return request.param


def _probe(older: str, unit: str, key: str) -> None:
    if older == "mapped":
        emit.set_session_key_probe(lambda session_id: key if session_id == unit else None)
    else:
        emit.set_session_key_probe(lambda session_id: None)


def _closed(unit: str) -> bool:
    return store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, unit))


def _custom(value: str) -> dict:
    return {
        "type": "uptime",
        "data": {"value": value},
        "writer": WRITER,
        "member": DASH,
        "crew_key": "a" * 40,
    }


def _custom_failed(error: str) -> dict:
    return {"writer": WRITER, "error": error, "member": DASH, "crew_key": "a" * 40}


def _agentic(value: int) -> dict:
    return {
        "field": "open_prs",
        "type": "number",
        "value": {"v": value},
        "instance_version": 1,
        "crew_key": "a" * 40,
    }


def _refused(field: str) -> dict:
    return {"code": "unknown_field", "field": field, "reason": f"no field {field}"}


def _goal(text: str) -> dict:
    return {"goals": [{"text": text}], "milestones": []}


def _waiting(text: str) -> dict:
    return {"items": [{"kind": "question", "text": text}]}


def _reset(older: str) -> None:
    """``d-b`` opens for the slot after ``d-a`` and carries it."""
    emit.on_session_opened(
        "d-b", agent="kirocrew", slot=DASH, previous_sid="d-a", session_key=DASH_SESSION_KEY
    )
    assert_drained()
    assert _closed("d-a") is (older == "closed")


def _open(older: str) -> None:
    _probe(older, "d-a", DASH_SESSION_KEY)
    emit.on_session_opened("d-a", agent="kirocrew", slot=DASH)
    assert_drained()


def _fold(name: str) -> dict:
    projection.forget_slot_folds()
    return projection.read_slot_projection(DASH, name).value


def _types_in(unit: str) -> "list[str]":
    """The entry types *unit*'s log holds, read off the file (as ``test_crew_log_slot_carry``)."""
    path = lg.crew_log_root(lg.KIND_SESSION) / _store_name(unit) / "log.jsonl"
    return [json.loads(line)["type"] for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.usefixtures("carrying")
def test_a_custom_update_resolved_to_the_older_unit_reads_after_the_carry(older):
    """The GPT trigger: the record route resolved ``d-a``, then the reset carried the slot."""
    _open(older)
    assert emit.on_dashboard_custom("d-a", _custom("first"), timeout=FALLBACK_TIMEOUT_SECS)
    assert_drained()
    _reset(older)

    # The route still holds ``d-a`` from ``_newest_unit`` and is told the write landed.
    assert emit.on_dashboard_custom("d-a", _custom("second"), timeout=FALLBACK_TIMEOUT_SECS)
    assert_drained()

    custom = _fold(CUSTOM_FOLD_NAME)
    assert custom["latest"]["uptime"]["data"] == {"value": "second"}
    assert custom["wrote"] == 2


@pytest.mark.usefixtures("carrying")
def test_a_cron_failure_resolved_to_the_older_unit_marks_the_carried_row_failed(older):
    """``custom_failed`` feeds the carried ``custom`` fold too: the row it marks is the carry's."""
    _open(older)
    assert emit.on_dashboard_custom("d-a", _custom("first"), timeout=FALLBACK_TIMEOUT_SECS)
    assert_drained()
    _reset(older)

    assert emit.on_dashboard_custom_failed(
        "d-a", _custom_failed("disk full"), timeout=FALLBACK_TIMEOUT_SECS
    )
    assert_drained()

    custom = _fold(CUSTOM_FOLD_NAME)
    assert custom["latest"]["uptime"]["last_error"] == "disk full"
    assert [row["writer"] for row in custom["failures"]] == [WRITER]
    assert "dashboard/custom_failed" in _types_in("d-b")


@pytest.mark.usefixtures("carrying")
def test_an_agentic_value_resolved_to_the_older_unit_reads_after_the_carry(older):
    """The agentic write route resolves its unit the same way."""
    _open(older)
    assert emit.on_dashboard_agentic("d-a", _agentic(1), timeout=FALLBACK_TIMEOUT_SECS)
    assert_drained()
    _reset(older)

    assert emit.on_dashboard_agentic("d-a", _agentic(2), timeout=FALLBACK_TIMEOUT_SECS)
    assert_drained()

    agentic = _fold(DASHBOARD_FOLD_NAME)
    assert agentic["fields"]["open_prs"]["value"] == 2
    assert agentic["wrote"] == 2


@pytest.mark.usefixtures("carrying")
def test_a_refusal_resolved_to_the_older_unit_reaches_the_carried_mistake_book(older):
    """``agentic_refused`` feeds the carried ``mistakes`` fold: the book counts both refusals."""
    _open(older)
    assert emit.on_dashboard_refused("d-a", _refused("prs"), timeout=FALLBACK_TIMEOUT_SECS)
    assert_drained()
    _reset(older)

    assert emit.on_dashboard_refused("d-a", _refused("prs"), timeout=FALLBACK_TIMEOUT_SECS)
    assert_drained()

    mistakes = _fold(MISTAKES_FOLD_NAME)
    assert mistakes["refused"] == 2
    assert [(group["field"], group["count"]) for group in mistakes["groups"]] == [("prs", 2)]


def _settled_write(kind: str, unit: str, data: dict) -> "list[bool]":
    answers: list[bool] = []
    if kind == "goal":
        emit.on_goal_recorded(unit, data, on_settled=answers.append)
    else:
        emit.on_waiting_changed(unit, data, on_settled=answers.append)
    assert_drained()
    return answers


@pytest.mark.usefixtures("carrying")
def test_a_goal_snapshot_resolved_to_the_older_unit_reads_after_the_carry(older):
    """The crewmate feed's goal snapshot feeds the carried ``goal`` fold."""
    _open(older)
    assert _settled_write("goal", "d-a", _goal("ship v1")) == [True]
    _reset(older)

    assert _settled_write("goal", "d-a", _goal("ship v2")) == [True]

    goal = _fold(GOAL_FOLD_NAME)
    assert goal["goal"] == "ship v2"
    assert goal["changes"] == 2


@pytest.mark.usefixtures("carrying")
def test_a_waiting_list_resolved_to_the_older_unit_reads_after_the_carry(older):
    """The crewmate feed's waiting list feeds the carried ``waiting`` fold."""
    _open(older)
    assert _settled_write("waiting", "d-a", _waiting("approve the plan")) == [True]
    _reset(older)

    assert _settled_write("waiting", "d-a", _waiting("review the PR")) == [True]

    waiting = _fold(WAITING_FOLD_NAME)
    assert [row["text"] for row in waiting["items"]] == ["review the PR"]
    assert waiting["changes"] == 2


def _feed_write(kind: str, unit: str, value: str, answers: "list[bool]") -> None:
    if kind == "goal":
        emit.on_goal_recorded(unit, _goal(value), on_settled=answers.append)
    else:
        emit.on_waiting_changed(unit, _waiting(value), on_settled=answers.append)


def _feed_value(kind: str) -> "tuple[str, int]":
    if kind == "goal":
        goal = _fold(GOAL_FOLD_NAME)
        return goal["goal"], goal["changes"]
    waiting = _fold(WAITING_FOLD_NAME)
    return waiting["newest"], waiting["changes"]


@pytest.mark.parametrize("kind", ["goal", "waiting"])
@pytest.mark.usefixtures("carrying")
def test_a_feed_write_handed_to_the_carrier_is_settled_once_with_true(older, kind):
    """*on_settled* is told by the job that appended in ``d-b``, never by the one that handed on."""
    _open(older)
    _feed_write(kind, "d-a", "v1", [])
    assert_drained()
    _reset(older)
    handed: "list[str]" = []
    real = emit_runtime._hand_over

    def _spy(unit: str, job: Any) -> None:
        if job.kind is JobKind.FOLLOW_ON:
            handed.append(unit)
        real(unit, job)

    answers: list[bool] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_hand_over", _spy)
        _feed_write(kind, "d-a", "v2", answers)
        assert_drained()

    assert handed[-1] == "d-b", "the entry was handed to the carrier"
    assert answers == [True]
    assert _feed_value(kind) == ("v2", 2)
    assert f"dashboard/{'goal_recorded' if kind == 'goal' else 'waiting_changed'}" in _types_in(
        "d-b"
    )


def _on_a_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.parametrize("kind", ["goal", "waiting"])
@pytest.mark.usefixtures("carrying")
def test_a_feed_write_reads_no_header_on_the_serving_loop(older, kind, monkeypatch):
    """The feed calls from the loop: the unit's header is read by its job, on the writer."""
    _open(older)
    _feed_write(kind, "d-a", "v1", [])
    assert_drained()
    _reset(older)
    reads: "list[bool]" = []
    real = session_ledger.canonical_slot

    def _guarded(slot_key: str, session_id: str) -> str:
        reads.append(_on_a_loop())
        return real(slot_key, session_id)

    monkeypatch.setattr(session_ledger, "canonical_slot", _guarded)
    answers: list[bool] = []

    async def _from_the_loop() -> None:
        _feed_write(kind, "d-a", "v2", answers)

    asyncio.run(_from_the_loop())
    assert_drained()

    assert True not in reads, "a header was read on the serving loop"
    # The closed unit's key went with its close, so its header is read once, off the loop.
    assert reads == ([False] if older == "closed" else [])
    assert answers == [True]
    assert _feed_value(kind) == ("v2", 2)


@pytest.mark.usefixtures("carrying")
def test_a_handed_on_job_that_failed_once_is_settled_once_after_its_retry(older, monkeypatch):
    """A follow-on retried after a failed append is told once, by the attempt that landed."""
    _open(older)
    _feed_write("goal", "d-a", "plan", [])
    assert_drained()
    _reset(older)
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    real = emit_runtime._handle
    failed: "list[str]" = []

    def _fails_once(session_id: str) -> Any:
        if session_id == "d-b" and not failed:
            failed.append(session_id)
            raise OSError("the disk stopped answering")
        return real(session_id)

    answers: list[bool] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_handle", _fails_once)
        emit.on_goal_recorded("d-a", _goal("ship"), on_settled=answers.append)
        assert_drained()

    assert failed == ["d-b"]
    assert answers == [True]
    assert _feed_value("goal") == ("ship", 2)


@pytest.mark.usefixtures("carrying")
def test_a_refused_handed_on_job_is_settled_false_once_and_lands_nothing(older, monkeypatch):
    """A refusal in the carrier is final: told False once, and its requeued copy appends nothing."""
    _open(older)
    _feed_write("goal", "d-a", "plan", [])
    assert_drained()
    _reset(older)
    real = emit_runtime._handle
    refused: "list[str]" = []

    def _refuses_once(session_id: str) -> Any:
        if session_id == "d-b" and not refused:
            refused.append(session_id)
            raise CrewLogError("another process owns this log", code=CODE_ALREADY_OWNED)
        return real(session_id)

    answers: list[bool] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_handle", _refuses_once)
        emit.on_goal_recorded("d-a", _goal("ship"), on_settled=answers.append)
        assert_drained()

    assert refused == ["d-b"]
    assert answers == [False]
    assert _feed_value("goal") == ("plan", 1)
    assert (_types_in("d-a") + _types_in("d-b")).count("dashboard/goal_recorded") == 1


class _RepairOwed:
    """A tracker that owes *unit* a tail repair, the case ``_settle_rejected`` skips the drop hook in."""

    def __init__(self, unit: str) -> None:
        self.unit = unit
        self.taken: "list[str]" = []

    def repair_debt(self, session_id: str) -> str:
        return DASH if session_id == self.unit else ""

    def take_repair_debt(self, session_id: str) -> None:
        self.taken.append(session_id)


@pytest.mark.parametrize("repair_owed", [False, True], ids=["drop-hook", "repair-owed"])
@pytest.mark.usefixtures("carrying")
def test_a_feed_write_the_writer_rejects_at_its_ceiling_is_settled_false_once(repair_owed):
    """A rejected first job never runs: *on_settled* is still told, through its drop or its finish."""
    _open("closed")
    real = emit_runtime._hand_over

    def _rejects_the_append(unit: str, job: Any) -> None:
        if unit == "d-a" and job.kind is JobKind.APPEND:
            emit_runtime._settle_rejected(unit, job)
            return
        real(unit, job)

    answers: list[bool] = []
    owed = _RepairOwed("d-a")
    repairs: "list[tuple[str, str]]" = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_hand_over", _rejects_the_append)
        if repair_owed:
            patch.setattr(emit_runtime, "_tracker_instance", owed)
            patch.setattr(emit, "_queue_tail_repair", lambda sid, slot: repairs.append((sid, slot)))
        emit.on_goal_recorded("d-a", _goal("ship"), on_settled=answers.append)
    assert_drained()

    assert answers == [False]
    assert owed.taken == (["d-a"] if repair_owed else [])
    assert repairs == ([("d-a", DASH)] if repair_owed else [])
    assert "dashboard/goal_recorded" not in _types_in("d-a")


def _waiting_write(kind: str, unit: str, value: str, timeout: float) -> bool:
    if kind == "custom":
        return emit.on_dashboard_custom(unit, _custom(value), timeout=timeout)
    return emit.on_dashboard_agentic(unit, _agentic(int(value)), timeout=timeout)


def _waiting_value(kind: str) -> "tuple[Any, int]":
    if kind == "custom":
        custom = _fold(CUSTOM_FOLD_NAME)
        return custom["latest"]["uptime"]["data"]["value"], custom["wrote"]
    agentic = _fold(DASHBOARD_FOLD_NAME)
    return str(agentic["fields"]["open_prs"]["value"]), agentic["wrote"]


@pytest.mark.parametrize("kind", ["custom", "agentic"])
@pytest.mark.usefixtures("carrying")
def test_a_dashboard_waiter_that_gave_up_before_the_handed_on_job_ran_gets_nothing_landed(
    older, kind
):
    """``False`` is final across the hand-over: the newer unit's job appends nothing."""
    _open(older)
    assert _waiting_write(kind, "d-a", "1", FALLBACK_TIMEOUT_SECS)
    assert_drained()
    _reset(older)
    held: "list[tuple[str, Any]]" = []
    real = emit_runtime._hand_over

    def _hold_follow_ons(unit: str, job: Any) -> None:
        if job.kind is JobKind.FOLLOW_ON:
            held.append((unit, job))
            return
        real(unit, job)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_hand_over", _hold_follow_ons)
        # The handed-on job is held, so nothing can wake this waiter: a 0 s wait gives up.
        answered = _waiting_write(kind, "d-a", "2", 0.0)

    assert [unit for unit, _job in held] == ["d-b"], "the job was handed to the carrier"
    assert answered is False
    for unit, job in held:
        real(unit, job)
    assert_drained()

    assert _waiting_value(kind) == ("1", 1)


#: The append primitives of ``emit_surfaces``, each mapped to the index of the positional
#: argument naming the entry type (``test_crew_log_types._EMIT_PRIMITIVES``' shape).
_APPENDS = {"_write": 1, "append": 0, "_append_dashboard": 1, "_record_on_slot_lane": 1}


def _callee(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _surface_appends() -> "tuple[list[tuple[str, str, str]], set[str]]":
    """Every ``(function, entry type, callee)`` a literal append names, and the landing functions.

    A landing function is a top-level definition whose body asks
    ``late_writes.hand_to_carrier``, nested job included. Read off the module's syntax.
    """
    tree = ast.parse(Path(emit_surfaces.__file__).read_text(encoding="utf-8"))
    appends: list[tuple[str, str, str]] = []
    landing: set[str] = set()
    for top in tree.body:
        if not isinstance(top, (ast.FunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(top):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee(node)
            if callee == "hand_to_carrier":
                landing.add(top.name)
            index = _APPENDS.get(callee, -1)
            if 0 <= index < len(node.args):
                named = node.args[index]
                if isinstance(named, ast.Constant) and named.value in SESSION_ENTRY_TYPES:
                    appends.append((top.name, named.value, callee))
    return appends, landing


def _off_the_lane(appends: "list[tuple[str, str, str]]", landing: "set[str]") -> "list[str]":
    slot_keyed = CARRIED_FOLD_NAMES - set(CARRIED_SESSION_FOLD_NAMES)
    return sorted(
        f"{function}: {entry_type}"
        for function, entry_type, callee in appends
        if set(late_writes.folds_fed_by(entry_type)) & slot_keyed
        and function not in landing
        and callee not in landing
    )


def test_every_surface_write_that_feeds_a_slot_keyed_carried_fold_lands_on_the_slot_lane():
    """A type that feeds a slot-keyed carried fold is appended only through the hand-over.

    Its caller names a unit before the job runs, so an append that does not ask
    ``hand_to_carrier`` on the slot's lane is lost behind a carry that landed in between.
    """
    appends, landing = _surface_appends()
    types = {entry_type for _function, entry_type, _callee in appends}

    # Controls: the extractor reads both sides, and the check can fail.
    assert {"panel/published", "work/recorded", "dashboard/goal_recorded", "card/proposed"} <= types
    assert {"_record_on_slot_lane", "on_panel_published", "on_work_recorded"} <= landing
    assert "_append_dashboard" in landing
    assert "on_goal_recorded: dashboard/goal_recorded" in _off_the_lane(appends, set())

    offenders = _off_the_lane(appends, landing)
    assert not offenders, (
        "these appends feed a slot-keyed carried fold and skip the slot-lane hand-over, so a "
        f"write resolved before a carry is lost behind it: {offenders}"
    )
