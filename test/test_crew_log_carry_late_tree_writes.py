"""A tree decision filed under a slot's older unit after its tree carry still decides.

The slot carry copies the slot's newest ``session/adopted`` or ``session/released`` into
the successor (``tree_carry.tree_carries``), and the tree ranks a newer log's decision
above an older log's (``session_tree.edge_supersedes``). A decision whose writer resolved
the older unit before the reset (``session_handoff._move_tree_edge`` reads the target's
id before it awaits the recipient's) and lands after the carry must not lose to the copy
of the decision it replaced: the caller was told the tree moved. So the tree writers
decide their unit on the slot's writer lane, the way the carried folds' writers do
(``emit_session._tree_lands_in``, ``late_writes.hand_to_carrier`` under ``TREE_KEY``).
"""

from __future__ import annotations

import ast
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


def _adopted_then_carried(carrying: "dict[str, str]", *, still_mapped: bool) -> None:
    """``w1`` is adopted by ``chat-b``, then ``w2`` carries that adoption."""
    if still_mapped:
        carrying["w1"] = "dashboard:imported-copy"
    emit.on_session_opened(
        "w1", agent="kirocrew", slot=WORKER, parent_slot="chat-a", session_key=WORKER_KEY
    )
    assert_drained()
    emit.on_session_adopted("w1", slot=WORKER, parent_slot="chat-b", previous_parent_slot="chat-a")
    assert_drained()
    session_ledger.record_update(WORKER, session_id="w1", goal="g", event="e1")
    assert_drained()
    emit.on_session_opened(
        "w2",
        agent="kirocrew",
        slot=WORKER,
        parent_slot="chat-a",
        previous_sid="w1",
        session_key=WORKER_KEY,
    )
    assert_drained()
    assert _live_parent(WORKER) == "chat-b"


def _decide(decision: str, settled: "list[bool]") -> "str | None":
    """File *decision* under ``w1``, as a writer that resolved it before the reset does."""
    if decision == "adopted":
        emit.on_session_adopted(
            "w1",
            slot=WORKER,
            parent_slot="chat-c",
            previous_parent_slot="chat-b",
            on_settled=settled.append,
        )
        return "chat-c"
    emit.on_session_released(
        "w1", slot=WORKER, previous_parent_slot="chat-b", on_settled=settled.append
    )
    return None


@pytest.mark.parametrize("still_mapped", [False, True], ids=["closed-superseded", "still-mapped"])
@pytest.mark.parametrize("decision", ["adopted", "released"])
def test_a_decision_filed_under_the_older_unit_after_the_tree_carry_is_the_tree(
    carrying, decision, still_mapped
):
    _adopted_then_carried(carrying, still_mapped=still_mapped)

    settled: list[bool] = []
    want = _decide(decision, settled)
    assert_drained()

    assert settled == [True]
    assert _live_parent(WORKER) == want
    assert _cold_parent(WORKER) == want
    assert eager.drain(timeout=FALLBACK_TIMEOUT_SECS), "the eager folder never settled"
    store.sweep_expired(0, now=time.time() + 60)
    assert CrewLog.exists(lg.KIND_SESSION, "w2")
    assert _cold_parent(WORKER) == want


@pytest.mark.parametrize("decision", ["adopted", "released"])
def test_a_decision_filed_under_the_older_unit_after_a_restart_is_the_tree(carrying, decision):
    """A restart between the carry and the decision forgets the carry's note.

    The older unit is still mapped, so it is not closed either, and only the newer unit's
    log can say the slot moved on: the job reads it there (``late_writes._read_carries``).
    """
    _adopted_then_carried(carrying, still_mapped=True)
    emit.reset_caches()

    settled: list[bool] = []
    want = _decide(decision, settled)
    assert_drained()

    assert settled == [True]
    assert _cold_parent(WORKER) == want
    assert [e["type"] for e in _entries("w1")][-1] != "session/adopted"


