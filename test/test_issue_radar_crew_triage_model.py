"""A crew's triage model: which model each turn runs on, and how the slot is switched.

The rule under test is the whole contract of the setting:

  * unset, the slot is never touched -- a crew without a triage model behaves
    exactly as one always has;
  * set, a turn with no open item in an acting phase runs on the triage model,
    and any acting item puts the crew back on its work model;
  * the switch lands between turns, through the slot's deferred session reset;
  * a model a person picked in the crew's session holds the slot until the crew's
    model settings change -- the crew switches only a model it chose itself;
  * a triage model the session withheld falls back to the work model and is
    named for the crew page.
"""

from __future__ import annotations

import ast
import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import kiro_crew
from kiro_crew.apps.builtins.issue_radar.backend import crew_model as cm
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.dashboard.chat_utils import session_key_for

WORK = "claude-opus-4.5"
TRIAGE = "claude-haiku-4.5"


class _Slot:
    """The slot attributes the switch reads and writes, and nothing else."""

    def __init__(self, key: str = "crew-c_1", model: str = WORK) -> None:
        self.key = key
        self.model = model
        self.running = False
        self.model_withheld: bool | None = None
        self._pending_reset_history_key: str | None = None
        self._eager_spawn_task = None
        self._model_pick_gen = 0
        self._model_runtime_pick_gen: int | None = None
        self._model_pick_lock = asyncio.Lock()
        self._dirty = False


class _Sessions:
    def __init__(self, live: set[str] | None = None) -> None:
        self.live = set(live or ())

    def get_provider(self, key: str) -> object | None:
        return object() if key in self.live else None


class _State:
    """``live`` names SLOTS with a registered session; keyed the way the runtime asks."""

    def __init__(self, live: set[str] | None = None) -> None:
        self.sessions = _Sessions({session_key_for(key) for key in (live or ())})
        self._slots: dict[str, Any] = {}


def _crew(**fields: Any) -> dict[str, Any]:
    return {"id": "c_1", "model": WORK, "triage_model": "", **fields}


def _item(phase: str) -> dict[str, Any]:
    return {"number": 7, "phase": phase}


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        cm._withheld.clear()
        cm._written.clear()
        cm._held.clear()
        cm._hold_seen_at.clear()
        self.addCleanup(cm._withheld.clear)
        self.addCleanup(cm._written.clear)
        self.addCleanup(cm._held.clear)
        self.addCleanup(cm._hold_seen_at.clear)


def _pick(slot: _Slot, model: str) -> None:
    """What the model picker commits: the model, and a bump of the pick generation."""
    slot.model = model
    slot._model_pick_gen += 1


def _provider_switch(slot: _Slot) -> None:
    """What the gateway's provider switch writes on every slot with a model.

    The same three writes as the switch in the config watcher (whose own test
    pins them): the model cleared, the generation bumped, and that generation
    stamped as the runtime's.
    """
    slot.model = ""
    slot._model_pick_gen += 1
    slot._model_runtime_pick_gen = slot._model_pick_gen


class TestTurnModel(_Isolated):
    def test_unset_runs_every_turn_on_the_work_model(self):
        self.assertEqual(cm.turn_model(_crew(), []), WORK)
        self.assertEqual(cm.turn_model(_crew(), [_item("implementing")]), WORK)

    def test_an_empty_queue_runs_on_the_triage_model(self):
        self.assertEqual(cm.turn_model(_crew(triage_model=TRIAGE), []), TRIAGE)

    def test_items_only_waiting_run_on_the_triage_model(self):
        items = [_item("awaiting-ci"), _item("awaiting-merge"), _item("awaiting-reply")]
        self.assertEqual(cm.turn_model(_crew(triage_model=TRIAGE), items), TRIAGE)

    def test_any_acting_item_runs_on_the_work_model(self):
        for phase in ("selected", "claimed", "investigating", "implementing", "addressing-review"):
            with self.subTest(phase=phase):
                items = [_item("awaiting-ci"), _item(phase)]
                self.assertEqual(cm.turn_model(_crew(triage_model=TRIAGE), items), WORK)

    def test_acting_phases_cover_the_store_sets(self):
        self.assertTrue(set(cs.TTL_ACTIVE_PHASES) <= cm.ACTING_PHASES)
        self.assertTrue(set(cs.EDITING_PHASES) <= cm.ACTING_PHASES)
        self.assertTrue(cm.ACTING_PHASES <= set(cs.PHASES))


