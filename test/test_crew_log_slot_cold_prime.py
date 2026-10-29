"""A cold slot fold (``ProjectionRegistry.prime``) folds onto ONE private state.

``_SlotFold.apply`` copies the state before every step, so the incremental path can
hand out a state and still advance. The cold path (``fold_slot_warm`` with nothing
warm and no savepoint -> ``registry.prime``) shows no reader a state between the
first entry and the last, so a copy there only costs work: one copy of a state that
grows with every entry made a cold prime O(N^2). These tests pin the replacement.

* The value is the same: for every fold the catalogue registers, priming through
  ``apply_owned`` reaches the state the old copy-per-step loop reached, over a real
  busy log and over seeded generated sequences.
* The work is bounded: the number of state copies a cold prime makes does not grow
  with the number of entries.
* Ownership still holds: a state the registry hands out after a prime does not move
  when the registry is driven again, and the entries folded are not edited.
"""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest
from crew_log_projection_helpers import _busy_log, _entries  # noqa: F401
from test_crew_log_types import CANONICAL

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import projection_catalogue
from kiro_crew.crew_log.projection_slot_memo import _Ordinal, _ordinal_seq, _SlotFold
from kiro_crew.crew_log.schema import Entry
from kiro_crew.projection import ProjectionRegistry

SLOT = "dashboard:3"
UNIT = "acp-cold-prime"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Own data home, crew log on, no eager folder and no warm fold carried over."""
    monkeypatch = _floor_monkeypatch
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    from kiro_crew.crew_log import projection_workstreams

    # Module-global and cleared only on read: no test may see another's leftover count.
    monkeypatch.setattr(projection_workstreams, "_WORKSTREAMS_UNITS_DROPPED", {})
    yield
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


def _normalized(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _copy_per_step(name: str, events: list[_Ordinal]) -> Any:
    """The fold as ``prime`` ran it before: ``apply`` on every event, a copy each."""
    definition = _SlotFold(crew_log._FOLDS[name], SLOT)
    state = definition.init()
    for event in events:
        state = definition.apply(state, event)
    return state


def _primed(name: str, events: list[_Ordinal]) -> Any:
    registry = ProjectionRegistry(seq_of=_ordinal_seq)
    registry.register(_SlotFold(crew_log._FOLDS[name], SLOT))
    registry.prime(SLOT, iter(events))
    return registry.cells(SLOT)[name][0]


def _ordinals(entries: "tuple[Entry, ...] | list[Entry]") -> list[_Ordinal]:
    return [_Ordinal(index + 1, entry) for index, entry in enumerate(entries)]


#: Values a generated entry draws its id-like strings from, so items, owners and
#: sessions REPEAT across a sequence and the folds update rows they already hold.
_VARIANTS = ("a", "b", "c")


def _vary(value: Any, rng: random.Random) -> Any:
    if isinstance(value, dict):
        return {key: _vary(inner, rng) for key, inner in value.items()}
    if isinstance(value, list):
        return [_vary(inner, rng) for inner in value]
    if isinstance(value, str) and value and rng.random() < 0.5:
        return f"{value}-{rng.choice(_VARIANTS)}"
    if isinstance(value, int) and not isinstance(value, bool) and rng.random() < 0.3:
        return value + rng.randint(0, 3)
    return value


def _generated(seed: int, length: int) -> list[Entry]:
    """A seeded sequence drawn from one canonical example per declared type."""
    rng = random.Random(seed)
    types = sorted(CANONICAL)
    out: list[Entry] = []
    for seq in range(1, length + 1):
        entry_type = rng.choice(types)
        data = copy.deepcopy(CANONICAL[entry_type])
        if rng.random() < 0.7:
            data = _vary(data, rng)
        out.append(
            Entry(type=entry_type, seq=seq, time=1_700_000_000 + seq, src="gateway", data=data)
        )
    return out


def _outcome(fold: Any, name: str, events: list[_Ordinal]) -> tuple[str, str]:
    """``(state, rendered)`` as JSON, or the exception's type when the fold refuses."""
    try:
        state = fold(name, events)
    except Exception as exc:  # noqa: BLE001 -- both paths must refuse alike
        return ("raised", type(exc).__name__)
    return (_normalized(state), _normalized(crew_log._FOLDS[name].render(state)))


