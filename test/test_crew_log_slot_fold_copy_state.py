"""The slot folds' ``copy_state``: no handed-out state moves, and the value is unchanged.

A slot fold without ``copy_state`` falls back to ``copy.deepcopy`` once per folded
entry, a copy of every retained row for an entry that touched one. Each slot fold
declares a ``copy_state`` that copies only the containers its ``step`` can edit. Two
promises are
checked over seeded sequences that reach every branch that edits a container:

* every state ``apply`` handed out earlier still equals its snapshot, so a container
  left shared is caught the step after it is edited, wherever it was first shared;
* each step equals the copy-per-step fold (a deep copy stepped), including dict order,
  which savepoints and renders depend on.

The fold limits are shrunk so the evictions (closed window, tombstones, owner and
history caps) happen inside a short sequence.
"""

from __future__ import annotations

import copy
import json
import random
from typing import Any, Callable

import pytest

from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import (
    projection_dashboard,
    projection_ledger,
    projection_radar,
    projection_work,
)
from kiro_crew.crew_log.entry_types_ledgers import (
    RADAR_EVENT_KINDS,
    RADAR_PHASES,
    RADAR_SKIP_SCOPES,
)
from kiro_crew.crew_log.projection_slot_memo import _Ordinal, _SlotFold
from kiro_crew.crew_log.schema import Entry

SLOT = "dashboard:3"

#: Module limits shrunk per fold so evictions happen inside a short sequence.
_SMALL_LIMITS: dict[str, tuple[Any, dict[str, int]]] = {
    "work": (
        projection_work,
        {
            "WORK_ITEM_LIMIT": 8,
            "WORK_CLOSED_WINDOW": 3,
            "WORK_TOMBSTONE_LIMIT": 5,
            "WORK_EVENT_LIMIT": 4,
            "WORK_PARKED_LIMIT": 3,
        },
    ),
    "ledger": (
        projection_ledger,
        {"LEDGER_TRIED_LIMIT": 3, "LEDGER_EVENT_LIMIT": 4, "LEDGER_ARTIFACT_LIMIT": 3},
    ),
    "radar": (
        projection_radar,
        {
            "RADAR_ITEM_LIMIT": 4,
            "RADAR_EVENT_LIMIT": 5,
            "RADAR_TRIED_LIMIT": 2,
            "RADAR_PHASE_LINE_LIMIT": 3,
            "RADAR_SKIP_LIMIT": 3,
        },
    ),
    crew_log.PANEL_FOLD_NAME: (
        projection_dashboard,
        {"PANEL_OWNER_LIMIT": 2, "PANEL_HISTORY_LIMIT": 2},
    ),
    crew_log.DASHBOARD_FOLD_NAME: (projection_dashboard, {"DASHBOARD_VALUE_LIMIT": 3}),
    crew_log.MISTAKES_FOLD_NAME: (
        projection_dashboard,
        {"MISTAKE_GROUP_LIMIT": 3, "MISTAKE_CORRECTION_LIMIT": 2},
    ),
    crew_log.CUSTOM_FOLD_NAME: (
        projection_dashboard,
        {"CUSTOM_TYPE_LIMIT": 3, "CUSTOM_FAILURE_LIMIT": 2},
    ),
}