class TestApplyTurnModel(_Isolated):
    def test_unset_never_touches_the_slot(self):
        slot = _Slot(model="something-a-person-picked")
        self.assertFalse(cm.apply_turn_model(_State({slot.key}), slot, _crew(), []))
        self.assertEqual(slot.model, "something-a-person-picked")
        self.assertIsNone(slot._pending_reset_history_key)

    def test_a_triage_turn_switches_the_slot_and_arms_the_reset(self):
        slot = _Slot()
        moved = cm.apply_turn_model(_State({slot.key}), slot, _crew(triage_model=TRIAGE), [])
        self.assertTrue(moved)
        self.assertEqual(slot.model, TRIAGE)
        # The live session is still on the work model; the next turn tears it down
        # before acquiring one, so it resumes on the triage model.
        self.assertEqual(slot._pending_reset_history_key, session_key_for(slot.key))
        self.assertEqual(slot._model_pick_gen, 1)
        self.assertTrue(slot._dirty)

    def test_no_live_session_needs_no_reset(self):
        slot = _Slot()
        cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), [])
        self.assertEqual(slot.model, TRIAGE)
        self.assertIsNone(slot._pending_reset_history_key)

    def test_an_acting_item_puts_the_work_model_back(self):
        slot = _Slot(model=TRIAGE)
        crew = _crew(triage_model=TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State({slot.key}), slot, crew, [_item("claimed")]))
        self.assertEqual(slot.model, WORK)
        self.assertEqual(slot._pending_reset_history_key, session_key_for(slot.key))

    def test_a_running_turn_keeps_its_model(self):
        slot = _Slot()
        slot.running = True
        self.assertFalse(
            cm.apply_turn_model(_State({slot.key}), slot, _crew(triage_model=TRIAGE), [])
        )
        self.assertEqual(slot.model, WORK)
        self.assertIsNone(slot._pending_reset_history_key)

    def test_another_slot_mid_turn_on_the_session_is_left_alone(self):
        # The session_set_model pick's own deferral, through its own predicate: a
        # registered session with a turn in flight belongs to whoever is driving it.
        class _Busy:
            def has_active_turn(self) -> bool:
                return True

        class _Registry(_State):
            def __init__(self) -> None:
                super().__init__()
                self.sessions.get_provider = lambda key: _Busy()  # type: ignore[method-assign]

        slot = _Slot()
        self.assertFalse(cm.apply_turn_model(_Registry(), slot, _crew(triage_model=TRIAGE), []))
        self.assertEqual(slot.model, WORK)
        self.assertIsNone(slot._pending_reset_history_key)

    def test_another_running_slot_on_the_session_is_left_alone(self):
        other = _Slot(key="chat-7")
        other.running = True
        other._turn_key = session_key_for("crew-c_1")  # type: ignore[attr-defined]
        state = _State()
        state._slots = {"chat-7": other}
        slot = _Slot()
        with mock.patch(
            "kiro_crew.dashboard.chat_handlers._cancel_target",
            lambda s: getattr(s, "_turn_key", session_key_for(s.key)),
        ):
            self.assertFalse(cm.apply_turn_model(state, slot, _crew(triage_model=TRIAGE), []))
        self.assertEqual(slot.model, WORK)

    def test_an_idle_registry_still_switches(self):
        slot = _Slot()
        self.assertTrue(
            cm.apply_turn_model(_State({"crew-c_1"}), slot, _crew(triage_model=TRIAGE), [])
        )
        self.assertEqual(slot.model, TRIAGE)

    def test_a_picker_transaction_in_flight_is_left_alone(self):
        async def run() -> None:
            slot = _Slot()
            async with slot._model_pick_lock:
                cm.apply_turn_model(_State({slot.key}), slot, _crew(triage_model=TRIAGE), [])
            self.assertEqual(slot.model, WORK)

        asyncio.run(run())

    def test_already_on_the_target_is_a_no_op(self):
        slot = _Slot(model=TRIAGE)
        self.assertFalse(
            cm.apply_turn_model(_State({slot.key}), slot, _crew(triage_model=TRIAGE), [])
        )
        self.assertIsNone(slot._pending_reset_history_key)
        self.assertEqual(slot._model_pick_gen, 0)


