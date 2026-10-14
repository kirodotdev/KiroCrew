"""The poisoned-conversation escalation has one owner.

The streak of pre-stream-exhausted cycles and the discard one-shot cross turns
and are two cells on a chat slot. Every transition goes through
``PoisonedConversationEscalation``; the runner calls its seams instead of writing
the cells. The first half pins the state machine, the second pins that nothing
in the package but the escalation (and the slot's ``__slots__`` and constructor)
writes a cell or names one in a string, so the recovery-replay ledger's refund
goes through the escalation too.
"""

from __future__ import annotations

import ast
from types import SimpleNamespace

import pytest
from source_corpus import parsed_candidates, src_root

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.poisoned_escalation import (
    POISONED_SESSION_CYCLES,
    PoisonedConversationEscalation,
)
from kiro_crew.dashboard.recovery_replays import POLICIES, ReplayFamily
from kiro_crew.dashboard.state import _ChatSlot

_CELLS = frozenset({"_prestream_exhausted_cycles", "_poisoned_reset_used"})


def _slot(cycles: int = 0, spent: bool = False) -> SimpleNamespace:
    return SimpleNamespace(_prestream_exhausted_cycles=cycles, _poisoned_reset_used=spent)


def _exhaust(escalation: PoisonedConversationEscalation, times: int) -> None:
    for _ in range(times):
        escalation.record_terminal_failure(emitted=False, thought=False, transient=True)


# ---------------------------------------------------------------- the state machine


def test_the_cells_are_slot_fields_and_the_runner_reads_the_same_threshold() -> None:
    assert _CELLS <= set(_ChatSlot.__slots__)
    assert cr.POISONED_SESSION_CYCLES == POISONED_SESSION_CYCLES == 2


def test_a_silent_transient_failure_extends_the_streak() -> None:
    slot = _slot()
    escalation = PoisonedConversationEscalation(slot)

    _exhaust(escalation, 3)

    assert escalation.exhausted_cycles == slot._prestream_exhausted_cycles == 3


@pytest.mark.parametrize(
    ("emitted", "thought", "transient"),
    [(True, False, True), (False, True, True), (False, False, False)],
    ids=["output", "thinking", "non-transient"],
)
def test_any_other_terminal_failure_breaks_the_streak(
    emitted: bool, thought: bool, transient: bool
) -> None:
    slot = _slot(cycles=1)
    escalation = PoisonedConversationEscalation(slot)

    escalation.record_terminal_failure(emitted=emitted, thought=thought, transient=transient)

    assert slot._prestream_exhausted_cycles == 0
    assert not escalation.should_probe()


def test_a_probe_is_due_at_the_threshold_only_while_the_one_shot_is_unspent() -> None:
    escalation = PoisonedConversationEscalation(_slot())

    _exhaust(escalation, POISONED_SESSION_CYCLES - 1)
    assert not escalation.should_probe()
    _exhaust(escalation, 1)
    assert escalation.should_probe()

    spent = PoisonedConversationEscalation(_slot(cycles=POISONED_SESSION_CYCLES, spent=True))
    assert not spent.should_probe()


@pytest.mark.parametrize(
    ("emitted", "thought", "breaks"),
    [(True, False, True), (False, True, True), (False, False, False)],
    ids=["output", "thinking", "silent"],
)
def test_activity_breaks_the_streak_and_never_rearms(
    emitted: bool, thought: bool, breaks: bool
) -> None:
    slot = _slot(cycles=2, spent=True)

    PoisonedConversationEscalation(slot).record_activity(emitted=emitted, thought=thought)

    assert slot._prestream_exhausted_cycles == (0 if breaks else 2)
    assert slot._poisoned_reset_used is True


def test_a_landed_turn_resets_the_streak_and_rearms_the_one_shot() -> None:
    slot = _slot(cycles=3, spent=True)
    escalation = PoisonedConversationEscalation(slot)

    escalation.record_landed_turn()

    assert (escalation.exhausted_cycles, escalation.one_shot_spent) == (0, False)


@pytest.mark.parametrize(("clear_streak", "left"), [(False, 2), (True, 0)])
def test_a_discard_spends_the_one_shot_and_clears_the_streak_only_when_asked(
    clear_streak: bool, left: int
) -> None:
    slot = _slot(cycles=2)
    escalation = PoisonedConversationEscalation(slot)

    escalation.arm_discard(clear_streak=clear_streak)

    assert escalation.one_shot_spent is True
    assert slot._prestream_exhausted_cycles == left
    assert not escalation.should_probe()


def test_a_refund_rearms_the_one_shot_and_leaves_the_streak() -> None:
    slot = _slot(cycles=1, spent=True)
    escalation = PoisonedConversationEscalation(slot)

    escalation.refund_one_shot()

    assert (escalation.exhausted_cycles, escalation.one_shot_spent) == (1, False)