@pytest.mark.parametrize("name", crew_log.FOLD_NAMES)
def test_prime_reaches_the_copy_per_step_value_over_a_busy_log(name):
    events = _ordinals(_entries(_busy_log()))
    assert _outcome(_primed, name, events) == _outcome(_copy_per_step, name, events)


@pytest.mark.parametrize("name", crew_log.FOLD_NAMES)
@pytest.mark.parametrize("seed", range(12))
def test_prime_reaches_the_copy_per_step_value_over_generated_sequences(name, seed):
    """Property-style: seeded sequences over every declared type, both paths agree.

    The entries are checked unchanged afterwards as well: a step that keeps a
    reference to an entry's ``data`` and later edits it would edit the entry when the
    state is stepped in place.
    """
    entries = _generated(seed, 60)
    events = _ordinals(entries)
    snapshot = [copy.deepcopy(entry.data) for entry in entries]
    primed = _outcome(_primed, name, events)
    assert [entry.data for entry in entries] == snapshot, f"{name} edited an entry it folded"
    assert primed == _outcome(_copy_per_step, name, events)


def test_prime_uses_apply_for_a_definition_without_apply_owned():
    """The member log's definitions offer no ``apply_owned`` and keep ``apply``."""

    class _Counter:
        key = "count"
        state_version = 1

        def init(self) -> dict[str, int]:
            return {"n": 0}

        def apply(self, state: dict[str, int], event: Any) -> dict[str, int]:
            return {"n": state["n"] + 1}

        def view(self, state: dict[str, int]) -> dict:
            return dict(state)

    registry = ProjectionRegistry()
    registry.register(_Counter())
    registry.prime("s", ({"seq": seq} for seq in range(1, 6)))
    assert registry.cells("s")["count"] == ({"n": 5}, 5)


def test_prime_never_edits_the_object_init_handed_out():
    """``init()`` may return an object it keeps; prime copies it once before editing."""
    kept = {"n": 0}

    class _Shared:
        key = "shared"
        state_version = 1

        def init(self) -> dict[str, int]:
            return kept

        def apply(self, state: dict[str, int], event: Any) -> dict[str, int]:
            return {"n": state["n"] + 1}

        def apply_owned(self, state: dict[str, int], event: Any) -> dict[str, int]:
            state["n"] += 1
            return state

        def view(self, state: dict[str, int]) -> dict:
            return dict(state)

    registry = ProjectionRegistry()
    registry.register(_Shared())
    registry.prime("s", ({"seq": seq} for seq in range(1, 4)))
    assert registry.cells("s")["shared"][0] == {"n": 3}
    assert kept == {"n": 0}


@pytest.mark.parametrize("name", ["ledger", "work", "status"])
def test_a_state_handed_out_after_prime_does_not_move_when_the_registry_is_driven(name):
    """The registry's stored state cannot be reached through what ``cells`` returned.

    ``prime`` edits its own state in place; once it is stored, ``drive`` goes back to
    copy-on-step, so the object a caller kept still holds the primed value after more
    entries arrive.
    """
    entries = _entries(_busy_log())
    events = _ordinals(entries)
    cut = len(events) // 2
    registry = ProjectionRegistry(seq_of=_ordinal_seq)
    registry.register(_SlotFold(crew_log._FOLDS[name], SLOT))
    registry.prime(SLOT, iter(events[:cut]))
    held = registry.cells(SLOT)[name][0]
    frozen = copy.deepcopy(held)
    for event in events[cut:]:
        registry.drive(SLOT, event)
    assert held == frozen
    assert _normalized(registry.cells(SLOT)[name][0]) == _normalized(_copy_per_step(name, events))


# --- the work a cold prime does --------------------------------------------


def _write_work_unit(unit: str, count: int) -> None:
    handle = CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="kirocrew", slot=SLOT)
    base = CANONICAL["work/recorded"]
    for index in range(count):
        data = dict(base, slot=SLOT, item_id=f"it_{index % 7:08x}", summary=f"step {index}")
        handle.append("work/recorded", data, src="gateway")