class TestUnavailableTriageModel(_Isolated):
    def test_a_withheld_triage_model_falls_back_and_is_named(self):
        slot = _Slot(model=TRIAGE)
        slot.model_withheld = True  # the session that started on it could not run it
        crew = _crew(triage_model=TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State({slot.key}), slot, crew, []))
        self.assertEqual(slot.model, WORK)
        self.assertEqual(cm.unavailable_triage_model(crew), TRIAGE)
        # And it stays on the work model: the verdict outlives the slot moving off.
        slot.model_withheld = None
        self.assertFalse(cm.apply_turn_model(_State({slot.key}), slot, crew, []))
        self.assertEqual(slot.model, WORK)

    def test_changing_the_setting_clears_the_notice(self):
        slot = _Slot(model=TRIAGE)
        slot.model_withheld = True
        cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), [])
        other = _crew(triage_model="claude-sonnet-4.5")
        self.assertEqual(cm.unavailable_triage_model(other), "")
        self.assertEqual(cm.turn_model(other, []), "claude-sonnet-4.5")

    def test_a_runnable_verdict_reports_nothing(self):
        slot = _Slot(model=TRIAGE)
        slot.model_withheld = False
        crew = _crew(triage_model=TRIAGE)
        cm.apply_turn_model(_State(), slot, crew, [])
        self.assertEqual(cm.unavailable_triage_model(crew), "")
        self.assertEqual(slot.model, TRIAGE)


class TestSwitchingOff(_Isolated):
    def test_switching_off_puts_the_work_model_back(self):
        slot = _Slot()
        cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), [])
        self.assertEqual(slot.model, TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State({slot.key}), slot, _crew(), []))
        self.assertEqual(slot.model, WORK)
        self.assertFalse(cm.is_managed(slot))

    def test_a_model_picked_since_is_kept(self):
        slot = _Slot()
        cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), [])
        _pick(slot, "picked-by-hand")
        self.assertFalse(cm.apply_turn_model(_State(), slot, _crew(), []))
        self.assertEqual(slot.model, "picked-by-hand")

    def test_an_edit_after_a_restart_still_restores(self):
        # A restart empties the module's memory while the slot keeps the triage
        # model it persisted; the edit that clears the setting is what marks it.
        slot = _Slot(model=TRIAGE)
        cm.note_triage_change(slot, _crew(triage_model=TRIAGE), _crew())
        self.assertTrue(cm.apply_turn_model(_State(), slot, _crew(), []))
        self.assertEqual(slot.model, WORK)


