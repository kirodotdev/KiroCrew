"""A tree record filed under a slot's older unit after its carry lands in the newest unit.

A slot holding a session-tree or creation record keeps every older unit: its carry stands
down (``emit_session._holds_tree_record``). A record whose writer resolved the older unit
before the reset (``session_handoff._move_tree_edge`` reads the target's id before it
awaits the recipient's) and lands after a carry that closed that unit must not sit there:
the sweep collects it, and the tree and the ownership checks read the record. So the tree
writers decide their unit on the slot's writer lane, the way the carried folds' writers
do (``emit_session._tree_lands_in``, ``late_writes.hand_to_carrier`` under ``TREE_KEY``),
and the record, in the newest unit, stands the slot's next move down.
"""

from __future__ import annotations

import ast
import errno
import json
import time
from pathlib import Path
from typing import Any

import pytest
from crew_log_drain import assert_drained, settle

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import CrewLog, eager, emit, emit_runtime, emit_session, late_writes
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store, store_retention
from kiro_crew.crew_log.errors import CODE_ALREADY_OWNED, CrewLogError
from kiro_crew.crew_log.projection_catalogue import CARRIED_FOLD_NAMES
from kiro_crew.crew_log.session_tree_projection import CHECKPOINT_NAME, SessionTreeProjection
from kiro_crew.crew_log.writer import WriterLimits
from kiro_crew.session_ledger import _store_name
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

WORKER = "worker-1"
WORKER_KEY = f"dashboard:{WORKER}"
CREATOR = "chat-boss"
CREATOR_KEY = f"dashboard:{CREATOR}"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture
def carrying(monkeypatch):
    """The live emitter, the carry's fold inline, and a probe whose map is the yielded dict."""
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    stp.reset_for_tests()
    monkeypatch.setattr(emit, "_submit_carry_fold", lambda job: job())
    mapped: dict[str, str] = {}
    emit.set_session_key_probe(mapped.get)
    try:
        yield mapped
    finally:
        emit.set_session_key_probe(None)
        settle()
        tree = stp.projection()
        stp.reset_for_tests()
        wait_until(
            lambda: not tree._write_scheduled,
            describe=lambda: "the session tree's debounced checkpoint is still sleeping",
        )


def _live_parent(slot: str) -> "str | None":
    node = stp.projection().nodes().get(slot)
    return None if node is None else node.parent_slot


def _cold_parent(slot: str) -> "str | None":
    """The parent a fresh process folds from the logs alone, with no checkpoint."""
    checkpoint = lg.crew_log_root(lg.KIND_SESSION) / "projections" / CHECKPOINT_NAME
    checkpoint.unlink(missing_ok=True)
    cold = SessionTreeProjection()
    cold.ensure_seeded()
    node = cold.nodes().get(slot)
    return None if node is None else node.parent_slot


def _entries(unit_id: str) -> "list[dict]":
    path = lg.crew_log_root(lg.KIND_SESSION) / _store_name(unit_id) / "log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _adoptions_naming(parent: str) -> "list[str]":
    """The units holding a ``session/adopted`` that names *parent*."""
    return [
        unit
        for unit in ("w1", "w2")
        for entry in _entries(unit)
        if entry["type"] == "session/adopted" and entry["data"]["parent"]["slot"] == parent
    ]


def _carried(carrying: "dict[str, str]", *, still_mapped: bool) -> None:
    """``w1``, a root that holds no tree record, then ``w2``, which carries the slot."""
    if still_mapped:
        carrying["w1"] = "dashboard:imported-copy"
    emit.on_session_opened("w1", agent="kirocrew", slot=WORKER, session_key=WORKER_KEY)
    assert_drained()
    session_ledger.record_update(WORKER, session_id="w1", goal="g", event="e1")
    assert_drained()
    emit.on_session_opened(
        "w2", agent="kirocrew", slot=WORKER, previous_sid="w1", session_key=WORKER_KEY
    )
    assert_drained()
    assert any(e["type"] == "session/carried" for e in _entries("w2")), "the control"
    assert store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, "w1")) is not still_mapped
    assert _live_parent(WORKER) is None