@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "after-restart"])
def test_a_decision_filed_under_the_older_unit_loses_to_no_edge_the_carrier_holds_itself(
    carrying, monkeypatch, restart
):
    """The carrier holds an edge of its own, so its carry copies no tree entry, only folds.

    A decision filed under the older unit would still lose to that edge, so the tree hands
    over whatever the carry held of it (``emit_session._write_carries`` notes the tree on
    every carry, and the read after a restart takes any of the slot's carries for it).
    A ledger write before the carry has already read ``w2`` and found no carry there, so in
    the same process only the carry's own note can say the slot moved on.
    """
    carrying["w1"] = "dashboard:imported-copy"
    emit.on_session_opened(
        "w1", agent="kirocrew", slot=WORKER, parent_slot="chat-a", session_key=WORKER_KEY
    )
    assert_drained()
    session_ledger.record_update(WORKER, session_id="w1", goal="g", event="e1")
    assert_drained()
    held: "list[Any]" = []
    monkeypatch.setattr(emit, "_submit_carry_fold", held.append)
    emit.on_session_opened(
        "w2",
        agent="kirocrew",
        slot=WORKER,
        parent_slot="chat-a",
        previous_sid="w1",
        session_key=WORKER_KEY,
    )
    assert_drained()
    # ``w2`` is adopted before its carry folds, so the carry has no edge to copy.
    emit.on_session_adopted("w2", slot=WORKER, parent_slot="chat-b", previous_parent_slot="chat-a")
    assert_drained()
    session_ledger.record_update(WORKER, session_id="w1", event="e2")
    assert_drained()
    assert len(held) == 1
    held[0]()
    assert_drained()
    carried = [e for e in _entries("w2") if e.get("data", {}).get("carried") is True]
    assert carried == [] and any(e["type"] == "session/carried" for e in _entries("w2"))
    if restart:
        emit.reset_caches()

    settled: list[bool] = []
    emit.on_session_adopted(
        "w1",
        slot=WORKER,
        parent_slot="chat-c",
        previous_parent_slot="chat-a",
        on_settled=settled.append,
    )
    assert_drained()

    assert settled == [True]
    assert _adoptions_naming("chat-c") == ["w2"]
    assert _cold_parent(WORKER) == "chat-c"


def test_a_refused_handed_on_decision_is_told_false_once_and_lands_nothing(carrying, monkeypatch):
    """A refusal in the carrier is final: told once, and the follow-on's requeued copy stops."""
    _adopted_then_carried(carrying, still_mapped=True)
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
    assert _cold_parent(WORKER) == "chat-b"


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
                if isinstance(named, ast.Constant) and named.value in late_writes._TREE_ENTRY_TYPES:
                    appends.append((top.name, named.value))
    return appends, landing, module


def test_every_tree_entry_the_carry_copies_is_appended_through_the_hand_over():
    """A tree type the carry copies is appended only by a job that asks the slot's lane.

    Its caller names a unit before the job runs, so an append that skips the hand-over
    is filed behind a carry that landed in between and loses to the carried copy.
    """
    # The types the hand-over names are the ones the carry copies.
    copied = store_retention._TREE_EDGE_TYPES | {store_retention._SPAWNED_TYPE}
    assert late_writes._TREE_ENTRY_TYPES == copied
    assert late_writes.TREE_KEY not in CARRIED_FOLD_NAMES

    appends, landing, module = _session_tree_appends()
    # Controls: the extractor reads both sides, and the check can fail.
    assert {entry_type for _function, entry_type in appends} == copied
    assert {"on_session_adopted", "on_session_released", "on_session_spawned"} <= landing

    offenders = sorted(f"{fn}: {entry_type}" for fn, entry_type in appends if fn not in landing)
    assert not offenders, (
        "these appends of a tree entry the carry copies skip the slot-lane hand-over, so a "
        f"decision resolved before a carry loses to the carried copy: {offenders}"
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