class TestAPersonsPickWins(_Isolated):
    """The crew changes only a model it chose itself.

    A pick in the crew's session -- the model picker, or ``session_set_model`` --
    holds the slot until the crew's ``model`` or ``triage_model`` setting changes.
    """

    def _on_triage(self) -> _Slot:
        slot = _Slot()
        self.assertTrue(cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), []))
        self.assertEqual(slot.model, TRIAGE)
        return slot

    def test_a_hand_pick_survives_the_next_pass(self):
        slot = self._on_triage()
        _pick(slot, "claude-sonnet-4.5")
        crew = _crew(triage_model=TRIAGE)
        for _ in range(3):
            self.assertFalse(cm.apply_turn_model(_State({slot.key}), slot, crew, []))
        self.assertEqual(slot.model, "claude-sonnet-4.5")
        self.assertIsNone(slot._pending_reset_history_key)
        self.assertTrue(cm.is_held(crew))

    def test_a_hand_pick_survives_the_switch_back_to_the_work_model(self):
        slot = self._on_triage()
        _pick(slot, "claude-sonnet-4.5")
        crew = _crew(triage_model=TRIAGE)
        self.assertFalse(cm.apply_turn_model(_State(), slot, crew, [_item("implementing")]))
        self.assertEqual(slot.model, "claude-sonnet-4.5")

    def test_picking_one_of_the_crews_own_models_holds_too(self):
        # Told apart by the pick generation, not by the model's name: a person who
        # puts a cheap-turn crew on its work model by hand means it.
        slot = self._on_triage()
        _pick(slot, WORK)
        self.assertFalse(cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), []))
        self.assertEqual(slot.model, WORK)

    def test_the_crews_own_switches_still_happen(self):
        slot = self._on_triage()
        crew = _crew(triage_model=TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State(), slot, crew, [_item("claimed")]))
        self.assertEqual(slot.model, WORK)
        self.assertTrue(cm.apply_turn_model(_State(), slot, crew, [_item("awaiting-ci")]))
        self.assertEqual(slot.model, TRIAGE)
        self.assertFalse(cm.is_held(crew))

    def test_a_provider_switch_is_no_pick(self):
        # The Design finding: the provider switch bumps the generation too. Read as
        # a pick it would hold the crew on the agent default, its work model gone
        # for the acting turns as well, until someone pressed Switch back.
        slot = self._on_triage()
        _provider_switch(slot)
        crew = _crew(triage_model=TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State({slot.key}), slot, crew, []))
        self.assertEqual(slot.model, TRIAGE)
        self.assertFalse(cm.is_held(crew))
        self.assertFalse(cm.hold_to_record(crew))
        self.assertTrue(cm.apply_turn_model(_State(), slot, crew, [_item("claimed")]))
        self.assertEqual(slot.model, WORK)

    def test_a_provider_switch_on_the_work_model_puts_it_back(self):
        slot = self._on_triage()
        crew = _crew(triage_model=TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State(), slot, crew, [_item("implementing")]))
        _provider_switch(slot)
        self.assertTrue(cm.apply_turn_model(_State(), slot, crew, [_item("implementing")]))
        self.assertEqual(slot.model, WORK)
        self.assertFalse(cm.is_held(crew))

    def test_a_pick_after_a_provider_switch_still_holds(self):
        slot = self._on_triage()
        _provider_switch(slot)
        _pick(slot, "claude-sonnet-4.5")
        crew = _crew(triage_model=TRIAGE)
        self.assertFalse(cm.apply_turn_model(_State(), slot, crew, []))
        self.assertEqual(slot.model, "claude-sonnet-4.5")
        self.assertTrue(cm.is_held(crew))

    def test_switching_off_after_a_provider_switch_restores_the_work_model(self):
        slot = self._on_triage()
        _provider_switch(slot)
        self.assertTrue(cm.apply_turn_model(_State(), slot, _crew(), []))
        self.assertEqual(slot.model, WORK)

    def test_a_pin_the_runtime_respelled_is_still_the_crews(self):
        # A work model left on the agent default is written as "", and the turn
        # fills the pin with the id it was served -- with no pick generation bump.
        # That is not a pick, and it is already the work model: no rewrite, no reset.
        crew = _crew(model="", triage_model=TRIAGE)
        slot = _Slot(model=TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State(), slot, crew, [_item("claimed")]))
        self.assertEqual(slot.model, "")
        slot.model = "claude-opus-4.5"  # the runtime's backfill of the served id
        self.assertFalse(cm.apply_turn_model(_State({slot.key}), slot, crew, [_item("claimed")]))
        self.assertEqual(slot.model, "claude-opus-4.5")
        self.assertIsNone(slot._pending_reset_history_key)
        self.assertTrue(cm.apply_turn_model(_State(), slot, crew, []))
        self.assertEqual(slot.model, TRIAGE)

    def test_a_queued_session_set_model_pick_is_left_to_commit(self):
        # Switching now would bump the generation the pick was queued against, and
        # the commit would then drop it as overtaken.
        slot = self._on_triage()
        slot._pending_model_pick = object()  # type: ignore[attr-defined]
        crew = _crew(triage_model=TRIAGE)
        self.assertFalse(cm.apply_turn_model(_State(), slot, crew, [_item("claimed")]))
        self.assertEqual(slot.model, TRIAGE)
        self.assertEqual(slot._model_pick_gen, 1)

    def test_changing_the_triage_model_hands_the_slot_back(self):
        slot = self._on_triage()
        _pick(slot, "claude-sonnet-4.5")
        cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), [])
        edited = _crew(triage_model="claude-haiku-5")
        self.assertFalse(cm.is_held(edited))
        self.assertTrue(cm.apply_turn_model(_State(), slot, edited, []))
        self.assertEqual(slot.model, "claude-haiku-5")

    def test_changing_the_work_model_hands_the_slot_back(self):
        slot = self._on_triage()
        _pick(slot, "claude-sonnet-4.5")
        cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), [])
        edited = _crew(model="claude-opus-5", triage_model=TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State(), slot, edited, [_item("claimed")]))
        self.assertEqual(slot.model, "claude-opus-5")

    def test_an_edit_away_and_back_still_releases_it(self):
        slot = self._on_triage()
        _pick(slot, "claude-sonnet-4.5")
        crew = _crew(triage_model=TRIAGE)
        cm.apply_turn_model(_State(), slot, crew, [])
        away = _crew(triage_model="claude-haiku-5")
        cm.note_triage_change(slot, crew, away)
        cm.note_triage_change(slot, away, crew)
        self.assertFalse(cm.is_held(crew))
        self.assertTrue(cm.apply_turn_model(_State(), slot, crew, []))
        self.assertEqual(slot.model, TRIAGE)

    def test_the_record_keeps_the_hold_over_a_restart(self):
        # A restart empties process memory; the record's ``model_held`` remains.
        slot = _Slot(model="claude-sonnet-4.5")
        crew = _crew(triage_model=TRIAGE, model_held=True)
        self.assertFalse(cm.apply_turn_model(_State(), slot, crew, []))
        self.assertEqual(slot.model, "claude-sonnet-4.5")

    def test_a_hold_seen_here_is_owed_to_the_record_once(self):
        slot = self._on_triage()
        _pick(slot, "claude-sonnet-4.5")
        crew = _crew(triage_model=TRIAGE)
        self.assertFalse(cm.hold_to_record(crew))
        cm.apply_turn_model(_State(), slot, crew, [])
        self.assertTrue(cm.hold_to_record(crew))
        self.assertFalse(cm.hold_to_record({**crew, "model_held": True}))

    def test_a_hold_carries_the_release_count_it_was_seen_under(self):
        slot = self._on_triage()
        _pick(slot, "claude-sonnet-4.5")
        crew = _crew(triage_model=TRIAGE)
        self.assertIsNone(cm.hold_seen_at(crew))
        cm.apply_turn_model(_State(), slot, crew, [])
        self.assertEqual(cm.hold_seen_at(crew), cs.hold_releases("c_1"))
        cm.note_triage_change(slot, crew, crew, released=True)
        self.assertIsNone(cm.hold_seen_at(crew))

    def test_a_rebuilt_slot_is_not_a_pick(self):
        # A slot closed and resumed, or archived and rehydrated, is a new object
        # whose pick generation starts again at 0. That reset is nobody's pick.
        slot = self._on_triage()
        self.assertEqual(slot._model_pick_gen, 1)
        rebuilt = _Slot(model=TRIAGE)
        crew = _crew(triage_model=TRIAGE)
        self.assertFalse(cm.apply_turn_model(_State(), rebuilt, crew, []))
        self.assertFalse(cm.is_held(crew))
        self.assertTrue(cm.apply_turn_model(_State(), rebuilt, crew, [_item("claimed")]))
        self.assertEqual(rebuilt.model, WORK)
        # And a pick on the rebuilt object is still seen as one.
        _pick(rebuilt, "claude-sonnet-4.5")
        self.assertFalse(cm.apply_turn_model(_State(), rebuilt, crew, []))
        self.assertTrue(cm.is_held(crew))

    def test_switching_off_restores_a_rebuilt_slot_on_the_triage_model(self):
        slot = self._on_triage()
        rebuilt = _Slot(model=TRIAGE)
        self.assertTrue(cm.apply_turn_model(_State(), rebuilt, _crew(), []))
        self.assertEqual(rebuilt.model, WORK)
        del slot

    def test_switching_off_keeps_a_held_pick(self):
        slot = self._on_triage()
        _pick(slot, "claude-sonnet-4.5")
        cm.apply_turn_model(_State(), slot, _crew(triage_model=TRIAGE), [])
        before = _crew(triage_model=TRIAGE, model_held=True)
        cm.note_triage_change(slot, before, _crew())
        self.assertFalse(cm.apply_turn_model(_State(), slot, _crew(), []))
        self.assertEqual(slot.model, "claude-sonnet-4.5")

    def test_resume_switching_releases_the_hold(self):
        # The crew page's action: a patch of ``model_held: false`` with both models
        # unchanged. The next pass takes the slot on and switches it again.
        slot = self._on_triage()
        _pick(slot, "claude-sonnet-4.5")
        crew = _crew(triage_model=TRIAGE)
        cm.apply_turn_model(_State(), slot, crew, [])
        held = {**crew, "model_held": True}
        released = {**crew, "model_held": False}
        cm.note_triage_change(slot, held, released)
        self.assertFalse(cm.is_held(released))
        self.assertTrue(cm.apply_turn_model(_State(), slot, released, []))
        self.assertEqual(slot.model, TRIAGE)


