"""Every sigil-less plan action must be one the marker strip refuses.

The sigil guard is a PREFIX test, so a plan chip carrying no sigil is invisible to it.
Both runtimes dispatch these by casefolded equality -- the orchestrator's own validator
and the frontend's ``isPlanAction`` -- and the orchestrator's ``go all`` flips a slot into
unattended per-stage auto-approval. So stripping a marker off ``(recommended) Go All``
would promote an inert label into that dispatch, which the raw label never reaches
because it opens with ``(``.

``_RESERVED_PLAN_ACTIONS`` is a literal because ``constants`` cannot import the
orchestrator. This test is what keeps the literal honest, in both directions and against
both runtimes, so a fourth plan action cannot land without the guard learning about it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.constants import _RESERVED_PLAN_ACTIONS, strip_recommended_marker

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = ROOT / "src" / "kiro_crew" / "dashboard" / "chat_orchestrator.py"
FRONTEND_HOOK = ROOT / "website" / "src" / "hooks" / "usePlanActionMutation.ts"
FRONTEND_GRAMMAR = ROOT / "website" / "src" / "app-sdk" / "protocol" / "recommendation.ts"


def _backend_dispatch_actions() -> set[str]:
    """What the orchestrator validates an incoming plan action against.

    It reads :data:`_RESERVED_PLAN_ACTIONS` directly, so the dispatcher and the marker
    guard cannot hold different lists -- there is only one. :func:`_orchestrator_holds_no_
    private_list` is what keeps that true; without it this function would be circular.
    """
    return {a.casefold() for a in _RESERVED_PLAN_ACTIONS}


def _orchestrator_inline_literals() -> list[str]:
    """Plan-action tuples spelled out in the dispatcher instead of imported."""
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    return re.findall(r"if action not in \(([^)]*)\):", text)


def _frontend_dispatch_actions() -> set[str]:
    """The values ``isPlanAction`` compares a clicked label against."""
    text = FRONTEND_HOOK.read_text(encoding="utf-8")
    body = text[text.index("export function isPlanAction") :]
    return {v.casefold() for v in re.findall(r"v === '([^']+)'", body[:400])}


class TestThePlanActionListIsHonest:
    def test_the_scans_find_something(self):
        # Vacuity guard: an empty scan would satisfy every assertion below.
        assert _backend_dispatch_actions(), "backend scan found no plan actions"
        assert _frontend_dispatch_actions(), "frontend scan found no plan actions"

    def test_the_orchestrator_holds_no_private_plan_action_list(self):
        """The drift this guards is a dispatcher growing an action the strip never learns.

        Sharing one frozenset makes that unrepresentable, but only while the dispatcher
        keeps reading it -- an inline tuple here would silently restore the two-list shape
        a parity test can merely detect after the fact.
        """
        text = ORCHESTRATOR.read_text(encoding="utf-8")
        assert "_RESERVED_PLAN_ACTIONS" in text, (
            "the orchestrator no longer reads the shared plan-action set, so the marker "
            "guard and the dispatcher can now disagree"
        )
        assert not _orchestrator_inline_literals(), (
            f"the orchestrator re-lists plan actions inline as "
            f"{_orchestrator_inline_literals()!r}; import _RESERVED_PLAN_ACTIONS instead"
        )

    def test_every_backend_dispatch_action_is_declared(self):
        undeclared = _backend_dispatch_actions() - set(_RESERVED_PLAN_ACTIONS)
        assert not undeclared, (
            f"the orchestrator dispatches {sorted(undeclared)} but the marker strip would "
            "promote a label into it -- add it to _RESERVED_PLAN_ACTIONS"
        )

    def test_every_frontend_dispatch_action_is_declared(self):
        undeclared = _frontend_dispatch_actions() - set(_RESERVED_PLAN_ACTIONS)
        assert (
            not undeclared
        ), f"isPlanAction dispatches {sorted(undeclared)} and is undeclared: {sorted(undeclared)}"

    def test_the_two_runtimes_agree(self):
        assert _backend_dispatch_actions() == _frontend_dispatch_actions()

    def test_nothing_is_reserved_that_nobody_dispatches(self):
        # A reserved word no dispatcher matches would suppress a badge for no reason.
        # Keyed on the FRONTEND scan: the backend set is now the same object, so testing
        # it against itself would pass on any content.
        spurious = set(_RESERVED_PLAN_ACTIONS) - _frontend_dispatch_actions()
        assert not spurious, sorted(spurious)

    @pytest.mark.parametrize("action", sorted(_RESERVED_PLAN_ACTIONS))
    def test_a_marked_plan_action_is_left_verbatim(self, action):
        for spelling in (action, action.upper(), action.title()):
            label = f"(recommended) {spelling}"
            assert strip_recommended_marker(label) == label, spelling

    def test_a_plan_word_inside_a_longer_label_still_strips(self):
        assert (
            strip_recommended_marker("(recommended) Go all the way back") == "Go all the way back"
        )

    def test_the_frontend_grammar_declares_the_same_guard(self):
        text = FRONTEND_GRAMMAR.read_text(encoding="utf-8")
        m = re.search(r"RESERVED_PLAN_ACTION_RE = /\^\(\?:([^)]*)\)\$/(\w*)", text)
        assert m, "the frontend plan-action guard is missing -- the runtimes have drifted"
        declared = {v.strip().casefold() for v in m.group(1).split("|") if v.strip()}
        assert declared == set(_RESERVED_PLAN_ACTIONS), sorted(declared)
        # Case-insensitive because the orchestrator casefolds, and NOT global: a `g` flag
        # would carry `lastIndex` between calls and let every other marked label through.
        assert "i" in m.group(2), m.group(2)
        assert "g" not in m.group(2), "a global flag makes the guard stateful"