def _decide(decision: str, settled: "list[bool]") -> "str | None":
    """File *decision* under ``w1``, as a writer that resolved it before the reset does."""
    if decision == "adopted":
        emit.on_session_adopted(
            "w1",
            slot=WORKER,
            parent_slot="chat-c",
            on_settled=settled.append,
        )
        return "chat-c"
    emit.on_session_released("w1", slot=WORKER, on_settled=settled.append)
    return None


@pytest.mark.parametrize("still_mapped", [False, True], ids=["closed-superseded", "still-mapped"])
@pytest.mark.parametrize("decision", ["adopted", "released"])
def test_a_decision_filed_under_the_older_unit_after_the_carry_lands_in_the_newest(
    carrying, decision, still_mapped
):
    """The carry's own note hands the decision on (``late_writes.note_carried``, TREE_KEY)."""
    _carried(carrying, still_mapped=still_mapped)

    settled: list[bool] = []
    want = _decide(decision, settled)
    assert_drained()

    assert settled == [True]
    assert [e["type"] for e in _entries("w1")][-1] != f"session/{decision}"
    assert any(e["type"] == f"session/{decision}" for e in _entries("w2"))
    assert _live_parent(WORKER) == want
    assert _cold_parent(WORKER) == want
    assert eager.drain(timeout=FALLBACK_TIMEOUT_SECS), "the eager folder never settled"
    emit.reset_caches()
    store.sweep_expired(0, now=time.time() + 60)
    assert CrewLog.exists(lg.KIND_SESSION, "w2")
    assert CrewLog.exists(lg.KIND_SESSION, "w1") is still_mapped
    assert _cold_parent(WORKER) == want


@pytest.mark.parametrize("decision", ["adopted", "released"])
def test_a_decision_filed_under_the_older_unit_after_a_restart_is_the_tree(carrying, decision):
    """A restart between the carry and the decision forgets the carry's note.

    The older unit is still mapped, so it is not closed either, and only the newer unit's
    log can say the slot moved on: the job reads it there (``late_writes._read_carries``).
    """
    _carried(carrying, still_mapped=True)
    emit.reset_caches()

    settled: list[bool] = []
    want = _decide(decision, settled)
    assert_drained()

    assert settled == [True]
    assert _cold_parent(WORKER) == want
    assert [e["type"] for e in _entries("w1")][-1] != f"session/{decision}"


def test_a_refused_handed_on_decision_is_told_false_once_and_lands_nothing(carrying, monkeypatch):
    """A refusal in the carrier is final: told once, and the follow-on's requeued copy stops."""
    _carried(carrying, still_mapped=True)
    real = emit_runtime._handle
    refused: "list[str]" = []

    def _refuses_once(session_id: str) -> Any:
        if session_id == "w2" and not refused:
            refused.append(session_id)
            raise CrewLogError("another process owns this log", code=CODE_ALREADY_OWNED)
        return real(session_id)

    settled: list[bool] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_handle", _refuses_once)
        _decide("adopted", settled)
        assert_drained()

    assert refused == ["w2"]
    assert settled == [False]
    assert _adoptions_naming("chat-c") == []
    assert _cold_parent(WORKER) is None