#: Every writer in the tree that bumps ``_model_pick_gen``, by (file, function),
#: and what the bump means to the crew. "pick": a person's (or an agent's
#: ``session_set_model``) choice, which holds a crew's slot. "runtime": the
#: gateway's own write, which must stamp ``_model_runtime_pick_gen`` so the crew
#: does not read it as a pick. "crew": the crew's own switch.
PICK_GEN_WRITERS: dict[tuple[str, str], tuple[str, int]] = {
    ("dashboard/chat_api/slot_model.py", "api_chat_slot_model"): ("pick", 3),
    ("dashboard/chat_api/slot_model.py", "api_chat_slots_model"): ("pick", 1),
    # Settings' "follow the default" press: a person's choice for their own chats.
    # A crew's slot is app-owned, so the press never counts or clears it.
    ("dashboard/chat_follow_default.py", "_clear_loaded"): ("pick", 1),
    ("dashboard/session_control_parts/session_create.py", "create_session"): ("pick", 1),
    # A person's harness pick for a crewmate clears its DM chat's model pick.
    ("dashboard/agent_admin/crew_update.py", "_reload"): ("pick", 1),
    ("dashboard/session_control_parts/model_switch.py", "apply_pending_model_pick"): ("pick", 1),
    ("dashboard/server_runtime/config_watch.py", "_switch_provider"): ("runtime", 1),
    ("apps/builtins/issue_radar/backend/crew_model.py", "apply_turn_model"): ("crew", 1),
}