def _work(rng: random.Random) -> tuple[str, dict[str, Any]]:
    item = f"it_{rng.randrange(12):08x}"
    actor, action = rng.choice(
        [
            ("conductor", "create"),
            ("conductor", "create"),
            ("conductor", "close"),
            ("conductor", "close"),
            ("conductor", "bind"),
            ("conductor", "decide"),
            ("conductor", "verdict"),
            ("conductor", "accept"),
            ("conductor", "retire"),
            ("conductor", "goal"),
            ("worker", "report"),
            ("worker", "report"),
            ("worker", "report"),
            ("person", "steer"),
        ]
    )
    data: dict[str, Any] = {"slot": SLOT, "actor": actor, "by": SLOT, "action": action}
    data["item_id"] = item
    data["title"] = rng.choice(["a", "b"])
    data["state"] = rng.choice(["accepted", "rejected", "abandoned", "open"])
    data["decision"] = rng.choice(["go", "stop"])
    data["round"] = rng.randrange(3)
    data["goal"] = rng.choice(["g1", "g2"])
    data["goal_version"] = rng.randrange(4)
    data["acceptance"] = {"kind": "pr_checks", "n": rng.randrange(3)}
    data["status"] = rng.choice(["progress", "blocked", "done"])
    data["summary"] = rng.choice(["s1", "s2"])
    data["artifacts"] = {"pr": str(rng.randrange(9))}
    data["worker_session_key"] = rng.choice(["w1", "w2", ""])
    data["verdict"] = rng.choice(["pending", "pass", "fail"])
    data["pending_since"] = rng.choice(["", "2026-01-01T00:00:00+00:00"])
    data["wake_at"] = rng.choice(["", "2026-01-02T00:00:00+00:00"])
    data["event_kind"] = rng.choice(["report", "decide", ""])
    data["event"] = rng.choice(["e1", "e2"])
    if rng.random() < 0.15:
        data["baseline"] = True
    if rng.random() < 0.1:
        data["board_copy"] = True
    if rng.random() < 0.03:
        data["generation"] = rng.choice(["gen-a", "gen-b"])
    for key in list(data):
        if key not in ("slot", "actor", "by", "action", "item_id") and rng.random() < 0.4:
            del data[key]
    return "work/recorded", data


def _ledger(rng: random.Random) -> tuple[str, dict[str, Any]]:
    data: dict[str, Any] = {"slot": SLOT}
    if rng.random() < 0.5:
        data["phase"] = rng.choice(["implementation", "done", "review"])
    if rng.random() < 0.5:
        data["tried"] = {"approach": f"a{rng.randrange(5)}", "rejected_because": "r"}
    if rng.random() < 0.5:
        data["artifacts"] = {f"k{rng.randrange(5)}": f"v{rng.randrange(5)}"}
    if rng.random() < 0.6:
        data["event"] = f"e{rng.randrange(5)}"
        data["event_kind"] = rng.choice(["phase", "note", "decision"])
    if rng.random() < 0.3:
        data["goal"] = "g"
    return "ledger/recorded", data


def _radar(rng: random.Random) -> tuple[str, dict[str, Any]]:
    data: dict[str, Any] = {
        "crew_id": "c_0a1b2c3d",
        "owner": "o",
        "repo": "r",
        "event": f"e{rng.randrange(4)}",
        "event_kind": rng.choice(RADAR_EVENT_KINDS),
    }
    if rng.random() < 0.9:
        data["number"] = rng.randrange(1, 8)
    if rng.random() < 0.6:
        data["phase"] = rng.choice(RADAR_PHASES)
    if rng.random() < 0.4:
        data["tried"] = {"approach": f"a{rng.randrange(4)}", "rejected_because": "r"}
    if rng.random() < 0.3:
        data["ci_state"] = {"state": rng.choice(["running", "green"]), "round": rng.randrange(4)}
    if rng.random() < 0.3:
        data["skip"] = {"reason": f"r{rng.randrange(3)}", "scope": rng.choice(RADAR_SKIP_SCOPES)}
    if rng.random() < 0.2:
        data["carried"] = True
    if rng.random() < 0.2:
        data["clear"] = ["pr_number", "ci_state", "labels_applied", "next"]
    if rng.random() < 0.3:
        data["next"] = f"n{rng.randrange(3)}"
    if rng.random() < 0.3:
        data["labels_applied"] = ["x", "y"]
    if rng.random() < 0.2:
        data["pr_number"] = rng.randrange(100)
    return "radar/recorded", data


def _panel(rng: random.Random) -> tuple[str, dict[str, Any]]:
    return "panel/published", {
        "template": rng.choice(["default", "kpis", ""]),
        "data": {"cycle": rng.randrange(9)},
        "title": f"t{rng.randrange(3)}",
        "crew": "c",
        "crew_key": f"k{rng.randrange(3)}",
    }


def _agentic(rng: random.Random) -> tuple[str, dict[str, Any]]:
    return "dashboard/agentic_value", {
        "field": f"f{rng.randrange(5)}",
        "type": "string",
        "value": {"v": f"v{rng.randrange(3)}"},
        "instance_version": rng.randrange(3),
        "crew_key": "k",
    }