class _DiskFull:
    """A crew log whose tree appends fail with ENOSPC every time; the rest reach the file."""

    def __init__(self, real: Any, failed: "list[str]") -> None:
        self._real = real
        self._failed = failed

    def append(self, entry_type: str, *args: Any, **kwargs: Any) -> Any:
        if entry_type in emit_session._TREE_RECORD_TYPES:
            self._failed.append(entry_type)
            raise OSError(errno.ENOSPC, "No space left on device")
        return self._real.append(entry_type, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_a_handed_on_decision_the_writer_gives_up_on_is_told_false_once(carrying):
    """The follow-on that carries a handed-on decision tells its drop to the caller.

    The first job handed the entry to the newest unit, so its own ``after`` tells
    nothing; when the follow-on and its requeued copy both spend their attempt budget,
    the drop hook the follow-on carries tells ``False``, once, on the final drop.
    Otherwise the dashboard's tree fence on the slot would never come down.
    """
    limits = WriterLimits(retry_backoff_seconds=0.001, max_write_attempts=2)
    emit.reset_caches(limits=limits)
    _carried(carrying, still_mapped=True)
    real = emit_runtime._handle
    failed: "list[str]" = []

    def _full_in_the_newest(session_id: str) -> Any:
        log = real(session_id)
        return _DiskFull(log, failed) if session_id == "w2" and log is not None else log

    settled: list[bool] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_handle", _full_in_the_newest)
        _decide("adopted", settled)
        assert_drained()
        wait_until(
            lambda: settled,
            describe=lambda: "the handed-on adoption's caller was never told its drop",
        )
        assert_drained()

    assert settled == [False]
    # The follow-on, then its one requeued copy, each spent the whole attempt budget.
    assert failed == ["session/adopted"] * (2 * limits.max_write_attempts)
    assert _adoptions_naming("chat-c") == []
    assert _live_parent(WORKER) is None


def test_a_spawn_filed_under_the_creators_closed_unit_keeps_the_child_through_retention(
    carrying,
):
    """A late spawn filed under a closed ``superseded`` unit would go with it to retention.

    The child has no log yet, so the creator's ``session/spawned`` is its only creating
    edge, and the closed unit is collected however its tail grew after the close, once
    no handle holds its lease (the restart here, or the handle cache's own bound).
    """
    emit.on_session_opened("c1", agent="kirocrew", slot=CREATOR, session_key=CREATOR_KEY)
    assert_drained()
    emit.on_session_opened(
        "c2", agent="kirocrew", slot=CREATOR, previous_sid="c1", session_key=CREATOR_KEY
    )
    assert_drained()
    assert store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, "c1"))

    settled: list[bool] = []
    emit.on_session_spawned(
        "c1", creator_slot=CREATOR, child_slot="chat-kid", on_settled=settled.append
    )
    assert_drained()

    assert settled == [True]
    assert eager.drain(timeout=FALLBACK_TIMEOUT_SECS), "the eager folder never settled"
    emit.reset_caches()
    store.sweep_expired(0, now=time.time() + 60)
    assert not CrewLog.exists(lg.KIND_SESSION, "c1"), "the closed unit was collected"
    assert _cold_parent("chat-kid") == CREATOR
    assert any(e["type"] == "session/spawned" for e in _entries("c2"))

    # The spawn now sits in ``c2``, so the slot's next move stands down and keeps ``c2``.
    emit.on_session_opened(
        "c3", agent="kirocrew", slot=CREATOR, previous_sid="c2", session_key=CREATOR_KEY
    )
    assert_drained()
    assert not any(e["type"] == "session/carried" for e in _entries("c3"))
    assert not store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, "c2"))
    store.sweep_expired(0, now=time.time() + 60)
    assert CrewLog.exists(lg.KIND_SESSION, "c2")
    assert _cold_parent("chat-kid") == CREATOR


def _adopt_in_place(unit: str) -> None:
    """Append an adoption straight into *unit*, as a writer that resolved it earlier does.

    Through the emitter's own cached handle, on the writer thread the caller runs on, so
    nothing is queued and the record lands exactly where the hook fires.
    """
    log = emit_runtime._handle(unit)
    assert log is not None
    log.append("session/adopted", {"parent": {"slot": "chat-z"}}, src="gateway")
    del log