class _WriterVisitor(ast.NodeVisitor):
    """Charges each bump / stamp to the innermost enclosing function."""

    def __init__(self, rel: str, found: dict[tuple[str, str], list[Any]]) -> None:
        self.rel = rel
        self.found = found
        self.stack: list[str] = []

    def _func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        target = node.target
        if (
            isinstance(node.op, ast.Add)
            and isinstance(target, ast.Attribute)
            and target.attr == "_model_pick_gen"
        ):
            self.found.setdefault((self.rel, self.stack[-1] if self.stack else ""), [0, False])[
                0
            ] += 1
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if any(
            isinstance(t, ast.Attribute) and t.attr == "_model_runtime_pick_gen"
            for t in node.targets
        ):
            self.found.setdefault((self.rel, self.stack[-1] if self.stack else ""), [0, False])[
                1
            ] = True
        self.generic_visit(node)


def _pick_gen_writers() -> dict[tuple[str, str], tuple[int, bool]]:
    """(file, innermost function) -> (bumps of ``_model_pick_gen``, stamps the runtime gen)."""
    root = Path(kiro_crew.__file__).parent
    found: dict[tuple[str, str], list[Any]] = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "_model_pick_gen" in text:
            _WriterVisitor(path.relative_to(root).as_posix(), found).visit(ast.parse(text))
    return {key: (count, stamps) for key, (count, stamps) in found.items() if count}


class TestPickGenerationWriters(unittest.TestCase):
    """The crew reads a moved pick generation as a person's pick (the Design finding).

    So every writer that bumps it has to be accounted for: a new one fails here
    until it is classified, and a runtime writer must stamp its generation.
    """

    def test_every_writer_is_classified(self):
        found = _pick_gen_writers()
        self.assertEqual(
            {key: count for key, (count, _stamps) in found.items()},
            {key: count for key, (_kind, count) in PICK_GEN_WRITERS.items()},
            "a writer of _model_pick_gen was added, moved or removed: classify it in "
            "PICK_GEN_WRITERS, and if it is the runtime's own write, stamp "
            "_model_runtime_pick_gen beside the bump",
        )

    def test_every_runtime_writer_stamps_its_generation(self):
        found = _pick_gen_writers()
        for key, (kind, _count) in PICK_GEN_WRITERS.items():
            with self.subTest(writer=key):
                self.assertEqual(found[key][1], kind == "runtime")