def _mistakes(rng: random.Random) -> tuple[str, dict[str, Any]]:
    if rng.random() < 0.3:
        return "dashboard/agentic_refused", {
            "code": projection_dashboard.MISTAKE_CORRECTED_CODE,
            "field": f"f{rng.randrange(4)}",
            "corrects": [f"f{rng.randrange(4)}", f"f{rng.randrange(4)}"],
            "crew_key": "k",
        }
    return "dashboard/agentic_refused", {
        "code": rng.choice(["unknown_field", "bad_type"]),
        "field": f"f{rng.randrange(4)}",
        "reason": f"r{rng.randrange(3)}",
        "corrects": [],
        "crew_key": "k",
    }


def _custom(rng: random.Random) -> tuple[str, dict[str, Any]]:
    writer = f"cron:w{rng.randrange(3)}"
    if rng.random() < 0.35:
        return "dashboard/custom_failed", {"writer": writer, "error": "e", "member": "m"}
    data: dict[str, Any] = {
        "type": f"t{rng.randrange(5)}",
        "data": {"n": rng.randrange(9)},
        "writer": writer,
        "member": "m",
        "crew_key": "k",
    }
    if rng.random() < 0.3:
        data["max_age_s"] = 60
    return "dashboard/custom_value", data


_GENERATORS: dict[str, Callable[[random.Random], tuple[str, dict[str, Any]]]] = {
    "work": _work,
    "ledger": _ledger,
    "radar": _radar,
    crew_log.PANEL_FOLD_NAME: _panel,
    crew_log.DASHBOARD_FOLD_NAME: _agentic,
    crew_log.MISTAKES_FOLD_NAME: _mistakes,
    crew_log.CUSTOM_FOLD_NAME: _custom,
}


def _sequence(name: str, seed: int, length: int) -> list[_Ordinal]:
    rng = random.Random(seed)
    events = []
    for seq in range(1, length + 1):
        entry_type, data = _GENERATORS[name](rng)
        # Time is not monotonic: units fold in unit order, not time order.
        stamp = 1_700_000_000_000 + seq * 1000 + rng.randrange(-5000, 5000)
        entry = Entry(type=entry_type, seq=seq, time=stamp, src="gateway", data=data)
        events.append(_Ordinal(seq, entry))
    return events


def _json(value: Any) -> str:
    # Not sort_keys: dict ORDER is part of the value a savepoint and a render keep.
    return json.dumps(value, default=str)


@pytest.mark.parametrize("name", sorted(_GENERATORS))
def test_every_slot_fold_with_a_growing_state_declares_copy_state(name):
    assert crew_log._FOLDS[name].copy_state is not None, name


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("name", sorted(_GENERATORS))
def test_no_handed_out_state_moves_and_each_step_equals_the_copy_per_step_fold(
    name, seed, monkeypatch
):
    module, limits = _SMALL_LIMITS[name]
    for attr, value in limits.items():
        assert hasattr(module, attr), (module.__name__, attr)
        monkeypatch.setattr(module, attr, value)
    fold = crew_log._FOLDS[name]
    definition = _SlotFold(fold, SLOT)
    state = definition.init()
    handed: list[tuple[dict[str, Any], str]] = [(state, _json(state))]
    moved = 0
    for event in _sequence(name, seed, 160):
        reference = copy.deepcopy(state)
        fold.step(reference, event.entry)
        grown = definition.apply(state, event)
        assert _json(grown) == _json(reference), f"{name} differs at seq {event.ordinal}"
        for held, snapshot in handed:
            assert _json(held) == snapshot, f"{name} moved a handed-out state at {event.ordinal}"
        if grown is not state:
            moved += 1
            handed.append((grown, _json(grown)))
        state = grown
    assert moved > 40, f"the sequence barely moved {name} ({moved})"
    # The render reads every container too, and must agree with the deep-copy fold.
    assert _json(fold.render(state)) == _json(fold.render(copy.deepcopy(state)))