@pytest.mark.parametrize("family", [ReplayFamily.IMAGE_HISTORY, ReplayFamily.THINKING_BINDING])
def test_a_cancelled_replay_refunds_the_one_shot_through_the_escalation(
    family: ReplayFamily,
) -> None:
    """The ledger's policy asks for the refund; the runner's re-arm has the owner do it."""
    policy = POLICIES[family]

    assert policy.refunds_discard_one_shot
    for refunds in (policy.drain_refunds, policy.consume_refunds):
        slot = _slot(cycles=0, spent=True)
        cr._rearm_allowances(slot, refunds, discard_one_shot=policy.refunds_discard_one_shot)
        assert PoisonedConversationEscalation(slot).one_shot_spent is False


# ------------------------------------------------------------------- the one owner

_OWNER = "dashboard/poisoned_escalation.py"
_CONSTRUCTOR = "dashboard/state_parts/chat_slot.py"
#: ``_ChatSlot.__init__`` sets the cells' defaults through this collaborator's
#: ``init_ladder_fields`` (the class binds its collaborators, see state_parts/__init__.py).
_DEFAULTS = ("dashboard/state_parts/slot_ladder.py", "init_ladder_fields")


def _cell_writes_and_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Every store to a cell in *tree*, and every string that names one, as ``(cell, line)``.

    A string naming a cell is how a write reaches it with no attribute store in
    sight: ``setattr(slot, "<cell>", v)``, or a refund table of ``("<cell>", v)``
    pairs a loop hands to ``setattr``. Outside the slot's ``__slots__`` nothing
    needs one, so every such string counts, a ``getattr`` read included.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _CELLS:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                found.append((node.attr, node.lineno))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in _CELLS:
                found.append((node.value, node.lineno))
    return sorted(found)


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("slot._poisoned_reset_used = True", True),
        ("slot._prestream_exhausted_cycles += 1", True),
        ("slot._prestream_exhausted_cycles: int = 0", True),
        ("a, slot._poisoned_reset_used = 1, True", True),
        ("x = y = slot._prestream_exhausted_cycles = 0", True),
        ("del slot._poisoned_reset_used", True),
        ("setattr(slot, '_poisoned_reset_used', False)", True),
        ("refunds = (('_poisoned_reset_used', False),)", True),
        ("getattr(slot, '_prestream_exhausted_cycles')", True),
        ("if slot._poisoned_reset_used: pass", False),
        ("n = slot._prestream_exhausted_cycles + 1", False),
        ("setattr(slot, attr, False)", False),
    ],
)
def test_the_write_scan_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_cell_writes_and_names(ast.parse(source))) is flagged


def _declaration_lines(tree: ast.Module) -> set[int]:
    """The lines of ``_ChatSlot.__slots__`` and ``_ChatSlot.__init__``, which declare the cells."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "_ChatSlot":
            declarations = [
                item
                for item in node.body
                if (isinstance(item, ast.FunctionDef) and item.name == "__init__")
                or (
                    isinstance(item, ast.Assign)
                    and [getattr(t, "id", None) for t in item.targets] == ["__slots__"]
                )
            ]
            assert len(declarations) == 2, "_ChatSlot.__slots__ or __init__ not found"
            return {
                line
                for item in declarations
                for line in range(item.lineno, (item.end_lineno or item.lineno) + 1)
            }
    raise AssertionError("_ChatSlot not found")


def _function_lines(tree: ast.Module, name: str) -> set[int]:
    """The lines of the module-level function *name*."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    raise AssertionError(f"{name} not found")


def test_only_the_escalation_and_the_slot_declaration_write_or_name_the_cells() -> None:
    root = src_root()
    strays: list[str] = []
    scanned: set[str] = set()
    for path, _text, tree in parsed_candidates(
        require_any=tuple(sorted(_CELLS)), skip_syntax_errors=False
    ):
        rel = path.relative_to(root).as_posix()
        scanned.add(rel)
        hits = _cell_writes_and_names(tree)
        if rel == _OWNER:
            assert hits, "the scan found no write in the escalation itself"
            continue
        if rel == _CONSTRUCTOR:
            declared = _declaration_lines(tree)
            hits = [(cell, line) for cell, line in hits if line not in declared]
        elif rel == _DEFAULTS[0]:
            declared = _function_lines(tree, _DEFAULTS[1])
            hits = [(cell, line) for cell, line in hits if line not in declared]
        strays.extend(f"{rel}:{line} writes or names {cell}" for cell, line in hits)

    assert {_OWNER, _CONSTRUCTOR, _DEFAULTS[0]} <= scanned
    assert strays == []