@pytest.mark.parametrize("where", ["after-the-stand-down-read", "before-the-close"])
def test_an_adoption_landing_in_the_older_unit_mid_carry_keeps_it_open(
    carrying, monkeypatch, where
):
    """A tree record that lands in an older unit after the stand-down read keeps it.

    After the read and before the carry is written, the unit's tail moved, so the carry
    folds again and the second read stands it down; after the carry and before the
    close, the close finds the tail moved and is withheld. Either way the unit is not
    closed ``superseded``. Driven by a hook at each point, no sleeps.
    """
    emit.on_session_opened("w1", agent="kirocrew", slot=WORKER, session_key=WORKER_KEY)
    assert_drained()
    session_ledger.record_update(WORKER, session_id="w1", goal="g", event="e1")
    assert_drained()
    fired: list[str] = []
    if where == "after-the-stand-down-read":
        real_read = emit._holds_tree_record

        def _read_then_adopt(session_id: str, slot: str, older: "tuple[str, ...]") -> bool:
            held = real_read(session_id, slot, older)
            if not fired:
                fired.append(slot)
                _adopt_in_place("w1")
            return held

        monkeypatch.setattr(emit, "_holds_tree_record", _read_then_adopt)
    else:
        real_close = emit._close_superseded

        def _adopt_then_close(unit_id: str, *args: Any) -> None:
            if not fired:
                fired.append(unit_id)
                _adopt_in_place(unit_id)
            real_close(unit_id, *args)

        monkeypatch.setattr(emit, "_close_superseded", _adopt_then_close)
    emit.on_session_opened(
        "w2", agent="kirocrew", slot=WORKER, previous_sid="w1", session_key=WORKER_KEY
    )
    assert_drained()

    assert fired, "the hook never fired"
    assert not store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, "w1"))
    emit.reset_caches()
    store.sweep_expired(0, now=time.time() + 60)
    assert CrewLog.exists(lg.KIND_SESSION, "w1")
    assert _cold_parent(WORKER) == "chat-z"


# ── the hand-over is the only way a tree entry is appended ─────────────────────


def _callee(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _session_tree_appends() -> "tuple[list[tuple[str, str]], set[str], ast.Module]":
    """Every ``(function, entry type)`` a literal ``append`` of a tree type names, the
    top-level functions that ask ``_tree_lands_in``, and the module. Read off its syntax."""
    module = ast.parse(Path(emit_session.__file__).read_text(encoding="utf-8"))
    appends: "list[tuple[str, str]]" = []
    landing: "set[str]" = set()
    for top in module.body:
        if not isinstance(top, (ast.FunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(top):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee(node)
            if callee == "_tree_lands_in":
                landing.add(top.name)
            if callee == "append" and node.args:
                named = node.args[0]
                if (
                    isinstance(named, ast.Constant)
                    and named.value in emit_session._TREE_RECORD_TYPES
                ):
                    appends.append((top.name, named.value))
    return appends, landing, module


def test_every_tree_record_is_appended_through_the_hand_over():
    """A tree record type is appended only by a job that asks the slot's lane.

    Its caller names a unit before the job runs, so an append that skips the hand-over
    is filed behind a carry that landed in between, in a unit closed for the sweep.
    """
    # The types the hand-over names are the ones the stand-down reads beside the opening.
    copied = store_retention._TREE_EDGE_TYPES | {store_retention._SPAWNED_TYPE}
    assert emit_session._TREE_RECORD_TYPES == copied
    assert late_writes.TREE_KEY not in CARRIED_FOLD_NAMES

    appends, landing, module = _session_tree_appends()
    # Controls: the extractor reads both sides, and the check can fail.
    assert {entry_type for _function, entry_type in appends} == copied
    assert {"on_session_adopted", "on_session_released", "on_session_spawned"} <= landing

    offenders = sorted(f"{fn}: {entry_type}" for fn, entry_type in appends if fn not in landing)
    assert not offenders, (
        "these appends of a tree record skip the slot-lane hand-over, so a record resolved "
        f"before a carry lands in a unit closed for the sweep: {offenders}"
    )
    lands_in = next(
        top
        for top in module.body
        if isinstance(top, ast.FunctionDef) and top.name == "_tree_lands_in"
    )
    asks = [
        node
        for node in ast.walk(lands_in)
        if isinstance(node, ast.Call) and _callee(node) == "hand_to_carrier"
    ]
    assert asks and any(
        isinstance(arg, ast.Tuple)
        and any(isinstance(item, ast.Attribute) and item.attr == "TREE_KEY" for item in arg.elts)
        for call in asks
        for arg in call.args
    ), "_tree_lands_in no longer hands a tree entry over under TREE_KEY"