def _counted_cold_fold(monkeypatch, count: int) -> tuple[int, int]:
    """``(copied calls, top-level deepcopy calls)`` during one cold ``fold_slot_warm``."""
    unit = f"{UNIT}-{count}"
    _write_work_unit(unit, count)
    crew_log.forget_slot_folds()
    calls = {"copied": 0, "deepcopy": 0, "depth": 0}
    real_copied = projection_catalogue._Fold.copied
    real_deepcopy = copy.deepcopy

    def counting_copied(self, state):
        calls["copied"] += 1
        return real_copied(self, state)

    def counting_deepcopy(value, memo=None, _nil=[]):  # noqa: B006 -- copy's own signature
        if calls["depth"] == 0:
            calls["deepcopy"] += 1
        calls["depth"] += 1
        try:
            return real_deepcopy(value, memo)
        finally:
            calls["depth"] -= 1

    # Neither the savepoint read nor the memo may answer: the cold prime is measured.
    from kiro_crew.crew_log import projection_slot_memo

    monkeypatch.setattr(projection_slot_memo, "_resume_slot_fold", lambda *a, **k: None)
    monkeypatch.setattr(projection_catalogue._Fold, "copied", counting_copied)
    monkeypatch.setattr(copy, "deepcopy", counting_deepcopy)
    try:
        checkpoint = crew_log.fold_slot_warm("work", (unit,), slot=SLOT)
    finally:
        monkeypatch.setattr(copy, "deepcopy", real_deepcopy)
        monkeypatch.setattr(projection_catalogue._Fold, "copied", real_copied)
    assert checkpoint.last_seq == count
    return calls["copied"], calls["deepcopy"]


def test_a_cold_prime_copies_the_state_a_bounded_number_of_times(monkeypatch):
    """MUTATION-SENSITIVE: copies during a cold prime are O(1) in entries, not O(N).

    Before the fix every ``work/recorded`` entry cost one ``copied`` call, so 40 and
    400 entries differed by 360 copies. Now neither count may move with N.
    """
    small = _counted_cold_fold(monkeypatch, 40)
    large = _counted_cold_fold(monkeypatch, 400)
    assert small[0] == large[0] == 0, (small, large)
    assert small[1] == large[1], (small, large)


# --- a large slot on disk, every slot fold ---------------------------------

SLOT_FOLDS = tuple(crew_log.SLOT_PROJECTION_NAMES) + tuple(crew_log.INTERNAL_SLOT_PROJECTION_NAMES)


def _write_slot(units: int, per_unit: int, seed: int) -> tuple[str, ...]:
    """*units* session logs under SLOT, each holding *per_unit* generated entries.

    An entry whose varied payload the writer refuses is written in its canonical
    form instead, so every unit carries real, validated records.
    """
    rng = random.Random(seed)
    types = sorted(CANONICAL)
    ids: list[str] = []
    for index in range(units):
        unit = f"acp-large-{index:03d}"
        handle = CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="kirocrew", slot=SLOT)
        for _ in range(per_unit):
            entry_type = rng.choice(types)
            data = copy.deepcopy(CANONICAL[entry_type])
            try:
                handle.append(entry_type, _vary(data, rng), src="gateway")
            except Exception:  # noqa: BLE001 -- a varied enum the writer refuses
                handle.append(entry_type, data, src="gateway")
        ids.append(unit)
    return tuple(ids)


def _cold_value(name: str, units: tuple[str, ...]) -> str:
    from kiro_crew.crew_log import projection_slot_memo

    crew_log.forget_slot_folds()
    original = projection_slot_memo._resume_slot_fold
    projection_slot_memo._resume_slot_fold = lambda *a, **k: None
    try:
        # The workstreams fold reads its unit-overflow counter ONCE, in ``bind_slot``,
        # and the read clears it (``_workstreams_units_dropped``). It is set by the
        # unit SELECTION that precedes a fold, not by the fold, so two folds in a row
        # see "N" and then "0" whichever path runs first -- that order swap is the
        # only difference measured between the two paths on a real board. Each fold
        # here is preceded by the same selection result, so the count is compared
        # like every other field.
        from kiro_crew.crew_log import projection_workstreams

        if name == crew_log.WORKSTREAMS_FOLD_NAME:
            projection_workstreams._WORKSTREAMS_UNITS_DROPPED[SLOT] = 3
        checkpoint = crew_log.fold_slot_warm(name, units, slot=SLOT)
        return _normalized(crew_log.projection_of(checkpoint).value)
    finally:
        projection_workstreams._WORKSTREAMS_UNITS_DROPPED.pop(SLOT, None)
        projection_slot_memo._resume_slot_fold = original
        crew_log.forget_slot_folds()