def test_the_work_sequences_reach_every_container_the_step_edits(monkeypatch):
    """The generator is only proof if it hits the edits: forgets, tombstones, parking."""
    module, limits = _SMALL_LIMITS["work"]
    for attr, value in limits.items():
        monkeypatch.setattr(module, attr, value)
    seen = {"forgotten": 0, "tombstones": 0, "parked": 0, "events": 0, "omitted": 0}
    for seed in range(8):
        state = _SlotFold(crew_log._FOLDS["work"], SLOT).init()
        for event in _sequence("work", seed, 160):
            crew_log._FOLDS["work"].step(state, event.entry)
            seen["forgotten"] = max(seen["forgotten"], state["forgotten"]["items"])
            seen["tombstones"] = max(seen["tombstones"], len(state["tombstones"]))
            seen["parked"] = max(seen["parked"], len(state["parked"]))
            seen["omitted"] = max(seen["omitted"], state["omitted"])
            seen["events"] = max(
                [seen["events"]] + [len(i["events"]) for i in state["items"].values()]
            )
    assert all(seen.values()), seen


# --------------------------------------------------------------------------- #
# the cost, on the real store and the real read path
# --------------------------------------------------------------------------- #


@pytest.fixture
def _home(tmp_path, _floor_monkeypatch):
    """Own data home, crew log on, no eager folder and no warm fold carried over."""
    from kiro_crew.crew_log import eager as crew_log_eager
    from kiro_crew.crew_log import emit as crew_log_emit

    monkeypatch = _floor_monkeypatch
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield monkeypatch
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


def _append_creates(handle: Any, prefix: str, count: int) -> None:
    for nth in range(count):
        handle.append(
            "work/recorded",
            {
                "slot": SLOT,
                "actor": "conductor",
                "by": SLOT,
                "action": "create",
                "item_id": f"{prefix}-{nth}",
                "title": "a work item",
            },
            src="gateway",
        )


def _whole_state_copies(monkeypatch, call: Callable[[], Any]) -> int:
    """Top-level ``copy.deepcopy`` calls on a work fold state while *call* runs."""
    real = copy.deepcopy
    seen = {"n": 0}

    def counting(value, memo=None, _nil=[]):  # noqa: B006 -- copy's own signature
        if memo is None and isinstance(value, dict) and "tombstones" in value:
            seen["n"] += 1
        return real(value, memo)

    monkeypatch.setattr(copy, "deepcopy", counting)
    try:
        call()
    finally:
        monkeypatch.setattr(copy, "deepcopy", real)
    return seen["n"]


def test_a_warm_read_after_one_append_copies_no_whole_state_at_any_size(_home):
    """Row 2: one entry onto a warm cell. Before: one whole-state deep copy per read."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    counts = []
    for size in (50, 200, 400):
        unit = f"acp-copy-cost-{size}"
        handle = CrewLog.create(lg.KIND_SESSION, unit, owner="o", agent="kirocrew", slot=SLOT)
        _append_creates(handle, unit, size)
        crew_log.read_slot_projection(SLOT, "work")
        _append_creates(handle, f"{unit}-more", 1)
        counts.append(
            _whole_state_copies(_home, lambda: crew_log.read_slot_projection(SLOT, "work"))
        )
        crew_log.forget_slot_folds()
    assert counts == [0, 0, 0], counts


def test_a_restart_tail_copies_no_whole_state_per_entry(_home):
    """Row 4: a restart resumes a savepoint and folds the tail through ``apply``.

    Before: one whole-state deep copy per tail entry ([30, 100, 200] here).
    """
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    counts = []
    for tail in (30, 100, 200):
        unit = f"acp-copy-tail-{tail}"
        handle = CrewLog.create(lg.KIND_SESSION, unit, owner="o", agent="kirocrew", slot=SLOT)
        # Enough for the first read to write a savepoint; the tail stays under the
        # rewrite threshold, so the restart resumes it (repro row 4's shape).
        _append_creates(handle, unit, 300)
        crew_log.read_slot_projection(SLOT, "work")
        _append_creates(handle, f"{unit}-tail", tail)
        crew_log.forget_slot_folds()
        counts.append(
            _whole_state_copies(_home, lambda: crew_log.read_slot_projection(SLOT, "work"))
        )
        crew_log.forget_slot_folds()
    assert counts == [0, 0, 0], counts