class TestTriageInEffect(_Isolated):
    """Whether a turn may be told to stop short: only while the crew really switches."""

    def test_in_effect_when_set_and_usable(self):
        self.assertTrue(cm.triage_in_effect(_crew(triage_model=TRIAGE)))

    def test_not_in_effect_unset_or_equal_to_the_work_model(self):
        self.assertFalse(cm.triage_in_effect(_crew()))
        self.assertFalse(cm.triage_in_effect(_crew(triage_model=WORK)))

    def test_not_in_effect_when_withheld(self):
        cm._withheld["c_1"] = TRIAGE
        self.assertFalse(cm.triage_in_effect(_crew(triage_model=TRIAGE)))

    def test_not_in_effect_when_held(self):
        self.assertFalse(cm.triage_in_effect(_crew(triage_model=TRIAGE, model_held=True)))
        cm._held["c_1"] = (WORK, TRIAGE)
        self.assertFalse(cm.triage_in_effect(_crew(triage_model=TRIAGE)))


class TestWithheldVerdictReset(_Isolated):
    def test_switching_off_and_back_on_retries_a_withheld_model(self):
        slot = _Slot(model=TRIAGE)
        slot.model_withheld = True
        crew = _crew(triage_model=TRIAGE)
        cm.apply_turn_model(_State(), slot, crew, [])
        self.assertEqual(cm.unavailable_triage_model(crew), TRIAGE)
        cm.note_triage_change(slot, crew, _crew())
        cm.note_triage_change(slot, _crew(), crew)
        self.assertEqual(cm.unavailable_triage_model(crew), "")
        self.assertEqual(cm.turn_model(crew, []), TRIAGE)

    def test_the_verdict_is_cleared_without_a_resident_slot(self):
        cm._withheld["c_1"] = TRIAGE
        cm.note_triage_change(None, _crew(), _crew(triage_model=TRIAGE))
        self.assertEqual(cm.unavailable_triage_model(_crew(triage_model=TRIAGE)), "")