@pytest.mark.parametrize("name", SLOT_FOLDS)
def test_a_large_cold_slot_fold_equals_the_copy_per_step_fold(name, monkeypatch):
    """Many units, entries spread across them: the cold value is unchanged."""
    units = _write_slot(units=24, per_unit=60, seed=7)
    after = _cold_value(name, units)
    monkeypatch.delattr(_SlotFold, "apply_owned")
    before = _cold_value(name, units)
    assert after == before
    if name == crew_log.WORKSTREAMS_FOLD_NAME:
        assert json.loads(after)["units_omitted"] == 3


# --- session folds ---------------------------------------------------------


def _session_values(unit: str) -> dict[str, str]:
    bundle = crew_log.fold_session(unit, crew_log.SESSION_FOLD_NAMES)
    return {
        name: _normalized(crew_log.projection_of(point).value)
        for name, point in bundle.checkpoints.items()
    }


def test_a_cold_session_fold_equals_the_copy_per_step_fold_and_copies_nothing_per_entry(
    monkeypatch,
):
    """``_SessionFold`` too: a cold ``fold_session`` steps in place, same values.

    A cold session fold reaches ``prime_checkpointed`` with no savepoint; stepping its
    tail through ``apply`` would cost one copy per entry. The count of ``copied`` calls
    is pinned at zero, and the values against the copy-per-step path.
    """
    from kiro_crew.crew_log import checkpoint as savepoints
    from kiro_crew.crew_log.projection_session import _SessionFold

    monkeypatch.setattr(savepoints, "load", lambda *a, **k: None, raising=False)
    handle = CrewLog.create(lg.KIND_SESSION, UNIT, owner="owner", agent="kirocrew", slot=SLOT)
    rng = random.Random(3)
    for _ in range(300):
        entry_type = rng.choice(sorted(CANONICAL))
        data = copy.deepcopy(CANONICAL[entry_type])
        try:
            handle.append(entry_type, _vary(data, rng), src="gateway")
        except Exception:  # noqa: BLE001 -- a varied enum the writer refuses
            handle.append(entry_type, data, src="gateway")
    del handle

    calls = {"copied": 0}
    real_copied = projection_catalogue._Fold.copied

    def counting_copied(self, state):
        calls["copied"] += 1
        return real_copied(self, state)

    monkeypatch.setattr(projection_catalogue._Fold, "copied", counting_copied)
    after = _session_values(UNIT)
    assert calls["copied"] == 0
    monkeypatch.delattr(_SessionFold, "apply_owned")
    before = _session_values(UNIT)
    assert calls["copied"] > 0  # the old path really was taken
    assert after == before


# --- aliasing --------------------------------------------------------------


def test_folds_sharing_entries_in_one_registry_do_not_reach_each_other_or_the_entries():
    """Every catalogue fold, one registry, one stream of shared entries.

    In-place stepping has no copy standing between a step and any
    container an earlier step stored -- including a dict taken from an entry, which
    every fold in the registry is handed the SAME object of. So: each fold's primed
    state equals that fold primed alone through the copy-per-step path, no entry's
    data moves, and a state held after the prime does not move when more entries
    are driven (the drive path still copies on step).

    This is a property of the catalogue's steps, which ``apply_owned``'s contract
    requires (a step must not edit an entry's data). The per-fold generated test above
    checks the same contract fold by fold; this one checks the folds together.
    """
    entries = list(_entries(_busy_log())) + _generated(11, 120)
    entries = [
        Entry(type=e.type, seq=i + 1, time=e.time, src=e.src, data=e.data)
        for i, e in enumerate(entries)
    ]
    events = _ordinals(entries)
    snapshot = [copy.deepcopy(entry.data) for entry in entries]
    cut = len(events) * 2 // 3
    registry = ProjectionRegistry(seq_of=_ordinal_seq)
    survivors = []
    for name in crew_log.FOLD_NAMES:
        # A fold that refuses this generated mix refuses it on both paths (see
        # ``_outcome``); it is left out so the shared registry can finish.
        if _outcome(_copy_per_step, name, events)[0] == "raised":
            continue
        registry.register(_SlotFold(crew_log._FOLDS[name], SLOT))
        survivors.append(name)
    registry.prime(SLOT, iter(events[:cut]))
    assert [entry.data for entry in entries] == snapshot
    cells = registry.cells(SLOT)
    for name in survivors:
        assert _normalized(cells[name][0]) == _normalized(_copy_per_step(name, events[:cut])), name
    held = {name: copy.deepcopy(cells[name][0]) for name in survivors}
    kept = {name: cells[name][0] for name in survivors}
    for event in events[cut:]:
        registry.drive(SLOT, event)
    assert [entry.data for entry in entries] == snapshot
    for name in survivors:
        assert kept[name] == held[name], f"{name}: a held state moved after drive"
        final = registry.cells(SLOT)[name][0]
        assert _normalized(final) == _normalized(_copy_per_step(name, events)), name


# --- which prime_checkpointed passes count as cold -------------------------


class _Tracked:
    """A definition that records which of ``apply`` / ``apply_owned`` folded it."""

    state_version = 1

    def __init__(self, key: str, calls: list[str]) -> None:
        self.key = key
        self._calls = calls

    def init(self) -> dict[str, int]:
        return {"n": 0}

    def apply(self, state: dict[str, int], event: Any) -> dict[str, int]:
        self._calls.append(f"{self.key}:apply")
        return {"n": state["n"] + 1}

    def apply_owned(self, state: dict[str, int], event: Any) -> dict[str, int]:
        self._calls.append(f"{self.key}:owned")
        state["n"] += 1
        return state

    def view(self, state: dict[str, int]) -> dict:
        return dict(state)


class _Points:
    def __init__(self, points: dict[str, tuple[dict[str, int], int]]) -> None:
        self._points = points

    def load(self, store, key, *, state_version, identity, admit=None):
        from kiro_crew.projection import Savepoint

        state, watermark = self._points[key]
        return Savepoint(
            key=key,
            state_version=state_version,
            watermark=watermark,
            state=state,
            identity=dict(identity),
            witness={},
        )


def _checkpointed(points: dict[str, tuple[dict[str, int], int]]) -> tuple[list[str], dict]:
    calls: list[str] = []
    registry = ProjectionRegistry()
    for key in points:
        registry.register(_Tracked(key, calls))
    registry.prime_checkpointed(
        "s", _Points(points), {}, lambda floor: ({"seq": seq} for seq in range(floor + 1, 9))
    )
    return calls, {key: registry.cells("s")[key] for key in points}


def test_a_checkpointed_pass_with_every_cell_at_zero_folds_in_place():
    held = {"n": 0}
    calls, cells = _checkpointed({"a": (held, 0), "b": ({"n": 0}, 0)})
    assert set(calls) == {"a:owned", "b:owned"}
    assert cells == {"a": ({"n": 8}, 8), "b": ({"n": 8}, 8)}
    assert held == {"n": 0}  # the restored state a caller may hold is not edited


def test_a_checkpointed_pass_with_any_resumed_cell_keeps_apply():
    """MUTATION-SENSITIVE: a floor of 0 is not enough to call the pass cold.

    Here one fold sits at seq 0 and the other was resumed at seq 5. The floor is 0,
    but the resumed state came off disk and is untrusted: a fold's copier is what
    refuses one malformed below its top level, so it must still be stepped through
    ``apply``.
    """
    calls, cells = _checkpointed({"a": ({"n": 0}, 0), "b": ({"n": 5}, 5)})
    assert "b:owned" not in calls and "a:owned" not in calls
    assert cells == {"a": ({"n": 8}, 8), "b": ({"n": 8}, 8)}


def test_a_drive_landing_during_a_cold_checkpointed_tail_is_not_overwritten():
    """The in-place tail installs only over the cell it started from.

    A ``drive`` from another caller during the tail replaces that cell. Overwriting it
    would move the watermark backwards and drop an event already announced.
    """
    calls: list[str] = []
    registry = ProjectionRegistry()
    registry.register(_Tracked("a", calls))

    def tail(floor):
        yield {"seq": 1}
        registry.drive("s", {"seq": 20})
        yield {"seq": 2}

    registry.prime_checkpointed("s", _Points({"a": ({"n": 0}, 0)}), {}, tail)
    assert registry.cells("s")["a"][1] == 20