def test_a_session_set_model_pick_survives_the_crews_passes(tmp_path, monkeypatch):
    """The real pick path end to end: queued by the verb, committed at turn start."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard.chat_utils import slot_history_key

    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    for store in (cm._withheld, cm._written, cm._held, cm._hold_seen_at):
        store.clear()
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    slot = state.get_or_create_slot("crew-c_1", model=WORK)
    crew = _crew(triage_model=TRIAGE)
    try:
        assert cm.apply_turn_model(state, slot, crew, []) is True
        assert slot.model == TRIAGE
        out = asyncio.run(
            sc.set_model_target(
                state,
                caller_session_key=slot_history_key(caller),
                target="crew-c_1",
                model="sonnet",
            )
        )
        picked = out["model"]
        assert picked not in ("", TRIAGE, WORK)
        # Queued, not yet committed: the crew leaves it to commit.
        assert cm.apply_turn_model(state, slot, crew, [_item("claimed")]) is False
        assert sc.apply_pending_model_pick(state, slot) is True
        assert slot.model == picked
        # Committed: a watchdog pass with work to do, and one without, both keep it.
        assert cm.apply_turn_model(state, slot, crew, [_item("claimed")]) is False
        assert cm.apply_turn_model(state, slot, crew, []) is False
        assert slot.model == picked
        assert cm.is_held(crew)
    finally:
        for store in (cm._withheld, cm._written, cm._held, cm._hold_seen_at):
            store.clear()


class TestStoreField(unittest.TestCase):
    def test_a_record_without_the_field_reads_as_unset(self):
        self.assertEqual(cs._coerce_crew({"id": "c_1", "name": "Leo"})["triage_model"], "")

    def test_a_record_without_a_hold_reads_as_not_held(self):
        self.assertIs(cs._coerce_crew({"id": "c_1", "name": "Leo"})["model_held"], False)
        rec = cs._coerce_crew({"id": "c_1", "name": "Leo", "model_held": "yes"})
        self.assertIs(rec["model_held"], False)

    def test_a_patch_cannot_set_the_hold(self):
        self.assertEqual(cs._validated_crew_patch({"model_held": True}), {})
        self.assertEqual(cs._validated_crew_patch({"model_held": "false"}), {})

    def test_a_patch_can_release_the_hold(self):
        self.assertEqual(cs._validated_crew_patch({"model_held": False}), {"model_held": False})


class TestStoredHold(unittest.TestCase):
    """The hold on the crew record: set compare-and-set, released by a model edit."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.crew = cs.create_crew(
            "o", "r", {"name": "Leo", "model": WORK, "triage_model": TRIAGE}, self.root
        )

    def _hold(self, seen: int | None = None, **settings: str) -> bool:
        return cs.set_crew_model_held(
            "o",
            "r",
            self.crew["id"],
            model=settings.get("model", WORK),
            triage_model=settings.get("triage_model", TRIAGE),
            seen_releases=cs.hold_releases(self.crew["id"]) if seen is None else seen,
            root=self.root,
        )

    def _held(self) -> bool:
        return cs.read_crew("o", "r", self.crew["id"], self.root)["model_held"]

    def test_a_hold_is_written_once(self):
        self.assertTrue(self._hold())
        self.assertTrue(self._held())
        self.assertFalse(self._hold(), "already held: nothing to write")

    def test_a_hold_write_queued_before_a_release_does_not_undo_it(self):
        """Queue a hold write, release, then flush: the release wins.

        The second write is what a pass reading a snapshot from before the first
        write landed queues; "Switch back to triage model" lands while it waits.
        """
        self.assertTrue(self._hold())
        queued = cs.hold_releases(self.crew["id"])
        cs.update_crew("o", "r", self.crew["id"], {"model_held": False}, self.root)
        self.assertFalse(self._hold(seen=queued))
        self.assertFalse(self._held())
        # A pick seen after the release is a new hold, and it is written.
        self.assertTrue(self._hold())
        self.assertTrue(self._held())

    def test_a_release_of_a_hold_not_yet_written_refuses_the_queued_write(self):
        queued = cs.hold_releases(self.crew["id"])
        cs.update_crew("o", "r", self.crew["id"], {"model_held": False}, self.root)
        self.assertFalse(self._hold(seen=queued))
        self.assertFalse(self._held())

    def test_an_edit_that_releases_nothing_does_not_refuse_a_queued_write(self):
        queued = cs.hold_releases(self.crew["id"])
        cs.update_crew("o", "r", self.crew["id"], {"extra_prompt": "be brief"}, self.root)
        cs.set_crew_paused("o", "r", self.crew["id"], True, "lunch", self.root)
        self.assertTrue(self._hold(seen=queued))

    def test_a_hold_seen_under_older_settings_is_not_written(self):
        self.assertFalse(self._hold(triage_model="claude-haiku-5"))
        self.assertFalse(self._held())

    def test_a_model_edit_releases_it(self):
        for patch in ({"triage_model": ""}, {"triage_model": "claude-haiku-5"}, {"model": "x-1"}):
            with self.subTest(patch=patch):
                self.crew = cs.update_crew("o", "r", self.crew["id"], patch, self.root)
                self.assertTrue(self._hold(**{k: self.crew[k] for k in ("model", "triage_model")}))
                self.crew = cs.update_crew(
                    "o", "r", self.crew["id"], {"model": WORK, "triage_model": TRIAGE}, self.root
                )
                self.assertFalse(self._held())

    def test_an_edit_that_leaves_the_models_alone_keeps_it(self):
        self._hold()
        cs.update_crew("o", "r", self.crew["id"], {"extra_prompt": "be brief"}, self.root)
        cs.set_crew_paused("o", "r", self.crew["id"], True, "lunch", self.root)
        self.assertTrue(self._held())

    def test_a_valid_id_is_kept_and_trimmed(self):
        self.assertEqual(
            cs._validated_crew_patch({"triage_model": f"  {TRIAGE} "}), {"triage_model": TRIAGE}
        )

    def test_empty_switches_it_off(self):
        self.assertEqual(cs._validated_crew_patch({"triage_model": ""}), {"triage_model": ""})

    def test_an_id_the_picker_would_refuse_is_dropped(self):
        for bad in ("two words", "-leading-dash", 7, None, "x" * 300):
            with self.subTest(bad=bad):
                self.assertEqual(cs._validated_crew_patch({"triage_model": bad}), {})

    def test_a_hand_edited_bad_id_reads_as_unset(self):
        rec = cs._coerce_crew({"id": "c_1", "name": "Leo", "triage_model": "not a model"})
        self.assertEqual(rec["triage_model"], "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
